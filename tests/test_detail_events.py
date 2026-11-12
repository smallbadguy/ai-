"""详细版也收进业务版挑出的对话（replay/events.py 里的 business_events / merge_same_name）。"""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import events, util  # noqa: E402


class FakeRoster:
    def __init__(self):
        self.people, self.index = {}, {}

    def resolve(self, system, account):
        pid = f"{system}-{account}"
        self.people.setdefault(pid, dict(name=account.replace(".", " "), kind="human"))
        return pid


def cand(i, kind, ts, speaker, quote, source):
    return dict(id=f"C{i:03d}", fp=f"fp{i}", kind=kind, ts=ts, speaker=speaker, quote=quote, source=source)


def plan_event(cid, quote_zh="", **kw):
    base = dict(candidate=cid, chapter="c1", action_zh="提出调整", action_en="Proposed a change", context_zh="Ann 提出调整。",
                context_en="Ann proposed a change.", translation="我提议改一下", to=[])
    base.update(kw)
    return base


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = util.RAW
        util.RAW = pathlib.Path(self.tmp.name) / "raw"

    def tearDown(self):
        util.RAW = self.saved
        self.tmp.cleanup()

    def put(self, doc):
        util.write_json(util.camp_dir("9") / "business.json", doc)


class BusinessEvents(Sandbox):
    def test_no_business_data_means_no_extra_events(self):
        self.assertEqual(events.business_events("9", FakeRoster()), [])

    def test_slack_and_meeting_moments_become_events_in_both_languages(self):
        cands = [cand(1, "slack", "2026-07-12T10:00:00Z", "Ann.Lee", "I will send the report on Friday", "#acme"),
                 cand(2, "meeting", "2026-07-14T09:00:00Z", "Ann.Lee", "我们下周再看数据", "每周例会")]
        plan = dict(chapters=[dict(id="c1", name_zh="第一段", name_en="Start")],
                    events=[plan_event("C001", translation="我周五发报告"), plan_event("C002", translation="We will review the data next week")])
        self.put(dict(candidates=cands, plan=plan))
        evs = events.business_events("9", FakeRoster())
        self.assertEqual([e["src"] for e in evs], ["slack", "meeting"])
        slack, meeting = evs
        self.assertEqual(slack["en"]["quote"], "I will send the report on Friday")      # 英文原话：英文页原样
        self.assertEqual(slack["zh"]["translation"], "我周五发报告")                      # 中文页多一句译文
        self.assertEqual(meeting["zh"]["quote"], "我们下周再看数据")                      # 中文原话：中文页原样
        self.assertEqual(meeting["en"]["quote"], "We will review the data next week")   # 英文页放译文，不带汉字
        self.assertNotRegex(str(meeting["en"]), r"[㐀-鿿]")                     # 会议标题有汉字时英文来源里不带
        self.assertIn("每周例会", meeting["zh"]["ref"])
        self.assertTrue(all(e["kind"] == "chat" and e["key"].startswith("bz:") for e in evs))


