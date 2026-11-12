"""结果工具：AI 通过工具一条条增、改、删、查回放结果，不直接读写 JSON 文件（agent/online.ts 调用）。

结果仍存为 work/output/report.json（发布、审核、状态页照旧读它），但 AI 看不到这个文件：
- 节点（moments）、后台事实（facts）、进行中事项（items）各有编号 M001 / F001 / I001，按时间自动排序；
- 程序不核对原话（用户定的：只在提示词里要求写原文，后台不核对）；只检查页面渲染离不开的格式（来源类型、时间能识别、有原话）；
- 概要、现状总结（state_brief，给下一次刷新的 AI 看，不上页面）、阶段等用 set 写；用 finish 标记完成。

命令行：python -m replay.pi_report '{"op":"add","work":"/path/to/work","type":"moment","data":{...}}'，输出一行 JSON。
"""
import contextlib
import datetime as dt
import fcntl
import json
import pathlib
import re
import sys

from . import business, progress, util
from .pi_result import stamp

KINDS = ("slack", "email", "meeting", "hubspot", "github")
PREFIX = {"moment": "M", "fact": "F", "item": "I"}
GROUP = {"moment": "moments", "fact": "facts", "item": "open_items"}
SETTABLE = {"title_zh": str, "title_en": str, "summary_zh": str, "summary_en": str, "state_brief": str,
            "chapters": list, "people": list, "workstreams": list, "backend_changes": list, "handoffs": list,
            "unresolved": list, "coverage": dict}
EMPTY = {"status": "draft", "title_zh": "", "title_en": "", "summary_zh": "", "summary_en": "", "state_brief": "",
         "chapters": [], "moments": [], "backend_changes": [], "handoffs": [], "facts": [], "people": [],
         "workstreams": [], "open_items": [], "unresolved": [], "coverage": {}}


def path_of(work):
    return pathlib.Path(work) / "output/report.json"


@contextlib.contextmanager
def opened(work):
    """加锁读写（同一轮里可能并行调用好几个工具）。"""
    p = path_of(work)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.parent / ".report.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = load(work)
        holder = {"report": report, "dirty": False}
        yield holder
        if holder["dirty"]:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(holder["report"], ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)


def load(work):
    from .pi_result import load_report
    report = load_report(path_of(work)) if path_of(work).exists() else None
    report = {**EMPTY, **(report if isinstance(report, dict) else {})}
    ensure_ids(report)
    return report


def ensure_ids(report):
    """给没有编号的条目（例如上次的结果）补编号。"""
    for kind, group in GROUP.items():
        items = report.get(group) or []
        used = {str(x.get("id")) for x in items if isinstance(x, dict) and x.get("id")}
        n = max([int(i[1:]) for i in used if re.fullmatch(PREFIX[kind] + r"\d+", i)] or [0])
        for x in items:
            if isinstance(x, dict) and not x.get("id"):
                n += 1
                x["id"] = f"{PREFIX[kind]}{n:03d}"
        report[group] = items


def _next_id(report, kind):
    ids = [str(x.get("id")) for x in report[GROUP[kind]]] + [str(x.get("id")) for x in report.get("deleted") or []]
    nums = [int(i[1:]) for i in ids if re.fullmatch(PREFIX[kind] + r"\d+", i)]  # 删掉的编号也不再用
    return f"{PREFIX[kind]}{(max(nums or [0]) + 1):03d}"


def _sort(report, kind):
    def key(x):
        try:
            return stamp(x.get("ts") if kind != "item" else max((e.get("ts") for e in x.get("evidence") or []), default=""))[0]
        except (TypeError, ValueError, KeyError):
            return "9999"
    report[GROUP[kind]].sort(key=key)


def _need(data, fields):
    missing = [f for f in fields if not (isinstance(data.get(f), str) and data[f].strip())]
    if missing:
        raise ValueError("缺少必填项：" + "、".join(missing))




def _time(value, what):
    try:
        stamp(value)
    except (TypeError, ValueError, KeyError):
        raise ValueError(f"{what}时间无法识别（用原始记录的时间，ISO 格式，例如 2026-06-01T09:00:00Z；只知道日期就写 2026-06-01）")


def _cite(work, quote, file):
    """原话照 AI 写的存，不核对；AI 说了来自哪个查询结果就一并记下。"""
    ev = {"quote": quote.strip()}
    if file:
        ev["file"] = str(file)
    return ev, "stored"


