"""把已取到的原始记录整理成统一事件。不联网，只读 raw/。

事件文字全部来自固定模板（中英文各一套），时间、人、原话都取自原始记录；没有人工解读。
每个事件的公共字段：key, ts(UTC), src, srcs, kind, actor, to, ticket, link, stage, margin, baton, compact, flag, clear；
中英文各自的文字放在 zh / en 两个子字典里：title, text, quote, note, flag_text, meta, ref。
"""
import json
import re
import zlib
from collections import defaultdict

import slack_source
from sources import dur, dur_en, secs

from . import brainpages, attribution_context, corpus as corpus_mod
from .attribution import ATTRIBUTION_VERSION
from . import feeds
from . import github as ghm
from .collect_mcp import channel_name
from .people import Roster, name_key
from .util import RAW, camp_dir, has_han, iso, issue_re, num_re, raw_dir, read_json, safe_name

FEMINI_URL = "https://admin.feedmob.com/click_urls/{}"
CN_NOTE = "Original text is in Chinese and is not shown on this page; open the source for the full text."

# 标签 → (阶段, 中文含义, 英文含义)。含义是按公司里常见的走法推断的，页面里会说明。
WF = {
    "Code Review": ("review", "请求代码评审", "requests code review"),
    "PR": ("review", "评审已通过", "review passed"),
    "Self Check": ("check", "合并后自检", "self-check after merge"),
    "PA Production Verify": ("verify", "交验收人验证", "ready for acceptance testing"),
    "QA": ("verify", "验收通过", "accepted"),
    "enhancement needed": ("dev", "打回返工", "sent back for rework"),
}
# 标签 → 单子当前状态的说法（用于底部「还开着」提示）
STATE_TEXT = {
    "Code Review": ("代码在评审", "code is under review", "评审通过 → 合并 → 开发自检 → 验收人验证 → 关单",
                    "approval → merge → self-check → acceptance testing → close"),
    "PR": ("评审已通过，等合并", "review passed; waiting to merge", "合并 → 开发自检 → 验收人验证 → 关单",
           "merge → self-check → acceptance testing → close"),
    "Self Check": ("代码已合并，在自检", "code merged; in self-check", "开发自检 → 验收人验证 → 关单",
                   "self-check → acceptance testing → close"),
    "PA Production Verify": ("开发已自检，等验收人验证", "self-check done; waiting for acceptance testing",
                            "验收人验证 → 关单", "acceptance testing → close"),
    "QA": ("验收通过，等关单", "accepted; waiting to close", "关单", "close"),
    "enhancement needed": ("被打回，等开发返工", "sent back; waiting for rework", "开发返工 → 重新提交评审或验证",
                           "rework → resubmit for review or testing"),
}
KIND_TAG = {  # 动作类型 → 人物标签（中, 英）；按这个顺序挑最多的
    "merge": ("合并", "Merger"), "pr": ("开发", "Developer"), "review": ("评审", "Reviewer"),
    "close": ("关单", "Closer"), "create": ("建单", "Filer"), "assign": ("派单", "Assigner"),
    "change": ("调价", "Pricing"), "label": ("流转", "Workflow"), "comment": ("评论", "Commenter"),
    "chat": ("对话", "Chat"), "notify": ("播报", "Alerts"),
}
STAGES = [("new", "建单", "Filed"), ("plan", "安排", "Assigned"), ("dev", "开发", "Development"),
          ("review", "评审", "Review"), ("merged", "合并", "Merged"), ("check", "自检", "Self-check"),
          ("verify", "验证", "Verification"), ("done", "关单", "Closed")]


