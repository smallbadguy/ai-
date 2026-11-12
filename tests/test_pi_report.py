"""结果工具：AI 用工具增删改查结果，程序组装 JSON、宽松核对原文（假数据，不联网）。"""
import json
import pathlib
import tempfile
import unittest

from replay import pi_report, pi_result, pi_runner, util

SLACK_DAY = ("2026-08-10 16:48:17 Liwei.Breslin: Hi @Cedric Badiola - the net CPM seems off with vizio here, "
             "i marked red, can you pls check the reason? thanks!\n"
             "2026-08-10 17:02:03 Cedric Badiola: Checking now, it looks like the rate card was not updated after 8/1.")
EMAIL = ("Hi Quinn & Golden, Hope you both had a great weekend! Looks like the IPv4 issue has been fixed, "
         "so we're excited to push a bit more volume for the rest of the month. Let us know if TCL can support $1,500/day.")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = pathlib.Path(self.tmp.name) / "work"
        util.write_json(self.work / "seed/brief.json", {"campaign_id": "5555", "vendor_id": None, "terms": {}, "as_of": "2026-11-03T06:00:00Z"})
        util.write_json(self.work / "results/slack.json", {"ok": True, "tool": "feedai_api", "data": {"result": {"points": [
            {"id": "a", "payload": {"page_content": SLACK_DAY, "metadata": {"channel_name": "chime"}}}]}}})
        util.write_json(self.work / "results/email.json", {"ok": True, "tool": "mcp__femini__get_email_thread",
                                                           "data": {"messages": [{"from": "Sarah", "body": EMAIL}]}})
        util.write_json(self.work / "results/failed.json", {"ok": False, "data": {"text": "the secret plan was approved yesterday by everyone"}})

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, op, **args):
        return pi_report.run({"op": op, "work": str(self.work), **args})

    def moment(self, **over):
        return {"kind": "slack", "ts": "2026-08-10T16:48:17Z", "speaker": "Liwei.Breslin", "source": "#chime",
                "quote": "the net CPM seems off with vizio here, i marked red, can you pls check the reason?",
                "belongs_because": "频道 #chime 里点名 Vizio 渠道的 net CPM", "action_zh": "发现 Vizio net CPM 异常",
                "action_en": "Flags Vizio net CPM issue", **over}