def _lift(data):
    """原话按存储格式写在 evidence.quote 里（看旧条目时就是这样）也认，当作 quote（和 from_file）。"""
    ev = data.get("evidence")
    if not str(data.get("quote") or "").strip() and isinstance(ev, dict) and str(ev.get("quote") or "").strip():
        data = {**data, "quote": ev["quote"]}
        if ev.get("file") and not data.get("from_file"):
            data["from_file"] = ev["file"]
    return data


def _with_old(old, data):
    """修改时：旧条目的字段打底（原话存在 evidence.quote 里），再盖上这次要改的字段。"""
    base = dict(old or {})
    if old and isinstance(old.get("evidence"), dict):
        base["quote"] = old["evidence"].get("quote")
    return {**base, **data}


def build_moment(work, data, old=None):
    data = _lift(data)
    d = _with_old(old, data)
    _need(d, ["kind", "ts", "speaker", "quote"])
    if not (d.get("action_zh") or d.get("action_en")):
        raise ValueError("缺少必填项：action_zh 或 action_en（这条节点在页面上的标题）")
    if d["kind"] not in KINDS:
        raise ValueError("kind 只能是 " + "/".join(KINDS))
    note = ""
    if d.get("scope") and d["scope"] not in business.SCOPES:
        note = f"（scope「{d['scope']}」不是节点的取值，已按 case 处理）"
        d["scope"] = "case"
    _time(d["ts"], "节点")
    m ={k: d.get(k) for k in ("id", "kind", "speaker", "ts", "source", "scope", "belongs_because", "action_zh", "action_en",
                                "context_zh", "context_en", "translation", "to")}
    m["scope"] = d.get("scope") or "case"
    m["to"] = d.get("to") or []
    if "quote" in data or "from_file" in data or not old:
        m["evidence"], _ = _cite(work, d["quote"], d.get("from_file"))
    else:
        m["evidence"] = old["evidence"]
    return {k: v for k, v in m.items() if v is not None}, note


def build_fact(work, data, old=None):
    data = _lift(data)
    d = _with_old(old, data)
    _need(d, ["ts", "quote"])
    if not (d.get("title_zh") or d.get("title_en")):
        raise ValueError("缺少必填项：title_zh 或 title_en")
    _time(d["ts"], "后台事实")
    f = {k: d.get(k) for k in ("id", "ts", "actor", "title_zh", "title_en", "context_zh", "context_en")}
    f["actor"] = d.get("actor") or ""
    if "quote" in data or "from_file" in data or not old:
        f["evidence"], _ = _cite(work, d["quote"], d.get("from_file"))
    else:
        f["evidence"] = old["evidence"]
    return {k: v for k, v in f.items() if v is not None}, ""


def build_item(work, data, old=None):
    d = {**(old or {}), **data}
    if not (d.get("topic") or d.get("topic_en")):
        raise ValueError("缺少必填项：topic 或 topic_en")
    if d.get("status") not in progress.STATUSES:
        raise ValueError("status 只能是 " + "/".join(progress.STATUSES))
    if d.get("scope", "this_campaign") not in ("this_campaign", "client_general", "unclear"):
        raise ValueError("scope 只能是 this_campaign/client_general/unclear")
    basis = d.get("owner_basis") or "none"
    if basis not in progress.BASIS:
        raise ValueError("owner_basis 只能是 " + "/".join(progress.BASIS) + "（没有明确负责人就写 none）")
    it = {k: d.get(k) for k in ("id", "topic", "topic_en", "status", "state_note", "state_note_en", "next_step",
                                 "next_step_en", "next_owner", "confidence")}
    it.update(scope=d.get("scope") or "this_campaign", owner_basis=basis,
              next_owner="待确认" if basis == "none" else (d.get("next_owner") or "待确认"))
    if "evidence" in data or not old:
        ev = d.get("evidence")
        if not isinstance(ev, list) or not ev:
            raise ValueError("事项至少要有一条原话证据（evidence 列表，每条含 quote、speaker、ts）")
        out = []
        for i, e in enumerate(ev, 1):
            if not isinstance(e, dict):
                raise ValueError(f"第 {i} 条证据格式不对")
            _need(e, ["quote", "speaker", "ts"])
            _time(e["ts"], f"第 {i} 条证据")
            cite, _ = _cite(work, e["quote"], e.get("from_file"))
            out.append({**cite, "ts": e["ts"], "speaker": e["speaker"]})
        it["evidence"] = out
    else:
        it["evidence"] = old["evidence"]
    return {k: v for k, v in it.items() if v is not None}, ""


