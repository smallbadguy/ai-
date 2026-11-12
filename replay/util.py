"""公共路径与小工具。所有按案子区分的文件都放在以编号命名的目录里。"""
import hashlib
import json
import os
import pathlib
import re
import threading
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
OUT = ROOT / "out"
STATE = ROOT / "state"


def valid_number(n):
    """编号（活动编号、链接编号）只接受 1~9 位数字。其他一律拒绝，防止路径/命令注入。"""
    n = str(n).strip()
    if not re.fullmatch(r"\d{1,9}", n):
        raise ValueError(f"无效的编号：{n!r}（需要 1~9 位数字）")
    return n


def raw_dir(n):
    """某一条链接（click URL 编号）的原始记录目录。"""
    return RAW / "links" / valid_number(n)


def base_camp_dir(cid):
    """Shared campaign inventory, independent of the selected vendor."""
    return RAW / "campaigns" / valid_number(cid)


def camp_dir(cid):
    """Material and AI caches for this campaign/vendor variant."""
    from . import scope
    return scope.path(base_camp_dir(cid), cid)


def list_file():
    """全部在跑的活动清单（下拉列表用）。"""
    return RAW / "campaign_list.json"


def out_dir(n):
    from . import scope
    return scope.path(OUT / valid_number(n), n)


def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num_re(n):
    """裸编号匹配：前后不能是数字（也不是小数点后的数字）。"""
    return re.compile(rf"(?<![\d.$]){re.escape(str(n))}(?![\d])")


def issue_re(num):
    """GitHub 单号的写法：#14633、issues/14633、GH#14633。"""
    return re.compile(rf"(?:#|issues/|pull/)\s?{re.escape(str(num))}(?!\d)", re.I)


def iso(s):
    """'2026-09-03 03:20:08 UTC' / '2026-09-03T03:20:08.123Z' → '2026-09-03T03:20:08Z'；识别不了返回 None。"""
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", str(s))
    return f"{m.group(1)}T{m.group(2)}Z" if m else None


def write_json(path, obj):
    """原子写：先写临时文件再改名，避免读到写一半的文件。
    临时文件名带进程号和线程号：几个活动同时跑、写同一个共用缓存（会议、邮件、Slack 日页）时各写各的，不会互相踩。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default=None):
    path = pathlib.Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def safe_name(s):
    """把 slug 之类的字符串变成安全的文件名。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120] + "-" + hashlib.md5(s.encode()).hexdigest()[:6]


HAN = re.compile(r"[\u3400-\u9fff]")


def has_han(s):
    return bool(s and HAN.search(str(s)))