class Tools(Base):
    def test_add_find_update_delete_by_id(self):
        r = self.call("add", type="moment", data=self.moment())
        self.assertTrue(r["ok"], r)
        self.assertIn("M001", r["text"])
        r = self.call("add", type="moment", data=self.moment(ts="2026-08-10T17:02:03Z", speaker="Cedric Badiola",
                                                              quote="it looks like the rate card was not updated after 8/1",
                                                              belongs_because="同一讨论串里对上一条的回复",
                                                              action_zh="说明原因：费率卡没更新", action_en="Explains: rate card not updated"))
        self.assertTrue(r["ok"], r)
        report = util.read_json(self.work / "output/report.json")
        self.assertEqual([m["id"] for m in report["moments"]], ["M001", "M002"])
        self.assertEqual(report["moments"][0]["evidence"], {"quote": self.moment()["quote"]})  # 原话照写照存，不找位置
        self.assertEqual(report["status"], "draft")
        listing = self.call("find")["text"]
        self.assertIn("节点 2", listing)
        self.assertIn("M002 | 2026-08-10T17:02", listing)
        self.assertIn('"speaker": "Cedric Badiola"', self.call("find", id="M002")["text"])
        self.assertIn("M001", self.call("find", text="vizio")["text"])
        self.assertIn("M002 |", self.call("find", pattern=r"rate\s+card")["text"])  # 正则，像 grep
        self.assertNotIn("M001 |", self.call("find", pattern=r"rate\s+card")["text"])
        self.assertIn("M002 |", self.call("find", where={"speaker": "cedric"})["text"])  # 按字段
        self.assertNotIn("M002 |", self.call("find", where={"speaker": "liwei"})["text"])
        self.assertFalse(self.call("find", pattern="(")["ok"])
        self.assertNotIn("M002 |", self.call("find", text="vizio")["text"])
        self.assertTrue(self.call("update", id="M002", data={"context_zh": "费率卡 8/1 之后没更新"})["ok"])
        self.assertEqual(util.read_json(self.work / "output/report.json")["moments"][1]["context_zh"], "费率卡 8/1 之后没更新")
        self.assertTrue(self.call("update", id="M002", data={"quote": "words not found in any result"})["ok"])  # 不核对
        self.assertTrue(self.call("delete", id="M001", reason="重复")["ok"])
        report = util.read_json(self.work / "output/report.json")
        self.assertEqual([m["id"] for m in report["moments"]], ["M002"])
        self.assertEqual(report["deleted"][0]["reason"], "重复")
        self.assertIn("M003", self.call("add", type="moment", data=self.moment())["text"])  # 编号不复用
        self.assertTrue(self.call("delete", id="M003")["ok"])  # 原因可不写
        self.call("add", type="moment", data=self.moment(quote="2026-08-10 16:48:17 Liwei.Breslin: Hi", from_file="results/slack.json"))
        self.assertEqual(next(m for m in util.read_json(self.work / "output/report.json")["moments"] if m["id"] == "M004")["evidence"],
                         {"quote": "2026-08-10 16:48:17 Liwei.Breslin: Hi", "file": "results/slack.json"})  # 照 AI 写的存，程序不改

    def test_quotes_are_not_checked_only_page_format_is(self):
        # 原话不核对：查询结果里没有的话也照存（只在提示词里要求写原文）；占位字也不拦（同样只靠提示词）
        self.assertTrue(self.call("add", type="moment", data=self.moment(quote="Sarah agreed to raise the budget"))["ok"])
        self.assertTrue(self.call("add", type="moment", data=self.moment(action_zh="未核实：可能暂停了"))["ok"])
        # 页面离不开的格式：来源类型、发言人、时间、原话、标题
        for bad, word in ((self.moment(kind="chat"), "kind"), ({k: v for k, v in self.moment().items() if k != "speaker"}, "speaker"),
                          (self.moment(ts="last week"), "时间"), (self.moment(quote=" "), "quote"),
                          ({k: v for k, v in self.moment().items() if k not in ("action_zh", "action_en")}, "action")):
            r = self.call("add", type="moment", data=bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn(word, r["error"])
        self.assertTrue(self.call("add", type="moment", data={k: v for k, v in self.moment().items() if k != "action_en"})["ok"])

    def test_batch_add_and_update(self):
        r = self.call("add", items=[
            {"type": "moment", "data": self.moment()},
            {"type": "moment", "data": self.moment(ts="2026-08-10T17:02:03Z", speaker="Cedric Badiola", quote="rate card was not updated",
                                                     action_zh="说明原因", action_en="Explains")},
            {"type": "moment", "data": self.moment(kind="chat")},   # 这一条格式不对
            {"type": "fact", "data": {"ts": "2026-08-10", "quote": "net CPM", "title_zh": "net CPM 异常"}}])
        self.assertTrue(r["ok"], r)
        self.assertIn("已加入 M001", r["text"]); self.assertIn("已加入 M002", r["text"]); self.assertIn("已加入 F001", r["text"])
        self.assertIn("第 3 条没写进去：kind", r["text"])  # 失败的说明原因，成功的照样写入
        report = util.read_json(self.work / "output/report.json")
        self.assertEqual((len(report["moments"]), len(report["facts"])), (2, 1))
        r = self.call("update", items=[{"id": "M001", "data": {"context_zh": "甲"}}, {"id": "M002", "data": {"context_zh": "乙"}},
                                       {"id": "M009", "data": {"context_zh": "丙"}}])
        self.assertTrue(r["ok"]); self.assertIn("第 3 条没写进去", r["text"])
        report = util.read_json(self.work / "output/report.json")
        self.assertEqual([m.get("context_zh") for m in report["moments"]], ["甲", "乙"])
        bad = self.call("add", items=[{"type": "moment", "data": self.moment(kind="chat")}])
        self.assertFalse(bad["ok"])  # 全部失败才算出错
        self.assertFalse(self.call("add", items=[])["ok"])

    def test_quote_in_evidence_and_wrong_scope_are_accepted(self):
        data = {k: v for k, v in self.moment().items() if k != "quote"}
        data["evidence"] = {"quote": "the net CPM seems off", "file": "results/slack.json"}  # 照旧条目的格式写
        data["scope"] = "this_campaign"  # 事项才用的取值
        r = self.call("add", type="moment", data=data)
        self.assertTrue(r["ok"], r)
        self.assertIn("已按 case 处理", r["text"])
        m = util.read_json(self.work / "output/report.json")["moments"][0]
        self.assertEqual((m["evidence"], m["scope"]), ({"quote": "the net CPM seems off", "file": "results/slack.json"}, "case"))
        fact = {"ts": "2026-08-10", "evidence": {"quote": "rate card"}, "title_zh": "费率卡"}
        self.assertTrue(self.call("add", type="fact", data=fact)["ok"])

    def test_items_facts_set_and_finish(self):
        item = {"topic": "确认费率卡", "topic_en": "Confirm rate card", "status": "waiting_on_others", "scope": "this_campaign",
                "state_note": "Cedric 在查", "state_note_en": "Cedric is checking", "next_step": "等 Cedric 回复",
                "next_step_en": "Wait for Cedric", "next_owner": "Cedric Badiola", "owner_basis": "self_commitment",
                "evidence": [{"quote": "Checking now, it looks like the rate card was not updated", "speaker": "Cedric Badiola",
                              "ts": "2026-08-10T17:02:03Z"}]}
        self.assertTrue(self.call("add", type="item", data=item)["ok"])
        fact = {"ts": "2026-08-10", "quote": "the net CPM seems off with vizio here", "title_zh": "net CPM 异常", "title_en": "net CPM anomaly"}
        self.assertTrue(self.call("add", type="fact", data=fact)["ok"])
        report = util.read_json(self.work / "output/report.json")
        self.assertEqual((report["open_items"][0]["id"], report["facts"][0]["id"]), ("I001", "F001"))
        self.assertFalse(self.call("add", type="item", data={**item, "evidence": []})["ok"])  # 事项必须有原话
        self.assertFalse(self.call("add", type="item", data={**item, "owner_basis": "guess"})["ok"])
        nobody = self.call("add", type="item", data={**item, "owner_basis": "none"})
        self.assertTrue(nobody["ok"])
        self.assertEqual(util.read_json(self.work / "output/report.json")["open_items"][1]["next_owner"], "待确认")
        # 完成前必须有至少一个节点和现状总结
        r = self.call("finish")
        self.assertFalse(r["ok"])
        self.assertIn("state_brief", r["error"])
        self.assertIn("节点", r["error"])
        self.call("add", type="moment", data=self.moment())
        for f, v in (("title_zh", "Vizio 费率"), ("title_en", "Vizio rate"), ("summary_zh", "费率卡没更新"),
                     ("summary_en", "Rate card was stale"), ("state_brief", "8/10 发现 net CPM 异常，等 Cedric 确认。")):
            self.assertTrue(self.call("set", field=f, value=v)["ok"])
        self.assertFalse(self.call("set", field="chapters", value=[{"name_zh": "上线"}])["ok"])
        self.assertTrue(self.call("set", field="chapters", value=[{"name_zh": "上线", "name_en": "Launch", "start": "2026-08-01"}])["ok"])
        self.assertFalse(self.call("set", field="status", value="complete")["ok"])  # 状态只能由 finish 改
        self.assertTrue(self.call("finish")["ok"])
        self.assertEqual(util.read_json(self.work / "output/report.json")["status"], "complete")
        self.assertIn("8/10 发现", self.call("find", brief=True)["text"])

    def test_note_appends(self):
        self.assertTrue(self.call("note", text="查完 #chime 8 月", kind="progress")["ok"])
        self.assertTrue(self.call("note", text="追 Cedric 的回复", kind="evidence")["ok"])
        self.assertIn("查完 #chime 8 月", (self.work / "notes/progress.md").read_text())
        self.assertIn("追 Cedric", (self.work / "notes/evidence.md").read_text())
        self.assertFalse(self.call("note", text="x", kind="other")["ok"])


class Refresh(unittest.TestCase):
    def test_new_since_last_gives_all_three_screening_groups_after_previous_run(self):
        material = {"records": [{"id": "R1", "kind": "slack", "ts": "2026-11-02T10:00:00Z", "text": "old"},
                                {"id": "R2", "kind": "slack", "ts": "2026-11-03T02:00:00Z", "text": "new direct", "who": "Liwei",
                                 "attribution": {"reason": "活动名"}}],
                    "client_background": [{"id": "B1", "kind": "email", "ts": "2026-11-03T01:00:00Z", "subject": "Samsung", "text": "bg"}],
                    "excluded": [{"id": "X1", "kind": "slack", "ts": "2026-11-03T03:00:00Z", "attribution": {"reason": "别的活动"}}]}
        got = pi_runner.new_since(material, "2026-11-03T00:45:00Z")
        self.assertEqual([x["id"] for x in got], ["B1", "R2", "X1"])  # 按时间排，三类都给
        self.assertEqual(got[1]["initial_screening"], "初筛判为本活动")
        self.assertEqual(got[2]["screening_reason"], "别的活动")
        self.assertEqual(pi_runner.new_since(material, None), [])

    def test_prompts_have_the_write_rule_and_no_file_pointer_talk(self):
        with tempfile.TemporaryDirectory() as td:
            full = pi_runner.system_prompt(pathlib.Path(td), False).read_text()
            upd = pi_runner.system_prompt(pathlib.Path(td), True).read_text()
        for text in (full, upd):
            self.assertIn("查到就写", text)
            self.assertNotIn("report.json", text)
            self.assertNotIn("pointer", text.lower())
        self.assertIn("## Goal", full)
        self.assertNotIn("Goal", upd)
        self.assertIn("new_since_last.json", upd)
        self.assertIn("report_find(brief=true)", upd)


if __name__ == "__main__":
    unittest.main()
