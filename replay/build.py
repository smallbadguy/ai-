"""由已取到的原始数据生成某个编号的回放：out/<编号>/case.{zh,en}.json 和 replay.{zh,en}.html。不联网。"""
import base64
import functools
import json
import re
from collections import Counter

from sources import dur, dur_en, md_en, md_zh, secs
from translate_ui import english_ui

from . import events as E
from .scene import scene_document
from .util import ROOT, has_han, now_z, out_dir, write_json

TEMPLATE = ROOT / "src" / "template.html"
FONTS = ROOT / "src" / "vendor" / "fonts"
FONT_FACES = (("Lato", "400", "lato-400.woff2"), ("Lato", "700", "lato-700.woff2"), ("Lato", "900", "lato-900.woff2"),
              ("JetBrains Mono", "100 800", "jetbrains-mono.woff2"))
ACT_GAPS_DAYS = [5, 10, 20, 40]


def _acts(evs):
    """按沉默间隔切「段」：间隔 ≥5 天就另起一段；段太多就放宽间隔，最多 6 段。"""
    ts = [e["ts"] for e in evs]
    for gap in ACT_GAPS_DAYS + [10**6]:
        cuts = [0] + [i for i in range(1, len(ts)) if secs(ts[i - 1], ts[i]) >= gap * 86400]
        if len(cuts) <= 6:
            break
    bounds = cuts + [len(ts)]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(len(cuts))]


HAN_RUN = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]+")
PH = re.compile(r"\x00([^\x01]+)\x01")


def disp(R, pid, lang, field="label"):
    """某个人在某种语言里的显示名。field: name / short / label（句子里的称呼）。"""
    p = R.people[pid]
    if lang == "zh":
        return (p.get("label_zh") or p["short"]) if field == "label" else p[field]
    en = (p.get("label_en") or p.get("short_en") or p["short"]) if field == "label" else (p.get(f"{field}_en") or p[field])
    return pid if has_han(en) else en


def _sub(s, lang, R, stats):
    """占位符换成名字；英文页里意外出现的汉字（比如账号名）换成省略号并计数，不让整个案子生成失败。"""
    if not isinstance(s, str):
        return s
    s = PH.sub(lambda m: disp(R, m.group(1), lang), s)
    if lang == "en" and has_han(s):
        stats["sanitized"] += 1
        s = HAN_RUN.sub("…", s)
    return s


def _deep(o, lang, R, stats):
    if isinstance(o, dict):
        return {k: _deep(v, lang, R, stats) for k, v in o.items()}
    if isinstance(o, list):
        return [_deep(v, lang, R, stats) for v in o]
    return _sub(o, lang, R, stats)


def _flat(e, lang, idx, act_id):
    d = e[lang]
    pairs = e["holders_pairs"]
    holders = None if pairs is None else {pid: v[0 if lang == "zh" else 1] for pid, v in pairs.items()}
    return dict(key=e["key"], ts=e["ts"], src=e["src"], srcs=e["srcs"], kind=e["kind"], actor=e["actor"], who=None,
                to=e["to"], title=d["title"], quote=d.get("quote"), translation=d.get("translation"), text=d.get("text") or None, meta=d.get("meta") or [],
                link=e["link"], ref=d.get("ref"), ticket=e["ticket"], holders=holders, clear=e["clear"], stage=e["stage"],
                margin=e["margin"], baton=e["baton"], compact=e["compact"], prec="sec", note=d.get("note"),
                commentary=d.get("commentary"), commentary_label=d.get("commentary_label"),
                flag=e["flag"], flag_text=d.get("flag_text"), id=f"e{idx + 1:02d}", act=act_id)


