"""Spend 录入异常字典：种子 + 线上学到的内容，排查越多越准。

- 种子：config/spend_anomaly_seed.json（入库，全部来自真实案例）；
- 线上：knowledge/spend_dictionary.json（不入库，代码同步时排除，发布不会覆盖）。第一次读时由种子生成；
  之后代码里新增的种子条目会自动并进来，线上已有的条目以线上为准。
- 每次改动都追加一行到 knowledge/changelog.jsonl（谁、为什么、改了哪些条目）。
- 全程不需要人：学习规则（record）由验证结果驱动——「一致」= 确认一次，加例子和用户当时的说法；「部分一致」只记部分；
  「矛盾」= 否决一次；到期仍没有任何依据 = 未验证。验证依据是排查日之后的后台改动、之后的 Slack / HubSpot / 邮件和 GitHub
  （实际使用时 GitHub 里多半还没有这个异常，所以不能只靠它）。每条结论只记一次，判断变了先撤销（unrecord）再记。
  种子 / 候选确认满 2 次转为正式；否决满 3 次且否决多于确认的两倍，标为停用，不再进提示词。
- 结构由自动整理（replay/curate.py）决定：MiniMax 审候选条目，保留 / 合并（merge_into）/ 删掉（drop），程序把关并用回归集兜底，
  变差就回滚。合并、删掉的条目不真删，留理由可追溯；排查记录里引用的旧 id 会顺着合并找到最终条目（resolve）。
- 排查时给模型看的字典（prompt_view）会隐藏「本案」的例子：同一条链接、日期前后 30 天内、来源是 GitHub 或由排查学来的例子，
  否则等于把答案递给模型，回归集也就测不出真实水平。
"""
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re

from .util import ROOT, now_z, read_json, write_json

SEED = ROOT / "config" / "spend_anomaly_seed.json"
KNOWLEDGE = pathlib.Path(os.environ.get("REPLAY_KNOWLEDGE", str(ROOT / "knowledge")))
STATUSES = ("seed", "candidate", "confirmed", "deprecated", "merged")
PROMOTE_AT = 2           # 确认几次转为正式
DEPRECATE_AT = 3         # 否决几次（且否决 > 2 × 确认）停用
HIDE_DAYS = 30           # 本案 GitHub 例子的隐藏范围
MAX_EXAMPLES = 8
MAX_PHRASES = 40
VERDICTS = ("consistent", "partial", "contradict", "none")


def dict_path():
    return KNOWLEDGE / "spend_dictionary.json"


def changelog_path():
    return KNOWLEDGE / "changelog.jsonl"


def cases_dir():
    return KNOWLEDGE / "cases"


def _blank_stats():
    return dict(hits=0, confirmed=0, partial=0, rejected=0, unverified=0, last_seen=None)


def _norm_entry(e, source="slack"):
    e = copy.deepcopy(e)
    e.setdefault("source", source)
    e.setdefault("status", "seed" if source == "slack" else "candidate")
    e.setdefault("signals", [])
    e.setdefault("alert_patterns", [])
    e.setdefault("phrases", [])
    e.setdefault("examples", [])
    e["stats"] = {**_blank_stats(), **(e.get("stats") or {})}
    return e


def load():
    """→ 字典文档 {version, categories, entries:[...], updated_at}。线上文件不存在时由种子生成（不落盘，等第一次 save）。"""
    seed = read_json(SEED) or {"entries": [], "categories": {}}
    live = read_json(dict_path())
    if not live:
        return dict(version=seed.get("version", 1), categories=seed.get("categories", {}), updated_at=None,
                    entries=[_norm_entry(e) for e in seed.get("entries", [])])
    have = {e["id"] for e in live.get("entries", [])}
    live["entries"] = [_norm_entry(e, e.get("source", "slack")) for e in live.get("entries", [])]
    live["entries"] += [_norm_entry(e) for e in seed.get("entries", []) if e["id"] not in have]
    live["categories"] = {**seed.get("categories", {}), **live.get("categories", {})}
    return live


@contextlib.contextmanager
def locked():
    """读-改-写字典时持有的文件锁（排查学习、回访、建字典、自动整理可能同时发生，进程之间也生效）。"""
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    with open(KNOWLEDGE / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save(doc, note, changed=(), actor="system"):
    doc["updated_at"] = now_z()
    write_json(dict_path(), doc)
    changelog_path().parent.mkdir(parents=True, exist_ok=True)
    with changelog_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(ts=doc["updated_at"], actor=actor, note=note, entries=list(changed)), ensure_ascii=False) + "\n")


def changelog(limit=50):
    p = changelog_path()
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(x) for x in reversed(lines) if x.strip()]


def entry(doc, eid):
    return next((e for e in doc["entries"] if e["id"] == eid), None)


def active(doc):
    """模型能用的条目：停用的、已合并进别的条目的都不算。"""
    return [e for e in doc["entries"] if e.get("status") not in ("deprecated", "merged")]


