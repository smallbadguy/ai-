"""后台参数改动 ↔ 当时在哪讨论的（业务版、详细版都用）。

FeedMob Admin 里批准过的改动（利润率、单价、上限、开关）是结构化记录，但审批日志只有「提交 / 批准」，不写原因。
Ken 10-24 会上要的是：参数改了，能查到是在 Slack、邮件还是会议里谈的。做法：
- 候选：改动前 4 天到后 1 天、原话里提到这个设置（如 margin）的消息（Slack、邮件、HubSpot 备注、会议发言），
  按「提到改后的数值、是改动人自己说的、离改动近、出自本活动专用频道」打分，取前 MAX_CANDIDATES 条；机器人和系统提醒不要。
- AI 只负责挑出真在谈这次改动的消息并抄原话；程序逐字定位（说话人、时间取原始记录），再按规则把关：原话要点到本活动的
  平台/链接；同时点了别的平台的，只认改动人自己在改动前后 2 小时内说的；写了具体利润率数值的要和改后的值一致；
  「请求 / 商定」不能晚于改动。宁可「没找到」，也不挂错。
- 大多数改动没有书面讨论：没有就是没有，页面照实写。结果按改动缓存在 raw/campaigns/<id>/changes.json，
  候选或规则（LINK_VERSION）变了才重找；AI 不可用时这次先不找，页面也不显示「没找到」。
"""
import bisect
import concurrent.futures
import datetime as dt
import hashlib
import json
import re

from . import business as B
from . import corpus as corpus_mod
from . import progress as P
from . import scope as run_scope, attribution_context
from .people import name_key
from .util import camp_dir, has_han, iso, list_file, now_z, raw_dir, read_json, write_json

LINK_VERSION = 1           # 找讨论的提示词或把关规则变了就加一：已存的结果作废、重找
BEFORE_HOURS = 96          # 改动前多久的消息算候选（讨论通常在改之前）
AFTER_HOURS = 24           # 改动后多久（改完说一声、事后问起）
NEIGHBOR_DAYS = 7          # 同一链接前后这么多天内的其他改动一并告诉 AI，好区分
MAX_CANDIDATES = 40
MAX_PICKS = 3
MAX_QUOTE = 400
EXCERPT = 1500             # 长邮件、长发言只截关键词附近这么多字给 AI 看
WORKERS = 8
AMBIGUOUS_MINUTES = 120    # 原话同时点了别的平台：只认改动人自己在改动前后这么久内说的
REQUEST_LATE_MINUTES = 60  # 「请求 / 商定」最晚只能比改动晚这么久
ROLES = ("request", "decision", "reason", "done", "followup")

FIELDS = (("margin", "new_margin"), ("gross_cpi", "new_gross_cpi"), ("net_cpi", "new_net_cpi"), ("hard_cap", "new_hard_cap"),
          ("advertiser_cap", "new_advertiser_cap"), ("state", "new_state"), ("max_switch", "new_max_switch"))
TIMING = ("effective_at", "scheduled_time", "state_effective_at")
LABELS = {"zh": dict(margin="利润率", gross_cpi="Gross CPI", net_cpi="Net CPI", hard_cap="硬上限", advertiser_cap="广告主上限",
                     state="投放状态", max_switch="Max Switch"),
          "en": dict(margin="Margin", gross_cpi="Gross CPI", net_cpi="Net CPI", hard_cap="Hard cap", advertiser_cap="Advertiser cap",
                     state="Delivery state", max_switch="Max switch")}
_A, _Z = r"(?<![A-Za-z])", r"(?![A-Za-z])"  # 英文词边界：中文紧挨着英文（「vn的margin」）也算边界
TOPIC = dict(
    margin=_A + r"(?:margins?|mark-?ups?)" + _Z + r"|利润|毛利",
    gross_cpi=_A + r"(?:cpi|payouts?)" + _Z + r"|单价", net_cpi=_A + r"(?:cpi|payouts?)" + _Z + r"|单价",
    hard_cap=_A + r"caps?" + _Z + r"|上限", advertiser_cap=_A + r"caps?" + _Z + r"|上限",
    state=_A + r"(?:paus(?:e|ed|ing)|resum(?:e|ed|ing)|turn(?:ed)? (?:on|off))" + _Z + r"|暂停|恢复|上线|下线",
    max_switch=r"max switch")
