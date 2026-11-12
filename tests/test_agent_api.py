"""外部 agent 的接口（/agent-api/v1/…）：钥匙、领题到交答案的整条路、请求体检查；以及它不走 Google 登录、状态页要登录。"""
import email.message
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import jobqueue as Q  # noqa: E402
from service import agent_api, server  # noqa: E402

SYSTEM = 'Do the work.\nOutput one JSON object and nothing else:\n{"picks": []}'


class FakeHandler:
    def __init__(self, token=None, body=None, raw=None):
        self.headers = email.message.Message()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.headers["Content-Length"] = str(len(data))
        self.rfile = io.BytesIO(data)
        self.sent = None

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        self.sent = (code, body, ctype)


def call(method, route, token=None, body=None, query=None, raw=None):
    h = FakeHandler(token, body, raw)
    agent_api.handle(h, method, route, query or {})
    code, text, ctype = h.sent
    return code, (json.loads(text) if "json" in ctype else text)


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(Q, "DB_PATH", pathlib.Path(tmp.name) / "queue.db")
        p.start()
        self.addCleanup(p.stop)
        self.key = Q.add_agent("hermes", 200_000, model="claude")

    def test_protocol_is_readable_without_a_key_but_everything_else_needs_one(self):
        code, text = call("GET", "/agent-api/v1/protocol")
        self.assertEqual(code, 200)
        self.assertIn("/claim", text)
        self.assertEqual(call("POST", "/agent-api/v1/claim")[0], 401)
        self.assertEqual(call("POST", "/agent-api/v1/claim", token="rq_nope")[0], 401)

    def test_whole_round_trip_from_claim_to_accepted_answer(self):
        jid = Q.enqueue([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "records"}], campaign="5023",
                        data_fetched_at="2026-10-30T04:30:00Z", data_as_of="2026-10-30T02:00:00Z")
        code, res = call("POST", "/agent-api/v1/claim", self.key, {})
        self.assertEqual(code, 200)
        job = res["job"]
        self.assertEqual(job["id"], jid)
        code, inp = call("GET", f"/agent-api/v1/jobs/{jid}/input", self.key, query={"lease": [job["lease_id"]]})
        self.assertEqual(code, 200)
        self.assertEqual(inp["messages"][1]["content"], "records")
        self.assertEqual((inp["data_fetched_at"], inp["data_as_of"]), ("2026-10-30T04:30:00Z", "2026-10-30T02:00:00Z"))
        self.assertTrue(inp["input_fetched_at"])
        code, hb = call("POST", f"/agent-api/v1/jobs/{jid}/heartbeat", self.key, {"lease_id": job["lease_id"]})
        self.assertEqual((code, hb["continue"]), (200, True))
        code, bad = call("POST", f"/agent-api/v1/jobs/{jid}/result", self.key, {"lease_id": job["lease_id"], "output": "{oops"})
        self.assertEqual(code, 422)
        self.assertFalse(bad["ok"])
        code, ok = call("POST", f"/agent-api/v1/jobs/{jid}/result", self.key,
                        {"lease_id": job["lease_id"], "output": {"picks": []}, "model": "claude"})  # 直接放 JSON 对象也行
        self.assertEqual((code, ok["accepted"], ok["ok"]), (200, True, True))
        self.assertEqual(json.loads(Q.wait(jid, timeout=1, poll=0.01)[0]), {"picks": []})
        code, me = call("GET", "/agent-api/v1/me", self.key)
        self.assertEqual(me["agent"]["done_count"], 1)

    def test_empty_queue_says_so(self):
        code, res = call("POST", "/agent-api/v1/claim", self.key, {})
        self.assertEqual((code, res["job"], res["reason"]), (200, None, "empty"))

    def test_bad_requests_get_clear_errors(self):
        self.assertEqual(call("GET", "/agent-api/v1/claim", self.key)[0], 405)
        self.assertEqual(call("POST", "/agent-api/v1/claim", self.key, raw=b"not json")[0], 400)
        self.assertEqual(call("POST", "/agent-api/v1/nothing", self.key, {})[0], 404)
        self.assertEqual(call("POST", "/agent-api/v1/jobs/J1234567890/result", self.key, {"lease_id": "L1234567890"})[0], 404)
        with patch.object(agent_api, "MAX_BODY", 10):
            self.assertEqual(call("POST", "/agent-api/v1/claim", self.key, {"x": "0123456789"})[0], 413)

    def test_disabled_agent_is_refused(self):
        Q.update_agent("hermes", enabled=False)
        self.assertEqual(call("POST", "/agent-api/v1/claim", self.key, {})[0], 403)

    def test_protocol_is_the_onboarding_doc(self):
        code, text = call("GET", "/agent-api/v1/protocol")
        self.assertEqual(text, agent_api.DOC.read_text(encoding="utf-8"))
        self.assertIn("retryable", text)

    def test_release_with_retryable_does_not_count_as_a_failure(self):
        jid = Q.enqueue([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "records"}], campaign="5023")
        lid = call("POST", "/agent-api/v1/claim", self.key, {})[1]["job"]["lease_id"]
        code, res = call("POST", f"/agent-api/v1/jobs/{jid}/release", self.key, {"lease_id": lid, "reason": "429", "retryable": True})
        self.assertEqual(code, 200)
        self.assertEqual(Q.job_detail(jid)["job"]["failures"], 0)
        lid = call("POST", "/agent-api/v1/claim", self.key, {})[1]["job"]["lease_id"]
        call("POST", f"/agent-api/v1/jobs/{jid}/release", self.key, {"lease_id": lid, "reason": "做不了"})
        self.assertEqual(Q.job_detail(jid)["job"]["failures"], 1)

    def test_our_own_worker_solves_a_job_through_the_same_interface(self):
        from service import ai_worker as W

        def fake_api(method, path, token, body=None, timeout=120):
            route, _, qs = path.partition("?")
            query = {k: [v] for k, v in (kv.split("=", 1) for kv in qs.split("&") if kv)}
            return call(method, "/agent-api/v1" + route, token, body, query)

        with tempfile.TemporaryDirectory() as tmp, patch.object(W.P, "SECRETS", pathlib.Path(tmp)), \
                patch.object(W, "api", fake_api):
            tokens = W.ensure_agents(["internal-1"])
            self.assertEqual(W.ensure_agents(["internal-1"]), tokens)      # 重启：钥匙不变
            jid = Q.enqueue([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "records"}], campaign="5023")
            code, got = fake_api("POST", "/claim", tokens["internal-1"], {})
            answers = iter(["not json", '{"picks": []}'])                   # 第一次不合格，改了再交
            res = W.solve("internal-1", tokens["internal-1"], got["job"], ask=lambda convo: (next(answers), {}))
        self.assertEqual(res, "accepted")
        self.assertEqual(Q.job_detail(jid)["job"]["state"], "done")
        events = [e["kind"] for e in Q.job_detail(jid)["events"]]
        self.assertIn("rejected", events)


