"""异常排查（spend 录入异常）：放在活动回放里，只排查当前活动。

一次排查 = 活动 + 异常日期 D（必选）+ 描述（可选：发生了什么 / click URL / line item）+ 前后范围 N 天（1 / 3 / 7 / 14）。
流程（各步结果都落盘到 raw/campaigns/<活动>/diagnose/<D>/<编号>.json，页面轮询这个文件）：
  ① 解析描述（程序）：click URL 编号对照本活动的链接；line item 去合作方原始报告里反查；现象词对照字典「说法」；GitHub 单号只当文字。
  ② 原因证据（程序，不接 GitHub）：Femini 配置与改动历史、spend request、每日 spend、月度调整、daily feeds、line item 反查；
     Slack（窗口内全部频道的镜像）、HubSpot 工单、邮件、会议。选 Slack 时只按链接编号 / 活动名 / 广告主 / 合作方筛，不用 GitHub 单号。
  ③ 信号（程序）：replay/spend_checks.py。
  ④ 原因（MiniMax）：字典 + skill + 信号 + 证据 → 程序核对（replay/diagnose_ai.py）。结果先落盘，之后才碰 GitHub。
  ⑤ 修复状态 + 验证（GitHub 此时才可读）：按链接编号 / 活动名 / 广告主 spend 搜 feed-mob 的单子；MiniMax 判每条结论
     一致 / 部分一致 / 矛盾 / GitHub 无记录，并找修复动作；程序按「数据现在是否正常 + 修复动作 + 工单状态」定修复状态。
  ⑥ 学习：replay/knowledge.py 按验证结果更新字典；一致的案例进回归集；字典条目状态变了就（限频）重跑回归集。
时间范围：配置历史取截至 D+N 的全部；数据和沟通取 D±N（spend 数据另取前 14 天当基线）；修复和验证取 D−N 到 D+30。
全程只读：Femini 只调白名单里的查询工具；GitHub 只发 GET。
"""
import datetime as dt
import fcntl
import hashlib
import json
import re
import time
import traceback
from collections import Counter

import slack_source

from . import brainpages, campaign, feeds
from . import corpus as C
from . import diagnose_ai as A
from . import knowledge as K
from . import progress as P
from . import spend_checks as S
from .collect_mcp import channel_name, mcp_client
from .util import RAW, STATE, camp_dir, iso, now_z, read_json, valid_number, write_json

RANGES = (1, 3, 7, 14)
DEFAULT_RANGE = 3
MAX_QUERY = 500
FIX_DAYS = 30
SLACK_CAP, EMAIL_CAP, TICKET_CAP, MEETING_CAP, DAY_CAP = 160, 25, 40, 8, 120
TEXT_CAP = 1200
EVIDENCE_CHARS = 160_000
REGRESS_COOLDOWN = 6 * 3600
REGRESS_LIMIT = 20
SKILLS_AGE = 7 * 86400
# Femini 只用这些查询类工具（写入类一律不调）
FEMINI_READ = {"get_click_url", "get_click_url_spend_metrics", "list_spend_requests", "list_monthly_spend_adjustments",
               "list_daily_feeds", "list_partner_report_rows", "get_skill", "read_skill_file", "list_skills"}
SPEND = re.compile(r"spend|花费|录入|录错|补录|重传|补传|上传|\bnet\b|gross|margin|利润|mapping|映射|line ?items?|report|报告|paus|暂停|"
                   r"upload|discrepan|差异|对不上|invoice|\bcost|费用|\bfee|\bjob\b|missing|缺|makegood|credit", re.I)
BOT = re.compile(r"^unknown$|bot\b|\[bot\]|feedai|feedmob\w*(?:ng|reports|admin)|monitor|alert", re.I)
STAGES = {"queued": ("排队中", "Queued"), "collect": ("收集证据", "Collecting evidence"), "cause": ("分析原因", "Analysing causes"),
          "status": ("核对修复状态 / GitHub 验证", "Checking fixes / GitHub verification"), "learn": ("更新字典", "Updating dictionary"),
          "done": ("完成", "Done"), "error": ("出错", "Failed")}
FIX_STATUS = ("fixed", "handled_pending_data", "in_progress", "no_action")
# 合作方 → Femini 里登记的合作方原始报告来源（line item 反查用）
REPORT_SOURCES = {"bidease": "bidease_reports", "beeswax": "beeswax_reports", "freewheel": "beeswax_reports", "bigo": "bigo_reports",
                  "disco": "disco_reports", "inmobi": "inmobi_cost_reports", "ironsource": "iron_source_aura_reports",
                  "jampp": "jampp_reports", "kayzen": "kayzen_reports", "lg": "lg_ads_reports", "liftoff": "liftoff_reports",
                  "personaly": "personaly_reports", "pubmatic": "pubmatic_reports", "reddit": "reddit_reports", "remerge": "remerge_reports",
                  "samsung": "samsung_tv_reports", "smadex": "smadex_reports", "thanksco": "thanksco_reports", "vizio": "vizio_reports",
                  "youappi": "you_appi_reports"}
# 描述里的现象词 → 程序信号（字典里条目自己的「说法」另外匹配）
SYMPTOMS = [(re.compile(r"未录入|没有录入|没录|漏录|缺|missing|not entered|no spend", re.I), ["spend_missing", "zero_with_delivery"]),
            (re.compile(r"暂停|pause", re.I), ["spend_when_paused"]),
            (re.compile(r"margin|利润率", re.I), ["margin_mismatch"]),
            (re.compile(r"多了|偏高|高估|少了|偏低|对不上|不一致|mismatch|discrepan|overstat|understat", re.I),
             ["margin_mismatch", "net_cost_ratio_shift", "gross_rate_mismatch", "net_rate_mismatch", "spend_jump"]),
            (re.compile(r"匹配失败|mapping|映射|line ?item", re.I), ["spend_missing"]),
            (re.compile(r"录错|错的链接|wrong (?:url|link)", re.I), ["spend_jump", "net_cost_ratio_shift"])]


class ReadOnlyFemini:
    """包一层：只放行白名单里的查询工具。"""

    def __init__(self, client):
        self.c = client

    def call(self, name, args):
        if name not in FEMINI_READ:
            raise RuntimeError(f"排查只读：不允许调用 {name}")
        return self.c.call(name, args)


def femini():
    return ReadOnlyFemini(mcp_client.femini())


# ───────────────────────── 存放 ─────────────────────────
def norm_query(q):
    return re.sub(r"\s+", " ", (q or "").strip())[:MAX_QUERY]


def run_id(date, query, rng):
    return hashlib.sha1(f"{date}|{norm_query(query).lower()}|{rng}".encode()).hexdigest()[:10]


def run_path(cid, date, rid):
    return camp_dir(cid) / "diagnose" / date / f"{rid}.json"


