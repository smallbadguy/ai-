"""邮件和 HubSpot 工单（replay/brainpages.py）以及它们在业务版、详细版里的用法：假的大脑，不联网。"""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import brainpages as bp, business, corpus, events, progress, util  # noqa: E402

TERMS = corpus.build_terms({"name": "Acme_CTV_US_CPM_LG_Video_PMP_Freewheel", "mobile_app": {"name": "Acme Banking"}},
                           ["777777"], ["Freewheel"], ["14242"])
M = corpus.Matcher(TERMS)


def email_page(slug, subject, msgs, summary="The email exchange revolves around setup."):
    head = f"# {subject}\n\n## 2026-09-03 · \n\n- To: \n- Gmail ID: ``\n\nSubject: {subject}\nFrom: Ann <ann@feedmob.com>\nTo: \nSent At: 2026-07-13T20:00:00Z\n\n\n\n"
    body = "\n\n".join(f"From: {f}\nTo: {t}\nSent At: {ts}\n\n{text}" for f, t, ts, text in msgs)
    tail = f"\n\nFrom: Ann <ann@feedmob.com>\nTo: Bob <bob@x.com>\nSent At: 2026-07-14T09:00:00Z\n{summary}"
    return dict(slug=slug, title=subject, updated_at="2026-10-01T00:00:00Z", frontmatter={"source": "feedai-email"},
                compiled_truth=head + body + tail)


def ticket_page(tid, title, body, notes, created="2026-08-19T07:23:41Z"):
    lines = "\n".join("- " + json.dumps(n) for n in notes)
    ct = (f"# {title}\n\n- Status: Completed\n- Priority: P2\n- Owner: Yi Han\n- Created (建单时间, UTC): {created}\n\n"
          f"## Content\n\n{body}\n\n## Notes\n\n{lines}\n")
    return dict(slug=f"tickets/{tid}", title=title, updated_at="2026-10-01T00:00:00Z",
                frontmatter={"hubspot_ticket_id": tid, "hubspot_created_at": created, "hubspot_updated_at": "2026-09-01T16:17:53Z"},
                compiled_truth=ct)


def note(i, ts, first, last, body):
    return {"id": i, "hs_created_at": ts, "body": body, "owner": {"first_name": first, "last_name": last}}


LAUNCH = email_page("emails/1", "FeedMob <> Freewheel - Acme x LG PMP campaign launch", [
    ("Ann Lee <ann@feedmob.com>", '"DiDona, Russell" <rd@freewheel.com>', "2026-07-13T20:06:27Z",
     "Hi Russell! I just created a new campaign for Acme\xe2\x80\x99s PMP deal. Where can I add the deal ID?"),
    ("\"'DiDona, Russell' via Ally Pod\" <allypod@feedmob.com>", "Ann Lee <ann@feedmob.com>", "2026-07-13T21:36:23Z",
     "Deals are added under the App/Site section of targeting at the line item level."),
    ("\"'DiDona, Russell' via Ally Pod\" <allypod@feedmob.com>", "Ann Lee <ann@feedmob.com>", "2026-07-13T21:40:00Z",
     "Hi Russell! I just created a new campaign for Acme\xe2\x80\x99s PMP deal. Where can I add the deal ID?"),
])
OTHER = email_page("emails/2", "Invoice for Nov", [("Zed <z@x.com>", "Ann <ann@feedmob.com>", "2026-07-20T10:00:00Z", "Please find the invoice.")])
TICKET = ticket_page("4945", "Acme <> LG video PMP Freewheel spend tracking GH#14242", "Spend tracking for the PMP line",
                     [note(9021, "2026-08-19T07:26:07Z", "summer", "wu", "Hi @Yi Han, please check the spend request for Acme LG.")])
SAMSUNG = ticket_page("5000", "Acme <> Samsung PMP report", "Samsung report", [])


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = util.RAW
        util.RAW = pathlib.Path(self.tmp.name) / "raw"

    def tearDown(self):
        util.RAW = self.saved
        self.tmp.cleanup()

    def cache(self, kind, *pages):
        for p in pages:
            util.write_json(bp.cache_dir(kind) / (util.safe_name(p["slug"]) + ".json"), p)


