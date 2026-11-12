"""AI 原料：把一个活动的全部沟通（Slack 频道 + 会议逐字稿 + 邮件 + HubSpot 工单）整理成一份带时间的记录。

只读 raw/ 里已取到的数据，会议页另外经大脑（只读）按窗口拉取并缓存。不做任何写操作，不判断「这条算不算本活动」
——只负责「别漏」，并标明每条为什么被收进来（命中了哪些词），归属交给后面的 AI 步骤去判断。

来源和规则：
- Slack：①正式流程已读到的频道（按点击链接编号/GitHub 单号命中，见 raw/links/<链接>/brain.json）；
  ②窗口内全部 Slack 日页镜像到 raw/slack（按大脑 slack 标签的页面清单，只拉清单里有的，没更新的不重拉），
  不靠猜频道、不靠搜索；频道名带广告主名或特征词的只是标注来源。
  频道名里带广告主名字的是「专用频道」，消息全留；其余是「共用频道」，只留命中本活动词的消息和它前后各一条，
  且机器人/自动播报的消息必须命中活动全名、链接编号或单号（只提到广告主名不算）。
- 会议：大脑里窗口内的全部会议页（全局缓存 raw/meetings，所有活动共用），逐场按内容里是否出现本活动的词筛（宁多勿漏，归属交给后面的 AI）：
    direct   出现活动全名 / 点击链接编号 / 关联 GitHub 单号
    likely   广告主名出现过（1 次就算），且出现活动名里的特征词（合作方、库存方等）
    mention  广告主名出现过
    title    标题里带广告主名
  广告主名一次都没提的不收：只提平台不提客户的会（如 Binance 的会上谈 TCL）不是这个客户的事。广告主名当普通英文词用的
  （chime in = 插话）不算提到。direct 或标题带广告主名的留整场；其余只留锚点前后几行，并标 partial。锚点：点到活动/链接/单号、
  广告主名、特征词，以及特征词被听写走了样的写法（free well ← Freewheel、Kaizen ← Kayzen），这样提完客户接着谈平台的部分也带上。
  （2026-10-28 前要求广告主名出现 3 次、锚点只认广告主名：5120 的 371 场会只收了 91 场，只提一两次客户的相关会议整场漏掉。）
- 会议页里「Fireflies 抽的待办」单独放在 todos（谁要做什么，没有完成状态，仅供参考）；自动摘要不放（避免给 AI 递答案）。
- 会议室标签（如 SFO-5-Emery）、Speaker N 都原样保留，不在这里映射真人。
- 邮件、HubSpot 工单：大脑里全部镜像到全局缓存，按 replay/brainpages.py 的相关性档位筛（direct / strong / loose）。
- 2026-10-31 起 REPLAY_SOURCE=direct 时，以上来源改读 replay/feeds.py 的直连副本（FeedAI / Fireflies / Femini），
  不再经过大脑；页面格式和筛选规则都不变。Teams 会议不再收。
"""
import datetime as dt
import re
from collections import Counter

import slack_source

from . import brainpages, attribution_context
from .attribution import ATTRIBUTION_VERSION
from . import feeds, source_status
from . import util
from . import collect_mcp
from .collect_mcp import _page, channel_name, mcp_client
from .util import camp_dir, iso, issue_re, now_z, num_re, raw_dir, read_json, safe_name, valid_number, write_json

CONTEXT_LINES = 12          # 非整场收录时，命中位置前后各取几行
LIST_PAGE = 100             # 大脑 list_pages 单次上限
MEETING_SLACK_DAYS = 2      # 窗口起点往前多看几天
MIN_MENTIONS = 1            # 会议正文里广告主名出现几次就收（只截提到的段落；标题带广告主名不受限）
# 广告主名同时是常用英文词：当普通词用的说法不算提到客户
IDIOMS = {"chime": re.compile(r"(?<![A-Za-z0-9])chime\s+in(?:to)?(?![A-Za-z0-9])", re.I)}   # chime in = 插话
_LETTERS = re.compile(r"[A-Za-z]+")
CHANNEL_LIST_AGE = 12 * 3600
# 自动播报/机器人的发言人（大小写不敏感）；「Unknown」是大脑副本里没有账号名的发言
BOT_RE = re.compile(r"unknown|bot\b|\[bot\]|messenger|sentry|circle|alert|monitor|notif|digest|zapier|workflow", re.I)

