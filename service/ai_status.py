"""AI 运行状态（V3）：每个「活动 + 渠道」的 Pi 智能体运行在做什么。只读。

- /ai               列表：正在跑的在最上面（含取数初筛阶段），下面是最近跑完的
- /ai/run?c=&vendor=&run=   一次运行：顶部是阶段、用时、轮数、工具调用、草稿；下面按聊天记录排：
                    AI 的话在左（思考默认折叠），工具调用和程序提示在中间折叠，审核 Agent 的结论在左并标「审核」；
                    AI 正在输出时，最底下显示它当前写到哪里。

数据都来自运行目录（raw/campaigns/<活动>/[vendors/<渠道>/]pi-runs/<运行>/）和活动状态（state/），不改任何东西。
events.jsonl 很大（大部分是逐字输出的 message_update），这里按文件偏移增量读，只解析完整的消息和工具调用。
"""
import datetime as dt
import html
import json
import os
import pathlib
import re
import threading
import time

from replay import scope, util

RUN_NAME = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}\Z")
MAX_ITEMS = 600          # 详情页最多显示最近多少条（运行可能有上千次工具调用）
LIST_LIMIT = 40
TAIL_BYTES = 400_000     # 看「当前正在输出什么」时读文件尾部多少
_CACHE = {}
_LOCK = threading.Lock()
esc = html.escape

TOOL_LABEL = {
    "read": ("读文件", "Read file"), "ls": ("列目录", "List folder"), "write": ("写文件", "Write file"),
    "edit": ("改文件", "Edit file"), "feedai_api": ("FeedAI", "FeedAI"), "github_api": ("GitHub", "GitHub"),
    "fireflies_api": ("Fireflies 会议", "Fireflies"), "review_verdict": ("审核结论", "Review verdict"),
    "report_find": ("看结果", "View result"), "report_add": ("写入结果", "Add to result"),
    "report_update": ("修改结果", "Update result"), "report_delete": ("删除结果条目", "Delete from result"),
    "report_set": ("写结果整体部分", "Set result field"), "report_finish": ("标记完成", "Finish"),
    "note_add": ("记笔记", "Add note"),
}


REPORT_STATUS = {"complete": ("AI 标为已写完", "marked complete"), "draft": ("草稿", "draft")}


def _now():
    return time.time()


def _ts(value):
    """ISO 字符串或毫秒数 → 秒。"""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _clock(sec, z):
    """北京时间（中文）/ UTC（英文）的时:分:秒。"""
    if not sec:
        return ""
    if z:
        return time.strftime("%m-%d %H:%M:%S", time.gmtime(sec + 8 * 3600))
    return time.strftime("%m-%d %H:%M:%S UTC", time.gmtime(sec))


