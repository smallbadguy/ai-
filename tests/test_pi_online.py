"""Online-only Pi integration: fake APIs and temporary data, no remote writes or model calls."""
import io
import os
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from replay import pi_tools, pi_result, pi_runner, pipeline, util, scope


class OnlineTools(unittest.TestCase):
    def test_feedai_read_passthrough_and_pagination(self):
        response={"result":{"points":[],"next_page_offset":"next"}}
        with patch.object(pi_tools.feeds,"_qdrant",return_value=response) as call:
            got=pi_tools.feedai({"operation":"scroll","collection":"slack_api_messages_v3","body":{"offset":"prev","filter":{"must":[]}}})
            self.assertIs(got,response)
            self.assertEqual(call.call_args.args[1]["offset"],"prev")
            self.assertEqual(call.call_args.args[0],"/collections/slack_api_messages_v3/points/scroll")
            self.assertFalse(call.call_args.args[1]["with_vector"])

    def test_real_minimax_array_and_boolean_wrappers(self):
        condition={"key":"metadata.channel_id","match":{"value":"CALFU62NS"}}
        date={"key":"metadata.timestamp","range":{"gte":"2026-06-11T12:00:00Z","lte":"2026-06-13T23:00:00Z"}}
        body={"filter":{"must":{"item":[condition,date]}},"limit":"60","with_payload":"true"}
        before=json.dumps(body,sort_keys=True)
        with patch.object(pi_tools.feeds,"_qdrant",return_value={}) as call:
            pi_tools.feedai({"operation":"scroll","collection":"slack_api_messages_v3","body":body})
            sent=call.call_args.args[1]
        self.assertEqual(sent["filter"],{"must":[condition,date]})
        self.assertIs(sent["with_payload"],True)
        self.assertEqual(sent["limit"],60)
        self.assertEqual(json.dumps(body,sort_keys=True),before)
        self.assertEqual(pi_tools.request_body({"filter":{"must":{"item":condition}}})["filter"],{"must":[condition]})
        self.assertEqual(pi_tools.request_body({"filter":condition})["filter"],{"must":[condition]})
        self.assertEqual(pi_tools.request_body({"filter":{"must":{"item":{"item":condition}}}})["filter"],{"must":[condition]})
        with patch.object(pi_tools.feeds,"_qdrant") as call:
            with self.assertRaisesRegex(ValueError,"不要用 from/to"):
                pi_tools.feedai({"operation":"scroll","collection":"slack_api_messages_v3","body":{"filter":{"must":[{"key":"metadata.timestamp","range":{"from":"2026-10-01","to":"2026-10-05"}}]}}})
            call.assert_not_called()

    def test_transport_coercion_never_changes_match_values(self):
        condition={"key":"metadata.document_id","match":{"value":"000123"}}
        got=pi_tools.request_body({"filter":{"must":[condition],"should":None},"with_payload":{"include":{"item":["metadata"]}}})
        self.assertEqual(got["filter"],{"must":[condition],"should":None})
        self.assertEqual(got["with_payload"],{"include":["metadata"]})
        self.assertEqual(pi_tools.request_body({"ids":{"item":[123,"uuid"]}})["ids"],[123,"uuid"])

    def test_writes_and_arbitrary_endpoints_blocked(self):
        with patch.object(pi_tools.feeds,"_qdrant") as q:
            for op in ("upsert","delete","update","set_payload"):
                with self.assertRaises(ValueError):pi_tools.feedai({"operation":op,"collection":"slack_api_messages_v3"})
            with self.assertRaises(ValueError):pi_tools.feedai({"operation":"scroll","collection":"../../secrets"})
            q.assert_not_called()
        with patch.object(pi_tools.github,"GH") as gh:
            for path in ("https://example.org", "repos/feed-mob/x/../../user", "user", "repos/another-org/x/issues", "repos/feed-mob/x/issues?method=POST"):
                with self.assertRaises(ValueError):pi_tools.github_read({"path":path})
            gh.assert_not_called()
        with patch.object(pi_tools.feeds,"_gql") as ff:
            with self.assertRaises(ValueError):pi_tools.fireflies({"operation":"mutation"})
            ff.assert_not_called()
        self.assertFalse(any(n.startswith(("create_","update_","delete_")) for n in pi_tools.FEMINI_READ))

    def test_github_does_not_limit_to_seed_issues(self):
        with patch.object(pi_tools.github,"GH") as gh:
            pi_tools.github_read({"path":"repos/feed-mob/another-project/issues/98765/comments","params":{"page":2}})
            self.assertEqual(gh.return_value.get.call_args.args[0],"repos/feed-mob/another-project/issues/98765/comments")
            self.assertEqual(gh.return_value.get.call_args.kwargs["params"]["page"],2)


