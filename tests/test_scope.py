"""Vendor replay paths and callbacks must not leak across concurrent requests."""
import concurrent.futures
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from replay import pipeline, scope, util


class VendorScope(unittest.TestCase):
    def test_default_paths_are_backwards_compatible(self):
        self.assertEqual(util.camp_dir('42'), util.RAW / 'campaigns/42')
        self.assertEqual(util.out_dir('42'), util.OUT / '42')
        self.assertEqual(pipeline.state_path('42'), pipeline.STATE / '42.json')

    def test_scoped_outputs_are_separate_but_inventory_is_shared(self):
        with scope.use('42', '17'):
            self.assertEqual(util.camp_dir('42'), util.RAW / 'campaigns/42/vendors/17')
            self.assertEqual(util.base_camp_dir('42'), util.RAW / 'campaigns/42')
            self.assertEqual(util.out_dir('42'), util.OUT / '42/vendors/17')
            self.assertEqual(pipeline.state_path('42'), pipeline.STATE / '42/vendors/17/state.json')
            self.assertEqual(util.camp_dir('99'), util.RAW / 'campaigns/99')
        self.assertIsNone(scope.vendor_for('42'))

    def test_nested_scope_and_exceptions_restore_the_previous_request(self):
        with scope.use('42', '17'):
            try:
                with scope.use('42', '18'):
                    self.assertEqual(scope.vendor_for('42'), '18')
                    raise RuntimeError('example')
            except RuntimeError:
                pass
            self.assertEqual(scope.vendor_for('42'), '17')
            with scope.use('42'):
                self.assertIsNone(scope.vendor_for('42'))
            self.assertEqual(scope.vendor_for('42'), '17')
        self.assertIsNone(scope.vendor_for('42'))

    def test_scope_propagates_to_parallel_ai_callbacks(self):
        def read(_):
            return str(util.camp_dir('42'))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            with scope.use('42', '17'):
                results = list(scope.thread_map(pool, read, range(8)))
            self.assertTrue(all(x.endswith('/42/vendors/17') for x in results))
            self.assertIsNone(pool.submit(scope.vendor_for, '42').result())

    def test_captured_callbacks_can_run_concurrently_without_reentering_context(self):
        with scope.use('42', '17'):
            callback = scope.bind(lambda _: scope.vendor_for('42'))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            self.assertEqual(list(pool.map(callback, range(16))), ['17'] * 16)
        self.assertIsNone(scope.vendor_for('42'))

    def test_concurrent_requests_never_overwrite_each_others_material(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(util, 'RAW', pathlib.Path(tmp)):
            def write_one(key):
                with scope.use('42', key):
                    file = util.camp_dir('42') / 'result.json'
                    util.write_json(file, {'vendor':key})
                    return file
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                a, b = list(pool.map(write_one, ['17', '18']))
            self.assertNotEqual(a, b)
            self.assertEqual(util.read_json(a), {'vendor':'17'})
            self.assertEqual(util.read_json(b), {'vendor':'18'})
            self.assertFalse((util.camp_dir('42') / 'result.json').exists())

    def test_state_reader_supports_an_explicit_vendor(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, 'STATE', pathlib.Path(tmp)):
            util.write_json(pipeline.state_path('42', '17'), {'running':True})
            self.assertTrue(pipeline.read_state('42', '17')['running'])
            self.assertEqual(pipeline.read_state('42'), {})

    def test_queue_cache_is_isolated_by_vendor_and_attribution_version(self):
        from replay import jobqueue as Q
        messages = [{'role':'system', 'content':'Return {"items": []}'},
                    {'role':'user', 'content':'Example material'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(Q, 'DB_PATH', pathlib.Path(tmp) / 'queue.db'):
            with scope.use('42', '17'):
                first = Q.enqueue(messages, campaign='42', namespace='identity-v1')
                self.assertEqual(first, Q.enqueue(messages, campaign='42', namespace='identity-v1'))
                revised = Q.enqueue(messages, campaign='42', namespace='identity-v2')
            with scope.use('42', '18'):
                other = Q.enqueue(messages, campaign='42', namespace='identity-v1')
            total = Q.enqueue(messages, campaign='42', namespace='identity-v1')
            self.assertEqual(len({first, revised, other, total}), 4)
            con = Q._connect()
            try:
                rows = {r['id']:r['vendor_id'] for r in con.execute('SELECT id,vendor_id FROM jobs')}
            finally:
                con.close()
            self.assertEqual(rows[first], '17')
            self.assertEqual(rows[other], '18')
            self.assertIsNone(rows[total])

    def test_queue_callback_remembers_scope_even_in_a_plain_thread(self):
        from replay import jobqueue as Q
        with tempfile.TemporaryDirectory() as tmp, patch.object(util, 'RAW', pathlib.Path(tmp)):
            with scope.use('42', '17'):
                util.write_json(util.camp_dir('42') / 'corpus.json', {
                    'built_at':'2026-01-01T00:00:00Z', 'attribution_version':'identity-v1', 'records':[]})
                ask = Q.asker('42', 'example')
            seen = []
            def enqueue(messages, **kw):
                seen.append((scope.vendor_for('42'), kw['namespace'], kw['data_fetched_at']))
                return 'example-job'
            with patch.object(Q, 'enqueue', enqueue), patch.object(Q, 'wait', return_value=('{}', {})):
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    pool.submit(ask, []).result()
            self.assertEqual(seen, [('17', 'identity-v1', '2026-01-01T00:00:00Z')])
            self.assertIsNone(scope.vendor_for('42'))

    def test_vendor_identifiers_cannot_be_paths_or_unbounded_strings(self):
        for value in ('../17', '17/18', '0', '017', '-1', 'x' * 500, '<script>', '17?x=1', '17\x00', 'v-xxx'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scope.valid_vendor(value)
        self.assertEqual(scope.valid_vendor('v-123456abcdef'), 'v-123456abcdef')
        for value in (None, '', 'all'):
            self.assertIsNone(scope.valid_vendor(value))
