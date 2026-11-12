"""业务版（AI 产出）：读 AI 原料，生成和 V11 同样界面的「谁在什么时候说了什么」业务回放，写到 out/<活动>/business.{zh,en}.html。

核心约束（用户明确要求）：回放里的「原话」必须是真人在原始记录里说的话，不能是我们或 AI 写的旁白/摘要/翻译冒充的。所以：
- 第一步（逐块）：AI 只负责在原料里「挑」出承载业务故事的消息，并抄下原话；程序逐字去原料里定位——找不到就丢，
  说话人和时间一律取自原始记录（Slack 取消息时间；会议取该发言所在行的时钟时间），绝不用 AI 填的。
- 相关性闸门：一条原话要么本身点到广告主/活动名/链接号，要么出自专用频道或标题就带广告主的会议；只和合作方有关的丢弃。
- 第二步（汇总）：AI 在已核实的候选里选出最终的几十条、排好阶段，写双语的「动作」「当时的情况」、原话的另一语言译文、
  概要和业务推进；这些整理文字和原话分开展示（译文单列、整理说明不是原话）。
- 负责人岗位只取自人物对照表（config/people.json），表里没有的写「团队成员」，不让 AI 猜岗位。
- 另外加入后台里批准过的参数改动（结构化记录），每次改动挂上当时在哪讨论的（AI 挑、程序逐字核对，见 replay/changes.py）。
增量：已处理过的原料记录不再重复给 AI；汇总每次按全部已核实候选重做（几十条，很便宜）。固定地址，内容原地更新。
密钥与网关沿用 replay/progress.py 的 secrets/ai.env。
"""
import datetime as dt
import json
import pathlib
import re

from . import build as build_mod
from . import corpus as corpus_mod
from . import progress as P
from . import progress_labels, scope, attribution_context
from .curated import display_original
from .people import Roster, name_key
from .util import ROOT, camp_dir, has_han, now_z, out_dir, read_json, valid_number, write_json

UI_FILE = ROOT / "config" / "business_ui.json"
MAX_QUOTE = 700
WINDOW_VERSION = 3  # 各时间窗的提示词/结构变了就加一：窗缓存作废（要重新调模型）
PLAN_VERSION = 6  # 汇总的提示词/结构变了就加一，已有活动下次会重新汇总一次（不重挑候选）
WINDOW = 80              # 汇总时每个时间窗放多少条候选
WINDOW_WORKERS = 8       # 各窗并行调用（6 个活动同时重建、每个 4 路并行时 AI 服务没出过错，提到 8 路）
REPLY_MINUTES = 30       # 没点到活动特征词的原话，只有紧接在同一场对话的特征词消息之后（这么多分钟内）才算本活动的
MEETING_CONTEXT_LINES = 8  # 会上的一句话归不归本活动：看它前后这么多行（前面说了「TCL 那边」，接着谈的几句不会再提平台名）
MIN_ATTRIBUTABLE = 15    # 过滤后少于这个数就不启用过滤（特征词太少的活动不能被误删光）
PICK_CHUNK_CHARS = 150_000  # 挑选时每块的大小：比 AI 进行中事项的块小，让模型看得更仔细
PICK_VERSION = 2            # 挑选提示词/块大小变了就加一：已有候选保留，所有原料重新挑一遍（只新增不重复的）
PICK_WORKERS = 8            # 挑选的各块互不相关：同时问 AI，按顺序收（进行中事项要接着上一块的清单往下改，不能这样并行）
SCOPES = ("case", "related", "client")
_SPEAKER_LABEL = re.compile(r"^Speaker \d+$", re.I)
_TURN = re.compile(r"^\*\*(.+?)\*\* \((\d{1,2}:\d{2}:\d{2})\): ?(.*)$")

PICK_SYSTEM = """You help build a business replay of ONE ad campaign: a sequence of real messages that show who said what, when, and what happened.
Advertiser: "{advertiser}". Campaign: "{campaign}" (keywords: {keywords}).

From the records below (each starts with an id like R123) pick the messages that carry the BUSINESS story of this campaign: asking for or promising work; decisions; launch, pause, resume, budget or setup changes; test or performance results and what was concluded; client requests, approvals or feedback; hand-offs between people.

Do NOT pick:
- greetings, chatter, scheduling noise;
- developer or QA details, manual or automatic data entry, technical test chatter (the client does not want development details);
- bot alerts and automatic reports;
- messages about other advertisers or other campaigns, or that only concern an ad partner or inventory source (Samsung, Vizio, TCL, Roku, LG, Magnite...) without being about {advertiser}'s campaign.

Rules:
1. quote: verbatim, contiguous text copied from ONE Slack message, ONE email, ONE HubSpot note or ONE speaker's turn in a meeting. Emails with partners and the client often carry the external side of the story (launch notices, setup questions, performance feedback): pick them like chat messages. A HubSpot "ticket created" record has no known author, so never quote it; HubSpot notes can be quoted. Same language as the record, no ellipsis, no paraphrase, at most {max_quote} characters. Prefer the sentence(s) that carry the point.
2. speaker: exactly as written in the record (for an email, the sender; for a HubSpot note, its author).
3. scope: "case" (about this campaign), "related" (related campaigns of the same advertiser or partner), "client" (client-level matters).
4. Be thorough: a busy stretch can hold dozens of relevant messages, so pick EVERY message that carries a distinct request, answer, decision, launch/pause/change, result or client reaction for this campaign, including the answer to a request you pick and the follow-up that shows what happened. Do not stop at a round number and do not skip a thread because it looks small. Skip only exact repeats and the exclusions above. Return an empty list if nothing in these records is relevant.

Output one JSON object and nothing else:
{{"picks": [{{"record": "R123", "speaker": str, "quote": str, "scope": "case|related|client", "why": str}}]}}"""