class Result(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
        self.raw=patch.object(util,"RAW",self.root/"raw");self.out=patch.object(util,"OUT",self.root/"out")
        self.raw.start();self.out.start()
        self.work=self.root/"run/work"
        self.spec={"file":"results/request.json","pointer":"/data/record","quote":"Please launch this link tomorrow."}
        util.write_json(self.work/"results/request.json",{"ok":True,"tool":"github_api","data":{"record":{"body":self.spec["quote"],"author":"Ann Example"}}})
        terms={"name":"Acme_CTV_US","advertiser":"Acme","links":["777777"],"context":[],"issues":[]}
        util.write_json(self.work/"seed/brief.json",{"campaign_id":"5555","vendor_id":None,"terms":terms,"as_of":"2026-11-02"})
        util.write_json(self.work/"seed/changes.json",[]);util.write_json(self.work/"seed/handoffs.json",[])
        self.report={"status":"complete","title_zh":"Acme 上线回放","title_en":"Acme launch","summary_zh":"确认上线要求。","summary_en":"A launch was requested.",
            "state_brief":"10-01 Ann 请求次日上线链接 777777，尚无执行确认。",
            "chapters":[{"name_zh":"上线","name_en":"Launch","start":"2026-10-01"}],
            "moments":[{"kind":"github","speaker":"Ann Example","ts":"2026-10-01","source":"#123",
                "evidence":self.spec,"scope":"case","belongs_because":"在线读取完整工单后确认链接，初筛没有这条",
                "action_zh":"请求上线","action_en":"Request launch","context_zh":"请求明天上线。","context_en":"Launch is requested for tomorrow.",
                "translation":"请明天上线这条链接。","to":[]}],
            "backend_changes":[],"handoffs":[],"facts":[],"people":[],"workstreams":[],"open_items":[],
            "coverage":{"searched":["GitHub"],"not_available":[],"remaining":[]},"unresolved":[]}
        util.write_json(self.work/"output/report.json",self.report)

    def tearDown(self):
        self.raw.stop();self.out.stop();self.tmp.cleanup()

    def test_online_evidence_not_in_first_pass_still_renders(self):
        result=pi_result.publish("5555",self.work)
        self.assertEqual(result["moments"],1)
        case=util.read_json(util.out_dir("5555")/"business.case.zh.json")
        self.assertEqual(case["meta"]["engine"],"pi-online-v3")
        self.assertEqual(case["events"][0]["prec"],"day")
        self.assertEqual(case["events"][0]["quote"],self.spec["quote"])
        self.assertTrue((util.out_dir("5555")/"business.en.html").exists())

    def test_grouped_changes_do_not_fabricate_a_single_delta(self):
        util.write_json(self.work/"seed/changes.json",[
            {"id":"1","ts":"2026-10-01T12:00:00Z","user":"Ops Example","link":"777777","new":{"margin":20},"prev":{},"when":{}},
            {"id":"2","ts":"2026-10-02T12:00:00Z","user":"Ops Other","link":"777777","new":{"margin":30},"prev":{"margin":20},"when":{}}])
        self.report["backend_changes"]=[{"ids":["1","2"],"title_zh":"两次利润率调整","title_en":"Two margin adjustments",
            "context_zh":"十月一日至二日先调至20%，再调至30%。","context_en":"Set to 20% on October 1 and 30% on October 2."}]
        util.write_json(self.work/"output/report.json",self.report)
        cases,state,_=pi_result.adapt("5555",self.work)
        grouped=[e for e in cases["zh"]["events"] if e["src"]=="femini"]
        self.assertEqual(len(grouped),1)
        self.assertEqual(grouped[0]["ts"],"2026-10-02T12:00:00Z")
        self.assertIsNone(grouped[0]["margin"])
        self.assertEqual(state["plan"]["backend_changes"][0]["ids"],["1","2"])

    def test_normal_exit_without_output_is_not_forced_to_continue(self):
        (self.work/"output/report.json").unlink()
        with patch.object(pi_runner,"prepare",return_value=self.work.parent),patch.object(pi_runner,"configure",return_value=(["pi"],{})),patch.object(pi_runner.subprocess,"Popen") as popen:
            p=popen.return_value;p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0
            with self.assertRaisesRegex(RuntimeError,"未输出回放文件"):
                pi_runner.execute("5555",log=lambda *_:None)
            self.assertEqual(popen.call_count,1)  # 没有会话文件可接着做，就不续跑
            self.assertEqual(popen.call_args.kwargs["stdin"],pi_runner.subprocess.DEVNULL)

    def test_unfinished_run_is_reminded_in_same_session(self):
        run=self.work.parent
        (run/"session").mkdir();(run/"session/s.jsonl").write_text("{}\n")
        draft={**self.report,"status":"draft","moments":[]}
        util.write_json(self.work/"output/report.json",draft)
        calls=[]
        def fake(cmd,**kw):
            calls.append(cmd)
            if len(calls)==2:util.write_json(self.work/"output/report.json",self.report)  # 第二次接着做完
            p=unittest.mock.MagicMock();p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0;return p
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{})),\
             patch.object(pi_runner,"reviews_on",return_value=False),patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            got=pi_runner.execute("5555",log=lambda *_:None)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[1][1:3],["--session",str(run/"session/s.jsonl")])
        self.assertIn("还没有任何关键节点",calls[1][-1])
        self.assertEqual(got["moments"],1)
        self.assertEqual(util.read_json(run/"run.json")["continues"],1)

    def test_broken_report_json_asks_for_fix_instead_of_crashing(self):
        p=self.work/"output/report.json";p.write_text('{"status":"draft",')
        self.assertIn("结果文件损坏",pi_runner.unfinished(p))
        # 中文里夹英文双引号：自动修复，原文件另存 .broken
        p.write_text('{"status":"complete","state_brief":"现状","moments":[{"context_zh":"确认"spend 录入正常"，复核"没问题"。"}]}',encoding="utf-8")
        self.assertEqual(pi_runner.unfinished(p),"")
        self.assertEqual(util.read_json(p)["moments"][0]["context_zh"],'确认"spend 录入正常"，复核"没问题"。')
        self.assertTrue(p.with_suffix(".json.broken").exists())

    def test_quota_exhaustion_waits_then_continues_same_session(self):
        run=self.work.parent
        (run/"session").mkdir();(run/"session/s.jsonl").write_text("{}\n")
        (self.work/"output/report.json").unlink()
        blocked=json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"error",
            "errorMessage":"429: Token Plan usage limit reached"}})+"\n"
        calls=[]
        def fake(cmd,**kw):
            calls.append(cmd)
            if len(calls)==2:util.write_json(self.work/"output/report.json",self.report)
            p=unittest.mock.MagicMock();p.stdout=io.StringIO(blocked if len(calls)==1 else "");p.wait.return_value=0;p.poll.return_value=0;return p
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{})),\
             patch.object(pi_runner,"reviews_on",return_value=False),patch.object(pi_runner,"wait_for_quota",return_value=5.0) as wait,\
             patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            got=pi_runner.execute("5555",log=lambda *_:None)
        self.assertEqual(wait.call_count,1)
        self.assertIn("额度用完中断",calls[1][-1])
        saved=util.read_json(run/"run.json")
        self.assertEqual((saved["quota_waits"],saved.get("continues",0)),(1,0))
        self.assertEqual(got["moments"],1)

    def test_reviewer_rejects_then_author_fixes_then_passes(self):
        run=self.work.parent
        (run/"session").mkdir();(run/"session/s.jsonl").write_text("{}\n")
        util.write_json(run/"config/models.json",{"providers":{"feedmob":{"models":[{"id":"m","samplingParams":{"reasoning_effort":"low"}}]}}})
        calls=[]
        verdicts=[{"decision":"reject","summary":"邮件没查","suggestions":[{"target":"coverage","problem":"没查邮件","suggestion":"查邮件线程"}]},
                  {"decision":"pass","summary":"可以展示","suggestions":[]}]
        def fake(cmd,**kw):
            calls.append((cmd,kw.get("env") or {}))
            if "review_verdict" in cmd:  # 审核 Agent：只开放这一个工具，结论写到指定文件
                self.assertEqual(cmd[cmd.index("--tools")+1],"review_verdict")
                self.assertNotIn("builtin:mcp",cmd)
                self.assertNotIn("PI_FEMINI_TOKEN",kw["env"])
                util.write_json(pathlib.Path(kw["env"]["REPLAY_REVIEW_OUT"]),verdicts.pop(0))
            p=unittest.mock.MagicMock();p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0;return p
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{"PI_FEMINI_TOKEN":"x"})),\
             patch.object(pi_runner,"reviews_on",return_value=True),patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            got=pi_runner.execute("5555",log=lambda *_:None)
        kinds=["review" if "review_verdict" in c else "author" for c,_ in calls]
        self.assertEqual(kinds,["author","review","author","review"])
        self.assertIn("审核打回（第 1 轮）",calls[2][0][-1])
        self.assertIn("查邮件线程",calls[2][0][-1])
        self.assertEqual(calls[2][0][1:3],["--session",str(run/"session/s.jsonl")])
        packet=(run/"review-1/system.md").read_text()
        self.assertIn("你是业务回放的审核员",packet);self.assertIn("Acme 上线回放",packet);self.assertNotIn("程序核对原话的结果",packet)
        saved=util.read_json(run/"run.json")
        self.assertEqual([r["decision"] for r in saved["reviews"]],["reject","pass"])
        self.assertEqual(set(saved["phase_seconds"]),{"author","review"})
        self.assertNotIn("review_failed",saved)
        self.assertEqual(got["moments"],1)

    def test_complete_with_remaining_coverage_is_unfinished(self):
        p=self.work/"output/report.json"
        rep=dict(self.report,status="complete",coverage={"searched":["slack"],"remaining":["邮件余下34页未查"]})
        util.write_json(p,rep)
        self.assertIn("coverage.remaining",pi_runner.unfinished(p))
        rep["coverage"]={"searched":["slack"],"not_available":["Fireflies 非管理员"],"remaining":[]}
        util.write_json(p,rep)
        self.assertEqual(pi_runner.unfinished(p),"")

    def test_refresh_prompt_has_no_goal_and_full_has_no_update_section(self):
        with tempfile.TemporaryDirectory() as td:
            run=pathlib.Path(td)
            full=pi_runner.system_prompt(run,False).read_text()
            self.assertIn("## Goal",full);self.assertNotIn("## 增量更新",full);self.assertIn("## 工作方法",full)
            upd=pi_runner.system_prompt(run,True).read_text()
            self.assertNotIn("Goal",upd);self.assertIn("## 增量更新",upd);self.assertIn("## 归属判断由你完成",upd)
            self.assertIn("原话不用重新核对",upd)

    def test_refresh_is_not_held_by_remaining_and_reminder_has_no_goal(self):
        p=self.work/"output/report.json"
        util.write_json(p,dict(self.report,status="complete",coverage={"remaining":["邮件余下3页"]}))
        self.assertEqual(pi_runner.unfinished(p,update=True),"")
        self.assertIn("coverage.remaining",pi_runner.unfinished(p))
        run=self.work.parent
        (run/"session").mkdir(exist_ok=True);(run/"session/s.jsonl").write_text("{}\n")
        util.write_json(self.work/"seed/brief.json",{**util.read_json(self.work/"seed/brief.json"),"mode":"update"})
        util.write_json(p,{**self.report,"status":"draft"})
        calls=[]
        def fake(cmd,**kw):
            calls.append(cmd)
            if len(calls)==2:util.write_json(p,{**self.report,"status":"complete","coverage":{"remaining":["还有一点"]}})
            m=unittest.mock.MagicMock();m.stdout=io.StringIO("");m.wait.return_value=0;m.poll.return_value=0;return m
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{})),\
             patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            pi_runner.execute("5555",log=lambda *_:None)
        self.assertEqual(len(calls),2)  # 只为「还标着草稿」提醒一次；remaining 不再催
        self.assertNotIn("Goal",calls[1][-1]);self.assertIn("结果还没写完",calls[1][-1])

    def test_author_gets_deadline_for_remaining_time(self):
        run=self.work.parent
        calls=[]
        def fake(cmd,**kw):
            calls.append((cmd,kw["env"]))
            m=unittest.mock.MagicMock();m.stdout=io.StringIO("");m.wait.return_value=0;m.poll.return_value=0;return m
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi","开工"],{"A":"1"})),\
             patch.object(pi_runner,"reviews_on",return_value=False),patch.object(pi_runner.subprocess,"Popen",side_effect=fake),\
             patch.dict(pi_runner.os.environ,{"REPLAY_PI_MAX_MINUTES":"90"}):
            before=pi_runner.time.time()
            pi_runner.execute("5555",log=lambda *_:None)
        cmd,env=calls[0]
        self.assertIn("本次运行还剩约 90 分钟",cmd[-1])
        self.assertAlmostEqual(int(env["REPLAY_PI_DEADLINE"]),before+90*60,delta=5)  # 扩展据此在每次工具结果末尾附剩余时间
        self.assertEqual(env["A"],"1")

    def test_update_mode_skips_review(self):
        run=self.work.parent
        (run/"session").mkdir(exist_ok=True);(run/"session/s.jsonl").write_text("{}\n")
        util.write_json(self.work/"seed/brief.json",{**util.read_json(self.work/"seed/brief.json"),"mode":"update"})
        calls=[]
        def fake(cmd,**kw):
            calls.append(cmd)
            p=unittest.mock.MagicMock();p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0;return p
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{})),\
             patch.object(pi_runner,"reviews_on",return_value=True),patch.object(pi_runner,"review") as rv,\
             patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            pi_runner.execute("5555",log=lambda *_:None)
        rv.assert_not_called()
        self.assertEqual(util.read_json(run/"run.json")["review_skipped"],"update")

    def test_review_failed_keeps_existing_v3_page(self):
        run=self.work.parent
        (run/"session").mkdir();(run/"session/s.jsonl").write_text("{}\n")
        util.write_json(run/"config/models.json",{"providers":{"feedmob":{"models":[{"id":"m"}]}}})
        out=util.out_dir("5555");out.mkdir(parents=True,exist_ok=True)
        util.write_json(out/"business.case.zh.json",{"meta":{"engine":"pi-online-v3"},"events":[1,2,3]})
        (out/"business.zh.html").write_text("old v3 page")
        reject={"decision":"reject","summary":"没查全","suggestions":[{"target":"coverage","problem":"邮件没查","suggestion":"查邮件"}]}
        def fake(cmd,**kw):
            if "review_verdict" in cmd:util.write_json(pathlib.Path(kw["env"]["REPLAY_REVIEW_OUT"]),reject)
            p=unittest.mock.MagicMock();p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0;return p
        with patch.object(pi_runner,"prepare",return_value=run),patch.object(pi_runner,"configure",return_value=(["pi"],{})),\
             patch.object(pi_runner,"reviews_on",return_value=True),patch.object(pi_runner.subprocess,"Popen",side_effect=fake):
            got=pi_runner.execute("5555",log=lambda *_:None)
        self.assertTrue(got["kept_previous"])
        self.assertEqual((out/"business.zh.html").read_text(),"old v3 page")
        self.assertEqual(util.read_json(run/"run.json")["status"],"kept_previous")

    def test_invalid_quote_or_scope_leaves_published_page(self):
        p=util.out_dir("5555")/"business.zh.html";p.parent.mkdir(parents=True);p.write_text("old")
        self.report["moments"][0]["evidence"]={"quote":""}  # 没有原话：严格模式拒绝，旧页面保留
        util.write_json(self.work/"output/report.json",self.report)
        with self.assertRaises(ValueError):pi_result.publish("5555",self.work)
        self.assertEqual(p.read_text(),"old")
        self.report["moments"][0]["evidence"]={"quote":"words not in any result"}  # 原话不核对（用户定的）：照样发布
        util.write_json(self.work/"output/report.json",self.report)
        self.assertEqual(pi_result.publish("5555",self.work)["moments"],1)
        with scope.use("5555","9"):
            with self.assertRaises(ValueError):pi_result.adapt("5555",self.work)

    def test_work_in_progress_report_never_replaces_a_page(self):
        p=util.out_dir("5555")/"business.zh.html";p.parent.mkdir(parents=True);p.write_text("previous page")
        self.report["status"]="draft";util.write_json(self.work/"output/report.json",self.report)
        with self.assertRaisesRegex(ValueError,"草稿"):
            pi_result.publish("5555",self.work)
        self.assertEqual(p.read_text(),"previous page")

    def test_first_pass_displays_draft_without_claiming_content_review(self):
        self.report["status"]="draft"
        self.report["moments"][0]["evidence"]={**self.spec,"pointer":"/wrong"}
        util.write_json(self.work/"output/report.json",self.report)
        got=pi_result.publish("5555",self.work,test_mode=True)
        self.assertTrue(got["first_pass_test"])
        self.assertEqual((got["reference_notes"],got["reference_fixed"]),(0,0))  # 位置不管，不核对
        self.report["moments"][0]["evidence"]={**self.spec,"quote":"this sentence was never said"}
        util.write_json(self.work/"output/report.json",self.report)
        got=pi_result.publish("5555",self.work,test_mode=True)
        self.assertEqual((got["reference_notes"],got["reference_fixed"]),(0,0))  # 原话不核对
        case=util.read_json(util.out_dir("5555")/"business.case.zh.json")
        self.assertTrue(case["meta"]["first_pass_test"])
        self.assertNotIn("首轮测试",json.dumps(case,ensure_ascii=False))  # 只记后台，页面上不出现测试/未审核字样
        self.assertEqual(case["events"][0]["quote"],"this sentence was never said")  # 首轮照样展示，只记为待核对

    def test_first_pass_skips_bad_items_instead_of_failing_page(self):
        self.report["open_items"]=[{"topic":"确认花费","topic_en":"Confirm spend","scope":"this_campaign","status":"waiting_on_others",
            "state_note":"等回复","state_note_en":"Waiting","next_step":"跟进","next_step_en":"Follow up","next_owner":"Ann","owner_basis":"guess",
            "evidence":[{**self.spec,"ts":"2026-10-01"}]}]  # 证据缺发言人、负责人依据不合法
        self.report["chapters"]=[]
        util.write_json(self.work/"output/report.json",self.report)
        with self.assertRaises(ValueError):pi_result.adapt("5555",self.work)  # 严格模式仍然拒绝
        got=pi_result.publish("5555",self.work,test_mode=True)
        self.assertEqual((got["moments"],got["open_items"]),(1,0))
        state=util.read_json(util.camp_dir("5555")/"business.json")
        self.assertTrue(any("跳过" in w for w in state["reference_notes"]))

    def test_update_starts_from_previous_published_result(self):
        runs=util.camp_dir("5555")/"pi-runs"
        old=runs/"20261101T000000Z-aaaaaa";(old).mkdir(parents=True)
        import shutil
        shutil.copytree(self.work,old/"work")
        (old/"work/notes").mkdir();(old/"work/notes/evidence.md").write_text("E1 launch request")
        util.write_json(old/"work/output/report.json",{**self.report,"coverage":{"searched":["slack"],"remaining":["邮件列表22页只读了第1页"]}})
        util.write_json(util.camp_dir("5555")/"pi-published.json",{"run":old.name})
        self.assertEqual(pi_runner.previous_run("5555"),old)
        new=runs/"20261102T000000Z-bbbbbb"
        for name in ("seed","results","notes","output"):(new/"work"/name).mkdir(parents=True)
        brief={"as_of":"2026-11-02T00:00:00Z","mode":"full"}
        self.assertTrue(pi_runner.seed_update(new,old,brief))
        self.assertEqual((brief["mode"],brief["previous_run"],brief["previous_as_of"]),("update",old.name,"2026-11-02"))
        carried=util.read_json(new/"work/output/report.json")
        self.assertEqual(carried["status"],"draft")  # 上次的结果作为这次的起点，没做完不会被当成完成版
        self.assertEqual([{k:v for k,v in m.items() if k!="id"} for m in carried["moments"]],self.report["moments"])
        self.assertEqual([m["id"] for m in carried["moments"]],["M001"])  # 上次的条目补上编号，这次按编号增改
        # 上一轮的旧缺口挪到 previous_remaining，不挡这次增量更新的完成
        self.assertEqual(carried["coverage"]["remaining"],[])
        self.assertEqual(carried["coverage"]["previous_remaining"],["邮件列表22页只读了第1页"])
        util.write_json(new/"work/output/report.json",{**carried,"status":"complete"})
        self.assertEqual(pi_runner.unfinished(new/"work/output/report.json"),"")
        self.assertTrue((new/"work/results/request.json").is_file())  # 旧节点的引用仍能核对
        self.assertEqual((new/"work/seed/previous_notes/evidence.md").read_text(),"E1 launch request")
        self.assertEqual(pi_result.evidence(new/"work",carried["moments"][0]["evidence"]),self.spec["quote"])

    def test_quotes_are_used_as_written_without_checking(self):
        self.assertEqual(pi_result.evidence(self.work,{"quote":"  anything the AI wrote  "}),"anything the AI wrote")
        with self.assertRaises(ValueError):pi_result.evidence(self.work,{"quote":" "})

    def test_new_progress_is_used_without_overwriting_old_before_render(self):
        item={"topic":"等待上线","topic_en":"Await launch","state_note":"尚无执行确认","state_note_en":"No confirmation yet",
              "next_step":"确认执行","next_step_en":"Confirm delivery","next_owner":"Ann Example","owner_basis":"none",
              "status":"waiting_on_others","scope":"this_campaign","evidence":[{**self.spec,"ts":"2026-10-01","speaker":"Ann Example"}]}
        self.report["open_items"]=[item];util.write_json(self.work/"output/report.json",self.report)
        cases,_,prog=pi_result.adapt("5555",self.work)
        self.assertEqual(prog["items"][0]["next_owner"],"待确认")
        self.assertEqual(len(cases["zh"]["presentation"]["progress"]["items"]),1)
        self.assertFalse((util.camp_dir("5555")/"progress.json").exists())