def _people(ctx, evs, lang, campaign_roles=None):
    R = ctx["roster"]
    campaign_roles = campaign_roles or {}
    cnt = {pid: Counter() for pid in R.used}
    for e in evs:
        cnt[e["actor"]][e["kind"]] += 1
    out = []
    for pid in R.used:
        p = R.people[pid]
        total = sum(cnt[pid].values())
        best = next((k for k in E.KIND_TAG if cnt[pid][k] == max(cnt[pid].values(), default=0) and cnt[pid][k] > 0), None) \
            if total else None
        tag = E.KIND_TAG[best][0 if lang == "zh" else 1] if best else ("出场" if lang == "zh" else "Mentioned")
        name, short = disp(R, pid, lang, "name"), disp(R, pid, lang, "short")
        out.append(dict(
            id=pid, name=name, short=short, kind=p["kind"], color=p["color"], tag=tag, external=bool(p.get("external")),
            role=(f"本案里 {total} 个动作" if lang == "zh" else f"{total} actions in this case") if total
            else ("只被提到、没有自己的动作" if lang == "zh" else "Mentioned only; no actions of their own"),
            job_title=R.job_title(pid, lang) if p["kind"] == "human" else None,
            responsibilities=[item[lang] for item in campaign_roles.get(pid, [])]))
    return out


def _tickets(ctx, lang):
    out = {}
    for k, t in ctx["tickets"].items():
        out[k] = dict(name=t["name_zh"] if lang == "zh" else t["name_en"], link=t.get("link"), stages=True, chain=[])
    return out


def _watch(ctx, evs, lang):
    z = lang == "zh"
    opens = [(k, t) for k, t in ctx["tickets"].items() if t.get("state") == "open"]
    if not opens:
        last = evs[-1]
        return dict(ticket=None, head="所选单子都已关闭，或本案没有关联的 GitHub 单子" if z else "All selected issues are closed, or this case has no linked GitHub issue",
                    text=("最后一个动作：" if z else "Last action: ") + last[lang]["title"])
    k, t = max(opens, key=lambda kt: kt[1].get("updated") or "")
    L = f"#{t['num']}" if t["repo"] == "feed-mob/feedmob" else f"{t['repo'].split('/')[1]}#{t['num']}"
    label = next((x for x in t["labels"] if x in E.STATE_TEXT), None)
    st = E.STATE_TEXT.get(label)
    wait = None
    for e in evs:  # 最后一次请求评审之后，还没有评审/合并 → 在等评审
        if e["ticket"] != k or not e.get("hop"):
            continue
        if e["hop"][0] == "request":
            wait = e["hop"][1]
        elif e["hop"][0] in ("reviewed", "merged"):
            wait = None
    R = ctx["roster"]
    if z:
        head = f"{L} 还开着：" + (st[0] if st else "当前标签 " + (", ".join(t["labels"]) or "无"))
        if wait and label == "Code Review":
            head += f"（在等 {disp(R, wait, 'zh')} 评审）"
        text = ("接下来（按流程推断，尚未发生）：" + st[2]) if st else "没有可识别的流程标签，无法推断下一步。"
    else:
        head = f"{L} is still open: " + (st[1] if st else "current labels: " + (", ".join(t["labels"]) or "none"))
        if wait and label == "Code Review":
            head += f" (waiting for {disp(R, wait, 'en')} to review)"
        text = ("Likely next steps (inferred from the usual workflow; not yet observed): " + st[3]) if st else "No recognizable workflow label, so the next step cannot be inferred."
    return dict(ticket=k, head=head, text=text)


def _slack_mix(evs, lang):
    """Slack 消息有两条路进来：写到编号或单号的消息（sl:），和 AI 从广告主频道的对话里挑出的关键发言（bz:）。
    返回 (总条数, 前一种条数, 后一种条数, 频道说明)。"""
    z = lang == "zh"
    sl = [e for e in evs if e["src"] == "slack"]
    named, picked = sum(e["key"].startswith("sl:") for e in sl), sum(e["key"].startswith("bz:") for e in sl)
    chans = []
    for e in sl:
        m = re.match(r"Slack (#[^\s·（(]+)", e["zh"].get("ref") or "")
        if m and m.group(1) not in chans:
            chans.append(m.group(1))
    head = ("、" if z else ", ").join(chans[:3])
    if len(chans) > 3:
        head += f" 等 {len(chans)} 个频道" if z else f" and {len(chans) - 3} more channels"
    return len(sl), named, picked, head