class _Auth:
    enabled, ready = True, True

    def user(self, cookie):
        return None

    def origin_ok(self, origin):
        return True


def raw_request(method, path, body=b""):
    h = server.Handler.__new__(server.Handler)
    h.path, h.command, h.request_version = path, method, "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.headers = email.message.Message()
    h.headers["Content-Length"] = str(len(body))
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    getattr(h, "do_" + method)()
    return int(h.wfile.getvalue().split(b" ", 2)[1])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(Q, "DB_PATH", pathlib.Path(tmp.name) / "queue.db"), patch.object(server, "AUTH", _Auth()),
                  patch.object(server.Handler, "log_message", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_agent_api_uses_its_own_keys_instead_of_google_login(self):
        self.assertEqual(raw_request("POST", "/agent-api/v1/claim", b"{}"), 401)   # 不是跳去 Google 登录（303）
        self.assertEqual(raw_request("GET", "/agent-api/v1/protocol"), 200)

    def test_queue_page_needs_a_google_login(self):
        self.assertEqual(raw_request("GET", "/queue"), 303)
        self.assertEqual(raw_request("GET", "/queue.json"), 303)


class QueuePageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(Q, "DB_PATH", pathlib.Path(tmp.name) / "queue.db")
        p.start()
        self.addCleanup(p.stop)

    def test_pages_show_agents_jobs_and_the_full_story_of_a_job(self):
        from service import queue_page
        Q.add_agent("hermes", 200_000, model="claude", note="<b>x</b>")
        jid = Q.enqueue([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "r"}], campaign="5023", step="业务版")
        lid = Q.claim("hermes")["job"]["lease_id"]
        Q.submit(jid, lid, "hermes", '{"picks": []}')
        page = queue_page.overview(Q.snapshot(), {"5023": "Chime LG"})
        self.assertIn("hermes", page)
        self.assertIn("Chime LG", page)
        self.assertIn("已完成", page)
        self.assertNotIn("<b>x</b>", page)  # 备注要转义
        detail = queue_page.detail(Q.job_detail(jid), {"5023": "Chime LG"})
        for word in ("出题", "领取", "收下", "原料截至"):
            self.assertIn(word, detail)


if __name__ == "__main__":
    unittest.main()