def _days_apart(a, b):
    try:
        return abs((dt.date.fromisoformat(str(a)[:10]) - dt.date.fromisoformat(str(b)[:10])).days)
    except ValueError:
        return 10 ** 6


def hidden(example, links, date):
    """本案的例子：来源是 GitHub 或由排查学来（带 case），链接重合、日期在前后 HIDE_DAYS 天内。"""
    if example.get("source") != "github" and not example.get("case"):
        return False
    sc = example.get("scope") or {}
    return bool(set(map(str, sc.get("links") or [])) & set(map(str, links))) and _days_apart(sc.get("date"), date) <= HIDE_DAYS


def score(e):
    s = e["stats"]
    return s["confirmed"] * 2 + s["partial"] - s["rejected"] * 2


def prompt_view(doc, links, date, boost=()):
    """给模型看的字典：停用的不给；本案 GitHub 例子隐藏；按「用户说法命中 → 战绩」排序。"""
    out = []
    for e in sorted(active(doc), key=lambda e: (e["id"] not in boost, -score(e), e["id"])):
        ex = [x for x in e.get("examples", []) if not hidden(x, links, date)][:2]
        s = e["stats"]
        out.append(dict(id=e["id"], category=e["category"], status=e["status"], name=e.get("name_en"), description=e.get("desc_en"),
                        data_signals=e.get("signals", []), alert_patterns=e.get("alert_patterns", []), phrases=e.get("phrases", [])[:15],
                        config_target=e.get("config_target"), fix=e.get("fix_en"),
                        track_record=f"confirmed {s['confirmed']}, partial {s['partial']}, rejected {s['rejected']}",
                        examples=[dict(source=x.get("source"), quote=x.get("quote"), when=(x.get("scope") or {}).get("date")) for x in ex]))
    return out


def _phrase_hit(phrase, text):
    try:
        return bool(re.search(phrase, text, re.I))
    except re.error:
        return phrase.lower() in text.lower()


def match_phrases(doc, text):
    """用户描述里命中了哪些条目的说法 → {条目: 命中的说法}。"""
    if not (text or "").strip():
        return {}
    hits = {}
    for e in active(doc):
        got = [p for p in e.get("phrases", []) if len(p) >= 2 and _phrase_hit(p, text)]
        if got:
            hits[e["id"]] = got
    return hits


def _add_unique(lst, item, key, cap):
    if any(key(x) == key(item) for x in lst):
        return False
    lst.append(item)
    del lst[:-cap]
    return True


def record(doc, eid, verdict, case_ref, phrase=None, example=None, when=None):
    """按一次验证结果更新条目统计；返回条目状态是否变化。条目已被合并的，记到合并后的条目上。"""
    e = entry(doc, resolve(doc, eid))
    if not e or verdict not in VERDICTS:
        return False
    s, before = e["stats"], e["status"]
    s["hits"] += 1
    s["last_seen"] = when or now_z()
    if verdict == "consistent":
        s["confirmed"] += 1
        if example:
            _add_unique(e["examples"], {**example, "case": case_ref}, lambda x: (x.get("quote"), x.get("source")), MAX_EXAMPLES)
        if phrase and len(phrase.strip()) >= 2:
            _add_unique(e["phrases"], phrase.strip()[:60], lambda x: x.lower(), MAX_PHRASES)
    elif verdict == "partial":
        s["partial"] += 1
    elif verdict == "contradict":
        s["rejected"] += 1
    else:
        s["unverified"] += 1
    _restatus(e)
    return e["status"] != before


STAT_OF = dict(consistent="confirmed", partial="partial", contradict="rejected", none="unverified")


def unrecord(doc, eid, verdict):
    """撤销一次记录（同一条结论换了判断：回访补到了依据、重新排查）。例子和说法不撤。返回状态是否变化。"""
    e = entry(doc, resolve(doc, eid))
    if not e or verdict not in STAT_OF:
        return False
    s, before = e["stats"], e["status"]
    s[STAT_OF[verdict]] = max(0, s[STAT_OF[verdict]] - 1)
    s["hits"] = max(0, s["hits"] - 1)
    _restatus(e)
    return e["status"] != before


def _restatus(e):
    """状态完全由战绩决定：否决多 → 停用；确认满 PROMOTE_AT → 正式；否则回到出身（种子 / 候选）。
    自动整理合并掉或删掉的条目不再由战绩改回来。"""
    if e.get("status") == "merged" or (e.get("curation") or {}).get("action") == "drop":
        return
    s = e["stats"]
    if s["rejected"] >= DEPRECATE_AT and s["rejected"] > 2 * s["confirmed"]:
        e["status"] = "deprecated"
    elif s["confirmed"] >= PROMOTE_AT:
        e["status"] = "confirmed"
    else:
        e["status"] = "seed" if e.get("source") == "slack" else "candidate"


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s[:40] or "pattern"