WINDOW_SYSTEM = """You compose one time window of the business replay of ONE ad campaign from verified real messages.
Advertiser: "{advertiser}". Campaign: "{campaign}". Each candidate below is a real message or meeting turn: id, UTC time, speaker, where it was said (Slack channel, EMAIL, HubSpot ticket or MEETING), scope and the verbatim quote. They all fall in one time window of a longer campaign; other windows are handled separately, so make sure THIS window is well represented.

Choose the key moments from these candidates, in chronological order. The replay is about how the business work actually moved between people, so keep in particular:
- hand-offs: when someone asks another person or team (AdOps, creative, operations, a partner) to do something, include the request AND the message that confirms it was done or answers it, even when the confirmation is short ("done with the upload", "here's the ticket");
- launches, pauses, resumes, budget, spend and setup changes, and the notices about them;
- problems and their fixes: a report of something wrong, the diagnosis, and what was done about it;
- tests and results with the decision they led to; client requests, approvals and feedback;
- decisions, plans, assignments and results stated in meetings (MEETING lines): much of the real discussion happens there, so keep such a meeting turn just like a message.
Prefer these concrete actions over long summary reports, and keep a report only when it contains a result or decision. Roughly 6 to 12 moments for a window this size (fewer if it is quiet). Drop near-duplicates, small talk, and ones that matter only outside this campaign. Prefer scope "case"; keep "related" or "client" ones only when they explain this campaign. Never invent a moment; use only candidate ids.

For every chosen moment write:
- action_zh / action_en: the business action in a few words (Chinese at most 16 characters, English at most 8 words), e.g. asking for a tracker, pausing delivery.
- context_zh / context_en: ONE neutral sentence about the situation. It must not state anything the quote does not support, and must not quote or paraphrase beyond what is said.
- translation: a faithful translation of the quote into the OTHER language (into Chinese if the quote is English; into English if the quote is Chinese). Translate the whole quote, keep names and numbers.
- to: speaker names (exactly as in candidates) the message is addressed to, only when clear from the quote (an @mention or a direct reply); otherwise [].
All *_en fields must be English only, with no Chinese characters. Plain business language, no jargon.

Output one JSON object and nothing else:
{{"events": [{{"candidate": "C001", "action_zh": str, "action_en": str, "context_zh": str, "context_en": str, "translation": str, "to": [str]}}]}}"""

META_SYSTEM = """You write the overall frame of the business replay of ONE ad campaign. Advertiser: "{advertiser}". Campaign: "{campaign}".
Below are the chosen key moments in chronological order: date | speaker | action | situation. Use only them.

Write:
1. title_zh / title_en: a short title for the whole replay.
2. summary_zh / summary_en: 2 to 3 sentences on what happened.
3. chapters: 3 to 5 stages, chronological, each {{name_zh, name_en, start}} where start is the date (YYYY-MM-DD) of the first moment of that stage; the first stage starts at the first moment's date.
4. workstreams: 4 to 6 items {{title_zh, title_en, text_zh, text_en, people}} describing how the work moved; people are speaker names from the moments.
5. people: one entry per speaker: {{name (exactly as written), role_zh, role_en}}. The role is ONE short phrase on what this person did in these moments (their part in this campaign). Base it only on the moments; do not state a job title unless a moment says it.
All *_en fields must be English only, with no Chinese characters. Plain business language, no jargon.

Output one JSON object and nothing else:
{{"title_zh": str, "title_en": str, "summary_zh": str, "summary_en": str,
  "chapters": [{{"name_zh": str, "name_en": str, "start": "YYYY-MM-DD"}}],
  "workstreams": [{{"title_zh": str, "title_en": str, "text_zh": str, "text_en": str, "people": [str]}}],
  "people": [{{"name": str, "role_zh": str, "role_en": str}}]}}"""


# ───────────────────────── 原话定位 ─────────────────────────
def _flex(quote):
    words = (quote or "").split()
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.I) if words else None


def meeting_turns(record):
    """会议原料 → [(发言人, 当天时钟, 该人连续发言的正文)]；同一人连续的行合并成一次发言。"""
    turns = []
    for line in record["text"].split("\n"):
        m = _TURN.match(line.strip())
        if not m:
            continue
        who, clock, text = m.groups()
        if turns and turns[-1][0] == who:
            turns[-1][2] += " " + text
        else:
            turns.append([who, clock, text])
    return turns


def _stamp(record_ts, clock):
    """会议：用会议当天日期 + 发言所在行的时钟（UTC）；时钟比开会时间早很多就当作跨了零点。"""
    day = dt.date.fromisoformat(record_ts[:10])
    h, m, s = (int(x) for x in clock.split(":"))
    start_min = int(record_ts[11:13]) * 60 + int(record_ts[14:16])
    if h * 60 + m < start_min - 12 * 60:
        day += dt.timedelta(days=1)
    return f"{day.isoformat()}T{h:02d}:{m:02d}:{s:02d}Z"


def locate(record, quote):
    """在一条原料记录里逐字找到 quote。→ dict(speaker, ts, text) 或 None。text 是原文里的真实片段。"""
    rx = _flex(quote)
    if not rx or len(quote) > MAX_QUOTE * 2:
        return None
    if record["kind"] in ("slack", "email", "hubspot"):
        if not record.get("who"):  # HubSpot 建单记录不知道是谁写的，不能当原话
            return None
        m = rx.search(record["text"])
        return dict(speaker=record["who"], ts=record["ts"], text=m.group(0)) if m else None
    for who, clock, text in meeting_turns(record):
        m = rx.search(text)
        if m:
            return dict(speaker=who, ts=_stamp(record["ts"], clock), text=m.group(0))
    return None


# ───────────────────────── 第一步：逐块挑候选 ─────────────────────────
def _render(r):
    stamp = r['ts'].replace('T', ' ').rstrip('Z')
    if r["kind"] == "slack":
        return f"[{r['id']}] {stamp} {r['channel']} | {r['who']}: {r['text']}"
    if r["kind"] == "email":
        return f"[{r['id']}] {stamp} EMAIL «{r['subject']}» | {r['who']} → {', '.join(r.get('to') or []) or '-'}: {r['text']}"
    if r["kind"] == "hubspot":
        if r.get("who"):
            return f"[{r['id']}] {stamp} HUBSPOT ticket #{r['ticket']} «{r['title']}» | note by {r['who']}: {r['text']}"
        return f"[{r['id']}] {stamp} HUBSPOT ticket #{r['ticket']} «{r['title']}» created (no known author, do not quote; owner {r.get('owner') or '-'}): {r['text']}"
    head = f"\n=== MEETING [{r['id']}] | {r['ts'][:10]} | {r['title']}{' | excerpt only' if r.get('partial') else ''} | clock times are the meeting's own ===\n"
    return f"{head}{r['text']}\n=== END MEETING ===\n"


def _chunks(records, limit=PICK_CHUNK_CHARS):
    out, cur, size = [], [], 0
    for r in records:
        n = len(_render(r))
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def pick_once(recs, terms, ask):
    system = PICK_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"],
                                keywords=", ".join(terms["context"] + terms["links"]) or "-", max_quote=MAX_QUOTE)
    system += attribution_context.prompt_context(terms)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Records (chronological):\n\n" + "\n".join(_render(r) for r in recs)}]
    doc, usage = P.ask_json(messages, ask)
    return doc.get("picks") or [], usage