class Config(unittest.TestCase):
    def test_native_mcp_only_no_secret_in_config_no_brain_no_end_gate(self):
        with tempfile.TemporaryDirectory() as td:
            run=pathlib.Path(td);(run/"work").mkdir()
            with patch.object(pi_runner.progress,"load_ai",return_value={"AI_BASE_URL":"https://model.example/v1","AI_API_KEY":"secret-for-test"}),patch.object(pi_runner.feeds,"_femini_token",return_value="mcp-secret-test"),patch.object(pi_runner.feeds,"setting",side_effect=lambda n,d="":d):
                cmd,env=pi_runner.configure(run)
            combined="\n".join(p.read_text() for p in (run/"config").glob("*.json"))
            self.assertNotIn("secret-for-test",combined);self.assertNotIn("mcp-secret-test",combined)
            self.assertNotIn("brain",combined);self.assertNotIn("mirror",combined)
            self.assertIn("builtin:mcp",cmd);self.assertNotIn("submit_replay",str(cmd))
            mcp=json.loads((run/"config/mcp.json").read_text())
            self.assertEqual(mcp["mcpServers"]["femini"]["exposure"],"hidden")
            self.assertNotIn("create_note",mcp["mcpServers"]["femini"]["toolExposure"])
            self.assertEqual(env["PI_FLASH_KEY"],"secret-for-test")
            provider=json.loads((run/"config/models.json").read_text())["providers"]["feedmob"]
            self.assertTrue(provider["baseUrl"].endswith("/v1"))
            self.assertEqual(provider["models"][0]["contextWindow"],1000000)

    def test_subscription_model_uses_shared_login_and_thinking_flag(self):
        with tempfile.TemporaryDirectory() as td:
            run=pathlib.Path(td)/"run";(run/"work").mkdir(parents=True)
            agent=pathlib.Path(td)/"pi-agent";agent.mkdir();(agent/"auth.json").write_text("{}")
            settings={"REPLAY_PI_MODEL":"openai-codex/gpt-6-luna","REPLAY_PI_AGENT_DIR":str(agent),"REPLAY_PI_THINKING":"medium"}
            with patch.dict(os.environ,settings),patch.object(pi_runner.progress,"load_ai",return_value={"AI_BASE_URL":"https://m.example/v1","AI_API_KEY":"k"}),\
                 patch.object(pi_runner.feeds,"_femini_token",return_value="t"),patch.object(pi_runner.feeds,"setting",side_effect=lambda n,d="":d):
                cmd,env=pi_runner.configure(run)
            i=cmd.index("--model")
            self.assertEqual(cmd[i:i+4],["--model","openai-codex/gpt-6-luna","--thinking","medium"])
            self.assertEqual(env["PI_CODING_AGENT_DIR"],str(agent.resolve()))  # 订阅登录凭据只有一份，所有运行共用
            self.assertEqual(json.loads((agent/"auth.json").read_text()),{})  # 不动登录凭据
            override=json.loads((agent/"models.json").read_text())["providers"]["openai-codex"]["modelOverrides"]["gpt-6-luna"]
            self.assertEqual(override["contextWindow"],1000000)
            with patch.dict(os.environ,{"REPLAY_PI_AGENT_DIR":str(pathlib.Path(td)/"missing")}):
                with self.assertRaisesRegex(RuntimeError,"还没有登录"):pi_runner.subscription_home()

    def test_explicit_resume_reuses_session_and_time_budget(self):
        with tempfile.TemporaryDirectory() as td,patch.object(util,"RAW",pathlib.Path(td)/"raw"):
            run=util.camp_dir("5555")/"pi-runs/known-run"
            util.write_json(run/"run.json",{"status":"failed","seconds":500,"turns":9,"started_at":"2026-11-02T15:00:00Z"})
            session=run/"session/original.jsonl";session.parent.mkdir();session.write_text('{"type":"session"}\n')
            util.write_json(run/"work/output/report.json",{})
            (run/"events.jsonl").write_text('{"type":"old-event"}\n')
            with patch.object(pi_runner,"prepare") as prepare,patch.object(pi_runner,"configure",return_value=(["pi"],{})),patch.object(pi_runner.subprocess,"Popen") as popen,patch.object(pi_result,"publish",return_value={"events":1}):
                p=popen.return_value;p.stdout=io.StringIO("");p.wait.return_value=0;p.poll.return_value=0
                pi_runner.execute("5555",resume_run=run,log=lambda *_:None)
                prepare.assert_not_called()
                args=popen.call_args.args[0]
                self.assertEqual(args[args.index("--session")+1],str(session.resolve()))
                self.assertAlmostEqual(p.wait.call_args.kwargs["timeout"],6700,delta=1)
            saved=util.read_json(run/"run.json")
            self.assertEqual(saved["resumptions"],1)
            self.assertEqual(saved["turns"],9)
            self.assertIn("old-event",(run/"events.jsonl").read_text())
            self.assertTrue((run/"attempt-1.json").exists())

    def test_pi_failure_is_not_reported_success_or_fallen_back(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td)
            with patch.object(util,"RAW",root/"raw"),patch.object(pipeline,"STATE",root/"state"),patch.object(pi_runner,"enabled",return_value=True),patch.object(pi_runner,"execute",side_effect=RuntimeError("failed online")),patch.object(pipeline.business,"build") as old,patch.object(pipeline.build,"build") as detail:
                util.write_json(util.camp_dir("5555")/"corpus.json",{"records":[]})
                got=pipeline._fill_locked("5555")
                self.assertFalse(got["ok"]);self.assertIn("failed online",got["error"])
                old.assert_not_called();detail.assert_not_called()


if __name__=="__main__":unittest.main()
