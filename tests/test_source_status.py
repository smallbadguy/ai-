import copy
import json
import pathlib
import re
import shutil
import subprocess
import unittest

from replay import source_status as S


class SourceReadTests(unittest.TestCase):
    def corpus(self, scan):
        return {'source': 'direct', 'stats': {'source_scan': {'email': scan}}}

    def case(self):
        return {'events': [], 'sources': [], 'charter': [{'t': 'Gmail 邮件已读', 'ok': False}]}

    def test_successful_read_with_zero_attributed_emails_is_checked(self):
        case = self.case()
        S.annotate(case, self.corpus({'ok': True, 'as_of': '2026-11-02T09:10:45Z'}), 'zh')
        self.assertEqual(case['source_reads']['email']['status'], 'ok')
        self.assertIn('收录 0 条', case['source_reads']['email']['note'])
        self.assertEqual(case['charter'], [{'t': 'Gmail 邮件已读', 'ok': True}])
        self.assertEqual(case['sources'], [])  # no fabricated email source/events
        self.assertEqual(case['events'], [])

    def test_failed_refresh_with_old_timestamp_is_not_checked(self):
        case = self.case()
        S.annotate(case, self.corpus({'ok': False, 'as_of': '2026-10-01T00:00:00Z'}), 'zh')
        self.assertEqual(case['source_reads']['email']['status'], 'error')
        self.assertFalse(case['charter'][0]['ok'])
        self.assertIn('读取失败', case['charter'][0]['t'])

    def test_timestamp_without_success_receipt_does_not_claim_read(self):
        case = self.case()
        doc = {'source': 'direct', 'stats': {'brain_scan': {'emails': {'source': 'direct', 'as_of': '2026-11-02'}}}}
        S.annotate(case, doc, 'zh')
        self.assertEqual(case['source_reads']['email']['status'], 'unknown')
        self.assertFalse(case['charter'][0]['ok'])

    def test_events_are_not_read_receipts(self):
        case = self.case()
        case['events'] = [{'src': 'email', 'quote': 'Retained source text'}]
        S.annotate(case, self.corpus({}), 'zh')
        self.assertEqual(case['source_reads']['email']['status'], 'unknown')
        self.assertFalse(case['charter'][0]['ok'])

    def test_legacy_scan_can_be_empty_successful_or_partial(self):
        for failed, state in ((0, 'ok'), (2, 'limited')):
            with self.subTest(failed=failed):
                doc = {'source': 'brain', 'stats': {'brain_scan': {'emails': {'listed': 0, 'fetched': 0, 'failed': failed}}}}
                case = self.case()
                S.annotate(case, doc, 'zh')
                self.assertEqual(case['source_reads']['email']['status'], state)
                self.assertEqual(case['charter'][0]['ok'], failed == 0)

    def test_receipts_omit_upstream_errors_and_private_fields(self):
        receipt = S.receipts({'emails': {'ok': False, 'as_of': 'old', 'error': 'PRIVATE_VALUE', 'api_key': 'PRIVATE_VALUE'}})
        self.assertEqual(receipt['email'], {'ok': False, 'as_of': 'old'})
        self.assertNotIn('PRIVATE_VALUE', json.dumps(receipt))

    def test_only_labels_change_and_success_restores_a_failed_label(self):
        case = self.case()
        case.update(meta={'campaign_id': 4947}, presentation={'metrics': [1, 2]}, client_background=[{'text': 'uncertain'}])
        case['events'] = [{'src': 'slack', 'text': 'unchanged'}]
        original = copy.deepcopy(case)
        S.annotate(case, self.corpus({'ok': False}), 'zh')
        S.annotate(case, self.corpus({'ok': True, 'as_of': '2026-11-02'}), 'zh')
        for key in ('meta', 'presentation', 'client_background', 'events'):
            self.assertEqual(case[key], original[key])
        self.assertEqual(case['charter'], [{'t': 'Gmail 邮件已读', 'ok': True}])

    def test_english_labels_have_no_chinese_and_meetings_use_own_receipt(self):
        case = {'events': [], 'charter': [{'t': 'Gmail read', 'ok': False}]}
        scan = S.receipts({kind: {'ok': True, 'as_of': '2026-11-02'} for kind in S.KINDS.values()})
        S.annotate(case, {'source': 'direct', 'stats': {'source_scan': scan}}, 'en')
        self.assertNotRegex(json.dumps(case, ensure_ascii=False), r'[\u3400-\u9fff]')
        self.assertNotIn('Teams', case['source_reads']['meeting']['name'])
        self.assertEqual(case['source_reads']['meeting']['status'], 'ok')
        self.assertTrue(case['charter'][0]['ok'])

    @unittest.skipUnless(shutil.which('node'), 'node is required for label rendering')
    def test_header_chip_uses_receipt_and_escapes_its_tooltip(self):
        template = (pathlib.Path(__file__).resolve().parents[1] / 'src/template.html').read_text()
        helper = template[template.index('const sourceReadChip ='):template.index('const sysChip =')]
        script = '''const D = {source_reads:{}};
const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
''' + helper + '''
const result = [];
for (const status of ['ok', 'error', 'limited', 'unknown']) {
 D.source_reads.email = {status, name:'Gmail', note:'<source>"', word:'read state'};
 result.push(sourceReadChip('email'));
}
console.log(JSON.stringify(result));'''
        rows = json.loads(subprocess.check_output(['node', '-e', script], text=True))
        self.assertIn('class="sc ok"', rows[0])
        self.assertIn('✓', rows[0])
        self.assertIn('&lt;source>\u0026quot;', rows[0])
        for row in rows[1:]:
            self.assertNotIn('✓', row)
            self.assertIn('class="sc none"', row)


if __name__ == '__main__':
    unittest.main()
