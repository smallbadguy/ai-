"""AI 任务外包队列：流水线里每一次 AI 调用是一道「题」，外部 agent（每分钟定时来领）或我们自己的处理方领题、作答、交回。

为什么按「一次 AI 调用」外包：题目自带全部材料（说明 + 原始记录），处理方不需要我们的代码、数据或任何系统权限，
读完交回一个 JSON 就行。交回的答案照旧经过流水线的程序核对（原话逐字定位、说话人和时间取原始记录），
所以不管谁做的、用什么模型，编造的话都上不了页面。

状态（jobs.state）
- queued     排队中：等处理方来领
- leased     处理中：某个处理方领走了，截止时间之前要交回（可续期）
- done       已完成：收到了第一个合格的答案
- failed     失败：反复超时或不合格，连兜底也没做成
- cancelled  已作废：没有流水线在等这道题了

规则
1. 一个处理方同时只拿一道题；没交之前再来领，拿到的还是同一道。
2. 领题时给截止时间（40 分钟，2026-10-31 用户定），可续期，一次领取最长 60 分钟。过了截止时间没交 = 失联：题目重新排队，换人做。
3. 一道题只认第一个合格的答案。失联后才交回的旧答案，只要题目还没完成（或已判失败）也收下（先到先得），
   正在做同一道题的处理方下次续期或交答案时被告知停止；题目已经完成的，迟到的答案只记一笔，不用。
4. 答案不合格（不是 JSON 对象、缺少题目要求的字段）当场退回原因，题目没完成之前可以改了再交；一次领取最多交 7 次。
5. 同一道题失败（超时 / 交满 7 次仍不合格 / 放弃）满 3 次，只交给我们自己的处理方（兜底）；满 6 次算失败。
6. 连续超时 3 次的处理方暂停领题 30 分钟；交上一个合格答案就清零。
7. 一模一样的题（内容逐字相同）只做一次：做过的直接复用答案，排着或做着的就等它。
8. 按大小派题、不切题：处理方注册时写明一次能读多少 token，读不下的题不派给它；大题优先派给读得下大题的处理方。
   我们自己的处理方也只是普通处理方（用 Flash），和外部一样领题、交答案，没有捷径；只有失败满 3 次的题只交给它。
9. 时间一律 UTC 记下：出题、原料取数、原料截至（最新一条记录）、领题、取题、续期、交回。
10. 优先级：首页手动刷新、每天的定时刷新出的题是 0，最先派；批量出的题是 1、2、3…（AI_QUEUE_PRIORITY=<活动的先后>，
   low 等于 1），数字越小越先派——先开始的活动先做完，页面一个个出来，不会全部拖到最后。一模一样的题重复出时，按更靠前的算。

存储：SQLite（state/queue.db，可用 REPLAY_QUEUE_DB 换位置），流水线进程、网页服务、自家处理方共用；
每次改动都在一个写事务里做（BEGIN IMMEDIATE），多个处理方同时来领也只会有一个拿到。
开关：AI_EXECUTOR=queue（环境变量或 secrets/ai.env）时，流水线的 AI 调用改走队列；不设就照旧直接调网关。
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import sqlite3
import threading
import time
import zlib

from .util import STATE, camp_dir, read_json

DB_PATH = pathlib.Path(os.environ.get("REPLAY_QUEUE_DB", str(STATE / "queue.db")))

LEASE_SECONDS = 40 * 60          # 领一次题的截止时间（用户定 40 分钟）；续期一次再给这么长
MAX_LEASE_SECONDS = 60 * 60      # 一次领取最长多久（续期也不能超过）
MAX_SUBMITS = 7                  # 一次领取最多交几次（不合格可以改了再交）
FAILS_TO_INTERNAL = 3            # 失败满几次只交给自家处理方
FAILS_TO_FAILED = 6              # 失败满几次算失败
PAUSE_AFTER_TIMEOUTS = 3         # 处理方连续超时几次就暂停领题
PAUSE_SECONDS = 30 * 60
ORPHAN_SECONDS = 15 * 60         # 排队的题超过这么久没有流水线来取结果 → 作废
UNREADABLE_SECONDS = 10 * 60     # 排队超过这么久、所有启用的处理方都读不下 → 判失败（不让流水线干等 6 小时）
OUTPUT_RESERVE_TOKENS = 40_000   # 作答要留的额度（思考 + 答案）
CONTEXT_MARGIN = 0.85            # 处理方自己的说明、工具也占上下文，只按注册值的 85% 算
CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
WAIT_SECONDS = 6 * 3600          # 流水线最多等一道题多久
WAIT_POLL = 5
WAITER_TOUCH = 60                # 流水线每隔这么久报一次「我还在等」
PRUNE_SECONDS = 3600             # 多久清理一次旧题目原文
KEEP_INPUT_DAYS = 3              # 已结束的题，原文留几天（答案一直留着，同样的题可以复用）

STATES = ("queued", "leased", "done", "failed", "cancelled")
STATE_LABELS = {"queued": "排队中", "leased": "处理中", "done": "已完成", "failed": "失败", "cancelled": "已作废"}
KIND_LABELS = {
    "progress_update": "进行中事项 · 更新", "progress_digest": "进行中事项 · 摘要", "progress_combine": "进行中事项 · 合成",
    "progress_scope": "进行中事项 · 归属", "business_pick": "业务版 · 挑原话", "business_window": "业务版 · 分段汇总",
    "business_meta": "业务版 · 概要", "business_zh": "业务版 · 补译", "change_link": "后台改动 · 找讨论",
    "json_fix": "格式修正", "other": "其他",
}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
ID_RE = re.compile(r"[JL][0-9A-F]{8,24}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY, token_hash TEXT UNIQUE, model TEXT NOT NULL DEFAULT '', context_tokens INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '', internal INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, last_seen_at TEXT, paused_until TEXT, consecutive_timeouts INTEGER NOT NULL DEFAULT 0,
  done_count INTEGER NOT NULL DEFAULT 0, timeout_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0,
  late_count INTEGER NOT NULL DEFAULT 0, released_count INTEGER NOT NULL DEFAULT 0, busy_seconds INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inputs(key TEXT PRIMARY KEY, body BLOB NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, key TEXT NOT NULL, campaign TEXT, step TEXT, kind TEXT NOT NULL, expect_key TEXT,
  input_chars INTEGER NOT NULL, input_tokens INTEGER NOT NULL, data_fetched_at TEXT, data_as_of TEXT, expect_type TEXT,
  priority INTEGER NOT NULL DEFAULT 0, vendor_id TEXT,
  state TEXT NOT NULL, created_at TEXT NOT NULL, queued_at TEXT NOT NULL, updated_at TEXT NOT NULL, waiter_seen_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, internal_only INTEGER NOT NULL DEFAULT 0,
  lease_id TEXT, lease_agent TEXT, lease_expires_at TEXT,
  result TEXT, result_usage TEXT, result_agent TEXT, result_model TEXT, result_lease TEXT, done_at TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(key);
CREATE INDEX IF NOT EXISTS jobs_campaign ON jobs(campaign, created_at);
CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at);
CREATE TABLE IF NOT EXISTS leases(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, agent TEXT NOT NULL, claimed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  input_fetched_at TEXT, last_heartbeat_at TEXT, ended_at TEXT, end_reason TEXT, submits INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS leases_agent ON leases(agent, ended_at);
CREATE INDEX IF NOT EXISTS leases_job ON leases(job_id);
CREATE INDEX IF NOT EXISTS leases_open ON leases(ended_at, expires_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT, lease_id TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


# ───────────────────────── 基础 ─────────────────────────
def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) if s else None


def _new_id(prefix):
    return f"{prefix}{int(time.time()):X}{secrets.token_hex(3).upper()}"


_READY = set()


def _connect():
    path = pathlib.Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    if str(path) not in _READY:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("expect_type", "TEXT"), ("priority", "INTEGER NOT NULL DEFAULT 0"), ("vendor_id", "TEXT")):  # 早先建的库补列
            if col not in have:
                try:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as e:  # 别的进程同时补上了
                    if "duplicate column" not in str(e):
                        raise
        _READY.add(str(path))
    return con


@contextlib.contextmanager
def _tx():
    """一个写事务：同一时刻只有一个进程/线程在改队列，领题、交答案都不会撞车。"""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def _event(con, job_id, kind, agent=None, lease=None, detail=None, at=None):
    con.execute("INSERT INTO events(job_id, at, kind, agent, lease_id, detail) VALUES (?,?,?,?,?,?)",
                (job_id, at or _iso(_now()), kind, agent, lease, detail))


def estimate_tokens(text):
    """估算 token：中日韩文字一个字算一个 token，其余每 3 个字符算一个（宁可估多，不派给读不下的处理方）。"""
    cjk = len(CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 3 + 1


def fits(tokens, context):
    """一道题（估算 tokens）放不放得进一次能读 context token 的处理方。"""
    return tokens + OUTPUT_RESERVE_TOKENS <= int(context * CONTEXT_MARGIN)


# ───────────────────────── 处理方（agent）登记 ─────────────────────────
def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_name(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("名字只能用英文字母、数字和 _ . -，1~40 个字符，以字母或数字开头")


def _check_context(context_tokens):
    ctx = int(context_tokens)
    if not 8_000 <= ctx <= 10_000_000:
        raise ValueError("一次能读的 token 数要在 8000 到 10000000 之间")
    return ctx


def add_agent(name, context_tokens, model="", note="", internal=False):
    """登记一个处理方。外部处理方返回它的钥匙（只在这里出现一次，库里只存摘要）；自家处理方没有钥匙。"""
    _check_name(name)
    ctx = _check_context(context_tokens)
    token = None if internal else "rq_" + secrets.token_urlsafe(32)
    with _tx() as con:
        if con.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise ValueError(f"处理方 {name} 已经登记过了")
        con.execute("INSERT INTO agents(name, token_hash, model, context_tokens, note, internal, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)",
                    (name, _hash(token) if token else None, (model or "")[:80], ctx, (note or "")[:200], 1 if internal else 0, _iso(_now())))
    return token


def ensure_internal(name, context_tokens, model):
    """自家处理方：没有就登记，有就更新模型和上下文、恢复启用。钥匙用 rotate_token 发（它和外部处理方一样走接口）。"""
    _check_name(name)
    ctx = _check_context(context_tokens)
    with _tx() as con:
        row = con.execute("SELECT internal FROM agents WHERE name=?", (name,)).fetchone()
        if row and not row["internal"]:
            raise ValueError(f"{name} 是外部处理方的名字，换一个")
        if row:
            con.execute("UPDATE agents SET model=?, context_tokens=?, enabled=1, note='自家处理方' WHERE name=?", (model[:80], ctx, name))
        else:
            con.execute("INSERT INTO agents(name, token_hash, model, context_tokens, note, internal, enabled, created_at) VALUES (?,NULL,?,?,?,1,1,?)",
                        (name, model[:80], ctx, "自家处理方", _iso(_now())))


def update_agent(name, enabled=None, context_tokens=None, model=None, note=None, unpause=False):
    with _tx() as con:
        if not con.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise ValueError(f"没有处理方 {name}")
        if enabled is not None:
            con.execute("UPDATE agents SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        if context_tokens is not None:
            con.execute("UPDATE agents SET context_tokens=? WHERE name=?", (_check_context(context_tokens), name))
        if model is not None:
            con.execute("UPDATE agents SET model=? WHERE name=?", (model[:80], name))
        if note is not None:
            con.execute("UPDATE agents SET note=? WHERE name=?", (note[:200], name))
        if unpause:
            con.execute("UPDATE agents SET paused_until=NULL, consecutive_timeouts=0 WHERE name=?", (name,))


def rotate_token(name):
    """换一把新钥匙，旧的立即失效。"""
    token = "rq_" + secrets.token_urlsafe(32)
    with _tx() as con:
        row = con.execute("SELECT internal FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"没有处理方 {name}")
        con.execute("UPDATE agents SET token_hash=? WHERE name=?", (_hash(token), name))
    return token


def agent_for_token(token):
    """钥匙 → (名字, 是否启用)；钥匙不对返回 None。"""
    if not token or not token.startswith("rq_") or len(token) > 200:
        return None
    con = _connect()
    try:
        row = con.execute("SELECT name, enabled FROM agents WHERE token_hash=?", (_hash(token),)).fetchone()
    finally:
        con.close()
    return (row["name"], bool(row["enabled"])) if row else None


def list_agents():
    con = _connect()
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM agents ORDER BY internal, name")]
        held = {r["agent"]: dict(r) for r in con.execute("SELECT agent, job_id, claimed_at, expires_at FROM leases WHERE ended_at IS NULL")}
    finally:
        con.close()
    for r in rows:
        r.pop("token_hash", None)
        r["holding"] = held.get(r["name"])
    return rows


def agent_status(name):
    me = next((a for a in list_agents() if a["name"] == name), None)
    if not me:
        return {"status": 404, "reason": "unknown_agent", "message": "没有这个处理方"}
    return {"status": 200, "agent": me, "message": "处理方信息"}


# ───────────────────────── 出题、等答案（流水线这一侧） ─────────────────────────
_OUT_KEY = re.compile(r'Output one JSON object and nothing else:\s*\{\s*"([A-Za-z_]+)"\s*:\s*(\[\s*\{|\[|str|\{)?')
TYPE_LABELS = {"list_of_objects": "一个列表，每一项是一个对象 {...}", "list": "一个列表 [...]", "string": "一段文字", "object": "一个对象 {...}"}


def _shape(s):
    s = re.sub(r"\s+", "", s or "")
    if s.startswith("[{"):
        return "list_of_objects"
    return {"[": "list", "s": "string", "{": "object"}.get(s[:1])


def classify(messages):
    """从题目的说明里认出这是哪一步的题、答案顶层必须有的字段和它的样子（照说明里的输出模板）。→ (kind, expect_key, expect_type)"""
    from . import business, changes, progress
    system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
    user = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
    m = _OUT_KEY.search(system)
    expect = m.group(1) if m else None
    etype = _shape(m.group(2)) if m else None
    if any(x.get("role") == "assistant" for x in messages):
        return "json_fix", expect, etype

    def starts(tpl):
        head = tpl.split("{", 1)[0][:80]
        return bool(head.strip()) and system.startswith(head)
    if starts(progress.DIGEST_SYSTEM):
        kind = "progress_digest"
    elif starts(progress.RESCOPE_SYSTEM):
        kind = "progress_scope"
    elif starts(progress.SYSTEM):
        kind = "progress_combine" if "New events (chronological)" in user else "progress_update"
    elif starts(business.PICK_SYSTEM):
        kind = "business_pick"
    elif starts(business.WINDOW_SYSTEM):
        kind = "business_window"
    elif starts(business.META_SYSTEM):
        kind = "business_meta"
    elif starts(business.ZH_FIX_SYSTEM):
        kind = "business_zh"
    elif starts(changes.LINK_SYSTEM):
        kind = "change_link"
    else:
        kind = "other"
    return kind, expect, etype


def enqueue(messages, campaign=None, step=None, data_fetched_at=None, data_as_of=None, priority=0, vendor=None, namespace=None):
    """出一道题，返回题号。一模一样的题已经排着、做着或做完的，直接返回那道题（不重复外包）。
    priority：0 普通（首页刷新、每天的刷新）；批量用 1、2、3…，数字越小越先派。"""
    priority = max(0, int(priority or 0))
    from . import scope
    vendor = scope.valid_vendor(vendor) if vendor is not None else scope.vendor_for(campaign)
    body = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    identity = json.dumps([str(campaign), vendor, namespace, messages], ensure_ascii=False, sort_keys=True) if vendor or namespace else body
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    kind, expect, etype = classify(messages)
    text = "".join(str(m.get("content") or "") for m in messages)
    chars, tokens = len(text), estimate_tokens(text)
    now = _iso(_now())
    with _tx() as con:
        row = con.execute("SELECT id, state, priority FROM jobs WHERE key=? AND state IN ('queued','leased','done') ORDER BY created_at DESC LIMIT 1",
                          (key,)).fetchone()
        if row:
            con.execute("UPDATE jobs SET waiter_seen_at=?, priority=MIN(priority, ?) WHERE id=?", (now, priority, row["id"]))
            return row["id"]
        jid = _new_id("J")
        con.execute("INSERT OR REPLACE INTO inputs(key, body, created_at) VALUES (?,?,?)", (key, zlib.compress(body.encode("utf-8"), 6), now))
        con.execute("""INSERT INTO jobs(id, key, campaign, step, kind, expect_key, expect_type, input_chars, input_tokens, data_fetched_at,
                       data_as_of, priority, vendor_id, state, created_at, queued_at, updated_at, waiter_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)""",
                    (jid, key, campaign, step, kind, expect, etype, chars, tokens, data_fetched_at, data_as_of, priority, vendor, now, now, now, now))
        _event(con, jid, "created", detail=f"{KIND_LABELS.get(kind, kind)}，{chars} 字，约 {tokens} token", at=now)
    return jid


def wait(jid, timeout=None, poll=None):
    """等一道题的答案。→ (答案原文, 用量)。失败、作废或等太久就报错（流水线这一步算失败，页面保留上次成功的）。"""
    timeout = WAIT_SECONDS if timeout is None else timeout
    poll = WAIT_POLL if poll is None else poll
    start, touched = time.time(), 0.0
    while True:
        con = _connect()
        try:
            row = con.execute("SELECT state, result, result_usage, error FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is not None and row["state"] in ("queued", "leased") and time.time() - touched >= WAITER_TOUCH:
                con.execute("UPDATE jobs SET waiter_seen_at=? WHERE id=?", (_iso(_now()), jid))
                touched = time.time()
        finally:
            con.close()
        if row is None:
            raise RuntimeError(f"AI 任务 {jid} 不存在")
        if row["state"] == "done":
            return row["result"] or "", json.loads(row["result_usage"] or "{}")
        if row["state"] in ("failed", "cancelled"):
            raise RuntimeError(f"AI 任务 {jid}{STATE_LABELS[row['state']]}：{row['error'] or ''}")
        if time.time() - start > timeout:
            raise RuntimeError(f"AI 任务 {jid} 等了 {int(timeout // 60)} 分钟还没有结果")
        time.sleep(poll)


def enabled():
    """AI_EXECUTOR=queue（环境变量优先，其次 secrets/ai.env）时流水线的 AI 调用走队列。"""
    v = os.environ.get("AI_EXECUTOR")
    if v is None:
        try:
            from . import progress
            v = progress.load_ai().get("AI_EXECUTOR") if progress.configured() else None
        except Exception:
            v = None
    return (v or "").strip().lower() == "queue"


def queue_settings():
    """批量用的环境变量 → (优先级, 每道题最多等几秒；None = 默认 WAIT_SECONDS)。
    AI_QUEUE_PRIORITY=<数字>（low 等于 1，不设是 0）；AI_QUEUE_WAIT_HOURS=<小时>（排在后面的活动要等前面的做完，批量要等得久）。"""
    raw = os.environ.get("AI_QUEUE_PRIORITY", "").strip().lower()
    priority = 1 if raw == "low" else (int(raw) if raw.isdigit() else 0)
    try:
        wait_seconds = float(os.environ.get("AI_QUEUE_WAIT_HOURS") or 0) * 3600 or None
    except ValueError:
        wait_seconds = None
    return priority, wait_seconds


def asker(campaign, step):
    """给流水线用的 ask(messages) → (文字, 用量)：出题、等答案。题目上记下这次原料是什么时候取的、截至哪条记录。
    环境变量（批量用）见 queue_settings。"""
    info = {}
    priority, wait_seconds = queue_settings()
    lock = threading.Lock()

    def times():
        # 同一步会同时出好几道题（多个线程一起调 ask）：两个时间要一次算好再放进去，否则别的线程会读到只填了一半的
        # info（2026-10-31 活动 5240 因此报 KeyError: 'as_of'，业务版没做成）。
        with lock:
            if not info:
                doc = read_json(camp_dir(campaign) / "corpus.json") or {}
                as_of = max((r.get("ts") or "" for r in doc.get("records") or []), default="") or None
                info.update(fetched=doc.get("built_at"), as_of=as_of, namespace=doc.get("attribution_version"))
        return info

    def ask(messages):
        t = times()
        jid = enqueue(messages, campaign=str(campaign), step=step, data_fetched_at=t["fetched"], data_as_of=t["as_of"], priority=priority, namespace=t["namespace"])
        return wait(jid, timeout=wait_seconds)
    from . import scope
    return scope.bind(ask)


# ───────────────────────── 处理方这一侧：领题、取题、续期、交答案、放弃 ─────────────────────────
def _job_view(j, lease):
    return dict(id=j["id"], lease_id=lease["id"], kind=j["kind"], kind_label=KIND_LABELS.get(j["kind"], j["kind"]),
                campaign=j["campaign"], vendor_id=dict(j).get("vendor_id"), step=j["step"], input_chars=j["input_chars"], input_tokens=j["input_tokens"],
                data_fetched_at=j["data_fetched_at"], data_as_of=j["data_as_of"],
                claimed_at=lease["claimed_at"], expires_at=lease["expires_at"],
                input_url=f"/agent-api/v1/jobs/{j['id']}/input?lease={lease['id']}",
                output=dict(format="json_object", top_key=j["expect_key"], top_type=j["expect_type"],
                            top_type_label=TYPE_LABELS.get(j["expect_type"] or "", "")))


def _lease_job(con, jid, lid, agent):
    """核对这次领取是不是这个处理方的、是不是这道题的。→ (领取, 题, 出错时的回应)"""
    if not ID_RE.fullmatch(jid or "") or not ID_RE.fullmatch(lid or ""):
        return None, None, {"status": 400, "reason": "bad_id", "message": "题号或领取号格式不对"}
    lease = con.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
    if not lease or lease["job_id"] != jid or lease["agent"] != agent:
        return None, None, {"status": 404, "reason": "unknown_lease", "message": "找不到这次领取（题号、领取号或钥匙对不上）"}
    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return lease, job, None


def _end_lease(con, lid, reason, at):
    con.execute("UPDATE leases SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL", (at, reason, lid))


def _requeue(con, job, now, reason, failure):
    nows = _iso(now)
    fails = job["failures"] + (1 if failure else 0)
    if fails >= FAILS_TO_FAILED:
        con.execute("""UPDATE jobs SET state='failed', failures=?, error=?, lease_id=NULL, lease_agent=NULL, lease_expires_at=NULL,
                       updated_at=? WHERE id=?""", (fails, f"失败 {fails} 次（最后一次：{reason}）", nows, job["id"]))
        _event(con, job["id"], "failed", detail=f"失败 {fails} 次，最后一次：{reason}", at=nows)
        return
    internal = 1 if (job["internal_only"] or fails >= FAILS_TO_INTERNAL) else 0
    con.execute("""UPDATE jobs SET state='queued', queued_at=?, failures=?, internal_only=?, lease_id=NULL, lease_agent=NULL,
                   lease_expires_at=NULL, updated_at=? WHERE id=?""", (nows, fails, internal, nows, job["id"]))
    _event(con, job["id"], "requeued", detail=reason + (f"（第 {fails} 次失败）" if failure else ""), at=nows)
    if internal and not job["internal_only"]:
        _event(con, job["id"], "internal_only", detail=f"失败满 {FAILS_TO_INTERNAL} 次，改由我们自己的处理方做", at=nows)


def _agent_timeout(con, name, now):
    row = con.execute("SELECT consecutive_timeouts FROM agents WHERE name=?", (name,)).fetchone()
    if not row:
        return
    n = row["consecutive_timeouts"] + 1
    paused = _iso(now + dt.timedelta(seconds=PAUSE_SECONDS)) if n >= PAUSE_AFTER_TIMEOUTS else None
    con.execute("UPDATE agents SET timeout_count=timeout_count+1, consecutive_timeouts=?, paused_until=COALESCE(?, paused_until) WHERE name=?",
                (n, paused, name))
    if paused:
        _event(con, None, "agent_paused", name, detail=f"连续超时 {n} 次，暂停领题到 {paused}", at=_iso(now))


def _housekeeping(con, now):
    """收回过了截止时间的领取（题目重新排队）；作废没人在等的题；谁都读不下的题判失败；定期清理旧题目原文。"""
    nows = _iso(now)
    for lease in con.execute("SELECT * FROM leases WHERE ended_at IS NULL AND expires_at < ?", (nows,)).fetchall():
        _end_lease(con, lease["id"], "expired", nows)
        _event(con, lease["job_id"], "expired", lease["agent"], lease["id"], detail=f"截止 {lease['expires_at']} 前没有交回", at=nows)
        _agent_timeout(con, lease["agent"], now)
        job = con.execute("SELECT * FROM jobs WHERE id=?", (lease["job_id"],)).fetchone()
        if job and job["state"] == "leased" and job["lease_id"] == lease["id"]:
            _requeue(con, job, now, f"{lease['agent']} 超时失联", failure=True)
    cutoff = _iso(now - dt.timedelta(seconds=ORPHAN_SECONDS))
    for job in con.execute("SELECT id FROM jobs WHERE state='queued' AND waiter_seen_at < ?", (cutoff,)).fetchall():
        con.execute("UPDATE jobs SET state='cancelled', error=?, updated_at=? WHERE id=?",
                    ("没有流程在等这道题了（超过 15 分钟没人来取结果）", nows, job["id"]))
        _event(con, job["id"], "cancelled", detail="没有流程在等这道题了", at=nows)
    caps = [r["context_tokens"] for r in con.execute("SELECT context_tokens FROM agents WHERE enabled=1")]
    if caps:  # 一个处理方都没有时不判：等有人登记
        stuck = _iso(now - dt.timedelta(seconds=UNREADABLE_SECONDS))
        for job in con.execute("SELECT id, input_tokens FROM jobs WHERE state='queued' AND queued_at < ?", (stuck,)).fetchall():
            if not fits(job["input_tokens"], max(caps)):
                why = f"所有启用的处理方都读不下（约 {job['input_tokens']} token）"
                con.execute("UPDATE jobs SET state='failed', error=?, updated_at=? WHERE id=?", (why, nows, job["id"]))
                _event(con, job["id"], "failed", detail=why, at=nows)
    last = con.execute("SELECT v FROM meta WHERE k='pruned_at'").fetchone()
    if not last or (now - _parse(last["v"])).total_seconds() >= PRUNE_SECONDS:
        old = _iso(now - dt.timedelta(days=KEEP_INPUT_DAYS))
        con.execute("""DELETE FROM inputs WHERE key IN (SELECT key FROM jobs WHERE state IN ('done','failed','cancelled') AND updated_at < ?)
                       AND key NOT IN (SELECT key FROM jobs WHERE state IN ('queued','leased') OR updated_at >= ?)""", (old, old))
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('pruned_at', ?)", (nows,))


def claim(agent):
    """处理方来领题。一个处理方同时只有一道；没交又来领，拿到的还是那一道。→ 回应（status 是 HTTP 状态码）"""
    now = _now()
    nows = _iso(now)
    with _tx() as con:
        _housekeeping(con, now)
        me = con.execute("SELECT * FROM agents WHERE name=?", (agent,)).fetchone()
        if not me:
            return {"status": 403, "reason": "unknown_agent", "message": "没有这个处理方"}
        if not me["enabled"]:
            return {"status": 403, "reason": "disabled", "message": "这个处理方已停用"}
        con.execute("UPDATE agents SET last_seen_at=? WHERE name=?", (nows, agent))
        held = con.execute("SELECT * FROM leases WHERE agent=? AND ended_at IS NULL", (agent,)).fetchone()
        if held:
            job = con.execute("SELECT * FROM jobs WHERE id=?", (held["job_id"],)).fetchone()
            return {"status": 200, "job": _job_view(job, held), "again": True,
                    "message": f"你还有一道题 {job['id']} 没交（截止 {held['expires_at']} UTC），先把它做完；一个处理方同时只能拿一道"}
        if me["paused_until"] and me["paused_until"] > nows:
            return {"status": 200, "job": None, "reason": "paused", "paused_until": me["paused_until"],
                    "message": f"连续超时，暂停领题到 {me['paused_until']} UTC"}
        rows = con.execute("SELECT rowid AS seq, * FROM jobs WHERE state='queued' ORDER BY priority, created_at, rowid").fetchall()
        caps = [r["context_tokens"] for r in con.execute("SELECT context_tokens FROM agents WHERE enabled=1")]
        smallest = min(caps) if caps else None
        best = None
        for job in rows:
            if not fits(job["input_tokens"], me["context_tokens"]):
                continue
            if job["internal_only"] and not me["internal"]:  # 失败满 3 次的题只给自家处理方
                continue
            big = 0 if (smallest is not None and not fits(job["input_tokens"], smallest)) else 1  # 小处理方读不下的大题优先
            rank = (job["priority"], big, job["created_at"], job["seq"])  # 普通档先；同档里大题先给大处理方；再按出题先后
            if best is None or rank < best[0]:
                best = (rank, job)
        if best is None:
            return {"status": 200, "job": None, "reason": "empty" if not rows else "nothing_fits",
                    "message": "现在没有题" if not rows else "现在排着的题都不适合你（读不下，或者留给别的处理方）"}
        job = best[1]
        lid = _new_id("L")
        exp = _iso(now + dt.timedelta(seconds=LEASE_SECONDS))
        con.execute("INSERT INTO leases(id, job_id, agent, claimed_at, expires_at) VALUES (?,?,?,?,?)", (lid, job["id"], agent, nows, exp))
        con.execute("""UPDATE jobs SET state='leased', lease_id=?, lease_agent=?, lease_expires_at=?, attempts=attempts+1, updated_at=?
                       WHERE id=?""", (lid, agent, exp, nows, job["id"]))
        _event(con, job["id"], "claimed", agent, lid, detail=f"截止 {exp}", at=nows)
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        lease = con.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
        return {"status": 200, "job": _job_view(job, lease), "message": f"领到题 {job['id']}，截止 {exp} UTC"}


def get_input(jid, lid, agent):
    """下载题目原文（说明 + 材料）。第一次取的时间记在这次领取上。"""
    now = _iso(_now())
    with _tx() as con:
        lease, job, err = _lease_job(con, jid, lid, agent)
        if err:
            return err
        if job["state"] in ("done", "cancelled"):
            return {"status": 409, "reason": job["state"], "message": f"这道题{STATE_LABELS[job['state']]}，不用再做了"}
        if lease["input_fetched_at"] is None:
            con.execute("UPDATE leases SET input_fetched_at=? WHERE id=?", (now, lid))
            _event(con, jid, "input", agent, lid, at=now)
            fetched = now
        else:
            fetched = lease["input_fetched_at"]
        body = con.execute("SELECT body FROM inputs WHERE key=?", (job["key"],)).fetchone()
    if not body:
        return {"status": 410, "reason": "input_gone", "message": "这道题的原文已经清理掉了"}
    messages = json.loads(zlib.decompress(body["body"]).decode("utf-8"))
    return {"status": 200, "job_id": jid, "lease_id": lid, "kind": job["kind"], "kind_label": KIND_LABELS.get(job["kind"], job["kind"]),
            "campaign": job["campaign"], "data_fetched_at": job["data_fetched_at"], "data_as_of": job["data_as_of"],
            "input_fetched_at": fetched, "expires_at": lease["expires_at"], "messages": messages,
            "output": {"format": "json_object", "top_key": job["expect_key"], "top_type": job["expect_type"],
                       "top_type_label": TYPE_LABELS.get(job["expect_type"] or "", "")},
            "message": "按 messages 作答：system 是工作说明，user 是材料；只输出一个 JSON 对象"}


def heartbeat(jid, lid, agent):
    """续期：截止时间延到现在 + 40 分钟（一次领取最长 60 分钟）。题目已完成或作废时告诉处理方停下。"""
    now = _now()
    nows = _iso(now)
    with _tx() as con:
        _housekeeping(con, now)
        lease, job, err = _lease_job(con, jid, lid, agent)
        if err:
            return err
        if job["state"] in ("done", "cancelled"):
            _end_lease(con, lid, "superseded" if job["state"] == "done" else "cancelled", nows)
            return {"status": 200, "continue": False, "state": job["state"],
                    "message": f"这道题{STATE_LABELS[job['state']]}，不用再做了"}
        cap = _parse(lease["claimed_at"]) + dt.timedelta(seconds=MAX_LEASE_SECONDS)
        new_exp = min(now + dt.timedelta(seconds=LEASE_SECONDS), cap)
        if lease["ended_at"] is None:
            con.execute("UPDATE leases SET expires_at=?, last_heartbeat_at=? WHERE id=?", (_iso(new_exp), nows, lid))
            con.execute("UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? AND lease_id=?", (_iso(new_exp), nows, jid, lid))
            _event(con, jid, "heartbeat", agent, lid, detail=f"截止延到 {_iso(new_exp)}", at=nows)
            note = "（已到一次领取的最长时间，不能再延）" if new_exp == cap else ""
            return {"status": 200, "continue": True, "expires_at": _iso(new_exp), "message": f"截止时间延到 {_iso(new_exp)} UTC{note}"}
        other = con.execute("SELECT 1 FROM leases WHERE agent=? AND ended_at IS NULL", (agent,)).fetchone()
        if job["state"] == "queued" and new_exp > now and not other and not job["internal_only"]:
            con.execute("UPDATE leases SET ended_at=NULL, end_reason=NULL, expires_at=?, last_heartbeat_at=? WHERE id=?", (_iso(new_exp), nows, lid))
            con.execute("""UPDATE jobs SET state='leased', lease_id=?, lease_agent=?, lease_expires_at=?, updated_at=? WHERE id=?""",
                        (lid, agent, _iso(new_exp), nows, jid))
            _event(con, jid, "reinstated", agent, lid, detail="超时后回来续期，题目还没人领，接着做", at=nows)
            return {"status": 200, "continue": True, "expires_at": _iso(new_exp), "message": f"你超时了，但题目还没人领，接着做；截止 {_iso(new_exp)} UTC"}
        return {"status": 200, "continue": True, "expires_at": None, "reassigned": True,
                "message": "你超时了，题目已经重新排队或分给别人；你交的合格答案仍然算数（先到先得）"}


def _no_constants(name):
    raise ValueError(f"答案里有标准 JSON 不允许的 {name}")


def validate(output, expect_key, expect_type=None):
    """答案合格 = 能读出一个标准的 JSON 对象（不收 NaN、Infinity），有题目要求的顶层字段，而且这个字段的样子对
    （说明里的输出模板写的是对象列表，就得是对象列表）。答案外面包着思考过程、代码块标记或一两句话也行（流水线本来就这样读），
    收下时只存读出来的 JSON。→ (合格吗, 错误说明, 出错位置附近的原文, 规整后的 JSON 文本)"""
    if not isinstance(output, str) or not output.strip():
        return False, "答案是空的", "", None
    body = re.sub(r"<think>.*?</think>", "", output, flags=re.S)
    body = re.sub(r"^```(?:json)?|```$", "", body.strip(), flags=re.M).strip()
    a, b = body.find("{"), body.rfind("}")
    if a < 0 or b < a:
        return False, "没有找到完整的 JSON 对象（缺少开头的 { 或结尾的 }）", body[-180:], None
    try:
        obj = json.loads(body[a:b + 1], parse_constant=_no_constants)
    except json.JSONDecodeError as e:
        return False, f"不是合法的 JSON：{e.msg}（第 {e.pos} 个字符附近）", e.doc[max(0, e.pos - 120):e.pos + 60], None
    except ValueError as e:
        return False, str(e), "", None
    if not isinstance(obj, dict):
        return False, "最外层必须是一个 JSON 对象 {...}", "", None
    if expect_key:
        if expect_key not in obj:
            return False, f"缺少题目要求的字段 \"{expect_key}\"", "", None
        v = obj[expect_key]
        good = {"list_of_objects": isinstance(v, list) and all(isinstance(x, dict) for x in v), "list": isinstance(v, list),
                "string": isinstance(v, str), "object": isinstance(v, dict)}.get(expect_type, True)
        if not good:
            return False, f"字段 \"{expect_key}\" 应该是{TYPE_LABELS[expect_type]}", json.dumps(v, ensure_ascii=False)[:180], None
    return True, "", "", json.dumps(obj, ensure_ascii=False)


def submit(jid, lid, agent, output, model=None, usage=None):
    """交答案。合格就收（先到先得）；不合格退回原因；题目已完成或作废就告诉它不用了。"""
    now = _now()
    nows = _iso(now)
    if isinstance(output, (dict, list)):
        output = json.dumps(output, ensure_ascii=False)
    with _tx() as con:
        _housekeeping(con, now)  # 截止时间过了还没收回的，先收回：超时后才交的答案记成迟到
        lease, job, err = _lease_job(con, jid, lid, agent)
        if err:
            return err
        submits = lease["submits"] + 1
        con.execute("UPDATE leases SET submits=? WHERE id=?", (submits, lid))
        if job["state"] == "done":
            if job["result_lease"] == lid:
                return {"status": 200, "accepted": True, "duplicate": True, "message": "这份答案之前已经收下了"}
            _end_lease(con, lid, "superseded", nows)
            con.execute("UPDATE agents SET late_count=late_count+1 WHERE name=?", (agent,))
            _event(con, jid, "late_ignored", agent, lid, detail=f"题目已由 {job['result_agent']} 完成", at=nows)
            return {"status": 409, "accepted": False, "reason": "already_done", "message": "这道题已经有人交了合格答案，你这份不用了"}
        if job["state"] == "cancelled":
            _end_lease(con, lid, "cancelled", nows)
            return {"status": 409, "accepted": False, "reason": "cancelled", "message": "这道题已作废，答案不用了"}
        if submits > MAX_SUBMITS:
            return {"status": 429, "accepted": False, "reason": "too_many_submits", "message": f"这次领取已经交了 {MAX_SUBMITS} 次，不能再交"}
        ok, error, near, clean = validate(output, job["expect_key"], job["expect_type"])
        if not ok:
            con.execute("UPDATE agents SET rejected_count=rejected_count+1 WHERE name=?", (agent,))
            _event(con, jid, "rejected", agent, lid, detail=error, at=nows)
            left = MAX_SUBMITS - submits
            if left <= 0 and lease["ended_at"] is None:
                _end_lease(con, lid, "rejected", nows)
                if job["state"] == "leased" and job["lease_id"] == lid:
                    _requeue(con, job, now, f"{agent} 交满 {MAX_SUBMITS} 次仍不合格", failure=True)
            return {"status": 422, "accepted": False, "reason": "invalid", "error": error, "near": near, "submits_left": max(left, 0),
                    "message": f"答案不合格：{error}。改好后用同一个 lease_id 再交（还能交 {max(left, 0)} 次）"}
        late = lease["ended_at"] is not None or job["lease_id"] != lid
        con.execute("""UPDATE jobs SET state='done', result=?, result_usage=?, result_agent=?, result_model=?, result_lease=?, done_at=?,
                       lease_id=NULL, lease_agent=NULL, lease_expires_at=NULL, error=NULL, updated_at=? WHERE id=?""",
                    (clean, json.dumps(usage if isinstance(usage, dict) else {}, ensure_ascii=False), agent, str(model or "")[:80], lid,
                     nows, nows, jid))
        if lease["ended_at"] is None:
            _end_lease(con, lid, "submitted", nows)
        else:
            con.execute("UPDATE leases SET end_reason='submitted_late' WHERE id=?", (lid,))
        for other in con.execute("SELECT id, agent FROM leases WHERE job_id=? AND ended_at IS NULL", (jid,)).fetchall():
            _end_lease(con, other["id"], "superseded", nows)
            _event(con, jid, "superseded", other["agent"], other["id"], detail=f"{agent} 先交了合格答案", at=nows)
        busy = int((now - _parse(lease["claimed_at"])).total_seconds())
        con.execute("UPDATE agents SET done_count=done_count+1, consecutive_timeouts=0, busy_seconds=busy_seconds+? WHERE name=?", (busy, agent))
        was = "（这道题之前已判失败）" if job["state"] == "failed" else ""
        _event(con, jid, "late_accepted" if late else "accepted", agent, lid, detail=(str(model or "") + was).strip() or None, at=nows)
        return {"status": 200, "accepted": True, "late": late, "message": "收下了" + ("（超时后交回，题目还没完成，照样算数）" if late else "")}


def release(jid, lid, agent, reason="", count_failure=True):
    """放弃这道题，题目马上重新排队。count_failure=False（接口里 retryable: true）用于网关限流、处理方重启这类
    不是题目本身的问题：不算这道题失败一次。"""
    now = _now()
    nows = _iso(now)
    with _tx() as con:
        _housekeeping(con, now)
        lease, job, err = _lease_job(con, jid, lid, agent)
        if err:
            return err
        if lease["ended_at"] is None:
            _end_lease(con, lid, "released", nows)
            con.execute("UPDATE agents SET released_count=released_count+1 WHERE name=?", (agent,))
            _event(con, jid, "released", agent, lid, detail=(reason or "")[:200] or None, at=nows)
            if job["state"] == "leased" and job["lease_id"] == lid:
                _requeue(con, job, now, f"{agent} 放弃" + (f"：{reason[:80]}" if reason else ""), failure=count_failure)
        return {"status": 200, "released": True, "message": "已退回，题目重新排队"}


# ───────────────────────── 状态页用 ─────────────────────────
def snapshot(state=None, campaign=None, limit=300):
    now = _now()
    with _tx() as con:
        _housekeeping(con, now)  # 看页面时顺手收回超时的领取，状态是最新的
    con = _connect()
    try:
        counts = {s: 0 for s in STATES}
        for r in con.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state"):
            counts[r["state"]] = r["n"]
        where, args = [], []
        if state in STATES:
            where.append("state=?")
            args.append(state)
        if campaign:
            where.append("campaign=?")
            args.append(str(campaign))
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC, id DESC LIMIT ?"
        jobs = [dict(r) for r in con.execute(sql, args + [int(limit)])]
        last = {}
        if jobs:
            marks = ",".join("?" * len(jobs))
            for r in con.execute(f"SELECT job_id, kind, at, agent, detail FROM events WHERE id IN "
                                 f"(SELECT MAX(id) FROM events WHERE job_id IN ({marks}) GROUP BY job_id)", [j["id"] for j in jobs]):
                last[r["job_id"]] = dict(r)
        for j in jobs:
            j.pop("result", None)
            j["last_event"] = last.get(j["id"])
        since = _iso(now - dt.timedelta(days=2))
        camps = {}
        for r in con.execute("SELECT campaign, state, COUNT(*) n FROM jobs WHERE created_at >= ? GROUP BY campaign, state", (since,)):
            camps.setdefault(r["campaign"] or "-", {s: 0 for s in STATES})[r["state"]] = r["n"]
    finally:
        con.close()
    return dict(now=_iso(now), counts=counts, agents=list_agents(), jobs=jobs, campaigns=camps)


def job_detail(jid):
    if not ID_RE.fullmatch(jid or ""):
        return None
    con = _connect()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job:
            return None
        job = dict(job)
        job["result_chars"] = len(job.pop("result") or "")
        leases = [dict(r) for r in con.execute("SELECT * FROM leases WHERE job_id=? ORDER BY claimed_at", (jid,))]
        events = [dict(r) for r in con.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (jid,))]
    finally:
        con.close()
    return dict(job=job, leases=leases, events=events)


def campaign_counts(campaign, since=None):
    """某个活动从 since（这次刷新开始的时间；不给就是最近 2 天）起出的题，按状态计数（首页的「AI 任务 a/b」用）。"""
    since = since or _iso(_now() - dt.timedelta(days=2))
    if not pathlib.Path(DB_PATH).exists():  # 只是看一眼：还没有队列库就不建
        return {s: 0 for s in STATES}
    con = _connect()
    try:
        out = {s: 0 for s in STATES}
        for r in con.execute("SELECT state, COUNT(*) n FROM jobs WHERE campaign=? AND created_at >= ? GROUP BY state", (str(campaign), since)):
            out[r["state"]] = r["n"]
    finally:
        con.close()
    return out
