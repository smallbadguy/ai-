"""P0 建字典：从 GitHub（feed-mob 组织）里和 spend 录入有关的单子挖出错误模式，并进字典（knowledge.py）。

GitHub 只在这里（建字典）和排查后的验证 / 修复状态里用；排查原因时不读 GitHub。
做法：
1. 按关键词搜近 12 个月的单子（只发 GET），去重；
2. 标题去掉人名标签、日期、编号后按模板粗分成簇，每簇挑最有代表性的几张（评论多、已关单的优先），总数有上限；
3. 代表单子补上最后几条评论，分批交给 MiniMax：抽出「让每日 spend 录错的模式」，能对上已有条目的标出条目 id，
   每个模式必须引用单子原话，程序逐字核对，对不上的丢；
4. 新模式再让模型合并一次同类项；
5. 并进字典：已有条目只补例子（来源 github，带链接编号和日期，排查时会对本案隐藏）和说法；新模式成为「候选」条目，
   等排查时被 GitHub 验证确认满 2 次才转正式。报告写到 knowledge/dict_build_report.json。
"""
import concurrent.futures
import datetime as dt
import json
import re
import time
from collections import defaultdict

from . import diagnose_ai as A
from . import knowledge as K
from .github import GH
from .util import now_z, write_json

QUERIES = ["spend in:title", "录入 in:title", "spend mapping", "line item spend", "margin spend", "spend job", "direct spend",
           "spend automation", "net spend in:title", "gross spend in:title", "spend 调整", "spend discrepancy", "spend request"]
PAGES = 3                  # 每个关键词最多取几页（每页 100）
MAX_ISSUES = 360           # 交给模型的代表单子总数
PER_CLUSTER = 3
BATCH = 24
WORKERS = 6

EXTRACT = """You build a knowledge base of FAILURE PATTERNS that make FeedMob's daily SPEND entry wrong (spend missing, wrong amount, wrong link, entered while paused, margin/formula wrong, mapping missing, partner report problems, job errors, manual edits not synced, fees missing, etc.).

You get (1) the EXISTING DICTIONARY entries and (2) GitHub issues I01, I02, ... from FeedMob's repositories (title, labels, state, body, last comments).

For each recurring or instructive pattern you can see in these issues:
- entry: the existing dictionary id if it is the same pattern, otherwise "new";
- name_zh, name_en (short), category (config | upstream | system | manual), desc_zh, desc_en (one sentence each: what goes wrong and why spend ends up wrong),
- config_target (which setting / component is wrong, e.g. click_url.margin, spend_request.formula, campaign_mapping, spend_job),
- phrases: short words people use when they report it (both languages if seen), alert_patterns: literal substrings of automated alerts if any,
- fix_zh, fix_en, owner_role,
- issues: the issue ids showing it, each with a verbatim quote (contiguous text copied from that issue, max 300 characters).
Ignore issues that are not about spend being entered or calculated wrongly (pure feature work, reports for clients, UI). Do not invent. English fields must contain no Chinese characters.

Output one JSON object and nothing else:
{"patterns": [{"entry": str, "name_zh": str, "name_en": str, "category": str, "desc_zh": str, "desc_en": str, "config_target": str, "phrases": [str], "alert_patterns": [str], "fix_zh": str, "fix_en": str, "owner_role": str, "issues": [{"id": str, "quote": str}]}]}"""

MERGE = """These are NEW spend-entry failure patterns extracted from different batches of GitHub issues. Merge duplicates (same root cause) into one pattern. Keep distinct root causes separate.
Return one JSON object and nothing else: {"groups": [{"members": [int], "name_zh": str, "name_en": str, "category": str, "desc_zh": str, "desc_en": str, "config_target": str, "fix_zh": str, "fix_en": str, "owner_role": str}]}
Every input index must appear in exactly one group. English fields must contain no Chinese characters."""

_TAGS = re.compile(r"\[[A-Z]:[^\]]*\]|\((?:\d+(?:\.\d+)?)\)|\((?:from|From)[^)]*\)|【[^】]*】")


