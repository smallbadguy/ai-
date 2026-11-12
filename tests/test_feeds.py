"""直连数据源（replay/feeds.py）的测试：假的 FeedAI / Fireflies / Femini，不联网、不碰真实数据。"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import collect_mcp, corpus, feeds, util  # noqa: E402

CID, N = "5555", "777777"
CAMP = {"id": 5555, "name": "Acme_CTV_US_CPM_LG_Video_PMP_Freewheel", "mobile_app": {"name": "Acme – Mobile Banking"}}
TODAY = dt.datetime.now(dt.timezone.utc).date()


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def slack_point(pid, content, **meta):
    return {"id": pid, "payload": {"metadata": meta, "page_content": content}}


def msg(pid, chan, cid, day, clock, who, text):
    return slack_point(pid, f"{day} {clock} {who}: {text}", id=f"m{pid}", channel_id=cid, channel_name=chan,
                       timestamp=f"{day}T{clock}.000001")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = pathlib.Path(self.tmp.name)
        self.saved_raw = util.RAW
        util.RAW = self.t / "raw"
        env = {k: v for k, v in os.environ.items() if k not in ("REPLAY_SOURCE",)}
        env["REPLAY_SECRETS"] = str(self.t / "secrets")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        util.RAW = self.saved_raw
        self.tmp.cleanup()


class Switch(Sandbox):
    def test_env_first_then_feeds_env(self):
        self.assertFalse(feeds.enabled())
        (self.t / "secrets").mkdir()
        (self.t / "secrets" / "feeds.env").write_text("# 直连\nREPLAY_SOURCE=direct\nQDRANT_URL=https://q.example\n")
        self.assertTrue(feeds.enabled())
        self.assertEqual(feeds.setting("QDRANT_URL"), "https://q.example")
        with mock.patch.dict(os.environ, {"REPLAY_SOURCE": "brain"}):
            self.assertFalse(feeds.enabled())

    def test_not_ready_until_every_kind_had_a_full_pull(self):
        self.assertFalse(feeds.ready())
        write(feeds.state_path(), {"slack": {"full_at": "x"}, "emails": {"as_of": "x"}, "tickets": {"as_of": "x"},
                                   "meetings": {"fireflies_backfilled": True, "femini_backfilled": True}})
        self.assertFalse(feeds.ready())   # Gong 还没拉
        st = feeds.read_state()
        st["meetings"]["gong_done"] = True
        write(feeds.state_path(), st)
        self.assertTrue(feeds.ready())


class SlackFormat(Sandbox):
    def test_pages_are_byte_identical_to_the_brain_export(self):
        points = [
            slack_point(1, "Feedmob - acme-team\n2026-09-03 10:00:00 Ann: hello   team", id="m1", channel_id="C0AAAAAAA1",
                        channel_name="acme-team", timestamp="2026-09-03T10:00:00.000001",
                        permalink="https://x.slack.com/archives/C0AAAAAAA1/p1"),
            slack_point(2, "2026-09-03 09:00:00 Bob: api_key = sk-abcdefghijklmnopqrstu", id="m2", channel_id="C0AAAAAAA1",
                        channel_name="acme-team", timestamp="2026-09-03T09:00:00.000001"),
            # 长消息分两块，分块先到；父点的信息优先
            slack_point(3, "second part", parent_id="m3", is_chunk=True, chunk_index=1, channel_id="C0AAAAAAA1",
                        channel_name="acme-team", timestamp="2026-09-04T08:00:00.000001"),
            slack_point(4, "2026-09-04 08:00:00 Cat: first part", id="m3", has_chunks=True, channel_id="C0AAAAAAA1",
                        channel_name="acme-team", timestamp="2026-09-04T08:00:00.000001"),
            slack_point(5, "2026-09-04 07:00:00 Dan: dm", id="m5", channel_id="D0BBBBBBB1", channel_name="D0BBBBBBB1",
                        timestamp="2026-09-04T07:00:00.000001"),
            slack_point(6, "   ", id="m6", channel_id="C0AAAAAAA1", channel_name="acme-team", timestamp="2026-09-05T07:00:00"),
        ]
        pages, n = feeds.slack_pages(points)
        self.assertEqual(n, 6)
        self.assertEqual(sorted(pages), [("acme-team-c0aaaaaaa1", "2026-09-03"), ("acme-team-c0aaaaaaa1", "2026-09-04"),
                                         ("d0bbbbbbb1-d0bbbbbbb1", "2026-09-04")])   # 空消息不出页
        expected = "\n\n".join([
            "---", "type: note", 'title: "Slack acme-team — 2026-09-03"', 'date: "2026-09-03"',
            'channel_id: "C0AAAAAAA1"', 'channel_name: "acme-team"', 'visibility: "channel"',
            "tags: [slack, communication]", "source: feedai-slack",
            'source_url: "https://x.slack.com/archives/C0AAAAAAA1/p1"', "---", "", "# Slack acme-team — 2026-09-03", "",
            "2026-09-03 09:00:00 Bob: api_key = [REDACTED]",        # 按时间排；密钥脱敏
            "2026-09-03 10:00:00 Ann: hello team",                   # 第一行的「工作区 - 频道」去掉，空白压成一个
        ]) + "\n"
        self.assertEqual(pages[("acme-team-c0aaaaaaa1", "2026-09-03")]["content"], expected)
        self.assertEqual(pages[("acme-team-c0aaaaaaa1", "2026-09-03")]["updated_at"], "2026-09-03T10:00:00Z")
        self.assertIn("\n\n2026-09-04 08:00:00 Cat: first part second part\n",
                      pages[("acme-team-c0aaaaaaa1", "2026-09-04")]["content"])
        self.assertIn('visibility: "private-or-dm"', pages[("d0bbbbbbb1-d0bbbbbbb1", "2026-09-04")]["content"])

    def test_channel_dirs_and_titles_follow_the_brain_paths(self):
        self.assertEqual(feeds.slack_dir_name("femini-dev-ops", "C0BMN1JUGHZ"), "femini-dev-ops-c0bmn1jughz")
        self.assertEqual(feeds.slack_dir_name("account_analysts", "CR1TH0064"), "account_analysts-cr1th0064")
        self.assertEqual(feeds.slack_dir_name("", "C01F9MHLDG9"), "c01f9mhldg9")              # 大脑：--C01F9MHLDG9
        self.assertEqual(feeds.slack_dir_name("#impact integration", "C04NSNVDNSY"), "impact-integration-c04nsnvdnsy")
        self.assertEqual(feeds.slack_dir_name("feedmob!", "C019PJFSYLU"), "feedmob-c019pjfsylu")
        pages, _ = feeds.ticket_pages([{"id": 1, "payload": {"ticket": {"id": "5123", "subject": "[Kraken] CTV Creatives "}}}])
        self.assertEqual(pages["tickets/5123"]["title"], "[Kraken] CTV Creatives")         # 大脑的标题去掉首尾空白
        self.assertIn("# [Kraken] CTV Creatives \n", pages["tickets/5123"]["text"])       # 正文照原样

    def test_redact_shortcut_gives_the_same_result_as_running_every_rule(self):
        def slow(value):
            text = "" if value is None else str(value)
            for pattern in feeds._SECRET_PATTERNS:
                text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
            return text
        samples = ["plain text", None, 42, "API KEY: abc123", "Password is hunter2", "ſecret = x", "\u212aEY: v",
                   "token=eyJabcdefghijk.eyJabcdefghijk.abcdefghijkl", "sk-ABCDEFGHIJKLMNOPQR", "xoxb-1234567890-abc",
                   "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
                   "monkey business", "keynote: secret sauce: yes", "auth token : t0k3n and access-token: x"]
        for x in samples:
            self.assertEqual(feeds.redact(x), slow(x), x)

    def test_body_is_what_the_brain_returned_as_compiled_truth(self):
        self.assertEqual(feeds._body('---\ntitle: "x"\n---\n\n# x\n\nbody\n---\nmore\n'), "# x\n\nbody\n---\nmore")
        self.assertEqual(feeds._body("# no frontmatter\n"), "# no frontmatter")


class SlackRefresh(Sandbox):
    def setUp(self):
        super().setUp()
        self.old = (TODAY - dt.timedelta(days=10)).isoformat()
        self.recent = (TODAY - dt.timedelta(days=1)).isoformat()
        self.points = [msg(1, "acme-team", "C0AAAAAAA1", self.old, "10:00:00", "Ann", "old talk"),
                       msg(2, "acme-team", "C0AAAAAAA1", self.recent, "10:00:00", "Ann", "new talk"),
                       msg(3, "sales", "C0CCCCCCC1", self.recent, "11:00:00", "Bob", "link 777777 paused")]
        self.filters = []

    def scroll(self, collection, flt=None, payload=True, batch=1000):
        self.assertEqual(collection, feeds.SLACK_COLLECTION)
        self.filters.append(flt)
        if flt is None:
            return iter(list(self.points))
        lo = flt["must"][0]["range"]["gte"][:10]
        return iter([p for p in self.points if p["payload"]["metadata"]["timestamp"][:10] >= lo])

    def page(self, d, day):
        return feeds._read(feeds.slack_root() / d / f"{day}.json")

    def test_full_then_incremental_window(self):
        st = {}
        with mock.patch.object(feeds, "scroll", self.scroll):
            info = feeds.refresh_slack(st, log=lambda *_: None)
        self.assertTrue(info["full"])
        self.assertEqual(self.filters, [None])
        self.assertEqual(info["pages"], 3)
        self.assertTrue(st["full_at"])
        rec = self.page("sales-c0ccccccc1", self.recent)
        self.assertFalse(rec["missing"])
        self.assertIn("link 777777 paused", rec["data"]["content"])
        self.assertEqual(feeds.slack_candidates([N]), [("sales-c0ccccccc1", self.recent)])
        self.assertEqual(sorted(feeds.slack_index()), ["acme-team-c0aaaaaaa1", "sales-c0ccccccc1"])

        # 上游变了：Ann 的新消息改了字，Bob 的消息删了；本地另有两页上游已经没有（一页在最近几天里，一页很早）
        self.points[1] = msg(2, "acme-team", "C0AAAAAAA1", self.recent, "10:00:00", "Ann", "new talk edited")
        del self.points[2]
        for day in (self.recent, self.old):
            write(feeds.slack_root() / "gone-c0ddddddd1" / f"{day}.json", {"missing": False, "data": {"content": "x"}})
        old_page_before = (feeds.slack_root() / "acme-team-c0aaaaaaa1" / f"{self.old}.json").read_text()
        with mock.patch.object(feeds, "scroll", self.scroll):
            info = feeds.refresh_slack(st, log=lambda *_: None)
        self.assertFalse(info["full"])
        cutoff = (TODAY - dt.timedelta(days=feeds.SLACK_RECENT_DAYS)).isoformat()
        self.assertEqual(self.filters[-1], {"must": [{"key": "metadata.timestamp", "range": {"gte": f"{cutoff}T00:00:00Z"}}]})
        self.assertIn("new talk edited", self.page("acme-team-c0aaaaaaa1", self.recent)["data"]["content"])
        self.assertIsNone(self.page("sales-c0ccccccc1", self.recent))          # 最近几天里、上游已经没有：删掉
        self.assertIsNone(self.page("gone-c0ddddddd1", self.recent))
        self.assertIsNotNone(self.page("gone-c0ddddddd1", self.old))            # 窗口之外不动（等下次全量）
        self.assertEqual((feeds.slack_root() / "acme-team-c0aaaaaaa1" / f"{self.old}.json").read_text(), old_page_before)
        self.assertEqual(feeds.slack_candidates([N]), [])

        # 全量：窗口外上游已经没有的也删
        st["full_at"] = "2000-01-01T00:00:00Z"
        with mock.patch.object(feeds, "scroll", self.scroll):
            info = feeds.refresh_slack(st, log=lambda *_: None)
        self.assertTrue(info["full"])
        self.assertIsNone(self.page("gone-c0ddddddd1", self.old))

    def test_candidates_fall_back_to_a_full_scan_for_odd_numbers(self):
        with mock.patch.object(feeds, "scroll", self.scroll):
            feeds.refresh_slack({}, log=lambda *_: None)
        self.assertEqual(len(feeds.slack_candidates(["12"])), 3)   # 两位数不在数字索引里：逐页全扫
        self.assertEqual(feeds.slack_candidates([]), [])


class MailAndTickets(Sandbox):
    def test_email_thread_format_and_write(self):
        def p(pid, doc, date, sent, text, upd):
            return {"id": pid, "payload": {"page_content": f"From: A <a@x.com>\nSent At: {sent}\n\n{text}", "metadata": {
                "thread_id": "1330", "document_id": doc, "chunk_index": 0, "date": date, "subject": "Spend totals",
                "from": "A <a@x.com>", "to": "b@feedmob.com", "gmail_id": f"g{doc}", "updated_at": upd}}}
        points = [p(2, "12", "2026-02-05", "2026-02-05T09:00:00Z", "Second", "2026-02-06T00:00:00Z"),
                  p(1, "11", "2026-02-04", "2026-02-04T10:00:00Z", "First", "2026-02-05T00:00:00Z")]
        pages, n = feeds.email_threads(points)
        self.assertEqual(n, 2)
        pg = pages["emails/1330"]
        self.assertEqual(pg["text"], "\n".join([
            "---", "type: email", 'title: "Spend totals"', 'date: "2026-02-05"', 'thread_id: "1330"', "source: feedai-email",
            "---", "", "# Spend totals", "",
            "## 2026-02-04 · A <a@x.com>", "", "- To: b@feedmob.com", "- Gmail ID: `g11`", "",
            "From: A <a@x.com>\nSent At: 2026-02-04T10:00:00Z\n\nFirst", "",
            "## 2026-02-05 · A <a@x.com>", "", "- To: b@feedmob.com", "- Gmail ID: `g12`", "",
            "From: A <a@x.com>\nSent At: 2026-02-05T09:00:00Z\n\nSecond", ""]))
        self.assertEqual(pg["frontmatter"], {"date": "2026-02-05", "source": "feedai-email", "thread_id": "1330"})
        self.assertEqual(pg["newest"], "2026-02-06T00:00:00Z")
        self.assertEqual(feeds._write_pages("emails", pages, "2026-10-31T00:00:00Z"), (1, 0))
        self.assertEqual(feeds._write_pages("emails", pages, "2026-10-31T01:00:00Z"), (0, 0))   # 没变不重写
        saved = feeds.cached("emails")[0]
        self.assertEqual(saved["slug"], "emails/1330")
        self.assertTrue(saved["compiled_truth"].startswith("# Spend totals\n"))
        self.assertEqual(saved["queried_at"], "2026-10-31T00:00:00Z")
        self.assertEqual(feeds._write_pages("emails", {}, "x"), (0, 1))                          # 上游没了：删掉

    def test_directory_is_rebuilt_only_when_mail_changed(self):
        pt = {"id": 1, "payload": {"page_content": "From: Ann <ann@acme.com>\nSent At: 2026-02-04T10:00:00Z\n\nHi",
                                   "metadata": {"thread_id": "7", "document_id": "1", "date": "2026-02-04", "subject": "Hi"}}}
        with mock.patch.object(feeds, "scroll", lambda *a, **k: iter([pt])):
            first = feeds.refresh_emails({}, log=lambda *_: None)
            again = feeds.refresh_emails({}, log=lambda *_: None)
        self.assertGreater(first["directory"], 0)
        self.assertTrue(feeds.directory_path().exists())
        self.assertIsNone(again["directory"])                 # 邮件没变：通讯录不重算

    def test_ticket_format_matches_the_brain(self):
        ticket = {"id": "1089", "subject": "Kraken <>BIGO UK#13252", "hs_created_at": "2025-10-06T15:03:59Z",
                  "hs_updated_at": "2026-07-21T03:33:58Z", "status": "Open", "ticket_priority": "HIGH",
                  "pipeline": "Support", "pipeline_stage": "New", "owner": {"first_name": "Ann", "last_name": "Lee"},
                  "content": "please check 777777", "notes": [{"z": 1, "a": "note"}]}
        pages, _ = feeds.ticket_pages([{"id": 9, "payload": {"ticket": ticket}}])
        pg = pages["tickets/1089"]
        self.assertEqual(pg["frontmatter"], {"date": "2026-07-21T03:33:58Z", "source": "feedai-hubspot",
                                             "hubspot_ticket_id": "1089", "hubspot_created_at": "2025-10-06T15:03:59Z",
                                             "hubspot_updated_at": "2026-07-21T03:33:58Z"})
        body = feeds._body(pg["text"])
        self.assertTrue(body.startswith("# Kraken <>BIGO UK#13252\n\n- Status: Open\n- Priority: HIGH\n"))
        self.assertIn("- Owner: Ann Lee\n- Created (建单时间, UTC): 2025-10-06T15:03:59Z", body)
        self.assertTrue(body.endswith('## Notes\n\n- {"a": "note", "z": 1}'))   # 备注字段排好序，内容没变就一字不变


class FakeFemini:
    def __init__(self, token):
        self.token = token

    def call(self, name, args):
        assert name == "get_fireflies_meeting"
        return {"fireflies_meeting": {"utterances": [{"speaker_name": "Bea", "content": "budget  ok",
                                                      "start_time": "2026-09-02T10:00:05Z"}]}}


class Meetings(Sandbox):
    def setUp(self):
        super().setUp()
        day = (TODAY - dt.timedelta(days=1)).isoformat()
        self.day = day
        self.detail = {"id": "FF1AAAAAAAAAAAAAAAAAAAAAAAA1", "title": 'Acme "weekly"', "dateString": f"{day}T10:00:00.000Z",
                       "duration": 30, "host_email": "h@feedmob.com", "transcript_url": "https://app.fireflies.ai/view/x",
                       "speakers": [{"name": "Ann"}], "meeting_attendees": [], "participants": ["a@acme.com"],
                       "summary": {"overview": "ov", "action_items": "Ann: send deck"},
                       "sentences": [{"speaker_name": "Ann", "text": "link 777777   paused", "start_time": 5}]}
        self.feedai = {
            "FF1AAAAAAAAAAAAAAAAAAAAAAAA1": {"id": "FF1AAAAAAAAAAAAAAAAAAAAAAAA1", "title": "dup", "date": f"{day}T10:00:00Z"},
            "FF2BBBBBBBBBBBBBBBBBBBBBBBB2": {"id": "FF2BBBBBBBBBBBBBBBBBBBBBBBB2", "title": "Acme budget", "date": "2026-09-02T10:00:00Z",
                                             "duration": 20, "parent": 1, "chunks": {}, "coll": "c"},
            "FF3CCCCCCCCCCCCCCCCCCCCCCCC3": {"id": "FF3CCCCCCCCCCCCCCCCCCCCCCCC3", "title": "Acme sync", "date": "2026-09-03T10:00:00Z",
                                             "duration": 15, "parent": 2, "chunks": {0: 3}, "coll": "c"}}
        self.femini = {"FF2BBBBBBBBBBBBBBBBBBBBBBBB2": {"id": 1350, "fireflies_id": "FF2BBBBBBBBBBBBBBBBBBBBBBBB2",
                                                        "title": "Acme budget", "utterance_count": 1,
                                                        "participants": [{"name": "Bea", "email": "bea@acme.com"}]}}
        self.gql_calls = []

    def gql(self, query, variables):
        self.gql_calls.append(variables)
        return {"transcript": self.detail}

    def text(self, ref, summary_only):
        if summary_only:
            return "sum " + ref["title"], "", 0
        return "sum " + ref["title"], "Ann: we talked about Acme", 0

    def gong_scroll(self, collection, flt=None, payload=True, batch=1000):
        assert collection == feeds.GONG_COLLECTION
        meta = {"call_id": "123456789", "chunk_index": 0, "title": "AM weekly", "start_time": "2025-11-20T06:30:34-07:00",
                "duration": 600, "url": "https://gong/x", "direction": "Conference"}
        return iter([{"id": 1, "payload": {"metadata": meta, "page_content": "Ann: hi Acme"}}])

    def pull(self, st, full):
        with mock.patch.object(feeds, "_ff_list", return_value=[{"id": self.detail["id"]}]), \
                mock.patch.object(feeds, "_gql", self.gql), mock.patch.object(feeds, "FIREFLIES_SLEEP", 0), \
                mock.patch.object(feeds, "scan_feedai_fireflies", return_value=self.feedai), \
                mock.patch.object(feeds, "Femini", FakeFemini), \
                mock.patch.object(feeds, "scan_femini_meetings", return_value=(self.femini, {"bad": 0, "max_id": 1400})), \
                mock.patch.object(feeds, "fetch_feedai_text", self.text), \
                mock.patch.object(feeds, "scroll", self.gong_scroll):
            return feeds.refresh_meetings(st, full=full, log=lambda *_: None)

    def cached(self, fid):
        idx = feeds._meeting_index()
        return feeds._read(feeds.kind_dir("meetings") / idx[fid]["file"])

    def test_history_must_be_pulled_once_with_full(self):
        with self.assertRaises(RuntimeError):
            self.pull({}, full=False)

    def test_direct_first_then_femini_then_feedai_then_gong(self):
        st = {}
        info = self.pull(st, full=True)
        self.assertTrue(st["fireflies_backfilled"] and st["femini_backfilled"] and st["gong_done"])
        self.assertEqual(st["femini_max_id"], 1400)
        self.assertEqual(info["meetings"], 4)
        direct = self.cached("FF1AAAAAAAAAAAAAAAAAAAAAAAA1")       # Fireflies 直连拿得到的：用直连版本，不用 FeedAI 那份
        self.assertEqual(direct["origin"], "fireflies")
        self.assertEqual(direct["title"], "Acme 'weekly'")          # 和大脑一样：标题里的双引号换成单引号
        self.assertEqual(direct["datetime"], f"{self.day}T10:00:00.000Z")
        self.assertIn(f"**Ann** (10:00:05): link 777777 paused", direct["compiled_truth"])
        self.assertTrue(direct["compiled_truth"].startswith("# Acme \"weekly\"\n\n## 分析"))
        fem = self.cached("FF2BBBBBBBBBBBBBBBBBBBBBBBB2")          # Femini 有逐句：用 Femini，摘要取 FeedAI
        self.assertEqual(fem["origin"], "femini-fireflies")
        self.assertIn("## Summary\n\nsum Acme budget", fem["compiled_truth"])
        self.assertIn("## 全文转录\n\n**Bea** (10:00:05): budget ok", fem["compiled_truth"])
        txt = self.cached("FF3CCCCCCCCCCCCCCCCCCCCCCCC3")          # 只有 FeedAI 的文本
        self.assertEqual(txt["origin"], "feedai-fireflies")
        self.assertIn("## 全文转录（FeedAI 文本，无逐句时间）\n\nAnn: we talked about Acme", txt["compiled_truth"])
        gong = self.cached("gong:123456789")
        self.assertEqual((gong["origin"], gong["source_id"]), ("gong", "gong"))
        self.assertTrue(gong["slug"].startswith("gong/2025-11-20-am-weekly-456789"))
        want, stat = feeds.meetings_in(dt.date(2026, 9, 1), dt.date(2026, 9, 3))
        self.assertEqual(sorted(w["slug"][:20] for w in want), ["meetings/2026-09-02-", "meetings/2026-09-03-"])
        self.assertEqual(stat["source"], "direct")

    def test_recent_meetings_are_not_refetched_every_hour(self):
        st = {}
        self.pull(st, full=True)
        self.assertEqual(len(self.gql_calls), 1)
        self.pull(st, full=False)                                    # 刚拉过（不到 6 小时）：不再取详情
        self.assertEqual(len(self.gql_calls), 1)
        idx = feeds._meeting_index()
        idx["FF1AAAAAAAAAAAAAAAAAAAAAAAA1"]["queried_at"] = "2000-01-01T00:00:00Z"
        feeds._save_meeting_index(idx)
        self.pull(st, full=False)                                    # 最近两天的会、本地那份旧了：重拉
        self.assertEqual(len(self.gql_calls), 2)

    def test_direct_failure_does_not_let_other_versions_take_its_place(self):
        st = {}
        with mock.patch.object(feeds, "_ff_list", side_effect=feeds.FirefliesError("HTTP 500")), \
                mock.patch.object(feeds, "scan_feedai_fireflies", side_effect=AssertionError("直连失败时不该补会")), \
                mock.patch.object(feeds, "scroll", self.gong_scroll):
            with self.assertRaises(RuntimeError) as ctx:
                feeds.refresh_meetings(st, full=True, log=lambda *_: None)
        self.assertIn("Fireflies 直连", str(ctx.exception))
        self.assertFalse(st.get("fireflies_backfilled"))
        self.assertTrue(st["gong_done"])                            # 不相干的 Gong 照样拉完


class RefreshControl(Sandbox):
    def test_fresh_kinds_are_skipped_and_one_failure_does_not_stop_others(self):
        calls = []

        def ok(st, full=False, log=print):
            calls.append("slack")
            return {"pages": 1}

        def boom(st, full=False, log=print):
            calls.append("emails")
            if st.get("ok") is None:
                return {"pages": 2}
            raise RuntimeError("FeedAI down")

        with mock.patch.dict(feeds.RUNNERS, {"slack": ok, "emails": boom}):
            self.assertEqual(feeds.refresh(["slack", "emails"], log=lambda *_: None), {"slack": "ok", "emails": "ok"})
            first = feeds.read_state()["emails"]["as_of"]
            self.assertEqual(feeds.refresh(["slack", "emails"], log=lambda *_: None),
                             {"slack": "skipped", "emails": "skipped"})        # 10 分钟内刷新过
            out = feeds.refresh(["slack", "emails"], force=True, log=lambda *_: None)
            self.assertEqual(out, {"slack": "ok", "emails": "error"})
            st = feeds.read_state()
            self.assertFalse(st["emails"]["ok"])
            self.assertIn("FeedAI down", st["emails"]["error"])
            self.assertEqual(st["emails"]["as_of"], first)                       # 失败不改「数据截至」
            self.assertEqual(feeds.refresh(["emails"], log=lambda *_: None), {"emails": "skipped"})  # 刚失败：过会儿再试
        self.assertEqual(calls, ["slack", "emails", "slack", "emails"])

    def test_ensure_fresh_refuses_before_the_first_full_pull(self):
        with self.assertRaises(RuntimeError):
            feeds.ensure_fresh(log=lambda *_: None)

    def test_ensure_fresh_keeps_going_with_the_existing_copy_when_refresh_fails(self):
        write(feeds.state_path(), {"slack": {"full_at": "x", "as_of": "2026-10-31T00:00:00Z", "ok": True},
                                   "emails": {"as_of": "2026-10-31T00:00:00Z", "ok": False},
                                   "tickets": {"as_of": "2026-10-31T00:00:00Z", "ok": True},
                                   "meetings": {"as_of": "2026-10-31T00:00:00Z", "ok": True, "fireflies_backfilled": True,
                                                "femini_backfilled": True, "gong_done": True}})
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="emails 刷新失败")
        with mock.patch.object(feeds.subprocess, "run", return_value=done) as run:
            info = feeds.ensure_fresh(log=lambda *_: None)
        self.assertEqual(run.call_args[0][0][1:], ["-m", "replay.feeds", "refresh"])
        self.assertEqual(info["slack"]["as_of"], "2026-10-31T00:00:00Z")
        self.assertIn("emails", info["warning"])
        with mock.patch.object(feeds.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIn("先用已有副本", feeds.ensure_fresh(log=lambda *_: None)["warning"])


class DirectCollect(Sandbox):
    def seed_copy(self):
        tickets = {"tickets/42": dict(title="Acme 777777 launch", text="---\ntitle: x\n---\n\n# Acme 777777 launch\n\nbody\n",
                                      frontmatter={"hubspot_created_at": "2026-08-20T00:00:00Z"}, newest="2026-09-01"),
                   "tickets/43": dict(title="Other", text="---\n---\n\n# Other\n\nprice 17777779\n", frontmatter={}, newest="2026-09-02")}
        feeds._write_pages("tickets", tickets, "2026-10-31T00:00:00Z")
        points = [msg(1, "acme-team", "C0AAAAAAA1", "2026-09-03", "10:00:00", "Ann", "click 777777 is live"),
                  msg(2, "acme-team", "C0AAAAAAA1", "2026-09-04", "10:00:00", "Ann", "777777 again"),
                  msg(3, "ops", "C0EEEEEEE1", "2026-09-04", "10:00:00", "Bob", "see #14633"),
                  msg(4, "noise", "C0FFFFFFF1", "2026-09-04", "10:00:00", "Cat", "id 1777777 is not it")]
        with mock.patch.object(feeds, "scroll", lambda *a, **k: iter(points)):
            feeds.refresh_slack({}, log=lambda *_: None)
        write(feeds.state_path(), {"slack": {"as_of": "2026-10-31T08:00:00Z", "ok": True}})
        write(util.raw_dir(N) / "github.json", dict(selected=[dict(repo="r/x", number=14633)]))

    def test_tickets_and_channels_found_by_number_without_the_brain(self):
        self.seed_copy()
        fem = {"click_url": {"campaign": {"name": CAMP["name"]}, "proposed_changes": [{"created_at": "2026-09-02T00:00:00Z"}]}}
        with mock.patch.object(collect_mcp.mcp_client, "brain", side_effect=AssertionError("不该连大脑")):
            man = collect_mcp.collect_direct(N, fem, log=lambda *_: None)
        self.assertEqual(man["source"], "direct")
        self.assertEqual(man["tickets"], ["tickets/42"])                       # 17777779 不算 777777
        self.assertEqual(man["channels"], ["acme-team-c0aaaaaaa1", "ops-c0eeeeeee1"])  # 命中两天的排前面
        self.assertEqual(man["hit_pages"], {"acme-team-c0aaaaaaa1": 2, "ops-c0eeeeeee1": 1})
        self.assertEqual(man["slack_synced_until"], "2026-10-31T08:00:00Z")
        self.assertEqual(man["window"][0], "2026-08-18")                        # 工单建单 08-20 前推 2 天
        rec = util.read_json(util.raw_dir(N) / "tickets" / "42.json")
        self.assertEqual(rec["page"]["frontmatter"]["hubspot_created_at"], "2026-08-20T00:00:00Z")
        self.assertEqual(util.read_json(util.raw_dir(N) / "brain.json")["source"], "direct")

    def test_corpus_reads_only_the_local_copy_in_direct_mode(self):
        raw = util.RAW
        write(raw / "campaigns" / CID / "campaign.json", dict(campaign=CAMP, links=[N]))
        write(raw / "links" / N / "femini.json", dict(click_url=dict(partner=dict(name="Freewheel"))))
        write(raw / "links" / N / "brain.json", dict(channels=["acme-team-c0aaaaaaa1"], window=["2026-09-01", "2026-09-05"],
                                                     source="direct"))
        points = [msg(1, "acme-team", "C0AAAAAAA1", "2026-09-03", "10:00:00", "Ann", "hello team"),
                  msg(2, "random", "C0GGGGGGG1", "2026-09-03", "10:00:00", "Eve", "nothing")]
        with mock.patch.object(feeds, "scroll", lambda *a, **k: iter(points)):
            feeds.refresh_slack({}, log=lambda *_: None)
        idx = {}
        feeds._store_meeting(idx, "FFX", slug="meetings/2026-09-02-acme-weekly-ffx", title="Acme weekly",
                             when="2026-09-02T10:00:00Z", host="", origin="fireflies", source_id="fireflies",
                             text="---\n---\n\n# Acme weekly\n\n---\n\n## 全文转录\n\n**A** (10:00:00): link 777777 paused\n", fid="FFX")
        feeds._save_meeting_index(idx)
        with mock.patch.object(feeds, "enabled", return_value=True), \
                mock.patch.object(collect_mcp.mcp_client, "brain", side_effect=AssertionError("不该连大脑")):
            res = corpus.build(CID, log=lambda *_: None, today=dt.date(2026, 9, 5))
        doc = util.read_json(util.camp_dir(CID) / "corpus.json")
        self.assertEqual(doc["source"], "direct")
        self.assertEqual({r['who'] for r in doc['records'] if r['kind'] == 'slack'}, set())
        self.assertEqual({r['who'] for r in doc['client_background'] if r['kind'] == 'slack'}, {'Ann'})
        self.assertEqual([r["slug"] for r in doc["records"] if r["kind"] == "meeting"], ["meetings/2026-09-02-acme-weekly-ffx"])
        self.assertEqual(doc["stats"]["meeting_scan"]["source"], "direct")
        self.assertEqual(res["meetings"], 1)


if __name__ == "__main__":
    unittest.main()