PACE = re.compile(_A + r"(?:pac(?:e|ed|ing)|budgets?|catch(?:ing)? up|pull(?:ed|ing)? back|ramp(?:ed|ing)? up|throttl\w*|CPE)" + _Z
                  + r"|under-?(?:pac|deliver|spend)|over-?(?:pac|deliver|spend)|预算|放量|控量", re.I)
# 原话里写明的利润率数值：「45%」「改为30」「margin back to 30」
_MARGIN_VALUE = re.compile(r"(?<![\d.$#/])(\d{1,3}(?:\.\d+)?)\s?%|(?:改为|改成|调到|调至|设为|设成|降到|提到|升到|弄成)\s?(\d{1,3}(?:\.\d+)?)"
                           r"|margins?(?:\s+\w+){0,3}?\s+(?:to|at|of)\s+(\d{1,3}(?:\.\d+)?)(?![\d.])", re.I)
BOT_MORE = re.compile(r"feedai|assistant|feedmobadmin|delivery manager", re.I)
# 活动名里的地区缩写 → 口头说的国家名（中英文）：会上不说 VN，说 Vietnam / 越南
COUNTRY_NAMES = {
    "VN": ("Vietnam", "Vietnamese", "越南"), "BR": ("Brazil", "Brazilian", "巴西"), "TH": ("Thailand", "Thai", "泰国"),
    "BD": ("Bangladesh", "孟加拉"), "MX": ("Mexico", "Mexican", "墨西哥"), "KR": ("Korea", "Korean", "韩国"),
    "JP": ("Japan", "Japanese", "日本"), "SG": ("Singapore", "新加坡"), "PH": ("Philippines", "Filipino", "菲律宾"),
    "VE": ("Venezuela", "委内瑞拉"), "CR": ("Costa Rica", "哥斯达黎加"), "PK": ("Pakistan", "巴基斯坦"), "NG": ("Nigeria", "尼日利亚"),
    "KE": ("Kenya", "肯尼亚"), "EG": ("Egypt", "埃及"), "TW": ("Taiwan", "台湾"), "HK": ("Hong Kong", "香港"),
    "UK": ("United Kingdom", "英国"), "AU": ("Australia", "澳大利亚", "澳洲"), "PE": ("Peru", "秘鲁"), "CL": ("Chile", "智利"),
    "MY": ("Malaysia", "马来西亚"), "AR": ("Argentina", "阿根廷"), "KH": ("Cambodia", "柬埔寨"), "KZ": ("Kazakhstan", "哈萨克斯坦"),
    "KW": ("Kuwait", "科威特"), "DZ": ("Algeria", "阿尔及利亚"), "UAE": ("Emirates", "Dubai", "阿联酋", "迪拜"), "CH": ("Switzerland", "瑞士"),
}
# 活动名里常见、但不是平台/合作方的词：不拿来判断「点了别的平台」
GENERIC = {"agency", "introductory", "roadblocks", "targeting", "mweb", "churn", "reactivation", "12m", "12mp", "openmarket"}

LINK_SYSTEM = """You link one change made in FeedMob Admin (the ad-ops back office) to the conversation where it was discussed.
Advertiser: "{advertiser}". Campaign: "{campaign}" (platform / partner keywords: {keywords}). Tracking link #{link} "{link_name}".
The advertiser runs other campaigns at the same time (platforms / partners: {others}). Their margins and settings are changed too, often on the same day, and one message may list several of them.

THE CHANGE: at {ts} UTC, {user} changed this link: {what}.
Other changes on this link around that time (to tell them apart): {neighbors}.

Below are messages from {before} days before to {after} day after the change; each starts with an id like U12. Pick the messages that show where THIS change was discussed:
- before the change: someone asking for it, proposing or agreeing on it, or explaining why it is needed;
- after the change: the person saying it was done (and why), or someone asking about this exact change (naming this link or campaign, or its new value).
Strict rules:
- The message must talk about the setting itself: for a margin change it says "margin" (or markup), not just a percentage. Percentages of other things (IPM, top scores, frequency, budget share, CPE, spend) are not the margin.
- It must be clearly about this campaign or link: it names the link number, the campaign, or its platform / partner, or it answers a message that does. A list of several platforms counts only if it includes this one AND is about this change.
- Updating margins in reports, spreadsheets or spend tracking after the fact (backfilling numbers) is data entry, not this change; a request posted after the change that asks someone to change something is about something else.
- Do not pick general remarks about margins, other campaigns' or platforms' settings, other advertisers, reports or alerts.
Most changes have no written discussion. If nothing clearly matches, return an empty list; never guess. At most {max_picks} messages.

For each pick:
- record: the id;
- quote: verbatim contiguous text copied from that one message (or that one speaker's turn), same language, no ellipsis, no paraphrase, at most {max_quote} characters: the sentence(s) that carry the point, including the words about the setting;
- role: "request", "decision", "reason", "done" or "followup";
- translation: a faithful translation of the quote into the other language (Chinese if the quote is English, English if it is Chinese).
reason_zh / reason_en: one short sentence on why the setting was changed, only if a picked quote itself says why; otherwise "". Do not add anything the quotes do not say. reason_en must be English only.

Output one JSON object and nothing else:
{{"picks": [{{"record": "U12", "quote": str, "role": str, "translation": str}}], "reason_zh": str, "reason_en": str}}"""