BUILD = {"moment": build_moment, "fact": build_fact, "item": build_item}


def _kind_of(ident):
    for kind, p in PREFIX.items():
        if re.fullmatch(p + r"\d+", str(ident or "")):
            return kind
    raise ValueError("编号不对：节点是 M001 这样，后台事实 F001，事项 I001（用 report_find 查编号）")


def _find_entry(report, ident):
    kind = _kind_of(ident)
    for i, x in enumerate(report[GROUP[kind]]):
        if str(x.get("id")) == str(ident):
            return kind, i, x
    raise ValueError(f"没有编号 {ident}（用 report_find 查现有编号）")


def _line(kind, x):
    if kind == "moment":
        q = (x.get("evidence") or {}).get("quote") or ""
        return f"{x.get('id')} | {str(x.get('ts'))[:16]} | {x.get('kind')} {x.get('source') or ''} | {x.get('speaker')} | {x.get('action_zh')} | 「{q}」"
    if kind == "fact":
        return f"{x.get('id')} | {str(x.get('ts'))[:16]} | 后台 | {x.get('title_zh')}"
    last = max((str(e.get("ts")) for e in x.get("evidence") or []), default="")[:10]
    return f"{x.get('id')} | {x.get('status')} | 最近 {last} | {x.get('topic')} | 下一步：{x.get('next_step')}（{x.get('next_owner')}）"


# ---------- 操作 ----------

def _batch(args, single):
    """一次一条（type/data 或 id/data）或一批（items 列表）。"""
    items = args.get("items")
    if items is None:
        return [single]
    if not isinstance(items, list) or not items:
        raise ValueError("items 要是一个非空列表")
    return items


def _apply(work, entries, one):
    """逐条处理，成功的写入，失败的说明原因；全部失败才算这次调用出错。"""
    lines, ok = [], 0
    with opened(work) as h:
        for i, e in enumerate(entries, 1):
            try:
                if not isinstance(e, dict):
                    raise ValueError("格式不对")
                lines.append(one(h["report"], e))
                ok += 1
            except (ValueError, KeyError, TypeError) as exc:
                lines.append(f"{'第 %d 条' % i if len(entries) > 1 else ''}没写进去：{exc}")
        if ok:
            h["dirty"] = True
    if not ok:
        raise ValueError("\n".join(lines))
    return "\n".join(lines)


def op_add(work, args):
    def one(report, e):
        kind, data = e.get("type"), e.get("data") or {}
        if kind not in BUILD:
            raise ValueError("type 只能是 moment（关键节点）、fact（后台事实）、item（进行中事项）")
        entry, note = BUILD[kind](work, data)
        entry["id"] = _next_id(report, kind)
        report[GROUP[kind]].append(entry)
        _sort(report, kind)
        return f"已加入 {entry['id']}。{note}"
    return _apply(work, _batch(args, {"type": args.get("type"), "data": args.get("data")}), one)


def op_update(work, args):
    def one(report, e):
        ident, data = e.get("id"), e.get("data") or {}
        if not data:
            raise ValueError("data 里写要改的字段")
        kind, i, old = _find_entry(report, ident)
        entry, note = BUILD[kind](work, data, old)
        entry["id"] = old["id"]
        report[GROUP[kind]][i] = entry
        _sort(report, kind)
        return f"已修改 {ident}。{note}"
    return _apply(work, _batch(args, {"id": args.get("id"), "data": args.get("data")}), one)


def op_delete(work, args):
    ident, reason = args.get("id"), str(args.get("reason") or "").strip()
    with opened(work) as h:
        report = h["report"]
        kind, i, old = _find_entry(report, ident)
        report[GROUP[kind]].pop(i)
        report.setdefault("deleted", []).append({"id": ident, "reason": reason, "at": util.now_z(),
                                                 "summary": _line(kind, old)})
        h["dirty"] = True
    return f"已删除 {ident}" + (f"（原因：{reason}）。" if reason else "。")