def valid_date(s):
    s = str(s or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError("日期必选，格式 YYYY-MM-DD")
    dt.date.fromisoformat(s)
    return s


def valid_range(r):
    try:
        r = int(r)
    except (TypeError, ValueError):
        r = DEFAULT_RANGE
    if r not in RANGES:
        raise ValueError(f"前后范围只能是 {RANGES} 天")
    return r


def read_run(cid, date, rid):
    if not re.fullmatch(r"[0-9a-f]{10}", rid or ""):
        return None
    return read_json(run_path(valid_number(cid), valid_date(date), rid))


def list_runs(cid):
    base = camp_dir(valid_number(cid)) / "diagnose"
    out = []
    if base.exists():
        for p in base.glob("*/*.json"):
            if p.name.endswith(".input.json"):
                continue
            r = read_json(p) or {}
            out.append(dict(id=r.get("id"), date=r.get("date"), query=r.get("query"), range=r.get("range"), stage=r.get("stage"),
                            created_at=r.get("created_at"), updated_at=r.get("updated_at"), error=r.get("error"),
                            summary_zh=(r.get("cause") or {}).get("summary_zh"), summary_en=(r.get("cause") or {}).get("summary_en"),
                            findings=len((r.get("cause") or {}).get("findings") or []),
                            verdicts=dict(Counter(c.get("verdict") for c in (r.get("verify") or {}).get("checks") or [])),
                            pending=bool((r.get("followup") or {}).get("pending")) and not (r.get("followup") or {}).get("final")
                            and bool((r.get("cause") or {}).get("findings"))))
    return sorted(out, key=lambda x: x.get("created_at") or "", reverse=True)


def _save(run, stage=None, **kw):
    if stage:
        run["stage"] = stage
        run["stage_label"] = STAGES[stage]
    run.update(kw)
    run["updated_at"] = now_z()
    write_json(run_path(run["campaign"]["id"], run["date"], run["id"]), run)


def data_bounds(cid):
    """能排查的日期范围：活动有数据的第一天（metrics.json 里 spend / 安装 / 曝光任一项不为 0 的最早一天；没有这份数据就不设下限）到昨天（当天的数还没录完）。"""
    series = (read_json(camp_dir(valid_number(cid)) / "metrics.json") or {}).get("series") or {}
    days = [x[0] for rows in series.values() for x in rows if isinstance(x, (list, tuple)) and len(x) == 2 and x[1]]
    yday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    return (min(days) if days else None), yday


def prepare(cid, date, query, rng):
    """校验输入、建（或复用）排查记录。→ (记录, 是否需要开跑)。同一组「日期 + 描述 + 范围」重复提交时复用；正在跑的不重开。"""
    cid, date, rng, query = valid_number(cid), valid_date(date), valid_range(rng), norm_query(query)
    info = campaign.info(cid) or {}
    if not info.get("links"):
        raise ValueError("这个活动还没有取过数，先生成回放")
    first, last = data_bounds(cid)
    if (first and date < first) or date > last:
        raise ValueError(f"{date} 没有数据可排查，可选日期：{first or '…'} ~ {last}")
    rid = run_id(date, query, rng)
    have = read_json(run_path(cid, date, rid))
    if have and have.get("stage") not in ("done", "error"):
        return have, False
    camp = info.get("campaign") or {}
    # 重新排查同一组参数：带上上一次已经记进字典的结果，新结论和旧的一样就不重复计数，不一样就先撤销旧的
    run = dict(id=rid, campaign=dict(id=cid, name=camp.get("name")), date=date, query=query, range=rng,
               created_at=now_z(), version=A.CAUSE_VERSION, learned=(have or {}).get("learned") or {})
    _save(run, "queued")
    return run, True


# ───────────────────────── ① 解析描述 ─────────────────────────
def parse_query(query, links, doc=None):
    q = norm_query(query)
    gh = sorted(set(re.findall(r"(?:GH\s*#|github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/|(?<![\w/])#)(\d{3,6})", q, re.I)))
    li = []
    for m in re.finditer(r"line[\s_-]*items?\s*(?:id)?\s*[:：#]?\s*([A-Za-z0-9][\w.-]{0,60})", q, re.I):
        tok = m.group(1).strip(".-")
        if tok.lower() not in ("匹配失败", "mapping", "spend") and tok not in li:
            li.append(tok)
    rest = re.sub(r"line[\s_-]*items?\s*(?:id)?\s*[:：#]?\s*[A-Za-z0-9][\w.-]{0,60}", " ", q, flags=re.I)
    rest = re.sub(r"(?:GH\s*#|github\.com/\S+|(?<![\w/])#)\d{3,6}", " ", rest, flags=re.I)
    rest = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}", " ", rest)
    nums = [x for x in dict.fromkeys(re.findall(r"(?<![\d.$])\d{4,6}(?![\d.])", rest)) if not re.fullmatch(r"20\d\d", x)]  # 年份不算链接
    links = [str(x) for x in links]
    kinds = []
    for rx, ks in SYMPTOMS:
        if rx.search(q):
            kinds += [k for k in ks if k not in kinds]
    return dict(links_in=[n for n in nums if n in links], links_out=[n for n in nums if n not in links], line_items=li,
                gh_refs=gh, symptom_kinds=kinds, phrase_hits=K.match_phrases(doc, q) if doc else {})


# ───────────────────────── ② 原因证据（没有 GitHub） ─────────────────────────
def _ev(src, ts, text, kind, actor="", where="", url="", text_zh=None, **meta):
    e = dict(src=src, ts=ts or "", kind=kind, actor=actor or "", where=where, url=url, text=(text or "")[:TEXT_CAP * 3])
    if text_zh:
        e["text_zh"] = text_zh[:TEXT_CAP * 3]
    if meta.get("approver"):
        e["approver"] = meta.pop("approver")
    if meta:
        e["meta"] = meta
    return e


_FIELDS = (("new_margin", "margin", "%"), ("new_state", "state", ""), ("new_gross_cpi", "gross rate", "$"), ("new_net_cpi", "net rate", "$"),
           ("new_hard_cap", "hard cap", ""), ("new_advertiser_cap", "advertiser cap", ""), ("new_max_switch", "max switch", ""))


def _change_text(n, c):
    parts = []
    for k, label, unit in _FIELDS:
        if c.get(k) is not None:
            v = c[k]
            parts.append(f"{label} → {'$' if unit == '$' else ''}{v}{'%' if unit == '%' else ''}")
    when = iso(c.get("new_state_effective_at")) or iso(c.get("new_effective_at"))
    sub = c.get("user_name") or "unknown"
    return (f"FeedMob Admin change #{c.get('id')} on link {n}: " + "; ".join(parts) + (f" (effective {when})" if when else "")
            + f". Submitted by {sub}, approved by {S.approver(c) or sub}.",
            f"后台改动 #{c.get('id')}（链接 {n}）：" + "；".join(parts) + (f"（生效 {when}）" if when else "") + f"。提交 {sub}，批准 {S.approver(c) or sub}。")


def _snapshot_text(n, cu, day):
    ca, pa = S.action_on(cu.get("client_paid_actions"), day), S.action_on(cu.get("partner_paid_actions"), day)
    co, pc = S.origin_on(cu.get("client_spend_event_sources"), day), S.origin_on(cu.get("partner_spend_event_sources"), day)
    state, since, _ = S.state_on(cu, day)
    _, m, _ = S.margin_on(cu, day)
    notes = "; ".join(str(x.get("content") or x)[:200] for x in (cu.get("notes") or [])[:3]) if isinstance(cu.get("notes"), list) else ""
    return (f"Link {n} settings on {day}: partner {((cu.get('partner') or {}).get('name'))}; state {state or 'live (no state change on record)'}"
            + (f" since {since[:16]}" if since else "") + f"; margin {m if m is not None else 'not used'}"
            + f"; client spend request: {cu.get('client_spend_request_summary') or '-'}; partner spend request: {cu.get('partner_spend_request_summary') or '-'}"
            + f"; client paid action: {ca or '-'}; partner paid action: {pa or '-'}; client event source {co[0] or '-'}; partner event source {pc[0] or '-'}"
            + f"; hard cap {cu.get('hard_cap')}; advertiser cap {cu.get('advertiser_cap')}" + (f"; notes: {notes}" if notes else ""))