def _sources(ctx, st, lang):
    z = lang == "zh"
    gh, brain = st.get("github", {}), ctx["brain"]
    direct = brain.get("source") == "direct"  # 直连 FeedAI / Fireflies 取的数（replay/feeds.py），不再经过大脑
    man = ctx["gh_man"]
    gh_asof = man.get("queried_at") if man else None
    if man and not ctx["missing_github"]:
        gh_status = "ok"
        lim = (f"数据截至 {gh_asof}（UTC）。" if z else f"Data as of {gh_asof} UTC. ") + \
              (f"另有 {len(man['skipped'])} 张提到该编号的单子没有展开。" if z else f"{len(man['skipped'])} more issues mention this number but were not expanded.") if man["skipped"] else \
              (f"数据截至 {gh_asof}（UTC）。" if z else f"Data as of {gh_asof} UTC.")
    elif man:
        gh_status = "limited"
        k = len(ctx["missing_github"])
        lim = (f"有 {k} 条链接的 GitHub 数据还没有取到（需要在有 GitHub 权限的机器上取一次）。" if z else
               f"GitHub data for {k} of the links has not been fetched yet (it needs to be fetched once from a machine with GitHub access).")
    else:
        gh_status = "limited"
        lim = "这个活动的 GitHub 数据还没有取到（需要在有 GitHub 权限的机器上取一次）。" if z else "GitHub data for this campaign has not been fetched yet (it needs to be fetched once from a machine with GitHub access)."
    synced = brain.get("slack_synced_until")
    n_sl, _, picked, chans = _slack_mix(ctx["events"], lang)
    if synced and direct:
        slack_lim = f"生成时直接从 FeedAI 读取，数据截至 {synced}（UTC）。" if z else f"Read directly from FeedAI at generation time; data as of {synced} UTC."
    elif synced:
        slack_lim = f"副本每天凌晨同步一次，当前同步到 {synced}（UTC）。" if z else f"The snapshot syncs daily; currently synced to {synced} UTC."
    elif n_sl:  # 没有频道直接写到编号，消息全是从广告主频道的对话里挑出来的
        slack_lim = (f"没有频道直接写到这个编号；这里的 {n_sl} 条消息是 AI 从 {chans} 的对话里挑出的关键发言。" if z else
                     f"No channel names this number directly; the {n_sl} messages here are key remarks the AI picked from conversations in {chans}.")
    else:
        slack_lim = "没有找到讲到该编号的频道。" if z else "No channel discussing this number was found."
    return [
        dict(id="github", name="GitHub", status=gh_status, head="GitHub：只读" if z else "GitHub: read-only",
             pts=(["按编号搜索，只展开标题或正文真的提到该编号的单子", "单子的指派、标签、评论，以及关联的代码合并请求（谁提交、谁评审、谁合并）"] if z else
                  ["Search by number; only issues whose title or body mention it are expanded", "Assignments, labels, comments and linked pull requests (who opened, reviewed, merged)"]),
             lim=lim + ("　单子正文里的截图没有读。" if z else " Screenshots in issue bodies were not read.")),
        dict(id="hubspot", name="HubSpot", status="ok", head=(("HubSpot：FeedAI 里的只读数据" if z else "HubSpot: read-only data in FeedAI") if direct else
                                                    ("HubSpot：公司大脑里的只读副本" if z else "HubSpot: read-only Brain snapshot")),
             pts=(["点到本活动的工单（写了活动名、链接号或单号，或同时写了广告主和活动的特征词）：负责人、优先级、状态、备注（备注带时间和作者）"] if z else
                  ["Tickets about this campaign (naming the campaign, its links or issues, or the advertiser together with the campaign's keywords): owner, priority, status and dated notes"]),
             lim=(f"找到 {len(ctx['hubspot'])} 张工单；副本没有逐项状态变更记录。" if z else
                  f"{len(ctx['hubspot'])} tickets found; the snapshot has no individual status-change history.")),
        dict(id="slack", name="Slack", status="ok", head=(("Slack：FeedAI 里的只读消息，没有打开 Slack" if z else "Slack: read-only messages in FeedAI; Slack itself was not opened") if direct else
                  ("Slack：公司大脑里的只读副本，没有打开 Slack" if z else "Slack: read-only Brain snapshot; Slack itself was not opened")),
             pts=(["先确定哪些频道讲到过这个编号，再把这些频道按天全部读一遍", "收录明确写了该编号或相关单号的消息（摘短句），以及 AI 从整段对话里挑出的关键发言，原话由程序逐字核对"] if z else
                  ["First find the channels that discuss this number, then read those channels day by day", "Included: messages that name the number or its issues (short excerpts), plus key remarks the AI picked from the full conversation, each quote checked word for word by program"]),
             lim=slack_lim),
        dict(id="femini", name="FeedMob Admin 后台" if z else "FeedMob Admin", status="ok", head="FeedMob Admin 后台：只读查询" if z else "FeedMob Admin: read-only queries",
             pts=(["该活动下的链接、渠道，以及已批准的价格、利润率等调整（谁起草、谁批准，精确到秒）"] if z else
                  ["The links and partners under this campaign, and approved price / margin changes (who drafted, who approved, to the second)"]),
             lim="FeedMob Admin 后台还有能改数据的功能，只用了查询。" if z else "FeedMob Admin also has write functions; only queries were used."),
        dict(id="meeting", name=("Fireflies 会议" if z else "Fireflies meetings") if direct else ("Fireflies / Teams 会议" if z else "Fireflies / Teams meetings"), status="ok",
             head=(("会议：直接从 Fireflies 拉取的逐字稿，加上 FeedAI、Femini 里存的 Fireflies 逐字稿和历史 Gong 通话" if z else
                    "Meetings: transcripts pulled directly from Fireflies, plus Fireflies transcripts kept in FeedAI and Femini, and past Gong calls") if direct else
                   ("会议：Fireflies 和 Teams 录下、同步进公司大脑的逐字稿" if z else "Meetings: Fireflies and Teams transcripts synced into the Brain")),
             pts=(["与该活动相关的会议逐字稿，由 AI 挑出关键发言，原话由程序逐字核对"] if z else
                  ["Transcripts of meetings about this campaign; the AI picks key remarks and each quote is checked word for word by program"]),
             lim="只用了有真人姓名的发言，「Speaker N」这类无法确认是谁的发言没有用。" if z else "Only remarks with a real speaker name are used; anonymous “Speaker N” turns are left out."),
        dict(id="email", name="Gmail 邮件" if z else "Gmail", status="ok" if any(e["src"] == "email" for e in ctx["events"]) else "limited",
             head=(("Gmail 邮件：FeedAI 里的只读副本" if z else "Gmail: read-only copy in FeedAI") if direct else
                   ("Gmail 邮件：同步进公司大脑的只读副本" if z else "Gmail: read-only Brain snapshot")),
             pts=(["和合作方、客户的往来邮件：点到本活动的线程整串收录，每封信的发件人、收件人、时间取自原信"] if z else
                  ["Emails with partners and the client: whole threads about this campaign, with sender, recipients and time from each message"]),
             lim=(("只收录 FeedAI 能读到的邮箱。" if z else "Only mailboxes FeedAI can read are included.") if direct else
                  ("只收录同步进公司大脑的邮箱。" if z else "Only mailboxes synced into the Brain are included."))),
    ]