def template(title):
    t = _TAGS.sub(" ", title or "")
    t = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|\d+", "N", t)
    t = re.sub(r"https?://\S+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()[:60]


def search(g, log):
    since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    found = {}
    for q in QUERIES:
        for page in range(1, PAGES + 1):
            res = None
            for wait in (0, 65):  # 搜索接口有频率限制（403），等一分钟再试一次
                time.sleep(wait or 2.5)
                try:
                    res = g.get("search/issues", {"q": f"{q} org:feed-mob created:>={since}", "per_page": 100, "page": page, "sort": "updated"})
                    break
                except Exception as e:
                    log(f"搜索失败（{q} p{page}）：{str(e)[:80]}")
            if res is None:
                break
            items = (res or {}).get("items") or []
            for it in items:
                if not it.get("pull_request"):
                    found.setdefault(it["html_url"], it)
            if len(items) < 100:
                break
    log(f"GitHub：{len(found)} 张 spend 相关单子")
    return list(found.values())


def pick(issues):
    groups = defaultdict(list)
    for it in issues:
        groups[template(it.get("title"))].append(it)
    ordered = sorted(groups.values(), key=len, reverse=True)
    out = []
    for grp in ordered:
        grp.sort(key=lambda it: (it.get("state") != "closed", -(it.get("comments") or 0)))
        out += [(it, len(grp)) for it in grp[:PER_CLUSTER]]
        if len(out) >= MAX_ISSUES:
            break
    return out[:MAX_ISSUES], len(groups)


def issue_record(g, it, size):
    repo = it["repository_url"].split("/repos/")[-1]
    comments = []
    if it.get("comments"):
        try:
            comments = g.get(f"repos/{repo}/issues/{it['number']}/comments", {"per_page": 100}) or []
        except Exception:
            comments = []
    labels = ", ".join(lb.get("name", "") for lb in it.get("labels") or [])
    text = (f"TITLE: {it.get('title')}\nSTATE: {it.get('state')} | labels: {labels or '-'} | created {it['created_at'][:10]} | similar issues: {size}\n"
            f"BODY: {(it.get('body') or '')[:1500]}")
    if comments:
        text += "\nLAST COMMENTS:\n" + "\n".join(f"- {c['user']['login']}: {(c.get('body') or '')[:500]}" for c in comments[-3:])
    links = sorted(set(re.findall(r"(?<![\d.#/])(\d{5})(?![\d.])", (it.get("title") or "") + " " + (it.get("body") or ""))))[:10]
    return dict(src="github", ts=it["created_at"], text=text, url=it["html_url"], where=f"{repo}#{it['number']}", links=links, date=it["created_at"][:10])


def _view(doc):
    return [dict(id=e["id"], category=e["category"], name=e.get("name_en"), description=e.get("desc_en"), config_target=e.get("config_target"))
            for e in K.active(doc)]


def extract(batch, view):
    recs = {f"I{i + 1:02d}": r for i, r in enumerate(batch)}
    user = "EXISTING DICTIONARY:\n" + json.dumps(view, ensure_ascii=False) + "\n\nISSUES:\n" + "\n\n".join(f"=== {k} ({r['where']})\n{r['text']}" for k, r in recs.items())
    out, usage = A.ask([{"role": "system", "content": EXTRACT}, {"role": "user", "content": user}])
    pats = []
    for p in out.get("patterns") or []:
        if not isinstance(p, dict):
            continue
        cites = []
        for c in p.get("issues") or []:
            r = recs.get(str((c or {}).get("id")))
            q = A.verify_quote(r, c.get("quote")) if r else None
            if q:
                cites.append(dict(source="github", ref=r["url"], quote=q[:300], scope=dict(links=r["links"], date=r["date"])))
        if cites:
            pats.append({**p, "examples": cites})
    return pats, usage


def build(log=print, max_issues=MAX_ISSUES):
    t0 = time.time()
    g = GH()
    if not g.available():
        raise RuntimeError("GitHub 不可用（没有令牌也没有 gh）")
    issues = search(g, log)
    picked, clusters = pick(issues)
    picked = picked[:max_issues]
    log(f"粗分 {clusters} 簇，挑出 {len(picked)} 张代表单子，补评论中…")
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        records = list(ex.map(lambda x: issue_record(g, *x), picked))
    doc = K.load()
    view = _view(doc)
    batches = [records[i:i + BATCH] for i in range(0, len(records), BATCH)]
    log(f"MiniMax 抽取：{len(batches)} 批")
    patterns, usage = [], {"prompt_tokens": 0, "completion_tokens": 0}
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for pats, u in ex.map(lambda b: _safe_extract(b, view, log), batches):
            patterns += pats
            for k in usage:
                usage[k] += (u or {}).get(k) or 0
    ids = {e["id"] for e in doc["entries"]}
    known = [p for p in patterns if p.get("entry") in ids]
    new = [p for p in patterns if p.get("entry") not in ids and (p.get("name_en") or p.get("name_zh"))]
    log(f"抽到 {len(patterns)} 个模式：对上已有条目 {len(known)}，新模式 {len(new)}")
    groups = merge(new, log) if new else []
    with K.locked():  # 抽取期间字典可能被排查学习改过：在锁里重新读一遍再并入
        doc = K.load()
        changed = _apply(doc, known, new, groups)
        K.save(doc, f"P0：从 GitHub 建字典（{len(records)} 张代表单子，{len(patterns)} 个模式）", sorted(set(changed)), actor="dict_build")
    report = dict(at=now_z(), issues=len(issues), clusters=clusters, read=len(records), batches=len(batches), patterns=len(patterns),
                  matched_existing=len(known), new_patterns=len(new), new_entries=len(groups), changed=sorted(set(changed)),
                  usage=usage, seconds=round(time.time() - t0))
    write_json(K.KNOWLEDGE / "dict_build_report.json", report)
    log(json.dumps({k: v for k, v in report.items() if k != "changed"}, ensure_ascii=False))
    from . import curate
    report["curate"] = curate.run(log=log, force=True)   # 新候选进来了：马上自动整理（合并重复、删掉不相关的）
    return report


def _apply(doc, known, new, groups):
    changed = []
    for p in known:
        e = K.entry(doc, p["entry"])
        for x in p["examples"]:
            K._add_unique(e["examples"], x, lambda y: (y.get("quote"), y.get("source")), K.MAX_EXAMPLES)
        for ph in (p.get("phrases") or [])[:6]:
            if isinstance(ph, str) and 2 <= len(ph) <= 60:
                K._add_unique(e["phrases"], ph, lambda y: y.lower(), K.MAX_PHRASES)
        for ap in (p.get("alert_patterns") or [])[:4]:
            if isinstance(ap, str) and len(ap) >= 6 and K._valid_re(re.escape(ap)):
                K._add_unique(e["alert_patterns"], re.escape(ap), lambda y: y, 12)
        changed.append(p["entry"])
    for grp in groups:
        members = [new[i] for i in grp["members"] if 0 <= i < len(new)]
        if not members:
            continue
        data = {**grp, "phrases": sorted({ph for m in members for ph in (m.get("phrases") or []) if isinstance(ph, str)})[:15],
                "alert_patterns": [re.escape(a) for m in members for a in (m.get("alert_patterns") or []) if isinstance(a, str) and len(a) >= 6][:6],
                "signals": [], "examples": [x for m in members for x in m["examples"]][:K.MAX_EXAMPLES]}
        eid = K.add_candidate(doc, data, "github", "P0 GitHub 建字典")
        if eid:
            changed.append(eid)
    return changed


def _safe_extract(batch, view, log):
    try:
        return extract(batch, view)
    except Exception as e:
        log(f"一批抽取失败：{str(e)[:120]}")
        return [], {}


def merge(new, log):
    items = [dict(i=i, name=p.get("name_en") or p.get("name_zh"), desc=p.get("desc_en"), target=p.get("config_target"), category=p.get("category"))
             for i, p in enumerate(new)]
    try:
        out, _ = A.ask([{"role": "system", "content": MERGE}, {"role": "user", "content": json.dumps(items, ensure_ascii=False)}])
        groups = [g for g in out.get("groups") or [] if isinstance(g, dict) and g.get("members")]
    except Exception as e:
        log(f"合并失败，按原样收：{str(e)[:120]}")
        groups = []
    covered = {i for g in groups for i in g["members"] if isinstance(i, int)}
    for i, p in enumerate(new):  # 模型漏掉的单独成组
        if i not in covered:
            groups.append({**{k: p.get(k) for k in ("name_zh", "name_en", "category", "desc_zh", "desc_en", "config_target", "fix_zh", "fix_en", "owner_role")},
                           "members": [i]})
    return groups