class MergeSameName(unittest.TestCase):
    def test_same_human_in_two_systems_becomes_one_person(self):
        R = FakeRoster()
        a, b, bot = R.resolve("slack", "Liwei.Breslin"), R.resolve("femini", "Liwei Breslin"), "bot-1"
        R.people[bot] = dict(name="Bot", kind="bot")
        evs = [dict(actor=a, to=[bot]), dict(actor=bot, to=[b]), dict(actor=b, to=[a])]
        events.merge_same_name(evs, R)
        self.assertEqual({e["actor"] for e in evs} - {bot}, {a})
        self.assertEqual(evs[1]["to"], [a])
        self.assertEqual(evs[2]["to"], [])       # 自己对自己不算「交给」

    def test_owner_and_assignment_use_the_merged_person_id(self):
        R = FakeRoster()
        a = R.resolve("slack", "Example.Owner")
        b = R.resolve("hubspot", "Example Owner")
        bot = "bot-example"
        R.people[bot] = dict(name="Example Bot", kind="bot")
        evs = [
            dict(actor=a, to=[], ticket=None),
            dict(actor=bot, to=[b], ticket="t1", hop=("owner", b)),
            dict(actor=bot, to=[b], ticket="t2", hop=("assign", b)),
            dict(actor=bot, to=[], ticket="t2", hop=("unassign", a)),
        ]
        events.merge_same_name(evs, R)
        events.apply_holders(evs)
        self.assertEqual(evs[1]["to"], [a])
        self.assertEqual(evs[1]["hop"], ("owner", a))
        self.assertEqual(evs[1]["holders_pairs"], {a: ("负责人", "Owner")})
        self.assertEqual(evs[2]["holders_pairs"], {a: ("负责", "Assigned")})
        self.assertEqual(evs[3]["holders_pairs"], {})

    def test_holder_only_reference_is_also_merged(self):
        R = FakeRoster()
        a = R.resolve("slack", "Example.Owner")
        b = R.resolve("hubspot", "Example Owner")
        bot = "bot-example"
        R.people[bot] = dict(name="Example Bot", kind="bot")
        evs = [dict(actor=a, to=[], ticket=None),
               dict(actor=bot, to=[], ticket="t1", hop=("owner", b))]
        events.merge_same_name(evs, R)
        events.apply_holders(evs)
        self.assertEqual(set(evs[1]["holders_pairs"]), {a})

    def test_slack_handle_and_mail_name_are_one_person(self):
        R = FakeRoster()
        a, b = R.resolve("slack", "alejandra.segovia"), R.resolve("email", "Alejandra Segovia")
        R.people[a].update(name="alejandra.segovia")
        R.people[b].update(name="Alejandra Segovia", external=True)
        evs = [dict(actor=a, to=[]), dict(actor=b, to=[])]
        events.merge_same_name(evs, R)
        self.assertEqual({e["actor"] for e in evs}, {b})   # 留像姓名的写法
        self.assertTrue(R.people[b]["external"])


class AccountNames(unittest.TestCase):
    def test_dotted_handle_reads_like_a_name(self):
        from replay.people import looks_like_name
        self.assertEqual(looks_like_name("alejandra.segovia"), "Alejandra Segovia")
        self.assertEqual(looks_like_name("toller892"), "toller892")
        self.assertEqual(looks_like_name("Jason hou"), "Jason hou")



class SlackMixTest(unittest.TestCase):
    """详细版的「Slack 副本已读」和来源说明：两条路进来的 Slack 消息都要算。"""

    def ev(self, key, ref):
        return dict(key=key, src="slack", zh=dict(ref=ref), en=dict(ref=ref))

    def test_counts_both_ways_in_and_names_the_channels(self):
        from replay import build
        evs = [self.ev("bz:a:1", "Slack #feedmob-chime · 2026-10-28 22:11:12 UTC"),
               self.ev("bz:b:2", "Slack #feedmob-chime · 2026-10-29 01:00:00 UTC"),
               self.ev("sl:c:3", "Slack #chime-vizio · 2026-10-01 09:00:00 UTC（副本）"),
               dict(key="gh:1", src="github", zh=dict(ref="GitHub #1"), en=dict(ref="GitHub #1"))]
        self.assertEqual(build._slack_mix(evs, "zh"), (3, 1, 2, "#feedmob-chime、#chime-vizio"))
        self.assertEqual(build._slack_mix(evs, "en")[3], "#feedmob-chime, #chime-vizio")

    def test_only_picked_remarks_still_count(self):
        from replay import build
        evs = [self.ev(f"bz:{i}:{i}", f"Slack #c{i} · 2026-10-0{i} 00:00:00 UTC") for i in range(1, 6)]
        n, named, picked, chans = build._slack_mix(evs, "zh")
        self.assertEqual((n, named, picked), (5, 0, 5))
        self.assertEqual(chans, "#c1、#c2、#c3 等 5 个频道")
        self.assertEqual(build._slack_mix([], "en"), (0, 0, 0, ""))


if __name__ == "__main__":
    unittest.main()