def admin_evidence(f, links, focus, cfgs, lo, hi, d, log):
    """Femini：改动历史、链接设置、spend request、每日 spend、月度调整、daily feeds。→ (证据, 每条链接的 spend 行)"""
    out, rows_by = [], {}
    for n in links:
        cu = cfgs[n]
        for c in sorted(S._approved(cu), key=lambda c: c.get("updated_at") or ""):
            t = iso(c.get("updated_at"))
            if not t or t[:10] > hi:
                continue
            recent = t[:10] >= S.shift(lo, -60)
            latest_state = c is (S.value_at(S.state_timeline(cu), d + "T23:59:59Z")[1])
            latest_margin = c is (S.value_at(S.margin_timeline(cu), d + "T23:59:59Z")[1])
            if recent or latest_state or latest_margin:
                en, zh = _change_text(n, c)
                out.append(_ev("femini", t, en, "config_change", actor=c.get("user_name"), approver=S.approver(c),
                               where=f"admin link {n}", url=f"https://admin.feedmob.com/click_urls/{n}", text_zh=zh, link=n,
                               change_id=c.get("id")))
        if n in focus:
            out.append(_ev("femini", d + "T00:00:00Z", _snapshot_text(n, cu, d), "link_settings", where=f"admin link {n}",
                           url=f"https://admin.feedmob.com/click_urls/{n}", link=n))
    seen_sr = set()
    for n in focus:
        try:
            srs = f.call("list_spend_requests", {"click_url_name": n, "limit": 10}).get("spend_requests") or []
        except Exception as e:
            log(f"spend request 取不到（{n}）：{str(e)[:100]}")
            srs = []
        cfgs[n]["_spend_requests"] = srs
        for sr in srs:
            if sr.get("id") in seen_sr:
                continue
            seen_sr.add(sr.get("id"))
            txt = (f"Spend request #{sr.get('id')} for links {', '.join(sr.get('click_url_names') or [])}: gross formula {sr.get('gross_spend_formula')!r} "
                   f"(source {sr.get('gross_spend_source')}); net formula {sr.get('net_spend_formula')!r} (source {sr.get('net_spend_source')}); "
                   f"margin {sr.get('margin_formula')!r}; notes: {sr.get('notes') or '-'}")
            out.append(_ev("femini", lo + "T00:00:00Z", txt, "spend_request", where="admin spend request", url=sr.get("url") or "", link=n))
    for n in links:
        try:
            rows_by[n] = S.fetch_metrics(f, n, S.shift(lo, -S.BASELINE_DAYS), hi)
        except Exception as e:
            log(f"每日 spend 取不到（{n}）：{str(e)[:100]}")
            rows_by[n] = []
    return out, rows_by


def day_evidence(cfgs, rows_by, focus, signals, lo, hi):
    sig_days = {(s["link"], s["date"]) for s in signals}
    rows = [(n, r) for n in focus for r in rows_by.get(n, []) if lo <= r.get("date", "") <= hi]
    rows.sort(key=lambda x: ((x[0], x[1]["date"]) not in sig_days, x[1]["date"]))
    out = []
    for n, r in rows[:DAY_CAP]:
        out.append(_ev("femini", r["date"] + "T23:59:00Z", S.day_summary(n, cfgs[n], r, "en"), "spend_day", where=f"admin daily spend {n}",
                       url=f"https://admin.feedmob.com/click_urls/{n}/net_spends", text_zh=S.day_summary(n, cfgs[n], r, "zh"), link=n))
    return out


def extra_admin(f, cid, adv, lo, hi, log):
    out = []
    months = sorted({lo[:7], hi[:7]})
    for mth in months:
        try:
            res = f.call("list_monthly_spend_adjustments", {"campaign_id": int(cid), "month": mth})
            rows = res.get("monthly_spend_adjustments") or res.get("data") or (res if isinstance(res, list) else [])
            for r in rows[:20]:
                out.append(_ev("femini", (iso(r.get("created_at")) or mth + "-01T00:00:00Z"),
                               "Monthly spend adjustment: " + json.dumps(r, ensure_ascii=False, default=str)[:600], "monthly_adjustment",
                               actor=r.get("user_name") or r.get("created_by") or "", where=f"admin monthly adjustment {mth}"))
        except Exception as e:
            log(f"月度调整取不到：{str(e)[:100]}")
    page = 1
    while page <= 5 and adv:
        try:
            res = f.call("list_daily_feeds", {"start_date": lo, "end_date": hi, "limit": 100, "page": page})
        except Exception as e:
            log(f"daily feeds 取不到：{str(e)[:100]}")
            break
        for r in res.get("daily_feeds") or []:
            if adv.lower() in (r.get("client_name") or "").lower():
                out.append(_ev("femini", iso(r.get("created_at")) or (r.get("published_date") or lo) + "T00:00:00Z",
                               f"Daily feed ({r.get('impact')}, {r.get('source_type')}): {r.get('title')}", "daily_feed", where="admin daily feed"))
        if not (res.get("meta") or {}).get("next_page"):
            break
        page += 1
    return out