def verify_picks(picks, recs, matcher, dedicated_of, log=print):
    """逐字定位 + 相关性闸门 → 已核实候选（说话人、时间取自原始记录）。"""
    by_id = {r["id"]: r for r in recs}
    out, missing, speakerless, off = [], 0, 0, 0
    for pk in picks:
        rec = by_id.get(pk.get("record"))
        if not rec:
            missing += 1
            continue
        hit = locate(rec, pk.get("quote"))
        if not hit or len(hit["text"]) > MAX_QUOTE:
            missing += 1
            continue
        if _SPEAKER_LABEL.match(hit["speaker"] or ""):  # 「Speaker 2」看不出是谁，业务回放里不用
            speakerless += 1
            continue
        related = matcher.quote_allowed(rec, hit['text']) if matcher.t.get('attribution_version') else (
            matcher.advertiser(hit['text']) or matcher.strong(hit['text']) or dedicated_of(rec))
        if not related:
            off += 1
            continue
        out.append(dict(fp=P.fingerprint(rec), kind=rec["kind"], ts=hit["ts"], speaker=hit["speaker"], quote=hit["text"],
                        scope=pk.get("scope") if pk.get("scope") in SCOPES else "case", why=str(pk.get("why") or "")[:200],
                        source=source_of(rec), anchored=anchored(rec), attribution=rec.get('attribution')))
    if missing or speakerless or off:
        log(f"  丢弃：原话查无 {missing}、说话人不明 {speakerless}、与本客户无关 {off}")
    return out


def source_of(rec):
    if rec["kind"] == "slack":
        return rec["channel"]
    if rec["kind"] == "email":
        return rec["subject"]
    if rec["kind"] == "hubspot":
        return f"HubSpot #{rec['ticket']} {rec['title']}"
    return rec["title"]


def anchored(rec):
    """邮件线程 / 工单本身就点到了本活动（活动名、链接号、单号，或广告主 + 两个特征词）：里面的回复不必再逐句点名。"""
    return rec["kind"] in ("email", "hubspot") and rec.get("tier") in ("direct", "strong")


def dedicated(rec):
    return (rec["kind"] == "slack" and rec.get("why") == "channel") or (rec["kind"] == "meeting" and rec.get("tier") in ("title", "direct")) \
        or anchored(rec)


# ───────────────────────── 第二步：分时间窗汇总 ─────────────────────────
def _ckey(c):
    return c["fp"] + ":" + c["quote"][:60]


def _said_in(c):
    src = c.get("source") or ""
    return {"slack": f"Slack {src}", "meeting": f"MEETING «{src}»", "email": f"EMAIL «{src}»"}.get(c["kind"], src)


def compose_window(cands, terms, ask):
    system = WINDOW_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"]) + attribution_context.prompt_context(terms)
    lines = [f"[{c['id']}] {c['ts'].replace('T', ' ').rstrip('Z')} | {c['speaker']} | {_said_in(c)} | scope={c['scope']} | {c['quote']}" for c in cands]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Candidates (chronological):\n\n" + "\n".join(lines)}]
    doc, usage = P.ask_json(messages, ask)
    return doc.get("events") or [], usage


def compose_meta(moments, terms, ask):
    system = META_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"]) + attribution_context.prompt_context(terms)
    lines = [f"{m['ts'][:10]} | {m['speaker']} | {m['action_en']} | {m['context_en']}" for m in moments]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Key moments:\n\n" + "\n".join(lines)}]
    return P.ask_json(messages, ask)


def _minutes(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() / 60


def _meeting_context(quote, rec, matcher, scope, n=MEETING_CONTEXT_LINES):
    """会上的一句原话：它前后 n 行里点到本活动（平台、链接、活动名，含听写走样），而且别的平台没有离得更近；
    还要是在谈这个客户：会议标题或这几行里有客户名（内部早会、培训会上顺口提到平台名的不算）；
    和别的客户开的会（标题是别的客户）不算，一行里客户名其实是别的客户（「Binance US」）也不算。
    几个活动共用的平台名（如 Chime 的 LG、Vizio、TCL 都走 Freewheel），同一行里点了别的平台就不算本活动的。
    地区按国家名认：越南活动的会上说 Vietnam / 越南算本活动，说 Venezuela 算别的活动。"""
    lines = rec["text"].split("\n")
    rx = _flex(" ".join((quote or "").split()[:8]))
    at = next((i for i, ln in enumerate(lines) if rx and rx.search(ln)), None)
    if at is None:
        return False
    title = rec.get("title") or ""
    if scope.other_client(title) and not matcher.advertiser(title):
        return False
    own, other, client = [], [], bool(matcher.advertiser(title))
    for j in range(max(0, at - n), min(len(lines), at + n + 1)):
        ln = lines[j]
        client = client or (bool(matcher.advertiser(ln)) and not scope.other_client(ln))
        hits = set(matcher.context(ln)) | set(matcher.sounds_like(ln)) | set(scope.countries(ln))
        foreign = scope.other(ln) or scope.other_country(ln)
        if matcher.strong(ln) or any(h.lower() not in scope.shared for h in hits) or (hits and not foreign):
            own.append(abs(j - at))
        if foreign:
            other.append(abs(j - at))
    return bool(own) and client and (not other or min(own) <= min(other))


def attributable(chosen, cands, by_ckey, terms, log=print, meetings=None):
    """归属闸门：AI 自己标的 scope 不可靠（会把别的活动的预算讨论标成本活动）。入选节点的原话要么自己点到活动特征词
    （合作方/库存方、链接号、活动名、单号），要么是同一场对话里紧接在特征词消息之后的回复；其余丢弃。
    会上的一句话看前后文（见 _meeting_context）：会上很少每句都点平台名。
    活动名里没有特征词、或过滤后太少（< MIN_ATTRIBUTABLE）就不启用，免得把活动误删光。"""
    if not terms.get("context"):
        return chosen
    m = corpus_mod.Matcher(terms)

    def named(text):
        return bool(m.context(text) or m.strong(text))
    anchors = [(c["source"], _minutes(c["ts"])) for c in cands if named(c["quote"])]
    scope = []

    def in_meeting(c):
        rec = (meetings or {}).get(c["fp"])
        if c["kind"] != "meeting" or not rec:
            return False
        if not scope:
            from . import changes as CH
            scope.append(CH.Scope(terms))
        return _meeting_context(c["quote"], rec, m, scope[0])
    kept = []
    for e in chosen:
        c = by_ckey[e["ckey"]]
        t = _minutes(c["ts"])
        if c.get("anchored") or named(c["quote"]) or any(src == c["source"] and 0 <= t - at <= REPLY_MINUTES for src, at in anchors) \
                or in_meeting(c):
            kept.append(e)
    if len(kept) < MIN_ATTRIBUTABLE:
        log(f"  归属闸门：过滤后只剩 {len(kept)} 个节点，少于 {MIN_ATTRIBUTABLE}，本次不启用")
        return chosen
    if len(kept) < len(chosen):
        log(f"  归属闸门：{len(chosen)} 个节点里 {len(chosen) - len(kept)} 个没有点到活动特征词、也不是紧接着的回复，已丢弃")
    return kept


def compose(cands, terms, ask, cache, log=print, meetings=None):
    """候选按时间切成窗（每窗约 WINDOW 条），各窗独立挑关键节点；窗结果按内容缓存，只重算变化的窗；最后统一写标题、概要、阶段等。
    → (plan, 用量列表)。plan 里每个事件带 chapter（按阶段起始日期划分）。"""
    import concurrent.futures
    import hashlib
    windows = [cands[i:i + WINDOW] for i in range(0, len(cands), WINDOW)]
    keyed = [(hashlib.sha1("|".join(_ckey(c) for c in w).encode()).hexdigest()[:16], w) for w in windows]
    todo = [(h, w) for h, w in keyed if h not in cache]
    usages = []
    if todo:
        def run(item):
            h, w = item
            events, usage = compose_window(w, terms, ask)
            by_id = {c["id"]: c for c in w}
            out = []
            for e in events:  # 缓存里按候选内容（而不是会变的编号）记，下次候选重新编号也对得上
                c = by_id.get(e.get("candidate"))
                if c:
                    out.append({**e, "candidate": None, "ckey": _ckey(c)})
            return h, out, usage
        with concurrent.futures.ThreadPoolExecutor(max_workers=WINDOW_WORKERS) as pool:
            for h, out, usage in scope.thread_map(pool, run, todo):
                cache[h] = out
                usages.append(usage)
        log(f"  汇总：{len(keyed)} 个时间窗，重算 {len(todo)} 个")
    cid_of = {_ckey(c): c["id"] for c in cands}
    by_ckey = {_ckey(c): c for c in cands}
    chosen = []
    for h, _ in keyed:
        for e in cache.get(h, []):
            if e.get("ckey") in cid_of:
                chosen.append({**e, "candidate": cid_of[e["ckey"]]})
    chosen.sort(key=lambda e: by_ckey[e["ckey"]]["ts"])
    chosen = attributable(chosen, cands, by_ckey, terms, log=log, meetings=meetings)
    if not chosen:
        raise RuntimeError("汇总后没有可用的业务节点")
    moments = [dict(ts=by_ckey[e["ckey"]]["ts"], speaker=by_ckey[e["ckey"]]["speaker"], action_en=_text(e.get("action_en")),
                    context_en=_text(e.get("context_en"))) for e in chosen]
    meta, usage = compose_meta(moments, terms, ask)
    usages.append(usage)
    chapters = []
    for i, c in enumerate(meta.get("chapters") or [], 1):
        if _text(c.get("name_zh")) and _text(c.get("name_en")) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(c.get("start") or "")):
            chapters.append(dict(id=f"c{i}", name_zh=c["name_zh"], name_en=c["name_en"], start=c["start"]))
    chapters.sort(key=lambda c: c["start"])
    if not chapters:
        raise RuntimeError("汇总没有给出有效的阶段")
    for e in chosen:  # 阶段由日期决定：最后一个起始日不晚于该节点的阶段
        day = by_ckey[e["ckey"]]["ts"][:10]
        e["chapter"] = next((c["id"] for c in reversed(chapters) if c["start"] <= day), chapters[0]["id"])
    plan = dict(meta, chapters=chapters, events=chosen)
    return plan, usages