class Parsing(unittest.TestCase):
    def test_messages_have_sender_time_and_clean_text_without_summary_or_split_duplicates(self):
        msgs = bp.email_messages(LAUNCH)
        self.assertEqual([m["who"] for m in msgs], ["Ann Lee", "Russell DiDona"])
        self.assertEqual(msgs[0]["to"], ["Russell DiDona"])
        self.assertIn("Acme’s PMP deal", msgs[0]["text"])          # 乱码还原
        self.assertFalse(any("revolves around" in m["text"] for m in msgs))  # 尾部摘要不是信

    def test_recipients_split_on_addresses_not_commas(self):
        f = 'DiDona, Russell <rd@freewheel.com>, "Antonucci, Samantha" <sa@freewheel.com>, liwei@feedmob.com, Stacey <s@fw.com>'
        self.assertEqual(bp.recipients(f), ["Russell DiDona", "Samantha Antonucci", "liwei", "Stacey"])
        self.assertEqual([bp.is_external(x) for x in bp.parties(f)], [True, True, False, True])
        self.assertTrue(bp.is_external("\"'FreeWheel' via Ally Pod\" <allypod@feedmob.com>"))

    def test_names(self):
        self.assertEqual(bp.person_name('"\'FreeWheel\' via Ally Pod" <allypod@feedmob.com>'), "FreeWheel")
        self.assertEqual(bp.person_name("liwei@feedmob.com"), "liwei")

    def test_ticket_fields_and_notes(self):
        t = bp.ticket(TICKET)
        self.assertEqual((t["id"], t["owner"], t["status"], t["created"]), ("4945", "Yi Han", "Completed", "2026-08-19T07:23:41Z"))
        self.assertEqual(t["notes"][0]["who"], "Summer Wu")
        self.assertEqual(t["body"], "Spend tracking for the PMP line")

    def test_sse_keeps_unicode_line_separators_inside_json(self):
        sys.path.insert(0, str(ROOT / "collect"))
        import mcp_client
        raw = 'event: message\ndata: {"id":1,"result":{"t":"a b"}}\n\ndata: {"id":2,\ndata: "x":1}\n\n'
        self.assertEqual(mcp_client.sse_messages(raw), [{"id": 1, "result": {"t": "a b"}}, {"id": 2, "x": 1}])


