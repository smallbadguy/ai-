"""只读取后台（Femini）和公司大脑（Brain）里和某个 click URL 编号有关的数据。

后台：get_click_url（含利润率调整记录、所属活动、渠道）、get_campaign。
大脑：
  1. 用几种写法搜这个编号（搜索会漏，也会混进无关结果，所以只当「线索」）；
  2. 把线索页面拿下来，页面里真的出现编号（或相关 GitHub 单号）才算相关；
  3. 相关的 HubSpot 工单（tickets/*）原样存档；
  4. 出现过相关聊天的频道，从案子最早的记录开始到今天，按天全部拉下来（带缓存），
     之后在整理阶段再逐条消息筛选。这样不会因为搜索排名而漏掉关键聊天。
只调用读取类工具（search、get_page、get_click_url、get_campaign），不做任何写操作。
需要 REPLAY_SECRETS 指向凭证目录。
"""
import datetime as dt
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "collect"))
import mcp_client  # noqa: E402

from . import feeds  # noqa: E402
from .github import issue_file  # noqa: E402
from .util import RAW, issue_re, iso, now_z, num_re, raw_dir, read_json, write_json  # noqa: E402

MAX_TICKETS = 20
MAX_HIT_PAGES = 80
MAX_CHANNELS = 8
SLACK_CACHE = RAW / "slack"


def channel_name(dirname):
    """'mighty-team-cezefg55j' → 'mighty-team'；没有尾巴的原样返回。"""
    m = re.match(r"^(.*)-[a-z0-9]{8,12}$", dirname)
    return m.group(1) if m else dirname


def _page(b, slug):
    return b.call("get_page", {"slug": slug, "include_content": True})


def sync_channel(b, d, start, today):
    """把某个频道从 start 到 today 的日页拉到全局缓存 raw/slack（已稳定的日子不重拉）。→ (新拉页数, 页面更新时间列表)"""
    fetched, synced = 0, []
    day = start
    while day <= today:
        path = SLACK_CACHE / d / f"{day.isoformat()}.json"
        rec = read_json(path)
        settled = rec and rec["queried_at"][:10] >= (day + dt.timedelta(days=2)).isoformat()
        if not settled:
            rec = {"source": "brain", "tool": "get_page", "queried_at": now_z(),
                   "args": {"slug": f"channels/{d}/{day.isoformat()}", "include_content": True}}
            try:
                rec["data"] = _page(b, f"channels/{d}/{day.isoformat()}")
                rec["missing"] = False
            except RuntimeError:
                rec["missing"] = True
            write_json(path, rec)
            fetched += 1
        if rec and not rec.get("missing"):
            u = iso(rec["data"].get("updated_at"))
            if u:
                synced.append(u)
        day += dt.timedelta(days=1)
    return fetched, synced


def collect_femini(n, log):
    f = mcp_client.femini()
    click = f.call("get_click_url", {"name": n})
    cu = click.get("click_url", click)
    camp = None
    try:
        camp = f.call("get_campaign", {"id": cu["campaign"]["id"]}).get("campaign")
    except Exception as e:  # 活动详情拿不到不影响主线
        log(f"  get_campaign 失败（忽略）：{str(e)[:120]}")
    doc = {"queried_at": now_z(), "click_url": cu, "campaign": camp}
    write_json(raw_dir(n) / "femini.json", doc)
    log(f"后台：{cu['name']} · {cu['campaign']['name']} · 渠道 {cu['partner']['name']} · 利润率等调整 {len(cu.get('proposed_changes', []))} 条")
    return doc


def _issue_numbers(n):
    man = read_json(raw_dir(n) / "github.json") or {}
    return [str(x["number"]) for x in man.get("selected", [])]


