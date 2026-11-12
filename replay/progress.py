"""进行中事项 + 下一步负责人（AI）：读 AI 原料（corpus.json），产出 raw/campaigns/<活动>/progress.json。

做法（来自 2026-10-22 的 Chime 试验，见 docs/AI进行中事项.md）：
- 原料按时间切成若干块，顺着上一份清单往下更新：每次给模型「目前的进行中清单 + 新的一块记录」，让它加新事项、更新状态、
  把已做完/取消的移到已结束。之后每天只喂新增记录（没有新增就不调模型）。
- 新记录有好几块时（新活动第一次生成、规则改了要重读）：一块接一块地接力要一块等一块，内容多了太慢。改成先各块同时让模型
  摘出「事」（请求、承诺、指派、等回复、进展、完成、取消，各附原话，程序逐字核对，对不上的丢掉），再按时间把这些事一次交给
  模型合成清单。只有一块（平时每天）照旧直接更新。
- 模型引用的每一句原话由程序在全部原料里逐字核对：逐字/近似才留，查无的丢掉；一个事项没有任何可核对的原话就不收。
- 负责人没有明确依据（自己承诺 / 被点名请求且未拒绝 / 明确指派）一律写「待确认」，不按职位或谁最后发言猜。
- 「久未更新」由程序按最近一条原话的日期判断（>14 天），不让模型算天数。
- 会议逐字稿里客户只是会议室标签（SFO-5-Emery）、Speaker N，原样使用，不当成人名。
密钥与网关地址在 REPLAY_SECRETS/ai.env（AI_BASE_URL、AI_MODEL、AI_API_KEY），不入库、不打印。
模型现在是 minimax-m3（2026-10-30 起；之前是 minimax-m3.1-flash），网关登记的上下文是 52 万 token。
思考深度可选 AI_REASONING_EFFORT（low / medium / high / xhigh / max，flash 时期用过；不填就用模型默认）；
同名环境变量优先，做对比测试用。温度按 MiniMax 官方建议传 1（flash 不认温度，实际一直是 1）。
AI_THINKING=disabled 关掉 m3 的思考（m3 只有开/关，没有分档）：2026-10-30 实测快约 9 倍，但会漏事项、归属判断变差，
平时不开；只在第一次把全部活动铺开时用（批量脚本里设环境变量，不写进 ai.env，每天的刷新照样思考）。
"""
import datetime as dt
import difflib
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

from . import corpus as corpus_mod
from . import scope, attribution_context
from .util import camp_dir, iso, list_file, now_z, read_json, valid_number, write_json

CHUNK_CHARS = 300_000      # 一次给模型的新记录大约多少字符（试验里 36 万字符 / 11 万 token 跑得通）
MAX_OUTPUT_TOKENS = 90_000 # 模型的「思考」也占输出额度，32000 会被用光
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
THINKING_MODES = ("disabled", "adaptive")  # MiniMax 接口 thinking.type 的取值；不传就是开着思考
JSON_ATTEMPTS = 7          # 模型偶尔吐出格式坏掉的 JSON（输出很长、关了思考时更常见，比如漏掉一个 {"）：把原文和报错发回去让它自己改，最多改 6 轮（连第一次共 7 次）
DIGEST_WORKERS = 16        # 好几块新记录时同时摘要的块数：一个活动一般十块以内，一次全跑完；超过 16 块才分轮，免得一下子压给 AI 服务太多
COMBINE_CHARS = 400_000    # 合成时一次给模型多少字的摘要：模型一次能读 52 万 token（还要留出输出额度），摘要又紧凑，一般一次合成完，不用分组接力
DIGEST_KINDS = ("request", "commitment", "assignment", "waiting", "update", "done", "cancelled")
STALE_DAYS = 14
SCOPE_VERSION = 2          # 归属判断的说明改了就加一，已有清单会重新判一次
SECRETS = pathlib.Path(os.environ.get("REPLAY_SECRETS", pathlib.Path(__file__).resolve().parent.parent / "secrets"))
STATUSES = ("in_progress", "waiting_on_others", "on_hold")
SCOPES = ("this_campaign", "same_client", "client_general", "unclear")
BASIS = ("self_commitment", "direct_request", "explicit_assignment", "none")

