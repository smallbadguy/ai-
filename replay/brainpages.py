"""邮件和 HubSpot 工单：从公司大脑（只读）镜像到全局缓存，并解析成一条条带时间、带人的记录。

- 邮件来自大脑的 ken-gmail（emails/<线程>），一页是一个线程，里面每封信是「From / To / Sent At + 空行 + 正文」；
  线程末尾大脑附了一段自动摘要（Sent At 后面没有空行），不是原信，丢掉。大脑存的正文有一部分是 UTF-8 被当成 Latin-1
  读出来的乱码（’ 变成 â\\x80\\x99），这里还原。
- 工单来自大脑的 ken-hubspot（tickets/<编号>）：标题、负责人、状态、优先级、建单时间、正文，以及带时间和作者的备注。
- 缓存放 raw/brain_emails、raw/brain_tickets，所有活动共用；页面的 updated_at 没变就不重拉（只在内容变化时才更新）。
- 相关性（relevance）：direct = 点到活动全名/链接号/单号；strong = 标题（邮件主题/工单标题）写着广告主，且至少两个特征词
  （特征词不足两个就要求全部）；loose = 广告主写在标题里或正文里出现 3 次以上，且有一个特征词。
  合作方（如 FreeWheel）的一个线程常常同时谈好几个客户，所以广告主必须在标题里，才算这个活动的线程。
  详细版只收 direct/strong，AI 原料三档都收（由 AI 判断归属）。
- 通讯录（directory）：全部邮件里出现过的人 → 内部地址、外部地址各出现几次，存 raw/brain_directory.json。
  Slack 共享频道里的客户、合作方（如 alejandra.segovia ↔ alejandra.segovia@chime.com）靠它认出是公司外的人。
只调用 list_pages / get_page 两个读取工具。
"""
import functools
import json
import re

from . import feeds, util
from .collect_mcp import _page
from .util import iso, now_z, read_json, safe_name, write_json

LIST_PAGE = 100
MIN_MENTIONS = 3   # 标题里没有广告主时，正文里至少提到几次才算
SOURCES = {"emails": ("ken-gmail", "emails/"), "tickets": ("ken-hubspot", "tickets/")}


def cache_dir(kind):
    """直连模式（replay/feeds.py）读 FeedAI 拉来的副本，否则读从大脑镜像来的缓存。两边格式一样。"""
    return feeds.kind_dir(kind) if feeds.enabled() else util.RAW / f"brain_{kind}"


def list_source(brain, kind):
    source, prefix = SOURCES[kind]
    out, off = [], 0
    while True:
        rows = brain.call("list_pages", {"source_id": source, "limit": LIST_PAGE, "offset": off, "sort": "slug"})
        if not isinstance(rows, list) or not rows:
            break
        out += [r for r in rows if r["slug"].startswith(prefix)]
        off += LIST_PAGE
        if len(rows) < LIST_PAGE:
            break
    return out


def sync(brain, kind, log=print):
    """把大脑里全部邮件线程 / 工单镜像到缓存（没变的不重拉）。→ 统计"""
    rows = list_source(brain, kind)
    fetched = failed = 0
    for r in rows:
        path = cache_dir(kind) / (safe_name(r["slug"]) + ".json")
        have = read_json(path)
        if have and have.get("updated_at") == r.get("updated_at"):
            continue
        try:
            pg = _page(brain, r["slug"])
        except Exception as e:  # 单页取不到不拖垮整体
            failed += 1
            if failed <= 5:
                log(f"  {r['slug']} 取不到（忽略）：{str(e)[:100]}")
            continue
        write_json(path, dict(queried_at=now_z(), slug=r["slug"], title=pg.get("title") or r.get("title") or "",
                              updated_at=pg.get("updated_at") or r.get("updated_at"),
                              frontmatter=pg.get("frontmatter") if isinstance(pg.get("frontmatter"), dict) else {},
                              compiled_truth=pg.get("compiled_truth") or ""))
        fetched += 1
    if kind == "emails" and (fetched or not directory_path().exists()):
        build_directory()
    name = "邮件线程" if kind == "emails" else "HubSpot 工单"
    log(f"{name}：大脑共 {len(rows)} 个，新拉 {fetched} 个" + (f"，失败 {failed}" if failed else ""))
    return dict(listed=len(rows), fetched=fetched, failed=failed)


def cached(kind):
    d = cache_dir(kind)
    if not d.exists():
        return []
    return [x for x in (read_json(p) for p in sorted(d.glob("*.json"))) if x]


# ───────────────────────── 解析 ─────────────────────────
_MOJI = re.compile(r"[\xc2-\xf4][\x80-\xbf]+")


def demojibake(s):
    def fix(m):
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except UnicodeError:
            return m.group(0)
    return _MOJI.sub(fix, s or "")


