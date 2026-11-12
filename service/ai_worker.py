"""我们自己的处理方：和外部 agent 一模一样——各自一把钥匙，走同一套接口（/agent-api/v1）领题、取题、续期、交答案，
不直接碰队列，没有捷径。区别只是作答用公司网关的模型（默认 minimax-m3.1-flash）。

- 处理方 internal-1 … internal-N（N = AI_WORKER_SLOTS，默认 8；2026-10-31 凌晨用户定 2 个，当天下午改为 8 个），每个同时只做一道题，各自一个线程。
- 一次能读 AI_WORKER_CONTEXT（默认 1000000）token，登记时写进队列，派题按它判断。
- 登记和发钥匙在启动时做一次（和管理员登记外部处理方一样）；钥匙存在 secrets/internal-agents.json（600），没有或失效就换新的。
- 做题期间每 5 分钟续期一次；答案不合格（422）把原因发回给模型让它改（最多改 3 轮）再交；题目已完成或作废（409）就丢掉。
- 网关限流或额度用完：题目退回队列并注明不算这道题失败（retryable），歇 10 分钟再领；别的错误退回（算一次失败），歇 1 分钟。
- 接口暂时连不上（比如网页服务在重启）：歇一会儿再试；已经做好的答案会多试几次交上去，不白做。
- 收到停止信号时把手上的题退回队列（不算失败）。
用法：在 app 目录下 python3 -u service/ai_worker.py（stage 上由 systemd 的 ai-2734-replay-stage-worker 服务常驻）。
"""
import json
import os
import pathlib
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from replay import jobqueue as Q  # noqa: E402
from replay import progress as P  # noqa: E402

SLOTS = int(os.environ.get("AI_WORKER_SLOTS", "8"))
MODEL = os.environ.get("AI_WORKER_MODEL", "minimax-m3.1-flash")
CONTEXT = int(os.environ.get("AI_WORKER_CONTEXT", "1000000"))
API = os.environ.get("AI_WORKER_API", "http://127.0.0.1:8792/agent-api/v1").rstrip("/")
IDLE_SECONDS = 20
HEARTBEAT_SECONDS = 300
LIMITED_PAUSE = 600
ERROR_PAUSE = 60
FIX_ROUNDS = 3
SUBMIT_TRIES = 10   # 交答案时接口连不上，隔 30 秒再试，最多这么多次

STOP = threading.Event()
CURRENT = {}  # 处理方名字 → (题号, 领取号, 钥匙)


def log(*parts):
    """一行一次写完：几个处理方线程同时写，日志行不会串在一起。"""
    line = " ".join([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *map(str, parts)])
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def limited(err):
    s = str(err)
    return "429" in s or "Too Many" in s or "rate limit" in s.lower() or "quota" in s.lower() or "额度" in s


class ApiDown(Exception):
    """接口连不上（不是接口回了错误码）。"""


