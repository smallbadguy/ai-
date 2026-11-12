"""Google 登录（可选）：只让公司邮箱进来，登录状态放在签名 Cookie 里。

只用标准库。流程是标准的「授权码 + PKCE」：
    任何页面 → /auth/login → Google → /auth/google/callback → 校验 → 写登录 Cookie → 回到原来的页面

环境变量（没设 REPLAY_AUTH=google 就完全不要求登录，本机开发和旧站不受影响）：
    REPLAY_AUTH             设为 google 才启用登录
    REPLAY_PUBLIC_URL       对外地址，如 https://replay-stage.feedmob.it.com；回调地址由它拼出来：
                            <对外地址>/auth/google/callback，要登记到 Google 的授权重定向 URI 里
    REPLAY_ALLOWED_DOMAINS  允许的邮箱域，逗号分隔，默认 feedmob.com
    REPLAY_ALLOWED_EMAILS   另外放行的个别邮箱，逗号分隔（可选）
    REPLAY_SECRETS          凭证目录：google-oauth-client.json（必需，Google 控制台下载的格式）；
                            session.key（没有就自动生成，权限 600）
    REPLAY_SESSION_DAYS     登录有效天数，默认 7
    REPLAY_OAUTH_AUTH_URL / REPLAY_OAUTH_TOKEN_URL   仅供测试，把 Google 换成假的

启用登录但凭证没配好时「关门」：除 /healthz 外一律 503，不会因为没配完就变成公开。
id_token 是服务器直接通过 TLS 从 Google 令牌接口拿到的，按 OpenID Connect 规范可以不再验签，
但会校验发行者、受众、有效期、nonce、邮箱已验证以及邮箱域。
"""
import base64
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
CALLBACK_PATH = "/auth/google/callback"
SESSION_COOKIE = "replay_session"
FLOW_COOKIE = "replay_oauth"
FLOW_SECONDS = 600
CLOCK_SKEW = 60


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key, payload):
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    mac = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(key, token, kind, now):
    """验签并检查用途和有效期；任何不对都返回 None。"""
    try:
        body, mac = (token or "").split(".", 1)
        want = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(mac.encode("ascii"), want.encode("ascii")):
            return None
        data = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("k") != kind:
        return None
    if not isinstance(data.get("exp"), (int, float)) or data["exp"] < now:
        return None
    return data


def cookies(header):
    out = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name] = value
    return out


def safe_next(value):
    """登录后回到哪里：只接受站内路径，防止被利用跳到别的网站。"""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/auth/") or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    return value


def _split(value):
    return frozenset(x.strip().lower() for x in (value or "").split(",") if x.strip())


class Config:
    def __init__(self, enabled=False, error="", public_url="", allowed_domains=frozenset(), allowed_emails=frozenset(),
                 client_id="", client_secret="", session_key=b"", auth_url=GOOGLE_AUTH_URL, token_url=GOOGLE_TOKEN_URL,
                 session_seconds=7 * 86400):
        self.enabled, self.error = enabled, error
        self.public_url = public_url
        self.allowed_domains, self.allowed_emails = allowed_domains, allowed_emails
        self.client_id, self.client_secret = client_id, client_secret
        self.session_key = session_key
        self.auth_url, self.token_url = auth_url, token_url
        self.session_seconds = session_seconds

    @property
    def ready(self):
        return self.enabled and not self.error

    @property
    def redirect_uri(self):
        return self.public_url + CALLBACK_PATH

    @property
    def secure_cookie(self):
        return self.public_url.startswith("https://")