SYSTEM = """You are a careful operations analyst at FeedMob, an ad-tech company. You maintain a list of work that is still IN PROGRESS for one advertiser account, and who owns the next step, by reading raw working communication (Slack messages, meeting transcripts, emails with partners and clients, and HubSpot tickets with their notes).

Context: advertiser "{advertiser}"; the campaign this list is for is "{campaign}" (keywords: {keywords}). Records may also talk about other campaigns, other ad partners, or other clients entirely.

You receive (a) the current list of open items, found from earlier records, and (b) new records in chronological order. Return the UPDATED list.

Rules
1. Use only the records and the current list. Treat the as-of time as "now". No outside knowledge, no guessing.
2. An item is OPEN only if someone asked for it, promised it, or an answer/decision is awaited, AND no later record shows it was done, delivered, answered or cancelled. When new records show an open item is finished or cancelled, remove it from items and put it in closed with the closing evidence.
3. Keep existing items' "id" unchanged when you keep or update them. New items get id null. Merge duplicates about the same matter.
4. next_owner is the single person or party who must make the next move.
   - owner_basis "self_commitment": that person said they would do it.
   - "direct_request": someone asked that named person to do it and they did not decline.
   - "explicit_assignment": a record explicitly assigns it.
   - If none is clearly supported, next_owner is "待确认" and owner_basis "none". Never infer an owner from a job title or from who spoke last.
   - Meeting speaker labels such as "SFO-5-Emery" or "Speaker 2" are labels, not names. Use them exactly as written; never turn them into a person's name.
   - In an EMAIL line the speaker is the sender. A HUBSPOT "ticket created" line has no known author (the owner is the assignee, not the writer); a HUBSPOT "note by X" line is written by X.
   - Lines in a meeting block under "Fireflies to-dos" are an automatic extraction (who is to do what), without completion status; they may support an item but never prove it is still open.
5. Evidence: 1-3 verbatim quotes per item, copied exactly from the records or from the existing item's evidence (same language, no ellipsis, no paraphrase), each with timestamp and speaker. Each quote is contiguous and at most 240 characters. Prefer the latest relevant quotes.
6. scope: "this_campaign" when the item is about this campaign ({campaign}), including {advertiser}'s work on this campaign's own partner or platform (the one named in the campaign name); "same_client" when about the same advertiser ({advertiser}) but another campaign or partner; "client_general" for account-level matters (measurement, budget, reporting, legal); "unclear" if you cannot tell.
   Ad partners and inventory sources (for example Samsung, Vizio, TCL, Roku, LG, Magnite) also work with many other advertisers. Work that only concerns such a partner, without the records tying it to {advertiser}, is NOT wanted: leave it out. At least one quote of every item must name {advertiser} itself, unless it comes from a channel dedicated to {advertiser}.
7. Write topic, state_note and next_step twice: Chinese (topic, state_note, next_step) and English (topic_en, state_note_en, next_step_en), plain business language, no jargon. Keep quotes in their original language.
8. This list is for this campaign: keep items about this campaign and account-level matters of {advertiser}. Leave out work that only concerns another campaign of {advertiser} (another platform, market or partner); it is tracked on that campaign's own list. Return at most 15 open items, most important first. Prefer fewer, well-supported items to many weak ones.

Output one JSON object and nothing else:
{{"items": [{{"id": str|null, "topic": str, "topic_en": str, "scope": "this_campaign|same_client|client_general|unclear", "status": "in_progress|waiting_on_others|on_hold", "state_note": str, "state_note_en": str, "next_step": str, "next_step_en": str, "next_owner": str, "owner_basis": "self_commitment|direct_request|explicit_assignment|none", "evidence": [{{"ts": str, "speaker": str, "quote": str}}], "confidence": "high|medium|low"}}], "closed": [{{"id": str, "reason": str, "reason_en": str, "evidence": [{{"ts": str, "speaker": str, "quote": str}}]}}]}}"""