def collect_brain(n, femini_doc, log):
    b = mcp_client.brain()
    pat = num_re(n)
    issues = _issue_numbers(n)
    issue_pats = [issue_re(i) for i in issues]
    camp_name = femini_doc["click_url"]["campaign"]["name"]

    def relevant(text):
        return bool(pat.search(text)) or any(p.search(text) for p in issue_pats)

    # 1) 搜索只当线索
    # 单号用「裸数字」和「GH#单号」两种写法搜（实测 HubSpot 工单标题里写的是 GH#单号，裸数字最稳）；
    # 「GH#」偶尔会让大脑返回异常数据，失败就忽略，只当线索。
    queries = [n, f"click_urls/{n}", f"#{n} click URL {n}", camp_name] + issues + [f"GH#{i}" for i in issues]
    cand = {}
    for q in queries:
        try:
            for x in b.call("search", {"query": q, "limit": 100}):
                cand.setdefault(x["slug"], x)
        except Exception as e:
            log(f"  搜索「{q}」失败（忽略）：{str(e)[:100]}")
    tickets = [s for s in cand if s.startswith("tickets/")]
    chan_pages = [s for s in cand if s.startswith("channels/")]

    # 2) HubSpot 工单：页面里真的提到编号/单号才留
    kept_tickets = []
    for slug in tickets[:MAX_TICKETS * 2]:
        if len(kept_tickets) >= MAX_TICKETS:
            break
        try:
            pg = _page(b, slug)
        except Exception:
            continue
        if relevant(pg.get("content") or ""):
            write_json(raw_dir(n) / "tickets" / (slug.split("/")[-1] + ".json"),
                       {"queried_at": now_z(), "slug": slug, "page": pg})
            kept_tickets.append(slug)
    log(f"大脑：HubSpot 工单 {len(kept_tickets)} 张相关（线索 {len(tickets)} 张）")

    # 3) 聊天页：线索页逐个核对，确定相关频道
    hits = Counter()
    first_day = {}
    for slug in chan_pages[:MAX_HIT_PAGES]:
        parts = slug.split("/")
        if len(parts) != 3:
            continue
        d, day = parts[1], parts[2]  # 页面标识形如 channels/<频道目录>/<日期>
        try:
            pg = _page(b, slug)
        except Exception:
            continue
        if relevant(pg.get("content") or ""):
            hits[d] += 1
            first_day[d] = min(first_day.get(d, day), day)
    channels = [d for d, _ in hits.most_common(MAX_CHANNELS)]
    start, today = _window(n, femini_doc, first_day, channels, kept_tickets)

    fetched = 0
    synced = []
    if start:
        for d in channels:
            f, u = sync_channel(b, d, start, today)
            fetched += f
            synced += u
    log(f"大脑：相关频道 {[channel_name(c) for c in channels]}，窗口 {start}→{today}，新拉 {fetched} 页")

    manifest = {"queried_at": now_z(), "queries": queries, "tickets": kept_tickets, "channels": channels,
                "window": [start.isoformat() if start else None, today.isoformat()],
                "slack_synced_until": max(synced) if synced else None,
                "hit_pages": dict(hits)}
    write_json(raw_dir(n) / "brain.json", manifest)
    return manifest


def _window(n, femini_doc, first_day, channels, kept_tickets):
    """扫描窗口：从案子最早的记录（前推 2 天）到今天。→ (起点或 None, 今天)"""
    starts = [dt.date.fromisoformat(first_day[d]) for d in channels]
    for c in femini_doc["click_url"].get("proposed_changes", []):
        t = iso(c.get("created_at"))
        if t:
            starts.append(dt.date.fromisoformat(t[:10]))
    man = read_json(raw_dir(n) / "github.json") or {}
    for h in man.get("selected", []):
        doc = read_json(issue_file(h["repo"], h["number"]))
        if doc:
            starts.append(dt.date.fromisoformat(doc["issue"]["created_at"][:10]))
    for slug in kept_tickets:
        t = read_json(raw_dir(n) / "tickets" / (slug.split("/")[-1] + ".json"))
        c = iso(((t or {}).get("page") or {}).get("frontmatter", {}).get("hubspot_created_at"))
        if c:
            starts.append(dt.date.fromisoformat(c[:10]))
    today = dt.datetime.now(dt.timezone.utc).date()
    return (min(starts) - dt.timedelta(days=2)) if starts else None, today


