"""最小只读 MCP 客户端（Streamable HTTP）。

- 只用 initialize / tools/list / tools/call 三个方法；不做任何写类操作的封装。
- 凭证不写在代码里：
  - femini：环境变量 FEMINI_MCP_API_KEY（由 secrets/femini.env 提供，权限 600）；
  - brain：OAuth 令牌取自 secrets/mcp-credentials.json（权限 600）；过期时用 refresh_token 续期并写回。
- 令牌永远不打印、不写日志。
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECRETS = pathlib.Path(os.environ.get("REPLAY_SECRETS", ROOT / "secrets"))
# 刷新令牌续期会写回这个文件；多个环境（正式/stage）必须共用同一份，否则轮换后互相作废
CREDS = pathlib.Path(os.environ.get("REPLAY_MCP_CREDENTIALS", SECRETS / "mcp-credentials.json"))
FEMINI_ENV = SECRETS / "femini.env"
BRAIN = "https://brain.feedmob.it.com"
PROTOCOL = "2025-06-18"


def _load_femini_env():
    if FEMINI_ENV.exists() and "FEMINI_MCP_API_KEY" not in os.environ:
        for line in FEMINI_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("FEMINI_MCP_API_KEY="):
                os.environ["FEMINI_MCP_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")


def _creds_entry(url_prefix):
    data = json.loads(CREDS.read_text(encoding="utf-8"))
    for key, entry in data["servers"].items():
        if entry.get("url", "").startswith(url_prefix):
            return data, key, entry
    raise RuntimeError(f"凭证文件里找不到 {url_prefix}")


def _save_creds(data):
    tmp = CREDS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CREDS)


def _refresh_brain_token(stale):
    """用 refresh_token 换新的访问令牌并写回。几个任务同时发现过期时只换一次：刷新令牌是轮换的，同一个用两次后一次会失败，
    所以先拿文件锁，再重读凭证——令牌已经不是自己手里那个（别人刚换过）就直接用新的。"""
    import fcntl
    with open(CREDS.with_name(CREDS.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data, key, entry = _creds_entry(BRAIN)
        if entry["tokens"].get("access_token") != stale:
            return entry["tokens"]["access_token"]
        return _exchange_brain_token(data, key, entry)


def _exchange_brain_token(data, key, entry):
    meta = json.loads(urllib.request.urlopen(BRAIN + "/.well-known/oauth-authorization-server", timeout=30).read())
    client = entry["clientInformation"]
    form = {
        "grant_type": "refresh_token",
        "refresh_token": entry["tokens"]["refresh_token"],
        "client_id": client["client_id"],
    }
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    req = urllib.request.Request(meta["token_endpoint"], data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
    entry["tokens"].update({k: v for k, v in tok.items() if k in ("access_token", "refresh_token", "expires_in", "token_type")})
    entry["updatedAt"] = int(time.time() * 1000)
    data["servers"][key] = entry
    _save_creds(data)
    return entry["tokens"]["access_token"]


def sse_messages(raw):
    """SSE 正文 → JSON 消息列表。只按 \n 切行（splitlines 会把正文里的 U+2028 等也当换行，切断 JSON），
    同一事件的多行 data: 按规范用 \n 拼回。"""
    msgs, buf = [], []
    for line in raw.replace("\r\n", "\n").split("\n") + [""]:
        if line.startswith("data:"):
            buf.append(line[6:] if line.startswith("data: ") else line[5:])
        elif line == "" and buf:
            body = "\n".join(buf)
            buf = []
            if body.strip():
                msgs.append(json.loads(body))
    return msgs


class McpHttp:
    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.session = None
        self.id = 0

    def _post(self, payload):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.session = r.headers.get("Mcp-Session-Id") or self.session
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            msgs = sse_messages(raw)
            return next((m for m in msgs if m.get("id") == payload.get("id")), msgs[-1] if msgs else {})
        return json.loads(raw) if raw.strip() else {}

    def _rpc(self, method, params=None):
        self.id += 1
        msg = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            msg["params"] = params
        out = self._post(msg)
        if "error" in out:
            raise RuntimeError(f"{method} 失败：{out['error'].get('message', '')[:200]}")
        return out.get("result", {})

    def initialize(self):
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "replay-collector", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def tools(self):
        return self._rpc("tools/list").get("tools", [])

    def call(self, name, args):
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        if res.get("isError"):
            raise RuntimeError(f"{name} 返回错误：{' '.join(texts)[:200]}")
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined


def femini():
    _load_femini_env()
    key = os.environ.get("FEMINI_MCP_API_KEY")
    if not key:
        raise RuntimeError("缺少 FEMINI_MCP_API_KEY（secrets/femini.env）")
    return McpHttp("https://assistant.feedmob.ai/mcp", {"Authorization": f"Bearer {key}"}).initialize()


def brain():
    _, _, entry = _creds_entry(BRAIN)
    token = entry["tokens"]["access_token"]
    try:
        return McpHttp(BRAIN + "/mcp", {"Authorization": f"Bearer {token}"}).initialize()
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        token = _refresh_brain_token(token)
        return McpHttp(BRAIN + "/mcp", {"Authorization": f"Bearer {token}"}).initialize()