# ───────────────────────── 后台改动 ─────────────────────────
def _val(k, v):
    if k == "margin":
        return f"{float(v):g}%"
    if k in ("gross_cpi", "net_cpi"):
        return f"${float(v):g}"
    return f"{v:g}" if isinstance(v, float) else str(v)


def param_changes(cid):
    """后台里批准过的改动（结构化记录，不经 AI）。→ [{id, ts, user, link, link_name, new{设置: 值}, prev{设置: 同一链接上次的值}, when{生效时间}}]，按时间排。"""
    rows = []
    info = read_json(camp_dir(cid) / "campaign.json") or {}
    for n in info.get("links") or []:
        cu = (read_json(raw_dir(n) / "femini.json") or {}).get("click_url") or {}
        for c in cu.get("proposed_changes") or []:
            new = {k: c[f] for k, f in FIELDS if c.get(f) is not None}
            if c.get("approve_status") != "approved" or not new or not iso(c.get("updated_at")):
                continue
            rows.append(dict(id=str(c.get("id") or f"{n}@{iso(c['updated_at'])}"), ts=iso(c["updated_at"]), user=c.get("user_name") or "unknown",
                             link=str(n), link_name=cu.get("name") or "", new=new,
                             when={k: iso(c[f"new_{k}"]) for k in TIMING if iso(c.get(f"new_{k}"))}))
    out, last = [], {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        seen = last.setdefault(r["link"], {})
        r["prev"] = {k: seen.get(k) for k in r["new"]}
        seen.update(r["new"])
        out.append(r)
    return out


def describe(ch, lang):
    """「利润率从 30% 调整至 50%」/「Margin adjusted from 30% to 50%」；带生效时间的写上。"""
    z, parts = lang == "zh", []
    for k, v in ch["new"].items():
        p, name = ch["prev"].get(k), LABELS[lang][k] + (" " if z and LABELS[lang][k][-1].isascii() else "")
        if p is not None and p != v:
            parts.append(f"{name}从 {_val(k, p)} 调整至 {_val(k, v)}" if z else f"{name} adjusted from {_val(k, p)} to {_val(k, v)}")
        else:
            parts.append(f"{name}调整至 {_val(k, v)}" if z else f"{name} set to {_val(k, v)}")
    s = ("，" if z else "; ").join(parts)
    eff = ch["when"].get("effective_at")
    if eff:
        s += f"（{eff[:10]} {eff[11:16]} UTC 起生效）" if z else f" (effective {eff[:10]} {eff[11:16]} UTC)"
    return s


def gap(minutes, lang):
    """讨论离改动多久：「改动前 22 小时」/「22 h before the change」。"""
    m = abs(minutes)
    z = lang == "zh"
    if m < 1:
        return "和改动同时" if z else "at the time of the change"
    n, zu, eu = (round(m), "分钟", "min") if m < 60 else ((round(m / 60), "小时", "h") if m < 48 * 60 else (round(m / 1440), "天", "days"))
    if z:
        return f"改动{'前' if minutes < 0 else '后'} {n} {zu}"
    return f"{n} {'day' if eu == 'days' and n == 1 else eu} {'before' if minutes < 0 else 'after'} the change"


# ───────────────────────── 候选 ─────────────────────────
def _minutes(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() / 60


def _bot(who):
    return not who or bool(corpus_mod.BOT_RE.search(who) or BOT_MORE.search(who) or B._SPEAKER_LABEL.match(who))


def units_of(records):
    """原料 → 一条条能被引用的发言：Slack 消息、邮件、HubSpot 备注各一条，会议按发言人一段段拆开（时间取那段的时钟）。"""
    out = []
    for r in records:
        if r["kind"] == "meeting":
            out += [dict(rec=r, who=who, ts=B._stamp(r["ts"], clock), text=text) for who, clock, text in B.meeting_turns(r)]
        elif r["kind"] in ("slack", "email", "hubspot") and r.get("who"):
            out.append(dict(rec=r, who=r["who"], ts=r["ts"], text=r["text"]))
    out = [u for u in out if not _bot(u["who"])]
    for u in out:
        u["min"] = _minutes(u["ts"])
    out.sort(key=lambda u: u["min"])
    return out


def _value_rx(k, v):
    if k == "margin":
        return re.compile(r"(?<![\d.])" + re.escape(f"{float(v):g}") + r"\s?%")
    if k in ("gross_cpi", "net_cpi"):
        return re.compile(r"\$\s?" + re.escape(f"{float(v):g}") + r"(?!\d)")
    return None


def _topic(ch):
    return re.compile("|".join(TOPIC[k] for k in ch["new"]), re.I)


def candidates(ch, units, matcher):
    """改动前后、提到这个设置的发言，按相关程度取前 MAX_CANDIDATES 条（按时间排好）。"""
    t, topical, maker = _minutes(ch["ts"]), _topic(ch), name_key(ch["user"])
    vals = [rx for rx in (_value_rx(k, v) for k, v in ch["new"].items()) if rx]
    prevs = [rx for rx in (_value_rx(k, v) for k, v in ch["prev"].items() if v is not None) if rx]
    mins = [u["min"] for u in units]
    lo, hi = bisect.bisect_left(mins, t - BEFORE_HOURS * 60), bisect.bisect_right(mins, t + AFTER_HOURS * 60)
    scored = []
    for u in units[lo:hi]:
        text, d = u["text"], u["min"] - t
        if not topical.search(text):  # 原话里必须提到这个设置，没提到的反正也挂不上
            continue
        s = 4 + (3 if any(rx.search(text) for rx in vals) else 0) + (1 if PACE.search(text) else 0)
        s += (1 if any(rx.search(text) for rx in prevs) else 0) + (2 if name_key(u["who"]) == maker else 0)
        s += (1 if B.dedicated(u["rec"]) else 0) + (1 if (matcher.context(text) or matcher.strong(text)) else 0)
        s += 2 if abs(d) <= 360 else (1 if abs(d) <= 1440 else 0)
        scored.append((s, -abs(d), u))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return sorted((u for _, _, u in scored[:MAX_CANDIDATES]), key=lambda u: u["min"])


def _excerpt(text, rx):
    if len(text) <= EXCERPT:
        return text
    m = rx.search(text)
    start = max(0, (m.start() if m else 0) - EXCERPT // 3)
    return text[start:start + EXCERPT]


def _render(u, uid, rx):
    r = u["rec"]
    head = {"slack": lambda: f"Slack {r['channel']}", "email": lambda: f"EMAIL «{r['subject']}» to {', '.join(r.get('to') or []) or '-'}",
            "hubspot": lambda: f"HUBSPOT ticket #{r['ticket']} «{r['title']}» note"}.get(r["kind"], lambda: f"MEETING «{r['title']}»")()
    return f"[{uid}] {u['ts'].replace('T', ' ')[:16]} {head} | {u['who']}: {_excerpt(u['text'], rx)}"


# ───────────────────────── 归属 ─────────────────────────
def _w(word):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.I)


class Scope:
    """原话归不归本活动：点到本活动的平台/合作方、链接号或活动名（不分大小写，「vn的margin」也算），频道名点到也算。
    同广告主其他活动的平台词、别的广告主名另记，用来判断「一句话说了好几个平台」「说的是别的客户」。"""

    def __init__(self, terms):
        self.m = corpus_mod.Matcher(terms)
        own = {c.lower() for c in terms["context"]}
        others, advs, everyone = set(), set(), set()
        own_geo, other_geo = {t for t in corpus_mod._SPLIT.split(terms["name"]) if t in COUNTRY_NAMES}, set()
        for r in (read_json(list_file()) or {}).get("campaigns") or []:
            name = str(r.get("name") or "")
            if not name or name == terms["name"]:
                continue
            t = corpus_mod.build_terms(dict(name=name), [], [], [])
            if t["advertiser"].lower() == terms["advertiser"].lower():
                everyone |= {c.lower() for c in t["context"]}
                other_geo |= {g for g in corpus_mod._SPLIT.split(name) if g in COUNTRY_NAMES}
                others |= {c for c in t["context"] if c.lower() not in own and c.lower() not in GENERIC}
            elif len(t["advertiser"]) >= 4:
                advs.add(t["advertiser"])
        self.others = sorted(others)
        self.shared = own & everyone  # 本活动的平台词里，同广告主别的活动也用的（如 Chime 几个活动都走 Freewheel）
        self.geo_rx = {g: [_w(n) for n in COUNTRY_NAMES[g]] for g in sorted(own_geo)}
        self.other_geo_rx = [_w(n) for g in sorted(other_geo - own_geo) for n in COUNTRY_NAMES[g]]
        self.own_rx = [_w(c) for c in terms["context"]]
        self.other_rx = [_w(c) for c in self.others]
        self.adv_rx = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])") for a in sorted(advs)]  # 区分大小写：广告主名可能是普通单词
        # 别的客户的名字，加上口头的分开写法：BinanceUS 会说成「Binance US」（后半截只认大写，免得和英文的 us 混）
        spaced = [re.match(r"^([A-Z][a-z]+)([A-Z]{2,})$", a) for a in sorted(advs)]
        self.client_rx = self.adv_rx + [re.compile(r"(?<![A-Za-z0-9])(?i:" + re.escape(x.group(1)) + r")\s+" + x.group(2) + r"(?![A-Za-z0-9])")
                                        for x in spaced if x]

    def own(self, text, rec):
        chan = (rec.get("channel") or "") if rec["kind"] == "slack" else ""
        return bool(self.m.strong(text)) or any(rx.search(text) or rx.search(chan) for rx in self.own_rx)

    def other(self, text):
        return any(rx.search(text) for rx in self.other_rx)

    def countries(self, text):
        """本活动的地区被说成了国家名（Vietnam / 越南 ← VN）。→ 地区缩写"""
        return [g for g, rxs in self.geo_rx.items() if any(rx.search(text) for rx in rxs)]

    def other_country(self, text):
        """说到了同广告主别的活动的国家（越南活动页上说 Venezuela）。"""
        return any(rx.search(text) for rx in self.other_geo_rx)

    def other_client(self, text):
        """点到了别的客户（含「Binance US」这种分开写法）。"""
        return any(rx.search(text) for rx in self.client_rx)

    def foreign(self, text):
        return not self.m.advertiser(text) and any(rx.search(text) for rx in self.adv_rx)


