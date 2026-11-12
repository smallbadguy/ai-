"""异常排查里的三次 MiniMax 调用和程序把关。

1. 原因（cause）：字典 + skill + 信号 + 证据（没有 GitHub）→ 归类、错误配置、操作人、相关 HubSpot 工单。
2. 验证（verify）：原因结论已经落盘之后，才给模型看排查日之后发生的事——后台配置改动（C）、Slack / HubSpot / 邮件（L）、
   GitHub（G）→ 每条结论「一致 / 部分一致 / 矛盾 / 暂无依据」，以及修复动作的出处。实际使用时 GitHub 里多半还没有
   这个异常，所以不只看 GitHub；暂无依据的结论之后会自动回访（见 diagnose.py）。
3. 回归（regress）：用回归集里存好的原因输入、按当前字典重跑原因这一步，看归类还对不对。

模型只负责挑和判断，程序把关（照 changes.py / progress.py 的做法，宁可写「没找到」也不挂错）：
- 证据编号必须存在；原话逐字核对（近似的换成原文），查无就丢；
- 操作人不由模型写名字：模型只指出是哪条证据，名字取自那条原始记录的动手人（后台改动的提交人 / 批准人、Slack 发言人、HubSpot 备注人）；
- 原因结论引用了 GitHub 来源一律作废（原因阶段的证据包里本来也没有 GitHub）；
- 工单状态取自 HubSpot 原始字段，不让模型写。
"""
import json

from . import progress as P

CAUSE_VERSION = 1
ROLES = ("changed_config", "approved_config", "entered_spend", "reported", "fixed", "asked")
CONFIDENCE = ("high", "medium", "low")
SYMPTOM = ("confirmed", "not_confirmed", "insufficient")
VERDICT = ("consistent", "partial", "contradict", "none")
MAX_QUOTE = 400

CAUSE_SYSTEM = """You are a FeedMob ad-ops investigator. You explain why daily SPEND was entered wrongly (missing, wrong amount, wrong link, entered while paused, etc.) for ONE campaign around ONE date.

Campaign: "{campaign}" (advertiser {advertiser}; click URLs {links}). Anomaly date: {date} (window ±{window} days). The user described the problem as: "{query}" (a clue to check, not evidence; it may be empty or wrong).

You get:
- BUSINESS CONTEXT from FeedMob's internal skills (terminology, validation rules).
- The SPEND ANOMALY DICTIONARY: known failure patterns with data signals, alert patterns, typical phrases, which setting goes wrong and how it is fixed. Prefer these ids. Patterns marked "confirmed" have a good track record.
- DATA SIGNALS computed by a program from FeedMob Admin (facts about the numbers; each has an index S0, S1, ...).
- EVIDENCE records E001... from FeedMob Admin (configuration changes with who submitted/approved, spend requests, link settings, daily spend rows, line item lookups), Slack, HubSpot tickets, emails and meetings. GitHub is deliberately NOT available; do not mention or guess GitHub tickets.

Task:
1. symptom_check: is the user's described problem (or, if empty, any spend problem ON THE ANOMALY DATE {date} itself) supported by the data and records? "confirmed", "not_confirmed" (records show the numbers on that date are fine, or the claim is contradicted), or "insufficient". Judge the anomaly date only: problems on neighbouring days in the window are context for finding causes, never a reason to confirm the symptom for {date}; if only neighbouring days are wrong, answer "not_confirmed" and say so in the explanation.
2. findings: each distinct root cause that made spend wrong in this window. For each:
   - entry: the dictionary id that fits; "new" only if no entry fits (then fill new_entry).
   - link: the click URL concerned (one of {links}) or "" if campaign-wide; dates: affected dates (YYYY-MM-DD).
   - misconfig: the setting that was wrong — setting (e.g. click_url.state, click_url.margin, spend_request.formula, campaign_mapping), its actual value and the expected value (each written once in Chinese and once in English), and a one-sentence explanation. If the cause is not a setting (an upstream outage, a missing report), say so in the explanation and leave actual/expected empty.
   - operators: who did what, ONLY by pointing to an evidence id whose author is that person: role is one of changed_config (submitted the setting change), approved_config, entered_spend (entered or edited spend by hand), reported (raised the problem), fixed (did the fix), asked (requested something). Never name anyone who is not the author of the cited record; a person merely @-mentioned is not the actor.
   - tickets: evidence ids of HubSpot records about this problem.
   - evidence: the evidence ids that prove it, each with a verbatim quote (contiguous text copied from that record, same language, no ellipsis, max {max_quote} characters). For FeedMob Admin records you may quote the record text.
   - signals: indexes of the data signals it explains (e.g. ["S0","S3"]).
   - confidence: high (data signal + matching record), medium, or low.
   - title_zh / title_en and explain_zh / explain_en: one short sentence each. English fields must contain no Chinese characters.
3. Do not invent anything not in the records. If nothing explains the anomaly, return an empty findings list. At most 5 findings.

Output one JSON object and nothing else:
{{"symptom_check": {{"status": str, "explain_zh": str, "explain_en": str, "evidence": [str]}},
 "findings": [{{"entry": str, "new_entry": {{"name_zh": str, "name_en": str, "category": "config|upstream|system|manual", "desc_en": str, "config_target": str, "phrases": [str], "fix_en": str}} | null,
   "link": str, "dates": [str], "title_zh": str, "title_en": str, "explain_zh": str, "explain_en": str,
   "misconfig": {{"setting": str, "actual_zh": str, "actual_en": str, "expected_zh": str, "expected_en": str, "explain_zh": str, "explain_en": str}},
   "operators": [{{"evidence": str, "role": str, "note_zh": str, "note_en": str}}],
   "tickets": [str], "evidence": [{{"id": str, "quote": str}}], "signals": [str], "confidence": str}}],
 "summary_zh": str, "summary_en": str}}"""