ZH_FIX_SYSTEM = """Translate each item's English action and situation into natural Simplified Chinese for a business replay.
Keep people's names, product names and numbers as they are. Do not add or drop information.
Output one JSON object and nothing else: {"items": [{"i": int, "action_zh": str, "context_zh": str}]}"""


def fix_chinese(plan, ask, log=print):
    """AI 偶尔把中文字段写成英文：只把这些句子从英文版翻成中文补上（一次调用），没有就不调。→ 用量或 None"""
    bad = [e for e in plan.get("events") or []
           if any(_text(e.get(k)) and not has_han(e[k]) for k in ("action_zh", "context_zh"))]
    if not bad:
        return None
    items = [dict(i=i, action_en=_text(e.get("action_en")), context_en=_text(e.get("context_en"))) for i, e in enumerate(bad)]
    messages = [{"role": "system", "content": ZH_FIX_SYSTEM}, {"role": "user", "content": json.dumps(items, ensure_ascii=False)}]
    doc, usage = P.ask_json(messages, ask)
    fixed = 0
    for it in doc.get("items") or []:
        i = it.get("i")
        if isinstance(i, int) and 0 <= i < len(bad):
            for k in ("action_zh", "context_zh"):
                v = _text(it.get(k))
                if v and has_han(v) and not has_han(bad[i].get(k) or ""):
                    bad[i][k] = v
                    fixed += 1
    log(f"  中文字段写成了英文的 {len(bad)} 条，已补译 {fixed} 处")
    return usage


def _en_ok(*values):
    return not any(has_han(v) for v in values if isinstance(v, str))


def _text(v):
    return v.strip() if isinstance(v, str) else ""


_PLACEHOLDER = re.compile(r"\[(?:link omitted|attachment)\]")


def meaningful(quote):
    """去掉「[link omitted]」这类占位后还有几个词：只剩一个链接的原话当不了依据。"""
    rest = _PLACEHOLDER.sub(" ", quote or "")
    return len(re.findall(r"[A-Za-z0-9']+", rest)) + len(re.findall(r"[一-鿿]", rest)) >= 3


def progress_block(cid, lang, doc=None, strict=True):
    """进行中事项 → 业务版里的一个页签（文字、标签都在这里按语言定好，模板只负责画）。没有数据返回 None。"""
    if doc is None:
        doc = read_json(camp_dir(cid) / "progress.json")
    if not doc or not doc.get("items"):
        return None
    T, z = progress_labels.T[lang], lang == "zh"
    order = {s: i for i, s in enumerate(("this_campaign", "client_general", "same_client", "unclear"))}
    items = []
    for it in sorted(doc["items"], key=lambda i: (bool(i.get("stale")), order.get(i.get("scope"), 9))):
        if it.get("scope") == "same_client":
            continue  # 同客户的其他活动（如 LG 活动页里的 Vizio 排期）在那个活动自己的页面里看，这里不放
        topic, note, step = (progress_labels.pick(it, k, lang) for k in ("topic", "state_note", "next_step"))
        if strict and not z and not (_text(it.get("topic_en")) and _en_ok(it.get("topic_en"), it.get("state_note_en"), it.get("next_step_en"))):
            continue  # 英文页只放有干净英文的事项
        unknown = it.get("next_owner") == "待确认"
        items.append(dict(
            topic=topic, status=it.get("status"), status_name=T["status"].get(it.get("status"), ""), stale=bool(it.get("stale")),
            stale_text=T["stale_days"].format(n=it.get("age_days")) if it.get("stale") else "", scope_name=T["scope"].get(it.get("scope"), ""),
            note=note, next=step, owner=T["unconfirmed"] if unknown else it.get("next_owner"), owner_unknown=unknown,
            basis="" if unknown else T["basis"].get(it.get("owner_basis"), ""), last=it.get("last_activity") or "",
            evidence=[dict(quote=q, who=e.get("speaker") or "", when=str(e.get("ts", ""))[:16].replace("T", " "))
                      for e in it.get("evidence") or [] for q in [display_original(e["quote"])] if meaningful(q)]))
        if not items[-1]["evidence"]:  # 证据只剩隐藏掉的链接：没有能给人看的依据，这条不显示
            items.pop()
    if not items:
        return None
    return dict(items=items, labels=dict(title=T["title"], intro=T["sub"], next=T["next"], owner=T["owner"], last=T["last"],
                                         evidence=T["evidence"], empty=T["empty"], foot=T["foot"]))