DIGEST_SYSTEM = """You read raw working communication of one advertiser account (Slack messages, meeting transcripts, emails with partners and clients, HubSpot tickets and notes) and pull out the events that matter for tracking open work. A later step combines the events of all periods into the list of work still in progress, so be complete and concrete.

Context: advertiser "{advertiser}"; the campaign is "{campaign}" (keywords: {keywords}). Records may also talk about other campaigns, other ad partners, or other clients entirely.

From the records below (each starts with an id like R12), list every event about work for {advertiser}:
- request: someone asks a person or team to do something, or asks a question that needs an answer;
- commitment: someone says they will do something;
- assignment: a record explicitly assigns work to someone;
- waiting: an answer, decision, approval or delivery is awaited;
- update: progress on a matter (partly done, blocked, delayed, changed);
- done: something asked, promised or awaited was done, delivered, answered or resolved;
- cancelled: something was dropped or is no longer needed.
Skip greetings, chatter, bot alerts and automatic reports, and anything only about other advertisers. Lines under "Fireflies to-dos" are an automatic extraction: you may use them for a request or commitment, never for done.

For each event give:
- record: the id;
- quote: verbatim contiguous text from that one record (one message, one email, one note, or one speaker's turn in a meeting), same language, no ellipsis, no paraphrase, at most 240 characters: the sentence(s) that carry the point;
- ts: the record's time; for a meeting, the meeting date with the turn's clock time (YYYY-MM-DD HH:MM:SS);
- speaker: exactly as written in the record (for an email, the sender; meeting labels such as "SFO-5-Emery" or "Speaker 2" stay as written);
- kind: request, commitment, assignment, waiting, update, done or cancelled;
- what: one short English sentence naming the matter concretely (who, what, which campaign, partner, link or ticket), so the same matter can be recognised in other periods; for a short reply such as "done", say what was done when the conversation shows it;
- next: for request, commitment, assignment and waiting, the person or party expected to act, exactly as written; otherwise "".

Output one JSON object and nothing else:
{{"events": [{{"record": "R12", "ts": str, "speaker": str, "kind": str, "what": str, "next": str, "quote": str}}]}}"""


# ───────────────────────── 原料 → 文字 ─────────────────────────
def fingerprint(r):
    key = "|".join([r["kind"], r["ts"], r.get("who") or r.get("title") or "", r["text"][:300]])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def render(r):
    stamp = r['ts'].replace('T', ' ').rstrip('Z')
    if r["kind"] == "slack":
        return f"[{stamp}] {r['channel']} | {r['who']}: {r['text']}"
    if r["kind"] == "email":
        return f"[{stamp}] EMAIL «{r['subject']}» | {r['who']} → {', '.join(r.get('to') or []) or '-'}: {r['text']}"
    if r["kind"] == "hubspot":
        if r.get("who"):
            return f"[{stamp}] HUBSPOT ticket #{r['ticket']} «{r['title']}» | note by {r['who']}: {r['text']}"
        return (f"[{stamp}] HUBSPOT ticket #{r['ticket']} «{r['title']}» created (owner {r.get('owner') or '-'}, "
                f"status {r.get('status') or '-'}, priority {r.get('priority') or '-'}): {r['text']}")
    head = f"\n=== MEETING | {r['ts'][:10]} | {r['title']}{' | excerpt only' if r.get('partial') else ''} | clock times below are the meeting's own ===\n"
    todos = f"\n--- Fireflies to-dos (automatic extraction, reference only) ---\n{r['todos']}\n" if r.get("todos") else "\n"
    return f"{head}{r['text']}{todos}=== END MEETING ===\n"


def chunks(records, limit=CHUNK_CHARS):
    """按时间顺序把记录切成块；一条记录不拆开。"""
    out, cur, size = [], [], 0
    for r in records:
        n = len(render(r))
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


# ───────────────────────── 原话核对 ─────────────────────────
_PREFIX = re.compile(r"(?m)^\*\*[^*\n]+\*\* \(\d\d:\d\d:\d\d\): ")  # 会议逐字稿每行开头的「发言人 (时间): 」