VERIFY_SYSTEM = """You check an earlier root-cause analysis of a SPEND entry anomaly against what happened AFTERWARDS. The earlier analysis could not see these records.

Campaign: "{campaign}" (click URLs {links}). Anomaly date: {date}. User's description: "{query}".

You get the FINDINGS (F0, F1, ...) of the earlier analysis and three kinds of later records:
- CONFIG C001...: changes approved in FeedMob Admin AFTER the anomaly date (who changed which setting on which link to what).
- LATER L001...: Slack / HubSpot / email records after the anomaly date (people reporting, explaining, fixing).
- GITHUB G001...: GitHub issues with comments and state. Often there is nothing yet: a fresh anomaly usually has no GitHub ticket.

For each finding decide, using ANY of these sources:
- verdict: "consistent" (a later record supports this exact root cause: e.g. a config change after the date sets exactly the setting the finding names to the value it says is expected, a person states this cause or fixes exactly this, or a GitHub ticket describes it), "partial" (same problem area, different or incomplete detail), "contradict" (later records show a different root cause for this anomaly), or "none" (nothing later speaks to it yet).
- support: the C / L / G ids supporting the verdict, each with a verbatim quote (contiguous text from that record, max {max_quote} characters). Required unless verdict is "none".
- fix: records showing the problem was fixed or is being fixed (C / L / G ids with verbatim quotes).
- true_cause: only for "contradict": the root cause the later records show, as {{"name_zh","name_en","category","desc_en","config_target","fix_en","phrases"}}.
- reason_zh / reason_en: one sentence. English must contain no Chinese characters.
Also list "missed": root causes later records show for this anomaly that no finding covers (same shape as true_cause, plus "support": [{{"id","quote"}}]).
Judge only from the records. A record about a different link, setting or period does not count. A routine change unrelated to the finding does not count.

Output one JSON object and nothing else:
{{"checks": [{{"finding": "F0", "verdict": str, "support": [{{"id": str, "quote": str}}], "fix": [{{"id": str, "quote": str}}], "true_cause": object|null, "reason_zh": str, "reason_en": str}}],
 "missed": [object]}}"""


def _record_text(e):
    head = f"{e['id']} [{e['ts'].replace('T', ' ').rstrip('Z')} UTC] {e['src'].upper()}"
    if e.get("where"):
        head += f" {e['where']}"
    if e.get("actor"):
        head += f" | by {e['actor']}"
    if e.get("approver") and e.get("approver") != e.get("actor"):
        head += f" (approved by {e['approver']})"
    return f"{head}: {e['text']}"


def render_evidence(evidence):
    return "\n".join(_record_text(e) for e in evidence)


def render_signals(signals):
    return "\n".join(f"S{i} [{s['severity']}] {s['date']} link {s['link']} {s['kind']}: {s['text_en']} | facts {json.dumps(s['facts'], ensure_ascii=False, default=str)}"
                     for i, s in enumerate(signals)) or "(no data signals)"