def select(cands, plan, log=print, strict=True):
    """AI 汇总里哪些节点有效：候选存在、不重复、字段齐全、英文里没有汉字。→ (阶段, 阶段编号, [(候选, 汇总, 原话语言)])，按时间排好。"""
    by_id = {c["id"]: c for c in cands}
    chapters = [dict(id=c["id"], zh=_text(c.get("name_zh")), en=_text(c.get("name_en"))) for c in plan.get("chapters") or []
                if c.get("id") and _text(c.get("name_zh")) and _text(c.get("name_en")) and (not strict or _en_ok(c.get("name_en")))]
    chap_ids = [c["id"] for c in chapters]
    chosen, used, dropped = [], set(), 0
    for e in plan.get("events") or []:
        c = by_id.get(e.get("candidate"))
        if not c or c["id"] in used or e.get("chapter") not in chap_ids:
            dropped += 1
            continue
        fields = [_text(e.get(k)) for k in ("action_zh", "action_en", "context_zh", "context_en", "translation")]
        if strict and (not all(fields) or not _en_ok(e.get("action_en"), e.get("context_en"))):
            dropped += 1
            continue
        lang = "zh" if has_han(c["quote"]) else "en"
        if strict and lang == "zh" and not _en_ok(e.get("translation")):  # 中文原话 → 英文译文里不能有汉字
            dropped += 1
            continue
        used.add(c["id"])
        chosen.append((c, e, lang))
    if dropped:
        log(f"  汇总里有 {dropped} 条无效（候选不存在/重复/缺字段/英文含汉字），已忽略")
    chosen.sort(key=lambda t: t[0]["ts"])
    return chapters, chap_ids, chosen


def source_tag(c, L, platform_of):
    """每条原话的具体出处标签：Slack #频道 / Fireflies 会议 / Teams 会议 / Gmail 邮件 / HubSpot 工单 #编号。"""
    k = c["kind"]
    if k == "slack":
        return f"Slack {c['source']}"
    if k == "github" and c.get("source"):
        return L["sourceLabels"]["github"] + " " + c["source"]
    if k == "meeting":
        return L["platforms"]["ken-teams" if platform_of.get(c["fp"]) == "ken-teams" else "fireflies"]
    if k == "hubspot":
        m = re.match(r"HubSpot #(\d+)", c.get("source") or "")
        return f"{L['sourceLabels']['hubspot']} #{m.group(1)}" if m else L["sourceLabels"]["hubspot"]
    return L["sourceLabels"][k]


def source_origin(c, L, z):
    """出处的具体名字（会议名、邮件主题、工单标题）；英文页遇到中文标题就不写，避免英文页夹中文。"""
    k = c["kind"]
    if k not in L["origin"]:
        return ""
    name = c.get("source") or ""
    if k == "hubspot":
        name = re.sub(r"^HubSpot #\d+\s*", "", name)
    if not name or (not z and has_han(name)):
        return ""
    return L["origin"][k] + name


GROUPS = ("team", "partner", "room")
_ROOM = re.compile(r"^[A-Z]{2,4}-\d+-")  # 会议室设备的录音标签，如 SFO-5-Sunset：认不出具体是谁


def group_of(name, external):
    if _ROOM.match(name or ""):
        return "room"
    return "partner" if name in external else "team"


