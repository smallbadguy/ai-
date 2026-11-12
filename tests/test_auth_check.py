"""/auth/check：Caddy 在进入 /v1/、/v2/ 两套独立服务前，用它核对同一个公司登录。"""
import http.server
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from service import server


class FakeAuth:
    enabled = True
    ready = True

    def user(self, cookie):
        return "ann@feedmob.com" if cookie and "ok=1" in cookie else ""

    def origin_ok(self, origin):
        return origin == "https://replay-stage.feedmob.it.com"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AuthCheck(unittest.TestCase):
    def setUp(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()

    def call(self, headers):
        req = urllib.request.Request(self.base + "/auth/check", headers=headers)
        try:
            with self.opener.open(req) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def test_logged_in_passes_and_anonymous_goes_to_login(self):
        with patch.object(server, "AUTH", FakeAuth()):
            code, headers = self.call({"Cookie": "ok=1", "X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/v1/c/5/zh"})
            self.assertEqual((code, headers.get("X-Replay-User")), (200, "ann@feedmob.com"))
            code, headers = self.call({"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/v1/c/5/zh"})
            self.assertEqual(code, 303)
            self.assertEqual(headers["Location"], "/auth/login?next=%2Fv1%2Fc%2F5%2Fzh")

    def test_posts_need_login_and_same_origin(self):
        with patch.object(server, "AUTH", FakeAuth()):
            self.assertEqual(self.call({"X-Forwarded-Method": "POST"})[0], 401)
            self.assertEqual(self.call({"Cookie": "ok=1", "X-Forwarded-Method": "POST", "Origin": "https://evil.example"})[0], 403)
            self.assertEqual(self.call({"Cookie": "ok=1", "X-Forwarded-Method": "POST",
                                        "Origin": "https://replay-stage.feedmob.it.com"})[0], 200)


if __name__ == "__main__":
    unittest.main()
