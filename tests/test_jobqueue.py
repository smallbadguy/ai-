"""外包任务队列：领题、超时、迟到的答案、按大小派题、兜底、作废等状态流转。"""
import datetime as dt
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import jobqueue as Q  # noqa: E402


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 31, 0, 0, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t

    def tick(self, minutes=0, seconds=0):
        self.t += dt.timedelta(minutes=minutes, seconds=seconds)


def msgs(text="records", key="picks"):
    return [{"role": "system", "content": f'Do the work.\nOutput one JSON object and nothing else:\n{{"{key}": [{{"record": str}}]}}'},
            {"role": "user", "content": text}]


GOOD = '{"picks": [{"record": "R1"}]}'


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        # 这些测试里没有流水线在等答案：先关掉「没人等就作废」，专门的测试里再打开
        for p in (patch.object(Q, "DB_PATH", pathlib.Path(tmp.name) / "queue.db"), patch.object(Q, "_now", self.clock),
                  patch.object(Q, "ORPHAN_SECONDS", 10 ** 7)):
            p.start()
            self.addCleanup(p.stop)

    def job(self, text="records", **kw):
        return Q.enqueue(msgs(text), campaign="5023", step="业务版", **kw)

    def state(self, jid):
        return Q.job_detail(jid)["job"]["state"]

    # ── 基本流程 ──
    def test_claim_input_submit_and_the_waiting_pipeline_gets_the_answer(self):
        Q.add_agent("a1", 200_000)
        jid = self.job(data_fetched_at="2026-10-30T04:30:00Z", data_as_of="2026-10-30T02:00:00Z")
        got = Q.claim("a1")["job"]
        self.assertEqual(got["id"], jid)
        self.assertEqual(got["output"]["top_key"], "picks")
        self.clock.tick(minutes=1)
        inp = Q.get_input(jid, got["lease_id"], "a1")
        self.assertEqual(inp["messages"], msgs())
        self.assertEqual(inp["data_fetched_at"], "2026-10-30T04:30:00Z")   # 原料什么时候取的
        self.assertEqual(inp["data_as_of"], "2026-10-30T02:00:00Z")        # 原料截至哪条记录
        self.assertEqual(inp["input_fetched_at"], "2026-10-31T00:01:00Z")  # 处理方什么时候取的题
        res = Q.submit(jid, got["lease_id"], "a1", GOOD, model="m-x")
        self.assertEqual(res["status"], 200)
        self.assertFalse(res["late"])
        self.assertEqual(Q.wait(jid, timeout=1, poll=0.01)[0], GOOD)
        d = Q.job_detail(jid)
        self.assertEqual(d["job"]["result_agent"], "a1")
        self.assertEqual(d["leases"][0]["input_fetched_at"], "2026-10-31T00:01:00Z")
        self.assertEqual([e["kind"] for e in d["events"]], ["created", "claimed", "input", "accepted"])

    def test_one_agent_holds_one_job_and_gets_the_same_one_again(self):
        Q.add_agent("a1", 200_000)
        Q.add_agent("a2", 200_000)
        j1, j2 = self.job("one"), self.job("two")
        first = Q.claim("a1")
        again = Q.claim("a1")
        self.assertEqual(first["job"]["id"], j1)
        self.assertEqual(again["job"]["id"], j1)
        self.assertEqual(again["job"]["lease_id"], first["job"]["lease_id"])
        self.assertTrue(again["again"])
        self.assertEqual(Q.claim("a2")["job"]["id"], j2)
        self.assertTrue(Q.claim("a2")["again"])

    def test_identical_questions_are_outsourced_once(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        self.assertEqual(self.job(), jid)  # 排着的同一道题
        lease = Q.claim("a1")["job"]["lease_id"]
        Q.submit(jid, lease, "a1", GOOD)
        self.assertEqual(self.job(), jid)  # 做完的同一道题：直接复用答案
        self.assertEqual(Q.wait(jid, timeout=1, poll=0.01)[0], GOOD)
        self.assertEqual(len(Q.snapshot()["jobs"]), 1)

    # ── 失联、重新排队、迟到的答案 ──
    def test_lost_agent_job_goes_back_to_queue_and_its_late_answer_still_counts(self):
        Q.add_agent("a1", 200_000)
        Q.add_agent("a2", 200_000)
        jid = self.job()
        l1 = Q.claim("a1")["job"]["lease_id"]
        self.clock.tick(seconds=Q.LEASE_SECONDS + 60)                       # a1 过了截止时间还没交
        l2 = Q.claim("a2")["job"]["lease_id"]             # 题目重新排队，a2 领到
        self.assertNotEqual(l1, l2)
        res = Q.submit(jid, l1, "a1", GOOD)               # a1 的答案这时才回来：题目还没完成，收下
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["late"])
        self.assertEqual(self.state(jid), "done")
        self.assertFalse(Q.heartbeat(jid, l2, "a2")["continue"])   # a2 被告知停下
        late = Q.submit(jid, l2, "a2", GOOD)
        self.assertEqual((late["status"], late["reason"]), (409, "already_done"))
        d = Q.job_detail(jid)
        self.assertEqual(d["job"]["result_agent"], "a1")
        kinds = [e["kind"] for e in d["events"]]
        for k in ("expired", "requeued", "late_accepted", "superseded", "late_ignored"):
            self.assertIn(k, kinds)

    def test_late_answer_after_someone_else_finished_is_not_used(self):
        Q.add_agent("a1", 200_000)
        Q.add_agent("a2", 200_000)
        jid = self.job()
        l1 = Q.claim("a1")["job"]["lease_id"]
        self.clock.tick(seconds=Q.LEASE_SECONDS + 60)
        l2 = Q.claim("a2")["job"]["lease_id"]
        self.assertEqual(Q.submit(jid, l2, "a2", '{"picks": [{"record": "R2"}]}')["status"], 200)
        self.assertEqual(Q.submit(jid, l1, "a1", GOOD)["status"], 409)
        self.assertEqual(Q.wait(jid, timeout=1, poll=0.01)[0], '{"picks": [{"record": "R2"}]}')
        a1 = next(a for a in Q.list_agents() if a["name"] == "a1")
        self.assertEqual((a1["timeout_count"], a1["late_count"]), (1, 1))

    def test_same_answer_submitted_twice_is_accepted_once(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        self.assertEqual(Q.submit(jid, lid, "a1", GOOD)["status"], 200)
        again = Q.submit(jid, lid, "a1", GOOD)
        self.assertEqual(again["status"], 200)
        self.assertTrue(again["duplicate"])

    def test_claim_gives_forty_minutes_and_heartbeat_extends_up_to_one_hour(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        got = Q.claim("a1")["job"]
        lid = got["lease_id"]
        self.assertEqual(got["expires_at"], "2026-10-31T00:40:00Z")      # 用户定的：领一次题给 40 分钟
        self.clock.tick(minutes=15)
        self.assertEqual(Q.heartbeat(jid, lid, "a1")["expires_at"], "2026-10-31T00:55:00Z")
        self.clock.tick(minutes=15)
        self.assertEqual(Q.heartbeat(jid, lid, "a1")["expires_at"], "2026-10-31T01:00:00Z")  # 封顶：领取后 60 分钟
        self.clock.tick(minutes=31)
        Q.snapshot()  # 过了上限：收回，重新排队
        self.assertEqual(self.state(jid), "queued")

    def test_job_is_taken_back_only_after_forty_minutes(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        Q.claim("a1")
        self.clock.tick(minutes=39)
        Q.snapshot()
        self.assertEqual(self.state(jid), "leased")       # 40 分钟之内不收回
        self.clock.tick(minutes=2)
        Q.snapshot()
        self.assertEqual(self.state(jid), "queued")

    def test_agent_that_comes_back_after_timeout_continues_if_nobody_took_the_job(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        self.clock.tick(seconds=Q.LEASE_SECONDS + 60)
        Q.snapshot()                                       # 收回：重新排队
        self.assertEqual(self.state(jid), "queued")
        res = Q.heartbeat(jid, lid, "a1")
        self.assertTrue(res["continue"])
        self.assertEqual(self.state(jid), "leased")        # 没人领：接着做
        self.assertEqual(Q.submit(jid, lid, "a1", GOOD)["status"], 200)

    # ── 不合格、放弃、失败、兜底 ──
    def test_invalid_answer_is_returned_with_the_reason_and_can_be_fixed(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        bad = Q.submit(jid, lid, "a1", 'here you go: {"picks": [1,}')
        self.assertEqual(bad["status"], 422)
        self.assertIn("JSON", bad["error"])
        missing = Q.submit(jid, lid, "a1", '{"items": []}')
        self.assertEqual(missing["status"], 422)
        self.assertIn("picks", missing["error"])
        self.assertEqual(missing["submits_left"], Q.MAX_SUBMITS - 2)
        self.assertEqual(Q.submit(jid, lid, "a1", "```json\n" + GOOD + "\n```")["status"], 200)

    def test_too_many_bad_answers_end_the_lease_and_requeue(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        for _ in range(Q.MAX_SUBMITS):
            res = Q.submit(jid, lid, "a1", "nope")
        self.assertEqual(res["submits_left"], 0)
        self.assertEqual(self.state(jid), "queued")
        self.assertEqual(Q.submit(jid, lid, "a1", GOOD)["status"], 429)

    def test_release_requeues_and_only_counts_as_failure_when_asked(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        Q.release(jid, lid, "a1", "读不下")
        self.assertEqual((self.state(jid), Q.job_detail(jid)["job"]["failures"]), ("queued", 1))
        lid = Q.claim("a1")["job"]["lease_id"]
        Q.release(jid, lid, "a1", "网关限流", count_failure=False)
        self.assertEqual(Q.job_detail(jid)["job"]["failures"], 1)

    def test_repeated_failures_hand_the_job_to_our_own_worker_then_fail(self):
        Q.add_agent("a1", 200_000)
        Q.ensure_internal("internal-1", 1_000_000, "flash")
        jid = self.job()
        for _ in range(Q.FAILS_TO_INTERNAL):
            lid = Q.claim("a1")["job"]["lease_id"]
            Q.release(jid, lid, "a1", "做不了")
        self.assertTrue(Q.job_detail(jid)["job"]["internal_only"])
        self.assertIsNone(Q.claim("a1")["job"])            # 外部处理方不再领它
        for _ in range(Q.FAILS_TO_FAILED - Q.FAILS_TO_INTERNAL):
            got = Q.claim("internal-1")["job"]
            self.assertEqual(got["id"], jid)
            Q.release(jid, got["lease_id"], "internal-1", "出错")
        self.assertEqual(self.state(jid), "failed")
        with self.assertRaises(RuntimeError):
            Q.wait(jid, timeout=1, poll=0.01)

    def test_valid_answer_for_a_failed_job_is_still_accepted(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        first = Q.claim("a1")["job"]["lease_id"]
        self.clock.tick(seconds=Q.LEASE_SECONDS + 60)
        with patch.object(Q, "FAILS_TO_FAILED", 1):          # 把门槛调到 1 次：这次超时就判失败
            Q.snapshot()
        self.assertEqual(self.state(jid), "failed")
        self.assertEqual(Q.submit(jid, first, "a1", GOOD)["status"], 200)  # 先到先得：迟到但合格，照样收
        self.assertEqual(self.state(jid), "done")

    def test_agent_is_paused_after_three_timeouts_in_a_row(self):
        Q.add_agent("a1", 200_000)
        for i in range(Q.PAUSE_AFTER_TIMEOUTS):
            self.job(f"q{i}")
            self.assertIsNotNone(Q.claim("a1")["job"])
            self.clock.tick(seconds=Q.LEASE_SECONDS + 60)
        res = Q.claim("a1")
        self.assertIsNone(res["job"])
        self.assertEqual(res["reason"], "paused")
        Q.update_agent("a1", unpause=True)
        self.assertIsNotNone(Q.claim("a1")["job"])

    # ── 按大小派题、优先级、自家处理方 ──
    def test_jobs_go_to_agents_that_can_read_them_and_big_jobs_go_first_to_big_agents(self):
        Q.add_agent("small", 200_000)
        Q.add_agent("big", 1_000_000)
        small_job = self.job("x" * 1000)
        big_job = self.job("y" * 450_000)    # 约 16 万 token：200k 的处理方读不下
        self.assertEqual(Q.claim("big")["job"]["id"], big_job)   # 大处理方先拿大题，哪怕它出得晚
        self.assertEqual(Q.claim("small")["job"]["id"], small_job)
        Q.add_agent("small2", 200_000)
        self.job("z" * 450_000)
        self.assertEqual(Q.claim("small2")["reason"], "nothing_fits")

    def test_our_own_worker_is_an_ordinary_agent_with_no_head_start_or_delay(self):
        Q.add_agent("a1", 200_000)
        Q.ensure_internal("internal-1", 1_000_000, "flash")
        jid = self.job()
        self.assertEqual(Q.claim("internal-1")["job"]["id"], jid)    # 新题自家处理方也马上能领，不用等
        j2 = self.job("other")
        self.assertEqual(Q.claim("a1")["job"]["id"], j2)

    def test_our_own_worker_gets_a_key_and_uses_the_same_interface(self):
        Q.ensure_internal("internal-1", 1_000_000, "flash")
        token = Q.rotate_token("internal-1")
        self.assertEqual(Q.agent_for_token(token), ("internal-1", True))
        Q.ensure_internal("internal-1", 1_000_000, "flash")          # 重启再登记：钥匙不变
        self.assertEqual(Q.agent_for_token(token), ("internal-1", True))

    def test_normal_jobs_go_before_batch_jobs(self):
        Q.add_agent("a1", 200_000)
        batch = Q.enqueue(msgs("batch"), campaign="5001", step="业务版", priority=1)
        self.clock.tick(minutes=5)
        normal = Q.enqueue(msgs("normal"), campaign="5002", step="业务版")
        self.assertEqual(Q.claim("a1")["job"]["id"], normal)          # 后出的普通题先派
        Q.add_agent("a2", 200_000)
        self.assertEqual(Q.claim("a2")["job"]["id"], batch)

    def test_batch_jobs_go_campaign_by_campaign_after_normal_jobs(self):
        for a in ("a1", "a2", "a3"):
            Q.add_agent(a, 200_000)
        later = Q.enqueue(msgs("campaign 2 round 1"), campaign="5002", step="业务版", priority=2)
        self.clock.tick(minutes=5)
        earlier = Q.enqueue(msgs("campaign 1 round 2"), campaign="5001", step="业务版", priority=1)  # 先开始的活动，题出得晚也先派
        self.clock.tick(minutes=1)
        normal = Q.enqueue(msgs("homepage refresh"), campaign="5003", step="业务版")                # 首页刷新、每天刷新最先
        self.assertEqual([Q.claim(a)["job"]["id"] for a in ("a1", "a2", "a3")], [normal, earlier, later])

    def test_asker_is_safe_when_a_step_asks_many_questions_at_once(self):
        """同一步同时出好几道题时，每道题都要拿到完整的原料时间。以前第一道题刚填好「取数时间」、还在算「截至哪条记录」
        （原料大时要一会儿）时，后面几道题会读到只填了一半的记录，报 KeyError: 'as_of'（2026-10-31 活动 5240）。"""
        import threading
        import time as _time
        seen, errors = [], []

        class SlowRecord(dict):  # 原料很大时，算「截至哪条记录」要一会儿
            def get(self, key, default=None):
                _time.sleep(0.05)
                return super().get(key, default)
        doc = {"built_at": "2026-10-31T00:00:00Z", "records": [SlowRecord(ts="2026-10-30T12:00:00Z") for _ in range(6)]}

        def fake_enqueue(messages, **kw):
            seen.append((kw["data_fetched_at"], kw["data_as_of"]))
            return "J00000000"

        def call():
            try:
                ask(msgs())
            except Exception as e:  # 以前这里是 KeyError: 'as_of'
                errors.append(e)
        with patch.object(Q, "read_json", lambda path: doc), patch.object(Q, "enqueue", fake_enqueue), \
                patch.object(Q, "wait", lambda jid, timeout=None: ("{}", {})):
            ask = Q.asker("5240", "业务版")
            first = threading.Thread(target=call)
            first.start()
            _time.sleep(0.1)                                   # 第一道题正在算时间时，其余几道也来了
            rest = [threading.Thread(target=call) for _ in range(7)]
            [x.start() for x in rest]
            first.join()
            [x.join() for x in rest]
        self.assertEqual(errors, [])
        self.assertEqual(seen, [("2026-10-31T00:00:00Z", "2026-10-30T12:00:00Z")] * 8)

    def test_queue_settings_come_from_the_environment(self):
        with patch.dict(os.environ, {"AI_QUEUE_PRIORITY": "7", "AI_QUEUE_WAIT_HOURS": "48"}):
            self.assertEqual(Q.queue_settings(), (7, 48 * 3600))
        with patch.dict(os.environ, {"AI_QUEUE_PRIORITY": "low", "AI_QUEUE_WAIT_HOURS": "oops"}):
            self.assertEqual(Q.queue_settings(), (1, None))
        with patch.dict(os.environ, {}):
            os.environ.pop("AI_QUEUE_PRIORITY", None)
            os.environ.pop("AI_QUEUE_WAIT_HOURS", None)
            self.assertEqual(Q.queue_settings(), (0, None))

    def test_same_job_asked_again_at_normal_priority_is_promoted(self):
        Q.add_agent("a1", 200_000)
        first = Q.enqueue(msgs("shared"), campaign="5001", step="业务版", priority=1)
        other = Q.enqueue(msgs("older normal"), campaign="5003", step="业务版")
        self.clock.tick(minutes=1)
        self.assertEqual(Q.enqueue(msgs("shared"), campaign="5001", step="业务版"), first)   # 同一道题，不重复出
        self.assertEqual(Q.job_detail(first)["job"]["priority"], 0)
        self.assertEqual(Q.claim("a1")["job"]["id"], first)            # 提到普通档后同档按出题先后：它出得早，先派
        Q.add_agent("a2", 200_000)
        self.assertEqual(Q.claim("a2")["job"]["id"], other)

    def test_job_nobody_can_read_fails_after_ten_minutes_instead_of_waiting_hours(self):
        Q.add_agent("small", 200_000)
        jid = self.job("y" * 450_000)    # 约 15 万 token：200k 的处理方读不下
        self.assertEqual(Q.claim("small")["reason"], "nothing_fits")
        self.clock.tick(minutes=11)
        Q.snapshot()
        self.assertEqual(self.state(jid), "failed")
        self.assertIn("读不下", Q.job_detail(jid)["job"]["error"])
        with self.assertRaises(RuntimeError):
            Q.wait(jid, timeout=1, poll=0.01)

    def test_big_job_waits_while_no_agent_is_registered_at_all(self):
        jid = self.job("y" * 450_000)
        self.clock.tick(minutes=30)
        Q.snapshot()
        self.assertEqual(self.state(jid), "queued")
        Q.add_agent("big", 1_000_000)
        self.assertEqual(Q.claim("big")["job"]["id"], jid)

    def test_retryable_release_does_not_count_as_a_failure(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        for _ in range(Q.FAILS_TO_INTERNAL + 1):
            lid = Q.claim("a1")["job"]["lease_id"]
            Q.release(jid, lid, "a1", "网关限流", count_failure=False)
        job = Q.job_detail(jid)["job"]
        self.assertEqual(job["failures"], 0)
        self.assertFalse(job["internal_only"])
        self.assertEqual(job["state"], "queued")

    # ── 作废、钥匙 ──
    def test_queued_job_nobody_waits_for_is_cancelled_and_late_answers_are_refused(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        with patch.object(Q, "ORPHAN_SECONDS", 15 * 60):
            self.clock.tick(seconds=Q.LEASE_SECONDS + 60)                     # 领了不交 → 重新排队
            self.clock.tick(minutes=16)                     # 一直没有流水线来取结果
            Q.snapshot()
        self.assertEqual(self.state(jid), "cancelled")
        self.assertEqual(Q.submit(jid, lid, "a1", GOOD)["status"], 409)
        self.assertNotEqual(self.job(), jid)                # 同样的题再出一次：新开一道

    def test_waiting_pipeline_keeps_the_job_alive(self):
        with patch.object(Q, "ORPHAN_SECONDS", 15 * 60):
            jid = self.job()
            self.clock.tick(minutes=10)
            with self.assertRaises(RuntimeError):
                Q.wait(jid, timeout=0.05, poll=0.01)        # 等的时候报到
            self.clock.tick(minutes=10)
            Q.snapshot()
            self.assertEqual(self.state(jid), "queued")

    def test_keys_identify_agents_and_can_be_rotated_or_disabled(self):
        token = Q.add_agent("a1", 200_000, model="claude")
        self.assertEqual(Q.agent_for_token(token), ("a1", True))
        self.assertIsNone(Q.agent_for_token("rq_wrong"))
        new = Q.rotate_token("a1")
        self.assertIsNone(Q.agent_for_token(token))
        self.assertEqual(Q.agent_for_token(new), ("a1", True))
        Q.update_agent("a1", enabled=False)
        self.assertEqual(Q.agent_for_token(new), ("a1", False))
        self.assertEqual(Q.claim("a1")["status"], 403)
        with self.assertRaises(ValueError):
            Q.add_agent("a1", 200_000)
        with self.assertRaises(ValueError):
            Q.add_agent("bad name", 200_000)

    def test_other_agents_cannot_use_someone_elses_lease(self):
        Q.add_agent("a1", 200_000)
        Q.add_agent("a2", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        self.assertEqual(Q.submit(jid, lid, "a2", GOOD)["status"], 404)
        self.assertEqual(Q.get_input(jid, lid, "a2")["status"], 404)
        self.assertEqual(Q.submit(jid, "not-an-id", "a1", GOOD)["status"], 400)

    # ── 审查后补的：中文按字估、非标准或样子不对的答案、截止后才交 ──
    def test_chinese_text_counts_one_token_per_character(self):
        Q.add_agent("small", 200_000)
        Q.add_agent("big", 1_000_000)
        jid = self.job("中" * 150_000)                    # 15 万字中文 ≈ 15 万 token：200k 的处理方读不下
        self.assertGreaterEqual(Q.job_detail(jid)["job"]["input_tokens"], 150_000)
        self.assertEqual(Q.claim("small")["reason"], "nothing_fits")
        self.assertEqual(Q.claim("big")["job"]["id"], jid)

    def test_nonstandard_or_wrongly_shaped_answers_are_returned(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        for bad, word in (('{"picks": NaN}', "NaN"), ('{"picks": "R1"}', "列表"), ('{"picks": ["R1"]}', "对象"), ('{"picks": {}}', "列表")):
            res = Q.submit(jid, lid, "a1", bad)
            self.assertEqual(res["status"], 422, bad)
            self.assertIn(word, res["error"], bad)
        ok = Q.submit(jid, lid, "a1", 'Sure! <think>hmm</think> {"picks": [{"record": "R1"}]} hope it helps')
        self.assertEqual(ok["status"], 200)
        self.assertEqual(Q.wait(jid, timeout=1, poll=0.01)[0], GOOD)   # 只存读出来的 JSON，外面的话不存

    def test_answer_after_the_deadline_is_recorded_as_late_even_if_nobody_else_came(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        lid = Q.claim("a1")["job"]["lease_id"]
        self.clock.tick(seconds=Q.LEASE_SECONDS + 60)
        res = Q.submit(jid, lid, "a1", GOOD)                # 截止后第一个请求就是交答案
        self.assertEqual((res["status"], res["late"]), (200, True))
        a1 = next(a for a in Q.list_agents() if a["name"] == "a1")
        self.assertEqual((a1["timeout_count"], a1["consecutive_timeouts"]), (1, 0))
        kinds = [e["kind"] for e in Q.job_detail(jid)["events"]]
        self.assertEqual(kinds[-3:], ["expired", "requeued", "late_accepted"])

    def test_agents_are_told_what_the_answer_field_must_look_like(self):
        Q.add_agent("a1", 200_000)
        jid = self.job()
        got = Q.claim("a1")["job"]
        self.assertEqual(got["output"]["top_type"], "list_of_objects")
        self.assertIn("对象", Q.get_input(jid, got["lease_id"], "a1")["output"]["top_type_label"])

    # ── 认题、开关 ──
    def test_classify_knows_each_step_and_the_required_answer_field(self):
        from replay import business, progress
        terms = dict(advertiser="Chime", name="C1", context=["LG"], links=["23702"], issues=[])
        pick = [{"role": "system", "content": business.PICK_SYSTEM.format(advertiser="Chime", campaign="C1", keywords="LG", max_quote=300)},
                {"role": "user", "content": "Records"}]
        self.assertEqual(Q.classify(pick), ("business_pick", "picks", "list_of_objects"))
        update = [{"role": "system", "content": progress._system(terms)}, {"role": "user", "content": "As-of time: x\n\nNew records (chronological):"}]
        self.assertEqual(Q.classify(update), ("progress_update", "items", "list_of_objects"))
        combine = [update[0], {"role": "user", "content": "As-of time: x\n\nNew events (chronological). ..."}]
        self.assertEqual(Q.classify(combine)[0], "progress_combine")
        fix = pick + [{"role": "assistant", "content": "{"}, {"role": "user", "content": "fix"}]
        self.assertEqual(Q.classify(fix), ("json_fix", "picks", "list_of_objects"))

    def test_pipeline_uses_the_queue_only_when_switched_on(self):
        from replay import pipeline
        with patch.dict(os.environ, {"AI_EXECUTOR": "queue"}):
            self.assertTrue(callable(pipeline._ask("5023", "业务版")))
        with patch.dict(os.environ, {"AI_EXECUTOR": "gateway"}):
            self.assertIsNone(pipeline._ask("5023", "业务版"))


if __name__ == "__main__":
    unittest.main()