def plain(s):
    s = s.replace("**", "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return s.replace("–", "-").replace("—", "-").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def norm(s):
    return re.sub(r"\s+", " ", plain(s)).strip().lower()


def _tokens(s):
    return re.findall(r"[a-z0-9']+|[\u4e00-\u9fff]", norm(s))


def _trigrams(toks):
    return {tuple(toks[i:i + 3]) for i in range(len(toks) - 2)}


def build_docs(records):
    docs = []
    for r in records:
        text = _PREFIX.sub("", r["text"]) if r["kind"] == "meeting" else r["text"]
        if r.get("todos"):
            text += "\n" + r["todos"]
        # 专用来源：广告主专用频道的消息，或标题就带广告主/直接点到本活动的会议
        dedicated = (r["kind"] == "slack" and r.get("why") == "channel") or (r["kind"] == "meeting" and r.get("tier") in ("title", "direct")) \
            or (r["kind"] in ("email", "hubspot") and r.get("tier") in ("direct", "strong"))
        docs.append(dict(text=plain(text), norm=norm(text), tri=_trigrams(_tokens(text)), dedicated=dedicated, record=r))
    return docs


def locate(quote, docs):
    """→ (结果, 出处下标列表)。exact：逐字（忽略大小写/空白/跨行）｜near：几乎一致（标点或断句被理顺）｜missing：查无"""
    parts = [p for p in (norm(x) for x in re.split(r"\.\.\.|…", quote or "")) if len(p) >= 6]
    if not parts:
        return "missing", []
    if all(any(p in d["norm"] for d in docs) for p in parts):
        return "exact", [i for i, d in enumerate(docs) if any(p in d["norm"] for p in parts)]
    tri = _trigrams(_tokens(quote))
    if tri:
        scores = [len(tri & d["tri"]) / len(tri) for d in docs]
        if scores and max(scores) >= 0.85:
            return "near", [i for i, sc in enumerate(scores) if sc >= 0.85]
    return "missing", []


def original(quote, docs, where):
    """近似命中时换成出处里那段真实原文：AI 偶尔会改一两个词、补个称呼，页面上要显示原话。"""
    q, best = plain(quote).lower(), None
    for i in where:
        raw = docs[i]["text"]
        low = raw.lower()
        m = difflib.SequenceMatcher(None, q, low, autojunk=False).find_longest_match(0, len(q), 0, len(low))
        lo, hi = max(0, m.b - m.a - 40), min(len(low), m.b + len(q) - m.a + 40)
        blocks = [b for b in difflib.SequenceMatcher(None, q, low[lo:hi], autojunk=False).get_matching_blocks() if b.size >= 4]
        if not blocks:
            continue
        # 从最长的一段向两边连，中间空太多（比如跳过邮件头）就断开
        k = max(range(len(blocks)), key=lambda j: blocks[j].size)
        a = b = k
        while a > 0 and blocks[a].b - blocks[a - 1].b - blocks[a - 1].size <= 40:
            a -= 1
        while b < len(blocks) - 1 and blocks[b + 1].b - blocks[b].b - blocks[b].size <= 40:
            b += 1
        score = sum(x.size for x in blocks[a:b + 1])
        if best is None or score > best[0]:
            best = (score, raw, lo + blocks[a].b, lo + blocks[b].b + blocks[b].size)
    if not best:
        return quote
    _, raw, start, end = best
    while start > 0 and raw[start - 1].isalnum():
        start -= 1
    while end < len(raw) and raw[end].isalnum():
        end += 1
    return re.sub(r"\s+", " ", raw[start:end]).strip()


def checked(e, docs, matcher=None):
    """→ 核对过的证据（近似的换成原文），查无则 None。"""
    chk, where = locate(e.get("quote"), docs)
    if chk == "missing":
        return None, where
    quote = original(e["quote"], docs, where) if chk == "near" else e["quote"]
    if matcher and matcher.t.get('attribution_version'):
        where = [i for i in where if matcher.quote_allowed(docs[i].get('record') or {}, quote)]
        if not where:
            return None, []
    return dict(ts=str(e.get("ts") or ""), speaker=str(e.get("speaker") or ""), quote=quote, check=chk), where


def check_quote(quote, docs):
    return locate(quote, docs)[0]


def _date_of(ts):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(ts or ""))
    return m.group(1) if m else None


def clean_items(items, docs, today, matcher=None, log=print):
    """核对原话、相关性闸门、收紧负责人、算久未更新。返回通过的事项。"""
    out, dropped, off_topic = [], 0, 0
    for it in items:
        ev, relevant = [], matcher is None
        for e in it.get("evidence") or []:
            got, where = checked(e, docs, matcher)
            if got:
                ev.append(got)
                if matcher and (matcher.t.get('attribution_version') or matcher.advertiser(e['quote']) or matcher.strong(e['quote']) or any(docs[i]['dedicated'] for i in where)):
                    relevant = True
        if not ev or not it.get("topic"):
            dropped += 1
            continue
        if not relevant:  # 只和合作方/库存方有关、没有任何一句点到广告主：不是本客户的事
            off_topic += 1
            continue
        basis = it.get("owner_basis") if it.get("owner_basis") in BASIS else "none"
        owner = it.get("next_owner") or "待确认"
        if basis == "none":
            owner = "待确认"
        dates = [d for d in (_date_of(e["ts"]) for e in ev) if d]
        last = max(dates) if dates else None
        age = (today - dt.date.fromisoformat(last)).days if last else None
        status = it.get("status") if it.get("status") in STATUSES else "in_progress"
        out.append(dict(
            id=it.get("id"), topic=it["topic"], topic_en=it.get("topic_en") or "", scope=it.get("scope") if it.get("scope") in SCOPES else "unclear",
            status=status, state_note=it.get("state_note") or "", state_note_en=it.get("state_note_en") or "",
            next_step=it.get("next_step") or "", next_step_en=it.get("next_step_en") or "",
            next_owner=owner, owner_basis=basis, evidence=ev, confidence=it.get("confidence") or "low",
            last_activity=last, age_days=age, stale=bool(age is not None and age > STALE_DAYS)))
    if dropped:
        log(f"  有 {dropped} 个事项因为引用的原话查无而被丢弃")
    if off_topic:
        log(f"  有 {off_topic} 个事项没有任何一句点到广告主，按「不是本客户的事」丢弃")
    return out