def collect_direct(n, femini_doc, log):
    """直连模式（replay/feeds.py，2026-10-31 起）：不经过大脑搜索，直接在本地副本里按链接编号、单号找工单和 Slack 频道。
    规则和 collect_brain 一样（正文真的写了编号或单号才算；工单最多 20 张、频道按命中天数取前 8 个），只是不再受
    搜索结果条数的限制。副本在流水线开头已经刷新到最新；清单照旧写 brain.json（后面的代码按这个文件名读），并注明来源。"""
    pat = num_re(n)
    issues = _issue_numbers(n)
    issue_pats = [issue_re(i) for i in issues]

    def relevant(text):
        return bool(pat.search(text)) or any(p.search(text) for p in issue_pats)

    kept_tickets = []
    for pg in feeds.tickets_matching(relevant)[:MAX_TICKETS]:
        slug = pg["slug"]
        write_json(raw_dir(n) / "tickets" / (slug.split("/")[-1] + ".json"),
                   {"queried_at": now_z(), "slug": slug, "source": "feedai",
                    "page": {"slug": slug, "title": pg.get("title"), "frontmatter": pg.get("frontmatter") or {},
                             "compiled_truth": pg.get("compiled_truth") or "", "content": pg.get("compiled_truth") or ""}})
        kept_tickets.append(slug)

    hits = Counter()
    first_day = {}
    for d, day in feeds.slack_candidates([n] + issues):
        if relevant(feeds.slack_page_text(d, day)):
            hits[d] += 1
            first_day[d] = min(first_day.get(d, day), day)
    channels = [d for d, _ in hits.most_common(MAX_CHANNELS)]
    start, today = _window(n, femini_doc, first_day, channels, kept_tickets)
    as_of = {k: feeds.as_of(k) for k in feeds.KINDS}
    log(f"直连：HubSpot 工单 {len(kept_tickets)} 张，相关频道 {[channel_name(c) for c in channels]}，窗口 {start}→{today}")
    manifest = {"queried_at": now_z(), "source": "direct", "queries": [], "tickets": kept_tickets, "channels": channels,
                "window": [start.isoformat() if start else None, today.isoformat()],
                "slack_synced_until": as_of["slack"], "as_of": as_of, "hit_pages": dict(hits)}
    write_json(raw_dir(n) / "brain.json", manifest)
    return manifest


METRICS = ("gross_spend", "installs", "impressions")
METRIC_DAYS = 400  # 往前取多少天；前面没投放的日子在页面里会被裁掉


def collect_metrics(cid, log=print, today=None):
    """活动的每日投放数据（FeedMob Admin 后台，只读）：毛花费、安装、曝光。存 raw/campaigns/<活动>/metrics.json。"""
    from .util import camp_dir
    from . import campaign, scope
    today = today or dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=METRIC_DAYS)
    f = mcp_client.femini()
    series = {}
    vendor = scope.vendor_for(cid)
    filters = ([{'partner_id':int(vendor)}] if vendor and vendor.isdigit() else
               [{'click_url_name':n} for n in campaign.links(cid)] if vendor else [{}])
    for m in METRICS:
        by_date = {}
        for selector in filters:
            args = dict(start_date=start.isoformat(), end_date=today.isoformat(), metric=m, campaign_id=int(cid), **selector)
            rows = f.call('list_daily_campaign_metrics', args).get('data') or []
            for row in rows:
                if row.get('date'):
                    value = row.get('value')
                    if value is not None:
                        by_date[row['date']] = (by_date.get(row['date']) or 0) + float(value)
                    else:
                        by_date.setdefault(row['date'], None)
        series[m] = [[day, value] for day, value in sorted(by_date.items())]
    doc = dict(queried_at=now_z(), campaign_id=int(cid), vendor_id=vendor,
               start=start.isoformat(), end=today.isoformat(), series=series)
    write_json(camp_dir(cid) / "metrics.json", doc)
    days = sum(1 for _, v in series.get("gross_spend", []) if v)
    log(f"每日投放数据：{days} 天有花费")
    return dict(days_with_spend=days)


def collect(n, log=print):
    st = {}
    try:
        fem = collect_femini(n, log)
        st["femini"] = {"status": "ok", "asof": fem["queried_at"]}
    except Exception as e:
        # 编号在后台查不到：整个案子无从谈起，直接报错
        raise RuntimeError(f"后台查不到编号 {n}：{str(e)[:200]}") from None
    try:
        direct = feeds.enabled()
        man = collect_direct(n, fem, log) if direct else collect_brain(n, fem, log)
        st["brain"] = {"status": "ok", "source": "direct" if direct else "brain", "asof": man["queried_at"],
                       "synced_until": man["slack_synced_until"], "tickets": len(man["tickets"]),
                       "channels": len(man["channels"])}
    except Exception as e:
        log(f"沟通记录取数失败：{str(e)[:200]}")
        st["brain"] = {"status": "error", "error": str(e)[:200]}
    return st