def _minutes(sec, z):
    if sec is None:
        return ""
    m = int(sec // 60)
    return (f"{m} 分钟" if z else f"{m} min") if m else (f"{int(sec)} 秒" if z else f"{int(sec)} s")


def _json(path, default=None):
    """读 JSON；AI 写坏的文件（运行中途可能出现）当作没有，不让页面报错。"""
    try:
        return util.read_json(path, default)
    except (ValueError, OSError, UnicodeError):
        return default


def _base(cid, vendor):
    base = util.RAW / "campaigns" / str(cid)
    return base / "vendors" / str(vendor) if vendor else base


def _state(cid, vendor):
    path = util.STATE / str(cid) / "vendors" / str(vendor) / "state.json" if vendor else util.STATE / f"{cid}.json"
    return _json(path, {}) or {}


def all_runs():
    """[(cid, vendor, run 目录)]，新的在前。"""
    found = []
    root = util.RAW / "campaigns"
    if not root.is_dir():
        return found
    for camp in root.iterdir():
        if not camp.name.isdigit():
            continue
        dirs = [(None, camp / "pi-runs")]
        vroot = camp / "vendors"
        if vroot.is_dir():
            dirs += [(v.name, v / "pi-runs") for v in vroot.iterdir() if v.is_dir()]
        for vendor, d in dirs:
            if d.is_dir():
                found += [(camp.name, vendor, r) for r in d.iterdir() if RUN_NAME.match(r.name) and (r / "run.json").is_file()]
    found.sort(key=lambda x: x[2].name, reverse=True)
    return found


def find_run(cid, vendor, name):
    if not (str(cid).isdigit() and RUN_NAME.match(name or "")) or (vendor and not scope.valid_vendor(vendor)):
        return None
    path = _base(cid, vendor) / "pi-runs" / name
    return path if (path / "run.json").is_file() else None


# ---------- 读运行记录 ----------

def _tool_summary(name, args):
    if not isinstance(args, dict):
        return str(args)[:160]
    if name in ("read", "write", "edit"):
        bits = [str(args.get("path") or args.get("file_path") or "")]
        if args.get("offset") or args.get("limit"):
            bits.append(f"行 {args.get('offset') or 1} 起 {args.get('limit') or ''}".strip())
        return " · ".join(b for b in bits if b)
    if name == "ls":
        return str(args.get("path") or ".")
    if name == "report_add":
        def one(e):
            d = e.get("data") or {}
            return " · ".join(str(x) for x in (e.get("type"), d.get("ts"), d.get("action_zh") or d.get("title_zh") or d.get("topic")) if x)
        items = args.get("items")
        return (f"{len(items)} 条：" + "；".join(one(e) for e in items if isinstance(e, dict))) if isinstance(items, list) else one(args)
    if name == "report_update" and isinstance(args.get("items"), list):
        return f"{len(args['items'])} 条：" + "、".join(str(e.get("id")) for e in args["items"] if isinstance(e, dict))
    if name in ("report_update", "report_delete"):
        return " · ".join(str(x) for x in (args.get("id"), args.get("reason") or ", ".join((args.get("data") or {}).keys())) if x)
    if name == "report_set":
        v = args.get("value")
        return f'{args.get("field")} · {len(v)} {"字" if isinstance(v, str) else "项"}' if v is not None else str(args.get("field"))
    if name == "report_find":
        return " · ".join(f"{k}={v}" for k, v in args.items() if v not in (None, "", False)) or "总览"
    if name == "note_add":
        return f'{args.get("kind") or "progress"} · {str(args.get("text") or "")[:80]}'

    if name == "feedai_api":
        body = args.get("body") or {}
        return " · ".join(str(x) for x in (args.get("operation") or args.get("op"), args.get("collection"),
                                            f"limit {body.get('limit')}" if isinstance(body, dict) and body.get("limit") else "") if x)
    if name == "github_api":
        params = args.get("params") or {}
        q = params.get("q") if isinstance(params, dict) else ""
        return " · ".join(str(x) for x in (args.get("path"), q) if x)
    if name == "fireflies_api":
        return " · ".join(str(x) for x in (args.get("operation"), json.dumps(args.get("variables") or {}, ensure_ascii=False)[:100]) if x)
    return json.dumps(args, ensure_ascii=False)[:160]


def _result_text(result):
    if isinstance(result, dict):
        parts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
        return "\n".join(p for p in parts if p)
    return "" if result is None else str(result)


def _message_item(msg):
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    thinking = [c.get("thinking", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "thinking"]
    text = "\n".join(t for t in texts if t.strip())
    think = "\n".join(t for t in thinking if t.strip())
    ts = _ts(msg.get("timestamp"))
    if role == "user":
        return {"kind": "program", "ts": ts, "text": text} if text else None
    if role == "assistant":
        err = msg.get("errorMessage")
        if not (text or think or err):
            return None
        return {"kind": "ai", "ts": ts, "text": text, "thinking": think, "error": str(err)[:400] if err else ""}
    return None


def _read_events(run):
    """增量解析 events.jsonl。返回 (条目列表, 统计)。"""
    path = run / "events.jsonl"
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return [], {}
    with _LOCK:
        c = _CACHE.get(key)
        if c is None or size < c["offset"]:
            c = {"offset": 0, "items": [], "pending": {}, "turns": 0, "tools": 0, "errors": 0, "last_ts": None}
            _CACHE[key] = c
        if size > c["offset"]:
            with open(path, "rb") as fh:
                fh.seek(c["offset"])
                data = fh.read(size - c["offset"])
            cut = data.rfind(b"\n")
            if cut >= 0:
                c["offset"] += cut + 1
                for line in data[:cut].split(b"\n"):
                    if not line or line.startswith(b'{"type":"message_update"'):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    t = e.get("type")
                    if t == "turn_start":
                        c["turns"] += 1
                    elif t == "message_end":
                        item = _message_item(e.get("message") or {})
                        ts = _ts((e.get("message") or {}).get("timestamp"))
                        if ts:
                            c["last_ts"] = ts
                        if item:
                            c["items"].append(item)
                    elif t == "tool_execution_start":
                        name = e.get("toolName") or ""
                        item = {"kind": "tool", "ts": c["last_ts"], "name": name, "args": e.get("args"),
                                "summary": _tool_summary(name, e.get("args")), "state": "running"}
                        c["pending"][e.get("toolCallId")] = item
                        c["items"].append(item)
                        c["tools"] += 1
                    elif t == "tool_execution_end":
                        item = c["pending"].pop(e.get("toolCallId"), None)
                        if item is None:
                            continue
                        bad = e.get("isError") in (True, "True", "true")
                        item.update(state="error" if bad else "ok", ms=e.get("durationMs"),
                                    result=_result_text(e.get("result"))[:1500])
                        c["errors"] += bad
            if len(c["items"]) > MAX_ITEMS * 2:
                c["items"] = c["items"][-MAX_ITEMS:]
        return list(c["items"]), {"turns": c["turns"], "tools": c["tools"], "errors": c["errors"]}


def _live(run):
    """AI 正在逐字输出时，取最后一条未完成的消息（只看文件尾部）。"""
    path = run / "events.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    lines = tail.split(b"\n")
    for line in reversed(lines):
        if not line.strip():
            continue
        if not line.startswith(b'{"type":"message_update"'):
            return None  # 最后一条已是完整事件：没有在输出
        try:
            e = json.loads(line)
        except ValueError:
            continue
        item = _message_item(e.get("message") or {})
        return item
    return None


def _reviews(run, s):
    out = []
    for i, r in enumerate(s.get("reviews") or [], 1):
        vf = run / f"review-{r.get('round', i)}" / "verdict.json"
        ts = vf.stat().st_mtime if vf.is_file() else None
        out.append({"kind": "review", "ts": ts, "round": r.get("round", i), "decision": r.get("decision"),
                    "text": r.get("summary") or "", "suggestions": r.get("suggestions") or []})
    return out


def _report(run):
    return _json(run / "work/output/report.json", {}) or {}


def _is_current(cid, vendor, run, st):
    """这次运行是不是该活动+渠道正在进行的那一次：必须是最新的运行目录，并且是在这一轮刷新开始之后建的。"""
    d = _base(cid, vendor) / "pi-runs"
    latest = max((r.name for r in d.iterdir() if RUN_NAME.match(r.name)), default=None) if d.is_dir() else None
    if run.name != latest:
        return False
    began, made = _ts(st.get("started_at")), _ts((_json(run / "run.json", {}) or {}).get("started_at"))
    return not (began and made and made < began - 5)


def phase(cid, vendor, run, s, z):
    """(阶段文字, 是否还在跑)。"""
    status = s.get("status")
    if status == "completed":
        return ("已完成" if z else "Completed"), False
    if status == "kept_previous":
        return ("已完成（保留上一版页面）" if z else "Completed (previous page kept)"), False
    if status == "failed":
        return ("未正常结束" if z else "Did not finish"), False
    st = _state(cid, vendor)
    if not st.get("running") or not _is_current(cid, vendor, run, st):
        # 活动没在跑；或者活动在跑，但跑的是更新的那一次——这条是以前被停掉、记录停在 running 的旧运行
        return ("已中止" if z else "Stopped"), False
    pending = [p for p in run.glob("review-*") if p.is_dir() and not (p / "verdict.json").exists()]
    if pending:
        n = max(int(p.name.split("-")[1]) for p in pending)
        return (f"审核中（第 {n} 轮）" if z else f"Reviewing (round {n})"), True
    if any(r.get("decision") == "reject" for r in s.get("reviews") or []):
        return ("按审核意见修改" if z else "Revising after review"), True
    return ("调查中" if z else "Investigating"), True


FINISHED = ("completed", "kept_previous", "failed")  # 程序正常收尾、写过统计的运行状态


def _elapsed(s, run=None, live=True):
    """用时。正常收尾的用记录里的秒数；被中止的（run.json 还停在 running）算到最后一次有动静为止，不算到现在。"""
    if s.get("seconds") is not None and s.get("status") in FINISHED:
        return s["seconds"]
    start = _ts(s.get("started_at"))
    if not start:
        return None
    if live or run is None:
        return _now() - start
    last = max((p.stat().st_mtime for p in (run / "events.jsonl", run / "run.json") if p.is_file()), default=start)
    return max(0, last - start)


def _row_info(cid, vendor, run, z):
    s = _json(run / "run.json", {}) or {}
    label, live = phase(cid, vendor, run, s, z)
    if s.get("status") in FINISHED and isinstance(s.get("tools"), dict):  # 正常收尾的直接用运行记录里的统计，不去读几十 MB 的事件文件
        counts = {"turns": s.get("turns") or 0, "tools": sum(v for k, v in s["tools"].items() if k != "review_verdict"),
                  "errors": s.get("tool_errors") or 0}
    else:
        _, counts = _read_events(run)
    rep = _report(run)
    brief = _json(run / "work/seed/brief.json", {}) or {}
    return {"s": s, "label": label, "live": live, "counts": counts, "rep": rep,
            "mode": brief.get("mode"), "started": _ts(s.get("started_at")), "elapsed": _elapsed(s, run, live)}


def _prefetch_rows(z):
    """正在取数、初筛，AI 还没开工的活动（这时还没有运行目录）。"""
    rows = []
    if not util.STATE.is_dir():
        return rows
    files = [(f.stem, None, f) for f in util.STATE.glob("*.json") if f.stem.isdigit()]
    files += [(f.parts[-4], f.parts[-2], f) for f in util.STATE.glob("*/vendors/*/state.json")]
    for cid, vendor, f in files:
        st = _json(f, {}) or {}
        if not st.get("running"):
            continue
        started = _ts(st.get("started_at"))
        runs = sorted((_base(cid, vendor) / "pi-runs").glob("*")) if (_base(cid, vendor) / "pi-runs").is_dir() else []
        latest = _ts(_json(runs[-1] / "run.json", {}).get("started_at")) if runs else None
        if latest and started and latest >= started - 5:
            continue
        steps = [x.get("step") for x in (st.get("current") or {}).get("steps", []) if x.get("step")]
        rows.append({"cid": cid, "vendor": vendor, "started": started,
                     "label": "取数初筛" if z else "Fetching data", "step": steps[-1] if steps else ""})
    return rows


# ---------- 页面 ----------

STYLE = """
.chat{display:flex;flex-direction:column;gap:.6rem}
.who{font-size:.75rem;color:var(--bs-secondary-color);margin-bottom:.15rem}
.bubble{max-width:78%;padding:.6rem .8rem;border-radius:.75rem;background:var(--bs-tertiary-bg);white-space:pre-wrap;word-break:break-word}
.left{align-self:flex-start}.mid{align-self:center;max-width:88%;width:100%}
.mid details{font-size:.82rem;color:var(--bs-secondary-color);background:transparent;border:1px solid var(--bs-border-color);border-radius:.5rem;padding:.25rem .6rem}
.mid summary{cursor:pointer}
.mid pre,.bubble pre{white-space:pre-wrap;word-break:break-word;font-size:.78rem;margin:.4rem 0 0;max-height:24rem;overflow:auto}
.think{margin-top:.4rem;font-size:.82rem}
.state-error{color:var(--bs-danger-text-emphasis)}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:.5rem 1rem}
.kv div small{display:block;color:var(--bs-secondary-color)}
.live{border:1px dashed var(--bs-border-color)}
"""


def _shell(title, body, lang, css, script=""):
    z = lang == "zh"
    return f"""<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="/static/bootstrap.min.css">
<style>{css}{STYLE}</style></head><body data-bs-theme="dark">
<main class="container py-4" style="max-width:1140px">{body}</main>{script}</body></html>"""


def _header(title, sub, lang, back, extra_query=""):
    z = lang == "zh"
    langs = (f'<a class="btn btn-outline-secondary{" active" if z else ""}" href="?{extra_query}lang=zh">{"中文" if z else "Chinese"}</a>'
             f'<a class="btn btn-outline-secondary{"" if z else " active"}" href="?{extra_query}lang=en">English</a>')
    return f"""<header class="d-flex justify-content-between align-items-start gap-3 mb-4">
  <div class="d-flex align-items-center gap-3"><span class="brand-mark"><i></i></span><div><h1 class="h4 mb-1">{esc(title)}</h1><p class="text-body-secondary mb-0">{esc(sub)}</p></div></div>
  <div class="text-end"><div class="btn-group btn-group-sm" role="group">{langs}</div>
  <div class="small text-body-secondary mt-2">{back}</div></div>
</header>"""


def list_page(lang, css="", name=None, channel=None):
    z = lang == "zh"
    name = name or (lambda n: n)
    rows_live, rows_done = [], []

    def scope_cell(cid, vendor):
        sub = (channel(cid, vendor, lang) if channel else "") or (f"#{vendor}" if vendor else "")
        return f'<td><b>{esc(name(cid) or cid)}</b>{f"<small>{esc(sub)}</small>" if sub else ""}</td>'

    for r in _prefetch_rows(z):
        step = f"<small>{esc(r['step'])}</small>" if r["step"] else ""
        rows_live.append(f'<tr>{scope_cell(r["cid"], r["vendor"])}<td><span class="badge text-bg-primary">{esc(r["label"])}</span>'
                         f'{step}</td><td>{_clock(r["started"], z)}</td>'
                         f'<td>{_minutes(_now() - r["started"], z) if r["started"] else ""}</td><td>—</td><td>—</td><td></td></tr>')
    for cid, vendor, run in all_runs()[:LIST_LIMIT]:
        info = _row_info(cid, vendor, run, z)
        c, rep = info["counts"], info["rep"]
        q = f"c={cid}&run={run.name}" + (f"&vendor={vendor}" if vendor else "")
        mode = {"update": "增量更新" if z else "Update", "full": "完整调查" if z else "Full"}.get(info["mode"], "")
        badge = "text-bg-primary" if info["live"] else "text-bg-secondary"
        draft = f'{len(rep.get("moments") or [])} / {len(rep.get("open_items") or [])}' if rep else "—"
        row = (f'<tr>{scope_cell(cid, vendor)}<td><span class="badge {badge}">{esc(info["label"])}</span>'
               f'{f"<small>{esc(mode)}</small>" if mode else ""}</td>'
               f'<td>{_clock(info["started"], z)}</td><td>{_minutes(info["elapsed"], z)}</td>'
               f'<td>{c.get("turns", 0)} / {c.get("tools", 0)}</td><td>{draft}</td>'
               f'<td><a href="/ai/run?{q}&lang={lang}">{"查看" if z else "Open"}</a></td></tr>')
        (rows_live if info["live"] else rows_done).append(row)
    head = (f'<thead><tr><th>{"活动 / 渠道" if z else "Campaign / channel"}</th><th>{"状态" if z else "Status"}</th>'
            f'<th>{"开始" if z else "Started"}</th><th>{"用时" if z else "Time"}</th>'
            f'<th>{"轮数 / 工具调用" if z else "Turns / tool calls"}</th><th>{"草稿 节点 / 事项" if z else "Draft moments / items"}</th><th></th></tr></thead>')
    empty = f'<tr><td colspan="7" class="empty">{"没有记录。" if z else "Nothing here."}</td></tr>'

    def card(title, rows):
        return (f'<section class="card mb-3"><div class="card-body"><div class="d-flex justify-content-between align-items-baseline mb-2">'
                f'<h2 class="h6 mb-0">{title}</h2><span class="count">{len(rows)}</span></div>'
                f'<div class="table-responsive"><table class="table table-hover align-middle mb-0">{head}<tbody>{"".join(rows) or empty}</tbody></table></div></div></section>')
    title = "AI 运行状态" if z else "AI run status"
    body = (_header(title, "每个回放的 AI 智能体正在做什么" if z else "What each replay's AI agent is doing", lang,
                    f'<a href="/?lang={lang}">{"返回首页" if z else "Back to home"}</a>')
            + f'<div id="ai-body">{card("进行中" if z else "Running", rows_live)}{card("最近的运行" if z else "Recent runs", rows_done)}</div>')
    script = """<script>(()=>{let t=setInterval(()=>{if(document.hidden)return;fetch(location.href,{credentials:'same-origin'}).then(r=>r.text()).then(h=>{
const d=new DOMParser().parseFromString(h,'text/html').getElementById('ai-body');if(d)document.getElementById('ai-body').innerHTML=d.innerHTML;}).catch(()=>{});},5000);})();</script>"""
    return _shell(title, body, lang, css, script)


def _item_html(item, z):
    kind = item["kind"]
    when = _clock(item.get("ts"), z)
    if kind == "ai" and not item.get("text") and not item.get("error") and item.get("thinking"):
        think = item["thinking"].strip()
        if item.get("live"):  # 正在想：直接把最新想到的摊开，这正是「现在在干啥」
            return (f'<div class="left"><div class="who">{"AI · 正在思考" if z else "AI · thinking"} · {when}</div>'
                    f'<div class="bubble live text-body-secondary">{esc(think[-1500:])}</div></div>')
        first = " ".join(think.split())[:120]  # 只想没说：压成一行，点开看全文
        return (f'<div class="mid"><details><summary>{"AI 思考" if z else "AI thinking"} · {when} · {esc(first)}</summary>'
                f'<pre>{esc(think[-20000:])}</pre></details></div>')
    if kind == "ai":
        think = (f'<details class="think"><summary>{"思考过程" if z else "Thinking"}</summary><pre>{esc(item["thinking"][-20000:])}</pre></details>'
                 if item.get("thinking") else "")
        err = f'<div class="state-error small mt-1">{esc(item["error"])}</div>' if item.get("error") else ""
        text = esc(item["text"]) if item.get("text") else f'<span class="text-body-secondary">{"（只有思考，没有说话）" if z else "(thinking only)"}</span>'
        live = " live" if item.get("live") else ""
        who = ("AI · 正在输出" if z else "AI · writing") if item.get("live") else "AI"
        return f'<div class="left"><div class="who">{who} · {when}</div><div class="bubble{live}">{text}{think}{err}</div></div>'
    if kind == "review":
        ok = item.get("decision") == "pass"
        verdict = ("通过" if ok else "打回") if z else ("Passed" if ok else "Sent back")
        sugg = "".join(f'<li>{esc(str(s.get("target", "")))}：{esc(str(s.get("problem", "")))}</li>' for s in item["suggestions"])
        sugg = f'<details class="think"><summary>{"打回意见" if z else "Suggestions"}（{len(item["suggestions"])}）</summary><ul class="mb-0">{sugg}</ul></details>' if sugg else ""
        return (f'<div class="left"><div class="who">{"审核" if z else "Review"} · {"第" if z else "round "}{item["round"]}{"轮" if z else ""} · {when}</div>'
                f'<div class="bubble"><b>{verdict}</b>\n{esc(item["text"])}{sugg}</div></div>')
    if kind == "program":
        first = item["text"].strip().splitlines()[0][:120] if item["text"].strip() else ""
        return (f'<div class="mid"><details><summary>{"程序提示" if z else "Program message"} · {when} · {esc(first)}</summary>'
                f'<pre>{esc(item["text"][:8000])}</pre></details></div>')
    label = TOOL_LABEL.get(item["name"], (None, None))[0 if z else 1]
    if not label:
        label = ("后台 " if z else "Admin ") + item["name"].replace("mcp__femini__", "") if item["name"].startswith("mcp__femini__") else item["name"]
    state = {"running": "进行中" if z else "running", "ok": "完成" if z else "done", "error": "报错" if z else "error"}[item["state"]]
    cls = " state-error" if item["state"] == "error" else ""
    ms = f' · {int(item["ms"]) / 1000:.1f}s' if str(item.get("ms") or "").replace(".", "").isdigit() else ""
    args = json.dumps(item.get("args"), ensure_ascii=False, indent=1)[:3000] if isinstance(item.get("args"), (dict, list)) else str(item.get("args"))[:3000]
    res = f'<pre>{esc(item.get("result") or "")}</pre>' if item.get("result") else ""
    return (f'<div class="mid"><details><summary class="{cls.strip()}">{"工具" if z else "Tool"} · {esc(label)} · {esc(item["summary"][:140])} · {state}{ms}</summary>'
            f'<pre>{esc(args)}</pre>{res}</details></div>')


def run_page(cid, vendor, run, lang, css="", name=None, channel=None):
    z = lang == "zh"
    s = _json(run / "run.json", {}) or {}
    label, live = phase(cid, vendor, run, s, z)
    items, counts = _read_events(run)
    items = items + _reviews(run, s)
    items.sort(key=lambda x: x.get("ts") or 0)
    shown = items[-MAX_ITEMS:]
    if live:
        cur = _live(run)
        if cur:
            cur["live"] = True
            cur["ts"] = _now()
            if cur.get("thinking") and not cur.get("text"):
                cur["thinking"] = cur["thinking"][-4000:]
            shown.append(cur)
    rep = _report(run)
    brief = _json(run / "work/seed/brief.json", {}) or {}
    mode = {"update": "增量更新" if z else "Incremental update", "full": "完整调查" if z else "Full investigation"}.get(brief.get("mode"), "")
    sub = (channel(cid, vendor, lang) if channel else "") or (f"#{vendor}" if vendor else "")
    title = f'{(name or (lambda n: n))(cid) or cid}' + (f" · {sub}" if sub else "")
    model = s.get("model") or ""
    kv = [("状态" if z else "Status", f'<span class="badge {"text-bg-primary" if live else "text-bg-secondary"}">{esc(label)}</span>'),
          ("方式" if z else "Mode", esc(mode) or "—"),
          ("开始" if z else "Started", _clock(_ts(s.get("started_at")), z)),
          ("用时" if z else "Time", _minutes(_elapsed(s, run, live), z)),
          ("模型" if z else "Model", esc(model.split("/")[-1])),
          ("轮数" if z else "Turns", str(counts.get("turns", s.get("turns") or 0))),
          ("工具调用 / 报错" if z else "Tool calls / errors", f'{counts.get("tools", 0)} / {counts.get("errors", 0)}'),
          ("草稿 节点 / 事项" if z else "Draft moments / items",
           f'{len(rep.get("moments") or [])} / {len(rep.get("open_items") or [])}'
           + (f' · {esc(REPORT_STATUS.get(rep.get("status"), (str(rep.get("status")),) * 2)[0 if z else 1])}' if rep.get("status") else "") if rep else "—"),
          ("还没查完" if z else "Not yet covered", str(len((rep.get("coverage") or {}).get("remaining") or [])) if rep else "—")]
    kv_html = "".join(f"<div><small>{k}</small>{v}</div>" for k, v in kv)
    progress = (run / "work/notes/progress.md")
    progress_html = ""
    if progress.is_file():
        text = progress.read_text(errors="replace")[-12000:]
        progress_html = (f'<details class="mt-3"><summary>{"AI 自己写的进度记录" if z else "Progress notes written by the AI"}</summary>'
                         f'<pre class="small mt-2" style="white-space:pre-wrap">{esc(text)}</pre></details>')
    older = len(items) - len(shown) + (1 if live and shown and shown[-1].get("live") else 0)
    older_note = f'<div class="text-center small text-body-secondary">{"更早的" if z else "Earlier"} {older} {"条未显示" if z else "entries hidden"}</div>' if older > 0 else ""
    chat = "".join(_item_html(i, z) for i in shown) or f'<div class="text-body-secondary">{"还没有记录。" if z else "Nothing yet."}</div>'
    q = f"c={cid}&run={run.name}&" + (f"vendor={vendor}&" if vendor else "")
    body = (_header(title, ("AI 运行详情 · " if z else "AI run · ") + run.name, lang,
                    f'<a href="/ai?lang={lang}">{"返回 AI 运行状态" if z else "Back to AI run status"}</a>', extra_query=q)
            + f'<div id="ai-body"><section class="card mb-3"><div class="card-body"><div class="kv">{kv_html}</div>{progress_html}</div></section>'
            + f'<section class="card"><div class="card-body"><div class="chat">{older_note}{chat}</div></div></section></div>')
    script = "" if not live else """<script>(()=>{setInterval(()=>{if(document.hidden)return;
const atBottom=(innerHeight+scrollY)>=document.body.scrollHeight-80;const open=[...document.querySelectorAll('#ai-body details[open]')].length;
if(open)return;fetch(location.href,{credentials:'same-origin'}).then(r=>r.text()).then(h=>{const d=new DOMParser().parseFromString(h,'text/html').getElementById('ai-body');
if(d){document.getElementById('ai-body').innerHTML=d.innerHTML;if(atBottom)scrollTo(0,document.body.scrollHeight);}}).catch(()=>{});},5000);
scrollTo(0,document.body.scrollHeight);})();</script>"""
    return _shell(title, body, lang, css, script)
