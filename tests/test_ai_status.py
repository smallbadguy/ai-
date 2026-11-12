"""AI 运行状态页：从运行目录读出阶段、AI 的话、工具调用、审核结论和正在输出的内容（假数据，不联网）。"""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from replay import util
from service import ai_status


def line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


class AiStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.raw, self.state = self.tmp / "raw", self.tmp / "state"
        self.state.mkdir()
        self.run = self.raw / "campaigns/5169/pi-runs/20261103T035041Z-8e37b2"
        (self.run / "work/notes").mkdir(parents=True)
        (self.run / "work/output").mkdir(parents=True)
        (self.run / "work/seed").mkdir(parents=True)
        (self.run / "review-1").mkdir()
        util.write_json(self.run / "review-1/verdict.json", {"decision": "pass"})
        util.write_json(self.run / "work/seed/brief.json", {"mode": "update"})
        util.write_json(self.run / "work/output/report.json", {"status": "draft", "moments": [1, 2, 3], "open_items": [1],
                                                               "coverage": {"remaining": ["邮件第 3 页"]}})
        (self.run / "work/notes/progress.md").write_text("已查：Slack 9 个频道\n下一步：邮件")
        ms = 1793680000000
        events = [
            {"type": "message_end", "message": {"role": "user", "timestamp": ms, "content": [{"type": "text", "text": "这是增量更新，先读上次结果"}]}},
            {"type": "turn_start"},
            {"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": "部分"}]}},
            {"type": "message_end", "message": {"role": "assistant", "timestamp": ms + 1000,
                                                "content": [{"type": "thinking", "thinking": "先看邮件列表"}, {"type": "text", "text": "接下来查 10 月邮件"}]}},
            {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "feedai_api", "args": {"operation": "scroll", "collection": "email_documents_v3", "body": {"limit": 50}}},
            {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "feedai_api", "isError": False, "durationMs": 1200,
             "result": {"content": [{"type": "text", "text": "Source evidence saved: results/a.json"}]}},
            {"type": "tool_execution_start", "toolCallId": "c2", "toolName": "mcp__femini__get_campaign", "args": {"id": 5169}},
            {"type": "tool_execution_end", "toolCallId": "c2", "toolName": "mcp__femini__get_campaign", "isError": True, "durationMs": 300,
             "result": {"content": [{"type": "text", "text": "bad id"}]}},
        ]
        (self.run / "events.jsonl").write_text("".join(line(e) for e in events))
        util.write_json(self.run / "run.json", {"engine": "pi-online-v3", "model": "feedmob/minimax-m3.1-flash", "status": "completed",
                                                "started_at": "2026-11-03T03:50:57Z", "seconds": 2637.4,
                                                "reviews": [{"round": 1, "decision": "pass", "summary": "可以给业务同事看", "suggestions": []}]})
        self.patches = [patch.object(util, "RAW", self.raw), patch.object(util, "STATE", self.state)]
        for p in self.patches:
            p.start()
        ai_status._CACHE.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_run_page_reads_messages_tools_and_review(self):
        page = ai_status.run_page("5169", None, self.run, "zh", name=lambda n: "Chime_CTV_US_CPM_Agency_NEW_L")
        self.assertIn("已完成", page)
        self.assertIn("增量更新", page)
        self.assertIn("接下来查 10 月邮件", page)
        self.assertIn("先看邮件列表", page)              # 思考在折叠区里
        self.assertIn("FeedAI · scroll · email_documents_v3", page)
        self.assertIn("后台 get_campaign", page)
        self.assertIn("报错", page)
        self.assertIn("这是增量更新", page)              # 程序提示
        self.assertIn("可以给业务同事看", page)          # 审核结论
        self.assertIn("已查：Slack 9 个频道", page)      # AI 自己的进度记录
        self.assertNotIn(">部分<", page)                 # 逐字输出的中间状态不进记录
        self.assertIn("3 / 1", page)                     # 草稿节点 / 事项
        self.assertLess(page.index("接下来查 10 月邮件"), page.index("可以给业务同事看"))

    def test_live_output_and_phase_while_running(self):
        util.write_json(self.run / "run.json", {"status": "running", "started_at": "2026-11-03T03:50:57Z", "reviews": []})
        (self.run / "review-1/verdict.json").unlink()
        util.write_json(self.state / "5169.json", {"running": True})
        with open(self.run / "events.jsonl", "a") as fh:
            fh.write(line({"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": "正在写 10/28 的节点"}]}}))
        page = ai_status.run_page("5169", None, self.run, "zh")
        self.assertIn("审核中（第 1 轮）", page)
        self.assertIn("正在写 10/28 的节点", page)
        self.assertIn("AI · 正在输出", page)

    def test_list_page_and_lookup_guard(self):
        page = ai_status.list_page("zh", name=lambda n: "Chime_CTV_US_CPM_Agency_NEW_L")
        self.assertIn("Chime_CTV_US_CPM_Agency_NEW_L", page)
        self.assertIn("run=20261103T035041Z-8e37b2", page)
        self.assertEqual(ai_status.find_run("5169", None, "20261103T035041Z-8e37b2"), self.run)
        self.assertIsNone(ai_status.find_run("5169", None, "../../secrets"))
        self.assertIsNone(ai_status.find_run("51x9", None, "20261103T035041Z-8e37b2"))

    def test_broken_report_does_not_break_pages(self):
        (self.run / "work/output/report.json").write_text('{"moments": [ "未闭合')
        self.assertIn("Chime", ai_status.list_page("zh", name=lambda n: "Chime"))
        self.assertIn("已完成", ai_status.run_page("5169", None, self.run, "zh"))

    def test_stopped_run_time_and_counts_come_from_its_own_events(self):
        # 被中止的运行：run.json 停在 running、统计全是 0；用时算到最后一次有动静，轮数和工具调用从事件里数
        import os
        util.write_json(self.run / "run.json", {"status": "running", "started_at": "2026-11-03T03:50:57Z",
                                                "turns": 0, "tools": {}, "tool_errors": 0})
        start = ai_status._ts("2026-11-03T03:50:57Z")
        for f in ("events.jsonl", "run.json"):
            os.utime(self.run / f, (start + 1800, start + 1800))
        info = ai_status._row_info("5169", None, self.run, True)
        self.assertEqual(info["label"], "已中止")
        self.assertEqual(int(info["elapsed"]), 1800)
        self.assertEqual((info["counts"]["turns"], info["counts"]["tools"]), (1, 2))
        self.assertIn("30 分钟", ai_status.list_page("zh"))

    def test_thinking_only_turn_is_one_line_and_status_is_chinese(self):
        with open(self.run / "events.jsonl", "a") as fh:
            fh.write(line({"type": "message_end", "message": {"role": "assistant", "timestamp": 1793680005000,
                                                              "content": [{"type": "thinking", "thinking": "再翻第 4 页邮件\n看有没有 TCL"}]}}))
        util.write_json(self.run / "work/output/report.json", {"status": "complete", "moments": [1], "open_items": []})
        page = ai_status.run_page("5169", None, self.run, "zh")
        self.assertNotIn("只有思考", page)
        self.assertIn("AI 思考 · ", page)
        self.assertIn("再翻第 4 页邮件 看有没有 TCL</summary>", page)
        self.assertIn("AI 标为已写完", page)
        self.assertNotIn("· complete", page)

    def test_old_stopped_runs_are_not_shown_running_when_campaign_runs_again(self):
        # 以前被停掉的运行，run.json 停在 running；同一活动又开始刷新时，它不能也显示「调查中」
        util.write_json(self.run / "run.json", {"status": "running", "started_at": "2026-11-03T03:50:57Z", "reviews": []})
        util.write_json(self.state / "5169.json", {"running": True, "started_at": "2026-11-03T06:43:04Z"})
        self.assertEqual(ai_status.phase("5169", None, self.run, {"status": "running"}, True)[0], "已中止")  # 新一轮还在取数
        new = self.run.parent / "20261103T064500Z-abcdef"
        new.mkdir()
        util.write_json(new / "run.json", {"status": "running", "started_at": "2026-11-03T06:45:00Z"})
        self.assertEqual(ai_status.phase("5169", None, self.run, {"status": "running"}, True)[0], "已中止")
        self.assertEqual(ai_status.phase("5169", None, new, {"status": "running"}, True), ("调查中", True))

    def test_prefetch_stage_shows_before_agent_starts(self):
        util.write_json(self.state / "4947.json", {"running": True, "started_at": "2026-11-03T05:00:00Z",
                                                   "current": {"steps": [{"step": "更新沟通记录"}]}})
        page = ai_status.list_page("zh")
        self.assertIn("取数初筛", page)
        self.assertIn("更新沟通记录", page)


if __name__ == "__main__":
    unittest.main()
