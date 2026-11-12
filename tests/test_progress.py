"""进行中事项（replay/progress.py）的测试：假的模型，不联网。"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import progress, util  # noqa: E402

CID = "5555"
TODAY = dt.date(2026, 10, 22)
TERMS = dict(name="Acme_CTV_LG", advertiser="Acme", context=["LG"], links=["777777"], issues=[])


def slack(ts, who, text, i=0):
    return dict(kind="slack", ts=ts, channel="#acme", who=who, text=text, why="channel", id=f"R{i:03d}")


def meeting(ts, title, text, todos=""):
    return dict(kind="meeting", ts=ts, title=title, text=text, todos=todos, partial=False, id="R900")


def item(topic, quote, ts="2026-10-20T10:00:00Z", speaker="Ann", **kw):
    base = dict(id=None, topic=topic, topic_en=topic + " en", scope="this_campaign", status="in_progress", state_note="n",
                state_note_en="n", next_step="s", next_step_en="s", next_owner="Ann", owner_basis="self_commitment",
                evidence=[dict(ts=ts, speaker=speaker, quote=quote)], confidence="high")
    base.update(kw)
    return base


class Scripted:
    """按顺序返回预设答案，并记录每次收到的消息。"""

    def __init__(self, *answers, scopes=None):
        self.answers = list(answers)
        self.calls = []
        self.scopes, self.rescoped = scopes or {}, []

    def __call__(self, messages):
        if messages[0]["content"].startswith("You classify open work items"):  # 整理完以后的归属判断，单独记
            self.rescoped.append(messages)
            ids = [r["id"] for r in json.loads(messages[1]["content"])]
            return json.dumps({"scopes": [dict(id=i, scope=self.scopes[i]) for i in ids if i in self.scopes]}), {}
        self.calls.append(messages)
        return json.dumps(self.answers.pop(0), ensure_ascii=False), {"prompt_tokens": 1, "completion_tokens": 1}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = [(util, "RAW", util.RAW)]
        util.RAW = pathlib.Path(self.tmp.name) / "raw"

    def tearDown(self):
        for mod, name, val in self.saved:
            setattr(mod, name, val)
        self.tmp.cleanup()

    def put_corpus(self, records):
        util.write_json(util.camp_dir(CID) / "corpus.json", dict(campaign=dict(id=5555, name="Acme_CTV_LG"), terms=TERMS, records=records))


class QuoteCheck(unittest.TestCase):
    def test_near_quote_is_shown_as_the_original_words(self):
        docs = progress.build_docs([dict(kind="slack", text="Header line\nHi @Liwei, hope you had a great weekend. I just noticed that we stopped getting the weekly spend report on Monday, can you check it with the data team please?")])
        got, _ = progress.checked(dict(ts="t", speaker="s", quote="Hi @Liwei, hope you had a good weekend. I just noticed that we stopped getting the weekly spend report on Monday, can you check it with the data team please?"), docs)
        self.assertEqual(got["check"], "near")
        self.assertEqual(got["quote"], "Hi @Liwei, hope you had a great weekend. I just noticed that we stopped getting the weekly spend report on Monday, can you check it with the data team please?")

    def test_exact_near_missing(self):
        docs = progress.build_docs([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday, okay?")])
        self.assertEqual(progress.check_quote("send the report on Friday", docs), "exact")
        self.assertEqual(progress.check_quote("I will send the REPORT on   friday", docs), "exact")
        self.assertEqual(progress.check_quote("I will send the report on Friday okay", docs), "near")  # 标点被理顺 = 近似
        self.assertEqual(progress.check_quote("I will mail the invoice next week", docs), "missing")

    def test_meeting_speaker_prefix_is_not_part_of_the_quote(self):
        docs = progress.build_docs([meeting("2026-10-20T10:00:00Z", "Sync", "**Ara** (15:31:23): this training is for the weekly task")])
        self.assertEqual(progress.check_quote("this training is for the weekly task", docs), "exact")


class Build(Sandbox):
    def test_unverifiable_quotes_and_items_are_dropped_and_owner_is_tightened(self):
        self.put_corpus([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday")])
        ask = Scripted({"items": [
            item("Send report", "I will send the report on Friday"),
            item("Invented", "we agreed to double the budget"),                       # 原话查无 → 整个事项丢弃
            item("No basis", "I will send the report on Friday", next_owner="Bob", owner_basis="none"),
        ], "closed": []})
        res = progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        doc = util.read_json(util.camp_dir(CID) / "progress.json")
        topics = {x["topic"]: x for x in doc["items"]}
        self.assertEqual(set(topics), {"Send report", "No basis"})
        self.assertEqual(topics["No basis"]["next_owner"], "待确认")   # 没依据不猜负责人
        self.assertEqual(topics["Send report"]["next_owner"], "Ann")
        self.assertEqual(sorted(x["id"] for x in doc["items"]), ["P001", "P002"])
        self.assertEqual(res["calls"], 1)

    def test_item_that_never_names_the_advertiser_is_dropped_unless_from_a_dedicated_source(self):
        shared = dict(slack("2026-10-20T10:00:00Z", "Ann", "Samsung needs the new report format"), why="match")
        named = dict(slack("2026-10-20T11:00:00Z", "Bob", "Acme wants the daily report sent"), why="match")
        dedicated = slack("2026-10-20T12:00:00Z", "Cat", "please upload the creatives today")  # why=channel：专用频道
        self.put_corpus([shared, named, dedicated])
        ask = Scripted({"items": [
            item("Samsung report", "Samsung needs the new report format"),
            item("Acme daily report", "Acme wants the daily report sent"),
            item("Creatives", "please upload the creatives today"),
        ], "closed": []})
        progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        topics = {x["topic"] for x in util.read_json(util.camp_dir(CID) / "progress.json")["items"]}
        self.assertEqual(topics, {"Acme daily report", "Creatives"})

    def test_stale_is_computed_by_program_from_latest_verified_quote(self):
        self.put_corpus([slack("2026-09-01T10:00:00Z", "Ann", "please check the pixel setup"),
                         slack("2026-10-20T10:00:00Z", "Bob", "tracking is live")])
        ask = Scripted({"items": [item("Pixel", "please check the pixel setup", ts="2026-09-01T10:00:00Z"),
                                  item("Tracking", "tracking is live", ts="2026-10-20T10:00:00Z")], "closed": []})
        progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        by = {x["topic"]: x for x in util.read_json(util.camp_dir(CID) / "progress.json")["items"]}
        self.assertTrue(by["Pixel"]["stale"])
        self.assertEqual(by["Pixel"]["age_days"], 51)
        self.assertFalse(by["Tracking"]["stale"])

    def test_incremental_run_only_sends_new_records_and_keeps_ids(self):
        a = slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday", 1)
        self.put_corpus([a])
        ask = Scripted({"items": [item("Send report", "I will send the report on Friday")], "closed": []})
        progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        # 第二天：多了一条新消息；原料里的记录编号会变，但指纹不变
        b = slack("2026-10-21T09:00:00Z", "Ann", "report sent, thanks", 2)
        self.put_corpus([dict(a, id="R010"), dict(b, id="R011")])
        existing = util.read_json(util.camp_dir(CID) / "progress.json")["items"]
        ask2 = Scripted({"items": [], "closed": [dict(id=existing[0]["id"], reason="已发送", reason_en="sent",
                                                      evidence=[dict(ts="2026-10-21T09:00:00Z", speaker="Ann", quote="report sent, thanks")])]})
        res = progress.build(CID, log=lambda *_: None, ask=ask2, today=TODAY)
        self.assertEqual(res["new_records"], 1)
        sent = ask2.calls[0][1]["content"]
        self.assertIn("report sent, thanks", sent)
        self.assertNotIn("I will send the report on Friday\n", sent.split("New records")[1])  # 旧记录不再重复喂
        self.assertIn(existing[0]["id"], sent)                                              # 但现有清单会带上
        doc = util.read_json(util.camp_dir(CID) / "progress.json")
        self.assertEqual(doc["items"], [])
        self.assertEqual(doc["closed"][0]["id"], existing[0]["id"])
        self.assertEqual(doc["closed"][0]["topic"], "Send report")  # 关闭时保留标题，页面才能展示

    def test_as_of_is_the_newest_date_even_when_older_records_arrive_later(self):
        self.put_corpus([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday", 1)])
        progress.build(CID, log=lambda *_: None, ask=Scripted({"items": [item("Send report", "I will send the report on Friday")], "closed": []}), today=TODAY)
        # 之后补进来一条更早的记录（比如镜像回填了旧日期）
        old = slack("2026-07-09T20:00:00Z", "Bob", "kickoff notes shared", 2)
        self.put_corpus([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday", 1), old])
        existing = util.read_json(util.camp_dir(CID) / "progress.json")["items"]
        progress.build(CID, log=lambda *_: None, ask=Scripted({"items": [dict(existing[0])], "closed": []}), today=TODAY)
        self.assertTrue(util.read_json(util.camp_dir(CID) / "progress.json")["as_of"].startswith("2026-10-20"))

    def test_no_new_records_means_no_model_call(self):
        self.put_corpus([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday")])
        progress.build(CID, log=lambda *_: None, ask=Scripted({"items": [item("Send report", "I will send the report on Friday")], "closed": []}),
                       today=TODAY)
        ask = Scripted()
        res = progress.build(CID, log=lambda *_: None, ask=ask, today=dt.date(2026, 11, 20))
        self.assertEqual((res["calls"], ask.calls, ask.rescoped), (0, [], []))
        self.assertTrue(util.read_json(util.camp_dir(CID) / "progress.json")["items"][0]["stale"])  # 但久未更新照常重算

    def test_scope_is_judged_again_after_the_list_is_updated(self):
        self.put_corpus([slack("2026-10-20T10:00:00Z", "Ann", "I will send the report on Friday")])
        ask = Scripted({"items": [dict(item("Send report", "I will send the report on Friday"), scope="client_general")], "closed": []},
                       scopes={"P001": "same_client"})
        progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertEqual(len(ask.rescoped), 1)
        self.assertEqual(util.read_json(util.camp_dir(CID) / "progress.json")["items"][0]["scope"], "same_client")

    def test_many_chunks_are_digested_at_the_same_time_then_combined_once(self):
        import re
        import threading
        import time
        recs = [slack("2026-10-01T10:00:00Z", "Ann", "Bob, can you create the LG tracker for Acme?", 1),
                slack("2026-10-02T10:00:00Z", "Bob", "Sure, I will create the Acme LG tracker tomorrow", 2),
                slack("2026-10-03T10:00:00Z", "Cat", "lunch?", 3),
                slack("2026-10-04T10:00:00Z", "Bob", "Acme LG tracker is created and shared", 4),
                slack("2026-10-05T10:00:00Z", "Ann", "Acme wants the weekly report every Monday, can you own it Dan?", 5)]
        self.put_corpus(recs)
        saved = progress.chunks
        progress.chunks = lambda rs, limit=None: [[r] for r in rs]                  # 每条一块：5 块
        self.addCleanup(setattr, progress, "chunks", saved)
        live, peak, lock, combine = [0], [0], threading.Lock(), []
        events_of = {"can you create the LG tracker": ("request", "Ann asks Bob to create the Acme LG tracker", "Bob"),
                     "I will create the Acme LG tracker": ("commitment", "Bob will create the Acme LG tracker", "Bob"),
                     "tracker is created and shared": ("done", "Acme LG tracker created", ""),
                     "can you own it Dan": ("request", "Ann asks Dan to own the Acme weekly report", "Dan")}

        def ask(messages):
            system, user = messages[0]["content"], messages[1]["content"]
            if system.startswith("You read raw working communication"):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.05)
                with lock:
                    live[0] -= 1
                rid = re.search(r"\[(R\d+)\]", user).group(1)
                evs = [dict(record=rid, ts="", speaker="", kind=k, what=w, next=n, quote=q) for q, (k, w, n) in events_of.items() if q in user]
                if "lunch" in user:
                    evs.append(dict(record=rid, ts="", speaker="Cat", kind="request", what="x", next="", quote="please approve the budget now"))  # 编的
                return json.dumps({"events": evs}), {}
            if system.startswith("You classify open work items"):
                return json.dumps({"scopes": []}), {}
            combine.append(user)
            return json.dumps({"items": [item("周报", "Acme wants the weekly report every Monday, can you own it Dan?", ts="2026-10-05T10:00:00Z",
                                              next_owner="Dan", owner_basis="direct_request")], "closed": []}), {}
        res = progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)
        self.assertGreater(peak[0], 1)                                     # 各块同时摘要
        self.assertEqual(len(combine), 1)                                  # 只合成一次
        self.assertIn("Bob (done): tracker is created and shared", combine[0])     # 完成的事带着原话传下去
        self.assertIn("(done)", combine[0])
        self.assertNotIn("please approve the budget", combine[0])          # 对不上原话的事不往下传
        self.assertLess(combine[0].index("can you create the LG tracker"), combine[0].index("tracker is created"))  # 按时间排
        self.assertEqual(res["calls"], 6)                                  # 5 块摘要 + 1 次合成
        doc = util.read_json(util.camp_dir(CID) / "progress.json")
        self.assertEqual([x["next_owner"] for x in doc["items"]], ["Dan"])
        self.assertEqual(len(doc["seen"]), 5)                               # 全部算读过
        self.assertEqual(progress.build(CID, log=lambda *_: None, ask=ask, today=TODAY)["calls"], 0)

    def test_big_corpus_is_split_into_ordered_chunks(self):
        recs = [slack(f"2026-10-{d:02d}T10:00:00Z", "Ann", "x" * 100, d) for d in range(1, 11)]
        blocks = progress.chunks(recs, limit=350)
        self.assertGreater(len(blocks), 1)
        flat = [r for b in blocks for r in b]
        self.assertEqual(flat, recs)

    def test_configured_follows_the_env_file(self):
        saved = progress.SECRETS
        progress.SECRETS = pathlib.Path(self.tmp.name) / "none"
        try:
            self.assertFalse(progress.configured())
            progress.SECRETS.mkdir()
            (progress.SECRETS / "ai.env").write_text("AI_BASE_URL=x\nAI_MODEL=y\nAI_API_KEY=z\n")
            self.assertTrue(progress.configured())
            self.assertEqual(progress.load_ai()["AI_MODEL"], "y")
        finally:
            progress.SECRETS = saved

    def test_chat_retries_transient_gateway_errors_with_backoff_then_gives_up(self):
        import io
        import urllib.error
        from unittest.mock import patch
        saved = progress.SECRETS
        progress.SECRETS = pathlib.Path(self.tmp.name)
        (progress.SECRETS / "ai.env").write_text("AI_BASE_URL=http://x\nAI_MODEL=m\nAI_API_KEY=k\n")
        ok = io.BytesIO(json.dumps({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1}}).encode())
        ok.__enter__ = lambda self: self
        ok.__exit__ = lambda self, *a: None
        calls, sleeps = [], []

        def flaky(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None)
            return ok
        try:
            with patch.object(progress.urllib.request, "urlopen", flaky), patch.object(progress.time, "sleep", sleeps.append):
                self.assertEqual(progress.chat([{"role": "user", "content": "x"}])[0], "hi")
            self.assertEqual((len(calls), sleeps), (3, [0, 10, 30]))      # 前两次 502，第三次成功，间隔逐步拉长
            calls.clear()
            with patch.object(progress.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))),                     patch.object(progress.time, "sleep", lambda s: None):
                with self.assertRaises(RuntimeError) as cm:
                    progress.chat([{"role": "user", "content": "x"}])
            self.assertIn("5 次", str(cm.exception))
            self.assertNotIn("k", str(cm.exception).replace("URLError", ""))  # 错误信息里不带密钥
        finally:
            progress.SECRETS = saved

    def test_reasoning_effort_comes_from_config_and_env_overrides_it(self):
        import io
        import os
        from unittest.mock import patch
        saved = progress.SECRETS
        progress.SECRETS = pathlib.Path(self.tmp.name)
        sent = []

        def fake(req, timeout=0):
            sent.append(json.loads(req.data))
            ok = io.BytesIO(json.dumps({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}}).encode())
            ok.__enter__ = lambda self: self
            ok.__exit__ = lambda self, *a: None
            return ok
        try:
            with patch.object(progress.urllib.request, "urlopen", fake), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AI_REASONING_EFFORT", None)
                (progress.SECRETS / "ai.env").write_text("AI_BASE_URL=http://x\nAI_MODEL=m\nAI_API_KEY=k\n")
                progress.chat([{"role": "user", "content": "x"}])
                self.assertNotIn("reasoning_effort", sent[-1])                      # 不配：用模型默认
                self.assertEqual(sent[-1]["temperature"], 1.0)                       # MiniMax 官方建议值
                (progress.SECRETS / "ai.env").write_text("AI_BASE_URL=http://x\nAI_MODEL=m\nAI_API_KEY=k\nAI_REASONING_EFFORT=medium\n")
                progress.chat([{"role": "user", "content": "x"}])
                self.assertEqual(sent[-1]["reasoning_effort"], "medium")             # 服务器配置
                os.environ["AI_REASONING_EFFORT"] = "high"
                progress.chat([{"role": "user", "content": "x"}])
                self.assertEqual(sent[-1]["reasoning_effort"], "high")               # 环境变量优先（对比测试用）
                os.environ["AI_REASONING_EFFORT"] = "none"
                with self.assertRaises(RuntimeError):
                    progress.chat([{"role": "user", "content": "x"}])                # 写错了直接报清楚，不去网关空转
                os.environ.pop("AI_REASONING_EFFORT")
                self.assertNotIn("thinking", sent[-1])                              # 不配：开着思考（模型默认）
                os.environ["AI_THINKING"] = "disabled"
                progress.chat([{"role": "user", "content": "x"}])
                self.assertEqual(sent[-1]["thinking"], {"type": "disabled"})        # 批量铺开时关思考
                os.environ["AI_THINKING"] = "off"
                with self.assertRaises(RuntimeError):
                    progress.chat([{"role": "user", "content": "x"}])
                os.environ.pop("AI_THINKING")
        finally:
            progress.SECRETS = saved

    def test_missing_ai_config_fails_clearly(self):
        saved = progress.SECRETS
        progress.SECRETS = pathlib.Path(self.tmp.name) / "none"
        try:
            with self.assertRaises(RuntimeError):
                progress.load_ai()
        finally:
            progress.SECRETS = saved


class Prompt(unittest.TestCase):
    def test_meeting_block_marks_excerpts_and_todos_as_reference(self):
        text = progress.render(meeting("2026-10-20T10:00:00Z", "Weekly", "**A** (1:00:00): hi", todos="**Bob**  \nSend report (1:00)"))
        self.assertIn("MEETING | 2026-10-20 | Weekly", text)
        self.assertIn("reference only", text)



class BrokenJson(unittest.TestCase):
    def test_broken_json_goes_back_to_the_model_with_the_error_to_fix(self):
        broken = '{"events":[{"record":"R1"},record":"R2"}]}'          # 2026-10-30 实际碰到的：漏掉一个 {"
        asked = []
        answers = [broken, '{"events": [{"record": "R1"}, {"record": "R2"}]}']

        def ask(messages):
            asked.append(messages)
            return answers.pop(0), {}
        first = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.assertEqual(progress.ask_json(first, ask)[0], {"events": [{"record": "R1"}, {"record": "R2"}]})
        self.assertEqual(asked[0], first)
        self.assertEqual(asked[1][:2], first)                              # 不从头重问：原来的问题 +
        self.assertEqual(asked[1][2], {"role": "assistant", "content": broken})   # 它自己写坏的原文 +
        self.assertIn("not valid JSON", asked[1][3]["content"])              # 报错和出错的位置，让它自己改
        self.assertIn('},record":"R2"', asked[1][3]["content"])

    def test_gives_up_after_the_limit(self):
        asked = []
        ask = lambda messages: (asked.append(messages), ("not json", {}))[1]
        with self.assertRaises(RuntimeError):
            progress.ask_json([{"role": "user", "content": "u"}], ask)
        self.assertEqual(len(asked), progress.JSON_ATTEMPTS)
        self.assertEqual(len(asked[-1]), 3)                                  # 每轮只带上一次的原文，不越堆越长


if __name__ == "__main__":
    unittest.main()
