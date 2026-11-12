"""Spend 录入信号：程序逐天核数，只陈述「哪里不对」，不做归因（归因是 AI 那一步的事）。

数据（全部只读，Femini）：
- get_click_url_spend_metrics：每天录入的 gross / net（及来源）、各来源的事件数、合作方上报成本；
- 链接配置（raw/links/<链接>/femini.json 的 click_url）：状态 / 利润率改动历史（含生效时间）、gross / net 单价及生效区间、
  计费事件来源、spend request 摘要。

信号（每条带 link、date、kind、severity、facts 和中英文一句话）：
  spend_when_paused     当天链接处于暂停状态，却录入了 spend
  spend_missing         合作方有成本 / 曝光，或按单价应有 spend，但当天没有任何 spend 记录
  zero_with_delivery    录入的是 0，但合作方有成本或明显曝光
  margin_mismatch       设了利润率，gross 和 net/(1-利润率) 对不上
  gross_rate_mismatch   按事件计费：gross ≠ 计费事件数 × gross 单价
  net_rate_mismatch     按事件计费：net ≠ 计费事件数 × net 单价
  net_cost_ratio_shift  net / 合作方成本 的比例和此前的常态（或 spend request 写明的系数）不一样
  spend_jump            gross 相对此前 14 天中位数突然翻 3 倍以上或跌到 1/3 以下
窗口外的此前 14 天只当基线，不出信号。容差：金额差 ≥ $1 且 ≥ 3%；利润率差 ≥ 1 个百分点。
"""
import datetime as dt
import re
import statistics

from .util import iso, raw_dir, read_json

STATE_OFF = {"paused", "pause", "stopped", "stop", "inactive", "ended", "disabled", "archived", "off"}
EVENT_KEYS = {"install": "installs", "installs": "installs", "first_open": "open", "open": "open"}
BASELINE_DAYS = 14
MAX_RANGE = 93           # get_click_url_spend_metrics 一次最多取多少天
ABS_TOL, REL_TOL, MARGIN_TOL, RATIO_TOL = 1.0, 0.03, 1.0, 0.05
SEVERITY = dict(spend_when_paused="high", spend_missing="high", zero_with_delivery="high", margin_mismatch="medium",
                gross_rate_mismatch="medium", net_rate_mismatch="medium", net_cost_ratio_shift="medium", spend_jump="low")
KINDS = tuple(SEVERITY)


def days(lo, hi):
    d, end = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def shift(day, n):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


# ───────────────────────── 取数 ─────────────────────────
def fetch_metrics(f, n, lo, hi):
    """链接 n 在 [lo, hi] 的每日 spend 与事件（超过 93 天分段取）。→ [行]"""
    rows, start = [], lo
    while start <= hi:
        end = min(hi, shift(start, MAX_RANGE - 1))
        res = f.call("get_click_url_spend_metrics", {"click_url_name": str(n), "start_date": start, "end_date": end})
        rows += res.get("rows") or []
        start = shift(end, 1)
    return rows


def link_config(n):
    """取数时存下的链接配置（click_url）；没有就返回空。"""
    return (read_json(raw_dir(n) / "femini.json") or {}).get("click_url") or {}


# ───────────────────────── 配置时间线 ─────────────────────────
def _approved(cu):
    return [c for c in cu.get("proposed_changes") or [] if c.get("approve_status") == "approved"]


def approver(c):
    """批准人：change_logs 里 comment=approved 的那一步的操作人（没有就是提交人）。"""
    for lg in reversed(c.get("change_logs") or []):
        if (lg.get("comment") or "").lower() == "approved":
            return lg.get("username")
    return c.get("user_name")


def _timeline(cu, field, when_keys):
    out = []
    for c in _approved(cu):
        if c.get(field) is None:
            continue
        t = next((iso(c.get(k)) for k in when_keys if iso(c.get(k))), None) or iso(c.get("updated_at"))
        if t:
            out.append((t, c[field], c))
    return sorted(out, key=lambda x: x[0])


def state_timeline(cu):
    return _timeline(cu, "new_state", ("new_state_effective_at",))


def margin_timeline(cu):
    return _timeline(cu, "new_margin", ("new_effective_at",))


