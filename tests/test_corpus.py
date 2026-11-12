"""AI 原料（replay/corpus.py）的测试：假的大脑，不联网、不碰真实数据。"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import collect_mcp, corpus, util  # noqa: E402

CID, N = "5555", "777777"
CAMP = {"id": 5555, "name": "Acme_CTV_US_CPM_LG_Video_PMP_Freewheel", "mobile_app": {"name": "Acme – Mobile Banking"}}


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def meeting_page(slug, title, transcript, todos=""):
    body = f"# {title}\n\n## 分析\n\n### Fireflies 自动摘要（仅供参考，勿当结论）\n\n- 摘要不该进原料\n\n"
    if todos:
        body += f"### Fireflies 抽的待办（仅供参考）\n\n{todos}\n\n---\n\n"
    body += f"## 全文转录\n\n{transcript}"
    return dict(slug=slug, title=title, compiled_truth=body, updated_at="2026-10-01T00:00:00.000Z",
                source_id="ken-client", frontmatter={"datetime": "2026-09-01T15:00:00.000Z", "host": "x@feedmob.com"})


class FakeBrain:
    def __init__(self, meetings, channels, search=None):
        self.meetings = {m["slug"]: m for m in meetings}
        self.channels = channels      # {目录: {日期: 正文}}
        self.search = search or {}
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args.get("slug") or args.get("query") or args.get("type") or args.get("tag")))
        if name == "list_pages" and args.get("type") == "meeting":
            rows = [dict(slug=s, title=m["title"], updated_at=m["updated_at"]) for s, m in sorted(self.meetings.items())]
            return rows[args.get("offset", 0):args.get("offset", 0) + args["limit"]]
        if name == "list_pages" and args.get("tag") == "slack":
            rows = [dict(slug=f"channels/{d}/{day}", updated_at=f"{day}T23:00:00.000Z") for d, days in self.channels.items() for day in days]
            return rows[args.get("offset", 0):args.get("offset", 0) + args["limit"]]
        if name == "get_page":
            slug = args["slug"]
            if slug in self.meetings:
                return self.meetings[slug]
            _, d, day = slug.split("/")
            if d in self.channels and day in self.channels[d]:
                return dict(slug=slug, content=self.channels[d][day], updated_at=f"{day}T23:00:00.000Z")
            raise RuntimeError("page_not_found")
        if name == "search":
            return self.search.get(args["query"], [])
        raise AssertionError(name)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.saved = [(util, "RAW", util.RAW), (collect_mcp, "SLACK_CACHE", collect_mcp.SLACK_CACHE)]
        util.RAW = t / "raw"
        collect_mcp.SLACK_CACHE = t / "raw" / "slack"
        self.t = t

    def tearDown(self):
        for mod, name, val in self.saved:
            setattr(mod, name, val)
        self.tmp.cleanup()

    def seed(self, channels=()):
        raw = util.RAW
        write(raw / "campaigns" / CID / "campaign.json", dict(campaign=CAMP, links=[N]))
        write(raw / "links" / N / "femini.json", dict(click_url=dict(partner=dict(name="Freewheel"))))
        write(raw / "links" / N / "github.json", dict(selected=[dict(number=14633)]))
        write(raw / "links" / N / "brain.json", dict(channels=list(channels), window=["2026-09-01", "2026-09-05"]))


class TermsAndMatching(unittest.TestCase):
    def terms(self):
        return corpus.build_terms(CAMP, [N], ["Freewheel"], ["14633"])

    def test_terms_drop_generic_words_and_keep_distinctive_ones(self):
        t = self.terms()
        self.assertEqual(t["advertiser"], "Acme")
        self.assertEqual(t["context"], ["LG", "Freewheel"])  # CTV/US/CPM/Video/PMP 不算特征词

    def test_unambiguous_geo_codes_are_context_terms_and_match_uppercase_only(self):
        camp = {"id": 1, "name": "Acme_CTV_VN_CPM_Kayzen_US_IN", "mobile_app": {"name": "Acme"}}
        t = corpus.build_terms(camp, [], [], [])
        self.assertEqual(t["context"], ["VN", "Kayzen"])           # VN 算特征词；US、IN 太容易和普通英文混，仍排除
        m = corpus.Matcher(t)
        self.assertEqual(m.context("we launched in VN today"), ["VN"])
        self.assertEqual(m.context("the vn team"), [])              # 小写不算
        self.assertEqual(m.context("kayzen is live"), ["Kayzen"])   # 其他特征词不区分大小写

    def test_tiers(self):
        m = corpus.Matcher(self.terms())
        self.assertEqual(m.tier("click 777777 is paused")[0], "direct")
        self.assertEqual(m.tier("see #14633 for details")[0], "direct")
        self.assertEqual(m.tier("Acme, Acme and Acme on LG inventory")[0], "likely")
        self.assertEqual(m.tier("Acme, Acme and Acme again")[0], "mention")
        self.assertEqual(m.tier("Acme mentioned once")[0], "mention")              # 提到 1 次就收（会议里只截那一段）
        self.assertEqual(m.tier("we only talked about LG inventory")[0], None)     # 只提平台不提客户：不是这个客户的事
        self.assertEqual(m.tier("price 17777779 is not the link")[0], None)  # 编号前后不能是数字

    def test_acme_must_be_a_whole_word(self):
        m = corpus.Matcher(self.terms())
        self.assertEqual(m.advertiser("Acmes Acmeish xAcme"), 0)


class MeetingSplit(unittest.TestCase):
    def test_split_keeps_transcript_and_todos_but_not_summary(self):
        pg = meeting_page("meetings/x", "T", "**A** (10:00:01): hi", todos="**Bob**  \nSend report (1:00)")
        body, todos = corpus.split_meeting(pg["compiled_truth"])
        self.assertEqual(body, "**A** (10:00:01): hi")
        self.assertIn("Send report", todos)
        self.assertNotIn("摘要不该进原料", body + todos)

    def test_partial_meeting_keeps_only_windows_around_hits(self):
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], ["Freewheel"], []))
        lines = [f"**A** (10:00:{i:02d}): filler line {i}" for i in range(60)]
        lines[30] = "**A** (10:01:00): Acme Acme Acme on LG"
        cached = dict(meeting_page("meetings/m1", "Weekly sync", "\n".join(lines)), datetime="2026-09-01T15:00:00Z")
        rec = corpus.meeting_record(cached, m)
        self.assertEqual(rec["tier"], "likely")
        self.assertTrue(rec["partial"])
        self.assertIn("Acme Acme Acme", rec["text"])
        self.assertNotIn("filler line 0\n", rec["text"] + "\n")
        self.assertLess(len(rec["text"]), sum(map(len, lines)))

    def test_title_with_advertiser_keeps_whole_meeting(self):
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], [], []))
        cached = dict(meeting_page("meetings/m2", "Acme <> FeedMob Bi-Weekly", "**A** (1:00:00): nothing special\nmore"),
                      datetime="2026-09-01T15:00:00Z")
        rec = corpus.meeting_record(cached, m)
        self.assertEqual((rec["tier"], rec["partial"]), ("title", False))
        self.assertIn("more", rec["text"])

    def test_unrelated_meeting_is_dropped(self):
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], [], []))
        cached = dict(meeting_page("meetings/m3", "Daily", "**A** (1:00:00): we only talked about LG inventory"), datetime="2026-09-01T15:00:00Z")
        self.assertIsNone(corpus.meeting_record(cached, m))

    def test_advertiser_used_as_an_ordinary_word_does_not_count(self):
        camp = {"id": 1, "name": "Chime_CTV_US_CPM_TCL_PMP_Freewheel", "mobile_app": {"name": "Chime – Mobile Banking"}}
        m = corpus.Matcher(corpus.build_terms(camp, [], [], []))
        self.assertEqual(m.advertiser("feel free to chime in, Chime is live"), 1)
        cached = dict(meeting_page("meetings/m4", "Daily", "**A** (1:00:00): feel free to chime in"), datetime="2026-09-01T15:00:00Z")
        self.assertIsNone(corpus.meeting_record(cached, m))

    def test_one_mention_keeps_the_talk_around_it_including_platform_words_and_misheard_names(self):
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], ["Freewheel"], []))
        lines = [f"**A** (10:{i // 60:02d}:{i % 60:02d}): filler line {i}" for i in range(120)]
        lines[20] = "**A** (10:00:20): Acme asked about the budget"
        lines[60] = "**B** (10:01:00): the LG deal is still slow"
        lines[100] = "**C** (10:01:40): free well numbers are off again"
        cached = dict(meeting_page("meetings/m5", "Weekly sync", "\n".join(lines)), datetime="2026-09-01T15:00:00Z")
        rec = corpus.meeting_record(cached, m)
        self.assertEqual((rec["tier"], rec["partial"]), ("likely", True))
        for kept in ("Acme asked", "LG deal", "free well numbers"):
            self.assertIn(kept, rec["text"])
        for dropped in ("filler line 0\n", "filler line 40\n", "filler line 115"):
            self.assertNotIn(dropped, rec["text"] + "\n")

    def test_sounds_like_catches_misheard_partner_names_only(self):
        m = corpus.Matcher(dict(name="X", advertiser="Acme", context=["Freewheel", "Kayzen", "LG"], links=[], issues=[]))
        self.assertEqual(m.sounds_like("the free well numbers"), ["Freewheel"])
        self.assertEqual(m.sounds_like("kaizen dashboard"), ["Kayzen"])
        self.assertEqual(m.sounds_like("free will and freeway"), [])


class SlackRecords(Sandbox):
    def day(self, d, day, lines):
        write(util.RAW / "slack" / d / f"{day}.json",
              dict(queried_at="2026-10-20T00:00:00Z", missing=False, data=dict(content="\n".join(lines))))

    def test_dedicated_keeps_all_shared_keeps_matches_neighbours_and_filters_bots(self):
        self.day("acme-c0abcdefg1", "2026-09-02", ["2026-09-02 10:00:00 Ann: hello", "2026-09-02 10:01:00 Ben: world"])
        self.day("sales-c0abcdefg2", "2026-09-02", [
            "2026-09-02 10:00:00 Ann: lunch?",
            "2026-09-02 10:01:00 Ben: Acme launched on LG",
            "2026-09-02 10:02:00 Cat: ok great",
            "2026-09-02 10:03:00 Dan: unrelated",
            "2026-09-02 10:04:00 Unknown: Acme daily report",
            "2026-09-02 10:05:00 Unknown: Acme_CTV_US_CPM_LG_Video_PMP_Freewheel paused",
        ])
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], ["Freewheel"], []))
        recs = corpus.slack_records(["acme-c0abcdefg1", "sales-c0abcdefg2"], m, "2026-09-01")
        dedicated = [r for r in recs if r["channel"] == "#acme"]
        self.assertEqual(len(dedicated), 2)
        shared = {r["who"]: r["why"] for r in recs if r["channel"] == "#sales"}
        self.assertEqual(shared["Ben"], "match")
        self.assertEqual(shared["Ann"], "context")
        self.assertEqual(shared["Cat"], "context")
        self.assertNotIn("Dan", shared)
        # 机器人只提到广告主名不算，提到活动全名才算
        self.assertEqual([r["text"] for r in recs if r["who"] == "Unknown" and r["why"] == "match"],
                         ["Acme_CTV_US_CPM_LG_Video_PMP_Freewheel paused"])

    def test_messages_before_window_are_dropped(self):
        self.day("acme-c0abcdefg1", "2026-08-01", ["2026-08-01 10:00:00 Ann: old"])
        m = corpus.Matcher(corpus.build_terms(CAMP, [N], [], []))
        self.assertEqual(corpus.slack_records(["acme-c0abcdefg1"], m, "2026-09-01"), [])


class Build(Sandbox):
    def make_brain(self):
        meetings = [
            meeting_page("meetings/2026-09-01-acme-bi-weekly", "Acme <> FeedMob Bi-Weekly", "**A** (1:00:00): link 777777 paused"),
            meeting_page("meetings/2026-09-02-daily", "Daily Sync", "nothing about the advertiser"),
            meeting_page("meetings/2020-01-01-ancient", "Acme <> old", "Acme 777777"),  # 窗口外
        ]
        channels = {
            "acme-c1": {"2026-09-03": "2026-09-03 10:00:00 Ann: hello team"},
            "lg-feedmob-c3": {"2026-09-03": "2026-09-03 10:00:00 Bob: Acme is live"},
            "random-c9": {"2026-09-03": "2026-09-03 10:00:00 Eve: nothing"},
            "sales-c2": {"2026-09-04": "2026-09-04 10:00:00 Cat: Acme budget"},
        }
        return FakeBrain(meetings, channels)

    def test_build_mirrors_all_channels_and_keeps_only_relevant_records(self):
        self.seed()
        brain = self.make_brain()
        res = corpus.build(CID, log=lambda *_: None, brain=brain, today=dt.date(2026, 9, 5))
        doc = util.read_json(util.camp_dir(CID) / "corpus.json")
        chans = doc["stats"]["channels"]
        self.assertEqual(chans["acme-c1"], "频道名含广告主")
        self.assertEqual(chans["lg-feedmob-c3"], "频道名含特征词")
        self.assertEqual(chans["sales-c2"], "内容筛选")  # 频道名无关，靠内容里提到广告主才收进来
        self.assertEqual(chans["random-c9"], "内容筛选")
        senders = {r["who"] for r in doc["records"] if r["kind"] == "slack"}
        self.assertEqual(senders, set())  # 客户频道/客户名本身不是活动归属证据
        background = {r['who'] for r in doc['client_background'] if r['kind'] == 'slack'}
        self.assertEqual(background, {'Ann', 'Bob', 'Cat'})  # 保留为背景，未销毁；Eve无关
        kinds = [(r["kind"], r.get("slug") or r.get("channel")) for r in doc["records"]]
        self.assertIn(("meeting", "meetings/2026-09-01-acme-bi-weekly"), kinds)
        self.assertNotIn(("meeting", "meetings/2026-09-02-daily"), kinds)
        self.assertNotIn(("meeting", "meetings/2020-01-01-ancient"), kinds)
        self.assertEqual([r["id"] for r in doc["records"]], [f"R{i:03d}" for i in range(1, len(doc["records"]) + 1)])
        self.assertEqual(sorted(r["ts"] for r in doc["records"]), [r["ts"] for r in doc["records"]])
        self.assertEqual(res["meetings"], 1)

    def test_second_build_does_not_refetch_unchanged_meetings(self):
        self.seed()
        brain = self.make_brain()
        corpus.build(CID, log=lambda *_: None, brain=brain, today=dt.date(2026, 9, 5))
        first = sum(1 for c in brain.calls if c[0] == "get_page" and str(c[1]).startswith("meetings/"))
        corpus.build(CID, log=lambda *_: None, brain=brain, today=dt.date(2026, 9, 5))
        second = sum(1 for c in brain.calls if c[0] == "get_page" and str(c[1]).startswith("meetings/"))
        self.assertEqual(first, 2)
        self.assertEqual(second, first)

    def test_second_build_does_not_refetch_unchanged_slack_pages(self):
        self.seed()
        brain = self.make_brain()
        corpus.build(CID, log=lambda *_: None, brain=brain, today=dt.date(2026, 9, 5))
        pages = lambda: sum(1 for c in brain.calls if c[0] == "get_page" and str(c[1]).startswith("channels/"))  # noqa: E731
        first = pages()
        corpus.build(CID, log=lambda *_: None, brain=brain, today=dt.date(2026, 9, 5))
        self.assertEqual(first, 4)
        self.assertEqual(pages(), first)

    def test_build_without_campaign_info_fails_clearly(self):
        with self.assertRaises(RuntimeError):
            corpus.build(CID, log=lambda *_: None, brain=FakeBrain([], {}))


if __name__ == "__main__":
    unittest.main()