def _margin_values(text):
    return {float(next(g for g in m if g)) for m in _MARGIN_VALUE.findall(text)}


# ───────────────────────── 找讨论 ─────────────────────────
def ask_one(ch, changes, cands, terms, scope, ask):
    """一次改动问一次 AI。→ (AI 的回答, 候选编号 → 发言, 用量)"""
    rx = re.compile("|".join([TOPIC[k] for k in ch["new"]] + [PACE.pattern]), re.I)
    ids = {f"U{i}": u for i, u in enumerate(cands, 1)}
    t = _minutes(ch["ts"])
    near = [c for c in changes if c["link"] == ch["link"] and c["id"] != ch["id"] and abs(_minutes(c["ts"]) - t) <= NEIGHBOR_DAYS * 1440]
    system = LINK_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"], keywords=", ".join(terms["context"] + terms["links"]) or "-",
                                link=ch["link"], link_name=ch["link_name"] or "-", others=", ".join(scope.others) or "-",
                                ts=ch["ts"].replace("T", " ")[:16], user=ch["user"], what=describe(ch, "en"),
                                neighbors="; ".join(f"{c['ts'].replace('T', ' ')[:16]} {describe(c, 'en')}" for c in near) or "none",
                                before=BEFORE_HOURS // 24, after=AFTER_HOURS // 24, max_picks=MAX_PICKS, max_quote=MAX_QUOTE)
    system += attribution_context.prompt_context(terms)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Messages (chronological):\n\n" + "\n".join(_render(u, k, rx) for k, u in ids.items())}]
    doc, usage = P.ask_json(messages, ask)
    return doc, ids, usage