def line_item_evidence(f, tokens, links, cfgs, adv, lo, hi, log):
    """line item 反查：在合作方原始报告里找这个 line item 映射到了哪条 click URL；查不到也是证据。"""
    partners = {((cfgs[n].get("partner") or {}).get("name") or "").lower() for n in links}
    srcs = sorted({src for key, src in REPORT_SOURCES.items() if any(key in p for p in partners)})
    start = max(lo, S.shift(hi, -30))
    out, notes = [], []
    if not tokens and not srcs:
        return out, notes
    rows_by = {}
    for src in srcs:
        rows, cursor = [], None
        for _ in range(3):
            args = {"source": src, "start_date": start, "end_date": hi, "limit": 1000}
            if cursor:
                args["cursor"] = cursor
            try:
                res = f.call("list_partner_report_rows", args)
            except Exception as e:
                log(f"{src} 取不到：{str(e)[:100]}")
                break
            rows += res.get("data") or []
            cursor = (res.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        rows_by[src] = rows
    for tok in tokens:
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", re.I)
        hits = []
        for src, rows in rows_by.items():
            for r in rows:
                ids = {str(r.get(k)) for k in ("campaign_id", "line_item_id", "creative_id", "id") if r.get(k) is not None}
                names = " ".join(str(r.get(k) or "") for k in ("campaign_name", "line_item_name", "creative_name"))
                if tok in ids or rx.search(names):
                    hits.append((src, r))
        if not srcs:
            txt = (f"Line item {tok!r}: the partners of this campaign ({', '.join(sorted(p for p in partners if p))}) have no registered "
                   f"raw report source in FeedMob Admin, so the line item cannot be looked up there.")
            notes.append(dict(zh=f"line item {tok}：本活动的合作方没有登记的原始报告来源，无法反查", en=f"Line item {tok}: no registered partner report source to look it up"))
        elif not hits:
            txt = f"Line item {tok!r} not found in {', '.join(srcs)} rows {start}..{hi} (searched campaign / line item / creative ids and names)."
            notes.append(dict(zh=f"line item {tok}：在 {', '.join(srcs)} 里查不到（未映射或不在这些来源）", en=f"Line item {tok}: not found in {', '.join(srcs)}"))
        else:
            mapped = Counter(str(r.get("click_url_name")) for _, r in hits if r.get("click_url_name"))
            unmapped = sum(1 for _, r in hits if not r.get("click_url_name"))
            dates = sorted({str(r.get("reported_date") or r.get("date")) for _, r in hits})
            money = sum(float(r.get("spend") or r.get("revenue") or 0) for _, r in hits)
            inside = [k for k in mapped if k in links]
            txt = (f"Line item {tok!r} in {', '.join(sorted({s for s, _ in hits}))}: {len(hits)} rows on {dates[0]}..{dates[-1]}, reported amount ${money:,.2f}; "
                   f"mapped to click URLs {dict(mapped) or '{}'}; unmapped rows {unmapped}; mapped to this campaign's links: {inside or 'none'}.")
        out.append(_ev("femini", hi + "T23:59:00Z", txt, "line_item_lookup", where="admin partner report rows"))
    for src, rows in rows_by.items():  # 合作方报告里没映射、但名字带广告主的行：mapping 缺失的直接证据
        um = [r for r in rows if not r.get("click_url_name") and adv and adv.lower() in str(r.get("campaign_name") or "").lower()]
        if um:
            names = Counter(str(r.get("campaign_name")) for r in um)
            out.append(_ev("femini", hi + "T23:59:00Z", f"Unmapped rows in {src} {start}..{hi} naming {adv}: {len(um)} rows; campaigns {dict(names.most_common(8))}.",
                           "unmapped_rows", where="admin partner report rows"))
    return out, notes


def _slack_day(day):
    out = []
    # 直连模式读 FeedAI 拉来的 Slack 副本（raw/feed/slack，刷新时更新）；旧的大脑镜像 raw/slack 10-31 起已停更
    root = feeds.slack_root() if feeds.enabled() else RAW / "slack"
    for p in sorted(root.glob(f"*/{day}.json")):
        page = read_json(p)
        if not page or page.get("missing"):
            continue
        body = slack_source._body(page)
        hits = list(slack_source._MSG.finditer(body))
        for i, m in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
            out.append(dict(channel=p.parent.name, ts=f"{m.group(1)}T{m.group(2)}Z", user=m.group(3).strip(),
                            text=slack_source.clean(body[m.end():end])))
    return out


def slack_evidence(matcher, alerts, lo, hi, d, cap=SLACK_CAP):
    adv = (matcher.t["advertiser"] or "").lower()
    ctx = [c.lower() for c in matcher.t["context"]]
    picked, per_tpl = [], Counter()
    for day in S.days(lo, hi):
        for m in _slack_day(day):
            t, low = m["text"], m["text"].lower()
            bot = bool(BOT.search(m["user"] or "")) or t.startswith("*Feedmob")
            strong = matcher.strong(t)
            dedicated = bool(adv) and adv in channel_name(m["channel"]).lower()
            alert = any(rx.search(t) for rx in alerts)
            if strong:
                why = "strong"
            elif bot:
                if not (adv and adv in low and (alert or SPEND.search(t)) and (any(c in low for c in ctx) or alert)):
                    continue
                why = "alert"
            elif (matcher.advertiser(t) or dedicated) and SPEND.search(t):
                why = "topic"
            else:
                continue
            if bot:
                tpl = (day, re.sub(r"\d+", "N", t[:120]))
                per_tpl[tpl] += 1
                if per_tpl[tpl] > 2:
                    continue
            picked.append((m, why))
    rank = {"strong": 0, "alert": 1, "topic": 2}
    center = dt.datetime.fromisoformat(d + "T12:00:00")
    picked.sort(key=lambda x: (rank[x[1]], abs((dt.datetime.fromisoformat(x[0]["ts"][:19]) - center).total_seconds())))
    out = [_ev("slack", m["ts"], m["text"][:TEXT_CAP], "message", actor=m["user"], where="#" + channel_name(m["channel"]), why=why)
           for m, why in picked[:cap]]
    return out


def _topic(text, matcher):
    return bool(SPEND.search(text or "")) or bool(matcher.strong(text or ""))


def brain_evidence(matcher, lo, hi, log, ticket_hi=None):
    out = []
    try:
        tks = [r for r in brainpages.ticket_records(matcher, lo, ticket_hi or hi) if _topic(r["title"] + " " + r["text"], matcher)]
    except Exception as e:
        log(f"HubSpot 缓存读不到：{str(e)[:100]}")
        tks = []
    for r in tks[:TICKET_CAP]:
        txt = (f"HubSpot ticket #{r['ticket']} «{r['title']}» (status {r.get('status') or '-'}, owner {r.get('owner') or '-'}, priority {r.get('priority') or '-'})"
               + (f" note: {r['text']}" if r.get("who") else f" created: {r['text']}"))
        out.append(_ev("hubspot", r["ts"], txt[:TEXT_CAP], "ticket_note" if r.get("who") else "ticket", actor=r.get("who") or "",
                       where=f"HubSpot #{r['ticket']}", ticket=r["ticket"], title=r["title"], status=r.get("status"), owner=r.get("owner")))
    try:
        ems = [r for r in brainpages.email_records(matcher, lo, hi) if _topic(r["subject"] + " " + r["text"], matcher)]
    except Exception as e:
        log(f"邮件缓存读不到：{str(e)[:100]}")
        ems = []
    for r in ems[:EMAIL_CAP]:
        out.append(_ev("email", r["ts"], f"Email «{r['subject']}» to {', '.join(r.get('to') or []) or '-'}: {r['text']}"[:TEXT_CAP * 2], "email",
                       actor=r.get("who") or "", where=f"email «{r['subject'][:60]}»"))
    meet = []
    d = C.meeting_cache_dir()
    if d.exists():
        for p in sorted(d.glob("*.json")):
            day = p.name[:10]
            if not (lo <= day <= hi):
                continue
            cached = read_json(p)
            rec = C.meeting_record(cached, matcher) if cached else None
            if not rec:
                continue
            lines = rec["text"].splitlines()
            keep = sorted({j for i, ln in enumerate(lines) if SPEND.search(ln) and matcher.anchor(ln) for j in (i - 1, i, i + 1) if 0 <= j < len(lines)})
            if keep:
                meet.append(_ev("meeting", rec["ts"], "\n".join(lines[j] for j in keep)[:TEXT_CAP * 2], "meeting", where=f"meeting «{rec['title'][:60]}»"))
    return out + meet[:MEETING_CAP]


def _assign_ids(evidence, prefix):
    evidence.sort(key=lambda e: (e["ts"], e["src"]))
    total, out = 0, []
    for e in evidence:
        total += len(e["text"])
        if total > EVIDENCE_CHARS:
            break
        out.append(e)
    for i, e in enumerate(out, 1):
        e["id"] = f"{prefix}{i:03d}"
    return out


def matcher_for(info, cfgs):
    """不带 GitHub 单号的词表：原因阶段不能用 GitHub 派生的线索去筛记录。"""
    partners = [((cfgs[n].get("partner") or {}).get("name")) for n in cfgs]
    terms = C.build_terms(info.get("campaign") or {}, info.get("links") or [], [p for p in partners if p], issues=[])
    return C.Matcher(terms)


def skills_text(f, log):
    """Femini 里的 FeedMob skill（核心口径 + 数据核对规则）摘要；缓存 7 天。"""
    p = K.KNOWLEDGE / "skills.json"
    have = read_json(p)
    if have and time.time() - have.get("at", 0) < SKILLS_AGE:
        return have["text"]
    parts = []
    try:
        for sk in f.call("list_skills", {}).get("skills") or []:
            if sk.get("name") not in ("fm-core-context", "fm-validation"):
                continue
            body = (f.call("get_skill", {"id": sk["id"]}).get("skill") or {})
            txt = body.get("instructions") or ""
            if sk["name"] == "fm-core-context":
                try:
                    g = f.call("read_skill_file", {"id": sk["id"], "path": "references/glossary.md"})
                    txt = (txt[txt.find("## Intelligent Defaults"):] if "## Intelligent Defaults" in txt else txt) + "\n" + (g.get("content") if isinstance(g, dict) else str(g))
                except Exception:
                    pass
            parts.append(f"### {sk['name']}\n{txt[:9000]}")
    except Exception as e:
        log(f"skill 取不到：{str(e)[:100]}")
    text = "\n\n".join(parts)
    if text:
        write_json(p, dict(at=time.time(), text=text))
    return text or (have or {}).get("text", "")


# ───────────────────────── ⑤ GitHub（只在原因落盘之后） ─────────────────────────
def github_evidence(links, camp_name, adv, lo, hi, refs, log):
    from .github import GH
    g = GH()
    if not g.available():
        return [], "GitHub 不可用（没有令牌也没有 gh）"
    found = {}
    queries = [f"{n} org:feed-mob updated:>={lo}" for n in links[:6]]
    if camp_name:
        queries.append(f'"{camp_name}" org:feed-mob updated:>={lo}')
    if adv:
        queries.append(f"{adv} spend in:title org:feed-mob created:{lo}..{hi}")
    for q in queries:
        try:
            for it in (g.get("search/issues", {"q": q, "per_page": 15, "sort": "updated"}) or {}).get("items") or []:
                if it["created_at"][:10] <= hi:
                    found.setdefault(it["html_url"], it)
        except Exception as e:
            log(f"GitHub 搜索失败（{q[:40]}）：{str(e)[:100]}")
    for num in refs:
        for repo in ("feedmob", "tracking_admin", "feedmob_worker"):
            try:
                it = g.get(f"repos/feed-mob/{repo}/issues/{num}")
                if it and it.get("html_url"):
                    found.setdefault(it["html_url"], it)
                    break
            except Exception:
                continue
    pats = [re.compile(rf"(?<!\d){n}(?!\d)") for n in links]
    # 标题或正文写了本活动链接编号的排前面，其余按最近更新；最多 10 张
    items = sorted(found.values(), key=lambda it: it.get("updated_at") or "", reverse=True)
    items = sorted(items, key=lambda it: not any(p.search((it.get("title") or "") + " " + (it.get("body") or "")) for p in pats))[:10]
    out = []
    for it in items:
        repo = it["repository_url"].split("/repos/")[-1] if it.get("repository_url") else it["html_url"].split("github.com/")[-1].rsplit("/", 2)[0]
        num = it["number"]
        try:
            comments = g.get(f"repos/{repo}/issues/{num}/comments", {"per_page": 100}) or []
        except Exception:
            comments = []
        state = it.get("state")
        labels = ", ".join(lb.get("name", "") for lb in it.get("labels") or [])
        head = (f"[{state}{' closed ' + it['closed_at'][:10] if it.get('closed_at') else ''}{' (PR)' if it.get('pull_request') else ''}] "
                f"labels: {labels or '-'}\nTITLE: {it.get('title')}\nBODY: {it.get('body') or ''}")
        cm = "\n".join(f"[{c['created_at'][:16]}] {c['user']['login']}: {(c.get('body') or '')[:700]}" for c in comments[-15:])
        out.append(_ev("github", iso(it["created_at"]), head + ("\n--- comments:\n" + cm if cm else ""), "issue", actor=(it.get("user") or {}).get("login"),
                       where=f"{repo}#{num}", url=it["html_url"], state=state, closed_at=it.get("closed_at"),
                       pr=bool(it.get("pull_request")), title=it.get("title")))
    return out, None


def later_evidence(matcher, alerts, d, log):
    """排查日之后（到 D+30 或今天）的 Slack / HubSpot / 邮件：找修复动作和验证依据。"""
    hi = min(S.shift(d, FIX_DAYS), dt.datetime.now(dt.timezone.utc).date().isoformat())
    lo = S.shift(d, 1)
    if lo > hi:
        return []
    out = slack_evidence(matcher, alerts, lo, hi, d, cap=120)
    out += [e for e in brain_evidence(matcher, lo, hi, log) if e["src"] in ("hubspot", "email")]
    return out


def config_followups(cfgs, focus, d):
    """排查日之后在后台批准的配置改动：改的正是结论里那个设置，就是最直接的验证依据（不依赖 GitHub）。"""
    out = []
    for n in focus:
        for c in S._approved(cfgs.get(n) or {}):
            t = iso(c.get("updated_at"))
            if t and t[:10] > d:
                en, zh = _change_text(n, c)
                out.append(_ev("femini", t, en, "config_change_after", actor=c.get("user_name"), approver=S.approver(c),
                               where=f"admin link {n}", url=f"https://admin.feedmob.com/click_urls/{n}", text_zh=zh, link=n,
                               change_id=c.get("id")))
    return out


# ───────────────────────── 修复状态 ─────────────────────────
CLOSED = re.compile(r"closed|resolved|done|complete|完成|关闭|已解决", re.I)


def fix_status(finding, check, current_signals, rows_by, records):
    """程序定修复状态：数据现在是否正常 + 修复动作（之后的配置改动、修复原话、已关的 GitHub 单子 / HubSpot 工单）+ 还开着的单子。"""
    link, dates = finding.get("link"), finding.get("dates") or []
    links = [link] if link else list(rows_by)
    hit = [s for s in current_signals if s["link"] in links and (not dates or s["date"] in dates)]
    had_rows = any(r.get("date") in dates for n in links for r in rows_by.get(n, [])) if dates else False
    data = "abnormal" if hit else ("normal" if had_rows else "unknown")
    rec = {e["id"]: e for e in records}
    cited = [rec[c["id"]] for c in (check or {}).get("support", []) + (check or {}).get("fix", []) if c["id"] in rec]
    gh = [e for e in cited if e["src"] == "github"]
    gh_closed = any((e.get("meta") or {}).get("state") == "closed" for e in gh)
    gh_open = any((e.get("meta") or {}).get("state") == "open" for e in gh)
    config_fix = any(e.get("kind") == "config_change_after" for e in cited)
    fixes = (check or {}).get("fix") or []
    t_closed = any(CLOSED.search(t.get("status") or "") for t in finding.get("tickets") or [])
    t_open = any(t.get("status") and not CLOSED.search(t["status"]) for t in finding.get("tickets") or [])
    acted = bool(fixes) or gh_closed or t_closed or config_fix
    if data == "normal" and acted:
        st = "fixed"
    elif acted:
        st = "handled_pending_data"
    elif gh_open or t_open:
        st = "in_progress"
    else:
        st = "no_action"
    return dict(status=st, data=data, data_signals=[f"{s['date']} {s['kind']}" for s in hit][:10], config_fix=config_fix,
                github_closed=gh_closed, github_open=gh_open, ticket_closed=t_closed, ticket_open=t_open)


# ───────────────────────── ⑥ 学习 ─────────────────────────
CHECK_DAYS = (3, 7, 14, 30)   # 排查日之后第几天自动回访（找之后的配置改动、修复原话、GitHub 单子）
VERDICT_ZH = {"consistent": "确认 +1", "partial": "部分一致 +1", "contradict": "否决 +1", "none": "未验证 +1"}
BASIS_ZH = {"config": "之后的后台改动", "later": "之后的沟通记录", "github": "GitHub", "manual": "人工验证"}
MANUAL_VERDICTS = ("consistent", "partial", "contradict")


def _phrase(query):
    p = re.sub(r"(?<![\d.])\d{3,}(?![\d.])", " ", query or "")
    p = re.sub(r"line[\s_-]*items?\s*[:：#]?\s*\S+", "line item", p, flags=re.I)
    p = re.sub(r"\s+", " ", p).strip(" ,，。.;；")
    return p if 2 <= len(p) <= 60 else None


def schedule(d, created):
    """还要回访的日子：D+3 / 7 / 14 / 30 里晚于建档日的。都已经过去（拿历史数据回测）就不用回访。"""
    return [S.shift(d, k) for k in CHECK_DAYS if S.shift(d, k) > created[:10]]


def input_path(run):
    return run_path(run["campaign"]["id"], run["date"], run["id"]).with_suffix(".input.json")


def _ref(run):
    return f"{run['campaign']['id']}/{run['date']}/{run['id']}"


def _example(run, f, cite):
    if not cite:
        return None
    return dict(source=cite["src"], ref=cite.get("link") or cite["id"], quote=(cite.get("quote") or "")[:300],
                scope=dict(links=[f["link"]] if f.get("link") else run["links"], date=run["date"]))


def _learn_one(doc, run, key, verdict, eid, example=None, new_entry=None, source="diagnose"):
    """一条结论记一次学习（全自动，不需要人）。同一条结论换了判断（回访补到依据、重新排查），先撤销上一次。→ 改动说明或 None"""
    learned = run.setdefault("learned", {})
    prev = learned.get(key)
    if eid == "new":
        if verdict not in ("consistent", "partial"):
            return None
        eid = K.add_candidate(doc, {**(new_entry or {}), "examples": [example] if example else []}, source, _ref(run))
        if not eid:
            return None
    if prev and prev["verdict"] == verdict and prev["entry"] == eid:
        return None
    if prev:
        K.unrecord(doc, prev["entry"], prev["verdict"])
    changed = K.record(doc, eid, verdict, _ref(run), phrase=_phrase(run["query"]) if verdict == "consistent" else None,
                       example=example if verdict == "consistent" else None, when=run["date"])
    learned[key] = dict(verdict=verdict, entry=eid, at=now_z())
    action = VERDICT_ZH[verdict] + (f"（撤销之前的「{VERDICT_ZH[prev['verdict']]}」）" if prev else "")
    return dict(entry=eid, action=action + ("，状态变为 " + K.entry(doc, eid)["status"] if changed else ""), at=now_z())


def _save_case(run):
    """有结论被确认（自动或人工）→ 存进回归集：原因阶段的完整输入 + 确认的归类。"""
    exp = sorted({v["entry"] for v in (run.get("learned") or {}).values() if v["verdict"] == "consistent"})
    inp = read_json(input_path(run))
    if exp and inp:
        K.save_case(f"{run['campaign']['id']}_{run['date']}_{run['id']}", dict(id=_ref(run), expected=exp, input=inp, saved_at=now_z()))


def learn(run, final=False):
    """按验证结果学习：有依据的（一致 / 部分 / 矛盾）记下；暂无依据的等回访，到最后一次回访仍没有才记「未验证」。"""
    with K.locked():
        doc = K.load()
        changes = []
        checks = run["verify"]["checks"]
        for i, (f, chk) in enumerate(zip(run["cause"]["findings"], checks)):
            v = chk["verdict"]
            if v == "none" and not final:
                continue
            c = _learn_one(doc, run, str(i), v, f["entry"], example=_example(run, f, (chk.get("support") or [None])[0]),
                           new_entry=f.get("new_entry"))
            if c:
                changes.append({**c, "basis": chk.get("basis") or []})
            if v == "contradict" and chk.get("true_cause"):
                nid = K.add_candidate(doc, {**chk["true_cause"], "examples": [x for x in [_example(run, f, (chk.get("support") or [None])[0])] if x]},
                                      "+".join(chk.get("basis") or ["later"]), _ref(run))
                if nid:
                    changes.append(dict(entry=nid, action="新候选（之后的记录显示的真实原因）", at=now_z()))
        for m in run["verify"].get("missed") or []:
            nid = K.add_candidate(doc, {**m, "examples": [x for x in [_example(run, {}, m["support"][0])] if x]},
                                  "+".join(m.get("basis") or ["later"]), _ref(run))
            if nid and not any(c["entry"] == nid for c in changes):
                changes.append(dict(entry=nid, action="新候选（原因阶段漏掉、之后的记录显示）", at=now_z()))
        if changes:
            K.save(doc, f"排查 {_ref(run)}", [c["entry"] for c in changes], actor="diagnose")
    _save_case(run)
    return changes


def apply_manual(run):
    """人工验证盖在验证结果上：人工的判断优先于 MiniMax 的（回访重算验证结果后也要再盖一次）。"""
    checks = (run.get("verify") or {}).get("checks") or []
    for k, m in (run.get("manual") or {}).items():
        i = int(k)
        if i < len(checks):
            old = checks[i]
            checks[i] = {**old, "verdict": m["verdict"], "basis": ["manual"], "manual": m,
                         "true_cause": old.get("true_cause") if m["verdict"] == "contradict" else None}


def all_manual(run):
    findings = (run.get("cause") or {}).get("findings") or []
    return bool(findings) and all(str(i) in (run.get("manual") or {}) for i in range(len(findings)))


def manual_verify(cid, date, rid, index, verdict, note="", by=""):
    """人工验证一条结论（一致 / 部分一致 / 矛盾），随时可以改判。→ (排查记录, 学习改动)
    这条结论 MiniMax 以后不再验证；每条结论都有人工验证后，排查不再自动回访。"""
    if verdict not in MANUAL_VERDICTS:
        raise ValueError("人工验证只能选：一致 / 部分一致 / 矛盾")

    def go():
        r = read_run(cid, date, rid)
        if not r or r.get("stage") != "done":
            raise ValueError("排查记录不存在，或还没跑完")
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise ValueError("结论编号不对") from None
        if not 0 <= i < len((r.get("verify") or {}).get("checks") or []):
            raise ValueError("没有这条结论")
        r.setdefault("manual", {})[str(i)] = dict(verdict=verdict, note=(note or "").strip()[:500], by=by or "", at=now_z())
        apply_manual(r)
        fu = r.get("followup") or dict(pending=[], done=[], final=True)
        if all_manual(r) and not fu.get("final"):
            fu = dict(pending=[], done=fu.get("done", []), final=True)
        r["followup"] = fu
        changes = learn(r, final=bool(fu.get("final")))
        r["learning"] = (r.get("learning") or []) + changes
        _save(r)
        return r, changes
    return _locked(cid, go)


# ───────────────────────── 回归集 ─────────────────────────
def regress(log=print, force=False):
    hist = read_json(K.regression_path()) or {"runs": []}
    last = hist["runs"][-1]["at"] if hist["runs"] else None
    if not force and last and (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)).total_seconds() < REGRESS_COOLDOWN:
        return None
    cases = K.list_cases()[-REGRESS_LIMIT:]
    if not cases:
        return None
    doc = K.load()
    ids = {e["id"] for e in K.active(doc)}
    results = []
    for c in cases:
        inp = c["input"]
        try:
            view = K.prompt_view(doc, inp["ctx"]["links"], inp["ctx"]["date"])
            out, _ = A.ask(A.cause_messages(inp["ctx"], view, inp.get("skills", ""), inp["evidence"], inp["signals"]))
            got = {f["entry"] for f in A.check_cause(out, inp["evidence"], inp["signals"], ids, inp["ctx"]["links"])["findings"]}
        except Exception as e:
            log(f"回归 {c['id']} 失败：{str(e)[:100]}")
            got = set()
        exp = set(c["expected"])
        results.append(dict(case=c["id"], expected=sorted(exp), got=sorted(got), hit=bool(exp & got), complete=exp <= got))
    n = len(results)
    summary = dict(at=now_z(), cases=n, hit_rate=round(sum(r["hit"] for r in results) / n, 3),
                   complete_rate=round(sum(r["complete"] for r in results) / n, 3), results=results)
    hist["runs"] = (hist["runs"] + [summary])[-30:]
    write_json(K.regression_path(), hist)
    return summary