def _checks(ctx, evs, st, lang):
    z = lang == "zh"
    c = []
    add = lambda lvl, tz, bz, te, be: c.append(dict(level=lvl, title=tz if z else te, text=bz if z else be))
    add("info", "时间都是 UTC", "GitHub、Slack、Gmail、HubSpot、FeedMob Admin 后台用的都是 UTC。页面默认换算成北京时间（+8 小时），右上角可以切回 UTC。",
        "Times are in UTC", "GitHub, Slack, Gmail, HubSpot and FeedMob Admin all use UTC. The page shows Beijing time (UTC+8) by default; use the toggle at the top right for UTC.")
    add("info", "本页由系统自动生成", "时间、人、原话都取自原始记录。收录写了编号或单号的记录、点到本活动的邮件和 HubSpot 工单，以及 AI 从对话里挑出的关键发言（说明文字由 AI 整理，原话由程序逐字核对）。",
        "This page is generated automatically", "Times, people and quotes come straight from the source records. It includes records that name the number or its issues, emails and HubSpot tickets about this campaign, and key remarks picked by AI from conversations (the AI writes the descriptions; each quote is checked word for word by program).")
    man = ctx["gh_man"]
    if not man:
        add("warn", "GitHub 数据还没有取到", "这个活动没有 GitHub 缓存。需要在有 GitHub 权限的机器上取一次，再同步过来。",
            "GitHub data not fetched yet", "There is no GitHub cache for this campaign. Fetch it once from a machine with GitHub access and sync it over.")
    else:
        mode = "服务器没有 GitHub 凭证，用的是同步过来的数据" if st.get("github", {}).get("status") == "skipped" else "本次直接取数"
        mode_en = "the server has no GitHub credentials, so synced data is used" if st.get("github", {}).get("status") == "skipped" else "fetched directly this run"
        add("warn" if st.get("github", {}).get("status") == "skipped" else "info", f"GitHub 数据截至 {man['queried_at']}",
            f"{mode}。更晚发生的 GitHub 动作要等下一次取数才会出现。", f"GitHub data as of {man['queried_at']}",
            f"{mode_en[0].upper() + mode_en[1:]}. Later GitHub activity appears after the next fetch.")
        if ctx["missing_github"]:
            k = len(ctx["missing_github"])
            add("warn", f"{k} 条链接没有 GitHub 数据", "这些链接的 GitHub 记录还没取到，页面里看不到它们的工单动作。",
                f"{k} links have no GitHub data", "GitHub records for these links have not been fetched, so their issue activity is not shown.")
        if man["skipped"]:
            lst = "、".join(f"{x['repo'].split('/')[1]}#{x['number']}" for x in man["skipped"][:8])
            add("warn", f"还有 {len(man['skipped'])} 张相关单子没有展开", f"只展开最近更新的 {len(man['selected'])} 张；其余：{lst}。",
                f"{len(man['skipped'])} more related issues not expanded", f"Only the {len(man['selected'])} most recently updated issues are expanded; the rest: {lst}.")
        if man["mentions_only"]:
            add("info", f"{len(man['mentions_only'])} 张单子只在评论里提到该编号", "这些单子的标题和正文没写这个编号，所以没有展开。",
                f"{len(man['mentions_only'])} issues mention the number only in comments", "Their titles and bodies do not contain the number, so they were not expanded.")
    failed = {**(st.get("github") or {}).get("failed", {}), **(st.get("links") or {}).get("failed", {})}
    if failed:
        names = "、".join(sorted(failed)[:8]) if z else ", ".join(sorted(failed)[:8])
        add("warn", f"{len(failed)} 条链接这次取数失败", f"失败的链接：{names}。页面沿用这些链接上一次取到的数据（如果有）。",
            f"{len(failed)} links failed to refresh", f"Failed links: {names}. The page keeps the last data fetched for them, if any.")
    extra = ctx["campaign"].get("links_not_expanded") or []
    if extra:
        add("warn", f"还有 {len(extra)} 条链接没有展开", f"一个活动最多展开 {len(ctx['campaign']['links'])} 条链接（编号大的优先）；其余：{'、'.join(extra[:8])}。",
            f"{len(extra)} more links not expanded", f"At most {len(ctx['campaign']['links'])} links per campaign are expanded (highest numbers first); the rest: {', '.join(extra[:8])}.")
    if ctx["margin_hidden"]:
        add("info", "利润率走势图没有画", "这个活动有多条链接都调过利润率，混在一张图里没有意义。每次调整仍在时间线里。",
            "Margin chart not drawn", "More than one link of this campaign had margin changes, so one chart would be misleading. Each change is still on the timeline.")
    synced = ctx["brain"].get("slack_synced_until")
    direct = ctx["brain"].get("source") == "direct"
    if synced and direct:
        add("info", f"Slack 数据截至 {synced}", "生成时直接从 FeedAI 读取；之后的聊天要再刷新一次才会进来。",
            f"Slack data as of {synced}", "Read directly from FeedAI at generation time; later chat comes in with the next refresh.")
    elif synced:
        add("warn", f"Slack 副本同步到 {synced}", "副本每天凌晨同步一次，更晚的聊天看不到。",
            f"Slack snapshot synced to {synced}", "The snapshot syncs once a day, so later chat is not visible.")
    if ctx["hubspot"]:
        add("info", "HubSpot 没有状态变更记录", "副本只有建单时间、更新时间和现状，没有「谁在几点改了状态」。",
            "No HubSpot status-change history", "The snapshot has creation and update times and the current state, but not who changed which status when.")
    if any(E.WF.get(x) for t in ctx["tickets"].values() for x in t.get("labels", [])) or any("review" == e["kind"] for e in evs):
        add("info", "标签含义靠推断", "Code Review、PR、Self Check、PA Production Verify、QA、enhancement needed 的含义是按公司里常见的走法推的：提交评审 → 评审通过 → 合并后自检 → 交验收人验证 → 验收通过 → 关单；打回用 enhancement needed。不是官方定义。",
            "Label meanings are inferred", "Code Review, PR, Self Check, PA Production Verify, QA and enhancement needed are read from the usual workflow: submit for review → review passed → self-check after merge → acceptance testing → accepted → close; sent back is enhancement needed. This is not an official definition.")
    add("info", "谁指派谁", "GitHub 指派事件里，动手指派的人看 assigner 字段，不是被指派的人。回放里的「谁指派谁」都取自 assigner。",
        "Who assigned whom", "In GitHub assigned events the person who made the assignment is the assigner, not the assignee. The replay uses assigner.")
    if direct:
        add("info", "只读", "整个回放只读：数据来自 GitHub、FeedAI 里的 Slack / Gmail / HubSpot、Fireflies 会议（直接拉取，另含 FeedAI、Femini 里存的会议逐字稿），以及 FeedMob Admin 后台的只读查询。没有向任何系统写入内容。",
            "Read-only", "The replay is read-only: data comes from GitHub, read-only Slack, Gmail and HubSpot data in FeedAI, Fireflies meetings (pulled directly, plus meeting transcripts kept in FeedAI and Femini), and read-only admin queries. Nothing was written to any system.")
    else:
        add("info", "只读", "整个回放只读：数据来自 GitHub、公司大脑里的 Slack / Gmail / HubSpot / Fireflies / Teams 只读副本、FeedMob Admin 后台的只读查询。没有向任何系统写入内容。",
            "Read-only", "The replay is read-only: data comes from GitHub, the read-only Brain snapshots of HubSpot and Slack, and read-only admin queries. Nothing was written to any system.")
    return c