def assemble(cid, terms, cands, plan, changes, today, log=print, handoffs=(), platform_of=None, external=frozenset(), discussions=None,
             progress_doc=None, strict=True):
    """候选 + AI 汇总 + 后台改动（及各自在哪讨论的）→ 与 V11 同形的 case（中、英各一份）。"""
    from . import changes as CH
    cfg = json.loads(UI_FILE.read_text(encoding="utf-8"))
    roster = Roster()
    chapters, chap_ids, chosen = select(cands, plan, log=log, strict=strict)
    if not chosen:
        raise RuntimeError("汇总后没有可用的业务节点")

    # 人物：说话人 + 后台改动的操作人；姓名、颜色、岗位取自人物对照表
    people, pid_of = [], {}

    def person(system, account):
        key = (system, account)
        if key in pid_of:
            return pid_of[key]
        rid = roster.resolve(system, account)
        r = roster.people[rid]
        for p in people:  # 同一个人在不同系统里的账号：登记成了两个人也按姓名合并
            if p["_rid"] == rid or name_key(p["name"]) == name_key(r["name"]):
                p["_accounts"].add(account)
                pid_of[key] = p["id"]
                return p["id"]
        pid = f"person-{len(people) + 1:02d}"
        people.append(dict(id=pid, _rid=rid, _accounts={account}, name=r["name"], short=r.get("short") or r["name"].split(" ")[0], kind="human",
                           color=r["color"], tag=None, role=None))
        pid_of[key] = pid
        return pid

    for c, _, _ in chosen:
        person(system_of(c), c["speaker"])
    change_rows = [(ch, person("femini", ch["user"])) for ch in changes]
    handoff_rows = [(h, person(h.get("system", "slack"), h["actor"])) for h in handoffs]

    def person_by_name(name):
        for (system, account), pid in pid_of.items():
            if system != "femini" and account == name:
                return pid
        return None

    roles = {}
    for r in plan.get("people") or []:
        zh, en = _text(r.get("role_zh")), _text(r.get("role_en"))
        if r.get("name") and zh and en and _en_ok(en):
            roles[r["name"]] = dict(zh=zh, en=en)

    case_events = []
    for c, e, lang in chosen:
        actor = person(system_of(c), c["speaker"])
        to = [pid for pid in (person_by_name(n) for n in (e.get("to") or [])) if pid and pid != actor]
        case_events.append(dict(cand=c, e=e, lang=lang, actor=actor, to=list(dict.fromkeys(to))))
    # 后台改动按时间并入
    timeline = [(x["cand"]["ts"], "msg", x) for x in case_events] + [(ch["ts"], "change", (ch, pid)) for ch, pid in change_rows] \
        + [(h["ts"], "handoff", (h, pid)) for h, pid in handoff_rows]
    timeline.sort(key=lambda t: t[0])

    first_ts, last_ts = timeline[0][0], timeline[-1][0]
    chapter_of = {}
    for x in case_events:
        chapter_of.setdefault(x["e"]["chapter"], []).append(x["cand"]["ts"])

    out = {}
    for lang in ("zh", "en"):
        z = lang == "zh"
        L = cfg[lang]
        title = _text(plan.get("title_zh" if z else "title_en")) or terms["advertiser"]
        # 后台改动挂到时间上最近的前一个阶段（没有就第一个）
        def act_for(ts):
            pick = chap_ids[0]
            for c in chapters:
                if c["id"] in chapter_of and min(chapter_of[c["id"]]) <= ts:
                    pick = c["id"]
            return pick
        events = []
        for i, (ts, kind, x) in enumerate(timeline, 1):
            base = dict(key=f"moment-{i:03d}", id=f"moment-{i:03d}", ts=ts, prec="sec", clear=[], text=None, margin=None)
            if kind == "msg":
                c, e = x["cand"], x["e"]
                shown = display_original(c["quote"])
                same = x["lang"] == lang
                tr = shown if same else _text(e["translation"])
                orig = dict(text=shown, language=x["lang"], speaker_label=c["speaker"], translation=tr)
                events.append({**base, "prec": c.get("prec", "sec"), "src": c["kind"], "srcs": [c["kind"]], "kind": "chat", "actor": x["actor"], "to": x["to"],
                               "action": _text(e["action_zh" if z else "action_en"]), "title": _text(e["action_zh" if z else "action_en"]),
                               "quote": shown, "translation": tr, "presentation_tag": source_tag(c, L, platform_of or {}),
                               "origin": source_origin(c, L, z),
                               "presentation_context": _text(e["context_zh" if z else "context_en"]), "act": f"a{chap_ids.index(e['chapter']) + 1}",
                               "baton": True, "compact": False, "original": orig})
            elif kind == "handoff":
                h, pid = x
                h_title, ctx_ = h["title_zh" if z else "title_en"], h["context_zh" if z else "context_en"]
                events.append({**base, "prec": h.get("prec", "sec"), "src": h["src"], "srcs": [h["src"]], "kind": h["kind"], "actor": pid, "to": [],
                               "action": h_title, "title": h_title, "quote": None, "translation": None,
                               "presentation_tag": h["tag_zh" if z else "tag_en"], "presentation_context": ctx_, "ticket_status": h.get("status"),
                               "act": f"a{chap_ids.index(act_for(ts)) + 1}", "baton": h["kind"] == "assign", "compact": True})
            else:
                ch, pid = x
                tail = CH.describe(ch, lang)
                row = {**base, "src": "femini", "srcs": ["femini"], "kind": "change", "actor": pid, "to": [],
                       "action": L["sourceLabels"]["femini"], "title": tail, "quote": None, "translation": None,
                       "presentation_tag": L["sourceLabels"]["femini"],
                       "presentation_context": tail + ("。" if z else "."),
                       "margin": float(ch["new"]["margin"]) if "margin" in ch["new"] else None,
                       "act": f"a{chap_ids.index(act_for(ts)) + 1}", "baton": False, "compact": True}
                found = (discussions or {}).get(ch["id"])
                if found is not None:  # 找过：有讨论列出来，没有也照实说；没找过（如没配 AI）就不显示这一块
                    row["discussion"] = [discussion_item(p, lang, L, platform_of or {}, roster) for p in found["picks"]]
                    row["discussion_reason"] = _text(found.get("reason_zh" if z else "reason_en")) if row["discussion"] else ""
                events.append(row)
        used_acts = sorted({int(e["act"][1:]) for e in events})
        acts = []
        for n in used_acts:
            cid_ = chap_ids[n - 1]
            ts_list = [e["ts"] for e in events if e["act"] == f"a{n}"]
            acts.append(dict(id=f"a{n}", name=chapters[n - 1]["zh" if z else "en"], span=f"{min(ts_list)[:10]} – {max(ts_list)[:10]}"))
        srcs = [s for s in ("slack", "meeting", "email", "hubspot", "github", "femini") if any(e["src"] == s for e in events)]
        plats = sorted({e["presentation_tag"] for e in events if e["src"] == "meeting"})
        meeting_name = " / ".join(plats) if plats else L["sources"]["meeting"]
        ppl = []
        speakers_used = {e["actor"] for e in events} | {p for e in events for p in e["to"]}
        for p in people:
            if p["id"] not in speakers_used:
                continue
            duty = next((v[lang] for k, v in roles.items() if k in p["_accounts"] and v.get(lang)), None)
            job = roster.job_title(p["_rid"], lang)
            role = f"{job} · {duty}" if job and duty else (job or duty or L["role_default"])
            rr = roster.people[p["_rid"]]
            name = (rr.get("name_en") or p["name"]) if not z else p["name"]
            short = (rr.get("short_en") or p["short"]) if not z else p["short"]
            g = group_of(p["name"], external)
            ppl.append(dict(id=p["id"], name=name, short=short, kind="human", color=p["color"], group=g, tag=L["ui"]["groupTags"][g], role=role))
        ppl.sort(key=lambda q: GROUPS.index(q["group"]))  # 同组的人排在一起（泳道表头按组分段），组内保持出场顺序
        names_to_short = {}
        for (system, account), pid in pid_of.items():
            if system != "femini":
                names_to_short[account] = next((p["short"] for p in ppl if p["id"] == pid), account)
        workstreams = []
        for w in plan.get("workstreams") or []:
            t, tx = _text(w.get("title_zh" if z else "title_en")), _text(w.get("text_zh" if z else "text_en"))
            if t and tx and (z or _en_ok(t, tx)):
                workstreams.append(dict(title=t, text=tx, people=[names_to_short[n] for n in (w.get("people") or []) if n in names_to_short]))
        ui = json.loads(json.dumps(L["ui"]).replace("{advertiser}", terms["advertiser"]).replace("{title}", title.replace('"', "'")))
        prog = progress_block(cid, lang, doc=progress_doc, strict=strict)
        if prog:
            ui["tabs"]["progress"] = prog["labels"]["title"]
        case = dict(
            meta=dict(no=terms["advertiser"], title=title, client=terms["advertiser"], campaign=terms["name"], click_urls=terms["links"],
                      presentation_mode=True, hide_charter=True, hide_acts=True,
                      playback="presentation", edition=L["edition"], version=today.strftime("%Y%m%d")),
            acts=acts, sources=[dict(id=s, name=meeting_name if s == "meeting" else L["sources"][s]) for s in srcs],
            summary=_text(plan.get("summary_zh" if z else "summary_en")),
            presentation=dict(ui=ui, channels=[L["channels"][s] for s in srcs], workstreams=workstreams, sourceLabels=L["sourceLabels"],
                              metrics=daily_metrics(cid, first_ts, last_ts),
                              **({"progress": prog} if prog else {})),
            ui=L["ui_top"], people=ppl, events=events)
        attribution_context.attach(case, cid, lang, terms)
        if not z:
            probe = json.loads(json.dumps(case, ensure_ascii=False))
            for it in (probe["presentation"].get("progress") or {}).get("items", []):  # 事项里的原话和人名保留原样
                it.pop("evidence", None)
                it.pop("owner", None)
            for e in probe["events"]:  # 真人原话保留原语言（含汉字也没关系），其余界面、整理文字、译文必须是英文
                e.pop("quote", None)
                for d in e.get("discussion") or []:
                    d.pop("quote", None)
                    d.pop("who", None)
                if e.get("original"):
                    e["original"].pop("text", None)
                    e["original"].pop("speaker_label", None)
                if e.get("original") and e["original"]["language"] == "en":
                    e.pop("translation", None)
                    e["original"].pop("translation", None)
            if strict and has_han(json.dumps(probe, ensure_ascii=False)):
                raise RuntimeError("英文版的界面或整理文字里还有汉字")
        out[lang] = case
    return out