def api(method, path, token, body=None, timeout=120):
    """调一次处理方接口 → (HTTP 状态码, 回应 JSON)。连不上抛 ApiDown。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiDown(f"{type(e).__name__}: {str(e)[:120]}")


def tokens_file():
    return P.SECRETS / "internal-agents.json"


def ensure_agents(names):
    """登记（或更新）自家处理方，拿到每个的钥匙；多出来的旧处理方停用。→ {名字: 钥匙}"""
    path = tokens_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    out = {}
    for n in names:
        Q.ensure_internal(n, CONTEXT, MODEL)
        tok = saved.get(n)
        if not tok or Q.agent_for_token(tok) != (n, True):
            tok = Q.rotate_token(n)
        out[n] = tok
    if out != saved:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    for a in Q.list_agents():
        if a["internal"] and a["name"] not in names and a["enabled"]:
            Q.update_agent(a["name"], enabled=False)
    return out


def keep_alive(name, token, jid, lid, done):
    while not done.wait(HEARTBEAT_SECONDS):
        try:
            code, res = api("POST", f"/jobs/{jid}/heartbeat", token, {"lease_id": lid})
            if code == 200 and res.get("continue") is False:
                log(name, jid, "题目已结束，停止续期")
                return
        except ApiDown as e:
            log(name, jid, "续期时接口连不上", e)


def chat(messages):
    return P.chat(messages, model=MODEL, plain=True)


def submit(name, token, jid, lid, text, usage):
    """交答案；接口连不上就隔一会儿再试，做好的答案不白做（超时后交的也算数，先到先得）。"""
    for i in range(SUBMIT_TRIES):
        try:
            return api("POST", f"/jobs/{jid}/result", token, {"lease_id": lid, "output": text, "model": MODEL, "usage": usage})
        except ApiDown as e:
            log(name, jid, f"交答案时接口连不上（第 {i + 1} 次）", e)
            if STOP.wait(30):
                break
    raise ApiDown("交答案一直连不上")


def solve(name, token, job, ask=None):
    """做一道题：取题 → 调模型 → 交答案；不合格就让模型改了再交。→ 结果说明"""
    ask = ask or chat
    jid, lid = job["id"], job["lease_id"]
    code, got = api("GET", f"/jobs/{jid}/input?lease={lid}", token)
    if code != 200:
        log(name, jid, "取题失败", code, got.get("reason"), got.get("message"))
        if code != 409:  # 题目还在：让出来（不算这道题失败）
            api("POST", f"/jobs/{jid}/release", token, {"lease_id": lid, "reason": f"取题失败 {code}", "retryable": True})
        return "skipped"
    done = threading.Event()
    threading.Thread(target=keep_alive, args=(name, token, jid, lid, done), daemon=True).start()
    try:
        messages, convo = got["messages"], list(got["messages"])
        for _ in range(FIX_ROUNDS + 1):
            text, usage = ask(convo)
            code, res = submit(name, token, jid, lid, text, usage)
            if code == 200:
                log(name, jid, "交回，已收下", "（超时后交回）" if res.get("late") else "")
                return "accepted"
            if code != 422:
                log(name, jid, "不用交了", code, res.get("reason"))
                return "dropped"
            convo = list(messages) + [{"role": "assistant", "content": text},
                                      {"role": "user", "content": P.FIX_JSON.format(error=res.get("error"), near=res.get("near") or "")}]
        api("POST", f"/jobs/{jid}/release", token, {"lease_id": lid, "reason": f"改了 {FIX_ROUNDS} 轮格式仍不合格"})
        return "released"
    finally:
        done.set()


def loop(name, token):
    while not STOP.is_set():
        try:
            code, got = api("POST", "/claim", token, {})
        except ApiDown as e:
            log(name, "接口连不上，稍后再试", e)
            STOP.wait(ERROR_PAUSE)
            continue
        if code != 200:
            log(name, "领题被拒", code, got.get("reason"), got.get("message"))
            STOP.wait(ERROR_PAUSE)
            continue
        job = got.get("job")
        if not job:
            STOP.wait(60 if got.get("reason") == "paused" else IDLE_SECONDS)
            continue
        jid, lid = job["id"], job["lease_id"]
        CURRENT[name] = (jid, lid, token)
        log(name, "领到", jid, job["kind"], f"约 {job['input_tokens']} token", "（之前领了没交的）" if got.get("again") else "")
        pause = 0
        try:
            solve(name, token, job)
        except ApiDown as e:
            log(name, jid, "接口连不上", e)
            pause = ERROR_PAUSE
        except Exception as e:
            hit = limited(e)
            log(name, jid, "出错，退回队列", "（网关限流/额度）" if hit else "", type(e).__name__, str(e)[:160])
            try:
                api("POST", f"/jobs/{jid}/release", token,
                    {"lease_id": lid, "reason": ("网关限流：" if hit else "出错：") + str(e)[:150], "retryable": hit})
            except Exception:
                pass
            pause = LIMITED_PAUSE if hit else ERROR_PAUSE
        finally:
            CURRENT.pop(name, None)
        if pause:
            STOP.wait(pause)


def on_stop(signum, frame):
    STOP.set()
    for name, (jid, lid, token) in list(CURRENT.items()):
        try:
            api("POST", f"/jobs/{jid}/release", token, {"lease_id": lid, "reason": "自家处理方重启", "retryable": True}, timeout=5)
        except Exception:
            pass
    log("收到停止信号，手上的题已退回队列")
    os._exit(0)


def main():
    names = [f"internal-{i}" for i in range(1, SLOTS + 1)]
    tokens = ensure_agents(names)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    log(f"自家处理方启动：{', '.join(names)}，模型 {MODEL}，一次能读 {CONTEXT} token，接口 {API}")
    for n in names:
        threading.Thread(target=loop, args=(n, tokens[n]), daemon=True).start()
    while not STOP.is_set():
        STOP.wait(60)


if __name__ == "__main__":
    main()