def assemble(cid, ctx, st, lang):
    z = lang == "zh"
    evs = ctx["events"]
    acts_idx = _acts(evs)
    act_of = {}
    acts = []
    for k, (a, b) in enumerate(acts_idx):
        aid = f"a{k + 1}"
        for i in range(a, b + 1):
            act_of[i] = aid
        span = (f"{md_zh(evs[a]['ts'])} 至 {md_zh(evs[b]['ts'])}" if z else f"{md_en(evs[a]['ts'])} – {md_en(evs[b]['ts'])}")
        acts.append(dict(id=aid, name=(f"第 {k + 1} 段" if z else f"Phase {k + 1}"), span=span))
    flat = [_flat(e, lang, i, act_of[i]) for i, e in enumerate(evs)]
    camp = ctx["campaign"]["campaign"]
    campaign_roles = ctx["roster"].campaign_roles(camp["id"])
    for pid in campaign_roles:
        ctx["roster"].touch(pid)
    people = _people(ctx, evs, lang, campaign_roles)
    cname = camp["name"]
    client = cname.split("_")[0]
    rest = cname[len(client):].strip("_").replace("_", " ")
    docs = ctx["links"]
    partners = sorted({d["click_url"]["partner"]["name"] for d in docs.values() if d["click_url"].get("partner")})
    parties = sorted({(d["click_url"].get("track_party") or {}).get("name") for d in docs.values()} - {None})
    H = len([p for p in people if p["kind"] == "human"])
    B = len(people) - H
    src_cnt = Counter(e["src"] for e in evs)
    first, last = evs[0]["ts"], evs[-1]["ts"]
    g, s, h, f, mt, em = (src_cnt.get(k, 0) for k in ("github", "slack", "hubspot", "femini", "meeting", "email"))
    summary = (f"从 {md_zh(first)} 到 {md_zh(last)}，共 {len(evs)} 个动作，涉及 {H} 位同事和 {B} 个机器人；来自 GitHub {g} 条、Slack {s} 条、Fireflies / Teams 会议 {mt} 条、Gmail 邮件 {em} 封、HubSpot 工单 {h} 条、FeedMob Admin 后台 {f} 条。"
               if z else f"From {md_en(first)} to {md_en(last)}: {len(evs)} actions involving {H} people and {B} bots; GitHub {g}, Slack {s}, Fireflies / Teams meetings {mt}, Gmail {em}, HubSpot tickets {h}, FeedMob Admin {f}.")
    rows = [[f"FeedMob Admin {cid}",
             (f"活动 {cname}；{len(docs)} 条链接，渠道 {'、'.join(partners) or '—'}；已批准的调整 {f} 次" if z else
              f"Campaign {cname}; {len(docs)} links, partners {', '.join(partners) or '—'}; {f} approved changes")]]
    for k, t in sorted(ctx["tickets"].items(), key=lambda kt: kt[1].get("updated") or "", reverse=True)[:4]:
        if t.get("standalone"):
            continue
        L = f"#{t['num']}" if t["repo"] == "feed-mob/feedmob" else f"{t['repo'].split('/')[1]}#{t['num']}"
        end = t.get("closed") or t.get("updated")
        state = ("已关闭" if t["state"] == "closed" else "还开着") if z else ("closed" if t["state"] == "closed" else "open")
        rows.append([f"GitHub {L}", (f"{state}；建单到{'关单' if t['state'] == 'closed' else '最近更新'} {dur(t['created'], end)}；评论 {t['comments']} 条"
                                      if z else f"{state}; {dur_en(t['created'], end)} from filing to {'closure' if t['state'] == 'closed' else 'latest update'}; {t['comments']} comments")])
    for hs in ctx["hubspot"][:2]:
        rows.append([f"HubSpot #{hs['id']}", (f"负责人 {hs['owner']}；{hs['priority']}；{hs['status']}" if z else f"Owner {hs['owner']}; {hs['priority']}; {hs['status']}")])
    n_sl, named, picked, chans = _slack_mix(evs, lang)
    if n_sl:
        parts = ([f"写到该编号或相关单号的 {named} 条" if z else f"{named} name the number or its issues"] if named else []) + \
                ([f"AI 从对话里挑出的关键发言 {picked} 条" if z else f"{picked} are key remarks the AI picked from conversations"] if picked else [])
        rows.append(["Slack", (f"{n_sl} 条消息，来自 {chans}（{'，'.join(parts)}）" if z else f"{n_sl} messages from {chans} ({'; '.join(parts)})")])
    ok_gh = bool(ctx["gh_man"])
    charter = [dict(t="GitHub 事件已读" if z else "GitHub events read", ok=ok_gh),
               dict(t=("Slack 已读" if z else "Slack read") if ctx["brain"].get("source") == "direct" else ("Slack 副本已读" if z else "Slack snapshot read"), ok=bool(ctx["brain"].get("channels")) or n_sl > 0),
               dict(t="FeedMob Admin 后台已读" if z else "FeedMob Admin read", ok=True),
               dict(t="HubSpot 工单已读" if z else "HubSpot tickets read", ok=bool(ctx["brain"])),
               dict(t="Gmail 邮件已读" if z else "Gmail read", ok=any(e["src"] == "email" for e in evs)),
               dict(t="没有写入任何系统" if z else "No changes made to source systems", ok=True)]
    case = dict(
        charter=charter, sources=_sources(ctx, st, lang), evidence_rows=rows, summary=summary,
        watch=_watch(ctx, evs, lang),
        meta=dict(no=cid, title=f"{client} {rest}".strip(), campaign=cname, campaign_id=camp["id"], client=client,
                  click_urls=[str(x) for x in ctx["campaign"].get("links") or []],
                  partner=", ".join(partners) or "—", mmp=", ".join(parties) or "—",
                  country=camp.get("country_code") or "—", os=camp.get("os") or "—", built_at=now_z(),
                  slack_until=ctx["brain"].get("slack_synced_until"), edition="自动生成" if z else "Auto-generated"),
        people=people, acts=acts,
        stages=[dict(id=i, name=zn if z else en) for i, zn, en in E.STAGES],
        tickets=_tickets(ctx, lang), events=flat, checks=_checks(ctx, evs, st, lang))
    from .attribution_context import attach
    return attach(case, cid, lang, ctx.get('attribution_terms') or {})