def _read_client(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("web") or data.get("installed") or data
    client_id, client_secret = block.get("client_id"), block.get("client_secret")
    if not (isinstance(client_id, str) and client_id and isinstance(client_secret, str) and client_secret):
        raise ValueError("缺少 client_id 或 client_secret")
    return client_id, client_secret


def _session_key(env, secrets_dir):
    given = env.get("REPLAY_SESSION_KEY")
    if given:
        return hashlib.sha256(given.encode("utf-8")).digest()
    path = secrets_dir / "session.key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return hashlib.sha256(path.read_text(encoding="utf-8").strip().encode("utf-8")).digest()


def load_config(env=None):
    env = os.environ if env is None else env
    if (env.get("REPLAY_AUTH") or "").strip().lower() != "google":
        return Config(enabled=False)
    base = dict(
        allowed_domains=_split(env.get("REPLAY_ALLOWED_DOMAINS") or "feedmob.com"),
        allowed_emails=_split(env.get("REPLAY_ALLOWED_EMAILS")),
        auth_url=env.get("REPLAY_OAUTH_AUTH_URL") or GOOGLE_AUTH_URL,
        token_url=env.get("REPLAY_OAUTH_TOKEN_URL") or GOOGLE_TOKEN_URL,
    )
    try:
        base["session_seconds"] = int(float(env.get("REPLAY_SESSION_DAYS") or 7) * 86400)
    except ValueError:
        return Config(True, "REPLAY_SESSION_DAYS 不是数字", **base)
    public = (env.get("REPLAY_PUBLIC_URL") or "").strip().rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", public):
        return Config(True, "REPLAY_PUBLIC_URL 没设置或格式不对", **base)
    base["public_url"] = public
    secrets_dir = pathlib.Path(env.get("REPLAY_SECRETS") or "")
    if not env.get("REPLAY_SECRETS") or not secrets_dir.is_dir():
        return Config(True, "REPLAY_SECRETS 凭证目录不存在", **base)
    try:
        base["client_id"], base["client_secret"] = _read_client(secrets_dir / "google-oauth-client.json")
    except (OSError, ValueError):
        return Config(True, "缺少或读不懂 google-oauth-client.json", **base)
    try:
        base["session_key"] = _session_key(env, secrets_dir)
    except OSError:
        return Config(True, "无法读取或创建 session.key", **base)
    return Config(True, "", **base)


_STYLE = ("body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070812;color:#f4f2fa;"
          "font:15px/1.6 -apple-system,'PingFang SC',system-ui,sans-serif}main{max-width:460px;padding:28px;margin:20px;"
          "border:1px solid rgba(255,255,255,.1);border-radius:16px;background:#10111d}h1{font-size:18px;margin:0 0 10px}"
          "p{margin:6px 0;color:#aaa7bc}a{color:#c4b5fd}.btn{display:inline-block;margin-top:14px;padding:9px 14px;border-radius:9px;"
          "background:linear-gradient(135deg,#765cf0,#5540d6);color:#fff;text-decoration:none;font-weight:700}")


def page(zh_title, en_title, zh_text, en_text, href=None, zh_link="", en_link="", lang=None):
    """极简提示页（登录出错、已退出、服务未配置、业务版还没生成）。lang 为 zh/en 时只显示一种语言，否则中英并列。"""
    e = html.escape
    both = lang not in ("zh", "en")
    pick = lambda zh, en: f"{e(zh)} · {e(en)}" if both else e(zh if lang == "zh" else en)  # noqa: E731
    text = f"<p>{e(zh_text)}</p><p>{e(en_text)}</p>" if both else f"<p>{e(zh_text if lang == 'zh' else en_text)}</p>"
    action = f'<a class="btn" href="{e(href)}">{pick(zh_link, en_link)}</a>' if href else ""
    return (f'<!doctype html><html lang="{"en" if lang == "en" else "zh-CN"}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{pick(zh_title, en_title)}</title>'
            f'<style>{_STYLE}</style></head><body><main><h1>{pick(zh_title, en_title)}</h1>{text}{action}</main></body></html>')


def unavailable_page():
    return page("登录尚未配置", "Sign-in is not configured", "服务暂不开放，请联系管理员完成登录配置。",
                "The service is closed until sign-in is configured. Please contact the administrator.")


class Auth:
    def __init__(self, cfg, opener=None, clock=None):
        self.cfg = cfg
        self._open = opener or urllib.request.urlopen
        self._now = clock or time.time

    @property
    def enabled(self):
        return self.cfg.enabled

    @property
    def ready(self):
        return self.cfg.ready

    def origin_ok(self, origin):
        return origin.rstrip("/") == self.cfg.public_url

    def _allowed(self, email):
        email = email.lower()
        name, at, domain = email.partition("@")
        if not name or not at or "@" in domain:
            return False
        return email in self.cfg.allowed_emails or domain in self.cfg.allowed_domains

    def _cookie(self, name, value, max_age, path="/"):
        parts = [f"{name}={value}", f"Path={path}", "HttpOnly", "SameSite=Lax", f"Max-Age={int(max_age)}"]
        if self.cfg.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def user(self, cookie_header):
        """当前登录的邮箱；没登录、过期、被改动、或邮箱已不在允许范围都返回 None。"""
        raw = cookies(cookie_header).get(SESSION_COOKIE)
        data = unsign(self.cfg.session_key, raw, "session", self._now()) if raw else None
        email = data.get("email") if data else None
        return email if isinstance(email, str) and self._allowed(email) else None

    def login(self, next_path):
        cfg, now = self.cfg, self._now()
        verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        flow = sign(cfg.session_key, {"k": "flow", "state": state, "nonce": nonce, "verifier": verifier,
                                      "next": safe_next(next_path), "exp": now + FLOW_SECONDS})
        params = {
            "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode("ascii")).digest()), "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if len(cfg.allowed_domains) == 1:
            params["hd"] = next(iter(cfg.allowed_domains))
        location = cfg.auth_url + ("&" if "?" in cfg.auth_url else "?") + urllib.parse.urlencode(params)
        return 303, {"Location": location, "Set-Cookie": [self._cookie(FLOW_COOKIE, flow, FLOW_SECONDS, "/auth/")]}, ""

    def _exchange(self, code, verifier):
        """用授权码换 id_token；失败返回 None（原因只写日志，不含密钥和授权码）。"""
        cfg = self.cfg
        data = urllib.parse.urlencode({
            "code": code, "client_id": cfg.client_id, "client_secret": cfg.client_secret,
            "redirect_uri": cfg.redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        }).encode("ascii")
        request = urllib.request.Request(cfg.token_url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self._open(request, timeout=10) as resp:
                body = json.loads(resp.read(200_000).decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = str(json.loads(e.read(2000).decode("utf-8")).get("error", ""))[:60]
            except (ValueError, OSError):
                pass
            finally:
                e.close()
            sys.stderr.write(f"oauth token exchange rejected: HTTP {e.code} {detail}\n")
            return None
        except (urllib.error.URLError, OSError, ValueError) as e:
            sys.stderr.write(f"oauth token exchange failed: {type(e).__name__}\n")
            return None
        token = body.get("id_token") if isinstance(body, dict) else None
        return token if isinstance(token, str) else None

    def _problem(self, claims, nonce):
        """校验 id_token 的内容；没问题返回 None，有问题返回简短原因。"""
        cfg = self.cfg
        aud = claims.get("aud")
        if claims.get("iss") not in ISSUERS or cfg.client_id not in (aud if isinstance(aud, list) else [aud]):
            return "bad_token"
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < self._now() - CLOCK_SKEW:
            return "expired"
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode("utf-8"), nonce.encode("utf-8")):
            return "bad_token"
        email = claims.get("email")
        if claims.get("email_verified") not in (True, "true") or not isinstance(email, str):
            return "unverified"
        if not self._allowed(email):
            return "denied"
        hd = claims.get("hd")
        if isinstance(hd, str) and hd.lower() not in cfg.allowed_domains and email.lower() not in cfg.allowed_emails:
            return "denied"
        return None

    def callback(self, query, cookie_header):
        """处理 Google 跳回来的请求。返回 (状态码, 响应头, 页面或空)。"""
        cfg, now = self.cfg, self._now()
        clear = self._cookie(FLOW_COOKIE, "", 0, "/auth/")
        retry = ("/auth/login", "重新登录", "Sign in again")

        def fail(status, zh_title, en_title, zh_text, en_text):
            return status, {"Set-Cookie": [clear]}, page(zh_title, en_title, zh_text, en_text, *retry)

        raw = cookies(cookie_header).get(FLOW_COOKIE)
        flow = unsign(cfg.session_key, raw, "flow", now) if raw else None
        one = lambda key: (query.get(key) or [""])[0]  # noqa: E731
        if not flow or not hmac.compare_digest(one("state").encode("utf-8"), str(flow.get("state", "")).encode("utf-8")):
            return fail(400, "登录已过期", "Sign-in expired", "这次登录已经失效或不完整，请重新登录。",
                        "This sign-in is no longer valid or is incomplete. Please sign in again.")
        if one("error") or not one("code"):
            return fail(403, "没有完成登录", "Sign-in was not completed", "Google 没有完成这次登录。", "Google did not complete this sign-in.")
        token = self._exchange(one("code"), flow["verifier"])
        claims = None
        if token:
            try:
                claims = json.loads(_unb64(token.split(".")[1]).decode("utf-8"))
            except (IndexError, ValueError, UnicodeError):
                claims = None
        if not isinstance(claims, dict):
            return fail(502, "登录服务暂时不可用", "Sign-in is temporarily unavailable", "暂时无法向 Google 确认身份，请稍后重试。",
                        "We could not confirm your identity with Google. Please try again shortly.")
        problem = self._problem(claims, str(flow.get("nonce", "")))
        if problem == "denied":
            who = str(claims.get("email", ""))[:120]
            allowed = ", ".join("@" + d for d in sorted(cfg.allowed_domains))
            return fail(403, "没有访问权限", "Access denied", f"账号 {who} 没有访问权限，请用公司邮箱（{allowed}）登录。",
                        f"The account {who} does not have access. Please sign in with a company email ({allowed}).")
        if problem:
            return fail(403, "无法确认身份", "Could not verify identity", "没能确认你的身份，请重新登录。",
                        "We could not verify your identity. Please sign in again.")
        session = sign(cfg.session_key, {"k": "session", "email": claims["email"].lower(), "exp": now + cfg.session_seconds})
        return 303, {"Location": safe_next(flow.get("next")),
                     "Set-Cookie": [self._cookie(SESSION_COOKIE, session, cfg.session_seconds), clear]}, ""

    def logout(self):
        return 200, {"Set-Cookie": [self._cookie(SESSION_COOKIE, "", 0)]}, page(
            "已退出", "Signed out", "你已退出登录。", "You have been signed out.", "/auth/login", "重新登录", "Sign in again")
