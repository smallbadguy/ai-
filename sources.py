"""读原始记录的公共小工具：GitHub 原始记录（raw/）和时间换算。replay/ 里的流程共用。

只读文件，不联网，不写任何东西。时间都是 UTC，格式 YYYY-MM-DDTHH:MM:SSZ。
"""
import json
import pathlib
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
RAW = ROOT / "raw"
FACTS = ROOT / "facts"  # 已核实的事实文件（可入库，不含评论原文）

ISS = "https://github.com/feed-mob/feedmob/issues/"
PRW = "https://github.com/feed-mob/feedmob_worker/pull/"


# ───────────────────────── GitHub 原始记录 ─────────────────────────
def load(name, raw=None):
    return json.load(open(pathlib.Path(raw or RAW) / name, encoding="utf-8"))


_cache = {}


def gh_events(n):
    if n not in _cache:
        _cache[n] = load(f"gh_{n}_events.json")
    return _cache[n]


def gh_event(n, event, **want):
    """找第一条满足条件的 GitHub 事件。actor/assignee/assigner 比登录名；label 比标签名；
    after 是时间下限；title_has 比改名后的标题。"""
    for e in gh_events(n):
        if e["event"] != event:
            continue
        ok = True
        for k, v in want.items():
            if k in ("actor", "assignee", "assigner"):
                ok &= ((e.get(k) or {}).get("login") == v)
            elif k == "label":
                ok &= ((e.get("label") or {}).get("name") == v)
            elif k == "after":
                ok &= e["created_at"] >= v
            elif k == "before":
                ok &= e["created_at"] <= v
            elif k == "title_has":
                ok &= v in ((e.get("rename") or {}).get("to") or "")
        if ok:
            return e
    raise SystemExit(f"找不到 GitHub 事件: #{n} {event} {want}")


def gh_comment(n, user, after=None, before=None, startswith=None, contains=None):
    for c in load(f"gh_{n}_comments.json"):
        if c["user"]["login"] != user:
            continue
        if after and c["created_at"] < after:
            continue
        if before and c["created_at"] > before:
            continue
        body = c["body"] or ""
        if startswith and not body.startswith(startswith):
            continue
        if contains and contains not in body:
            continue
        return c
    raise SystemExit(f"找不到评论: #{n} {user} after={after} startswith={startswith} contains={contains}")


def ghe(n, ev):
    return f"{ISS}{n}#event-{ev['id']}"


# ───────────────────────── 时间 ─────────────────────────
def parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def secs(a, b):
    return int((parse(b) - parse(a)).total_seconds())


def _split(a, b):
    s = secs(a, b)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return d, h, m, s


def dur(a, b):
    """中文时长：3 天 2 小时 / 5 小时 38 分 / 3 分 47 秒 / 7 秒。"""
    d, h, m, s = _split(a, b)
    if d:
        return f"{d} 天 {h} 小时" if h else f"{d} 天"
    if h:
        return f"{h} 小时 {m} 分" if m else f"{h} 小时"
    if m:
        return f"{m} 分 {s} 秒" if s else f"{m} 分"
    return f"{s} 秒"


def dur_en(a, b):
    """英文时长，口径与 dur 完全一致：3 days 2 hours / 5 hours 38 minutes / 3 minutes 47 seconds / 7 seconds。"""
    d, h, m, s = _split(a, b)

    def u(n, w):
        return f"{n} {w}" + ("" if n == 1 else "s")

    if d:
        return f"{u(d, 'day')} {u(h, 'hour')}" if h else u(d, "day")
    if h:
        return f"{u(h, 'hour')} {u(m, 'minute')}" if m else u(h, "hour")
    if m:
        return f"{u(m, 'minute')} {u(s, 'second')}" if s else u(m, "minute")
    return u(s, "second")


def to_bj(ts):
    """UTC 时间串 → 北京时间 datetime（UTC+8）。"""
    return parse(ts) + timedelta(hours=8)


def md_zh(ts):
    t = to_bj(ts)
    return f"{t.month}/{t.day}"


def md_en(ts):
    t = to_bj(ts)
    return f"{t.strftime('%b')} {t.day}"


def hm_bj(ts):
    return to_bj(ts).strftime("%m-%d %H:%M")