@functools.lru_cache(maxsize=1)
def font_css():
    """页面字体和原版 FeedOS 一样（Lato、JetBrains Mono），直接内嵌进页面：不去外网取，打不开 Google 的网络也不受影响。
    中文没有这两种字体，自动退回系统字体。"""
    return "".join(f'@font-face{{font-family:"{fam}";font-style:normal;font-weight:{w};font-display:swap;'
                   f'src:url(data:font/woff2;base64,{base64.b64encode((FONTS / f).read_bytes()).decode()}) format("woff2")}}'
                   for fam, w, f in FONT_FACES)


def render(case, lang):
    src = TEMPLATE.read_text(encoding="utf-8")
    tpl = src if lang == "zh" else english_ui(src)
    blob = json.dumps(case, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    scene = json.dumps(scene_document(case, lang), ensure_ascii=False, separators=(",", ":"))
    scene = scene.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return tpl.replace("/*__FONTS__*/", font_css()).replace("__CASE_NO__", str(case["meta"]["no"])).replace("__HTML_LANG__", "zh-CN" if lang == "zh" else "en").replace("__CASE_DATA__", blob).replace("__SCENE_DATA__", scene)


def build(cid, status=None):
    """生成两版页面。没有任何记录时报错，且不覆盖已有页面。"""
    ctx = E.build_events(cid)
    if not ctx["events"]:
        raise RuntimeError(f"活动 {cid} 没有找到任何可回放的记录（后台、GitHub、HubSpot、Slack 里都没有）")
    st = status or {}
    od = out_dir(cid)
    stats = Counter()
    for lang in ("zh", "en"):
        case = _deep(assemble(cid, ctx, st, lang), lang, ctx["roster"], stats)
        if lang == "en":
            assert not has_han(json.dumps(case, ensure_ascii=False)), "英文数据里还有汉字"
        write_json(od / f"case.{lang}.json", case)
        html = render(case, lang)
        tmp = od / f"replay.{lang}.html.tmp"
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(od / f"replay.{lang}.html")
    info = dict(number=cid, built_at=now_z(), events=len(ctx["events"]), people=len(ctx["roster"].used),
                tickets=len(ctx["tickets"]), campaign=ctx["campaign"]["campaign"]["name"],
                sanitized=stats["sanitized"])
    write_json(od / "build.json", info)
    return info