_MSG = re.compile(r"(?m)^From: (.*)\n(?:To: (.*)\n)?(?:Cc: (.*)\n)?Sent At: (\S+)[ \t]*\n")
_ADDR = re.compile(r"<([^>]+)>")


def person_name(field):
    """'"\\'DiDona, Russell\\' via Ally Pod" <allypod@x>' → 'Russell DiDona'；'Liwei Breslin <l@x>' → 'Liwei Breslin'。"""
    f = (field or "").strip()
    addr = (_ADDR.search(f) or [None, ""])[1]
    name = _ADDR.sub("", f).strip().strip('"').strip()
    name = re.sub(r"\s+via\s+.*$", "", name).strip().strip("'\"").strip()
    if "," in name and name.count(",") == 1:
        last, first = (x.strip() for x in name.split(","))
        if first and last:
            name = f"{first} {last}"
    if not name:
        name = addr or f
    if "@" in name and " " not in name:  # 只有地址没有名字：用 @ 前面的部分
        name = name.split("@")[0]
    return name


INTERNAL_DOMAIN = "feedmob.com"


def is_external(field):
    """发件人/收件人是不是公司外的人：地址不是 feedmob.com，或者是经 Ally Pod 这类群组转发进来的（「X via Ally Pod」）。"""
    f = field or ""
    addr = (_ADDR.search(f) or [None, f.strip()])[1]
    return bool(re.search(r"\svia\s", f)) or ("@" in addr and not addr.lower().endswith("@" + INTERNAL_DOMAIN))


_PARTY = re.compile(r'[^<>@]*<[^>]+>|[^,<>\s]+@[^,<>\s]+')


def parties(field):
    """收件人一栏 → 每个人一段。按尖括号里的地址切，不按逗号切（「DiDona, Russell <r@x>」名字里本身就有逗号）。"""
    return [p.strip().lstrip(",").strip() for p in _PARTY.findall(field or "") if p.strip().lstrip(",").strip()]


def recipients(field):
    return [n for n in (person_name(p) for p in parties(field)) if n]


_HEAD = re.compile(r"(?m)^(?:- )?(?:From|To|Cc): (.*)$")


def directory_path():
    return feeds.directory_path() if feeds.enabled() else util.RAW / "brain_directory.json"


def _keys(name):
    n = re.sub(r"\s+", " ", (name or "").strip()).casefold()
    return {n, re.sub(r"[._-]+", " ", n)} - {""}


def build_directory(pages=None, path=None):
    """全部邮件的 From / To / Cc → {名字或邮箱前缀: [内部次数, 外部次数]}。直连副本刷新时传入它自己的邮件和位置。"""
    seen = {}
    for page in (cached("emails") if pages is None else pages):
        for m in _HEAD.finditer(demojibake(page.get("compiled_truth") or "")):
            for p in parties(m.group(1)):
                p = p.strip("[]'\" ")
                addr = (_ADDR.search(p) or [None, p])[1].strip("[]'\" ").lower()
                if "@" not in addr:
                    continue
                ext = int(is_external(p))
                names = _keys(person_name(p)) | (set() if ext and re.search(r"\svia\s", p) else _keys(addr.split("@")[0]))
                for k in names:
                    seen.setdefault(k, [0, 0])[ext] += 1
    write_json(path or directory_path(), dict(built_at=now_z(), people=seen))
    _directory.cache_clear()
    return len(seen)


@functools.lru_cache(maxsize=1)
def _directory():
    return (read_json(directory_path()) or {}).get("people") or {}


def external_name(name):
    """这个名字在邮件里只用过公司外的地址（从没用过 feedmob 地址）→ 公司外的人。邮件里没出现过的不下结论；
    只有一个词的名字（leon、ali）容易撞上别人的邮箱前缀，也不下结论。"""
    hits = [_directory().get(k) for k in _keys(name) if " " in k]
    hits = [h for h in hits if h]
    return bool(hits) and not any(h[0] for h in hits) and any(h[1] for h in hits)


def email_messages(page):
    """线程页 → [dict(ts, who, to, text)]，按时间排；空正文、尾部摘要、与前面某封正文完全相同的（大脑拆信时的重复）都去掉。"""
    ct = demojibake(page.get("compiled_truth") or "")
    ms = list(_MSG.finditer(ct))
    out, bodies = [], set()
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(ct)
        rest = ct[m.end():end]
        if not rest.startswith("\n"):  # Sent At 后面没有空行：后面是大脑的自动摘要，不是信
            continue
        body = rest.strip()
        ts = iso(m.group(4))
        if not body or not ts:
            continue
        key = re.sub(r"\s+", " ", body)
        if key in bodies:
            continue
        bodies.add(key)
        external = ([person_name(m.group(1))] if is_external(m.group(1)) else []) + [person_name(x) for x in parties(m.group(2)) if is_external(x)]
        out.append(dict(ts=ts, who=person_name(m.group(1)), to=recipients(m.group(2)), text=body, external=external))
    out.sort(key=lambda x: x["ts"])
    return out