def verify(ans, ids, scope, ch):
    """AI 挑的原话 → 逐字定位 + 把关后的讨论（说话人、时间取原始记录）。→ (留下的, [(丢掉的原因, AI 的那条)])"""
    topical, t, maker = _topic(ch), _minutes(ch["ts"]), name_key(ch["user"])
    out, dropped, seen = [], [], set()
    for pk in (ans.get("picks") or [])[:MAX_PICKS]:
        u = ids.get(str(pk.get("record")))
        hit = B.locate(u["rec"], pk.get("quote")) if u else None
        if not hit or len(hit["text"]) > MAX_QUOTE * 1.5:
            dropped.append(("原话查无", pk))
            continue
        q, d = hit["text"], _minutes(hit["ts"]) - t
        role = pk.get("role") if pk.get("role") in ROLES else ""
        vals = _margin_values(q) if "margin" in ch["new"] else set()
        why = None
        if B._SPEAKER_LABEL.match(hit["speaker"] or "") or not B.meaningful(q):
            why = "说话人不明或没有内容"
        elif scope.m.t.get('attribution_version') and not scope.m.quote_allowed(u['rec'], q):
            why = '归属证据不足'
        elif not topical.search(q):
            why = "没提到这个设置"
        elif scope.foreign(q) or not scope.own(q, u["rec"]):
            why = "不是本活动的"
        elif scope.other(q) and not scope.m.strong(q) and not (name_key(hit["speaker"]) == maker and abs(d) <= AMBIGUOUS_MINUTES):
            why = "同时说了好几个平台"
        elif vals and float(ch["new"]["margin"]) not in vals:
            why = "数值对不上"
        elif role in ("request", "decision") and d > REQUEST_LATE_MINUTES:
            why = "请求晚于改动"
        if why or (P.fingerprint(u["rec"]), q) in seen:
            dropped.append((why or "重复", pk))
            continue
        seen.add((P.fingerprint(u["rec"]), q))
        tr = (pk.get("translation") or "").strip()
        if has_han(q) == has_han(tr):  # 译文要是另一种语言：中文原话配英文译文，英文原话配中文译文
            tr = ""
        out.append(dict(fp=P.fingerprint(u["rec"]), kind=u["rec"]["kind"], source=B.source_of(u["rec"]), ts=hit["ts"],
                        speaker=hit["speaker"], quote=q, translation=tr, role=role, minutes=round(d)))
    return out, dropped