class Relevance(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(bp.relevance(M, "Acme LG", "about 777777")[0], "direct")
        self.assertEqual(bp.relevance(M, "Acme x LG PMP", "on Freewheel")[0], "strong")
        self.assertEqual(bp.relevance(M, "Acme x LG", "banner")[0], "loose")
        self.assertIsNone(bp.relevance(M, "Acme Samsung", "report")[0])
        self.assertIsNone(bp.relevance(M, "LG Freewheel", "no advertiser")[0])

    def test_partner_thread_about_another_client_is_not_strong(self):
        # FreeWheel 的线程标题是别的客户，正文顺带提到 Acme、LG、Freewheel：不算本活动的线程
        self.assertIsNone(bp.relevance(M, "Freewheel - Aura launch", "Acme on LG via Freewheel")[0])
        self.assertEqual(bp.relevance(M, "Freewheel - Aura launch", "Acme Acme Acme on LG via Freewheel")[0], "loose")


class Records(Sandbox):
    def test_corpus_records_from_cache(self):
        self.cache("emails", LAUNCH, OTHER)
        self.cache("tickets", TICKET, SAMSUNG)
        em = bp.email_records(M, "2026-07-01", "2026-10-24")
        self.assertEqual([r["who"] for r in em], ["Ann Lee", "Russell DiDona"])
        self.assertTrue(all(r["tier"] == "strong" and r["why"] == "thread" for r in em))
        tk = bp.ticket_records(M, "2026-07-01", "2026-10-24")
        self.assertEqual([(r["ticket"], r["who"]) for r in tk], [("4945", ""), ("4945", "Summer Wu")])
        self.assertEqual(tk[0]["tier"], "direct")   # 标题里的单号
        self.assertEqual(bp.email_records(M, "2026-08-01", "2026-10-24"), [])  # 窗口外

    def test_sync_refetches_only_changed_pages(self):
        class Brain:
            def __init__(self):
                self.got = []

            def call(self, name, args):
                if name == "list_pages":
                    return [dict(slug="tickets/4945", updated_at="2026-10-01T00:00:00Z")] if args["offset"] == 0 else []
                self.got.append(args["slug"])
                return TICKET
        b = Brain()
        bp.sync(b, "tickets", log=lambda *_: None)
        bp.sync(b, "tickets", log=lambda *_: None)
        self.assertEqual(b.got, ["tickets/4945"])


class Business(unittest.TestCase):
    def test_email_and_note_quotes_are_located_and_ticket_creation_is_not_quotable(self):
        rec = dict(kind="email", id="R1", ts="2026-07-13T21:36:23Z", who="Russell DiDona", to=["Ann Lee"], subject="Acme x LG PMP",
                   text="Deals are added under the App/Site section of targeting.", tier="strong", why="thread")
        hit = business.locate(rec, "Deals are added under the App/Site section")
        self.assertEqual((hit["speaker"], hit["ts"]), ("Russell DiDona", "2026-07-13T21:36:23Z"))
        self.assertIn("EMAIL «Acme x LG PMP» | Russell DiDona → Ann Lee", business._render(rec))
        created = dict(kind="hubspot", id="R2", ts="2026-08-19T07:23:41Z", who="", ticket="4945", title="Acme LG", text="Spend tracking")
        self.assertIsNone(business.locate(created, "Spend tracking"))
        self.assertTrue(business.dedicated(rec) and business.anchored(rec))
        c = business.verify_picks([dict(record="R1", quote="Deals are added under the App/Site section")], [rec], M, business.dedicated,
                                  log=lambda *_: None)
        self.assertEqual((c[0]["source"], c[0]["anchored"]), ("Acme x LG PMP", True))  # 回复没点名也算本活动的

    def test_progress_renders_new_kinds(self):
        self.assertIn("HUBSPOT ticket #4945", progress.render(dict(kind="hubspot", ts="2026-08-19T07:23:41Z", who="Summer Wu", ticket="4945",
                                                                   title="Acme LG", text="please check")))


class Detail(Sandbox):
    def test_email_and_ticket_events_come_from_strong_corpus_records(self):
        self.cache("tickets", TICKET)
        self.cache("emails", LAUNCH)
        loose = dict(kind="email", ts="2026-07-20T00:00:00Z", thread="emails/9", subject="Acme banner", who="X", to=[], text="loose",
                     tier="loose", why="match")
        recs = bp.email_records(M, "2026-07-01", "2026-10-24") + bp.ticket_records(M, "2026-07-01", "2026-10-24") + [loose]
        util.write_json(util.camp_dir("9") / "corpus.json", dict(terms=TERMS, records=recs))

        class R:
            def __init__(self):
                self.people, self.index = {}, {}

            def resolve(self, system, account):
                pid = f"{system}-{account}"
                self.people.setdefault(pid, dict(name=account, kind="human"))
                return pid
        r = R()
        em = events.email_events("9", r)
        self.assertEqual([e["actor"] for e in em], ["email-Ann Lee", "email-Russell DiDona"])  # 弱相关的不进详细版
        self.assertEqual(em[0]["to"], ["email-Russell DiDona"])
        hs, info = events.corpus_tickets("9", r, {}, known=set())
        self.assertEqual([e["kind"] for e in hs], ["create", "comment"])
        self.assertEqual(info[0]["id"], "4945")
        self.assertEqual(events.corpus_tickets("9", r, {}, known={"4945"})[0], [])


if __name__ == "__main__":
    unittest.main()


class Directory(unittest.TestCase):
    def test_external_only_when_never_seen_inside(self):
        from unittest import mock
        D = {"alejandra segovia": [0, 40], "alejandra.segovia": [0, 40], "andrew tan": [170, 8], "leon": [0, 3]}
        with mock.patch.object(bp, "_directory", lambda: D):
            self.assertTrue(bp.external_name("alejandra.segovia"))
            self.assertTrue(bp.external_name("Alejandra Segovia"))
            self.assertFalse(bp.external_name("Andrew Tan"))    # 内部地址也用过：自己人
            self.assertFalse(bp.external_name("leon"))          # 一个词的名字不下结论
            self.assertFalse(bp.external_name("Nobody Here"))   # 邮件里没出现过：不下结论