def add_candidate(doc, data, source, case_ref=None):
    """新模式 → 候选条目（同名的不重复加，只补例子）。返回条目 id。"""
    name_en = _en_only(data.get("name_en") or data.get("name") or "")
    key = (name_en or (data.get("name_zh") or "")).strip()
    if not key:
        return None
    for e in doc["entries"]:
        if key.lower() in ((e.get("name_en") or "").strip().lower(), (e.get("name_zh") or "").strip().lower()):
            for x in data.get("examples") or []:
                _add_unique(e["examples"], {**x, "case": case_ref}, lambda y: (y.get("quote"), y.get("source")), MAX_EXAMPLES)
            return e["id"]
    base = "cand." + (_slug(name_en) if name_en else hashlib.sha1(key.encode()).hexdigest()[:8])
    eid, i = base, 2
    while entry(doc, eid):
        eid, i = f"{base}_{i}", i + 1
    cat = data.get("category") if data.get("category") in doc.get("categories", {}) else "config"
    e = _norm_entry(dict(
        id=eid, category=cat, name_zh=data.get("name_zh") or name_en, name_en=name_en,
        desc_zh=data.get("desc_zh") or "", desc_en=data.get("desc_en") or "",
        signals=[x for x in data.get("signals") or [] if isinstance(x, str)][:6],
        alert_patterns=[x for x in data.get("alert_patterns") or [] if isinstance(x, str) and _valid_re(x)][:6],
        phrases=[x for x in data.get("phrases") or [] if isinstance(x, str)][:15],
        config_target=data.get("config_target") or "", fix_zh=data.get("fix_zh") or "", fix_en=data.get("fix_en") or "",
        owner_role=data.get("owner_role") or "", examples=[{**x, "case": case_ref} for x in (data.get("examples") or [])][:MAX_EXAMPLES],
        status="candidate", source=source), source)
    doc["entries"].append(e)
    return eid


def _en_only(s):
    s = str(s or "").strip()
    return "" if re.search(r"[\u3400-\u9fff]", s) else s


def _valid_re(p):
    try:
        re.compile(p)
        return True
    except re.error:
        return False


def summary(doc):
    """字典页用：每类条目数、各状态数、总确认 / 否决。"""
    by_status, by_cat = {}, {}
    for e in doc["entries"]:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
    tot = {k: sum(e["stats"][k] for e in doc["entries"]) for k in ("hits", "confirmed", "partial", "rejected", "unverified")}
    return dict(entries=len(doc["entries"]), by_status=by_status, by_category=by_cat, totals=tot, updated_at=doc.get("updated_at"))


# ───────────────────────── 回归集 ─────────────────────────
def save_case(case_id, data):
    """GitHub 验证为一致的案例存进回归集：原因阶段的完整输入（证据包、信号、用户描述）和当时确认的归类。"""
    write_json(cases_dir() / f"{case_id}.json", data)


def list_cases():
    d = cases_dir()
    return [read_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []


def regression_path():
    return KNOWLEDGE / "regression.json"


# ───────────────────────── 自动整理（replay/curate.py 调用） ─────────────────────────
def merge_into(doc, src, dst, reason, when=None):
    """把条目 src 并进 dst：例子、说法、告警关键词、战绩都挪过去；src 标为已合并（不删，留着可追溯）。"""
    a, b = entry(doc, src), entry(doc, dst)
    if not a or not b or a is b:
        return False
    for x in a.get("examples", []):
        _add_unique(b["examples"], x, lambda y: (y.get("quote"), y.get("source")), MAX_EXAMPLES)
    for ph in a.get("phrases", []):
        _add_unique(b["phrases"], ph, lambda y: y.lower(), MAX_PHRASES)
    for ap in a.get("alert_patterns", []):
        _add_unique(b["alert_patterns"], ap, lambda y: y, 12)
    for k in ("hits", "confirmed", "partial", "rejected", "unverified"):
        b["stats"][k] += a["stats"][k]
    a["status"], a["merged_into"] = "merged", dst
    a["curation"] = dict(action="merge", into=dst, reason=reason, at=when or now_z())
    _restatus(b)
    return True


def drop(doc, eid, reason, when=None):
    """自动整理认定不是 spend 录入错误（或太空泛没法用）：停用，留理由。"""
    e = entry(doc, eid)
    if not e:
        return False
    e["status"] = "deprecated"
    e["curation"] = dict(action="drop", reason=reason, at=when or now_z())
    return True


def resolve(doc, eid):
    """顺着「已合并」找到最终的条目（排查记录里引用的旧 id 也能对上）。"""
    seen = set()
    e = entry(doc, eid)
    while e and e.get("status") == "merged" and e.get("merged_into") and e["id"] not in seen:
        seen.add(e["id"])
        e = entry(doc, e["merged_into"])
    return e["id"] if e else eid
