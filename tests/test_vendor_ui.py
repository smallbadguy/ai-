"""HTTP-level regression coverage for campaign/vendor replay scopes."""
import http.client
import http.server
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service import auth, server, vendor_ui  # noqa: E402


CAMPAIGN_ID = "77"
VENDORS = [
    {"id": "9", "name": "Vendor Nine", "links": ["901"], "active": False},
    {"id": "10", "name": "Vendor Ten", "links": ["902"], "active": None},
]


class FakeLogin:
    enabled = True
    ready = True

    def user(self, cookie_header):
        return "test.user@example.test" if "test_session=ok" in (cookie_header or "") else None

    def origin_ok(self, origin):
        return origin.rstrip("/") == "https://replay.example.test"


class VendorUIHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.out = self.root / "out"
        self.state = self.root / "state"
        self.out.mkdir()
        self.state.mkdir()

        self._replace(server, "OUT", self.out)
        self._replace(server, "STATE", self.state)
        self._replace(server, "AUTH", auth.Auth(auth.load_config({})))
        self._replace(server, "ensure_list", lambda: None)
        self._replace(server, "RUNNING", set())
        self._replace(server, "RUNNING_VENDORS", {})
        self._replace(server.campaign, "vendor_options", lambda cid, refresh=False: list(VENDORS))
        self._replace(server.campaign, "validate_vendor",
                      lambda cid, key: str(cid) == CAMPAIGN_ID and str(key) in {"9", "10"})
        self._replace(server.campaign, "load_list", lambda: {
            "campaigns": [{"id": CAMPAIGN_ID, "name": "Synthetic Campaign 77"}],
            "queried_at": "test-only",
        })
        self._replace(server.campaign, "find", lambda cid: {
            "id": CAMPAIGN_ID, "name": "Synthetic Campaign 77",
        })
        self._replace(server.campaign, "info", lambda cid: {"links": []})
        self._replace(server.campaign, "inactive", lambda cid: False)
        self._replace(server.pipeline, "known_numbers", lambda: [])

        def read_state(cid, vendor=None):
            return {
                "campaign": "Synthetic Campaign 77",
                "scope": vendor or "all",
                "private_state_marker": "must-not-appear-in-public-vendor-json",
                "last_ok": {"finished_at": "2026-11-01T00:00:00Z"},
            }

        self._replace(server.pipeline, "read_state", read_state)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _replace(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)
        return p

    def request(self, path, method="GET", cookie="", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1])
        sent = dict(headers or {})
        if cookie:
            sent["Cookie"] = cookie
        if method == "POST":
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            return response.status, response.msg, response.read().decode("utf-8")
        finally:
            conn.close()

    def write_page(self, vendor, filename="business.en.html", label=None, case_data=None):
        directory = server.scoped_out_dir(CAMPAIGN_ID, vendor)
        directory.mkdir(parents=True, exist_ok=True)
        case_script = ""
        if case_data is not None:
            case_script = ('<script id="case-data" type="application/json">'
                           + vendor_ui.safe_json(case_data) + "</script>")
        scope_name = label or (f"vendor-{vendor}" if vendor else "aggregate")
        page = ("<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
                f'<div class="app"><header id="top">{scope_name}</header>'
                f"<main>PAGE-{scope_name}</main>{case_script}</div></body></html>")
        (directory / filename).write_text(page, encoding="utf-8")
        return directory / filename

    def test_generated_vendor_pages_are_read_from_their_own_scopes(self):
        self.write_page(None, label="aggregate-secret")
        self.write_page("9", label="scope-nine")
        self.write_page("10", label="scope-ten")
        with patch.object(server, "start_refresh") as start:
            code9, _, page9 = self.request("/c/77/en?vendor=9")
            code10, _, page10 = self.request("/c/77/en?vendor=10")
        self.assertEqual((code9, code10), (200, 200))
        self.assertIn("PAGE-scope-nine", page9)
        self.assertNotIn("aggregate-secret", page9)
        self.assertNotIn("scope-ten", page9)
        self.assertIn("PAGE-scope-ten", page10)
        self.assertNotIn("aggregate-secret", page10)
        self.assertNotIn("scope-nine", page10)
        self.assertNotIn('id="rp-vendor-ui"', page9)                # 回放页不加状态栏（刷新在首页）
        self.assertNotIn('id="rp-vendor-select"', page9)            # 一个页面就是一个「活动 + 渠道」，不再切换
        # 多渠道的活动不做合并版：不带渠道的旧地址先选渠道，不显示合并页面
        code_all, _, page_all = self.request("/c/77/en")
        self.assertEqual(code_all, 200)
        self.assertNotIn("aggregate-secret", page_all)
        self.assertIn('href="/c/77/en?vendor=9"', page_all)
        self.assertIn('href="/c/77/en?vendor=10"', page_all)
        code_detail, _, detail_all = self.request("/c/77/zh/detail")
        self.assertEqual(code_detail, 200)
        self.assertIn('href="/c/77/zh/detail?vendor=9"', detail_all)
        start.assert_not_called()

    def test_unbuilt_vendor_is_404_with_build_button_and_selector_not_aggregate_fallback(self):
        self.write_page(None, label="aggregate-only-page")
        code, _, page = self.request("/c/77/en?vendor=9")
        self.assertEqual(code, 404)
        self.assertNotIn("aggregate-only-page", page)
        self.assertIn("Build replay", page)
        self.assertIn('name="vendor" value="9"', page)
        self.assertIn('href="/c/77/zh?vendor=9"', page)

    def test_vendor_navigation_keeps_scope_on_language_and_edition_switches(self):
        self.write_page("9", "business.en.html", "vendor-nine-business")
        self.write_page("9", "replay.en.html", "vendor-nine-detail")
        code, _, business = self.request("/c/77/en?vendor=9")
        self.assertEqual(code, 200)
        self.assertIn('href="/c/77/zh?vendor=9"', business)
        self.assertIn('href="/c/77/en/detail?vendor=9"', business)
        code, _, detail = self.request("/c/77/en/detail?vendor=9")
        self.assertEqual(code, 200)
        self.assertIn('href="/c/77/zh/detail?vendor=9"', detail)
        self.assertIn('href="/c/77/en?vendor=9"', detail)

    def test_invalid_path_and_repeated_vendor_values_are_rejected(self):
        bad_queries = (
            "vendor=abc",
            "vendor=../10",
            "vendor=%2Fetc%2Fpasswd",
            "vendor=9%2F..%2F10",
            "vendor=9&vendor=10",
            "vendor=&vendor=9",
            "vendor=&vendor=",
        )
        for query in bad_queries:
            with self.subTest(query=query):
                self.assertEqual(self.request("/c/77/en?" + query)[0], 400)
        bad_posts = ("vendor=abc", "vendor=9&vendor=10", "vendor=&vendor=9", "vendor=&vendor=")
        with patch.object(server, "start_refresh") as start:
            for fields in bad_posts:
                with self.subTest(post_fields=fields):
                    self.assertEqual(self.request(
                        "/c/77/refresh", "POST", body="lang=en&" + fields)[0], 400)
            start.assert_not_called()
        self.assertEqual(self.request("/c/77/en/../10")[0], 404)

    def test_well_formed_vendor_not_registered_to_this_campaign_is_404(self):
        self.assertEqual(self.request("/c/77/en?vendor=11")[0], 404)

    def test_vendors_json_preserves_paused_and_unknown_activity_without_raw_state(self):
        code, headers, body = self.request("/c/77/vendors.json")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        self.assertEqual([(item["id"], item["active"]) for item in payload["options"]],
                         [("9", False), ("10", None)])
        self.assertEqual(set(payload["scopes"]), {"all", "9", "10"})
        for scope_status in payload["scopes"].values():
            self.assertNotIn("state", scope_status)
        self.assertNotIn("private_state_marker", body)

    def test_vendor_get_and_post_routes_require_a_login(self):
        self.write_page("9", label="authenticated-vendor-page")
        self._replace(server, "AUTH", FakeLogin())
        with patch.object(server, "start_refresh") as start:
            get_code, get_headers, _ = self.request("/c/77/en?vendor=9")
            post_code, post_headers, _ = self.request(
                "/c/77/refresh", "POST", body="lang=en&vendor=9")
            self.assertEqual((get_code, post_code), (303, 303))
            self.assertTrue(get_headers["Location"].startswith("/auth/login?next="))
            self.assertEqual(post_headers["Location"], "/auth/login?next=%2F")
            start.assert_not_called()
        self.assertEqual(self.request("/c/77/en?vendor=9", cookie="test_session=ok")[0], 200)

    def test_refresh_post_passes_vendor_to_mocked_start_refresh(self):
        with patch.object(server, "start_refresh", return_value=True) as start, \
                patch.object(server.LIMITS, "allow_refresh", return_value=True):
            code, headers, _ = self.request("/c/77/refresh", "POST", body="lang=zh&vendor=9")
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "/?lang=zh&msg=started_refresh")
        start.assert_called_once_with("77", "9")

    def test_add_post_passes_vendor_to_mocked_start_refresh(self):
        with patch.object(server, "start_refresh", return_value=True) as start, \
                patch.object(server.LIMITS, "allow_add", return_value=True):
            code, headers, _ = self.request(
                "/add", "POST", body="campaign_id=77&lang=en&vendor=10")
        self.assertEqual(code, 303)
        self.assertIn("msg=started", headers["Location"])
        start.assert_called_once_with("77", "10")

    def test_external_campaign_lock_blocks_start_without_started_message(self):
        lock_path = self.state / "77.lock"
        script = ("import fcntl, sys\n"
                  "lock = open(sys.argv[1], 'a')\n"
                  "fcntl.flock(lock.fileno(), fcntl.LOCK_EX)\n"
                  "print('LOCKED', flush=True)\n"
                  "sys.stdin.readline()\n")
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop_process():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

        self.addCleanup(stop_process)
        self.assertEqual(process.stdout.readline().strip(), "LOCKED")
        with patch.object(server, "start_refresh", return_value=True) as start, \
                patch.object(server.LIMITS, "allow_refresh", return_value=True):
            code, headers, _ = self.request(
                "/c/77/refresh", "POST", body="lang=zh&vendor=9")
        self.assertEqual(code, 303)
        self.assertIn("msg=processing", headers["Location"])
        self.assertNotIn("msg=started", headers["Location"])
        start.assert_not_called()
        process.stdin.write("release\n")
        process.stdin.flush()
        process.wait(timeout=3)

    def test_progress_redirect_keeps_the_vendor_query(self):
        self.write_page("9", "business.en.html", "vendor-nine-business")
        code, headers, _ = self.request("/c/77/zh/progress?vendor=9")
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "/c/77/zh?vendor=9#t=progress")

    def test_replay_page_is_served_as_generated_with_only_the_navigation_added(self):
        # 2026-11-02 用户要求：回放页面的界面不能改。页面原样返回，只在 </body> 前加原有的导航条；
        # 不加状态栏、刷新按钮，也不把「待确认」记录摆到页面上。
        data = {"client_background": [{"speaker": "Client", "text": "uncertain-record-text"}],
                "client_background_total": 1}
        path = self.write_page("9", case_data=data, label="scope-nine")
        original = path.read_text(encoding="utf-8")
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                path.with_name(f"business.{lang}.html").write_text(original, encoding="utf-8")
                code, _, page = self.request(f"/c/77/{lang}?vendor=9")
                self.assertEqual(code, 200)
                cut = original.rfind("</body>")
                self.assertTrue(page.startswith(original[:cut]))           # 页面本身一个字不改
                self.assertTrue(page.endswith(original[cut:]))
                self.assertIn('id="rp-nav"', page)
                for injected in ('id="rp-vendor-ui"', 'rp-client-background', 'action="/c/77/refresh"',
                                 "Refresh replay", "刷新回放"):
                    self.assertNotIn(injected, page)
                visible = re.sub(r'<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>', '', page, flags=re.S | re.I)
                self.assertNotIn("uncertain-record-text", visible)

    def test_homepage_lists_each_campaign_channel_as_its_own_row_without_a_combined_one(self):
        self.write_page(None, "business.en.html", "aggregate-business")
        self.write_page(None, "replay.en.html", "aggregate-detail")
        self.write_page("9", "replay.en.html", "vendor-nine-detail")
        self._replace(server.pipeline, "known_numbers", lambda: [CAMPAIGN_ID])
        code, _, page = self.request("/?lang=en")
        self.assertEqual(code, 200)
        rows = page.split('<tbody id="rows">', 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(rows.count("<tr"), 2)                       # 两个渠道两行，没有合并版
        self.assertIn("Synthetic Campaign 77", rows)
        self.assertIn("Vendor Nine #901 (Paused)", rows)
        self.assertIn("Vendor Ten #902 (Status pending)", rows)
        self.assertIn('href="/c/77/en/detail?vendor=9"', rows)
        self.assertNotIn('href="/c/77/en"', rows)                  # 合并版页面不列出来
        self.assertIn('name="vendor" value="9"', rows)
        self.assertIn('name="vendor" value="10"', rows)
        self.assertIn('href="/static/bootstrap.min.css"', page)
        self.assertNotIn("Load channels", page)

    def test_multi_channel_campaign_must_choose_a_channel_to_build(self):
        with patch.object(server, "start_refresh", return_value=True) as start, \
                patch.object(server.LIMITS, "allow_add", return_value=True), \
                patch.object(server.LIMITS, "allow_refresh", return_value=True):
            add_code, add_headers, _ = self.request("/add", "POST", body="campaign_id=77&lang=en&vendor=")
            ref_code, ref_headers, _ = self.request("/c/77/refresh", "POST", body="lang=zh")
        self.assertEqual((add_code, ref_code), (303, 303))
        self.assertIn("msg=choose_channel", add_headers["Location"])
        self.assertIn("msg=choose_channel", ref_headers["Location"])
        start.assert_not_called()

    def test_homepage_stylesheet_is_served_locally(self):
        code, headers, body = self.request("/static/bootstrap.min.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        self.assertIn("Bootstrap", body[:200])
        self.assertNotIn("no-store", headers.get("Cache-Control", ""))


class PipelineReservationTests(unittest.TestCase):
    def test_queued_vendor_holds_root_lock_until_its_pipeline_finishes(self):
        from replay import scope
        threads, seen = [], []
        real_thread = threading.Thread
        gate = threading.Semaphore(0)

        def spawn(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        def run(cid):
            seen.append((cid, scope.vendor_for(cid)))
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, 'STATE', pathlib.Path(tmp)), \
                patch.object(server.pipeline, 'STATE', pathlib.Path(tmp)), \
                patch.object(server, 'RUNNING', set()), \
                patch.object(server, 'RUNNING_VENDORS', {}), \
                patch.object(server, 'SLOTS', gate), \
                patch.object(server.pipeline, '_refresh_locked', side_effect=run), \
                patch.object(server.threading, 'Thread', side_effect=spawn):
            try:
                self.assertTrue(server.start_refresh('77', '9'))
                self.assertTrue(server._root_lock_busy('77'))
                self.assertEqual(server.running_phase('77', '9'), 'queued')
                self.assertIsNone(server.running_phase('77', None, {'running': True}))
                self.assertFalse(server.start_refresh('77', '10'))
                self.assertTrue(server.pipeline.refresh('77', vendor='10')['busy'])
                self.assertEqual(seen, [])
            finally:
                gate.release()
                for thread in threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(seen, [('77', '9')])
            self.assertFalse(server._root_lock_busy('77'))
            self.assertEqual(server.RUNNING, set())
            self.assertEqual(server.RUNNING_VENDORS, {})

    def test_thread_start_failure_releases_reserved_lock(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, 'STATE', pathlib.Path(tmp)), \
                patch.object(server.pipeline, 'STATE', pathlib.Path(tmp)), \
                patch.object(server, 'RUNNING', set()), \
                patch.object(server, 'RUNNING_VENDORS', {}), \
                patch.object(server.threading, 'Thread', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                server.start_refresh('77', '9')
            self.assertFalse(server._root_lock_busy('77'))
            self.assertEqual(server.RUNNING, set())
            self.assertEqual(server.RUNNING_VENDORS, {})


if __name__ == "__main__":
    unittest.main()