def link(cid, changes, records, terms, ask, log=print):
    """每次改动找讨论（缓存过、候选没变的不再问）。→ ({改动编号: 结果}, 用量列表)。
    结果：picks（核实过的讨论原话）、reason_zh / reason_en（只在原话说了原因时有）。AI 出错的这次不记，下次再找。"""
    path = camp_dir(cid) / "changes.json"
    prev = read_json(path) or {}
    attribution_sig = json.dumps([terms.get('attribution_version'), terms], sort_keys=True) if terms.get('attribution_version') else None
    cache = (prev.get("results") or {}) if (prev.get("version") == LINK_VERSION
        and prev.get('attribution_version') == terms.get('attribution_version')
        and prev.get('attribution_sig') == attribution_sig) else {}
    if not changes:
        return {}, []
    units, matcher, scope = units_of(records), corpus_mod.Matcher(terms), Scope(terms)
    results, todo = {}, []
    for ch in changes:
        cands = candidates(ch, units, matcher)
        sig = hashlib.sha1(json.dumps([ch["ts"], ch["new"], ch["prev"], [[P.fingerprint(u["rec"]), u["ts"], u["who"]] for u in cands]],
                                      sort_keys=True).encode()).hexdigest()[:16]
        old = cache.get(ch["id"])
        if old and old.get("sig") == sig:
            results[ch["id"]] = old
        elif not cands:
            results[ch["id"]] = dict(sig=sig, at=now_z(), candidates=0, picks=[], reason_zh="", reason_en="")
        else:
            todo.append((ch, cands, sig))

    def run(item):
        ch, cands, sig = item
        try:
            ans, ids, usage = ask_one(ch, changes, cands, terms, scope, ask)
        except Exception as e:  # noqa: BLE001  AI 不可用或出错：这次先不找
            return ch, None, None, str(e)[:160]
        picks, _ = verify(ans, ids, scope, ch)
        rz, re_ = B._text(ans.get("reason_zh")), B._text(ans.get("reason_en"))
        return ch, dict(sig=sig, at=now_z(), candidates=len(cands), picks=picks, reason_zh=rz if picks and has_han(rz) else "",
                        reason_en=re_ if picks and re_ and not has_han(re_) else ""), usage, None
    usages, errors = [], []
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for ch, res, usage, err in run_scope.thread_map(pool, run, todo):
                if err:
                    errors.append(err)
                    if cache.get(ch["id"]):  # 先沿用上次的结果，下次再重找
                        results[ch["id"]] = cache[ch["id"]]
                    continue
                results[ch["id"]] = res
                usages.append(usage)
        found = sum(1 for r in results.values() if r["picks"])
        log(f"  后台改动 {len(changes)} 次：新找 {len(todo) - len(errors)} 次，累计 {found} 次找到讨论")
        if errors:
            log(f"  有 {len(errors)} 次改动这次没能找讨论（{errors[0]}），下次再找")
    write_json(path, dict(version=LINK_VERSION, built_at=now_z(), results=results,
                          attribution_version=terms.get('attribution_version'), attribution_sig=attribution_sig))
    return results, usages