def discussion_item(p, lang, L, platform_of, roster):
    """后台改动下面的一条讨论原话：出处、谁、什么时候（离改动多久）、原话，原话是另一种语言时附译文。"""
    from . import changes as CH
    z = lang == "zh"
    r = roster.people[roster.resolve(system_of(p), p["speaker"])]
    who = r["name"] if z else (r.get("name_en") or r["name"])
    if not z and has_han(who):
        who = p["speaker"]
    tag = source_tag(p, L, platform_of)
    if not z and has_han(tag):
        tag = L["sourceLabels"][p["kind"]]
    return dict(tag=tag, origin=source_origin(p, L, z), who=who, ts=p["ts"], when=CH.gap(p["minutes"], lang),
                quote=display_original(p["quote"]), translation=p["translation"] if has_han(p["quote"]) != z else "")


def handoff_events(cid, log=print):
    """业务版里的「谁接了单」：GitHub Issue 谁接手、谁关单，HubSpot 工单建单和负责人。结构化记录，不经 AI。
    只要谁、什么时候、哪张单；GitHub 的提交、评论、标签流转这些开发细节不放（用户要求）。取自详细版整理好的事件。"""
    from . import events as E
    try:
        ctx = E.build_events(cid, log=lambda *_: None)
    except Exception as e:  # 详细版的数据还不全：业务版照常生成，只是没有这部分
        log(f"  接单记录取不到（忽略）：{str(e)[:120]}")
        return []
    R, tickets = ctx["roster"], ctx["tickets"]
    hs_of_issue = {h["key"]: h["id"] for h in ctx["hubspot"] if not h["standalone"]}
    hs_info = {h["id"]: h for h in ctx["hubspot"]}

    def name(pid):
        p = R.people.get(pid) or {}
        return None if p.get("kind") != "human" or pid == "slackbot" else p.get("name")
    out = []
    for e in ctx["events"]:
        t = tickets.get(e["ticket"]) or {}
        if e["src"] == "github" and t.get("num") is not None:
            L = f"#{t['num']}" if t["repo"] == "feed-mob/feedmob" else f"{t['repo'].split('/')[1]}#{t['num']}"
            tag_zh, tag_en = f"GitHub Issue {L}", f"GitHub Issue {L}"
            hs = hs_of_issue.get(e["ticket"])
            via_zh, via_en = (f"对应 HubSpot 工单 #{hs}。", f"From HubSpot ticket #{hs}.") if hs else ("", "")
            if e["kind"] == "assign" and (e.get("hop") or ("",))[0] == "assign" and e["to"]:
                who, by = name(e["to"][0]), name(e["actor"])
                if not who:
                    continue
                self_ = by == who
                out.append(dict(ts=e["ts"], src="github", kind="assign", actor=who, tag_zh=tag_zh, tag_en=tag_en,
                                title_zh=f"{who} 接手 {tag_zh}", title_en=f"{who} takes {tag_en}",
                                context_zh=("自己领取。" if self_ else (f"由 {by} 指派。" if by else "系统自动指派。")) + via_zh,
                                context_en=("Self-assigned. " if self_ else (f"Assigned by {by}. " if by else "Assigned automatically. ")) + via_en))
            elif e["kind"] == "close":
                by = name(e["actor"])
                if not by:
                    continue
                out.append(dict(ts=e["ts"], src="github", kind="close", actor=by, tag_zh=tag_zh, tag_en=tag_en,
                                title_zh=f"{tag_zh} 已关单", title_en=f"{tag_en} closed",
                                context_zh=f"由 {by} 关单。" + via_zh, context_en=f"Closed by {by}. " + via_en))
        elif e["src"] == "hubspot" and e["kind"] == "create":
            tid = e["key"].split(":")[1]
            h = hs_info.get(tid) or {}
            owner = h.get("owner")
            if not owner:
                continue
            title = (h.get("title") or "").strip()
            title_en = E.en_title(title)
            out.append(dict(ts=e["ts"], src="hubspot", kind="create", actor=owner, status=h.get("status"), tag_zh=f"HubSpot 工单 #{tid}", tag_en=f"HubSpot ticket #{tid}",
                            title_zh=f"HubSpot 工单 #{tid}：{title}" if title else f"HubSpot 工单 #{tid}",
                            title_en=f"HubSpot ticket #{tid}: {title_en}" if title_en else f"HubSpot ticket #{tid}",
                            context_zh=f"建单，负责人 {owner}" + (f"，当前状态 {h['status']}。" if h.get("status") else "。"),
                            context_en=f"Ticket filed; owner {owner}" + (f"; current status {h['status']}." if h.get("status") else ".")))
    return sorted(out, key=lambda h: h["ts"])


def system_of(c):
    """候选的说话人按哪个系统的账号去对人物表：HubSpot 备注用 hubspot 账号，邮件用邮件署名，其余（Slack、会议）按显示名。"""
    return {"hubspot": "hubspot", "email": "email", "github": "github"}.get(c["kind"], "slack")


