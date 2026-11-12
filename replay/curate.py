"""字典自动整理：全程不需要人。MiniMax 审候选条目，程序把关，回归集兜底。

为什么要整理：建字典和排查学习会不断加进候选条目，其中有重复的（换个说法的同一个原因）、有不是 spend 录入错误的
（告警噪音、监控容差、流程规范）。不整理，字典会越来越杂，模型反而更难选对。

做法：
1. MiniMax 看全部条目：「已确立」的（种子 / 正式）只作参照、不能改；对每条「候选」决定
   保留（可顺便把名字、说明写清楚）/ 合并到另一条（同一个根因、同一个修法）/ 删掉（不是 spend 录错，或太空泛）。
2. 程序把关：只认当前候选的 id；合并目标必须是有效条目且不是自己，顺着同一批合并找最终目标，绕圈或落到被删的就改成保留；
   已经被验证确认过（confirmed ≥ 1）的候选只能保留或合并，不能删；没有理由的决定不算；英文字段不能有汉字。
3. 执行：合并（例子、说法、告警词、战绩并过去）/ 删掉都不真删，留理由可追溯（knowledge.merge_into / drop）。
4. 回归集兜底：整理后用当前字典重跑回归集；命中率比整理前低，就自动回滚这次整理。
触发：建字典之后（强制）；排查学到新候选、服务每 30 分钟的巡检里（maybe：候选有变化且距上次 ≥ 6 小时才跑）。
报告写到 knowledge/curate_report.json，改动和回滚都记进 knowledge/changelog.jsonl。
"""
import contextlib
import copy
import fcntl
import hashlib
import json
import time

from . import diagnose_ai as A
from . import knowledge as K
from .util import now_z, read_json, write_json

MIN_INTERVAL = 6 * 3600
ACTIONS = ("keep", "merge", "drop")

PROMPT = """You maintain the dictionary of FAILURE PATTERNS that an AI investigator uses to explain why FeedMob's daily SPEND was entered wrongly (spend missing, wrong amount, wrong link, entered while paused, wrong margin or formula, missing mapping, partner report problems, job errors, manual edits not synced, fees missing, ...). The dictionary must stay clean so the investigator picks the right pattern.

You get ESTABLISHED entries (status seed or confirmed: proven, you cannot change them) and CANDIDATE entries (new, unproven). Decide for EVERY candidate:
- "keep": a distinct root cause that makes spend wrong and that an investigator can check and act on.
- "merge": the same root cause AND the same fix as another entry (established or candidate). Give "into" = that entry's id. Prefer merging into established entries. Do NOT merge entries that only share a symptom (e.g. "spend missing") but have different causes or fixes.
- "drop": not about spend being entered or calculated wrongly (alert noise, monitoring tolerance, triage backlog, process hygiene such as missing comments, client report timing, feature work), or too vague to check.
For "keep" you may rewrite name_zh / name_en / desc_zh / desc_en to be clearer with the same meaning; leave them empty to keep the current text.
reason_zh / reason_en: one sentence each. English fields must contain no Chinese characters.

Output one JSON object and nothing else:
{"decisions": [{"id": str, "action": "keep|merge|drop", "into": str|null, "reason_zh": str, "reason_en": str, "name_zh": str, "name_en": str, "desc_zh": str, "desc_en": str}]}"""


def state_path():
    return K.KNOWLEDGE / "curate_state.json"


def report_path():
    return K.KNOWLEDGE / "curate_report.json"


@contextlib.contextmanager
def _single():
    """同一时间只整理一次（服务巡检和排查结束可能同时触发）；忙就跳过。"""
    K.KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    fh = open(K.KNOWLEDGE / ".curate.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def candidates(doc):
    return [e for e in doc["entries"] if e.get("status") == "candidate"]


def fingerprint(doc):
    """候选集合（含各自例子数）有变化才需要再整理。"""
    key = sorted(f"{e['id']}:{len(e.get('examples', []))}" for e in candidates(doc))
    return hashlib.sha1("|".join(key).encode()).hexdigest()[:16]


def _view(e):
    s = e["stats"]
    return dict(id=e["id"], status=e["status"], category=e["category"], name_zh=e.get("name_zh"), name_en=e.get("name_en"),
                description=e.get("desc_en") or e.get("desc_zh"), config_target=e.get("config_target"), signals=e.get("signals", []),
                phrases=e.get("phrases", [])[:8], fix=e.get("fix_en") or e.get("fix_zh"),
                examples=[(x.get("quote") or "")[:200] for x in e.get("examples", [])[:2]],
                track_record=f"confirmed {s['confirmed']}, partial {s['partial']}, rejected {s['rejected']}")


def messages(doc):
    est = [_view(e) for e in K.active(doc) if e["status"] != "candidate"]
    cand = [_view(e) for e in candidates(doc)]
    user = "ESTABLISHED:\n" + json.dumps(est, ensure_ascii=False) + "\n\nCANDIDATES:\n" + json.dumps(cand, ensure_ascii=False)
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]