# 活动名里不能当「特征词」的常见成分：国家、系统、计费方式、素材/购买形式
# 不易和英文单词混淆的国家代码：算活动特征词（只匹配大写，见 Matcher），因为活动常按地区区分（Binance_..._VN_...）。
# US、IN、IT、ID、DE、ES、CA、CO、AR 等容易和普通英文混在一起，仍然当通用词排除。
GEO_OK = {"VN", "BR", "TH", "BD", "MX", "KR", "JP", "SG", "PH", "VE", "CR", "PK", "NG", "KE", "EG", "TW", "HK", "UK", "AU", "PE", "CL", "MY"}
_STOP = {w.lower() for w in (
    "CTV OTT US UK CA MX BR DE FR ES IT JP KR IN AU ID TH VN PH SG MY TR SA AE NL PL CO AR CL PE "
    "CPM CPI CPA CPC CPE CPV CPL ROAS iOS Android Video Display Banner Native Audio PMP PG Open Auction "
    "Retargeting Prospecting UA Test New Campaign"
).split()} - {g.lower() for g in GEO_OK}
_SPLIT = re.compile(r"[\s_\-/|–—:]+")


# ───────────────────────── 活动词表 ─────────────────────────
def _within(a, b, limit):
    """a 改成 b 最多要动几个字母（增、删、换）不超过 limit；超过就提前停。"""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _word(term):
    # 地区代码只匹配大写（VN 是越南，vn 可能是别的）；其余词不区分大小写（团队聊天里常写成小写的 lg）
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", 0 if term in GEO_OK else re.I)


def build_terms(camp, links, partners, issues):
    """camp：后台活动信息；links：点击链接编号；partners：合作方名字；issues：GitHub 单号。"""
    name = camp.get("name") or ""
    app = ((camp.get("mobile_app") or {}).get("name") or "").strip()
    adv = _SPLIT.split(app)[0] if app else ""
    if len(adv) < 3:
        adv = (_SPLIT.split(name)[0] if name else "")
    context = []
    for tok in _SPLIT.split(name):
        low = tok.lower()
        if len(tok) >= 2 and low not in _STOP and low != adv.lower() and not tok.isdigit() and tok not in context:
            context.append(tok)
    for p in partners:
        for tok in _SPLIT.split(p or ""):
            if len(tok) >= 3 and tok.lower() not in _STOP and tok not in context and tok.lower() != adv.lower():
                context.append(tok)
    return dict(name=name, advertiser=adv, context=context, links=[str(x) for x in links],
                issues=[str(x) for x in issues])