def daily_metrics(cid, first_ts, last_ts):
    """证据图用的每日花费、安装：从回放第一天前 3 天到最后一天（不超过今天），没有数据返回 None。"""
    doc = read_json(camp_dir(cid) / "metrics.json")
    if not doc:
        return None
    s = doc["series"]
    lo = (dt.date.fromisoformat(first_ts[:10]) - dt.timedelta(days=3)).isoformat()
    hi = last_ts[:10]
    spend = {d: v for d, v in s.get("gross_spend", []) if lo <= d <= hi}
    installs = {d: v for d, v in s.get("installs", []) if lo <= d <= hi}
    dates = sorted(spend)
    if not any(spend.get(d) for d in dates):
        return None
    return dict(dates=dates, spend=[round(spend[d] or 0, 2) for d in dates], installs=[installs.get(d) for d in dates])


# ───────────────────────── 总装 ─────────────────────────
def build(cid, log=print, ask=None, today=None):
    cid = valid_number(cid)
    ask = ask or P.chat
    today = today or dt.datetime.now(dt.timezone.utc).date()
    doc = read_json(camp_dir(cid) / "corpus.json")
    if not doc:
        raise RuntimeError(f"活动 {cid} 还没有 AI 原料（corpus.json）")
    terms, records = doc["terms"], doc["records"]
    matcher = corpus_mod.Matcher(terms)
    terms_sig = json.dumps([terms["name"], terms["advertiser"], terms["context"], terms["links"], terms["issues"]], sort_keys=True)  # 归属闸门用的词表变了就要重做汇总
    state_path = camp_dir(cid) / "business.json"
    prev = read_json(state_path) or {}
    attribution_sig = json.dumps([doc.get('attribution_version'), terms], sort_keys=True) if doc.get('attribution_version') else None
    if attribution_sig and prev.get('attribution_sig') != attribution_sig:
        prev = {}  # Old client-wide summaries must not survive a tighter attribution rule/catalog.
    seen, cands = set(prev.get("seen") or []), list(prev.get("candidates") or [])
    if prev and prev.get("pick_version") != PICK_VERSION:
        seen = set()  # 挑选方式变了：候选留着，所有原料重新挑一遍
    by_fp = {P.fingerprint(r): r for r in records}
    kept = [c for c in cands if c["fp"] in by_fp]  # 原料筛选规则收紧后，已经不算本活动的记录里挑出的候选一并去掉
    pruned = len(cands) - len(kept)
    if pruned:
        log(f"  原料里已经没有的记录挑出的候选 {pruned} 条，去掉")
    cands = kept
    for c in cands:  # 线程是否点到本活动（anchored）跟着当前原料重算
        c["anchored"] = anchored(by_fp[c["fp"]])
    todo = [r for r in records if P.fingerprint(r) not in seen]
    log(f"业务版：原料 {len(records)} 条，新增 {len(todo)} 条，已核实候选 {len(cands)} 条")
    runs = prev.get("runs") or []
    blocks = _chunks(todo)
    known = {(c["fp"], c["quote"][:60]) for c in cands}

    def pick(recs):
        picks, usage = pick_once(recs, terms, ask)
        return recs, picks, usage
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=PICK_WORKERS) as pool:
        for i, (recs, picks, usage) in enumerate(scope.thread_map(pool, pick, blocks), 1):
            for c in verify_picks(picks, recs, matcher, dedicated, log=log):
                if (c["fp"], c["quote"][:60]) not in known:
                    known.add((c["fp"], c["quote"][:60]))
                    cands.append(c)
            seen.update(P.fingerprint(r) for r in recs)
            runs.append(dict(at=now_z(), step="pick", records=len(recs), usage=usage))
            write_json(state_path, dict(built_at=now_z(), seen=sorted(seen), candidates=cands, runs=runs, pick_version=PICK_VERSION,
                                        plan=prev.get("plan"), plan_version=prev.get("plan_version"), compose_cache=prev.get("compose_cache"),
                                        window_version=prev.get("window_version"), terms_sig=prev.get("terms_sig"),
                                        attribution_version=doc.get('attribution_version'), attribution_sig=attribution_sig))
            log(f"  第 {i}/{len(blocks)} 块（到 {recs[-1]['ts'][:10]}）：候选累计 {len(cands)} 条")
    if not cands:
        raise RuntimeError("没有挑出任何可用的业务原话")
    cands.sort(key=lambda c: c["ts"])
    for i, c in enumerate(cands, 1):
        c["id"] = f"C{i:03d}"
    usages = []
    redo = bool(blocks) or bool(pruned) or not prev.get("plan") or prev.get("plan_version") != PLAN_VERSION \
        or prev.get("window_version") != WINDOW_VERSION or prev.get("terms_sig") != terms_sig
    cache = dict(prev.get("compose_cache") or {}) if prev.get("window_version") == WINDOW_VERSION else {}
    if redo:
        plan, usages = compose(cands, terms, ask, cache, log=log, meetings={P.fingerprint(r): r for r in records if r["kind"] == "meeting"})
        runs.append(dict(at=now_z(), step="compose", candidates=len(cands), usage=usages))
    else:
        plan = prev["plan"]
    zh_usage = fix_chinese(plan, ask, log=log)
    if zh_usage:
        runs.append(dict(at=now_z(), step="fix_chinese", usage=zh_usage))
    platform_of = {P.fingerprint(r): r.get("source_id") for r in records if r["kind"] == "meeting"}
    external = {n for r in records if r["kind"] == "email" for n in r.get("external") or []}
    known = {p["name"] for p in Roster().people.values()}
    external |= {r["who"] for r in records if r.get("who") and r["who"] not in known and corpus_mod.brainpages.external_name(r["who"])}
    from . import changes as CH
    param = CH.param_changes(cid)
    found, link_usages = CH.link(cid, param, records, terms, ask, log=log)
    if link_usages:
        runs.append(dict(at=now_z(), step="changes", usage=link_usages))
    cases = assemble(cid, terms, cands, plan, param, today, log=log, handoffs=handoff_events(cid, log=log),
                     platform_of=platform_of, external=external, discussions=found)
    od = out_dir(cid)
    for lang, case in cases.items():
        html = build_mod.render(case, lang)
        tmp = od / f"business.{lang}.html.tmp"
        od.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(od / f"business.{lang}.html")
        write_json(od / f"business.case.{lang}.json", case)
    write_json(state_path, dict(built_at=now_z(), seen=sorted(seen), candidates=cands, plan=plan, plan_version=PLAN_VERSION, compose_cache=cache, window_version=WINDOW_VERSION, terms_sig=terms_sig, runs=runs, pick_version=PICK_VERSION,
                               attribution_version=doc.get('attribution_version'), attribution_sig=attribution_sig))
    return dict(candidates=len(cands), events=len(cases["zh"]["events"]), people=len(cases["zh"]["people"]),
                calls=len(blocks) + len(usages) + len(link_usages))
