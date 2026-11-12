"""Attribution must separate campaign facts, vendor facts and client background."""
import copy
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from replay import attribution as A, attribution_context as AC, business, corpus, events, progress, util

CURRENT = {'id':'4947', 'name':'Chime_CTV_US_CPM', 'advertiser':'Chime',
           'vendors':[{'id':'538','name':'Vizio','links':['23505']},
                      {'id':'539','name':'TCL','links':['23518']}], 'issues':['14063','14065']}
LG = {'id':'5023', 'name':'Chime_CTV_US_CPM_LG_Video_PMP_Freewheel', 'advertiser':'Chime',
      'vendors':[{'id':'551','name':'Freewheel','links':['23702']}], 'issues':['14242']}
OTHER_VIZIO = {'id':'5113', 'name':'Chime_CTV_US_CPM_Vizio_PMP_Freewheel', 'advertiser':'Chime',
               'vendors':[{'id':'551','name':'Freewheel','links':['23900']}], 'issues':['14500']}


def terms(vendor=None, other_vizio=False):
    t = dict(name=CURRENT['name'], advertiser='Chime', campaign_id='4947', vendor_id=vendor,
             links=['23505','23518'] if not vendor else ['23505'] if vendor=='538' else ['23518'],
             issues=['14063','14065'] if not vendor else ['14063'] if vendor=='538' else ['14065'],
             context=['Vizio','TCL'], vendors=copy.deepcopy(CURRENT['vendors']),
             catalog=copy.deepcopy([CURRENT, LG] + ([OTHER_VIZIO] if other_vizio else [])),
             attribution_version=A.ATTRIBUTION_VERSION)
    return t


def record(text, kind='slack', **kw):
    return dict(id='R1', kind=kind, ts='2026-01-01T00:00:00Z', text=text, who='Example Owner',
                channel='#chime', dedicated=True, why='channel', **kw)


class Evidence(unittest.TestCase):
    def test_exact_campaign_name_is_an_aggregate_fact(self):
        self.assertEqual(A.classify('Chime_CTV_US_CPM budget changed', terms())['level'], 'direct')

    def test_lg_full_name_is_not_a_short_campaign_name_hit(self):
        d = A.classify(LG['name']+' spend tracking', terms())
        self.assertFalse(d['accepted'])
        self.assertEqual(d['level'], 'other')

    def test_unlisted_longer_campaign_is_not_promoted_by_vendor_words(self):
        self.assertFalse(A.classify('Chime_CTV_US_CPM_TCL_PMP_Example launch', terms('539'))['accepted'])

    def test_hubspot_lg_ticket_and_known_issue_are_excluded(self):
        text = LG['name']+' GH#14242 spend tracking'
        self.assertFalse(A.classify(text, terms(), title='Chime <> Freewheel LG PMP spend tracking')['accepted'])

    def test_just_client_and_just_client_channel_are_background(self):
        self.assertEqual(A.classify('Chime update', terms())['level'], 'client')
        self.assertEqual(A.classify('Please send the report', terms(), channel='#chime')['level'], 'client')

    def test_unique_client_vendor_is_supported_without_a_full_campaign_name(self):
        self.assertEqual(A.classify('Chime Vizio test budget approved', terms('538'))['level'], 'vendor')

    def test_client_vendor_can_be_ambiguous_across_inventory_variants(self):
        self.assertEqual(A.classify('Chime Vizio test budget approved', terms('538', True))['level'], 'ambiguous')

    def test_campaign_name_alone_does_not_identify_a_vendor(self):
        self.assertFalse(A.classify(CURRENT['name'], terms('538'))['accepted'])

    def test_campaign_name_plus_explicit_vendor_resolves_that_vendor(self):
        self.assertTrue(A.classify(CURRENT['name']+' Vizio launches', terms('538', True))['accepted'])

    def test_same_campaign_joint_decision_is_available_to_both_vendors(self):
        text = CURRENT['name']+': Vizio and TCL each get a 30000 test budget'
        for key in ('538','539'):
            with self.subTest(key=key):
                self.assertTrue(A.classify(text, terms(key))['accepted'])

    def test_joint_urls_of_same_campaign_are_available_to_both_vendors(self):
        for key in ('538','539'):
            self.assertTrue(A.classify('URL #23505 and URL #23518 launch together', terms(key))['accepted'])

    def test_only_other_vendor_is_not_in_selected_vendor(self):
        self.assertFalse(A.classify('Chime TCL launches', terms('538'))['accepted'])
        self.assertFalse(A.classify('URL #23518 launches', terms('538'))['accepted'])

    def test_known_url_forms_and_number_boundaries(self):
        for text in ('click URL 23505', 'URL #23505', 'https://admin.example/click_urls/23505'):
            with self.subTest(text=text):self.assertTrue(A.classify(text, terms('538'))['accepted'])
        for text in ('$23505', 'click URL 123505', 'click URL 235050', 'Budget 23505'):
            with self.subTest(text=text):self.assertFalse(A.classify(text, terms('538'))['accepted'])

    def test_other_campaign_known_url_is_excluded(self):
        self.assertEqual(A.classify('click URL 23702 budget', terms('538'))['level'], 'other')

    def test_conflicting_current_url_and_sibling_name_are_not_certain(self):
        d = A.classify('click URL 23505 '+LG['name'], terms('538'))
        self.assertFalse(d['accepted'])
        self.assertEqual(d['level'], 'ambiguous')

    def test_company_domain_is_not_a_verified_vendor_mapping(self):
        self.assertFalse(A.classify('Chime update from test@vizio.com', terms('538'))['accepted'])

    def test_context_country_and_misspelling_never_create_vendor_facts(self):
        for text in ('Chime US CTV CPM plan', 'Chime Vizlo plan'):
            self.assertFalse(A.classify(text, terms('538'))['accepted'])

    def test_multiple_urls_for_one_vendor_remain_selectable(self):
        t = terms('538');t['links'].append('23506');t['vendors'][0]['links'].append('23506')
        self.assertTrue(A.classify('URL #23506 changes', t)['accepted'])