def _maybe_regress(changes, log):
    if any("新候选" in c["action"] for c in changes):
        from . import curate
        curate.maybe(log=log)   # 有新候选：让字典自动整理一次（限频；整理本身会重跑回归集）
    if any("状态变为" in c["action"] or "新候选" in c["action"] for c in changes):
        try:
            regress(log=log)
        except Exception as e:
            log(f"回归集重跑失败：{str(e)[:100]}")


# ───────────────────────── 总流程 ─────────────────────────
def _locked(cid, fn):
    lock = open(STATE / f"{valid_number(cid)}.diagnose.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        return fn()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def run(cid, date, rid, log=print):
    def go():
        r = read_json(run_path(cid, date, rid))
        if not r:
            raise ValueError("排查记录不存在")
        try:
            _run(r, log)
        except Exception as e:
            _save(r, "error", error=f"{type(e).__name__}: {str(e)[:300]}", trace=traceback.format_exc()[-800:])
        return r
    return _locked(cid, go)


def _context(cid, log):
    """活动、链接、实时配置、词表：原因阶段和回访都要。"""
    info = campaign.info(cid) or {}
    links = [str(x) for x in info.get("links") or []]
    f = femini()
    cfgs = {}
    for n in links:
        try:
            cu = f.call("get_click_url", {"name": n})
            cfgs[n] = cu.get("click_url", cu)
        except Exception as e:
            log(f"链接 {n} 配置取不到，用缓存：{str(e)[:100]}")
            cfgs[n] = S.link_config(n)
    return info, links, f, cfgs, matcher_for(info, cfgs)


def _verify(r, ctx, cfgs, rows_by, signals, matcher, alerts, log):
    """⑤ 用排查日之后的记录验证、定修复状态。原因结论此前已经落盘。"""
    d, n_days = r["date"], r["range"]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    camp_name = r["campaign"].get("name")
    github, gh_err = github_evidence(r["focus"], camp_name, matcher.t["advertiser"], S.shift(d, -max(n_days, 3)),
                                     min(S.shift(d, FIX_DAYS), today), r["parsed"]["gh_refs"], log)
    records = (_assign_ids(config_followups(cfgs, r["focus"], d), "C") + _assign_ids(later_evidence(matcher, alerts, d, log), "L")
               + _assign_ids(github, "G"))
    checks, missed, usage = [], [], {}
    findings = r["cause"]["findings"]
    if findings:
        vout, usage = A.ask(A.verify_messages(ctx, findings, [e for e in records if e["id"][0] == "C"],
                                              [e for e in records if e["id"][0] == "L"], [e for e in records if e["id"][0] == "G"]))
        checks, missed = A.check_verify(vout, findings, records)
    statuses = [fix_status(fd, chk, signals, rows_by, records) for fd, chk in zip(findings, checks)]
    notes = [dict(zh=gh_err, en="GitHub not available")] if gh_err else []
    return dict(checks=checks, missed=missed, records=records, usage=usage, checked_at=now_z(),
                counts=dict(Counter(e["id"][0] for e in records))), statuses, notes


def _run(r, log):
    t0 = time.time()
    cid, d, n_days = r["campaign"]["id"], r["date"], r["range"]
    lo, hi = S.shift(d, -n_days), S.shift(d, n_days)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    hi = min(hi, today)
    doc = K.load()
    info, links, f, cfgs, matcher = _context(cid, log)
    camp = info.get("campaign") or {}
    parsed = parse_query(r["query"], links, doc)
    focus = parsed["links_in"] or links
    notes = []
    if parsed["links_out"]:
        notes.append(dict(zh=f"描述里的编号 {', '.join(parsed['links_out'])} 不属于当前活动，已忽略（只排查当前活动）",
                          en=f"Numbers {', '.join(parsed['links_out'])} are not links of this campaign and were ignored"))
    if parsed["gh_refs"]:
        notes.append(dict(zh=f"描述里的 GitHub 单号 {', '.join(parsed['gh_refs'])} 在原因分析阶段只当文字，不读取；验证阶段才会读",
                          en=f"GitHub refs {', '.join(parsed['gh_refs'])} are kept as text during cause analysis and read only for verification"))
    _save(r, "collect", parsed=parsed, window=dict(lo=lo, hi=hi), links=links, focus=focus, notes=notes)

    # ② 原因证据：没有 GitHub
    adv = matcher.t["advertiser"]
    alerts = [re.compile(p, re.I) for e in K.active(doc) for p in e.get("alert_patterns", []) if K._valid_re(p)]
    ev, rows_by = admin_evidence(f, links, focus, cfgs, lo, hi, d, log)
    signals = []
    for n in links:
        signals += S.link_signals(n, cfgs[n], rows_by.get(n, []), lo, hi, cfgs[n].get("_spend_requests"))
    signals.sort(key=lambda s: (s["link"] not in focus, s["kind"] not in parsed["symptom_kinds"], {"high": 0, "medium": 1, "low": 2}[s["severity"]], abs(
        (dt.date.fromisoformat(s["date"]) - dt.date.fromisoformat(d)).days)))
    signals = signals[:60]
    ev += day_evidence(cfgs, rows_by, focus, signals, lo, hi)
    ev += extra_admin(f, cid, adv, lo, hi, log)
    li_ev, li_notes = line_item_evidence(f, parsed["line_items"], focus, cfgs, adv, lo, hi, log)
    ev += li_ev
    notes += li_notes
    ev += slack_evidence(matcher, alerts, lo, hi, d)
    ev += brain_evidence(matcher, lo, hi, log, ticket_hi=min(S.shift(d, max(n_days, 14)), today))
    assert all(e["src"] != "github" for e in ev), "原因阶段的证据包里不能有 GitHub"
    evidence = _assign_ids(ev, "E")
    ctx = dict(campaign=camp.get("name"), advertiser=adv, links=links, date=d, range=n_days, query=r["query"],
               parsed={k: v for k, v in parsed.items() if k != "gh_refs"})
    skills = skills_text(f, log)
    view = K.prompt_view(doc, links, d, boost=set(parsed["phrase_hits"]))
    _save(r, "cause", signals=signals, evidence=evidence, notes=notes, ctx=ctx,
          sources=dict(Counter(e["src"] for e in evidence)), timings=dict(collect=round(time.time() - t0, 1)))

    # ④ 原因（MiniMax）
    t1 = time.time()
    if not P.configured():
        raise RuntimeError("没有配置 AI（secrets/ai.env），无法分析原因")
    out, usage = A.ask(A.cause_messages(ctx, view, skills, evidence, signals))
    cause = A.check_cause(out, evidence, signals, {e["id"] for e in K.active(doc)}, links, date=d, query=r["query"])
    cause["usage"] = usage
    write_json(input_path(r), dict(ctx=ctx, evidence=evidence, signals=signals, skills=skills))
    _save(r, "status", cause=cause, timings={**r["timings"], "cause": round(time.time() - t1, 1)})

    # ⑤ 验证 + 修复状态（原因已落盘，现在才看排查日之后的记录和 GitHub）
    t2 = time.time()
    verify, statuses, vnotes = _verify(r, ctx, cfgs, rows_by, signals, matcher, alerts, log)
    left = schedule(d, r["created_at"]) if r["cause"]["findings"] else []   # 没有结论就没有什么要回访验证的
    _save(r, "learn", verify=verify, fix=statuses, notes=r["notes"] + vnotes,
          followup=dict(pending=left, done=[], final=not left), timings={**r["timings"], "verify": round(time.time() - t2, 1)})

    # ⑥ 学习：有依据的现在记；暂无依据的等回访（不用回访的就是最后一次）
    changes = learn(r, final=not left) if cause["findings"] else []
    _save(r, "done", learning=changes, timings={**r["timings"], "total": round(time.time() - t0, 1)})
    _maybe_regress(changes, log)


def due_followups(today=None):
    """到期该回访的排查：→ [(活动, 日期, 编号)]"""
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    out = []
    for p in (RAW / "campaigns").glob("*/diagnose/*/*.json"):
        if p.name.endswith(".input.json"):
            continue
        r = read_json(p) or {}
        fu = r.get("followup") or {}
        if r.get("stage") == "done" and not fu.get("final") and fu.get("pending") and fu["pending"][0] <= today:
            out.append((r["campaign"]["id"], r["date"], r["id"]))
    return out


def followup(cid, date, rid, log=print, today=None):
    """回访一次：重新取排查日之后的后台改动、沟通记录、GitHub 和当前数据，再验证暂无依据的结论、更新修复状态、学习。"""
    def go():
        r = read_run(cid, date, rid)
        if not r or r.get("stage") != "done":
            return None
        now = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
        fu = r.get("followup") or dict(pending=[], done=[], final=True)
        if all_manual(r) or not (r.get("cause") or {}).get("findings"):  # 每条结论都已有人工验证、或根本没有结论：不再验证，不再回访
            r["followup"] = dict(pending=[], done=fu.get("done", []), final=True)
            _save(r)
            return r
        left = [x for x in fu.get("pending", []) if x > now]
        info, links, f, cfgs, matcher = _context(cid, log)
        lo, hi = r["window"]["lo"], r["window"]["hi"]
        rows_by = {n: S.fetch_metrics(f, n, S.shift(lo, -S.BASELINE_DAYS), hi) for n in r["focus"]}
        signals = [s for n in r["focus"] for s in S.link_signals(n, cfgs[n], rows_by[n], lo, hi)]
        alerts = [re.compile(p, re.I) for e in K.active(K.load()) for p in e.get("alert_patterns", []) if K._valid_re(p)]
        verify, statuses, _ = _verify(r, r["ctx"], cfgs, rows_by, signals, matcher, alerts, log)
        old = r["verify"]["checks"]
        # 已有依据的结论保留原判断（只更新修复状态），暂无依据的换成这次的结果
        verify["checks"] = [o if o["verdict"] != "none" else n for o, n in zip(old, verify["checks"])] if old else verify["checks"]
        r["verify"], r["fix"] = verify, statuses
        apply_manual(r)  # 已经人工验证的结论，保留人工的判断
        r["followup"] = dict(pending=left, done=fu.get("done", []) + [now], final=not left)
        changes = learn(r, final=not left)
        r["learning"] = (r.get("learning") or []) + changes
        _save(r)
        _maybe_regress(changes, log)
        return r
    return _locked(cid, go)


def scan(cid, days_back=60, log=print):
    """不用 AI：最近 days_back 天里哪些日子有信号（日期选择器上打标记）。结果缓存 1 小时。"""
    cid = valid_number(cid)
    p = camp_dir(cid) / "diagnose" / "scan.json"
    have = read_json(p)
    first, last = data_bounds(cid)   # 可选日期的范围每次现算（扫描结果缓存 1 小时，范围不缓存）
    if have and time.time() - have.get("at", 0) < 3600:
        return {**have, "first_data": first, "last_data": last}
    info = campaign.info(cid) or {}
    links = [str(x) for x in info.get("links") or []]
    today = dt.datetime.now(dt.timezone.utc).date()
    hi = (today - dt.timedelta(days=1)).isoformat()
    lo = (today - dt.timedelta(days=days_back)).isoformat()
    f = femini()
    by_day = {}
    for n in links:
        cu = S.link_config(n)
        try:
            rows = S.fetch_metrics(f, n, S.shift(lo, -S.BASELINE_DAYS), hi)
        except Exception as e:
            log(f"{n}: {str(e)[:100]}")
            continue
        for s in S.link_signals(n, cu, rows, lo, hi):
            by_day.setdefault(s["date"], []).append(dict(link=n, kind=s["kind"], severity=s["severity"], text_zh=s["text_zh"], text_en=s["text_en"]))
    spend_days = [x[0] for x in (read_json(camp_dir(cid) / "metrics.json") or {}).get("series", {}).get("gross_spend", []) if x[1]]
    doc = dict(at=time.time(), lo=lo, hi=hi, days=by_day, first_spend=min(spend_days) if spend_days else None)
    write_json(p, doc)
    return {**doc, "first_data": first, "last_data": last}