RESCOPE_SYSTEM = """You classify open work items of the advertiser "{advertiser}" by which campaign they belong to.
This page is for ONE campaign: "{campaign}". Its campaign name says its platform / partner and market (keywords: {keywords}).
The same advertiser usually runs several campaigns at the same time on different platforms (for example one on LG, one on Vizio, one on Samsung). Work about another of those campaigns belongs on that campaign's page, not here.
The advertiser's other running campaigns: {others}.

For each item choose one scope:
- "this_campaign": about this campaign, including the advertiser's work on this campaign's own platform / partner.
- "same_client": about another campaign of the advertiser, or another platform / partner than this campaign's, even when it is a big account matter (for example a creative test or a budget on another platform).
- "client_general": an account-level matter that is not tied to any one platform or campaign (overall budget, billing, attribution set-up, legal, reporting across all campaigns).
- "unclear": the records do not say.
Judge only from the item text and its quotes.

Output one JSON object and nothing else: {{"scopes": [{{"id": str, "scope": "this_campaign|same_client|client_general|unclear"}}]}}"""


def other_campaigns(terms):
    """同一广告主的其他在跑活动名（取自活动清单），给归属判断参考：比如 Vizio 活动之外还有专门的 Vizio T-Mobile 定向活动。"""
    rows = (read_json(list_file()) or {}).get("campaigns") or []
    adv = (terms.get("advertiser") or "").casefold()
    return [r["name"] for r in rows if adv and str(r.get("name", "")).casefold().startswith(adv) and r.get("name") != terms["name"]]


def rescope(items, terms, ask):
    """整理完以后单独判一次每个事项属于哪个活动（一次调用）。→ 用量。整理时顺带标的归属不可靠：会把别的平台的事标成本活动或客户层面。"""
    if not items:
        return None
    system = RESCOPE_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"],
                                   keywords=", ".join(terms["context"] + terms["links"]) or "-", others=", ".join(other_campaigns(terms)) or "-")
    system += attribution_context.prompt_context(terms)
    rows = [dict(id=x["id"], topic=x.get("topic_en") or x["topic"], note=x.get("state_note_en") or x.get("state_note") or "",
                 quotes=[e["quote"] for e in x.get("evidence") or []]) for x in items]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(rows, ensure_ascii=False)}]
    text, usage = ask(messages)
    try:
        got = {r.get("id"): r.get("scope") for r in parse_json(text).get("scopes") or []}
    except ValueError:
        return usage  # 判不出来就保留原来的归属
    for x in items:
        if got.get(x["id"]) in SCOPES:
            x["scope"] = got[x["id"]]
    return usage


# ───────────────────────── 调模型 ─────────────────────────
def load_ai():
    p = SECRETS / "ai.env"
    if not p.exists():
        raise RuntimeError("没有配置 AI：缺少 secrets/ai.env（AI_BASE_URL / AI_MODEL / AI_API_KEY）")
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("AI_BASE_URL", "AI_MODEL", "AI_API_KEY"):
        if not env.get(k):
            raise RuntimeError(f"secrets/ai.env 缺少 {k}")
    return env


def configured():
    """secrets/ai.env 是否存在（没配就不做 AI 这一步，也不算失败）。"""
    return (SECRETS / "ai.env").exists()