class AtomicRecords(unittest.TestCase):
    def test_split_multi_campaign_paragraphs_does_not_lose_the_valid_part(self):
        source = record('click URL 23505 is approved.\n\n'+LG['name']+' is paused.')
        kept, bg, excluded = AC.partition([source], terms('538'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['text'], 'click URL 23505 is approved.')
        self.assertEqual(len(excluded), 1)
        self.assertEqual(source['text'].count('\n\n'), 1)

    def test_weekly_meeting_is_classified_per_original_turn(self):
        source = record('**Example Owner** (08:01:00): click URL 23505 is approved\n'
                        '**Example Reader** (08:02:00): '+LG['name']+' launches', kind='meeting', title='AM weekly')
        kept, bg, excluded = AC.partition([source], terms('538'))
        self.assertEqual(len(kept),1);self.assertEqual(len(excluded),1)
        self.assertIn('08:01:00', kept[0]['text'])
        self.assertNotIn('08:02:00', kept[0]['text'])

    def test_customer_background_does_not_pass_as_business_quote(self):
        r = record('Chime is preparing a plan')
        m = corpus.Matcher(terms('538'))
        self.assertEqual(business.verify_picks([{'record':'R1','quote':r['text']}], [r], m, lambda _:True, log=lambda *_:None), [])

    def test_email_reply_can_inherit_proven_context_but_not_quote_lg_history(self):
        r = record('We will send the asset tomorrow.\n> '+LG['name']+' launches', kind='email',
                   subject=CURRENT['name']+' Vizio', to=[], thread='example')
        self.assertTrue(AC.quote_allowed(r, 'We will send the asset tomorrow.', terms('538')))
        self.assertFalse(AC.quote_allowed(r, LG['name']+' launches', terms('538')))

    def test_detail_email_does_not_display_excluded_quoted_history(self):
        from replay.people import Roster
        r = record('We will send the asset tomorrow.\n> '+LG['name']+' launches', kind='email',
                   subject=CURRENT['name']+' Vizio', to=[], thread='example')
        with patch.object(events, 'corpus_records', return_value=[r]):
            rows = events.email_events('4947', Roster(), corpus.Matcher(terms('538')))
        self.assertEqual(len(rows), 1)
        self.assertIn('We will send the asset tomorrow.', rows[0]['zh']['quote'])
        self.assertNotIn(LG['name'], rows[0]['zh']['quote'])

    def test_detail_ticket_filters_each_note_not_the_entire_ticket(self):
        t = dict(id='1', title='Chime Vizio URL #23505', owner='', status='open', priority='normal',
                 created='2026-01-01T00:00:00Z', updated='', body='click URL 23505 budget', notes=[
                     {'id':'1','ts':'2026-01-02T00:00:00Z','who':'Example Owner','text':'We will send the asset'},
                     {'id':'2','ts':'2026-01-03T00:00:00Z','who':'Example Owner','text':LG['name']+' launches'}])
        from replay.people import Roster
        rows,_ = events.ticket_events(t, Roster(), {}, corpus.Matcher(terms('538')))
        self.assertTrue(any(e['key']=='hs:1:n1' for e in rows))
        self.assertFalse(any(e['key']=='hs:1:n2' for e in rows))

    def test_progress_does_not_trust_client_channel_alone(self):
        r = record('Chime is preparing a plan')
        docs = progress.build_docs([r]); m = corpus.Matcher(terms('538'))
        got,_ = progress.checked({'quote':r['text']}, docs, m)
        self.assertIsNone(got)

    def test_progress_rebuild_discards_old_client_wide_items(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(util, 'RAW', pathlib.Path(tmp)):
            r = record('click URL 23505 is approved')
            util.write_json(util.camp_dir('4947')/'corpus.json', {
                'campaign':{'id':4947,'name':CURRENT['name']}, 'terms':terms('538'), 'records':[r],
                'attribution_version':A.ATTRIBUTION_VERSION})
            util.write_json(util.camp_dir('4947')/'progress.json', {
                'seen':[progress.fingerprint(r)], 'items':[{'topic':'Old LG item'}]})
            calls=[]
            def ask(messages):
                calls.append(messages)
                return json.dumps({'items':[],'closed':[]}), {}
            result=progress.build('4947', ask=ask, log=lambda *_:None, today=dt.date(2026,1,3))
            self.assertEqual(result['calls'],1)
            saved=util.read_json(util.camp_dir('4947')/'progress.json')
            self.assertEqual(saved['items'],[])
            self.assertEqual(saved['attribution_version'],A.ATTRIBUTION_VERSION)
            self.assertIn('vendor_id', calls[0][0]['content'])