def ticket(page):
    """工单页 → dict(id, title, owner, status, priority, created, updated, body, notes=[dict(ts, who, text, id)])。"""
    ct = page.get("compiled_truth") or ""
    fm = page.get("frontmatter") or {}

    def field(lab):
        m = re.search(rf"(?m)^- {lab}[^:\n]*:\s*(.+)$", ct)
        return m.group(1).strip() if m else None
    body = ct.split("## Content", 1)[-1].split("## Notes", 1)[0].strip() if "## Content" in ct else ""
    notes = []
    for line in (ct.split("## Notes", 1)[-1] if "## Notes" in ct else "").splitlines():
        line = line.strip()
        if not line.startswith("- {"):
            continue
        try:
            n = json.loads(line[2:])
        except ValueError:
            continue
        ts = iso(n.get("hs_created_at") or n.get("hs_timestamp") or n.get("created_at"))
        o = n.get("owner") or {}
        who = f"{o.get('first_name', '')} {o.get('last_name', '')}".strip().title()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(n.get("body") or ""))).strip()
        if ts and text:
            notes.append(dict(ts=ts, who=who, text=text, id=n.get("id")))
    notes.sort(key=lambda x: x["ts"])
    return dict(id=str(fm.get("hubspot_ticket_id") or page["slug"].split("/")[-1]), slug=page["slug"],
                title=(page.get("title") or "").strip(), owner=field("Owner"), status=field("Status"), priority=field("Priority"),
                created=iso(fm.get("hubspot_created_at")), updated=iso(fm.get("hubspot_updated_at")), body=body, notes=notes)


# ───────────────────────── 相关性 ─────────────────────────
def relevance(matcher, title, text):
    """→ (档位, 命中)；档位 direct / strong / loose / None。"""
    blob = f"{title}\n{text}"
    strong = matcher.strong(blob)
    if strong:
        return "direct", dict(strong=strong, context=matcher.context(blob))
    in_title = bool(matcher.advertiser(title or ""))
    if not in_title and matcher.advertiser(blob) < MIN_MENTIONS:
        return None, None
    ctx = matcher.context(blob)
    need = min(2, len(matcher.t["context"]))
    if in_title and need and len(ctx) >= need:
        return "strong", dict(strong=[], context=ctx)
    if ctx:
        return "loose", dict(strong=[], context=ctx)
    return None, None


def email_records(matcher, lo, hi):
    """窗口 [lo, hi]（日期字符串）内与本活动有关的邮件 → 原料记录。强相关线程整串收；弱相关只收点到广告主或活动词的信和前后各一封。"""
    out = []
    for page in cached("emails"):
        msgs = [m for m in email_messages(page) if lo <= m["ts"][:10] <= hi]
        if not msgs:
            continue
        subject = demojibake(page.get("title") or "")
        tier, hits = relevance(matcher, subject, "\n".join(m["text"] for m in msgs))
        if not tier:
            continue
        keep = {}
        for i, m in enumerate(msgs):
            if tier in ("direct", "strong"):
                keep[i] = "thread"
            elif matcher.any_hit(m["text"]):
                keep[i] = "match"
                for j in (i - 1, i + 1):
                    if 0 <= j < len(msgs):
                        keep.setdefault(j, "context")
        for i in sorted(keep):
            m = msgs[i]
            out.append(dict(kind="email", ts=m["ts"], thread=page["slug"], subject=subject, who=m["who"], to=m["to"],
                            external=m["external"], text=m["text"], why=keep[i], tier=tier, hits=hits))
    return out


def ticket_records(matcher, lo, hi):
    """窗口内与本活动有关的 HubSpot 工单 → 原料记录：建单一条（没有建单人，不能当原话），每条备注一条。"""
    out = []
    for page in cached("tickets"):
        t = ticket(page)
        tier, hits = ticket_relevance(matcher, t)
        if not tier:
            continue
        meta = dict(ticket=t["id"], slug=t["slug"], title=t["title"], owner=t["owner"], status=t["status"], priority=t["priority"],
                    tier=tier, hits=hits)
        if t["created"] and lo <= t["created"][:10] <= hi:
            out.append(dict(kind="hubspot", ts=t["created"], who="", text=t["body"] or t["title"], note=None, **meta))
        for n in t["notes"]:
            if lo <= n["ts"][:10] <= hi:
                out.append(dict(kind="hubspot", ts=n["ts"], who=n["who"], text=n["text"], note=n["id"], **meta))
    return out


def ticket_relevance(matcher, t):
    return relevance(matcher, t["title"], t["body"] + "\n" + "\n".join(n["text"] for n in t["notes"]))
