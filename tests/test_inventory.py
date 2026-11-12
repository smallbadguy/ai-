"""The backend list is not a complete history. Preserve observed URLs and select safely."""
import datetime as dt
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from replay import campaign, collect_mcp, inventory, scope, util

CID = '5555'
CAMP = {'id':int(CID), 'name':'Example_CTV_US', 'active':True}


def click(name, vendor, label, cid=CID, changes=()):
    return {'name':str(name), 'campaign':{'id':int(cid)}, 'partner':{'id':vendor, 'name':label},
            'proposed_changes':list(changes), 'created_at':'2026-01-01 00:00:00 UTC'}


PAUSE = {'approve_status':'approved', 'new_state':'paused', 'new_state_effective_at':'2026-02-01 00:00:00 UTC'}


class Inventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(util, 'RAW', pathlib.Path(self.temp.name) / 'raw')
        self.patch.start(); self.addCleanup(self.patch.stop)

    def save(self, cu):
        util.write_json(util.raw_dir(cu['name']) / 'femini.json', {'click_url':cu, 'queried_at':'2026-03-01T00:00:00Z'})

    def refresh(self, listed=()):
        return inventory.refresh(CID, campaign=CAMP, listed=list(listed))

    def test_paused_url_is_recovered_even_when_active_list_is_nonempty(self):
        self.save(click('777', 10, 'Vendor One'))
        self.save(click('778', 11, 'Vendor Two', changes=[PAUSE]))
        found = self.refresh([{'name':'777'}])
        self.assertEqual({e['name'] for e in found['links']}, {'777','778'})
        options = {v['id']:v for v in found['vendors']}
        self.assertTrue(options['10']['active'])
        self.assertFalse(options['11']['active'])
        self.assertFalse(found['history_complete'])

    def test_same_vendor_can_have_multiple_urls(self):
        self.save(click('777', 10, 'Vendor One'))
        self.save(click('779', 10, 'Vendor One', changes=[PAUSE]))
        found = self.refresh([{'name':'777'}])
        self.assertEqual(len(found['vendors']), 1)
        self.assertEqual(set(found['vendors'][0]['links']), {'777','779'})
        self.assertTrue(found['vendors'][0]['active'])

    def test_disappearance_does_not_claim_a_pause(self):
        self.save(click('777', 10, 'Vendor One'))
        self.refresh([{'name':'777'}])
        found = self.refresh([])
        self.assertIsNone(found['vendors'][0]['active'])
        self.assertEqual(found['links'][0]['status'], 'unknown')

    def test_unapproved_and_future_pauses_are_not_current_state(self):
        for change in (dict(PAUSE, approve_status='pending'),
                       dict(PAUSE, new_state_effective_at='2099-01-01 00:00:00 UTC')):
            self.save(click('777', 10, 'Vendor One', changes=[change]))
            self.assertIsNone(self.refresh()['vendors'][0]['active'])

    def test_conflicting_list_and_pause_is_unknown_instead_of_false_certainty(self):
        self.save(click('777', 10, 'Vendor One', changes=[PAUSE]))
        found = self.refresh([{'name':'777'}])
        self.assertIsNone(found['vendors'][0]['active'])
        self.assertTrue(found['links'][0]['state_conflict'])

    def test_other_campaign_cache_is_not_borrowed(self):
        self.save(click('777', 10, 'Vendor One', cid='6666'))
        util.write_json(util.base_camp_dir(CID) / 'campaign.json', {'campaign':CAMP, 'links':['777']})
        found = self.refresh()
        self.assertEqual(found['links'], [])
        self.assertIn('777', found['errors'])

    def test_vendor_must_belong_to_the_selected_campaign(self):
        self.save(click('777', 10, 'Vendor One'))
        self.refresh()
        self.assertEqual(campaign.validate_vendor(CID, '10')['name'], 'Vendor One')
        with self.assertRaises(ValueError):
            campaign.validate_vendor(CID, '99')

    def test_catalog_homepage_reads_do_not_open_an_mcp_connection(self):
        self.save(click('777', 10, 'Vendor One'))
        self.refresh()
        with patch.object(collect_mcp.mcp_client, 'femini', side_effect=AssertionError('no network')):
            self.assertEqual(campaign.vendor_options(CID)[0]['links'], ['777'])

    def test_vendor_discovery_never_overwrites_aggregate_campaign_file(self):
        self.save(click('777', 10, 'Vendor One'))
        self.save(click('778', 11, 'Vendor Two', changes=[PAUSE]))
        class Backend:
            def call(inner, name, args):
                if name == 'get_campaign':return {'campaign':CAMP}
                if name == 'list_click_urls':return {'click_urls':[{'name':'777'}]}
                if name == 'get_click_url':
                    return {'click_url':util.read_json(util.raw_dir(args['name']) / 'femini.json')['click_url']}
                raise AssertionError(name)
        with patch.object(collect_mcp.mcp_client, 'femini', return_value=Backend()):
            with scope.use(CID, '11'):
                doc = campaign.discover(CID, log=lambda *_:None)
                self.assertEqual(doc['links'], ['778'])
                self.assertEqual(campaign.info(CID)['vendor_id'], '11')
            base = campaign.info(CID)
            self.assertEqual(set(base['links']), {'777','778'})
            self.assertNotIn('vendor_id', base)

    def test_missing_partner_id_uses_a_stable_safe_name_key(self):
        a = inventory.vendor_key({'name':'Vendor One'})
        self.assertEqual(a, inventory.vendor_key({'name':'vendor one'}))
        self.assertEqual(scope.valid_vendor(a), a)
        self.assertNotEqual(a, inventory.vendor_key({'name':'Vendor Two'}))

    def test_vendor_metrics_request_both_campaign_and_partner_filters(self):
        calls = []
        class Backend:
            def call(inner, name, args):
                calls.append((name,args))
                return {'data':[{'date':'2026-01-01','value':7}]}
        with patch.object(collect_mcp.mcp_client, 'femini', return_value=Backend()):
            with scope.use(CID, '10'):
                collect_mcp.collect_metrics(CID, log=lambda *_:None, today=dt.date(2026,3,1))
                metrics = util.read_json(util.camp_dir(CID) / 'metrics.json')
                self.assertEqual(metrics['vendor_id'], '10')
            self.assertFalse((util.camp_dir(CID) / 'metrics.json').exists())
        self.assertTrue(calls)
        self.assertTrue(all(a['campaign_id']==int(CID) and a['partner_id']==10 for _,a in calls))
