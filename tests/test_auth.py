"""Google 登录：用本机假的 Google 令牌接口跑完整流程，并覆盖各种该拒绝的情况。"""
import base64
import hashlib
import http.client
import http.server
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service import auth, server

CLIENT_ID = "test-client.apps.googleusercontent.com"
PUBLIC = "https://replay-stage.example.test"
NOW = 1_800_000_000.0


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_id_token(claims):
    """没有真签名：服务按规范直接信任从令牌接口（TLS）拿到的 id_token，只校验内容。"""
    return f"{b64(json.dumps({'alg': 'RS256'}).encode())}.{b64(json.dumps(claims).encode())}.sig"


class FakeGoogle:
    def __init__(self):
        self.id_token = ""
        self.status = 200
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                outer.requests.append({k: v[0] for k, v in body.items()})
                data = json.dumps({"id_token": outer.id_token} if outer.status == 200 else {"error": "invalid_grant"}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/token"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def cookie_pair(set_cookie):
    return set_cookie.split(";", 1)[0]


class LoginBase(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secrets = pathlib.Path(tmp.name) / "secrets"
        self.secrets.mkdir()
        (self.secrets / "google-oauth-client.json").write_text(
            json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": "shh"}}), encoding="utf-8")
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        self.now = [NOW]
        env = {"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": PUBLIC, "REPLAY_SECRETS": str(self.secrets),
               "REPLAY_OAUTH_AUTH_URL": "https://accounts.example.test/auth", "REPLAY_OAUTH_TOKEN_URL": self.google.url,
               **self.extra_env}
        self.auth = auth.Auth(auth.load_config(env), clock=lambda: self.now[0])
        for p in (patch.object(server, "AUTH", self.auth), patch.object(server, "ensure_list"),
                  patch.object(server.campaign, "load_list", return_value={"campaigns": [], "queried_at": ""}),
                  patch.object(server.pipeline, "known_numbers", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, method="GET", cookie="", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1])
        sent = dict(headers or {})
        if cookie:
            sent["Cookie"] = cookie
        try:
            conn.request(method, path, body, sent)
            r = conn.getresponse()
            return r.status, r.msg, r.read().decode("utf-8")
        finally:
            conn.close()

    def start_login(self, next_path="/"):
        status, headers, _ = self.request("/auth/login?" + urllib.parse.urlencode({"next": next_path}))
        self.assertEqual(status, 303)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)
        return headers, query, cookie_pair(headers.get_all("Set-Cookie")[0])

    def sign_in(self, email="liwei@feedmob.com", next_path="/", code="auth-code-123", state=None, flow_cookie=None, **claims):
        headers, query, flow = self.start_login(next_path)
        body = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": self.now[0] + 3600, "nonce": query["nonce"][0],
                "email": email, "email_verified": True, "hd": email.split("@", 1)[1]}
        body.update(claims)
        self.google.id_token = fake_id_token(body)
        path = "/auth/google/callback?" + urllib.parse.urlencode({"state": state or query["state"][0], "code": code})
        status, resp_headers, text = self.request(path, cookie=flow if flow_cookie is None else flow_cookie)
        return status, resp_headers, text, query

    def session_cookie(self, headers):
        for value in headers.get_all("Set-Cookie") or []:
            if value.startswith(auth.SESSION_COOKIE + "=") and "Max-Age=0" not in value:
                return cookie_pair(value)
        return ""


class LoginFlowTests(LoginBase):
    def test_pages_send_signed_out_visitors_to_login_and_remember_where_they_were_going(self):
        status, headers, _ = self.request("/")
        self.assertEqual((status, headers["Location"]), (303, "/auth/login?next=%2F"))
        status, headers, _ = self.request("/c/5023/zh/detail?x=1")
        self.assertEqual(headers["Location"], "/auth/login?next=%2Fc%2F5023%2Fzh%2Fdetail%3Fx%3D1")
        for path in ("/status", "/c/5023/status", "/rows.json?lang=zh"):
            self.assertEqual(self.request(path)[0], 401)  # 首页轮询拿到 401 就停，不会被跳转到登录页
        status, headers, _ = self.request("/c/5023/refresh", "POST", body="lang=zh")
        self.assertEqual((status, headers["Location"]), (303, "/auth/login?next=%2F"))

    def test_login_goes_to_google_with_pkce_and_the_company_domain(self):
        headers, q, flow = self.start_login("/c/5023/zh")
        location = headers["Location"]
        self.assertTrue(location.startswith("https://accounts.example.test/auth?"))
        one = {k: v[0] for k, v in q.items()}
        self.assertEqual(one["client_id"], CLIENT_ID)
        self.assertEqual(one["redirect_uri"], PUBLIC + "/auth/google/callback")
        self.assertEqual((one["response_type"], one["code_challenge_method"], one["hd"]), ("code", "S256", "feedmob.com"))
        self.assertIn("openid", one["scope"].split())
        self.assertIn("email", one["scope"].split())
        for key in ("state", "nonce", "code_challenge"):
            self.assertGreater(len(one[key]), 20)
        cookie = headers.get_all("Set-Cookie")[0]
        for part in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/auth/", "Max-Age=600"):
            self.assertIn(part, cookie)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_company_account_signs_in_and_returns_to_the_page_it_wanted(self):
        status, headers, _, query = self.sign_in(next_path="/c/5023/zh/detail")
        self.assertEqual((status, headers["Location"]), (303, "/c/5023/zh/detail"))
        sent = self.google.requests[-1]
        self.assertEqual((sent["code"], sent["client_id"], sent["client_secret"], sent["grant_type"]),
                         ("auth-code-123", CLIENT_ID, "shh", "authorization_code"))
        self.assertEqual(sent["redirect_uri"], PUBLIC + "/auth/google/callback")
        challenge = b64(hashlib.sha256(sent["code_verifier"].encode()).digest())
        self.assertEqual(challenge, query["code_challenge"][0])
        session = [c for c in headers.get_all("Set-Cookie") if c.startswith(auth.SESSION_COOKIE + "=")][0]
        for part in ("HttpOnly", "SameSite=Lax", "Secure", f"Max-Age={7 * 86400}"):
            self.assertIn(part, session)
        self.assertTrue(any(c.startswith(auth.FLOW_COOKIE + "=;") and "Max-Age=0" in c for c in headers.get_all("Set-Cookie")))
        page_status, _, page = self.request("/?lang=en", cookie=self.session_cookie(headers))
        self.assertEqual(page_status, 200)
        self.assertIn("liwei@feedmob.com", page)
        self.assertIn("Sign out", page)

    def test_email_domain_is_judged_by_the_part_after_the_last_at_sign(self):
        for email, extra in (("someone@gmail.com", {}), ("boss@feedmob.com.evil.example", {}), ("a@b@feedmob.com", {}),
                             ("x@feedmob.com", {"hd": "other.example"})):
            status, headers, text, _ = self.sign_in(email=email, **({"hd": extra["hd"]} if extra else {}))
            self.assertEqual(status, 403, email)
            self.assertEqual(self.session_cookie(headers), "", email)
        self.assertIn("Access denied", text)

    def test_unverified_email_is_refused(self):
        status, headers, _, _ = self.sign_in(email_verified=False)
        self.assertEqual((status, self.session_cookie(headers)), (403, ""))

    def test_wrong_token_contents_are_refused(self):
        for override in ({"aud": "someone-elses-app"}, {"iss": "https://evil.example"}, {"nonce": "replayed"},
                         {"exp": NOW - 3600}, {"aud": ["a", "b"]}):
            status, headers, _, _ = self.sign_in(**override)
            self.assertEqual((status, self.session_cookie(headers)), (403, ""), override)

    def test_state_and_flow_cookie_must_match_and_are_short_lived(self):
        status, _, _, _ = self.sign_in(state="forged")
        self.assertEqual(status, 400)
        status, _, _, _ = self.sign_in(flow_cookie="")
        self.assertEqual(status, 400)
        status, _, _, _ = self.sign_in(flow_cookie=auth.FLOW_COOKIE + "=garbage.value")
        self.assertEqual(status, 400)
        headers, query, flow = self.start_login()
        self.now[0] += auth.FLOW_SECONDS + 1
        status, _, _ = self.request("/auth/google/callback?" + urllib.parse.urlencode({"state": query["state"][0], "code": "x"}), cookie=flow)
        self.assertEqual(status, 400)

    def test_user_cancelling_at_google_is_a_polite_error(self):
        headers, query, flow = self.start_login()
        status, _, text = self.request("/auth/google/callback?" + urllib.parse.urlencode({"state": query["state"][0], "error": "access_denied"}), cookie=flow)
        self.assertEqual(status, 403)
        self.assertIn("Sign-in was not completed", text)

    def test_google_rejecting_or_failing_shows_a_friendly_error_not_a_session(self):
        self.google.status = 400
        status, headers, text, _ = self.sign_in()
        self.assertEqual((status, self.session_cookie(headers)), (502, ""))
        self.assertIn("temporarily unavailable", text)
        self.assertNotIn("shh", text)

    def test_session_cookie_cannot_be_forged_extended_or_outlive_its_permission(self):
        _, headers, _, _ = self.sign_in()
        cookie = self.session_cookie(headers)
        self.assertEqual(self.request("/", cookie=cookie)[0], 200)
        name, value = cookie.split("=", 1)
        body, mac = value.split(".")
        forged = auth.sign(b"another key", {"k": "session", "email": "ceo@feedmob.com", "exp": NOW + 99999})
        wrong_kind = auth.sign(self.auth.cfg.session_key, {"k": "flow", "email": "liwei@feedmob.com", "exp": NOW + 99999})
        for bad in (f"{name}={forged}", f"{name}={wrong_kind}", f"{name}={body}x.{mac}", f"{name}={body}", f"{name}="):
            self.assertEqual(self.request("/", cookie=bad)[0], 303, bad)
        with patch.object(self.auth.cfg, "allowed_domains", frozenset({"other.example"})):
            self.assertEqual(self.request("/", cookie=cookie)[0], 303)
        self.now[0] += 7 * 86400 + 1
        self.assertEqual(self.request("/", cookie=cookie)[0], 303)

    def test_login_never_redirects_to_another_site(self):
        for wanted in ("//evil.example/x", "https://evil.example", "/\\evil.example", "evil", "/auth/login", "/a\nb", ""):
            _, headers, _, _ = self.sign_in(next_path=wanted)
            self.assertEqual(headers["Location"], "/", repr(wanted))

    def test_already_signed_in_visitor_skips_google(self):
        _, headers, _, _ = self.sign_in()
        status, resp, _ = self.request("/auth/login?next=%2Fc%2F5%2Fen", cookie=self.session_cookie(headers))
        self.assertEqual((status, resp["Location"]), (303, "/c/5/en"))

    def test_writes_need_a_session_and_a_matching_origin(self):
        _, headers, _, _ = self.sign_in()
        cookie = self.session_cookie(headers)
        with patch.object(server, "start_refresh") as start, patch.object(server.LIMITS, "allow_refresh", return_value=True):
            self.assertEqual(self.request("/c/5023/refresh", "POST", cookie=cookie, headers={"Origin": "https://evil.example"}, body="lang=zh")[0], 403)
            self.assertEqual(self.request("/c/5023/refresh", "POST", cookie=cookie, headers={"Origin": "null"}, body="lang=zh")[0], 403)
            start.assert_not_called()
            self.assertEqual(self.request("/c/5023/refresh", "POST", headers={"Origin": PUBLIC}, body="lang=zh")[0], 303)
            start.assert_not_called()
            self.assertEqual(self.request("/c/5023/refresh", "POST", cookie=cookie, headers={"Origin": PUBLIC}, body="lang=zh")[0], 303)
            start.assert_called_once_with("5023")
            # 「刷新全部」2026-11-02 已去掉：登录了也只能逐个刷新
            self.assertEqual(self.request("/refresh-all", "POST", cookie=cookie, headers={"Origin": PUBLIC}, body="lang=zh")[0], 404)
            start.assert_called_once_with("5023")

    def test_login_endpoints_do_not_accept_posts_except_logout(self):
        self.assertEqual(self.request("/auth/login", "POST")[0], 405)
        self.assertEqual(self.request("/auth/google/callback", "POST")[0], 405)
        status, headers, _ = self.request("/auth/logout", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/auth/unknown")[0], 404)

    def test_logout_clears_the_session(self):
        status, headers, text = self.request("/auth/logout")
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("Signed out", text)

    def test_the_authorization_code_never_reaches_the_log(self):
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            self.sign_in(code="super-secret-code-987")
        self.assertNotIn("super-secret-code-987", err.getvalue())
        self.assertIn("/auth/google/callback?...", err.getvalue())


class IndividuallyAllowedEmailTests(LoginBase):
    extra_env = {"REPLAY_ALLOWED_EMAILS": "Partner@Gmail.com", "REPLAY_ALLOWED_DOMAINS": "feedmob.com,feedmob.cn"}

    def test_listed_outside_email_and_several_domains_work(self):
        self.assertEqual(self.sign_in(email="partner@gmail.com", hd=None)[0], 303)
        self.assertEqual(self.sign_in(email="x@feedmob.cn")[0], 303)
        self.assertEqual(self.sign_in(email="other@gmail.com", hd=None)[0], 403)

    def test_google_hint_is_only_sent_when_there_is_a_single_domain(self):
        _, query, _ = self.start_login()
        self.assertNotIn("hd", query)


class ClosedWhenMisconfiguredTests(unittest.TestCase):
    def serve(self, env):
        cfg = auth.load_config(env)
        for p in (patch.object(server, "AUTH", auth.Auth(cfg)), patch.object(server, "ensure_list"),
                  patch.object(server.campaign, "load_list", return_value={"campaigns": []}),
                  patch.object(server.pipeline, "known_numbers", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
            try:
                conn.request("GET", path)
                r = conn.getresponse()
                return r.status, r.read().decode("utf-8")
            finally:
                conn.close()
        return cfg, get

    def test_login_on_but_not_configured_means_closed_not_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            for env in ({"REPLAY_AUTH": "google"},
                        {"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": PUBLIC},
                        {"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": PUBLIC, "REPLAY_SECRETS": tmp},
                        {"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": "not a url", "REPLAY_SECRETS": tmp}):
                cfg, get = self.serve(env)
                self.assertTrue(cfg.enabled and not cfg.ready, env)
                for path in ("/", "/c/5023/zh", "/c/5023/zh/detail", "/status", "/auth/login", "/showcase/chime/zh"):
                    self.assertEqual(get(path)[0], 503, (env, path))
                self.assertEqual(get("/healthz"), (200, "ok"))

    def test_unreadable_client_file_means_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "google-oauth-client.json").write_text("{not json")
            cfg, get = self.serve({"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": PUBLIC, "REPLAY_SECRETS": tmp})
            self.assertFalse(cfg.ready)
            self.assertEqual(get("/")[0], 503)

    def test_login_off_keeps_the_site_open_as_before(self):
        cfg, get = self.serve({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(get("/")[0], 200)
        self.assertEqual(get("/healthz"), (200, "ok"))
        self.assertEqual(get("/auth/login")[0], 404)


class ConfigTests(unittest.TestCase):
    def make(self, tmp, **extra):
        (pathlib.Path(tmp) / "google-oauth-client.json").write_text(json.dumps({"web": {"client_id": "id", "client_secret": "secret"}}))
        return auth.load_config({"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": PUBLIC + "/", "REPLAY_SECRETS": tmp, **extra})

    def test_valid_configuration_and_a_private_stable_session_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make(tmp)
            self.assertTrue(cfg.ready)
            self.assertEqual(cfg.public_url, PUBLIC)  # 末尾斜杠去掉
            self.assertEqual(cfg.redirect_uri, PUBLIC + "/auth/google/callback")
            self.assertEqual(cfg.allowed_domains, frozenset({"feedmob.com"}))
            path = pathlib.Path(tmp) / "session.key"
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(self.make(tmp).session_key, cfg.session_key)  # 重启后登录不失效

    def test_google_download_formats_and_session_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "google-oauth-client.json").write_text(json.dumps({"installed": {"client_id": "i", "client_secret": "s"}}))
            cfg = auth.load_config({"REPLAY_AUTH": "google", "REPLAY_PUBLIC_URL": "http://localhost:8791", "REPLAY_SECRETS": tmp,
                                    "REPLAY_SESSION_DAYS": "0.5"})
            self.assertTrue(cfg.ready)
            self.assertEqual((cfg.client_id, cfg.session_seconds, cfg.secure_cookie), ("i", 43200, False))
            self.assertFalse(auth.load_config({"REPLAY_AUTH": "google", "REPLAY_SESSION_DAYS": "many"}).ready)

    def test_login_is_off_unless_explicitly_enabled(self):
        for env in ({}, {"REPLAY_AUTH": ""}, {"REPLAY_AUTH": "off"}):
            self.assertFalse(auth.load_config(env).enabled)

    def test_safe_next_only_allows_site_paths(self):
        self.assertEqual(auth.safe_next("/c/5023/zh?v=1"), "/c/5023/zh?v=1")
        for bad in (None, 5, "", "x", "//evil", "https://evil", "/\\evil", "/auth/login", "/auth/google/callback?x=1", "/a\r\nSet-Cookie: x=1"):
            self.assertEqual(auth.safe_next(bad), "/", repr(bad))

    def test_signed_values_reject_tampering_wrong_use_and_expiry(self):
        key = b"k" * 32
        token = auth.sign(key, {"k": "session", "email": "a@feedmob.com", "exp": 100})
        self.assertEqual(auth.unsign(key, token, "session", 50)["email"], "a@feedmob.com")
        self.assertIsNone(auth.unsign(key, token, "flow", 50))
        self.assertIsNone(auth.unsign(key, token, "session", 101))
        self.assertIsNone(auth.unsign(b"x" * 32, token, "session", 50))
        self.assertIsNone(auth.unsign(key, token[:-2] + "ab", "session", 50))
        self.assertIsNone(auth.unsign(key, "", "session", 50))
        self.assertIsNone(auth.unsign(key, None, "session", 50))


if __name__ == "__main__":
    unittest.main()