def op_set(work, args):
    field, value = args.get("field"), args.get("value")
    if field not in SETTABLE:
        raise ValueError("field 只能是 " + "、".join(SETTABLE))
    if not isinstance(value, SETTABLE[field]):
        raise ValueError(f"{field} 应该是 {SETTABLE[field].__name__}")
    if field == "chapters":
        for c in value:
            if not (isinstance(c, dict) and c.get("name_zh") and c.get("name_en")):
                raise ValueError("每个阶段要有 name_zh、name_en、start（YYYY-MM-DD）")
            dt.date.fromisoformat(str(c.get("start")))
    with opened(work) as h:
        h["report"][field] = value
        h["dirty"] = True
    size = f"（{len(value)} 字）" if isinstance(value, str) else f"（{len(value)} 项）"
    return f"已写入 {field}{size}。"


def op_find(work, args):
    report = load(work)
    ident = args.get("id")
    if ident:
        kind, _, x = _find_entry(report, ident)
        return json.dumps(x, ensure_ascii=False, indent=1)
    kinds = [args["type"]] if args.get("type") in GROUP else list(GROUP)
    text = norm_text(args.get("text"))
    try:
        rx = re.compile(args["pattern"], re.I) if args.get("pattern") else None
    except re.error as exc:
        raise ValueError(f"pattern 不是合法的正则：{exc}")
    where = args.get("where") if isinstance(args.get("where"), dict) else {}
    since, until = str(args.get("since") or ""), str(args.get("until") or "")
    lines = []
    for kind in kinds:
        for x in report[GROUP[kind]]:
            ts = str(x.get("ts") or max((str(e.get("ts")) for e in x.get("evidence") or []), default=""))
            if since and ts[:len(since)] < since or until and ts[:len(until)] > until:
                continue
            blob = json.dumps(x, ensure_ascii=False)
            if text and text not in norm_text(blob):
                continue
            if rx and not rx.search(blob):
                continue
            if any(norm_text(v) not in norm_text(x.get(k) if not isinstance(x.get(k), (dict, list)) else json.dumps(x.get(k), ensure_ascii=False))
                   for k, v in where.items()):
                continue
            lines.append(_line(kind, x))
    head = (f"状态 {report.get('status')} | 节点 {len(report['moments'])} | 后台事实 {len(report['facts'])} | "
            f"事项 {len(report['open_items'])} | 阶段 {len(report.get('chapters') or [])} | "
            f"标题{'已写' if report.get('title_zh') else '未写'} | 概要{'已写' if report.get('summary_zh') else '未写'} | "
            f"现状总结 {len(report.get('state_brief') or '')} 字")
    if args.get("brief"):
        head += "\n\n现状总结：\n" + (report.get("state_brief") or "（还没有）")
    return head + "\n" + ("\n".join(lines) if lines else "（没有符合条件的条目）")


def norm_text(value):
    return " ".join(str(value or "").lower().split())


def op_finish(work, args):
    with opened(work) as h:
        report = h["report"]
        missing = []
        if not report["moments"]:
            missing.append("至少一个关键节点（report_add type=moment）")
        if not str(report.get("state_brief") or "").strip():
            missing.append("现状总结 state_brief（report_set）")
        if missing:
            raise ValueError("还不能完成，缺少：" + "、".join(missing))
        report["status"] = "complete"
        h["dirty"] = True
    return "已标记完成。"


def op_note(work, args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    name = {"evidence": "evidence.md", "progress": "progress.md"}.get(args.get("kind") or "progress")
    if not name:
        raise ValueError("kind 只能是 progress（进度、已查范围、下一步）或 evidence（证据、线索、判断依据）")
    p = pathlib.Path(work) / "notes" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"\n- [{util.now_z()}] {text}\n")
    return f"已记到 notes/{name}。"


OPS = {"add": op_add, "update": op_update, "delete": op_delete, "set": op_set, "find": op_find,
       "finish": op_finish, "note": op_note}


def run(args):
    work = pathlib.Path(args.get("work") or "")
    if not (work / "seed/brief.json").is_file():
        return {"ok": False, "error": "工作目录不对"}
    op = OPS.get(args.get("op"))
    if not op:
        return {"ok": False, "error": "未知操作"}
    try:
        return {"ok": True, "text": op(work, args)}
    except (ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.argv[1])), ensure_ascii=False))