# ───────────────────────── 小工具 ─────────────────────────
def clean(s, lang):
    s = re.sub(r"<img[^>]*>", "[图片]" if lang == "zh" else "[image]", s or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[图片]" if lang == "zh" else "[image]", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"^[\s\-*>]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def trunc(s, n=240):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def quote_pair(raw):
    """原话：中文页完整保留；英文页只在原文没有汉字时显示（不伪造译文）。返回 (zh, en, 英文页是否缺了原话)。"""
    z = trunc(clean(raw, "zh"))
    if not z:
        return None, None, False
    if has_han(z):
        return z, None, True
    return z, trunc(clean(raw, "en")), False


def en_title(title):
    """英文页的单子标题：有汉字就只留 [V:..|D:..] 这种方括号标签。"""
    t = (title or "").strip()
    if not has_han(t):
        return t
    tags = " ".join(x for x in re.findall(r"\[[^\]]*\]", t) if not has_han(x))
    return tags


def ph(pid):
    """人名占位符：生成每种语言的数据时，再换成该语言的名字（避免英文页带进中文简称）。"""
    return f"\x00{pid}\x01"


def tkey(repo, num):
    return str(num) if repo == "feed-mob/feedmob" else f"{repo.split('/')[1]}-{num}"


def tlabel(repo, num):
    return f"#{num}" if repo == "feed-mob/feedmob" else f"{repo.split('/')[1]}#{num}"


def ev(key, ts, src, kind, actor, zh, en, *, to=None, ticket=None, link=None, stage=None, margin=None, baton=False,
       compact=False, flag=None, srcs=None, hop=None, unread_en=False):
    return dict(key=key, ts=ts, src=src, srcs=srcs or [src], kind=kind, actor=actor, to=[t for t in (to or []) if t != actor],
                ticket=ticket, link=link, stage=stage, margin=margin, baton=baton, compact=compact, flag=flag,
                clear=[], hop=hop, zh=zh, en=en, unread_en=unread_en)


def mentions(R, text, system):
    """正文里 @ 到的、对照表里已有的人。"""
    out = []
    for m in re.findall(r"@([A-Za-z0-9][A-Za-z0-9._-]*)", text or ""):
        pid = R.index.get((system, m.casefold()))
        if pid and pid not in out:
            out.append(pid)
    if system == "slack":
        for (sysname, alias), pid in R.index.items():
            if sysname == "slack" and f"@{alias}" in (text or "").casefold() and pid not in out:
                out.append(pid)
    return out


def en_text_with_note(text_en, lost):
    return (text_en + " " + CN_NOTE).strip() if lost else text_en


# ───────────────────────── GitHub ─────────────────────────
def github_events(n, R, log):
    man = read_json(raw_dir(n) / "github.json")
    if not man:
        return [], {}, {}
    out, tickets, pr_ticket = [], {}, {}
    for h in man["selected"]:
        doc = read_json(ghm.issue_file(h["repo"], h["number"]))
        if not doc:
            log(f"  缺少单子缓存：{h['repo']}#{h['number']}（跳过）")
            continue
        for pr in doc["prs"]:
            pr_ticket.setdefault(tuple(pr), (h["repo"], h["number"]))
        out += _issue_events(doc, R, tickets)
    for h in man["prs"]:
        doc = read_json(ghm.pr_file(h["repo"], h["number"]))
        if not doc:
            continue
        parent = pr_ticket.get((h["repo"], h["number"]))
        out += _pr_events(doc, R, parent)
    return out, tickets, man


def _issue_events(doc, R, tickets):
    repo, num, iss = doc["repo"], doc["number"], doc["issue"]
    T, L = tkey(repo, num), tlabel(repo, num)
    url = iss["html_url"]
    t_zh, t_en = (iss.get("title") or "").strip(), en_title(iss.get("title"))
    tickets[T] = dict(name_zh=f"{L} {t_zh}", name_en=f"{L} {t_en}".strip(), link=url, state=iss["state"],
                      labels=[x["name"] for x in iss.get("labels", [])], updated=iss.get("updated_at"),
                      created=iss["created_at"], closed=iss.get("closed_at"), repo=repo, num=num,
                      comments=len(doc["comments"]))
    out = []
    who = lambda login: R.resolve("github", login, bot=login.endswith("[bot]"))
    sh = ph

    creator = who(iss["user"]["login"])
    qz, qe, lost = quote_pair(iss.get("body"))
    out.append(ev(f"gh:{repo}#{num}:created", iss["created_at"], "github", "create", creator,
                  dict(title=f"{sh(creator)} 建单 {L}：{t_zh}", text="", quote=qz, ref=f"GitHub {repo}#{num} · 建单"),
                  dict(title=f"{sh(creator)} files {L}" + (f": {t_en}" if t_en else ""), quote=qe,
                       text=CN_NOTE if lost else "", ref=f"GitHub {repo}#{num} · issue created"),
                  ticket=T, link=url, stage="new", hop=("create", creator)))

    evs = sorted(doc["events"], key=lambda e: e["created_at"])
    i = 0
    while i < len(evs):
        e = evs[i]
        t, ts = e["event"], e["created_at"]
        login = (e.get("actor") or {}).get("login")
        if t in ("labeled", "unlabeled"):
            grp, j = [e], i + 1
            while (j < len(evs) and evs[j]["event"] in ("labeled", "unlabeled")
                   and (evs[j].get("actor") or {}).get("login") == login and secs(ts, evs[j]["created_at"]) <= 2):
                grp.append(evs[j])
                j += 1
            rem = [g["label"]["name"] for g in grp if g["event"] == "unlabeled"]
            add = [g["label"]["name"] for g in grp if g["event"] == "labeled"]
            a = who(login or "unknown")
            new = add[-1] if add else None
            if rem and add:
                zt = f"{sh(a)} 把 {L} 的标签从「{'、'.join(rem)}」改成「{'、'.join(add)}」"
                et = f"{sh(a)} changes {L} from “{', '.join(rem)}” to “{', '.join(add)}”"
            elif add:
                zt, et = f"{sh(a)} 给 {L} 加上标签「{'、'.join(add)}」", f"{sh(a)} adds label “{', '.join(add)}” to {L}"
            else:
                zt, et = f"{sh(a)} 去掉 {L} 的标签「{'、'.join(rem)}」", f"{sh(a)} removes label “{', '.join(rem)}” from {L}"
            gl = [(x, WF[x]) for x in add if x in WF]
            stage = WF[new][0] if new in WF else None
            flag = "caution" if "enhancement needed" in add else None
            out.append(ev(f"gh:{repo}#{num}:{e['id']}", ts, "github", "label", a,
                          dict(title=zt, text=("；".join(f"「{x}」大致表示{g[1]}" for x, g in gl) + "（含义按流程推断）") if gl else "",
                               ref=f"GitHub {repo}#{num} · labeled（{login}）",
                               flag_text="标签 enhancement needed 一般表示被打回返工（按流程推断）。" if flag else None),
                          dict(title=et, text=("; ".join(f"“{x}” roughly means: {g[2]}" for x, g in gl) + " (meaning inferred from the usual workflow)") if gl else "",
                               ref=f"GitHub {repo}#{num} · labeled ({login})",
                               flag_text="The label “enhancement needed” usually means the issue was sent back for rework (inferred from the usual workflow)." if flag else None),
                          ticket=T, link=f"{url}#event-{e['id']}", stage=stage, flag=flag))
            i = j
            continue
        if t in ("assigned", "unassigned"):
            assigner = who((e.get("assigner") or e.get("actor") or {}).get("login") or "unknown")
            assignee = who((e.get("assignee") or {}).get("login") or "unknown")
            if t == "assigned":
                out.append(ev(f"gh:{repo}#{num}:{e['id']}", ts, "github", "assign", assigner,
                              dict(title=f"{sh(assigner)} 把 {L} 指派给 {sh(assignee)}", text="",
                                   ref=f"GitHub {repo}#{num} · assigned（assigner={(e.get('assigner') or {}).get('login')}）"),
                              dict(title=f"{sh(assigner)} assigns {L} to {sh(assignee)}", text="",
                                   ref=f"GitHub {repo}#{num} · assigned (assigner={(e.get('assigner') or {}).get('login')})"),
                              to=[assignee], ticket=T, link=f"{url}#event-{e['id']}", stage="plan", baton=True,
                              hop=("assign", assignee)))
            else:
                out.append(ev(f"gh:{repo}#{num}:{e['id']}", ts, "github", "assign", assigner,
                              dict(title=f"{sh(assigner)} 取消了 {sh(assignee)} 在 {L} 上的指派", text="",
                                   ref=f"GitHub {repo}#{num} · unassigned"),
                              dict(title=f"{sh(assigner)} unassigns {sh(assignee)} from {L}", text="",
                                   ref=f"GitHub {repo}#{num} · unassigned"),
                              ticket=T, link=f"{url}#event-{e['id']}", hop=("unassign", assignee)))
        elif t in ("closed", "reopened"):
            a = who(login or "unknown")
            word_zh, word_en = ("关闭", "closes") if t == "closed" else ("重新打开", "reopens")
            c = ev(f"gh:{repo}#{num}:{e['id']}", ts, "github", "close" if t == "closed" else "create", a,
                   dict(title=f"{sh(a)} {word_zh}了 {L}", text="", ref=f"GitHub {repo}#{num} · {t}"),
                   dict(title=f"{sh(a)} {word_en} {L}", text="", ref=f"GitHub {repo}#{num} · {t}"),
                   ticket=T, link=f"{url}#event-{e['id']}", stage="done" if t == "closed" else None,
                   hop=("close",) if t == "closed" else None)
            out.append(c)
        i += 1

    for c in doc["comments"]:
        a = who(c["user"]["login"])
        qz, qe, lost = quote_pair(c.get("body"))
        out.append(ev(f"gh:{repo}#{num}:c{c['id']}", c["created_at"], "github", "comment", a,
                      dict(title=f"{sh(a)} 在 {L} 评论", text="", quote=qz, ref=f"GitHub {repo}#{num} · 评论（{c['user']['login']}）"),
                      dict(title=f"{sh(a)} comments on {L}", text=CN_NOTE if lost else "", quote=qe,
                           ref=f"GitHub {repo}#{num} · comment ({c['user']['login']})"),
                      to=mentions(R, c.get("body"), "github"), ticket=T, link=c.get("html_url")))
    return out


def _pr_events(doc, R, parent):
    repo, num, p = doc["repo"], doc["number"], doc["pull"]
    short = repo.split("/")[1]
    L = f"{short}#{num}"
    T = tkey(*parent) if parent else None
    url = p.get("html_url")
    who = lambda login: R.resolve("github", login, bot=(login or "").endswith("[bot]"))
    sh = ph
    tz, te = (p.get("title") or "").strip(), en_title(p.get("title"))
    out = []
    a = who((p.get("user") or {}).get("login") or "unknown")
    stats_zh = f"{p.get('changed_files', '?')} 个文件，增加 {p.get('additions', '?')} 行、删除 {p.get('deletions', '?')} 行"
    stats_en = f"{p.get('changed_files', '?')} files, +{p.get('additions', '?')} −{p.get('deletions', '?')} lines"
    out.append(ev(f"pr:{repo}#{num}:created", p["created_at"], "github", "pr", a,
                  dict(title=f"{sh(a)} 提交代码 PR {L}：{tz}", text=stats_zh, ref=f"GitHub {repo}#{num} · 创建",
                       meta=[["代码合并请求", L], ["改动", stats_zh]]),
                  dict(title=f"{sh(a)} opens PR {L}" + (f": {te}" if te else ""), text=stats_en,
                       ref=f"GitHub {repo}#{num} · created", meta=[["Pull request", L], ["Changes", stats_en]]),
                  ticket=T, link=url, stage="review" if T else None))
    for r in doc.get("requests", []):
        if r["event"] != "review_requested" or not r.get("requested_reviewer"):
            continue
        by = who((r.get("actor") or {}).get("login") or "unknown")
        to = who(r["requested_reviewer"]["login"])
        out.append(ev(f"pr:{repo}#{num}:rq:{r['created_at']}:{to}", r["created_at"], "github", "pr", by,
                      dict(title=f"{sh(by)} 请 {sh(to)} 评审 PR {L}", text="", ref=f"GitHub {repo}#{num} · review_requested"),
                      dict(title=f"{sh(by)} asks {sh(to)} to review PR {L}", text="", ref=f"GitHub {repo}#{num} · review_requested"),
                      to=[to], ticket=T, link=url, baton=True, stage="review" if T else None, hop=("request", to)))
    for r in doc.get("reviews", []):
        st = r.get("state")
        if st not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED") or not r.get("submitted_at") or not r.get("user"):
            continue
        if st == "COMMENTED" and not (r.get("body") or "").strip():
            continue
        by = who(r["user"]["login"])
        zw, ew = {"APPROVED": ("批准了", "approves"), "CHANGES_REQUESTED": ("要求修改", "requests changes on"),
                  "COMMENTED": ("评论了", "comments on")}[st]
        qz, qe, lost = quote_pair(r.get("body"))
        flag = "caution" if st == "CHANGES_REQUESTED" else None
        out.append(ev(f"pr:{repo}#{num}:rv:{r['submitted_at']}:{by}", r["submitted_at"], "github", "review", by,
                      dict(title=f"{sh(by)} {zw} PR {L}", text="", quote=qz, ref=f"GitHub {repo}#{num} · review {st}",
                           flag_text="评审人要求修改。" if flag else None),
                      dict(title=f"{sh(by)} {ew} PR {L}", text=CN_NOTE if lost else "", quote=qe,
                           ref=f"GitHub {repo}#{num} · review {st}", flag_text="The reviewer requested changes." if flag else None),
                      ticket=T, link=url, flag=flag, hop=("reviewed", by)))
    if p.get("merged_at") and p.get("merged_by"):
        m = who(p["merged_by"]["login"])
        gap_zh = f"从创建到合并 {dur(p['created_at'], p['merged_at'])}。"
        gap_en = f"{dur_en(p['created_at'], p['merged_at'])} from creation to merge."
        out.append(ev(f"pr:{repo}#{num}:merged", p["merged_at"], "github", "merge", m,
                      dict(title=f"{sh(m)} 合并 PR {L}", text=gap_zh, ref=f"GitHub {repo}#{num} · merged（{p['merged_by']['login']}）"),
                      dict(title=f"{sh(m)} merges PR {L}", text=gap_en, ref=f"GitHub {repo}#{num} · merged ({p['merged_by']['login']})"),
                      ticket=T, link=url, stage="merged" if T else None, hop=("merged",)))
    elif p.get("state") == "closed" and p.get("closed_at"):
        out.append(ev(f"pr:{repo}#{num}:closed", p["closed_at"], "github", "close", a,
                      dict(title=f"PR {L} 被关闭（没有合并）", text="", ref=f"GitHub {repo}#{num} · closed"),
                      dict(title=f"PR {L} closed without merging", text="", ref=f"GitHub {repo}#{num} · closed"),
                      ticket=T, link=url))
    return out


# ───────────────────────── 后台（利润率等调整）─────────────────────────
def femini_events(n, R):
    doc = read_json(raw_dir(n) / "femini.json")
    if not doc:
        return [], None
    cu = doc["click_url"]
    out, prev_margin = [], None
    rows = []
    for c in cu.get("proposed_changes", []):
        if c.get("approve_status") != "approved":
            continue
        created = next((x for x in c.get("change_logs", []) if x["action"] == "create"), None)
        rows.append((iso(c.get("updated_at")), iso((created or {}).get("created_at") or c.get("created_at")), c))
    rows.sort(key=lambda r: r[0] or "")
    for approved, drafted, c in rows:
        if not approved:
            continue
        a = R.resolve("femini", c.get("user_name") or "unknown")
        sh = ph(a)
        parts_zh, parts_en, margin = [], [], None
        if c.get("new_margin") is not None:
            margin = c["new_margin"]
            m = f"{margin:g}%"
            parts_zh.append(f"利润率 {prev_margin:g}%→{m}" if prev_margin is not None else f"利润率调到 {m}")
            parts_en.append(f"margin {prev_margin:g}%→{m}" if prev_margin is not None else f"margin set to {m}")
            prev_margin = margin
        for fld, zl, el in (("new_net_cpi", "净单价", "net CPI"), ("new_gross_cpi", "毛单价", "gross CPI"),
                            ("new_hard_cap", "硬上限", "hard cap"), ("new_advertiser_cap", "广告主上限", "advertiser cap"),
                            ("new_max_switch", "最大切换", "max switch"), ("new_state", "状态", "state")):
            if c.get(fld) is not None:
                parts_zh.append(f"{zl}调到 {c[fld]}")
                parts_en.append(f"{el} set to {c[fld]}")
        if not parts_zh:
            parts_zh, parts_en = ["其他设置"], ["other settings"]
        gap = secs(drafted, approved) if drafted else None
        how_zh = (f"起草到批准只隔 {dur(drafted, approved)}" if gap is not None and gap < 120
                  else (f"起草后约 {dur(drafted, approved)}批准" if gap is not None else "批准"))
        how_en = (f"approved {dur_en(drafted, approved)} after drafting" if gap is not None else "approved")
        out.append(ev(f"fm:{c['id']}", approved, "femini", "change", a,
                      dict(title=f"{sh} 调整 {n}：{'；'.join(parts_zh)}",
                           text=f"FeedMob Admin 后台记录：{how_zh}（调整编号 {c['id']}，操作人 {c.get('user_name')}）。",
                           ref=f"FeedMob Admin get_click_url({n}) 调整 #{c['id']}"),
                      dict(title=f"{sh} updates {n}: {'; '.join(parts_en)}",
                           text=f"FeedMob Admin record: {how_en} (change #{c['id']}, by {c.get('user_name')}).",
                           ref=f"FeedMob Admin get_click_url({n}) change #{c['id']}"),
                      margin=margin, compact=True, link=FEMINI_URL.format(n)))
    return out, doc


# ───────────────────────── HubSpot 工单（经大脑，或直连副本）─────────────────────────
def _tref(tid):
    """证据里写的工单出处：直连模式是 FeedAI 里的工单（页面上显示 tickets/编号），以前是大脑里的副本。"""
    return f"tickets/{tid}" if feeds.enabled() else f"Brain tickets/{tid}"


def ticket_events(t, R, issue_keys, matcher=None):
    """一张工单（brainpages.ticket 的结果）→ (建单 + 各条备注的事件, 工单信息)。"""
    tid, title, owner, status, prio = t["id"], t["title"], t["owner"], t["status"], t["priority"]
    gh = re.search(r"GH#(\d+)", title)
    T = gh.group(1) if gh and gh.group(1) in issue_keys else f"hs{tid}"
    info = dict(key=T, id=tid, title=title, owner=owner, status=status, priority=prio, created=t["created"],
                updated=t["updated"], standalone=T.startswith("hs"))
    out = []
    sysp = R.resolve("hubspot", "__system__")
    qz, qe, lost = quote_pair(t["body"])
    ownp = R.resolve("hubspot", owner) if owner else None
    def allowed(text):
        return matcher is None or matcher.assess(dict(kind='hubspot', title=title, text=text))['accepted']
    if t['created'] and allowed(t['body']):
        meta_zh = [["HubSpot 单", f"{tid} · 优先级 {prio} · 状态 {status}"]]
        meta_en = [["HubSpot ticket", f"{tid} · priority {prio} · status {status}"]]
        out.append(ev(f"hs:{tid}:created", t["created"], "hubspot", "create", sysp,
                      dict(title=f"HubSpot 建单 #{tid}：{title}", text=f"负责人 {owner}。" if owner else "", quote=qz, meta=meta_zh,
                           ref=f"{_tref(tid)} · hubspot_created_at", note="HubSpot 副本只有建单和更新时间，没有逐次状态变更记录。"),
                      dict(title=f"HubSpot ticket #{tid} created" + (f": {en_title(title)}" if en_title(title) else ""),
                           text=(f"Owner: {owner}. " if owner else "") + (CN_NOTE if lost else ""), quote=qe, meta=meta_en,
                           ref=f"{_tref(tid)} · hubspot_created_at",
                           note="The HubSpot snapshot has only creation and update times, not individual status changes."),
                      to=[ownp] if ownp else [], ticket=T, hop=("owner", ownp) if ownp else None))
    for n in t["notes"]:
        if not allowed(n['text']):
            continue
        a = R.resolve("hubspot", n["who"]) if n["who"] else sysp
        sh = ph(a)
        qz, qe, lost = quote_pair(n["text"])
        out.append(ev(f"hs:{tid}:n{n['id']}", n["ts"], "hubspot", "comment", a,
                      dict(title=f"{sh} 在 HubSpot 单 #{tid} 留言", text="", quote=qz, ref=f"{_tref(tid)} · 备注"),
                      dict(title=f"{sh} adds a note to HubSpot ticket #{tid}", text=CN_NOTE if lost else "", quote=qe,
                           ref=f"{_tref(tid)} · note"),
                      to=mentions(R, n["text"], "slack"), ticket=T))
    return out, info


def hubspot_events(n, R, issue_keys, matcher=None):
    """链接取数时按编号/单号找到的工单。"""
    man = read_json(raw_dir(n) / "brain.json") or {}
    out, tinfo = [], []
    for slug in man.get("tickets", []):
        rec = read_json(raw_dir(n) / "tickets" / (slug.split("/")[-1] + ".json"))
        if not rec:
            continue
        pg = rec["page"]
        t = brainpages.ticket(dict(slug=slug, title=pg.get("title"), frontmatter=pg.get("frontmatter") or {},
                                   compiled_truth=pg.get("compiled_truth") or pg.get("content") or ""))
        evs, info = ticket_events(t, R, issue_keys, matcher)
        out += evs
        if evs:
            tinfo.append(info)
    return out, tinfo


def corpus_records(cid, kind, matcher=None):
    """AI 原料里与本活动强相关（点到活动名/链接号/单号，或广告主 + 两个特征词）的邮件 / 工单记录。"""
    doc = read_json(camp_dir(cid) / "corpus.json") or {}
    if matcher:
        return [r for r in doc.get('records', []) if r['kind'] == kind and matcher.assess(r)['accepted']]
    return [r for r in doc.get("records", []) if r["kind"] == kind and r.get("tier") in ("direct", "strong")]


def corpus_tickets(cid, R, issue_keys, known, matcher=None):
    """原料里强相关的工单（不只是正文写了编号的那几张）。已经由链接取到的跳过。"""
    out, tinfo = [], []
    for slug in dict.fromkeys(r["slug"] for r in corpus_records(cid, "hubspot", matcher)):
        page = read_json(brainpages.cache_dir("tickets") / (safe_name(slug) + ".json"))
        if not page:
            continue
        t = brainpages.ticket(page)
        if t["id"] in known:
            continue
        evs, info = ticket_events(t, R, issue_keys, matcher)
        out += evs
        if evs:
            tinfo.append(info)
    return out, tinfo


# ───────────────────────── 邮件（经大脑）─────────────────────────
def email_events(cid, R, matcher=None):
    """和合作方、客户的往来邮件：强相关线程里的每一封信。发件人、收件人、时间、正文都取自原信。"""
    out = []
    recs = corpus_records(cid, "email", matcher)
    senders = {r["who"] for r in recs}  # 只抄送、从没在本活动邮件里发过言的人不进人物栏，否则全是「只被提到」
    external = {n for r in recs for n in r.get("external") or []}

    def person(name):
        pid = R.resolve("email", name)
        if name in external:
            R.people[pid]["external"] = True
        return pid
    for r in recs:
        a = person(r["who"])
        to = [x for x in dict.fromkeys(person(n) for n in r.get("to") or [] if n in senders) if x != a]
        sh = ph(a)
        current_body = attribution_context.unquoted(r['text']) if matcher else r['text']
        qz, qe, lost = quote_pair(current_body)
        stamp = f"{r['ts'][:10]} {r['ts'][11:19]} UTC"
        subj = r["subject"]
        out.append(ev(f"em:{r['thread']}:{r['ts']}:{zlib.crc32(r['text'][:80].encode()) % 10**6}", r["ts"], "email", "chat", a,
                      dict(title=f"{sh} 发邮件：{subj}", text="", quote=qz, ref=f"Gmail «{subj}» · {stamp}"),
                      dict(title=f"{sh} emails: {en_title(subj)}", text=CN_NOTE if lost else "", quote=qe,
                           ref=f"Gmail «{en_title(subj)}» · {stamp}"),
                      to=to))
    return out


# ───────────────────────── Slack 消息（经大脑副本）─────────────────────────
def slack_events(n, R, issue_keys, issue_nums, matcher=None):
    man = read_json(raw_dir(n) / "brain.json") or {}
    chans = set(man.get("channels", []))
    if not chans:
        return [], 0
    msgs = [m for m in slack_source.load(raw=feeds.slack_root() if feeds.enabled() else RAW / "slack") if m["channel"] in chans]
    pn = num_re(n)
    ips = {num: issue_re(num) for num in issue_nums}
    hits = []
    for m in msgs:
        reasons = []
        mm = pn.search(m["text"])
        if mm:
            reasons.append(("click", n, mm))
        for num, p in ips.items():
            mi = p.search(m["text"])
            if mi:
                reasons.append(("issue", num, mi))
        if reasons:
            record = dict(kind='slack', text=m['text'], channel='#' + channel_name(m['channel']))
            if matcher and not matcher.assess(record)['accepted']:
                continue
            hits.append((m, reasons))
    out, seen_bot = [], set()
    for m, reasons in hits:
        a = R.resolve("slack", m["user"])
        bot = R.people[a]["kind"] == "bot"
        if bot:  # 机器人的重复播报同一天只留一条
            k = (m["user"], m["ts"][:10], m["text"][:80])
            if k in seen_bot:
                continue
            seen_bot.add(k)
        kind_r, val, mm = reasons[0]
        what_zh = f"click URL {val}" if kind_r == "click" else f"单子 #{val}"
        what_en = f"click URL {val}" if kind_r == "click" else f"issue #{val}"
        text = m["text"]
        s, e = max(0, mm.start() - 70), min(len(text), mm.end() + 170)
        ex = ("…" if s > 0 else "") + text[s:e].strip() + ("…" if e < len(text) else "")
        lost = has_han(ex)
        ch = channel_name(m["channel"])
        sh = ph(a)
        tk = next((k for k, num in issue_keys.items() if kind_r == "issue" and num == val), None)
        stamp = f"{m['ts'][:10]} {m['ts'][11:19]} UTC"
        out.append(ev(f"sl:{m['channel']}:{m['ts']}:{a}:{zlib.crc32(m['text'][:40].encode()) % 10**6}", m["ts"], "slack",
                      "notify" if bot else "chat", a,
                      dict(title=f"{sh} 在 #{ch} 提到了 {what_zh}", text="", quote=ex, ref=f"Slack #{ch} · {stamp}（副本）"),
                      dict(title=f"{sh} mentions {what_en} in #{ch}", text=CN_NOTE if lost else "",
                           quote=None if lost else ex, ref=f"Slack #{ch} · {stamp} (snapshot)"),
                      to=mentions(R, text, "slack"), ticket=tk))
    return out, len(hits)


def business_events(cid, R, matcher=None):
    """业务版挑出来的关键对话（Slack、会议、邮件、HubSpot 备注）。原话已由程序在原始记录里逐字核对；说明文字是 AI 写的。
    详细版也要收进来，否则详细版里的人和事反而比业务版少。没有业务版数据时返回空。"""
    from . import business as B
    from .curated import display_original
    state = read_json(camp_dir(cid) / "business.json") or {}
    if matcher and state.get('attribution_version') != matcher.t.get('attribution_version'):
        return []
    if not state.get("plan") or not state.get("candidates") or not all("id" in c for c in state["candidates"]):
        return []  # 业务版正在生成到一半（候选还没编号）：这次先不收
    _, _, chosen = B.select(state["candidates"], state["plan"], log=lambda *_: None)
    from .progress import fingerprint
    platform_of = {fingerprint(r): r.get("source_id") for r in (read_json(camp_dir(cid) / "corpus.json") or {}).get("records", [])
                   if r["kind"] == "meeting"}
    out = []
    material = read_json(camp_dir(cid) / 'corpus.json', {}) or {}
    by_fp = {fingerprint(r):r for r in material.get('records', [])}
    for c, e, lang in chosen:
        if matcher and (c['fp'] not in by_fp or not matcher.quote_allowed(by_fp[c['fp']], c['quote'])):
            continue
        a = R.resolve(B.system_of(c), c["speaker"])
        shown = display_original(c["quote"])
        tr = B._text(e["translation"])
        stamp = f"{c['ts'][:10]} {c['ts'][11:19]} UTC"
        where = c.get("source") or ""
        if c["kind"] == "meeting":
            teams = platform_of.get(c["fp"]) == "ken-teams"
            lab_zh, lab_en = ("Teams 会议", "Teams meeting") if teams else ("Fireflies 会议", "Fireflies meeting")
        else:
            lab_zh, lab_en = {"email": ("Gmail", "Gmail"), "hubspot": ("", "")}.get(c["kind"], ("Slack", "Slack"))
        zh_ref = " ".join(x for x in (lab_zh, where) if x) + f" · {stamp}"
        en_where = "" if has_han(where) else where
        en_ref = " ".join(x for x in (lab_en, en_where) if x) + f" · {stamp}"
        out.append(ev(f"bz:{c['fp']}:{zlib.crc32(c['quote'][:60].encode()) % 10**6}", c["ts"], c["kind"], "chat", a,
                      dict(title=B._text(e["action_zh"]), text=B._text(e["context_zh"]), quote=shown, ref=zh_ref,
                           translation=tr if lang == "en" else None),
                      dict(title=B._text(e["action_en"]), text=B._text(e["context_en"]), quote=shown if lang == "en" else tr, ref=en_ref),
                      to=mentions(R, c["quote"], "slack")))
    return out


def merge_same_name(events, R):
    """同一个人在不同系统里有不同账号（Slack 一个、后台一个），对照表里是两条记录；同名的真人合并成一个，不然人物栏里会出现两个一样的名字。
    「alejandra.segovia」（Slack 账号）和「Alejandra Segovia」（邮件署名）也算同名；留表里的那个，其次留像姓名的写法。"""
    groups = defaultdict(list)
    for e in events:
        op = e.get("hop")
        ids = [e["actor"]] + list(e["to"]) + ([op[1]] if op and len(op) > 1 else [])
        for pid in ids:
            p = R.people[pid]
            if p["kind"] == "human" and pid not in groups[name_key(p["name"])]:
                groups[name_key(p["name"])].append(pid)

    def rank(pid):
        name = R.people[pid]["name"]
        return pid in getattr(R, "dynamic", ()), not (" " in name and name != name.lower())
    canon = {}
    for pids in groups.values():
        best = min(pids, key=rank)
        canon.update({pid: best for pid in pids})
        if any(R.people[x].get("external") for x in pids):
            R.people[best]["external"] = True
    for e in events:
        e["actor"] = canon.get(e["actor"], e["actor"])
        e["to"] = list(dict.fromkeys(t for t in (canon.get(t, t) for t in e["to"]) if t != e["actor"]))
        op = e.get("hop")
        if op and len(op) > 1:
            e["hop"] = (op[0], canon.get(op[1], op[1]), *op[2:])


# ───────────────────────── 总装 ─────────────────────────
def apply_holders(events):
    """按时间顺序维护「每张单子现在谁手上有」，只在变化的事件上写 holders。"""
    H = defaultdict(dict)
    for e in events:
        t, op = e["ticket"], e.get("hop")
        e["holders_pairs"] = None
        if not t or not op:
            continue
        h, kind = H[t], op[0]
        before = dict(h)
        if kind == "create":
            h.clear()
            h[op[1]] = ("建单", "Filed")
        elif kind == "assign":
            for k in [k for k, v in h.items() if v[0] == "建单"]:
                del h[k]
            h[op[1]] = ("负责", "Assigned")
        elif kind == "unassign":
            h.pop(op[1], None)
        elif kind == "owner":
            h[op[1]] = ("负责人", "Owner")
        elif kind == "request":
            h[op[1]] = ("评审", "Review")
        elif kind == "reviewed":
            if op[1] in h and h[op[1]][0] == "评审":
                del h[op[1]]
        elif kind == "merged":
            for k in [k for k, v in h.items() if v[0] == "评审"]:
                del h[k]
        elif kind == "close":
            h.clear()
            e["clear"] = [t]
        if h != before or kind == "close":
            e["holders_pairs"] = dict(h)


def _merge_manifests(mans):
    """多条链接的 GitHub 取数清单合并成一份（页面的来源说明和核对清单用）。"""
    mans = [m for m in mans if m]
    if not mans:
        return {}
    out = dict(queried_at=min(m["queried_at"] for m in mans), mode=mans[0].get("mode"), selected=[], prs=[], skipped=[],
               mentions_only=[])
    for key in ("selected", "prs", "skipped", "mentions_only"):
        seen = set()
        for m in mans:
            for x in m.get(key, []):
                k = (x["repo"], x["number"])
                if k not in seen:
                    seen.add(k)
                    out[key].append(x)
    return out


def _merge_brain(brains):
    brains = [b for b in brains if b]
    syn = [b["slack_synced_until"] for b in brains if b.get("slack_synced_until")]
    return dict(tickets=sorted({t for b in brains for t in b.get("tickets", [])}),
                channels=sorted({c for b in brains for c in b.get("channels", [])}),
                slack_synced_until=max(syn) if syn else None,
                # 直连模式（replay/feeds.py）取的数：页面「数据来源」据此改写说法
                source="direct" if brains and all(b.get("source") == "direct" for b in brains) else "brain")


def build_events(cid, log=print):
    """一个活动 = 它下面所有链接的记录合在一起。同一条记录被多条链接命中时只留一份。"""
    from . import campaign as camp_mod
    cinfo = camp_mod.info(cid)
    if not cinfo:
        raise RuntimeError(f"活动 {cid} 还没有取过数据")
    material = read_json(camp_dir(cid) / 'corpus.json', {}) or {}
    terms = material.get('terms') if material.get('attribution_version') == ATTRIBUTION_VERSION else None
    if not terms:
        camp, links, partners, issues, _, _ = corpus_mod._campaign_inputs(cid)
        terms = attribution_context.enrich(cid, corpus_mod.build_terms(camp, links, partners, issues))
    matcher = corpus_mod.Matcher(terms)
    R = Roster()
    gh_evs, fm_evs, hs_evs, sl_evs = [], [], [], []
    tickets, hubspot, mans, femini_docs, brains, slack_hits, margin_links = {}, [], [], {}, [], 0, 0
    for n in cinfo["links"]:
        g, t, man = github_events(n, R, log)
        gh_evs += g
        tickets.update(t)
        mans.append(man)
        f, doc = femini_events(n, R)
        fm_evs += f
        margin_links += any(e["margin"] is not None for e in f)
        if doc:
            femini_docs[n] = doc
        issue_keys = {k: str(v["num"]) for k, v in t.items()}
        h, hinfo = hubspot_events(n, R, issue_keys, matcher)
        hs_evs += h
        hubspot += [x for x in hinfo if x["id"] not in {y["id"] for y in hubspot}]
        sl, hits = slack_events(n, R, issue_keys, sorted(set(issue_keys.values())), matcher)
        sl_evs += sl
        slack_hits += hits
        brains.append(read_json(raw_dir(n) / "brain.json") or {})
    all_keys = {k: str(v["num"]) for k, v in tickets.items() if v.get("num") is not None}
    h, hinfo = corpus_tickets(cid, R, all_keys, {x["id"] for x in hubspot}, matcher)
    hs_evs += h
    hubspot += hinfo
    em_evs = email_events(cid, R, matcher)
    for h in hubspot:  # 没有 GitHub 对应单的 HubSpot 工单，单独占一个「单子」
        if h["standalone"]:
            tickets[h["key"]] = dict(name_zh=f"HubSpot #{h['id']} {h['title']}",
                                     name_en=f"HubSpot #{h['id']} {en_title(h['title'])}".strip(),
                                     link=None, state="hubspot", labels=[], standalone=True)
    # 利润率图只画一条链接的走势；多条链接都有调整时混在一起没有意义，不画
    if margin_links > 1:
        for e in fm_evs:
            e["margin"] = None
    # 后台改动在哪讨论的：业务版那一步找好的（replay/changes.py），这里只读结果
    from . import changes as changes_mod
    said = {lang: changes_mod.notes(cid, lang, terms=terms) for lang in ("zh", "en")}
    for e in fm_evs:
        k = e["key"][3:]
        if k in said["zh"]:
            e["zh"].update(commentary=said["zh"][k], commentary_label="在哪讨论的")
            e["en"].update(commentary=said["en"][k], commentary_label="Where it was discussed")
    bz_evs = business_events(cid, R, matcher)
    # 同一条消息（Slack、邮件、HubSpot 备注）两边都有时留业务版那条（说明更清楚），沿用原来的单子关联。
    # 同一个人在不同系统里是不同账号，所以按「时间 + 名字」认同一条。
    def same(e):
        return e["ts"], R.people[e["actor"]]["name"].casefold()
    plain = sl_evs + em_evs + [e for e in hs_evs if e["kind"] == "comment"]
    ticket_at = {same(e): e["ticket"] for e in plain if e["ticket"]}
    dup = {same(e) for e in bz_evs}
    drop = {e["key"] for e in plain if same(e) in dup}
    sl_evs, em_evs = [e for e in sl_evs if e["key"] not in drop], [e for e in em_evs if e["key"] not in drop]
    hs_evs = [e for e in hs_evs if e["key"] not in drop]
    for e in bz_evs:
        e["ticket"] = ticket_at.get(same(e))
    events = gh_evs + fm_evs + hs_evs + sl_evs + em_evs + bz_evs
    seen, uniq = set(), []
    for e in events:
        if e["key"] not in seen:
            seen.add(e["key"])
            uniq.append(e)
    order = {"femini": 0, "hubspot": 1, "github": 2, "slack": 3, "meeting": 3, "email": 3}
    uniq.sort(key=lambda e: (e["ts"], order[e["src"]], e["key"]))
    for pid in R.dynamic:  # 表里没有的人：邮件里只用过公司外地址的，是客户或合作方（Slack 共享频道里常见）
        if R.people[pid]["kind"] == "human" and brainpages.external_name(R.people[pid]["name"]):
            R.people[pid]["external"] = True
    merge_same_name(uniq, R)
    apply_holders(uniq)
    R.used = []
    for e in uniq:  # 人物按首次出现的顺序排列
        R.touch(e["actor"])
        for t in e["to"]:
            R.touch(t)
        for pid in e["holders_pairs"] or {}:
            R.touch(pid)
    return dict(events=uniq, roster=R, tickets=tickets, gh_man=_merge_manifests(mans), links=femini_docs,
                attribution_terms=terms, attribution_version=ATTRIBUTION_VERSION,
                campaign=cinfo, hubspot=hubspot, slack_hits=slack_hits, brain=_merge_brain(brains),
                margin_hidden=margin_links > 1, missing_github=[n for n in cinfo["links"] if not (raw_dir(n) / "github.json").exists()])
