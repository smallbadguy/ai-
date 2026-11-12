"""把 Brain 里的 Slack 日页副本（raw/mcp/slack/<频道>/<日期>.json）解析成一条条消息。

页面正文里每条消息的格式是：`YYYY-MM-DD HH:MM:SS 发言人: 内容`（时间是 UTC，同一行里可以连着多条）。
本模块只读文件，不联网，不写任何东西。

对外：
  load()                      读取全部频道的消息，按时间排序
  find(msgs, ...)             按频道/发言人/关键词找一条消息，找不到就报错（不让手写内容悄悄对不上）
  following(msgs, msg, ...)   某条消息之后，同频道里某人的下一条
  excerpt(msg, start, end)    从消息里按首尾标记截一段原话（保证是原文的一部分）
  copy_until(msgs)            副本里最新一条消息的时间
"""
import json
import pathlib
import re
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
RAW = ROOT / "raw" / "mcp" / "slack"

_MSG = re.compile(r"(?<![\w:])(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) ([^\n:]{1,60}?): ")


def clean(s):
    """Slack 链接 <url|文字> 只留文字，<url> 只留网址；空白压成一个空格。"""
    s = re.sub(r"<([^>|]*)\|([^>]*)>", r"\2", s)
    s = re.sub(r"<(https?://[^>]*)>", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def _body(page):
    content = page["data"].get("content") or ""
    # 去掉页首的 frontmatter（--- ... ---）和标题
    if content.startswith("---"):
        end = content.find("\n---", 3)
        content = content[end + 4:] if end != -1 else content
    return content


def load(raw=None):
    raw = pathlib.Path(raw) if raw else RAW
    out = []
    for p in sorted(raw.glob("*/*.json")):
        page = json.loads(p.read_text(encoding="utf-8"))
        if page.get("missing"):
            continue
        body = _body(page)
        hits = list(_MSG.finditer(body))
        for i, m in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
            out.append(dict(channel=p.parent.name, ts=f"{m.group(1)}T{m.group(2)}Z", user=m.group(3).strip(),
                            text=clean(body[m.end():end]), page=p.name))
    out.sort(key=lambda x: (x["ts"], x["channel"]))
    return out


def find(msgs, channel, user, contains=None, startswith=None, after=None, before=None):
    """找第一条满足条件的消息；找不到直接退出，提示是哪一条对不上。"""
    for m in msgs:
        if m["channel"] != channel or m["user"] != user:
            continue
        if after and m["ts"] < after:
            continue
        if before and m["ts"] > before:
            continue
        if contains and clean(contains) not in m["text"]:
            continue
        if startswith and not m["text"].startswith(clean(startswith)):
            continue
        return m
    raise SystemExit(f"Slack 副本里找不到消息：#{channel} {user} contains={contains!r} startswith={startswith!r} after={after}")


def following(msgs, msg, user=None, within_hours=6):
    """msg 之后、同一频道里（可指定发言人）的下一条消息；超过 within_hours 小时就当没有。"""
    t0 = datetime.strptime(msg["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    for m in msgs:
        if m["channel"] != msg["channel"] or m["ts"] <= msg["ts"]:
            continue
        if user and m["user"] != user:
            continue
        t1 = datetime.strptime(m["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if (t1 - t0).total_seconds() > within_hours * 3600:
            return None
        return m
    return None


def excerpt(msg, start, end=None):
    """从消息原文里截一段：从 start 开始，到 end 结束（含）；不给 end 就截到消息末尾。"""
    t, s = msg["text"], clean(start)
    i = t.find(s)
    if i < 0:
        raise SystemExit(f"原话起点不在消息里：{s!r}（{msg['channel']} {msg['ts']} {msg['user']}）")
    if end is None:
        return t[i:]
    e = clean(end)
    j = t.find(e, i)
    if j < 0:
        raise SystemExit(f"原话终点不在消息里：{e!r}（{msg['channel']} {msg['ts']} {msg['user']}）")
    return t[i:j + len(e)]


def copy_until(msgs):
    return max((m["ts"] for m in msgs), default=None)
