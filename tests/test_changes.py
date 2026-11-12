"""后台改动 ↔ 在哪讨论的（replay/changes.py）的测试：假的模型，不联网。"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import business, changes, corpus, util  # noqa: E402

CID = "5555"
TODAY = dt.date(2026, 10, 22)
TERMS = dict(name="Acme_CTV_LG", advertiser="Acme", context=["LG"], links=["777777"], issues=[])
CHANGE_TS = "2026-10-20T12:00:00Z"


def slack(i, ts, who, text, why="channel", channel="#acme"):
    return dict(kind="slack", ts=ts, channel=channel, who=who, text=text, why=why, id=f"R{i:03d}")


def admin(*rows, link="777777"):
    util.write_json(util.camp_dir(CID) / "campaign.json", dict(campaign={}, links=[link]))
    util.write_json(util.raw_dir(link) / "femini.json", dict(click_url=dict(name="Acme LG link", proposed_changes=list(rows))))


def change(id_, ts, user="Dan", **new):
    return dict(id=id_, approve_status="approved", updated_at=ts, user_name=user, **{f"new_{k}": v for k, v in new.items()})


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.saved = [(util, "RAW", util.RAW), (util, "OUT", util.OUT)]
        util.RAW, util.OUT = t / "raw", t / "out"
        util.write_json(util.list_file(), dict(campaigns=[dict(name="Acme_CTV_LG"), dict(name="Acme_CTV_Samsung"), dict(name="Zeta_CTV_LG")]))

    def tearDown(self):
        for mod, name, val in self.saved:
            setattr(mod, name, val)
        self.tmp.cleanup()


class Changes(Sandbox):
    def test_each_link_remembers_its_own_previous_value_and_every_field_is_described(self):
        util.write_json(util.camp_dir(CID) / "campaign.json", dict(campaign={}, links=["777777", "888888"]))
        util.write_json(util.raw_dir("777777") / "femini.json", dict(click_url=dict(proposed_changes=[
            change(1, "2026-10-01 10:00:00 UTC", margin=50.0),
            change(3, "2026-10-03 10:00:00 UTC", margin=25.0, gross_cpi=0.8, effective_at="2026-10-04 00:00:00 UTC"),
            dict(change(4, "2026-10-04 10:00:00 UTC", margin=1.0), approve_status="pending")])))
        util.write_json(util.raw_dir("888888") / "femini.json", dict(click_url=dict(proposed_changes=[
            change(2, "2026-10-02 10:00:00 UTC", margin=30.0)])))
        rows = changes.param_changes(CID)
        self.assertEqual([r["id"] for r in rows], ["1", "2", "3"])                      # 没批准的不算
        self.assertEqual(rows[1]["prev"], {"margin": None})                              # 另一条链接从头算
        self.assertEqual(rows[2]["prev"], {"margin": 50.0, "gross_cpi": None})
        self.assertEqual(changes.describe(rows[2], "zh"), "利润率从 50% 调整至 25%，Gross CPI 调整至 $0.8（2026-10-04 00:00 UTC 起生效）")
        self.assertEqual(changes.describe(rows[2], "en"), "Margin adjusted from 50% to 25%; Gross CPI set to $0.8 (effective 2026-10-04 00:00 UTC)")
        self.assertEqual(changes.describe(rows[0], "en"), "Margin set to 50%")

    def test_gap_reads_naturally(self):
        self.assertEqual(changes.gap(-180, "zh"), "改动前 3 小时")
        self.assertEqual(changes.gap(3, "en"), "3 min after the change")
        self.assertEqual(changes.gap(-1440 * 3, "en"), "3 days before the change")


class Candidates(Sandbox):
    def test_only_messages_about_the_setting_near_the_change_from_people(self):
        admin(change(1, CHANGE_TS, margin=25.0))
        ch = changes.param_changes(CID)[0]
        units = changes.units_of([
            slack(1, "2026-10-20T10:00:00Z", "Ann", "let's lower the LG margin to 25%"),
            slack(2, "2026-10-20T10:05:00Z", "FeedAI", "Daily digest: margin is 25% on LG"),       # 机器人
            slack(3, "2026-10-13T10:00:00Z", "Ann", "LG margin stays at 50%"),                    # 太早
            slack(4, "2026-10-20T11:00:00Z", "Bob", "LG is pacing low today"),                    # 没提利润率
            dict(kind="meeting", ts="2026-10-19T15:00:00Z", title="Acme sync", tier="title", todos="", partial=False, id="R005",
                 text="**Speaker 1** (15:01:00): margin talk\n**Cat** (15:02:00): 把 LG 的margin降到25%")])
        got = changes.candidates(ch, units, corpus.Matcher(TERMS))
        self.assertEqual([(u["who"], u["ts"]) for u in got], [("Cat", "2026-10-19T15:02:00Z"), ("Ann", "2026-10-20T10:00:00Z")])


class Verify(Sandbox):
    def setUp(self):
        super().setUp()
        admin(change(1, CHANGE_TS, margin=25.0))
        self.ch = changes.param_changes(CID)[0]
        self.recs = [
            slack(1, "2026-10-20T10:00:00Z", "Ann", "Can we lower the LG margin to 25% to slow spend?"),
            slack(2, "2026-10-20T11:00:00Z", "Bob", "I'd lower the LG margin to 30%"),
            slack(3, "2026-10-20T11:30:00Z", "Bob", "Samsung margin should go to 25% too"),
            slack(4, "2026-10-20T11:40:00Z", "Bob", "lowering margin on LG and Samsung"),
            slack(5, "2026-10-20T12:10:00Z", "Dan", "done, lowering margin on LG and Samsung"),
            slack(6, "2026-10-20T15:00:00Z", "Eve", "please change the LG margin to 25%"),
            slack(7, "2026-10-20T11:50:00Z", "Bob", "LG is pacing low"),
            slack(8, "2026-10-20T11:55:00Z", "Bob", "Zeta set their LG margin to 25%")]
        self.ids = {u["rec"]["id"]: u for u in changes.units_of(self.recs)}
        self.scope = changes.Scope(TERMS)

    def run_one(self, record, quote, role="reason", translation="译文"):
        return changes.verify({"picks": [dict(record=record, quote=quote, role=role, translation=translation)]}, self.ids, self.scope, self.ch)

    def test_kept_quote_is_verbatim_with_speaker_and_time_from_the_record(self):
        kept, dropped = self.run_one("R001", "can we lower the LG margin to 25%", role="request")
        self.assertEqual(dropped, [])
        self.assertEqual((kept[0]["speaker"], kept[0]["ts"], kept[0]["quote"], kept[0]["minutes"]),
                         ("Ann", "2026-10-20T10:00:00Z", "Can we lower the LG margin to 25%", -120))

    def test_rules_drop_wrong_links(self):
        cases = {
            ("R001", "Can we lower the LG margin to 40%", "request"): "原话查无",
            ("R002", "I'd lower the LG margin to 30%", "request"): "数值对不上",
            ("R003", "Samsung margin should go to 25% too", "request"): "不是本活动的",
            ("R004", "lowering margin on LG and Samsung", "done"): "同时说了好几个平台",     # 不是改动人
            ("R006", "please change the LG margin to 25%", "request"): "请求晚于改动",
            ("R007", "LG is pacing low", "reason"): "没提到这个设置",
            ("R008", "Zeta set their LG margin to 25%", "reason"): "不是本活动的",           # 别的广告主
        }
        for (rid, quote, role), why in cases.items():
            kept, dropped = self.run_one(rid, quote, role)
            self.assertEqual((kept, [d[0] for d in dropped]), ([], [why]), quote)

    def test_the_change_maker_naming_several_platforms_right_after_the_change_counts(self):
        kept, _ = self.run_one("R005", "done, lowering margin on LG and Samsung", role="done")
        self.assertEqual(kept[0]["speaker"], "Dan")

    def test_translation_must_be_the_other_language(self):
        kept, _ = self.run_one("R001", "Can we lower the LG margin to 25%", translation="Can we lower it")
        self.assertEqual(kept[0]["translation"], "")


class Link(Sandbox):
    def setUp(self):
        super().setUp()
        admin(change(1, CHANGE_TS, margin=25.0), change(2, "2026-10-21T12:00:00Z", margin=20.0))
        self.recs = [slack(1, "2026-10-20T10:00:00Z", "Ann", "Can we lower the LG margin to 25% to slow spend?")]

    def ask_with(self, picks):
        calls = []

        def ask(messages):
            calls.append(messages)
            return json.dumps({"picks": picks, "reason_zh": "放慢花费。", "reason_en": "To slow spend."}), {"total_tokens": 1}
        return ask, calls

    def test_found_once_then_cached_and_errors_retry_next_time(self):
        rows = changes.param_changes(CID)

        def down(messages):
            raise RuntimeError("网关 502")
        res, usages = changes.link(CID, rows, self.recs, TERMS, down, log=lambda *_: None)
        self.assertEqual((sorted(res), usages), ([], []))                                 # AI 出错：这次不记，下次再找
        ask, calls = self.ask_with([dict(record="U1", quote="Can we lower the LG margin to 25%", role="request", translation="能把利润率降到 25% 吗")])
        res, usages = changes.link(CID, rows, self.recs, TERMS, ask, log=lambda *_: None)
        self.assertEqual(len(calls), 2)                                                   # 两次改动各问一次
        self.assertEqual(res["1"]["picks"][0]["speaker"], "Ann")
        self.assertEqual(res["1"]["reason_en"], "To slow spend.")
        self.assertEqual(res["2"]["picks"], [])                                           # 「改为 25%」对不上改成 20% 的那次
        self.assertEqual(res["2"]["reason_zh"], "")                                       # 没有讨论就不写原因
        again, calls2 = self.ask_with([])
        res2, _ = changes.link(CID, rows, self.recs, TERMS, again, log=lambda *_: None)
        self.assertEqual((calls2, res2), ([], res))                                       # 没变化：不再问 AI

    def test_attribution_catalog_change_invalidates_cached_discussions(self):
        terms = dict(TERMS, attribution_version='1.0', catalog=[])
        ask, calls = self.ask_with([])
        changes.link(CID, changes.param_changes(CID), self.recs, terms, ask, log=lambda *_: None)
        self.assertEqual(len(calls), 2)
        again, cached_calls = self.ask_with([])
        changes.link(CID, changes.param_changes(CID), self.recs, terms, again, log=lambda *_: None)
        self.assertEqual(cached_calls, [])
        self.assertTrue(changes.notes(CID, 'en', terms=terms))
        changed = dict(terms, catalog=[{'id':'another', 'name':'Another sibling'}])
        self.assertEqual(changes.notes(CID, 'en', terms=changed), {})
        fresh, new_calls = self.ask_with([])
        changes.link(CID, changes.param_changes(CID), self.recs, changed, fresh, log=lambda *_: None)
        self.assertEqual(len(new_calls), 2)

    def test_detail_edition_notes_say_where_who_and_when(self):
        recs = self.recs + [slack(2, "2026-10-21T11:00:00Z", "Bo", "LG 的 margin 降到 20%，花费太快", channel="#acme-ops")]
        ask, _ = self.ask_with([])

        def answer(messages):
            if "changed this link: Margin adjusted from 25% to 20%" in messages[0]["content"]:
                return json.dumps({"picks": [dict(record="U2", quote="LG 的 margin 降到 20%", role="request", translation="Lower the LG margin to 20%")],
                                   "reason_zh": "", "reason_en": ""}), {}
            return json.dumps({"picks": [dict(record="U1", quote="Can we lower the LG margin to 25%", role="request", translation="能把利润率降到 25% 吗")],
                               "reason_zh": "为了放慢花费。", "reason_en": "To slow spend."}), {}
        changes.link(CID, changes.param_changes(CID), recs, TERMS, answer, log=lambda *_: None)
        zh, en = changes.notes(CID, "zh"), changes.notes(CID, "en")
        self.assertEqual(zh["1"], "Slack #acme · Ann · 10-20 10:00 UTC（改动前 2 小时）\n「Can we lower the LG margin to 25%\n译文：能把利润率降到 25% 吗」"
                                  "\n\n原因（据以上原话整理）：为了放慢花费。")
        self.assertEqual(en["2"], "Slack #acme-ops · Bo · 10-21 11:00 UTC (1 h before the change)\n“(translated) Lower the LG margin to 20%”")

    def test_no_candidates_means_checked_and_nothing_found_without_asking(self):
        ask, calls = self.ask_with([])
        res, _ = changes.link(CID, changes.param_changes(CID), [slack(1, "2026-10-20T10:00:00Z", "Ann", "LG is live")], TERMS, ask,
                              log=lambda *_: None)
        self.assertEqual((calls, res["1"]["picks"], res["1"]["candidates"]), ([], [], 0))


class BusinessPage(Sandbox):
    """业务版：后台改动下面列出在哪讨论的；没找到照实说；中英文各自的译文。"""

    def test_change_moment_carries_its_discussion_in_both_languages(self):
        recs = [slack(1, "2026-10-20T09:00:00Z", "Ann", "Bob, please create a tracker for the LG campaign"),
                slack(2, "2026-10-20T10:00:00Z", "Ann", "Can we lower the LG margin to 25% to slow spend?")]
        util.write_json(util.camp_dir(CID) / "corpus.json", dict(campaign=dict(id=5555, name="Acme_CTV_LG"), terms=TERMS, records=recs))
        admin(change(1, CHANGE_TS, margin=25.0), change(2, "2026-10-21T12:00:00Z", margin=20.0))
        meta = dict(title_zh="LG投放历程", title_en="LG campaign journey", summary_zh="概要。", summary_en="Summary.",
                    chapters=[dict(name_zh="准备", name_en="Preparation", start="2026-10-01")], workstreams=[])

        def ask(messages):
            system, user = messages[0]["content"], messages[1]["content"]
            if "You link one change" in system:
                return json.dumps({"picks": [dict(record="U1", quote="Can we lower the LG margin to 25%", role="request",
                                                  translation="我们能把 LG 的利润率降到 25% 吗")],
                                   "reason_zh": "为了放慢花费。", "reason_en": "To slow spend."}), {}
            if "Candidates" in user:
                return json.dumps({"events": [dict(candidate="C001", action_zh="请求跟踪", action_en="Ask for a tracker", context_zh="Ann 请 Bob 准备跟踪。",
                                                   context_en="Ann asks Bob to prepare tracking.", translation="请帮忙创建跟踪", to=[])]}), {}
            if "Key moments" in user:
                return json.dumps(meta), {}
            return json.dumps({"picks": [dict(record="R001", speaker="Ann", quote="please create a tracker for the LG campaign", scope="case", why="ask")]}), {}
        business.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        zh = [e for e in util.read_json(util.out_dir(CID) / "business.case.zh.json")["events"] if e["src"] == "femini"]
        en = [e for e in util.read_json(util.out_dir(CID) / "business.case.en.json")["events"] if e["src"] == "femini"]
        d = zh[0]["discussion"][0]
        self.assertEqual((d["tag"], d["who"], d["when"], d["quote"], d["translation"]),
                         ("Slack #acme", "Ann", "改动前 2 小时", "Can we lower the LG margin to 25%", "我们能把 LG 的利润率降到 25% 吗"))
        self.assertEqual(zh[0]["discussion_reason"], "为了放慢花费。")
        self.assertEqual((en[0]["discussion"][0]["translation"], en[0]["discussion"][0]["when"]), ("", "2 h before the change"))
        self.assertEqual(en[0]["discussion_reason"], "To slow spend.")
        self.assertEqual((zh[1]["discussion"], zh[1]["discussion_reason"]), ([], ""))      # 找过、没找到
        html = (util.out_dir(CID) / "business.zh.html").read_text(encoding="utf-8")
        self.assertIn("discHTML", html)
        self.assertIn("在哪讨论的", html)


if __name__ == "__main__":
    unittest.main()