def value_at(tl, ts):
    """时间线上 ts 那一刻的值和对应改动；之前没有改动 → (None, None)。"""
    cur = (None, None)
    for t, v, c in tl:
        if t <= ts:
            cur = (v, c)
        else:
            break
    return cur


def state_on(cu, day):
    """当天结束时的状态，以及从什么时候开始是这个状态。"""
    v, c = value_at(state_timeline(cu), day + "T23:59:59Z")
    since = next((t for t, vv, cc in state_timeline(cu) if cc is c), None) if c else None
    return (str(v).lower() if v is not None else None), since, c


def margin_on(cu, day):
    """当天开始和结束时设置的利润率（-1 或空 = 不按利润率算）。"""
    tl = margin_timeline(cu)

    def ok(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v < 100 else None

    a, _ = value_at(tl, day + "T00:00:00Z")
    b, c = value_at(tl, day + "T23:59:59Z")
    return ok(a), ok(b), c


def action_on(actions, day):
    for a in actions or []:
        if (a.get("effective_from") or "0000") <= day <= (a.get("effective_to") or "9999"):
            try:
                return dict(model=(a.get("pricing_model") or "").upper(), event=a.get("paid_event"), rate=float(a.get("rate")))
            except (TypeError, ValueError):
                return None
    return None


def origin_on(sources, day):
    for s in sources or []:
        if (s.get("effective_from") or "0000") <= day <= (s.get("effective_to") or "9999"):
            return s.get("event_data_origin"), s.get("source_category")
    return None, None


# ───────────────────────── 一天的数 ─────────────────────────
def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def facts_of(row):
    rec = row.get("recorded_spend") or {}
    by_origin, partner_imps, partner_clicks = {}, 0, 0
    for s in row.get("sources") or []:
        m = {k: v for k, v in (s.get("metrics") or {}).items() if v}
        by_origin[s.get("data_origin")] = m
        if s.get("category") == "partner":
            partner_imps += m.get("impressions") or 0
            partner_clicks += m.get("clicks") or 0
    costs = [c for c in row.get("reported_costs") or [] if _num(c.get("amount")) is not None]
    return dict(date=row.get("date"), gross=_num(rec.get("gross_spend")), net=_num(rec.get("net_spend")),
                gross_source=rec.get("gross_spend_source"), net_source=rec.get("net_spend_source"),
                events=by_origin, partner_impressions=partner_imps, partner_clicks=partner_clicks,
                reported_cost=round(sum(_num(c["amount"]) for c in costs), 2) if costs else None,
                cost_sources=[c.get("source") for c in costs])


def expected(cu, f, day, side):
    """按单价算应有的 gross（side=client）或 net（side=partner）。算不了 → None。"""
    act = action_on(cu.get(f"{side}_paid_actions"), day)
    if not act or act["rate"] is None:
        return None, None
    origin, _ = origin_on(cu.get(f"{side}_spend_event_sources"), day)
    ev = f["events"].get(origin) if origin else None
    if act["model"] in ("CPA", "CPI", "CPE", "CPR"):
        if ev is None or not act["event"]:
            return None, act
        key = EVENT_KEYS.get(act["event"], act["event"])
        return round((ev.get(key) or 0) * act["rate"], 2), {**act, "origin": origin, "count": ev.get(key) or 0}
    if act["model"] == "CPM":
        imps = (ev or {}).get("impressions") or f["partner_impressions"]
        return (round(imps / 1000 * act["rate"], 2), {**act, "origin": origin, "count": imps}) if imps else (None, act)
    if act["model"] == "CPC":
        clicks = (ev or {}).get("clicks") or f["partner_clicks"]
        return (round(clicks * act["rate"], 2), {**act, "origin": origin, "count": clicks}) if clicks else (None, act)
    return None, act


def _off(a, b):
    return abs(a - b) >= ABS_TOL and abs(a - b) >= REL_TOL * max(abs(a), abs(b))


_FACTOR = re.compile(r"[*×x]\s*(\d{1,3}(?:\.\d+)?)\s*%|[*×x]\s*(0?\.\d+)", re.I)


def formula_factor(spend_requests):
    """spend request 的 net 公式里写明的系数（如「net spend from Remerge * 95%」→ 0.95）。"""
    for sr in spend_requests or []:
        m = _FACTOR.search(sr.get("net_spend_formula") or "")
        if m:
            return float(m.group(1)) / 100 if m.group(1) else float(m.group(2))
    return None


def _money(x):
    return "—" if x is None else f"${x:,.2f}"


def _sig(kind, n, day, facts, zh, en):
    return dict(kind=kind, link=str(n), date=day, severity=SEVERITY[kind], facts=facts, text_zh=zh, text_en=en)


def link_signals(n, cu, rows, lo, hi, spend_requests=None):
    """链接 n 在 [lo, hi] 的信号。rows 可以带窗口前的日子（当基线用）。"""
    by_day = {r["date"]: facts_of(r) for r in rows if r.get("date")}
    base_days = [d for d in sorted(by_day) if shift(lo, -BASELINE_DAYS) <= d < lo]
    base_gross = [by_day[d]["gross"] for d in base_days if by_day[d]["gross"]]
    base_ratio = [by_day[d]["net"] / by_day[d]["reported_cost"] for d in base_days
                  if by_day[d]["net"] and by_day[d]["reported_cost"]]
    factor = formula_factor(spend_requests)
    out = []
    for day in days(lo, hi):
        f = by_day.get(day)
        if not f:
            continue
        g, ne, cost = f["gross"], f["net"], f["reported_cost"]
        state, since, _ = state_on(cu, day)
        paused = state in STATE_OFF
        exp_g, act_g = expected(cu, f, day, "client")
        exp_n, act_n = expected(cu, f, day, "partner")
        base = dict(gross=g, net=ne, reported_cost=cost, partner_impressions=f["partner_impressions"])
        if paused and ((g or 0) > 0 or (ne or 0) > 0):
            out.append(_sig("spend_when_paused", n, day, {**base, "state": state, "state_since": since},
                            f"链接 {n} 自 {str(since)[:10]} 起处于「{state}」，{day} 仍录入 gross {_money(g)}、net {_money(ne)}",
                            f"Link {n} has been '{state}' since {str(since)[:10]}, yet {day} recorded gross {_money(g)}, net {_money(ne)}"))
        if g is None and ne is None and not paused:
            why = []
            if cost:
                why.append(f"合作方成本 {_money(cost)}")
            if f["partner_impressions"]:
                why.append(f"合作方曝光 {f['partner_impressions']:,}")
            if exp_g:
                why.append(f"按单价应有 gross {_money(exp_g)}")
            if why:
                out.append(_sig("spend_missing", n, day, {**base, "expected_gross": exp_g},
                                f"链接 {n} 在 {day} 没有任何 spend 记录，但" + "、".join(why),
                                f"Link {n} has no spend record on {day}, although " + ", ".join(
                                    x.replace("合作方成本", "partner cost").replace("合作方曝光", "partner impressions").replace("按单价应有 gross", "expected gross by rate") for x in why)))
        if g == 0 and (ne or 0) == 0 and ((cost or 0) >= ABS_TOL or f["partner_impressions"] >= 1000):
            out.append(_sig("zero_with_delivery", n, day, base,
                            f"链接 {n} 在 {day} 录入为 0，但合作方成本 {_money(cost)}、曝光 {f['partner_impressions']:,}",
                            f"Link {n} recorded 0 on {day}, but partner cost {_money(cost)}, impressions {f['partner_impressions']:,}"))
        m0, m1, mc = margin_on(cu, day)
        if g and ne and g > 0 and m1 is not None:
            actual = round((g - ne) / g * 100, 2)
            if all(m is None or abs(actual - m) >= MARGIN_TOL for m in (m0, m1)):
                want = round(ne / (1 - m1 / 100), 2)
                if abs(want - g) >= ABS_TOL:
                    out.append(_sig("margin_mismatch", n, day, {**base, "actual_margin": actual, "configured_margin": m1,
                                                                "margin_at_day_start": m0, "expected_gross": want,
                                                                "margin_set_by": (mc or {}).get("user_name"), "margin_set_at": iso((mc or {}).get("updated_at"))},
                                    f"链接 {n} 在 {day} 实际利润率 {actual}%，设置是 {m1}%；按设置 gross 应为 {_money(want)}，录入 {_money(g)}",
                                    f"Link {n} on {day}: actual margin {actual}% vs configured {m1}%; gross should be {_money(want)} but is {_money(g)}"))
        elif g is not None and m1 is None and exp_g is not None and _off(g, exp_g):
            out.append(_sig("gross_rate_mismatch", n, day, {**base, "expected_gross": exp_g, "rate": act_g},
                            f"链接 {n} 在 {day} gross {_money(g)}，按 {act_g['count']} 个 {act_g['event']} × {_money(act_g['rate'])} 应为 {_money(exp_g)}",
                            f"Link {n} on {day}: gross {_money(g)}, expected {_money(exp_g)} ({act_g['count']} {act_g['event']} × {_money(act_g['rate'])})"))
        if ne is not None and exp_n is not None and not cost and _off(ne, exp_n):
            out.append(_sig("net_rate_mismatch", n, day, {**base, "expected_net": exp_n, "rate": act_n},
                            f"链接 {n} 在 {day} net {_money(ne)}，按 {act_n['count']} 个 {act_n['event']} × {_money(act_n['rate'])} 应为 {_money(exp_n)}",
                            f"Link {n} on {day}: net {_money(ne)}, expected {_money(exp_n)} ({act_n['count']} {act_n['event']} × {_money(act_n['rate'])})"))
        if ne and cost:
            ratio = ne / cost
            ref = factor if factor else (statistics.median(base_ratio) if len(base_ratio) >= 3 else None)
            if ref and abs(ratio - ref) / ref > RATIO_TOL and abs(ne - cost * ref) >= ABS_TOL:
                out.append(_sig("net_cost_ratio_shift", n, day, {**base, "ratio": round(ratio, 4), "reference": round(ref, 4),
                                                                  "reference_from": "spend_request" if factor else "baseline"},
                                f"链接 {n} 在 {day} net/合作方成本 = {ratio:.3f}，" + (f"spend request 写的是 {ref:.3f}" if factor else f"此前常态是 {ref:.3f}"),
                                f"Link {n} on {day}: net / partner cost = {ratio:.3f}, " + (f"spend request says {ref:.3f}" if factor else f"usual ratio was {ref:.3f}")))
        if g and len(base_gross) >= 5:
            med = statistics.median(base_gross)
            if med and (g / med >= 3 or g / med <= 1 / 3):
                out.append(_sig("spend_jump", n, day, {**base, "baseline_median": round(med, 2)},
                                f"链接 {n} 在 {day} gross {_money(g)}，此前 14 天中位数 {_money(med)}",
                                f"Link {n} on {day}: gross {_money(g)} vs 14-day median {_money(med)}"))
    return out


def day_summary(n, cu, row, lang="zh"):
    """一天的数据整理成一句话（证据清单用）。"""
    f = facts_of(row)
    day = f["date"]
    state, since, _ = state_on(cu, day)
    _, m1, _ = margin_on(cu, day)
    ev = "; ".join(f"{o}: " + ", ".join(f"{k}={v}" for k, v in m.items()) for o, m in f["events"].items() if m) or "—"
    if lang == "zh":
        return (f"{day} 链接 {n}：录入 gross {_money(f['gross'])}（{f['gross_source'] or '—'}）、net {_money(f['net'])}（{f['net_source'] or '—'}）；"
                f"合作方成本 {_money(f['reported_cost'])}；事件 {ev}；状态 {state or '—'}" + (f"（自 {since[:10]}）" if since else "")
                + f"；利润率设置 {m1 if m1 is not None else '—'}")
    return (f"{day} link {n}: gross {_money(f['gross'])} ({f['gross_source'] or '-'}), net {_money(f['net'])} ({f['net_source'] or '-'}); "
            f"partner cost {_money(f['reported_cost'])}; events {ev}; state {state or '-'}" + (f" (since {since[:10]})" if since else "")
            + f"; margin setting {m1 if m1 is not None else '-'}")