def chat(messages, timeout=1500, model=None, plain=False):
    """调网关；返回 (文字, 用量)。密钥不打印、错误信息里不带请求头。
    model：换一个模型（外包队列的自家处理方用）；plain=True：不带思考深度、关思考这些设置，按模型默认。"""
    env = load_ai()
    payload = {"model": model or env["AI_MODEL"], "messages": messages, "temperature": 1.0, "max_tokens": MAX_OUTPUT_TOKENS}
    effort = None if plain else (os.environ.get("AI_REASONING_EFFORT") or env.get("AI_REASONING_EFFORT"))
    if effort:
        if effort not in REASONING_EFFORTS:
            raise RuntimeError(f"AI_REASONING_EFFORT 只能是 {' / '.join(REASONING_EFFORTS)}，现在是 {effort}")
        payload["reasoning_effort"] = effort
    thinking = None if plain else (os.environ.get("AI_THINKING") or env.get("AI_THINKING"))
    if thinking:
        if thinking not in THINKING_MODES:
            raise RuntimeError(f"AI_THINKING 只能是 {' / '.join(THINKING_MODES)}，现在是 {thinking}")
        payload["thinking"] = {"type": thinking}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(env["AI_BASE_URL"].rstrip("/") + "/v1/chat/completions", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + env["AI_API_KEY"], "Content-Type": "application/json"})
    last = None
    for attempt, wait in enumerate((0, 10, 30, 90, 240), 1):  # 网关偶尔 502/超时：逐步拉长间隔重试，最多 5 次
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("模型输出被截断（思考用光了输出额度）")
            return choice["message"].get("content") or "", data.get("usage") or {}
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError, ValueError, KeyError) as e:
            last = e
    raise RuntimeError(f"AI 调用失败（已重试 5 次）：{type(last).__name__}: {str(last)[:160]}")


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


FIX_JSON = ("Your previous reply is not valid JSON ({error}). The problem is around: «{near}». "
            "Reply again with the complete corrected JSON object only: same content, nothing else.")


def ask_json(messages, ask):
    """问模型要 JSON。格式坏了不从头重问：把它写的原文和解析报错一起发回去，让它自己改（最多改 JSON_ATTEMPTS - 1 轮）。
    每轮只带上一次的原文，不把每一轮都堆进去。→ (解析好的对象, 最后一次的用量)"""
    convo = list(messages)
    for attempt in range(1, JSON_ATTEMPTS + 1):
        text, usage = ask(convo)
        try:
            return parse_json(text), usage
        except ValueError as e:
            if attempt == JSON_ATTEMPTS:
                raise RuntimeError(f"模型返回的不是合法 JSON（已让它自己改了 {JSON_ATTEMPTS - 1} 轮）")
            if isinstance(e, json.JSONDecodeError):
                error, near = f"{e.msg} at char {e.pos}", e.doc[max(0, e.pos - 120):e.pos + 60]
            else:
                error, near = "no JSON object found", text[-180:]
            convo = list(messages) + [{"role": "assistant", "content": text},
                                      {"role": "user", "content": FIX_JSON.format(error=error, near=near)}]


def _system(terms):
    return SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"], keywords=", ".join(terms["context"] + terms["links"]) or "-") + attribution_context.prompt_context(terms)


def update_once(items, recs, as_of, terms, ask):
    """给模型「当前清单 + 一块新记录」，返回 (新清单原始 items, closed, 用量)。ask(messages)→(文字, 用量)。"""
    user = (f"As-of time: {as_of.replace('T', ' ').rstrip('Z')} (UTC)\n\nCurrent open items (JSON):\n"
            f"{json.dumps(items, ensure_ascii=False)}\n\nNew records (chronological):\n\n" + "\n".join(render(r) for r in recs))
    doc, usage = ask_json([{"role": "system", "content": _system(terms)}, {"role": "user", "content": user}], ask)
    return doc.get("items") or [], doc.get("closed") or [], usage


# ───────────────────────── 好几块：各块同时摘要，再一次合成 ─────────────────────────
def _where(r):
    if r["kind"] == "slack":
        return f"Slack {r['channel']}"
    if r["kind"] == "email":
        return f"EMAIL «{r['subject']}»"
    if r["kind"] == "hubspot":
        return f"HUBSPOT ticket #{r['ticket']} «{r['title']}»"
    return f"MEETING «{r['title']}»"


def _with_id(r, rid):
    text = render(r)
    return text.replace("=== MEETING |", f"=== MEETING [{rid}] |", 1) if r["kind"] == "meeting" else f"[{rid}] {text}"


def digest_once(recs, terms, ask):
    """一块记录 → 模型摘出的事（未核对）、用量、编号 → 记录。"""
    ids = {f"R{i}": r for i, r in enumerate(recs, 1)}
    system = DIGEST_SYSTEM.format(advertiser=terms["advertiser"], campaign=terms["name"],
                                  keywords=", ".join(terms["context"] + terms["links"]) or "-") + attribution_context.prompt_context(terms)
    user = "Records (chronological):\n\n" + "\n".join(_with_id(r, k) for k, r in ids.items())
    doc, usage = ask_json([{"role": "system", "content": system}, {"role": "user", "content": user}], ask)
    return doc.get("events") or [], usage, ids