class Matcher:
    def __init__(self, terms):
        self.t = terms
        self.name = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(terms['name']) + r'(?![A-Za-z0-9_])', re.I) if terms['name'] else None
        self.nums = {n: num_re(n) for n in terms["links"]}
        self.issues = {i: issue_re(i) for i in terms["issues"]}
        self.adv = _word(terms["advertiser"]) if len(terms["advertiser"]) >= 3 else None
        self.idiom = IDIOMS.get(terms["advertiser"].lower())
        self.ctx = {c: _word(c) for c in terms["context"]}
        # 听写走样只对够长的特征词认（短词差一个字母就成了别的词）：6~7 个字母差 1 个、8 个以上差 2 个
        self.fuzzy = [(c, c.lower(), 1 if len(c) < 8 else 2) for c in terms["context"] if len(c) >= 6 and c.isalpha()]

    def strong(self, text):
        hits = []
        if self.name and self.name.search(text):
            hits.append("name")
        hits += [f"link:{n}" for n, p in self.nums.items() if p.search(text)]
        hits += [f"issue:{i}" for i, p in self.issues.items() if p.search(text)]
        return hits

    def assess(self, record, text=None):
        return attribution_context.assess(record, self.t, text)

    def quote_allowed(self, record, text):
        return attribution_context.quote_allowed(record, text, self.t)

    def advertiser(self, text):
        n = len(self.adv.findall(text)) if self.adv else 0
        return max(0, n - len(self.idiom.findall(text))) if n and self.idiom else n

    def context(self, text):
        return [c for c, p in self.ctx.items() if p.search(text)]

    def any_hit(self, text):
        return bool(self.strong(text)) or bool(self.advertiser(text))

    def sounds_like(self, text):
        """特征词被听写走了样的写法（free well ← Freewheel）：单个词或相邻两个词连起来，和特征词只差一两个字母。"""
        if not self.fuzzy:
            return []
        words = [w.lower() for w in _LETTERS.findall(text)]
        grams = set(words) | {a + b for a, b in zip(words, words[1:])}
        return [c for c, low, limit in self.fuzzy if any(g[0] == low[0] and _within(g, low, limit) for g in grams)]

    def anchor(self, line):
        """会议里截片段的锚点：点到活动/链接/单号、广告主名、特征词，或特征词的听写走样。"""
        return bool(self.strong(line) or self.advertiser(line) or self.context(line) or self.sounds_like(line))

    def tier(self, text):
        s, a, c = self.strong(text), self.advertiser(text), self.context(text)
        if s:
            return "direct", dict(strong=s, advertiser=a, context=c)
        if a >= MIN_MENTIONS and c:
            return "likely", dict(strong=[], advertiser=a, context=c)
        if a >= MIN_MENTIONS:
            return "mention", dict(strong=[], advertiser=a, context=[])
        return None, None


# ───────────────────────── 会议：全局缓存 ─────────────────────────
def meeting_cache_dir():
    """会议缓存：直连模式是 replay/feeds.py 拉来的副本，否则是从大脑拉来的缓存（格式一样）。"""
    return feeds.kind_dir("meetings") if feeds.enabled() else util.RAW / "meetings"


_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def slug_date(slug):
    m = _DATE.search(slug.split("/", 1)[-1])
    return m.group(1) if m else None


def list_meetings(brain):
    """大脑里全部会议页的清单（slug、标题、更新时间），翻页取完。"""
    out, off = [], 0
    while True:
        rows = brain.call("list_pages", {"type": "meeting", "limit": LIST_PAGE, "offset": off})
        if not isinstance(rows, list) or not rows:
            break
        out += rows
        off += LIST_PAGE
        if len(rows) < LIST_PAGE:
            break
    return out


def sync_meetings(start, end, log=print, brain=None):
    """把日期在 [start, end] 的会议页拉到全局缓存（已缓存且没更新的不重拉）。返回清单与统计。"""
    b = brain or mcp_client.brain()
    rows = list_meetings(b)
    want = [r for r in rows if (slug_date(r["slug"]) or "") >= start.isoformat()
            and (slug_date(r["slug"]) or "") <= end.isoformat()]
    fetched = failed = 0
    for r in want:
        path = meeting_cache_dir() / (safe_name(r["slug"]) + ".json")
        have = read_json(path)
        if have and have.get("updated_at") == r.get("updated_at"):
            continue
        try:
            pg = _page(b, r["slug"])
        except Exception as e:  # 单页取不到不拖垮整体
            failed += 1
            log(f"  会议页 {r['slug']} 取不到（忽略）：{str(e)[:100]}")
            continue
        fm = pg.get("frontmatter") if isinstance(pg.get("frontmatter"), dict) else {}
        write_json(path, dict(queried_at=now_z(), slug=r["slug"], title=pg.get("title") or r.get("title"),
                              updated_at=pg.get("updated_at") or r.get("updated_at"),
                              source_id=pg.get("source_id"), datetime=fm.get("datetime") or fm.get("date"),
                              host=fm.get("host"), compiled_truth=pg.get("compiled_truth") or ""))
        fetched += 1
    log(f"会议：大脑共 {len(rows)} 场，窗口内 {len(want)} 场，新拉 {fetched} 场" + (f"，失败 {failed}" if failed else ""))
    return want, dict(listed=len(rows), in_window=len(want), fetched=fetched, failed=failed)