def where(p, lang):
    """讨论出自哪里（详细版的文字说明用）：Slack #频道、Gmail 邮件「主题」、会议「名称」、HubSpot 工单 #编号。"""
    z, name = lang == "zh", p["source"] or ""
    if p["kind"] == "slack":
        return f"Slack {name}"
    if p["kind"] == "hubspot":
        return re.sub(r"^HubSpot #(\d+).*", r"HubSpot 工单 #\1" if z else r"HubSpot ticket #\1", name)
    label = {"email": ("Gmail 邮件", "Gmail"), "meeting": ("会议", "Meeting")}[p["kind"]][0 if z else 1]
    if not name or (not z and has_han(name)):
        return label
    return f"{label}「{name}」" if z else f"{label} “{name}”"


def notes(cid, lang, terms=None):
    """详细版用：{改动编号: 文字}。只读取与当前身份和清单一致的讨论。"""
    doc = read_json(camp_dir(cid) / "changes.json") or {}
    material = read_json(camp_dir(cid) / 'corpus.json', {}) or {}
    terms = terms if terms is not None else material.get('terms') or {}
    version = terms.get('attribution_version') or material.get('attribution_version')
    signature = json.dumps([version, terms], sort_keys=True) if version else None
    if (doc.get('version') != LINK_VERSION or doc.get('attribution_version') != version
            or (version and doc.get('attribution_sig') != signature)):
        return {}
    z, out = lang == "zh", {}
    for k, r in (doc.get("results") or {}).items():
        if not r["picks"]:
            out[k] = "前后几天的 Slack、邮件、会议里没有找到谈这次改动的消息。" if z else \
                "No message about this change was found in Slack, email or meetings in the days around it."
            continue
        lines = []
        for p in r["picks"]:
            when = p["ts"][5:16].replace("T", " ") + " UTC"
            head = f"{where(p, lang)} · {p['speaker']} · {when}（{gap(p['minutes'], 'zh')}）" if z \
                else f"{where(p, lang)} · {p['speaker']} · {when} ({gap(p['minutes'], 'en')})"
            quote = p["quote"]
            if not z and has_han(quote):
                quote = p["translation"] and f"(translated) {p['translation']}"
            if z and not has_han(quote) and p["translation"]:
                quote += f"\n译文：{p['translation']}"
            if quote:
                lines.append(f"{head}\n「{quote}」" if z else f"{head}\n“{quote}”")
        reason = r.get("reason_zh" if z else "reason_en")
        if reason:
            lines.append(("原因（据以上原话整理）：" if z else "Reason (summarized from the quotes above): ") + reason)
        out[k] = "\n\n".join(lines)
    return out
