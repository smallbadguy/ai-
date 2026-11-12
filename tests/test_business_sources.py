"""业务版：具体的出处标签、GitHub/HubSpot 接单记录（replay/business.py 的 source_tag / source_origin / handoff_events）。"""
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import business, events  # noqa: E402

UI = json.loads((ROOT / "config" / "business_ui.json").read_text(encoding="utf-8"))


class Tags(unittest.TestCase):
    def test_each_source_is_named_specifically(self):
        zh, en = UI["zh"], UI["en"]
        self.assertEqual(business.source_tag(dict(kind="slack", source="#feedmob-chime", fp="a"), zh, {}), "Slack #feedmob-chime")
        self.assertEqual(business.source_tag(dict(kind="meeting", source="AM weekly", fp="m1"), zh, {"m1": "ken-teams"}), "Teams 会议")
        self.assertEqual(business.source_tag(dict(kind="meeting", source="AM weekly", fp="m2"), en, {"m2": "ken-client"}), "Fireflies meeting")
        self.assertEqual(business.source_tag(dict(kind="email", source="Chime x LG", fp="e"), zh, {}), "Gmail 邮件")
        self.assertEqual(business.source_tag(dict(kind="hubspot", source="HubSpot #4945 Chime LG", fp="h"), en, {}), "HubSpot ticket #4945")
        self.assertEqual(business.source_origin(dict(kind="hubspot", source="HubSpot #4945 Chime LG"), zh, True), "工单：Chime LG")
        self.assertEqual(business.source_origin(dict(kind="meeting", source="周会"), en, False), "")   # 英文页不夹中文
        self.assertEqual(business.source_origin(dict(kind="slack", source="#x"), zh, True), "")
        for lang in ("zh", "en"):
            for v in UI[lang]["sources"].values():
                self.assertNotIn(v, ("后台", "后台调整", "投放后台", "邮件", "Email", "Campaign settings"))


class Handoffs(unittest.TestCase):
    def ctx(self):
        class R:
            people = {"gh-a": dict(name="Ann", kind="human"), "gh-b": dict(name="Bob", kind="human"),
                      "gh-bot": dict(name="ci[bot]", kind="bot"), "hs-sys": dict(name="HubSpot", kind="bot")}
        mk = lambda key, ts, src, kind, actor, to=(), ticket=None, hop=None: dict(key=key, ts=ts, src=src, kind=kind, actor=actor,
                                                                                  to=list(to), ticket=ticket, hop=hop)
        evs = [mk("gh:x:created", "2026-08-01T00:00:00Z", "github", "create", "gh-a", ticket="14242"),
               mk("gh:x:1", "2026-08-01T01:00:00Z", "github", "assign", "gh-a", ["gh-b"], "14242", ("assign", "gh-b")),
               mk("gh:x:c1", "2026-08-01T02:00:00Z", "github", "comment", "gh-b", ticket="14242"),
               mk("gh:x:pr", "2026-08-01T03:00:00Z", "github", "merge", "gh-b", ticket="14242"),
               mk("gh:x:2", "2026-08-02T00:00:00Z", "github", "close", "gh-b", ticket="14242", hop=("close",)),
               mk("gh:y:2", "2026-08-02T00:00:00Z", "github", "close", "gh-bot", ticket="14242"),
               mk("hs:4945:created", "2026-07-31T00:00:00Z", "hubspot", "create", "hs-sys", ticket="14242")]
        return dict(events=evs, roster=R(), tickets={"14242": dict(repo="feed-mob/feedmob", num=14242)},
                    hubspot=[dict(key="14242", id="4945", title="Chime LG spend tracking", owner="Yi Han", status="Completed", standalone=False)])

    def test_only_who_took_and_closed_the_work_no_development_details(self):
        with mock.patch.object(events, "build_events", lambda cid, log=None: self.ctx()):
            hs = business.handoff_events("9")
        self.assertEqual([(h["src"], h["kind"], h["actor"]) for h in hs],
                         [("hubspot", "create", "Yi Han"), ("github", "assign", "Bob"), ("github", "close", "Bob")])
        self.assertEqual(hs[1]["title_zh"], "Bob 接手 GitHub Issue #14242")
        self.assertIn("由 Ann 指派", hs[1]["context_zh"])
        self.assertIn("HubSpot 工单 #4945", hs[1]["context_zh"])
        self.assertEqual(hs[0]["context_en"], "Ticket filed; owner Yi Han; current status Completed.")

    def test_missing_detail_data_does_not_break_the_business_edition(self):
        def boom(cid, log=None):
            raise RuntimeError("no data")
        with mock.patch.object(events, "build_events", boom):
            self.assertEqual(business.handoff_events("9", log=lambda *_: None), [])


if __name__ == "__main__":
    unittest.main()


class FixChinese(unittest.TestCase):
    def test_only_english_written_chinese_fields_are_sent_and_filled(self):
        plan = {"events": [dict(action_zh="提高预算", context_zh="Russell raised the budget.", action_en="Raise budget", context_en="Russell raised the budget."),
                           dict(action_zh="确认回传", context_zh="Cathy 确认回传正常。", action_en="Confirm", context_en="Cathy confirmed.")]}
        sent = []

        def ask(messages):
            sent.append(json.loads(messages[1]["content"]))
            return json.dumps({"items": [{"i": 0, "action_zh": "提高预算", "context_zh": "Russell 提高了预算上限。"}]}), {}
        business.fix_chinese(plan, ask, log=lambda *_: None)
        self.assertEqual(len(sent[0]), 1)
        self.assertEqual(plan["events"][0]["context_zh"], "Russell 提高了预算上限。")
        self.assertEqual(plan["events"][1]["context_zh"], "Cathy 确认回传正常。")
        self.assertIsNone(business.fix_chinese(plan, ask, log=lambda *_: None))  # 都是中文了就不再调用