# ───────────────────────── 会议：切分 ─────────────────────────
_TODO = re.compile(r"(?ms)^### Fireflies 抽的待办.*?\n(.*?)(?=^#{1,3} |^---\s*$|\Z)")
_TRANSCRIPT = re.compile(r"(?m)^## 全文转录.*$")


def split_meeting(compiled_truth):
    """→ (逐字稿正文, 待办文字)。没有「全文转录」章节就把整页当正文。"""
    todos = ""
    m = _TODO.search(compiled_truth)
    if m:
        todos = m.group(1).strip()
    t = _TRANSCRIPT.search(compiled_truth)
    body = compiled_truth[t.end():].strip() if t else compiled_truth
    return body, todos


def _windows(lines, hit_idx, n):
    spans = []
    for i in sorted(hit_idx):
        lo, hi = max(0, i - n), min(len(lines), i + n + 1)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return spans


def excerpt(body, matcher, n=CONTEXT_LINES):
    lines = body.split("\n")
    idx = [i for i, ln in enumerate(lines) if matcher.anchor(ln)]
    if not idx:
        return ""
    parts = ["\n".join(lines[lo:hi]).strip() for lo, hi in _windows(lines, idx, n)]
    return "\n[…]\n".join(parts)


def meeting_record(cached, matcher):
    """一场会议 → 原料记录；与本活动无关返回 None。"""
    body, todos = split_meeting(cached["compiled_truth"])
    title_hit = bool(matcher.adv and matcher.adv.search(cached["title"] or ""))
    tier, hits = matcher.tier(body)
    if not tier and title_hit:
        tier, hits = "title", dict(strong=[], advertiser=matcher.advertiser(body), context=matcher.context(body))
    if not tier:
        return None
    whole = tier == "direct" or title_hit
    text = body if whole else excerpt(body, matcher)
    when = iso(cached.get("datetime")) or (slug_date(cached["slug"]) + "T12:00:00Z")
    return dict(kind="meeting", ts=when, title=cached["title"], slug=cached["slug"], source_id=cached.get("source_id"),
                tier=tier, hits=hits, partial=not whole, text=text,
                todos=todos if (whole or matcher.any_hit(todos)) else "")


# ───────────────────────── Slack ─────────────────────────
def slack_records(channels, matcher, since, raw=None):  # noqa: C901
    """本活动的频道 → 消息记录。专用频道全留，共用频道只留命中词的消息和前后各一条。"""
    raw = raw or (feeds.slack_root() if feeds.enabled() else util.RAW / "slack")
    msgs = [m for m in slack_source.load(raw=raw) if m["channel"] in set(channels) and m["ts"][:10] >= since]
    adv = matcher.t["advertiser"].lower()
    by_ch = {}
    for m in msgs:
        by_ch.setdefault(m["channel"], []).append(m)
    out = []
    for ch, rows in by_ch.items():
        label = "#" + channel_name(ch)
        dedicated = bool(adv) and adv in channel_name(ch).lower()
        keep = {}
        for i, m in enumerate(rows):
            if dedicated:
                keep[i] = "channel"
            elif matcher.strong(m["text"]) or (matcher.advertiser(m["text"]) and not BOT_RE.search(m["user"] or "")):
                keep[i] = "match"
                for j in (i - 1, i + 1):
                    if 0 <= j < len(rows):
                        keep.setdefault(j, "context")
        for i in sorted(keep):
            m = rows[i]
            out.append(dict(kind="slack", ts=m["ts"], channel=label, channel_dir=ch, who=m["user"], text=m["text"],
                            why=keep[i], dedicated=dedicated,
                            hits=dict(strong=matcher.strong(m["text"]), advertiser=matcher.advertiser(m["text"]))))
    return out