def cause_messages(ctx, dictionary_view, skills, evidence, signals):
    system = CAUSE_SYSTEM.format(campaign=ctx["campaign"], advertiser=ctx["advertiser"] or "-", links=", ".join(ctx["links"]),
                                 date=ctx["date"], window=ctx["range"], query=ctx["query"] or "", max_quote=MAX_QUOTE)
    user = ("BUSINESS CONTEXT (FeedMob skills):\n" + (skills or "(not available)") +
            "\n\nSPEND ANOMALY DICTIONARY:\n" + json.dumps(dictionary_view, ensure_ascii=False) +
            "\n\nUSER-DESCRIPTION HINTS (program parse): " + json.dumps(ctx.get("parsed") or {}, ensure_ascii=False) +
            "\n\nDATA SIGNALS:\n" + render_signals(signals) +
            "\n\nEVIDENCE:\n" + render_evidence(evidence))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def verify_messages(ctx, findings, config, later, github):
    system = VERIFY_SYSTEM.format(campaign=ctx["campaign"], links=", ".join(ctx["links"]), date=ctx["date"],
                                  query=ctx["query"] or "", max_quote=MAX_QUOTE)
    fs = [dict(finding=f"F{i}", entry=f.get("entry"), link=f.get("link"), dates=f.get("dates"), title=f.get("title_en"),
               explain=f.get("explain_en"), misconfig={k: v for k, v in (f.get("misconfig") or {}).items() if not k.endswith("_zh")})
          for i, f in enumerate(findings)]
    user = ("FINDINGS:\n" + json.dumps(fs, ensure_ascii=False) + "\n\nCONFIG:\n" + (render_evidence(config) or "(no config changes after the date)") +
            "\n\nLATER:\n" + (render_evidence(later) or "(none)") + "\n\nGITHUB:\n" + (render_evidence(github) or "(none found)"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def model():
    """排查用的模型：默认 MiniMax Flash（M3 有 5 小时额度，不给排查用）；可用 REPLAY_DIAG_MODEL 换。"""
    from . import feeds
    return feeds.setting("REPLAY_DIAG_MODEL", "minimax-m3.1-flash")


def ask(messages):
    text, usage = P.chat(messages, model=model())
    try:
        return P.parse_json(text), usage
    except ValueError:
        text, usage2 = P.chat(messages + [{"role": "assistant", "content": text[-2000:]},
                                          {"role": "user", "content": "That was not valid JSON. Output only the JSON object."}], model=model())
        return P.parse_json(text), {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in ("prompt_tokens", "completion_tokens")}


# ───────────────────────── 程序把关 ─────────────────────────
def _doc(e):
    return P.build_docs([dict(kind="slack", text=e["text"])])


def verify_quote(e, quote):
    """→ 核对后的原话（近似的换成原文）；查无 → None。"""
    if not quote or not str(quote).strip():
        return None
    docs = _doc(e)
    chk, where = P.locate(str(quote)[:MAX_QUOTE * 2], docs)
    if chk == "missing":
        return None
    return P.original(quote, docs, where) if chk == "near" else str(quote).strip()


def _cites(items, ev, allow=("femini", "slack", "hubspot", "email", "meeting")):
    out = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        e = ev.get(str(c.get("id") or ""))
        if not e or e["src"] not in allow:
            continue
        q = verify_quote(e, c.get("quote"))
        if q is None and e["src"] != "femini":   # 后台结构化记录可以只引用编号，其余必须有核对过的原话
            continue
        out.append(dict(id=e["id"], src=e["src"], ts=e["ts"], actor=e.get("actor") or "", quote=q or "", link=e.get("url") or ""))
    return out


def _han(s):
    from .util import has_han
    return has_han(s)


def _en(s):
    s = str(s or "").strip()
    return "" if _han(s) else s


def check_cause(out, evidence, signals, entry_ids, links, date=None, query=""):
    """模型的原因结论 → 程序核对后的结果。date / query 给了的话，再核一遍现象：没有描述、没有结论、所查那天也没有信号，却说「已证实」，改成「未证实」。"""
    ev = {e["id"]: e for e in evidence}
    sc = out.get("symptom_check") or {}
    symptom = dict(status=sc.get("status") if sc.get("status") in SYMPTOM else "insufficient",
                   explain_zh=str(sc.get("explain_zh") or ""), explain_en=_en(sc.get("explain_en")),
                   evidence=[x for x in (sc.get("evidence") or []) if x in ev])
    findings, dropped = [], []
    for f in (out.get("findings") or [])[:5]:
        if not isinstance(f, dict):
            continue
        entry = str(f.get("entry") or "")
        new = f.get("new_entry") if isinstance(f.get("new_entry"), dict) else None
        if entry not in entry_ids:
            entry = "new" if new and (new.get("name_en") or new.get("name_zh")) else ""
        cites = _cites(f.get("evidence"), ev)
        sigs = [s for s in (f.get("signals") or []) if isinstance(s, str) and s[1:].isdigit() and int(s[1:]) < len(signals)]
        if not entry or not (cites or sigs):
            dropped.append(dict(title=f.get("title_zh") or f.get("title_en"), reason="没有可核对的证据或信号" if entry else "归类无效"))
            continue
        ops = []
        for o in f.get("operators") or []:
            e = ev.get(str((o or {}).get("evidence") or ""))
            role = (o or {}).get("role")
            if not e or e["src"] == "github" or role not in ROLES:
                continue
            name = e.get("approver") if role == "approved_config" else e.get("actor")
            if not name:
                continue
            ops.append(dict(name=name, role=role, evidence=e["id"], src=e["src"], ts=e["ts"], url=e.get("url") or "",
                            note_zh=str(o.get("note_zh") or ""), note_en=_en(o.get("note_en"))))
        tickets = []
        for t in f.get("tickets") or []:
            e = ev.get(str(t))
            if e and e["src"] == "hubspot":
                m = e.get("meta") or {}
                if not any(x["ticket"] == m.get("ticket") for x in tickets):
                    tickets.append(dict(ticket=m.get("ticket"), title=m.get("title"), status=m.get("status"), owner=m.get("owner"),
                                        evidence=e["id"], url=e.get("url") or ""))
        mc = f.get("misconfig") if isinstance(f.get("misconfig"), dict) else {}
        link = str(f.get("link") or "")
        findings.append(dict(
            entry=entry, new_entry=new if entry == "new" else None, link=link if link in links else "",
            dates=[str(d)[:10] for d in (f.get("dates") or []) if str(d)[:4].isdigit()][:31],
            title_zh=str(f.get("title_zh") or ""), title_en=_en(f.get("title_en")),
            explain_zh=str(f.get("explain_zh") or ""), explain_en=_en(f.get("explain_en")),
            misconfig=dict(setting=str(mc.get("setting") or ""),
                           actual_zh=str(mc.get("actual_zh") or mc.get("actual") or ""), actual_en=_en(mc.get("actual_en") or mc.get("actual")),
                           expected_zh=str(mc.get("expected_zh") or mc.get("expected") or ""), expected_en=_en(mc.get("expected_en") or mc.get("expected")),
                           explain_zh=str(mc.get("explain_zh") or ""), explain_en=_en(mc.get("explain_en"))),
            operators=ops, tickets=tickets, evidence=cites, signals=sigs,
            confidence=f.get("confidence") if f.get("confidence") in CONFIDENCE else "low"))
    if date and symptom["status"] == "confirmed" and not findings and not (query or "").strip() and not any(s["date"] == date for s in signals):
        others = sorted({s["date"] for s in signals})
        symptom.update(status="not_confirmed",
                       explain_zh=f"{date} 当天的 spend 录入没有发现异常信号" + (f"；窗口内其他日期有信号：{', '.join(others)}（不是所查日期，仅供参考）" if others else ""),
                       explain_en=f"No anomaly signal on {date} itself" + (f"; signals on other days in the window: {', '.join(others)} (not the requested date, for reference only)" if others else ""))
    return dict(symptom=symptom, findings=findings, dropped=dropped,
                summary_zh=str(out.get("summary_zh") or ""), summary_en=_en(out.get("summary_en")))


BASIS = {"C": "config", "L": "later", "G": "github"}
VERIFY_SRC = ("femini", "github", "slack", "hubspot", "email", "meeting")


def check_verify(out, findings, records):
    """records：C / L / G 三类后续记录合在一起。→ (每条结论的验证结果, 漏掉的原因)"""
    rec = {e["id"]: e for e in records}
    res = []
    by = {str(c.get("finding")): c for c in (out.get("checks") or []) if isinstance(c, dict)}
    for i, _ in enumerate(findings):
        c = by.get(f"F{i}") or {}
        cites = _cites(c.get("support") or c.get("github"), rec, allow=VERIFY_SRC)
        verdict = c.get("verdict") if c.get("verdict") in VERDICT else "none"
        if verdict != "none" and not cites:
            verdict = "none"
        fix = _cites(c.get("fix"), rec, allow=VERIFY_SRC)
        tc = c.get("true_cause") if verdict == "contradict" and isinstance(c.get("true_cause"), dict) else None
        basis = sorted({BASIS.get(x["id"][:1], "other") for x in cites}) if verdict != "none" else []
        res.append(dict(verdict=verdict, support=cites, fix=fix, basis=basis, true_cause=tc,
                        reason_zh=str(c.get("reason_zh") or ""), reason_en=_en(c.get("reason_en"))))
    missed = []
    for m in out.get("missed") or []:
        if isinstance(m, dict):
            cites = _cites(m.get("support") or m.get("github"), rec, allow=VERIFY_SRC)
            if cites and (m.get("name_en") or m.get("name_zh")):
                missed.append({**m, "support": cites, "basis": sorted({BASIS.get(x["id"][:1], "other") for x in cites})})
    return res, missed
