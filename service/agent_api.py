"""给外部 agent 用的接口：/agent-api/v1/…，不走 Google 登录，每个处理方用自己的钥匙（Authorization: Bearer rq_…）。

规则和状态见 replay/jobqueue.py 开头；接入注册说明是 docs/agent接入注册.md，GET /agent-api/v1/protocol 取到的就是它（不用钥匙）。
所有回应都是 JSON：ok（成功与否）、message（给人看的说明）、reason（出错时给程序看的原因）以及各接口自己的字段。
"""
import json
import pathlib
import re

from replay import jobqueue as Q

MAX_BODY = 8 * 1024 * 1024   # 交答案的请求体上限
JOB_ROUTE = re.compile(r"/agent-api/v1/jobs/([A-Z0-9]{6,32})/(input|heartbeat|result|release)")

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "agent接入注册.md"


def protocol_text():
    """接入注册说明（给外部 agent 和它的维护人看）：docs/agent接入注册.md，每次现读。"""
    try:
        return DOC.read_text(encoding="utf-8")
    except OSError:
        return "# 外包任务接入说明\n\n说明文档暂时读不到，请联系管理员。\n"


def _json(h, status, payload):
    h._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
    return True


def _bearer(value):
    m = re.fullmatch(r"\s*Bearer\s+(\S+)\s*", value or "")
    return m.group(1) if m else None


class _TooBig(Exception):
    pass


def _read_body(h):
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        raise ValueError("Content-Length 不对")
    if length > MAX_BODY:
        raise _TooBig()
    raw = h.rfile.read(length) if length > 0 else b""
    if not raw.strip():
        return {}
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("请求体要是一个 JSON 对象")
    return body


def handle(h, method, route, query):
    """处理 /agent-api/ 开头的请求；h 是 http.server 的请求处理对象。"""
    if route == "/agent-api/v1/protocol":
        if method != "GET":
            return _json(h, 405, {"ok": False, "reason": "method", "message": "这个地址只能 GET"})
        h._send(200, protocol_text(), "text/markdown; charset=utf-8")
        return True
    try:
        who = Q.agent_for_token(_bearer(h.headers.get("Authorization")))
    except Exception as e:  # 队列库一时读不了也回 JSON
        return _json(h, 503, {"ok": False, "reason": "busy", "message": f"服务器忙：{type(e).__name__}，请稍后再试"})
    if not who:
        return _json(h, 401, {"ok": False, "reason": "unauthorized", "message": "钥匙不对：请求头要带 Authorization: Bearer <你的钥匙>"})
    name, active = who
    if not active:
        return _json(h, 403, {"ok": False, "reason": "disabled", "message": f"处理方 {name} 已停用"})
    body = {}
    if method == "POST":
        try:
            body = _read_body(h)
        except _TooBig:
            return _json(h, 413, {"ok": False, "reason": "too_large", "message": f"请求体超过 {MAX_BODY // 1024 // 1024} MB"})
        except (ValueError, UnicodeDecodeError) as e:
            return _json(h, 400, {"ok": False, "reason": "bad_json", "message": f"请求体不是合法的 JSON：{str(e)[:120]}"})
    try:
        if route == "/agent-api/v1/claim":
            if method != "POST":
                return _json(h, 405, {"ok": False, "reason": "method", "message": "领题要用 POST"})
            res = Q.claim(name)
        elif route == "/agent-api/v1/me":
            res = Q.agent_status(name)
        else:
            m = JOB_ROUTE.fullmatch(route)
            if not m:
                return _json(h, 404, {"ok": False, "reason": "not_found", "message": "没有这个接口，说明见 /agent-api/v1/protocol"})
            jid, action = m.groups()
            if (action == "input") != (method == "GET"):
                return _json(h, 405, {"ok": False, "reason": "method", "message": "取题用 GET，其余用 POST"})
            if action == "input":
                res = Q.get_input(jid, (query.get("lease") or [""])[0], name)
            elif action == "heartbeat":
                res = Q.heartbeat(jid, str(body.get("lease_id") or ""), name)
            elif action == "result":
                res = Q.submit(jid, str(body.get("lease_id") or ""), name, body.get("output"),
                               model=body.get("model"), usage=body.get("usage"))
            else:
                res = Q.release(jid, str(body.get("lease_id") or ""), name, str(body.get("reason") or ""),
                                count_failure=body.get("retryable") is not True)  # retryable：限流、重启这类，不算这道题失败
    except Exception as e:  # 队列出错也要回 JSON，不让处理方拿到半截页面
        return _json(h, 500, {"ok": False, "reason": "server_error", "message": f"服务器出错：{type(e).__name__}，请稍后再试"})
    status = res.pop("status", 200)
    res.setdefault("ok", status < 400)
    return _json(h, status, res)
