"""异常排查：描述解析、信号、字典学习规则、程序把关、修复状态、原因阶段不碰 GitHub、接口校验。全部离线。"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import diagnose as D  # noqa: E402
from replay import diagnose_ai as A  # noqa: E402
from replay import knowledge as K  # noqa: E402
from replay import spend_checks as S  # noqa: E402


def row(date, gross=None, net=None, cost=None, imps=0, events=None):
    sources = [{"category": "partner", "data_origin": "partner", "metrics": {"impressions": imps}}] if imps else []
    for origin, m in (events or {}).items():
        sources.append({"category": "mmp", "data_origin": origin, "metrics": m})
    return {"date": date, "recorded_spend": {"gross_spend": gross, "net_spend": net, "gross_spend_source": "imported" if gross is not None else None,
                                             "net_spend_source": "imported" if net is not None else None},
            "sources": sources, "reported_costs": [{"source": "x_reports", "amount": cost}] if cost is not None else []}


def change(cid, when, **kw):
    return {"id": cid, "approve_status": "approved", "user_name": "Liwei", "updated_at": when + " UTC",
            "change_logs": [{"username": "Approver", "comment": "approved", "created_at": when + " UTC"}], **kw}


class QueryParsing(unittest.TestCase):
    def test_link_numbers_are_split_into_this_campaign_and_others(self):
        p = D.parse_query("23660 spend 未录入，还有 99999", ["23660", "11111"])
        self.assertEqual(p["links_in"], ["23660"])
        self.assertEqual(p["links_out"], ["99999"])
        self.assertIn("spend_missing", p["symptom_kinds"])

    def test_line_item_and_github_refs_are_not_link_numbers(self):
        p = D.parse_query("line item 63 匹配失败 GH#14673 2026 年", ["23837"])
        self.assertEqual(p["line_items"], ["63"])
        self.assertEqual(p["gh_refs"], ["14673"])
        self.assertEqual(p["links_out"], [])          # 年份和单号都不算链接

    def test_line_item_words_are_not_taken_as_ids(self):
        self.assertEqual(D.parse_query("line item 匹配失败", [])["line_items"], [])
        self.assertEqual(D.parse_query("line item mapping 有问题", [])["line_items"], [])

    def test_dictionary_phrases_are_matched(self):
        doc = K.load()
        hits = D.parse_query("暂停了还在录", [], doc)["phrase_hits"]
        self.assertIn("cfg.paused_link_job_running", hits)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            D.valid_date("")
        with self.assertRaises(ValueError):
            D.valid_date("2026/10/03")
        with self.assertRaises(ValueError):
            D.valid_range(5)
        self.assertEqual(D.valid_range("7"), 7)


class Signals(unittest.TestCase):
    def test_spend_while_paused(self):
        cu = {"proposed_changes": [change(1, "2026-04-08 16:11:27", new_state="paused", new_state_effective_at="2026-04-10 17:10:26 UTC")]}
        rows = [row("2026-10-02"), row("2026-10-03", 45.0, 0.0)]
        sig = S.link_signals("18894", cu, rows, "2026-10-01", "2026-10-04")
        self.assertEqual([(s["kind"], s["date"]) for s in sig], [("spend_when_paused", "2026-10-03")])
        self.assertEqual(sig[0]["facts"]["state_since"][:10], "2026-04-10")

    def test_state_before_effective_time_is_still_live(self):
        cu = {"proposed_changes": [change(1, "2026-04-08 16:11:27", new_state="paused", new_state_effective_at="2026-04-10 17:10:26 UTC")]}
        self.assertIsNone(S.state_on(cu, "2026-04-09")[0])
        self.assertEqual(S.state_on(cu, "2026-04-10")[0], "paused")

    def test_margin_mismatch_uses_the_margin_in_force(self):
        cu = {"proposed_changes": [change(1, "2026-10-01 00:00:00", new_margin=30.0)]}
        rows = [row("2026-10-05", 120.0, 70.0), row("2026-10-06", 100.0, 70.0)]
        sig = S.link_signals("1", cu, rows, "2026-10-05", "2026-10-06")
        self.assertEqual([s["date"] for s in sig if s["kind"] == "margin_mismatch"], ["2026-10-05"])
        self.assertEqual(sig[0]["facts"]["expected_gross"], 100.0)

    def test_negative_margin_means_not_used(self):
        cu = {"proposed_changes": [change(1, "2026-10-01 00:00:00", new_margin=-1.0)]}
        self.assertEqual(S.margin_on(cu, "2026-10-05")[1], None)

    def test_missing_spend_with_partner_cost(self):
        sig = S.link_signals("1", {}, [row("2026-10-05", cost=50.0, imps=10000)], "2026-10-05", "2026-10-05")
        self.assertEqual([s["kind"] for s in sig], ["spend_missing"])

    def test_no_signal_for_a_quiet_day(self):
        self.assertEqual(S.link_signals("1", {}, [row("2026-10-05")], "2026-10-05", "2026-10-05"), [])

    def test_net_cost_ratio_against_spend_request_factor(self):
        srs = [{"net_spend_formula": "net spend from Remerge * 95%"}]
        self.assertEqual(S.formula_factor(srs), 0.95)
        sig = S.link_signals("1", {}, [row("2026-10-05", 100.0, 90.0, cost=100.0)], "2026-10-05", "2026-10-05", srs)
        self.assertIn("net_cost_ratio_shift", [s["kind"] for s in sig])

    def test_gross_rate_mismatch_for_cpa(self):
        cu = {"client_paid_actions": [{"pricing_model": "CPA", "paid_event": "registration", "rate": "45", "effective_from": "2026-01-01", "effective_to": "2050-01-01"}],
              "client_spend_event_sources": [{"event_data_origin": "raw_af", "source_category": "mmp", "effective_from": "2026-01-01", "effective_to": "2050-01-01"}]}
        ok = S.link_signals("1", cu, [row("2026-10-05", 90.0, 0.0, events={"raw_af": {"registration": 2}})], "2026-10-05", "2026-10-05")
        bad = S.link_signals("1", cu, [row("2026-10-05", 45.0, 0.0, events={"raw_af": {"registration": 2}})], "2026-10-05", "2026-10-05")
        self.assertEqual([s["kind"] for s in ok], [])
        self.assertEqual([s["kind"] for s in bad], ["gross_rate_mismatch"])

    def test_spend_jump_needs_a_baseline(self):
        base = [row(f"2026-09-{d:02d}", 100.0, 70.0) for d in range(20, 30)]
        sig = S.link_signals("1", {}, base + [row("2026-10-01", 500.0, 350.0)], "2026-10-01", "2026-10-01")
        self.assertEqual([s["kind"] for s in sig], ["spend_jump"])


class Dictionary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(K, "KNOWLEDGE", pathlib.Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_seed_is_loaded_and_every_seed_example_is_real_text(self):
        doc = K.load()
        self.assertGreaterEqual(len(doc["entries"]), 19)
        for e in doc["entries"]:
            self.assertTrue(e["examples"], e["id"])
            self.assertIn(e["category"], doc["categories"])
            for p in e["alert_patterns"]:
                re.compile(p)

    def test_two_confirmations_promote_and_rejections_deprecate(self):
        doc = K.load()
        eid = "cfg.stale_margin"
        K.record(doc, eid, "consistent", "c1", phrase="gross 多算")
        self.assertEqual(K.entry(doc, eid)["status"], "seed")
        K.record(doc, eid, "consistent", "c2")
        self.assertEqual(K.entry(doc, eid)["status"], "confirmed")
        self.assertIn("gross 多算", K.entry(doc, eid)["phrases"])
        other = "sys.sheet_data_error"
        for i in range(3):
            K.record(doc, other, "contradict", f"r{i}")
        self.assertEqual(K.entry(doc, other)["status"], "deprecated")
        self.assertNotIn(other, [e["id"] for e in K.prompt_view(doc, [], "2026-10-01")])

    def test_this_case_github_examples_are_hidden_from_the_model(self):
        doc = K.load()
        e = K.entry(doc, "cfg.paused_link_job_running")
        e["examples"] = [dict(source="github", quote="已暂停18894的自动录入spend", scope=dict(links=["18894"], date="2026-10-12"))]
        quotes = lambda v: [x["quote"] for y in v if y["id"] == e["id"] for x in y["examples"]]  # noqa: E731
        self.assertEqual(quotes(K.prompt_view(doc, ["18894"], "2026-10-03")), [])               # 同链接、30 天内：隐藏
        self.assertIn("已暂停18894的自动录入spend", quotes(K.prompt_view(doc, ["18894"], "2026-03-01")))   # 隔得远
        self.assertIn("已暂停18894的自动录入spend", quotes(K.prompt_view(doc, ["23837"], "2026-10-03")))   # 别的链接

    def test_candidate_is_not_duplicated_and_save_writes_changelog(self):
        doc = K.load()
        a = K.add_candidate(doc, dict(name_en="Report sent twice", category="upstream"), "github")
        b = K.add_candidate(doc, dict(name_en="report sent twice"), "github")
        self.assertEqual(a, b)
        self.assertEqual(K.entry(doc, a)["status"], "candidate")
        with K.locked():
            K.save(doc, "test", [a])
        self.assertEqual(K.changelog()[0]["entries"], [a])
        self.assertTrue(K.entry(K.load(), a))


EV = [
    dict(id="E001", src="femini", ts="2026-04-08T17:10:26Z", kind="config_change", actor="Liwei", approver="Yu An",
         text="FeedMob Admin change #70305 on link 18894: state → paused. Submitted by Liwei, approved by Yu An."),
    dict(id="E002", src="slack", ts="2026-10-08T12:41:20Z", kind="message", actor="Mandy Wang",
         text="click url 18894 2026-04-10 pause了，但 CreateChimeSamsungSpendJob每日还在执行 @Yi Han"),
    dict(id="E003", src="hubspot", ts="2026-10-09T01:00:00Z", kind="ticket", actor="", text="HubSpot ticket #9 «Pause job»",
         meta=dict(ticket="9", title="Pause job", status="Closed", owner="Yi Han")),
]
SIG = [dict(kind="spend_when_paused", link="18894", date="2026-10-03", severity="high", facts={}, text_zh="", text_en="")]


class CauseChecks(unittest.TestCase):
    def _out(self, **kw):
        f = dict(entry="cfg.paused_link_job_running", link="18894", dates=["2026-10-03"], title_zh="暂停后仍录入", title_en="Spend after pause",
                 operators=[dict(evidence="E001", role="changed_config"), dict(evidence="E001", role="approved_config"),
                            dict(evidence="E002", role="reported", note_en="raised it")],
                 tickets=["E003"], evidence=[dict(id="E002", quote="CreateChimeSamsungSpendJob每日还在执行")], signals=["S0"], confidence="high")
        f.update(kw)
        return {"symptom_check": {"status": "confirmed"}, "findings": [f]}

    def test_operator_names_come_from_the_records(self):
        res = A.check_cause(self._out(), EV, SIG, {"cfg.paused_link_job_running"}, ["18894"])
        ops = [(o["name"], o["role"]) for o in res["findings"][0]["operators"]]
        self.assertEqual(ops, [("Liwei", "changed_config"), ("Yu An", "approved_config"), ("Mandy Wang", "reported")])
        self.assertEqual(res["findings"][0]["tickets"][0]["status"], "Closed")

    def test_symptom_is_not_confirmed_by_a_neighbouring_day(self):
        nb = [dict(kind="spend_missing", severity="high", link="23203", date="2026-10-31", text_zh="漏录", text_en="missing")]
        out = {"symptom_check": {"status": "confirmed", "explain_zh": "10-31 漏录"}, "findings": []}
        res = A.check_cause(out, EV, nb, set(), ["23203"], date="2026-10-30", query="")
        self.assertEqual(res["symptom"]["status"], "not_confirmed")
        self.assertIn("2026-10-30 当天", res["symptom"]["explain_zh"])
        self.assertIn("2026-10-31", res["symptom"]["explain_zh"])
        self.assertFalse(re.search(r"[㐀-鿿]", res["symptom"]["explain_en"]))
        # 所查那天自己有信号、或用户写了描述、或有结论：不改
        own = [dict(nb[0], date="2026-10-30")]
        self.assertEqual(A.check_cause(out, EV, own, set(), ["23203"], date="2026-10-30", query="")["symptom"]["status"], "confirmed")
        self.assertEqual(A.check_cause(out, EV, nb, set(), ["23203"], date="2026-10-30", query="23203 没录入")["symptom"]["status"], "confirmed")
        self.assertEqual(A.check_cause(self._out(), EV, nb, {"cfg.paused_link_job_running"}, ["18894"], date="2026-10-30", query="")["symptom"]["status"], "confirmed")

    def test_invented_quotes_are_dropped(self):
        res = A.check_cause(self._out(evidence=[dict(id="E002", quote="this sentence was never said")], signals=[]), EV, SIG,
                            {"cfg.paused_link_job_running"}, ["18894"])
        self.assertEqual(res["findings"], [])
        self.assertEqual(len(res["dropped"]), 1)

    def test_github_records_never_count_in_the_cause_stage(self):
        gh = EV + [dict(id="E004", src="github", ts="2026-10-12T00:00:00Z", kind="issue", actor="x", text="已暂停18894的自动录入spend")]
        res = A.check_cause(self._out(evidence=[dict(id="E004", quote="已暂停18894的自动录入spend")], signals=[],
                                      operators=[dict(evidence="E004", role="fixed")]), gh, SIG, {"cfg.paused_link_job_running"}, ["18894"])
        self.assertEqual(res["findings"], [])

    def test_unknown_entry_needs_a_new_entry_description(self):
        res = A.check_cause(self._out(entry="made.up"), EV, SIG, {"cfg.paused_link_job_running"}, ["18894"])
        self.assertEqual(res["findings"], [])
        res = A.check_cause(self._out(entry="made.up", new_entry=dict(name_en="Something new")), EV, SIG, {"cfg.paused_link_job_running"}, ["18894"])
        self.assertEqual(res["findings"][0]["entry"], "new")

    def test_english_fields_never_carry_chinese(self):
        res = A.check_cause(self._out(title_en="暂停"), EV, SIG, {"cfg.paused_link_job_running"}, ["18894"])
        self.assertEqual(res["findings"][0]["title_en"], "")

    def test_verdict_needs_a_verified_quote_from_any_later_source(self):
        recs = [dict(id="G001", src="github", ts="2026-10-12T00:00:00Z", kind="issue", actor="x", text="已暂停18894的自动录入spend",
                     url="https://github.com/feed-mob/x/issues/1", meta=dict(state="closed")),
                dict(id="C001", src="femini", ts="2026-10-09T00:00:00Z", kind="config_change_after", actor="Liwei",
                     text="FeedMob Admin change #1 on link 18894: max switch → False.")]
        out = {"checks": [{"finding": "F0", "verdict": "consistent", "support": [{"id": "G001", "quote": "完全没有这句话的原文内容"}]}]}
        res, _ = A.check_verify(out, [{}], recs)
        self.assertEqual(res[0]["verdict"], "none")
        out["checks"][0]["support"][0]["quote"] = "已暂停18894的自动录入spend"
        res, _ = A.check_verify(out, [{}], recs)
        self.assertEqual((res[0]["verdict"], res[0]["basis"]), ("consistent", ["github"]))
        # 实际使用时 GitHub 里多半没有：之后的后台改动（结构化记录）也能当依据
        out["checks"][0]["support"] = [{"id": "C001"}]
        res, _ = A.check_verify(out, [{}], recs)
        self.assertEqual((res[0]["verdict"], res[0]["basis"]), ("consistent", ["config"]))


class FixStatus(unittest.TestCase):
    RECS = [dict(id="G001", src="github", kind="issue", meta=dict(state="closed")), dict(id="G002", src="github", kind="issue", meta=dict(state="open")),
            dict(id="C001", src="femini", kind="config_change_after")]

    def test_fixed_needs_normal_data_and_an_action(self):
        f = dict(link="1", dates=["2026-10-03"], tickets=[])
        rows = {"1": [{"date": "2026-10-03"}]}
        sig = [dict(link="1", date="2026-10-03", kind="spend_when_paused")]
        self.assertEqual(D.fix_status(f, dict(support=[dict(id="G001")], fix=[]), [], rows, self.RECS)["status"], "fixed")
        self.assertEqual(D.fix_status(f, dict(support=[dict(id="C001")], fix=[]), [], rows, self.RECS)["status"], "fixed")
        self.assertEqual(D.fix_status(f, dict(support=[dict(id="G001")], fix=[]), sig, rows, self.RECS)["status"], "handled_pending_data")
        self.assertEqual(D.fix_status(f, dict(support=[dict(id="G002")], fix=[]), sig, rows, self.RECS)["status"], "in_progress")
        self.assertEqual(D.fix_status(f, dict(support=[], fix=[]), sig, rows, self.RECS)["status"], "no_action")


class FollowUp(unittest.TestCase):
    def test_backtests_need_no_follow_up_and_live_runs_are_checked_four_times(self):
        self.assertEqual(D.schedule("2026-09-01", "2026-10-30T09:00:00Z"), [])                 # 30 天都过去了：回测不用回访
        self.assertEqual(D.schedule("2026-10-03", "2026-10-30T09:00:00Z"), ["2026-11-02"])
        self.assertEqual(D.schedule("2026-10-03", "2026-10-03T09:00:00Z"), ["2026-10-06", "2026-10-10", "2026-10-17", "2026-11-02"])
        self.assertEqual(D.schedule("2026-10-25", "2026-10-30T09:00:00Z"), ["2026-11-01", "2026-11-08", "2026-11-24"])


class NoGithubInCauseStage(unittest.TestCase):
    def test_slack_is_not_selected_by_github_issue_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = pathlib.Path(tmp) / "slack" / "allypod_devs-c0a4vhtduad"
            day.mkdir(parents=True)
            content = ("2026-10-03 01:00:00 Summer Wu: 请看 github.com/feed-mob/feedmob/issues/14673 这个问题\n"
                       "2026-10-03 02:00:00 Summer Wu: 18894 的 spend 录入有问题\n")
            (day / "2026-10-03.json").write_text(json.dumps({"data": {"content": content}, "missing": False}))
            info = {"campaign": {"name": "Chime_Android_US_CPI_Samsung_Banner_Agency"}, "links": ["18894"]}
            m = D.matcher_for(info, {"18894": {"partner": {"name": "Samsung"}}})
            self.assertEqual(m.t["issues"], [])
            with patch.object(D, "RAW", pathlib.Path(tmp)):
                ev = D.slack_evidence(m, [], "2026-10-03", "2026-10-03", "2026-10-03")
            self.assertEqual([e["text"] for e in ev], ["18894 的 spend 录入有问题"])

    def test_femini_wrapper_refuses_write_tools(self):
        class Fake:
            def call(self, name, args):
                return {}
        f = D.ReadOnlyFemini(Fake())
        f.call("get_click_url", {})
        for name in ("create_note", "update_campaign_spend_view", "delete_campaign_spend_view", "create_daily_feed"):
            with self.assertRaises(RuntimeError):
                f.call(name, {})


class Learning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        for p in (patch.object(K, "KNOWLEDGE", root / "knowledge"), patch.object(D, "camp_dir", lambda cid: root / "campaigns" / str(cid))):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, verdict, basis=("config",), entry="cfg.paused_link_job_running", true_cause=None):
        r = dict(id="abcdef0123", campaign=dict(id="2570"), date="2026-10-03", query="18894 暂停后还在录 spend", links=["18894"], stage="done",
                 cause=dict(findings=[dict(entry=entry, link="18894", new_entry=None)]),
                 verify=dict(checks=[dict(verdict=verdict, basis=list(basis) if verdict != "none" else [],
                                          support=[dict(id="C001", src="femini", link="", quote="max switch → False")] if verdict != "none" else [],
                                          fix=[], true_cause=true_cause)], missed=[]))
        D._save(r)
        from replay.util import write_json
        write_json(D.input_path(r), dict(ctx={}, evidence=[], signals=[]))
        return r

    def stats(self, eid="cfg.paused_link_job_running"):
        return K.entry(K.load(), eid)["stats"]

    def test_consistent_from_a_later_config_change_confirms_and_saves_a_regression_case(self):
        r = self._run("consistent")
        D.learn(r)
        self.assertEqual(self.stats()["confirmed"], 1)
        e = K.entry(K.load(), "cfg.paused_link_job_running")
        self.assertIn("暂停后还在录 spend", e["phrases"])          # 编号去掉了
        self.assertEqual(e["examples"][-1]["scope"], dict(links=["18894"], date="2026-10-03"))
        self.assertEqual(len(K.list_cases()), 1)

    def test_no_evidence_waits_for_follow_up_and_counts_as_unverified_only_at_the_end(self):
        r = self._run("none")
        self.assertEqual(D.learn(r), [])
        self.assertEqual(self.stats()["hits"], 0)
        D.learn(r, final=True)
        s = self.stats()
        self.assertEqual((s["hits"], s["unverified"], s["confirmed"]), (1, 1, 0))

    def _pending(self, n=1):
        """MiniMax 还没验证出结果（待回访）的排查，有 n 条结论。"""
        r = self._run("none")
        r["cause"]["findings"] = [dict(r["cause"]["findings"][0]) for _ in range(n)]
        r["verify"]["checks"] = [dict(r["verify"]["checks"][0]) for _ in range(n)]
        r.update(followup=dict(pending=["2026-10-06", "2026-10-10"], done=[], final=False), window=dict(lo="2026-09-30", hi="2026-10-06"), focus=["18894"], ctx={})
        D._save(r)
        root = pathlib.Path(self.tmp.name)
        (root / "state").mkdir(exist_ok=True)
        for p in (patch.object(D, "STATE", root / "state"), patch.object(D, "RAW", root)):
            p.start()
            self.addCleanup(p.stop)
        return r

    def test_manual_verdict_is_learned_at_once_and_replaces_the_pending_state(self):
        r = self._pending()
        out, changes = D.manual_verify("2570", r["date"], r["id"], 0, "consistent", "  Liwei 确认了  ", "a@b.com")
        chk = out["verify"]["checks"][0]
        self.assertEqual((chk["verdict"], chk["basis"]), ("consistent", ["manual"]))
        self.assertEqual(out["manual"]["0"]["by"], "a@b.com")
        self.assertEqual(out["manual"]["0"]["note"], "Liwei 确认了")
        self.assertEqual(self.stats()["confirmed"], 1)
        self.assertTrue(changes)
        self.assertEqual(D.read_run("2570", r["date"], r["id"])["manual"]["0"]["verdict"], "consistent")

    def test_when_every_finding_is_checked_by_a_person_minimax_never_follows_up(self):
        r = self._pending(2)
        D.manual_verify("2570", r["date"], r["id"], 0, "consistent")
        self.assertFalse(D.read_run("2570", r["date"], r["id"])["followup"]["final"])   # 还有一条没人验证
        out, _ = D.manual_verify("2570", r["date"], r["id"], 1, "contradict")
        self.assertEqual(out["followup"], dict(pending=[], done=[], final=True))
        self.assertEqual(D.due_followups("2099-01-01"), [])
        with patch.object(D, "_context", side_effect=AssertionError("MiniMax / Femini must not be called")):
            self.assertIsNotNone(D.followup("2570", r["date"], r["id"], today="2026-10-06"))

    def test_followup_keeps_the_person_s_verdict_over_what_minimax_finds(self):
        r = self._pending(2)
        D.manual_verify("2570", r["date"], r["id"], 0, "partial")
        redone = dict(checks=[dict(verdict="contradict", basis=["later"], support=[], fix=[], true_cause=None),
                              dict(verdict="consistent", basis=["config"], support=[], fix=[], true_cause=None)], missed=[], records=[], usage={})
        info = (dict(campaign=dict(name="x")), ["18894"], None, {"18894": {}}, None)
        with patch.object(D, "_context", return_value=info), patch.object(S, "fetch_metrics", return_value=[]), \
                patch.object(D, "_verify", return_value=(redone, [dict(status="no_action", data="unknown")] * 2, [])):
            out = D.followup("2570", r["date"], r["id"], today="2026-10-06")
        self.assertEqual([c["verdict"] for c in out["verify"]["checks"]], ["partial", "consistent"])
        self.assertEqual(out["followup"]["pending"], ["2026-10-10"])

    def test_a_person_can_change_the_verdict_and_the_dictionary_follows(self):
        r = self._pending()
        D.manual_verify("2570", r["date"], r["id"], 0, "consistent")
        D.manual_verify("2570", r["date"], r["id"], 0, "contradict")
        s = self.stats()
        self.assertEqual((s["confirmed"], s["rejected"]), (0, 1))

    def test_manual_verify_rejects_bad_input(self):
        r = self._pending()
        for args in ((0, "none"), (5, "consistent"), ("x", "consistent")):
            with self.assertRaises(ValueError):
                D.manual_verify("2570", r["date"], r["id"], *args)
        with self.assertRaises(ValueError):
            D.manual_verify("2570", r["date"], "0000000000", 0, "consistent")

    def test_a_run_without_findings_is_never_scheduled_for_follow_up(self):
        r = self._pending()
        r["cause"]["findings"], r["verify"]["checks"] = [], []
        D._save(r)
        self.assertFalse(D.list_runs("2570")[0]["pending"])     # 页面历史里不再标「待验证」
        out = D.followup("2570", r["date"], r["id"], today="2026-10-06")  # 旧记录里残留的回访：直接结掉，不取数、不调 AI
        self.assertTrue(out["followup"]["final"])
        self.assertEqual(out["followup"]["pending"], [])

    def test_each_finding_is_learned_once(self):
        r = self._run("consistent")
        D.learn(r)
        D.learn(r)
        self.assertEqual(self.stats()["confirmed"], 1)

    def test_contradiction_rejects_and_adds_the_true_cause_as_candidate(self):
        D.learn(self._run("contradict", basis=("later",), true_cause=dict(name_en="Partner report sent twice", category="upstream")))
        doc = K.load()
        self.assertEqual(K.entry(doc, "cfg.paused_link_job_running")["stats"]["rejected"], 1)
        self.assertTrue(any(e["name_en"] == "Partner report sent twice" and e["status"] == "candidate" for e in doc["entries"]))
        self.assertEqual(K.list_cases(), [])

    def test_a_follow_up_that_finds_evidence_replaces_the_wait_and_a_changed_verdict_is_undone_first(self):
        r = self._run("none")
        self.assertEqual(D.learn(r), [])
        r["verify"]["checks"][0].update(verdict="consistent", basis=["later"],
                                       support=[dict(id="L001", src="slack", link="", quote="已暂停18894的自动录入spend")])
        D.learn(r)
        self.assertEqual((self.stats()["confirmed"], self.stats()["unverified"]), (1, 0))
        r["verify"]["checks"][0]["verdict"] = "contradict"                       # 重新排查后判断变了：先撤销确认再记否决
        D.learn(r)
        self.assertEqual((self.stats()["confirmed"], self.stats()["rejected"]), (0, 1))

    def test_there_is_no_manual_review(self):
        self.assertFalse(hasattr(D, "review"))

    def test_unrecord_restores_status(self):
        doc = K.load()
        for _ in range(2):
            K.record(doc, "cfg.stale_margin", "consistent", "x")
        self.assertEqual(K.entry(doc, "cfg.stale_margin")["status"], "confirmed")
        K.unrecord(doc, "cfg.stale_margin", "consistent")
        self.assertEqual(K.entry(doc, "cfg.stale_margin")["status"], "seed")


class ServiceRoutes(unittest.TestCase):
    def test_english_panel_has_no_chinese_and_chinese_panel_is_chinese(self):
        from service import diagnose_ui
        en = diagnose_ui.panel("2570", "en", "Example")
        zh = diagnose_ui.panel("2570", "zh", "Example")
        self.assertFalse(re.search(r"[㐀-鿿]", en))
        self.assertIn("异常排查", zh)
        self.assertIn('"2570"', en)

    def test_replay_pages_have_no_diagnosis_entry_it_has_its_own_page(self):
        from service import server, diagnose_ui
        with patch.object(server, "OUT", pathlib.Path(tempfile.gettempdir()) / "nonexistent-out"):
            self.assertNotIn("异常排查", server.nav_bar("2570", "zh", "detail"))  # 回放页面界面不改（2026-11-03）
            page = server.with_nav(b"<html><body>x</body></html>", "2570", "zh", "detail").decode()
            self.assertNotIn("dg-root", page)
        html = diagnose_ui.page("2570", "zh", "Chime_X")
        self.assertIn("异常排查 · Chime_X", html)
        self.assertIn("window.dgOpen()", html)
        self.assertIn('"/?lang=zh"', html)  # 关闭回首页
        self.assertTrue(server.DIAG_PAGE.fullmatch("/c/2570/diagnose"))
        self.assertFalse(server.DIAG_PAGE.fullmatch("/c/2570/diagnose/runs"))

    def test_background_followup_is_off_by_default_and_model_is_flash(self):
        from service import server
        from replay import diagnose_ai
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REPLAY_DIAG_FOLLOWUP", None); os.environ.pop("REPLAY_DIAG_MODEL", None)
            with patch.object(server.pipeline.feeds, "_env_file", return_value={}):
                self.assertFalse(server.followup_on())
                self.assertEqual(diagnose_ai.model(), "minimax-m3.1-flash")
            with patch.dict(os.environ, {"REPLAY_DIAG_FOLLOWUP": "1", "REPLAY_DIAG_MODEL": "other"}):
                self.assertTrue(server.followup_on())
                self.assertEqual(diagnose_ai.model(), "other")


if __name__ == "__main__":
    unittest.main()


class RerunDoesNotDoubleCount(unittest.TestCase):
    def test_rerun_keeps_what_was_already_learned(self):
        with tempfile.TemporaryDirectory() as tmp:
            camp = pathlib.Path(tmp)
            with patch.object(D, "camp_dir", lambda c: camp / str(c)), \
                    patch.object(D.campaign, "info", return_value={"links": ["18894"], "campaign": {"name": "X"}}):
                run, fresh = D.prepare("2570", "2026-10-03", "18894 暂停", 3)
                self.assertTrue(fresh)
                run["learned"] = {"0": {"verdict": "consistent", "entry": "cfg.paused_link_job_running", "by": "auto", "at": "t"}}
                D._save(run, "done")
                again, fresh = D.prepare("2570", "2026-10-03", "18894 暂停", 3)
                self.assertTrue(fresh)
                self.assertEqual(again["learned"]["0"]["entry"], "cfg.paused_link_job_running")


class DateBounds(unittest.TestCase):
    """异常日期只能选「有数据的第一天 ~ 昨天」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.camp = pathlib.Path(self.tmp.name)
        for p in (patch.object(D, "camp_dir", lambda c: self.camp / str(c)),
                  patch.object(D.campaign, "info", return_value={"links": ["24388"], "campaign": {"name": "X"}})):
            p.start()
            self.addCleanup(p.stop)
        from replay.util import write_json
        (self.camp / "5334").mkdir()
        write_json(self.camp / "5334" / "metrics.json", dict(series=dict(
            gross_spend=[["2026-10-22", 0.0], ["2026-10-26", 5.0], ["2026-10-27", 0.0]],
            installs=[["2026-10-22", 0], ["2026-10-26", 3]],
            impressions=[["2026-10-22", 0], ["2026-10-23", 900], ["2026-10-24", 0]])))

    def test_first_day_is_the_earliest_day_with_any_data(self):
        first, last = D.data_bounds("5334")
        self.assertEqual(first, "2026-10-23")           # 曝光最早出现，比 spend 早三天
        self.assertEqual(last, (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat())

    def test_dates_outside_the_range_are_refused_by_the_server(self):
        first, last = D.data_bounds("5334")
        for bad in ("2025-01-01", "2026-10-22", (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()):
            with self.assertRaisesRegex(ValueError, "没有数据可排查"):
                D.prepare("5334", bad, "", 3)
        for ok in (first, "2026-10-27", last):
            self.assertTrue(D.prepare("5334", ok, "", 3)[1])

    def test_no_metrics_means_no_lower_limit(self):
        self.assertEqual(D.data_bounds("9999")[0], None)
        self.assertTrue(D.prepare("9999", "2025-01-01", "", 3)[1])


class Curation(unittest.TestCase):
    """字典自动整理：程序把关和回归兜底（MiniMax、回归集都用假的）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(K, "KNOWLEDGE", pathlib.Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        from replay import curate
        self.C = curate
        doc = K.load()
        self.a = K.add_candidate(doc, dict(name_en="Upstream revisions not backfilled", examples=[dict(source="github", quote="q1")]), "github")
        self.b = K.add_candidate(doc, dict(name_en="Alert backlog not triaged"), "github")
        self.c = K.add_candidate(doc, dict(name_en="Daily automatic spend missing", examples=[dict(source="github", quote="q3")]), "github")
        self.d = K.add_candidate(doc, dict(name_en="Same as upstream revisions"), "github")
        with K.locked():
            K.save(doc, "setup")

    def _d(self, cid, action, into=None, reason="r"):
        return dict(id=cid, action=action, into=into, reason_zh=reason, reason_en=reason)

    def test_program_rules_override_bad_decisions(self):
        doc = K.load()
        K.record(doc, self.b, "consistent", "case")            # 已被验证确认过：不能删
        out = {"decisions": [self._d(self.b, "drop"), self._d(self.a, "merge", "no.such.entry"),
                             self._d(self.d, "merge", self.a), self._d(self.c, "merge", "up.api_empty", reason=""),
                             self._d("cfg.stale_margin", "drop")]}
        plan = self.C.check(doc, out)
        self.assertEqual(plan[self.b]["action"], "keep")
        self.assertEqual(plan[self.a]["action"], "keep")        # 合并目标不存在
        self.assertEqual((plan[self.d]["action"], plan[self.d]["into"]), ("merge", self.a))
        self.assertNotIn(self.c, plan)                          # 没有理由的决定不算
        self.assertNotIn("cfg.stale_margin", plan)              # 种子不归整理管

    def test_merge_chains_resolve_and_merging_into_a_dropped_entry_keeps_it(self):
        doc = K.load()
        plan = self.C.check(doc, {"decisions": [self._d(self.d, "merge", self.a), self._d(self.a, "merge", "up.api_empty"),
                                                self._d(self.c, "merge", self.b), self._d(self.b, "drop")]})
        self.assertEqual(plan[self.d]["into"], "up.api_empty")
        self.assertEqual(plan[self.c]["action"], "keep")

    def test_merge_moves_evidence_and_later_learning_follows_it(self):
        doc = K.load()
        K.merge_into(doc, self.c, "up.api_empty", "same root cause")
        e, t = K.entry(doc, self.c), K.entry(doc, "up.api_empty")
        self.assertEqual((e["status"], e["merged_into"]), ("merged", "up.api_empty"))
        self.assertIn("q3", [x["quote"] for x in t["examples"]])
        self.assertNotIn(self.c, [x["id"] for x in K.active(doc)])
        K.record(doc, self.c, "consistent", "case")             # 旧 id 记到合并后的条目上
        self.assertEqual((t["stats"]["confirmed"], e["stats"]["confirmed"]), (1, 0))
        K.drop(doc, self.b, "alert noise")
        K.record(doc, self.b, "consistent", "case")
        self.assertEqual(K.entry(doc, self.b)["status"], "deprecated")   # 删掉的不会因战绩复活

    def test_curation_is_rolled_back_when_the_regression_set_gets_worse(self):
        from replay.util import write_json
        K.save_case("c1", dict(id="c1", expected=["up.api_empty"], input={}))
        write_json(K.regression_path(), {"runs": [{"at": "t", "hit_rate": 1.0}]})
        ask = lambda msgs: ({"decisions": [self._d(self.b, "drop", reason="alert noise")]}, {})  # noqa: E731
        rep = self.C.run(force=True, ask=ask, regress=lambda **kw: {"hit_rate": 0.5}, log=lambda *_: None)
        self.assertTrue(rep["rolled_back"])
        self.assertEqual(K.entry(K.load(), self.b)["status"], "candidate")
        rep = self.C.run(force=True, ask=ask, regress=lambda **kw: {"hit_rate": 1.0}, log=lambda *_: None)
        self.assertFalse(rep["rolled_back"])
        self.assertEqual(K.entry(K.load(), self.b)["status"], "deprecated")
        self.assertEqual(K.changelog()[0]["actor"], "curate")

    def test_maybe_skips_when_nothing_changed(self):
        calls = []
        ask = lambda msgs: (calls.append(1) or {"decisions": []}, {})  # noqa: E731
        self.C.run(force=True, ask=ask, log=lambda *_: None)
        self.assertIsNone(self.C.run(ask=ask, log=lambda *_: None))
        self.assertEqual(len(calls), 1)