# ───────────────────────── Slack：窗口内全量镜像 ─────────────────────────
def slack_index(brain):
    """大脑里全部 Slack 日页的清单 → {频道目录: {日期: 页面更新时间}}。全量列页约 36 秒，结果缓存半天。"""
    path = util.RAW / "slack_index.json"
    have = read_json(path)
    if have and have.get("queried_at") and have.get("index"):
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(have["queried_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        if age.total_seconds() < CHANNEL_LIST_AGE:
            return have["index"]
    index, off = {}, 0
    while True:
        rows = brain.call("list_pages", {"tag": "slack", "limit": LIST_PAGE, "offset": off})
        if not isinstance(rows, list) or not rows:
            break
        for x in rows:
            p = x["slug"].split("/")
            if len(p) == 3 and p[0] == "channels":
                index.setdefault(p[1], {})[p[2]] = x.get("updated_at")
        off += LIST_PAGE
        if len(rows) < LIST_PAGE:
            break
    if index:
        write_json(path, dict(queried_at=now_z(), index=index))
    return index


def sync_slack_window(brain, index, start, end, log=print):
    """把窗口 [start, end] 内清单里有的 Slack 日页全部缓存到 raw/slack（页面没更新的不重拉）。→ 新拉页数"""
    cache = collect_mcp.SLACK_CACHE
    lo, hi = start.isoformat(), end.isoformat()
    fetched = failed = total = 0
    for d, days in index.items():
        for day, upd in days.items():
            if not lo <= day <= hi:
                continue
            total += 1
            path = cache / d / f"{day}.json"
            have = read_json(path)
            if have and not have.get("missing") and (have.get("data") or {}).get("updated_at") == upd:
                continue
            rec = {"source": "brain", "tool": "get_page", "queried_at": now_z(),
                   "args": {"slug": f"channels/{d}/{day}", "include_content": True}}
            try:
                rec["data"] = _page(brain, f"channels/{d}/{day}")
                rec["missing"] = False
            except RuntimeError:
                rec["missing"] = True
                failed += 1
            write_json(path, rec)
            fetched += 1
    log(f"Slack 镜像：窗口内 {total} 个日页（{len(index)} 个频道有页面），新拉 {fetched} 页" + (f"，取不到 {failed}" if failed else ""))
    return fetched


def _name_tokens(dirname):
    return [t.lower() for t in re.split(r"[-_]+", channel_name(dirname)) if t]


def channel_reasons(dirs, matcher, known):
    """每个频道为什么被关注：流程命中 / 频道名含广告主 / 频道名含特征词 / 其余靠内容筛选。"""
    adv = matcher.t["advertiser"].lower()
    ctx = {c.lower() for c in matcher.t["context"]}
    out = {}
    for d in dirs:
        toks = _name_tokens(d)
        if d in known:
            out[d] = "流程命中"
        elif adv and (adv in toks or adv in channel_name(d).lower()):
            out[d] = "频道名含广告主"
        elif ctx & set(toks):
            out[d] = "频道名含特征词"
        else:
            out[d] = "内容筛选"
    return out


# ───────────────────────── 总装 ─────────────────────────
def _campaign_inputs(cid):
    info = read_json(camp_dir(cid) / "campaign.json") or {}
    camp = info.get("campaign") or {}
    links = info.get("links") or []
    partners, issues, channels, starts = [], [], [], []
    for n in links:
        fem = read_json(raw_dir(n) / "femini.json") or {}
        p = (((fem.get("click_url") or {}).get("partner")) or {}).get("name")
        if p and p not in partners:
            partners.append(p)
        gh = read_json(raw_dir(n) / "github.json") or {}
        issues += [str(x["number"]) for x in gh.get("selected", [])]
        br = read_json(raw_dir(n) / "brain.json") or {}
        channels += [c for c in br.get("channels", []) if c not in channels]
        w = (br.get("window") or [None])[0]
        if w:
            starts.append(w)
    return camp, links, partners, sorted(set(issues)), channels, (min(starts) if starts else None)


def build(cid, log=print, brain=None, today=None):
    """生成 raw/campaigns/<活动>/corpus.json。先决条件：这个活动的各条链接已经取过后台和大脑。"""
    cid = valid_number(cid)
    camp, links, partners, issues, channels, start = _campaign_inputs(cid)
    if not camp:
        raise RuntimeError(f"活动 {cid} 还没有取过活动信息")
    terms = attribution_context.enrich(cid, build_terms(camp, links, partners, issues))
    m = Matcher(terms)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    first = dt.date.fromisoformat(start) if start else today - dt.timedelta(days=90)
    first -= dt.timedelta(days=MEETING_SLACK_DAYS)

    direct = feeds.enabled()  # 直连：副本在流水线开头已经刷新到最新（replay/feeds.py），这里只读，不碰大脑
    source_scan = source_status.receipts(feeds.read_state()) if direct else {}
    b = None if direct else (brain or mcp_client.brain())
    index = feeds.slack_index() if direct else slack_index(b)
    if not direct:
        sync_slack_window(b, index, first, today, log=log)
    in_window = [d for d, days in index.items() if any(first.isoformat() <= x <= today.isoformat() for x in days)]
    found = channel_reasons(in_window, m, channels)
    want, mstat = feeds.meetings_in(first, today) if direct else sync_meetings(first, today, log=log, brain=b)
    meetings = []
    for r in want:
        cached = read_json(meeting_cache_dir() / (safe_name(r["slug"]) + ".json"))
        if cached:
            rec = meeting_record(cached, m)
            if rec:
                meetings.append(rec)
    slack = slack_records(list(found), m, first.isoformat())
    bstat = {}
    for kind in ("tickets", "emails"):
        if direct:
            bstat[kind] = dict(source="direct", as_of=feeds.as_of(kind))
            continue
        try:
            bstat[kind] = brainpages.sync(b, kind, log=log)
        except Exception as e:  # 大脑临时取不到：用已有缓存继续
            log(f"{kind} 同步失败，用已有缓存：{str(e)[:150]}")
            bstat[kind] = dict(error=str(e)[:150])
    lo, hi = first.isoformat(), today.isoformat()
    emails = brainpages.email_records(m, lo, hi)
    tickets = brainpages.ticket_records(m, lo, hi)

    candidates = sorted(slack + meetings + emails + tickets, key=lambda r: (r['ts'], r['kind']))
    records, background, excluded = attribution_context.partition(candidates, terms)
    records.sort(key=lambda r: (r['ts'], r['kind']))
    for i, r in enumerate(records, 1):
        r['id'] = f'R{i:03d}'
    for i, r in enumerate(background, 1):
        r['id'] = f'B{i:03d}'
    counts = dict(Counter(r['attribution']['level'] for r in records + background + excluded))
    slack = [r for r in records if r['kind'] == 'slack']
    meetings = [r for r in records if r['kind'] == 'meeting']
    emails = [r for r in records if r['kind'] == 'email']
    tickets = [r for r in records if r['kind'] == 'hubspot']
    stats = dict(channels=found, slack=len(slack), slack_channels=dict(Counter(r["channel"] for r in slack)),
                 meetings=len(meetings), meeting_tiers=dict(Counter(r["tier"] for r in meetings)),
                 meeting_scan=mstat, emails=len(emails), email_threads=len({r["thread"] for r in emails}),
                 email_tiers=dict(Counter(r["tier"] for r in emails)), tickets=len({r["ticket"] for r in tickets}),
                 ticket_records=len(tickets), ticket_tiers=dict(Counter(r["tier"] for r in tickets)), brain_scan=bstat,
                 source_scan=source_scan, chars=sum(len(r["text"]) for r in records))
    doc = dict(built_at=now_z(), source="direct" if direct else "brain",
               campaign=dict(id=int(cid), name=camp.get("name")), window=[first.isoformat(), today.isoformat()],
               terms=terms, stats=stats, records=records, client_background=background,
               excluded=excluded, attribution_summary=counts, attribution_version=ATTRIBUTION_VERSION,
               vendor_id=terms.get('vendor_id'))
    write_json(camp_dir(cid) / "corpus.json", doc)
    log(f"AI 原料：Slack {len(slack)} 条、会议 {len(meetings)} 场（{stats['meeting_tiers']}）、"
        f"邮件 {len(emails)} 封（{stats['email_threads']} 个线程）、HubSpot 工单 {stats['tickets']} 张（{len(tickets)} 条记录），共 {stats['chars']:,} 字符")
    return dict(slack=len(slack), meetings=len(meetings), emails=len(emails), tickets=stats["tickets"], chars=stats["chars"])