def check(doc, out):
    """模型的决定 → 程序认可的决定 {候选 id: 决定}。不合规的一律当「保留」。"""
    cand = {e["id"]: e for e in candidates(doc)}
    act = {e["id"] for e in K.active(doc)}
    raw = {}
    for d in out.get("decisions") or []:
        if not isinstance(d, dict) or d.get("id") not in cand or d.get("action") not in ACTIONS:
            continue
        if not str(d.get("reason_zh") or d.get("reason_en") or "").strip():
            continue
        raw[d["id"]] = d
    eff = {cid: ("keep" if d["action"] == "drop" and cand[cid]["stats"]["confirmed"] >= 1 else d["action"])
           for cid, d in raw.items()}                     # 被验证确认过的候选，不凭看法删

    def final(cid, seen):
        """顺着同一批里的合并找最终目标；目标无效 / 绕圈 / 要被删 → None。中间那条自己合并不成（会被保留）就停在它身上。"""
        into = raw[cid].get("into")
        if into in seen or into not in act or eff.get(into) == "drop":
            return None
        if eff.get(into) == "merge":
            return final(into, seen | {into}) or into
        return into

    plan = {}
    for cid, d in raw.items():
        a = eff[cid]
        into = final(cid, {cid}) if a == "merge" else None
        if a == "merge" and not into:
            a = "keep"
        plan[cid] = dict(action=a, into=into, reason_zh=str(d.get("reason_zh") or ""), reason_en=A._en(d.get("reason_en")),
                         **({k: (A._en(d.get(k)) if k.endswith("_en") else str(d.get(k) or "").strip())
                             for k in ("name_zh", "name_en", "desc_zh", "desc_en")} if a == "keep" else {}))
    return plan


def apply(doc, plan):
    changes, when = [], now_z()
    for cid, p in plan.items():
        reason = p["reason_zh"] or p["reason_en"]
        if p["action"] == "merge" and K.merge_into(doc, cid, p["into"], reason, when):
            changes.append(dict(entry=cid, action=f"合并到 {p['into']}", reason=reason))
        elif p["action"] == "drop" and K.drop(doc, cid, reason, when):
            changes.append(dict(entry=cid, action="删除（停用）", reason=reason))
        elif p["action"] == "keep":
            e = K.entry(doc, cid)
            renamed = [k for k in ("name_zh", "name_en", "desc_zh", "desc_en") if p.get(k) and p[k] != e.get(k)]
            for k in renamed:
                e[k] = p[k]
            e["curation"] = dict(action="keep", reason=reason, at=when)
            if renamed:
                changes.append(dict(entry=cid, action="保留并改写说明", reason=reason))
    return changes


def _last_rate():
    runs = (read_json(K.regression_path()) or {}).get("runs") or []
    return runs[-1]["hit_rate"] if runs else None


def run(log=print, force=False, ask=None, regress=None):
    """整理一次。ask / regress 可替换（测试用）。→ 报告或 None（不需要 / 正在整理）"""
    with _single() as ok:
        if not ok:
            return None
        doc = K.load()
        state = read_json(state_path()) or {}
        fp = fingerprint(doc)
        if not candidates(doc):
            return None
        if not force and (fp == state.get("fp") or time.time() - state.get("at", 0) < MIN_INTERVAL):
            return None
        t0 = time.time()
        out, usage = (ask or A.ask)(messages(doc))
        with K.locked():
            doc = K.load()
            before = copy.deepcopy(doc)
            plan = check(doc, out)
            changes = apply(doc, plan)
            if changes:
                K.save(doc, f"自动整理：{len(changes)} 处改动", [c["entry"] for c in changes], actor="curate")
            saved_at = doc.get("updated_at")
        report = dict(at=now_z(), candidates=len(plan), decisions={a: sum(1 for p in plan.values() if p["action"] == a) for a in ACTIONS},
                      changes=changes, usage=usage, rolled_back=False)
        if changes and K.list_cases():
            prev = _last_rate()
            if regress is None:
                from .diagnose import regress as regress_fn
            else:
                regress_fn = regress
            reg = regress_fn(log=log, force=True)
            report["regression"] = dict(before=prev, after=(reg or {}).get("hit_rate"))
            if reg and prev is not None and reg["hit_rate"] < prev:
                with K.locked():
                    if K.load().get("updated_at") == saved_at:          # 这期间没有别的改动才回滚，免得冲掉排查学到的东西
                        K.save(before, f"回滚自动整理：回归集命中率 {prev:.0%} → {reg['hit_rate']:.0%}", [c["entry"] for c in changes], actor="curate")
                        report["rolled_back"] = True
                    else:
                        report["rollback_skipped"] = "整理之后字典又有别的改动，没有自动回滚"
        report["seconds"] = round(time.time() - t0)
        write_json(report_path(), report)
        write_json(state_path(), dict(at=time.time(), fp=fingerprint(K.load())))
        log(f"自动整理：{report['decisions']}，改动 {len(changes)} 处" + ("，已回滚" if report["rolled_back"] else ""))
        return report


def maybe(log=print):
    """候选有变化且距上次 ≥ 6 小时才整理（排查结束、服务巡检时调用）；出错只记日志。"""
    try:
        return run(log=log)
    except Exception as e:
        log(f"自动整理失败：{str(e)[:150]}")
        return None