def digest(blocks, doc_of, terms, ask, runs, log=print):
    """各块同时摘出「事」；每条原话在它那条记录里逐字核对（对不上的丢掉），时间和说话人能取记录的就取记录的。→ 按时间排好的事"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as pool:
        answers = list(scope.thread_map(pool, lambda recs: (recs,) + digest_once(recs, terms, ask), blocks))
    events, dropped = [], 0
    matcher = corpus_mod.Matcher(terms)
    for recs, raw, usage, ids in answers:
        kept = 0
        for ev in raw:
            r = ids.get(str(ev.get("record")))
            got = checked(ev, [doc_of(r)], matcher)[0] if r else None
            if not got:
                dropped += 1
                continue
            meeting = r["kind"] == "meeting"
            events.append(dict(ts=(iso(ev.get("ts")) or r["ts"]) if meeting else r["ts"],
                               speaker=got["speaker"] if meeting else (r.get("who") or got["speaker"]),
                               kind=ev.get("kind") if ev.get("kind") in DIGEST_KINDS else "update",
                               what=str(ev.get("what") or "")[:300], next=str(ev.get("next") or "")[:80],
                               quote=got["quote"], source=_where(r)))
            kept += 1
        runs.append(dict(at=now_z(), step="digest", records=len(recs), events=kept, usage=usage))
    events.sort(key=lambda e: e["ts"])
    log(f"  摘要：{len(blocks)} 块同时读，摘出 {len(events)} 件事" + (f"（原话对不上丢掉 {dropped} 条）" if dropped else ""))
    return events


def _event_line(e):
    nxt = f" → next: {e['next']}" if e["next"] else ""
    return f"[{e['ts'].replace('T', ' ').rstrip('Z')}] {e['source']} | {e['speaker']} ({e['kind']}{nxt}): {e['quote']}\n    = {e['what']}"


def event_groups(events, limit=COMBINE_CHARS):
    """摘出来的事按时间分组，每组不超过 limit 字（一般一组就够）。"""
    out, cur, size = [], [], 0
    for e in events:
        n = len(_event_line(e))
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        out.append(cur)
    return out


def update_from_events(items, events, as_of, terms, ask):
    """合成：给模型「当前清单 + 摘出来的事」，返回格式同 update_once。"""
    user = (f"As-of time: {as_of.replace('T', ' ').rstrip('Z')} (UTC)\n\nCurrent open items (JSON):\n{json.dumps(items, ensure_ascii=False)}\n\n"
            "New events (chronological). They were extracted from the raw records of this period: each line has the record's time, source, "
            "speaker, the kind of event and its verbatim quote; the line after \"=\" is the extractor's one-line summary, not a quote. "
            "Use them like records: evidence quotes must be copied from these quotes.\n\n" + "\n".join(_event_line(e) for e in events))
    doc, usage = ask_json([{"role": "system", "content": _system(terms)}, {"role": "user", "content": user}], ask)
    return doc.get("items") or [], doc.get("closed") or [], usage


# ───────────────────────── 总装 ─────────────────────────
def latest_as_of(runs):
    """数据截至哪天：所有批次里最晚的一个。补进来的旧记录不能把日期往回拉。"""
    return max((x["as_of"] for x in runs if x.get("as_of")), default=None)


def build(cid, log=print, ask=None, today=None):
    """读 corpus.json，顺着上次的 progress.json 往下更新。→ 摘要。"""
    cid = valid_number(cid)
    ask = ask or chat
    today = today or dt.datetime.now(dt.timezone.utc).date()
    doc = read_json(camp_dir(cid) / "corpus.json")
    if not doc:
        raise RuntimeError(f"活动 {cid} 还没有 AI 原料（corpus.json）")
    records = doc["records"]
    docs = build_docs(records)
    where = {id(r): i for i, r in enumerate(records)}
    matcher = corpus_mod.Matcher(doc["terms"])
    prev = read_json(camp_dir(cid) / "progress.json") or {}
    attribution_sig = json.dumps([doc.get('attribution_version'), doc['terms']], sort_keys=True) if doc.get('attribution_version') else None
    if attribution_sig and prev.get('attribution_sig') != attribution_sig:
        prev = {}  # Re-read under the new scope instead of carrying forward client-wide items.
    seen = set(prev.get("seen") or [])
    items = prev.get("items") or []
    closed = prev.get("closed") or []
    runs = prev.get("runs") or []
    todo = [r for r in records if fingerprint(r) not in seen]
    log(f"进行中事项：原料 {len(records)} 条，新增 {len(todo)} 条，现有事项 {len(items)} 个")
    blocks = chunks(todo)
    calls = 0
    if len(blocks) > 1:  # 好几块：先各块同时摘出「事」，再按时间一次合成（一块接一块要一块等一块，内容多了太慢）
        events = digest(blocks, lambda r: docs[where[id(r)]], doc["terms"], ask, runs, log=log)
        calls += len(blocks)
        groups = event_groups(events)
        steps = [(g[-1]["ts"], "events", g) for g in groups]
        if steps:
            steps[-1] = (todo[-1]["ts"], "events", groups[-1])  # 最后一组的截至时间 = 这批原料里最晚的一条
    else:
        steps = [(recs[-1]["ts"], "records", recs) for recs in blocks]
    for i, (as_of, what, payload) in enumerate(steps, 1):
        if what == "records":
            raw_items, raw_closed, usage = update_once(items, payload, as_of, doc["terms"], ask)
        else:
            raw_items, raw_closed, usage = update_from_events(items, payload, as_of, doc["terms"], ask)
        calls += 1
        before = {x["id"]: x for x in items if x.get("id")}
        items = clean_items(raw_items, docs, today, matcher=matcher, log=log)
        # 新事项的 id 由程序分配，保证稳定
        used = {x["id"] for x in items if x.get("id")} | {c.get("id") for c in closed}
        n = len(used) + 1
        for x in items:
            if not x.get("id"):
                while f"P{n:03d}" in used:
                    n += 1
                x["id"] = f"P{n:03d}"
                used.add(x["id"])
        for c in raw_closed:
            ev = [got for got in (checked(e, docs, matcher)[0] for e in c.get("evidence") or []) if got]
            was = before.get(c.get("id"))
            if c.get("id") and ev and was:
                closed.append(dict(id=c["id"], topic=was["topic"], topic_en=was.get("topic_en") or "", reason=c.get("reason") or "",
                                   reason_en=c.get("reason_en") or "", evidence=ev, closed_at=as_of))
        if what == "records":
            seen.update(fingerprint(r) for r in payload)
            runs.append(dict(at=now_z(), as_of=as_of, records=len(payload), items=len(items), usage=usage))
        else:
            runs.append(dict(at=now_z(), step="combine", as_of=as_of, events=len(payload), items=len(items), usage=usage))
        write_json(camp_dir(cid) / "progress.json", dict(
            built_at=now_z(), as_of=latest_as_of(runs), campaign=doc["campaign"], items=items, closed=closed, seen=sorted(seen), runs=runs,
            attribution_version=doc.get('attribution_version'), attribution_sig=attribution_sig))
        log(f"  第 {i}/{len(steps)} {'块' if what == 'records' else '组（合成）'}（到 {as_of[:10]}）：{len(payload)} 条 → 进行中 {len(items)} 个")
    if len(blocks) > 1:  # 摘要的那几块：全部合成完才算读过（中途出错，下次整批重来）
        seen.update(fingerprint(r) for r in todo)
    if blocks or prev.get("scope_version") != SCOPE_VERSION:
        usage = rescope(items, doc["terms"], ask)
        if usage is not None:
            runs.append(dict(at=now_z(), step="rescope", items=len(items), usage=usage))
    # 以前存下的近似原话也换成原文
    for x in items + closed:
        x["evidence"] = [got for got in (checked(e, docs, matcher)[0] for e in x.get('evidence') or []) if got]
    # 没有新增也要按今天重算「久未更新」
    for x in items:
        if x.get("last_activity"):
            x["age_days"] = (today - dt.date.fromisoformat(x["last_activity"])).days
            x["stale"] = x["age_days"] > STALE_DAYS
    write_json(camp_dir(cid) / "progress.json", dict(
        built_at=now_z(), as_of=latest_as_of(runs), campaign=doc["campaign"], items=items, closed=closed,
        seen=sorted(seen), runs=runs, scope_version=SCOPE_VERSION,
        attribution_version=doc.get('attribution_version'), attribution_sig=attribution_sig))
    return dict(items=len(items), closed=len(closed), new_records=len(todo), calls=calls)
