"""直连数据源：回放不再经过公司大脑，自己从上游拉 Slack、Gmail、HubSpot 工单和会议，存成本地副本（raw/feed/）。

为什么（2026-10-31 用户定）：大脑（gbrain）是我们自己写的聚合层，每天北京时间 10:30 才同步一次，回放读它最多晚将近一天。
现在直接读上游：Slack / Gmail / HubSpot 读 FeedAI（Qdrant，只读）；会议直连 Fireflies，再用 FeedAI、Femini 里的
Fireflies 逐字稿补上 Fireflies 账号看不到的会；历史 Gong 通话读 FeedAI。Teams 不接（用不上）。

页面格式照搬大脑的导出脚本（ken-team-brain/scripts 下 feedai_qdrant_sync.py、fireflies_to_gbrain.py、extra_common.py、
extra_feedai.py，2026-10-31 版本）：同一条数据渲染出来和大脑里一字不差。AI 做过的题按内容认，所以换来源后已经答过的题
不会重做。改下面「照搬」的部分之前想清楚：改一个字，所有已生成活动里相关的 AI 题都会重做一遍。

本地副本和以前从大脑缓存下来的格式一样，后面整理、筛选的代码照旧：
  raw/feed/slack/<频道目录>/<日期>.json   一个频道一天一页（频道目录 = 频道名-频道ID 转小写，和大脑的页面路径一致）
  raw/feed/emails/ tickets/ meetings/     一个邮件线程 / 一张工单 / 一场会议一个文件（字段同 raw/brain_* 缓存）
  raw/feed/meetings.json                  会议清单（按 Fireflies 编号去重，记来源和日期）
  raw/feed/slack_numbers.json             每个 Slack 日页里出现过的数字（按链接编号、单号找相关频道用，省得每次全扫）
  raw/feed/directory.json                 邮件通讯录（认出 Slack 里哪些人是公司外的）
  raw/feed/state.json                     每类来源：从几点开始拉（= 数据截至）、拉到的最新一条、多少条、出错原因

刷新（refresh）：
- Slack：平时只重拉最近 3 天（按消息时间筛选，几秒）；距上次全量超过 20 小时就全部重读一次（约 20 秒），补上改过、删掉的消息。
- 邮件、工单：每次全部重读（合计十几秒），和大脑每天做的一样，不会漏掉改动。
- 会议：每小时最多看一次。Fireflies 直连看最近 2 天的会；FeedAI、Femini 里有、本地还没有的会补进来，规则和大脑一致：
  Fireflies 直连的优先，其次 Femini 的逐句记录，最后 FeedAI 的文本。第一次要把历史全部拉一遍（--full，约半小时）。
- 刚刷新过（Slack/邮件/工单 10 分钟内，会议 1 小时内）就跳过；刚失败过 5 分钟内也不重试。同一时刻只有一个刷新在跑（文件锁）。
- 一类来源失败不影响其它类，也不动已有副本，只记下原因；页面照旧用已有副本，并标明数据截至几点。

只读：Qdrant 只用 scroll / retrieve；Fireflies 只用查询；Femini 只用 get_fireflies_meeting。
凭证：secrets/feeds.env 里的 QDRANT_URL、QDRANT_API_KEY、FIREFLIES_API_KEY；Femini 用 FEMINI_MCP_TOKEN，没有就用
secrets/femini.env 里回放自己的 FEMINI_MCP_API_KEY。不入库、不打印。
开关：REPLAY_SOURCE=direct（环境变量优先，其次 secrets/feeds.env）时流水线改读这里；不设就照旧读大脑。

命令（在 app 目录下）：
  python3 -m replay.feeds refresh [--kinds slack,emails,tickets,meetings] [--force] [--full]
  python3 -m replay.feeds status
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import http.client
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

from . import util
from .util import now_z, safe_name, write_json

KINDS = ("slack", "emails", "tickets", "meetings")
MAX_AGE = {"slack": 600, "emails": 600, "tickets": 600, "meetings": 3600}  # 刚刷新过多久内不再刷新（秒）
RETRY_AFTER = 300              # 失败后至少隔多久再试（秒），免得上游出故障时每次生成都卡在重试上
SLACK_RECENT_DAYS = 3          # Slack 平时只重拉最近几天
SLACK_FULL_EVERY = 20 * 3600   # 距上次全量超过这么久就全部重读一次
FIREFLIES_DAYS = 2             # Fireflies 直连平时只看最近几天的会（和大脑一样）
FIREFLIES_RECHECK = 6 * 3600   # 最近几天的会：本地那份超过这么久就重拉（转写晚到、标题改了）
FIREFLIES_SLEEP = 1.1          # 每次取会议详情之间的间隔（Business 套餐每分钟 60 次）
REFRESH_TIMEOUT = 15 * 60      # 流水线里等一次刷新最多多久（秒）；超时就先用已有副本
NUMBER_RUN = re.compile(r"(?<!\d)\d{3,8}(?!\d)")  # 编号、单号都是一整串数字（见 util.num_re / issue_re）

SLACK_COLLECTION = "slack_api_messages_v3"
EMAIL_COLLECTION = "email_documents_v3"
TICKET_COLLECTION = "hubspot_tickets_v3"
GONG_COLLECTION = "gong_call_transcripts"
FF_COLLECTIONS = ("fireflies_call_transcripts_v3", "fireflies_call_transcripts")  # v3 优先
FF_FIELDS = ["metadata.id", "metadata.parent_id", "metadata.is_chunk", "metadata.has_chunks", "metadata.chunk_index",
             "metadata.title", "metadata.date", "metadata.duration_minutes", "metadata.transcript_url"]
FIREFLIES_API = "https://api.fireflies.ai/graphql"
FEMINI_URL = "https://assistant.feedmob.ai/mcp"
KNOWN_MAX_MEETING_ID = 1307    # 大脑设计时实测：Femini 会议编号 1~1307 连续。只作每日扫描起点的下限
MEETING_LOOKBACK = 60          # 每日扫描回看多少个编号（转写晚到的会议靠它补上）
MEETING_GAP_STOP = 40          # 全量扫描：连续这么多个编号都「不存在」就认为读到头了
MEETING_DAILY_GAP_STOP = 12    # 每日扫描：只需确认有没有新编号
MAX_PAGE_CHARS = 400_000       # 大脑补数据脚本的单页上限（Femini / FeedAI 会议、Gong 照它截断，保持一致）


# ───────────────────────── 位置与设置 ─────────────────────────
def root():
    return util.RAW / "feed"


def slack_root():
    return root() / "slack"


def kind_dir(kind):
    """emails / tickets / meetings 的副本目录。"""
    return root() / kind


def state_path():
    return root() / "state.json"


def numbers_path():
    return root() / "slack_numbers.json"


def meeting_index_path():
    return root() / "meetings.json"


def directory_path():
    return root() / "directory.json"


def _secrets():
    return pathlib.Path(os.environ.get("REPLAY_SECRETS", util.ROOT / "secrets"))


_ENV_CACHE = {}


def _env_file(name):
    """secrets 目录里的 KEY=VALUE 文件（按修改时间缓存）。"""
    path = _secrets() / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _ENV_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    _ENV_CACHE[path] = (mtime, out)
    return out


def setting(name, default=""):
    """环境变量优先，其次 secrets/feeds.env。"""
    return os.environ.get(name) or _env_file("feeds.env").get(name) or default


def enabled():
    """REPLAY_SOURCE=direct：流水线读这里的副本，不再经过大脑。"""
    return setting("REPLAY_SOURCE").strip().lower() == "direct"


def _read(path):
    """读一个副本文件；没有或坏了返回 None（坏文件下次刷新会重写）。"""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_state():
    return _read(state_path()) or {}


def as_of(kind):
    """这类来源的副本数据截至几点（最近一次成功刷新开始的时间，UTC）。"""
    return (read_state().get(kind) or {}).get("as_of")


def ready():
    """副本是不是已经完整初始化过（每类都成功全量拉过一次）。没初始化就开直连，会丢掉历史数据。"""
    st = read_state()
    m = st.get("meetings") or {}
    return bool((st.get("slack") or {}).get("full_at") and (st.get("emails") or {}).get("as_of")
                and (st.get("tickets") or {}).get("as_of") and m.get("fireflies_backfilled")
                and m.get("femini_backfilled") and m.get("gong_done"))


def _age(stamp):
    """距 stamp（ISO 时间）过了多少秒；没有就当很久。"""
    ts = parse_ts(stamp)
    return time.time() - ts if ts else float("inf")


# ───────────────────────── FeedAI（Qdrant，只读）─────────────────────────
def _qdrant(path, body=None, retries=4):
    url, key = setting("QDRANT_URL").rstrip("/"), setting("QDRANT_API_KEY")
    if not url or not key:
        raise RuntimeError("缺少 QDRANT_URL / QDRANT_API_KEY（secrets/feeds.env）")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"api-key": key, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last = e
                time.sleep(min(15.0, 2.0 ** attempt))
                continue
            raise RuntimeError(f"FeedAI {path} HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            last = e
            time.sleep(min(15.0, 2.0 ** attempt))
    raise RuntimeError(f"FeedAI {path} 失败：{last!r}"[:300])


def scroll(collection, flt=None, payload=True, batch=1000):
    offset = None
    while True:
        body = {"limit": batch, "with_payload": payload, "with_vector": False}
        if flt:
            body["filter"] = flt
        if offset is not None:
            body["offset"] = offset
        result = _qdrant(f"/collections/{collection}/points/scroll", body)["result"]
        points = result.get("points", [])
        yield from points
        offset = result.get("next_page_offset")
        if not points or offset is None:
            return


def retrieve(collection, ids):
    """按点编号取回完整 payload → {点编号: payload}。"""
    found = {}
    for i in range(0, len(ids), 100):
        result = _qdrant(f"/collections/{collection}/points",
                         {"ids": ids[i:i + 100], "with_payload": True, "with_vector": False})["result"]
        for point in result:
            found[point["id"]] = point.get("payload") or {}
    return found


# ═════════════ 以下照搬大脑导出脚本（逐字不改，渲染结果才和大脑里一样）═════════════
# ---- feedai_qdrant_sync.py ----
_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|password)\s*(?:is|=|:)\s*)\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


# 每条脱敏规则能命中的前提（文字里没有这些字样，那条规则不可能命中，就不跑正则）：结果和逐条全跑完全一样，只是快
# （邮件全量从约 45 秒降到十几秒）。第一条不分大小写，用 casefold 判断（ſ、K 这类特殊写法也算进去）。
_SECRET_HINTS = (
    lambda t: any(w in t.casefold() for w in ("key", "token", "secret", "password")),
    lambda t: "eyJ" in t,
    lambda t: "sk-" in t,
    lambda t: "xox" in t,
    lambda t: "AKIA" in t,
    lambda t: "-----BEGIN" in t,
)


def redact(value):
    text = "" if value is None else str(value)
    for pattern, hint in zip(_SECRET_PATTERNS, _SECRET_HINTS):
        if hint(text):
            text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text


def _safe(value):
    return json.dumps(value, ensure_ascii=False)


def merge_chunks(chunks):
    output = ""
    for _, raw in sorted(chunks):
        text = redact(raw).strip()
        if not text:
            continue
        if not output:
            output = text
            continue
        max_overlap = min(1000, len(output), len(text))
        overlap = 0
        for size in range(max_overlap, 39, -1):
            if output[-size:] == text[:size]:
                overlap = size
                break
        output += "\n" + text[overlap:]
    return output


def _file_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value)


# ---- extra_common.py ----
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_SEPS = str.maketrans({"\u2028": " ", "\u2029": " ", "\x85": " "})


class Raw(str):
    """frontmatter 里不加引号的原样值（例如会议页的 date: 2026-03-10）。"""


def clean(text, limit=0):
    value = _CTRL.sub("", "" if text is None else str(text)).replace("\r\n", "\n").replace("\r", "\n")
    value = redact(value)
    if limit and len(value) > limit:
        value = value[:limit].rstrip() + f"\n\n[… truncated {len(value) - limit} characters]"
    return value


def one_line(text, limit=0):
    value = " ".join(clean(text).split())
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def yaml_value(value):
    if isinstance(value, Raw):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(yaml_value(x) for x in value if x not in (None, "")) + "]"
    return json.dumps(one_line(value).translate(_LINE_SEPS), ensure_ascii=False)


def frontmatter(pairs):
    lines = ["---"]
    for key, value in pairs:
        if value is None or value == "" or value == [] or value == ():
            continue
        lines.append(f"{key}: {yaml_value(value)}")
    lines += ["---", ""]
    return lines


def cjk_slug(text, maxlen=60):
    value = (str(text or "untitled")).lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value, flags=re.UNICODE).strip("-")
    return (value[:maxlen].rstrip("-")) or "untitled"


def parse_ts(value):
    """ISO 时间 → epoch 秒；解析不了返回 None。没写时区按 UTC。"""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def utc_iso(value):
    ts = parse_ts(value)
    if ts is None:
        return ""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text):
    """大脑补数据脚本 write_if_changed 的单页截断。"""
    if len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS].rstrip() + f"\n\n[… page truncated at {MAX_PAGE_CHARS} characters]\n"
    return text


class FeminiError(Exception):
    """某一条记录读不到（没有这个编号、或这条数据在 Femini 那边本身就是坏的）。调用方可以跳过这条。"""


class FeminiNotFound(FeminiError):
    """明确回答了「没有这个编号」。"""


class FeminiFatal(FeminiError):
    """连不上、重试用尽、授权失效：不能当「这条读不到」跳过。"""


class Femini:
    """Femini MCP（JSON-RPC over HTTP，可能回 SSE）。网络抖动自动重试，业务错误直接抛出；每秒最多 4 次请求。"""

    def __init__(self, token, url=FEMINI_URL, timeout=60, retries=6, max_rps=4.0):
        if not token:
            raise FeminiFatal("缺少 Femini 凭证（FEMINI_MCP_TOKEN 或 secrets/femini.env）")
        self.url, self.token, self.timeout, self.retries = url, token, timeout, retries
        self._gap = 1.0 / max_rps if max_rps > 0 else 0.0
        self._gate = threading.Lock()
        self._next_at = 0.0

    def _throttle(self):
        if not self._gap:
            return
        with self._gate:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._gap
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _business_error(name, text):
        raise (FeminiNotFound if "not found" in text.lower() else FeminiError)(f"{name}: {text}")

    @staticmethod
    def _decode(text):
        text = text.strip()
        if text.startswith("event:") or "\ndata:" in text:
            text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
        return json.loads(text)

    def call(self, name, args=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}}).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        last = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    message = self._decode(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    last = exc
                    time.sleep(min(20.0, 1.7 ** attempt))
                    continue
                raise FeminiFatal(f"{name}: HTTP {exc.code}") from None
            except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError,
                    http.client.HTTPException, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(min(20.0, 1.7 ** attempt))
                continue
            if message.get("error"):
                self._business_error(name, json.dumps(message["error"], ensure_ascii=False)[:200])
            result = message.get("result") or {}
            if result.get("isError"):
                text = " ".join(str(c.get("text", "")) for c in (result.get("content") or []) if isinstance(c, dict))
                self._business_error(name, text[:200])
            structured = result.get("structuredContent")
            if structured is None:
                for part in result.get("content") or []:
                    try:
                        structured = json.loads(part.get("text", ""))
                        break
                    except (ValueError, AttributeError):
                        continue
            return structured if isinstance(structured, dict) else {}
        raise FeminiFatal(f"{name}: retries exhausted ({last!r})")


def _femini_token():
    return (setting("FEMINI_MCP_TOKEN") or _env_file("femini.env").get("FEMINI_MCP_API_KEY")
            or os.environ.get("FEMINI_MCP_API_KEY", ""))


# ---- fireflies_to_gbrain.py ----
LIST_QUERY = """
query Transcripts($limit: Int, $skip: Int, $fromDate: DateTime, $toDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate, toDate: $toDate) {
    id
    title
    dateString
    duration
    host_email
    participants
  }
}
"""

DETAIL_QUERY = """
query Transcript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    dateString
    duration
    host_email
    organizer_email
    participants
    transcript_url
    meeting_link
    speakers { id name }
    meeting_attendees { displayName email name }
    summary {
      keywords
      action_items
      overview
      topics_discussed
      meeting_type
    }
    sentences {
      index
      speaker_name
      text
      start_time
    }
  }
}
"""


class FirefliesError(RuntimeError):
    pass


def _gql(query, variables, max_retries=5):
    """发一次 GraphQL 请求（钥匙不打印）。429 / 5xx / 网络抖动退避重试；原脚本出错就退出进程，这里改成抛异常。"""
    key = setting("FIREFLIES_API_KEY").strip()
    if not key:
        raise FirefliesError("缺少 FIREFLIES_API_KEY（secrets/feeds.env）")
    payload = json.dumps({"query": query, "variables": variables}).encode()
    last_err = ""
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(FIREFLIES_API, data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 429:
                last_err = f"HTTP 429 {detail[:200]}"
                time.sleep(60 if attempt == 1 else min(300, 60 * attempt))
                continue
            if 500 <= e.code < 600:
                last_err = f"HTTP {e.code} {detail[:200]}"
                time.sleep(min(60, 5 * attempt))
                continue
            raise FirefliesError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            last_err = f"网络失败 {e}"
            time.sleep(min(60, 5 * attempt))
            continue
        if body.get("errors"):
            raise FirefliesError("GraphQL 报错：" + "; ".join(str(x.get("message", x)) for x in body["errors"])[:300])
        return body.get("data") or {}
    raise FirefliesError(f"重试 {max_retries} 次仍失败，最后一次：{last_err}"[:300])


def clock_hms(start_time, meeting_start):
    try:
        secs = float(start_time)
    except (TypeError, ValueError):
        secs = 0.0
    if secs < 0:
        secs = 0.0
    t = meeting_start + dt.timedelta(seconds=int(secs))
    return f"{t.hour}:{t.minute:02d}:{t.second:02d}"


def parse_dt(s):
    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (ValueError, AttributeError):
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def sanitize_speaker(name):
    n = (name or "Unknown Speaker").replace("*", "").replace("\n", " ").strip()
    return n or "Unknown Speaker"


def sanitize_text(text):
    return " ".join((text or "").split())


def slugify(s, maxlen=60):
    s = (s or "untitled").lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", s, flags=re.UNICODE).strip("-")
    return (s[:maxlen].rstrip("-")) or "untitled"


def yaml_list(items):
    clean_items = [str(x).replace('"', "'") for x in (items or []) if x]
    if not clean_items:
        return "[]"
    return "[" + ", ".join(f'"{x}"' for x in clean_items) + "]"


def render_page(t):
    """一个 Fireflies transcript → (slug, markdown)。"""
    date_iso = (t.get("dateString") or "")[:10] or "1970-01-01"
    meeting_start = parse_dt(t.get("dateString"))
    title = t.get("title") or "untitled"
    slug = f"{date_iso}-{slugify(title)}"
    speakers = []
    for s in t.get("speakers") or []:
        n = s.get("name")
        if n and n not in speakers:
            speakers.append(n)
    attendees = []
    for a in t.get("meeting_attendees") or []:
        v = a.get("displayName") or a.get("name") or a.get("email")
        if v and v not in attendees:
            attendees.append(v)
    summary = t.get("summary") or {}
    fm = [
        "---",
        "type: meeting",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f"date: {date_iso}",
        f'datetime: "{t.get("dateString") or ""}"',
        "timezone: UTC",
        "time_base: clock",
        "origin: fireflies",
        f'fireflies_id: "{t.get("id") or ""}"',
        f'fireflies_url: "{t.get("transcript_url") or ""}"',
        f"duration_minutes: {t.get('duration') or 0}",
        f'host: "{t.get("host_email") or ""}"',
        f"speakers: {yaml_list(speakers)}",
        f"attendees: {yaml_list(attendees)}",
        f"participants: {yaml_list(t.get('participants'))}",
        f"keywords: {yaml_list(summary.get('keywords'))}",
        "---",
        "",
    ]
    body = [f"# {title}", ""]
    body += ["## 分析", ""]
    if summary.get("overview"):
        body += ["### Fireflies 自动摘要（仅供参考，勿当结论）", "", str(summary["overview"]).strip(), ""]
    if summary.get("action_items"):
        body += ["### Fireflies 抽的待办（仅供参考）", "", str(summary["action_items"]).strip(), ""]
    if summary.get("topics_discussed"):
        body += ["### 话题", "", ", ".join(str(x) for x in summary["topics_discussed"]), ""]
    body += ["---", "", "## 全文转录", ""]
    lines = []
    for s in t.get("sentences") or []:
        txt = sanitize_text(s.get("text"))
        if not txt:
            continue
        lines.append(f"**{sanitize_speaker(s.get('speaker_name'))}** ({clock_hms(s.get('start_time'), meeting_start)}): {txt}")
    if not lines:
        lines = ["<!-- 这场会没有 sentences：可能是 silent meeting，或 API key 无权读取全文 -->"]
    body += lines
    body.append("")
    return slug, "\n".join(fm + body)


# ---- extra_feedai.py ----
_ID_AFTER_SEP = re.compile(r"::([0-9A-Za-z]{20,})")
_ID_IN_VIEW = re.compile(r"/view/([0-9A-Za-z]{20,})")


def native_id(meta):
    url = str(meta.get("transcript_url") or "")
    found = _ID_AFTER_SEP.search(url) or _ID_IN_VIEW.search(url)
    return found.group(1) if found else str(meta.get("id") or "")


def scan_collection(collection):
    meetings = {}
    for point in scroll(collection, payload={"include": FF_FIELDS}, batch=500):
        meta = (point.get("payload") or {}).get("metadata") or {}
        fid = native_id(meta)
        if not fid:
            continue
        ref = meetings.setdefault(fid, {"id": fid, "coll": collection, "title": "", "date": "", "duration": None,
                                        "parent": None, "standalone": None, "chunks": {}})
        ref["title"] = ref["title"] or str(meta.get("title") or "")
        ref["date"] = ref["date"] or str(meta.get("date") or "")
        if ref["duration"] is None and meta.get("duration_minutes") is not None:
            ref["duration"] = meta.get("duration_minutes")
        if meta.get("is_chunk"):
            ref["chunks"][int(meta.get("chunk_index") or 0)] = point["id"]
        elif meta.get("has_chunks"):
            ref["parent"] = point["id"]
        else:
            ref["standalone"] = point["id"]
    return meetings


def scan_feedai_fireflies():
    merged = {}
    for collection in reversed(FF_COLLECTIONS):  # 先旧后新，v3 覆盖 v1
        merged.update(scan_collection(collection))
    return merged


def read_meeting_row(fm, number):
    try:
        detail = fm.call("get_fireflies_meeting", {"id": number}).get("fireflies_meeting") or {}
    except FeminiNotFound:
        return "missing", None
    except FeminiFatal:
        raise
    except FeminiError:
        return "bad", None
    fid = str(detail.get("fireflies_id") or "")
    if not fid:
        return "bad", None
    return "ok", {"id": number, "fireflies_id": fid, "title": detail.get("title"),
                  "meeting_date": detail.get("meeting_date"), "duration": detail.get("duration"),
                  "participants": [p for p in detail.get("participants") or [] if isinstance(p, dict)],
                  "utterance_count": len(detail.get("utterances") or [])}


def scan_femini_meetings(fm, first_id=1, gap=MEETING_GAP_STOP):
    """从 first_id 往后按编号读 Femini 会议，直到连续 gap 个编号都「不存在」（一个一个串行读）。"""
    rows = {}
    stats = {"read": 0, "ok": 0, "bad": 0, "missing": 0, "max_id": first_id - 1}
    last_seen = first_id - 1
    number = first_id
    while number - 1 - last_seen < gap:
        state, row = read_meeting_row(fm, number)
        stats["read"] += 1
        stats[state] += 1
        if state != "missing":
            last_seen = number
        if row:
            old = rows.get(row["fireflies_id"])
            if old is None or row["utterance_count"] > old["utterance_count"]:
                rows[row["fireflies_id"]] = row
        number += 1
    stats["max_id"] = last_seen
    return rows, stats


def speaker(name):
    value = str(name or "Unknown Speaker").replace("*", "").replace("\n", " ").strip()
    return value or "Unknown Speaker"


def clock(value):
    ts = parse_ts(value)
    if ts is None:
        return ""
    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f"{t.hour}:{t.minute:02d}:{t.second:02d}"


def femini_lines(utterances):
    lines = []
    for u in utterances:
        text = one_line(u.get("content"))
        if not text:
            continue
        when = clock(u.get("start_time"))
        lines.append(f"**{speaker(u.get('speaker_name'))}** ({when}): {text}" if when
                     else f"**{speaker(u.get('speaker_name'))}**: {text}")
    return lines


def meeting_page(*, fid, title, date_iso, duration, origin, body, participants, timed, femini_id=None):
    emails = [p.get("email") for p in (participants or []) if p.get("email")]
    names = [p.get("name") for p in (participants or []) if p.get("name")]
    fm = frontmatter([
        ("type", "meeting"), ("title", title or "untitled"), ("date", Raw(date_iso[:10] or "1970-01-01")),
        ("datetime", date_iso), ("timezone", "UTC"), ("time_base", "clock" if timed else None),
        ("origin", origin), ("fireflies_id", fid), ("femini_id", femini_id),
        ("fireflies_url", f"https://app.fireflies.ai/view/{fid}"),
        ("duration_minutes", duration if isinstance(duration, (int, float)) else None),
        ("attendees", names), ("participants", emails),
        ("participants_known", bool(emails)), ("source", origin),
    ])
    return "\n".join(fm + [f"# {one_line(title) or 'untitled'}", ""] + body) + "\n"


def fetch_feedai_text(ref, summary_only):
    """返回 (摘要, 全文, 缺失块数)。summary_only=True 时只取父点（摘要）。"""
    ids = []
    if ref.get("parent"):
        ids.append(ref["parent"])
    if not summary_only:
        ids += [ref["chunks"][i] for i in sorted(ref["chunks"])]
        if ref.get("standalone"):
            ids.append(ref["standalone"])
    if not ids:
        return "", "", 0
    payloads = retrieve(ref["coll"], ids)

    def text_of(pid):
        return str((payloads.get(pid) or {}).get("page_content") or "")

    summary = clean(text_of(ref["parent"]).strip()) if ref.get("parent") else ""
    if summary_only:
        return summary, "", 0
    chunks = [(i, text_of(ref["chunks"][i])) for i in sorted(ref["chunks"])]
    if ref.get("standalone"):
        chunks.append((len(chunks), text_of(ref["standalone"])))
    transcript = merge_chunks(chunks)
    expected = None
    meta = (payloads.get(ref.get("parent")) or {}).get("metadata") or {}
    if isinstance(meta.get("chunk_count"), int):
        expected = meta["chunk_count"]
    missing = max(0, expected - len(ref["chunks"])) if expected else 0
    return summary, transcript, missing


def render_gong(call_id, chunks):
    chunks = sorted(chunks, key=lambda c: c[0])
    meta = chunks[0][2]
    start = utc_iso(meta.get("start_time"))
    title = str(meta.get("title") or f"Gong call {call_id}")
    total = max((int(c[2].get("total_chunks") or 0) for c in chunks), default=0)
    text = merge_chunks([(i, t) for i, t, _ in chunks])
    duration = meta.get("duration")
    minutes = round(duration / 60, 1) if isinstance(duration, (int, float)) and duration else None
    body = [f"# {one_line(title)}", "", "## Transcript (Gong)", "", clean(text), ""]
    if total and len({c[0] for c in chunks}) < total:
        body += [f"[note: only {len({c[0] for c in chunks})} of {total} transcript chunks were available in FeedAI]", ""]
    fm = frontmatter([
        ("type", "meeting"), ("title", title), ("date", Raw(start[:10] or "1970-01-01")), ("datetime", start),
        ("timezone", "UTC"), ("origin", "gong"), ("gong_call_id", call_id), ("gong_url", meta.get("url")),
        ("duration_minutes", minutes), ("direction", meta.get("direction")),
        ("participants_known", False), ("source", "feedai-gong"),
    ])
    path = f"{start[:10] or '1970-01-01'}-{cjk_slug(title)}-{call_id[-6:]}.md"
    return path, "\n".join(fm + body) + "\n", start, title
# ═════════════ 照搬部分到此为止 ═════════════


def _body(text):
    """页面正文：去掉开头的 frontmatter，再去掉首尾空白——和大脑 get_page 返回的 compiled_truth 一样。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


# ───────────────────────── Slack ─────────────────────────
def slack_dir_name(channel_name, channel_id):
    """频道目录 = 大脑页面路径 channels/<目录>/<日期> 里的目录：大脑导出脚本的目录名转小写，大脑生成页面路径时
    还会把连着的「-」并成一个、去掉首尾的「-」（实测：--C01F9MHLDG9 → c01f9mhldg9，feedmob--C019PJFSYLU → feedmob-c019pjfsylu）。"""
    return re.sub(r"-{2,}", "-", f"{_file_name(channel_name)}-{_file_name(channel_id)}".lower()).strip("-")


def slack_pages(points):
    """FeedAI 的 Slack 消息 → {(频道目录, 日期): 页面}（分组、排序、格式照搬大脑 feedai_qdrant_sync.sync_slack）。"""
    messages = {}
    count = 0
    for point in points:
        count += 1
        payload = point.get("payload") or {}
        meta = payload.get("metadata") or {}
        ident = str(meta.get("parent_id") or meta.get("id") or point["id"])
        slim = {k: meta.get(k) for k in ("channel_id", "channel_name", "timestamp", "permalink")}
        item = messages.setdefault(ident, {"meta": slim, "chunks": []})
        if not meta.get("is_chunk"):
            item["meta"] = slim
        item["chunks"].append((int(meta.get("chunk_index") or 0), payload.get("page_content", "")))
    grouped = defaultdict(list)
    for ident, item in messages.items():
        meta = item["meta"]
        channel_id = str(meta.get("channel_id") or "unknown")
        channel_name = str(meta.get("channel_name") or channel_id)
        timestamp = str(meta.get("timestamp") or "")
        day = timestamp[:10] if re.match(r"\d{4}-\d{2}-\d{2}", timestamp) else "unknown-date"
        body = merge_chunks(item["chunks"])
        lines = body.splitlines()
        if lines and channel_name.lower() in lines[0].lower():
            body = "\n".join(lines[1:])
        body = redact(re.sub(r"\s+", " ", body)).strip()
        if not body:
            continue
        private_like = channel_id.startswith(("D", "G"))
        grouped[(channel_id, channel_name, day, private_like)].append((timestamp, ident, body, meta))
    pages = {}
    for (channel_id, channel_name, day, private_like), rows in grouped.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        title = f"Slack {channel_name} — {day}"
        latest_link = next((str(row[3].get("permalink")) for row in reversed(rows) if row[3].get("permalink")), "")
        lines = [
            "---", "type: note", f"title: {_safe(title)}", f"date: {_safe(day)}",
            f"channel_id: {_safe(channel_id)}", f"channel_name: {_safe(channel_name)}",
            f"visibility: {_safe('private-or-dm' if private_like else 'channel')}",
            "tags: [slack, communication]", "source: feedai-slack",
        ]
        if latest_link:
            lines.append(f"source_url: {_safe(latest_link)}")
        lines += ["---", "", f"# {redact(title)}", ""]
        lines.extend(row[2] for row in rows)
        pages[(slack_dir_name(channel_name, channel_id), day)] = dict(
            title=title, content="\n\n".join(lines) + "\n", updated_at=utc_iso(rows[-1][0]) or None)
    return pages, count


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def refresh_slack(st, full=False, log=print):
    base = slack_root()
    whole = full or not st.get("full_at") or _age(st.get("full_at")) > SLACK_FULL_EVERY or not base.exists()
    cutoff, flt = None, None
    if not whole:
        cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=SLACK_RECENT_DAYS)).isoformat()
        flt = {"must": [{"key": "metadata.timestamp", "range": {"gte": f"{cutoff}T00:00:00Z"}}]}
    started = now_z()
    pages, points = slack_pages(scroll(SLACK_COLLECTION, flt))
    numbers = {} if whole else ((_read(numbers_path()) or {}).get("pages") or {})
    written, keep = 0, set()
    for (d, day), page in pages.items():
        key = f"{d}/{day}"
        keep.add(key)
        path = base / d / f"{day}.json"
        have = _read(path)
        if not have or (have.get("data") or {}).get("content") != page["content"]:
            write_json(path, {"source": "feedai", "queried_at": started, "missing": False,
                              "data": {"slug": f"channels/{d}/{day}", "title": page["title"],
                                       "content": page["content"], "updated_at": page["updated_at"]}})
            written += 1
        numbers[key] = " ".join(sorted(set(NUMBER_RUN.findall(page["content"]))))
    removed = 0  # 上游已经没有的日页删掉：全量时看全部；平时只看重拉的那几天
    if base.exists():
        for path in base.glob("*/*.json"):
            key = f"{path.parent.name}/{path.stem}"
            if key in keep:
                continue
            if whole or (_DAY.fullmatch(path.stem) and path.stem >= cutoff):
                path.unlink()
                numbers.pop(key, None)
                removed += 1
    write_json(numbers_path(), {"built_at": started, "pages": numbers})
    newest = max((p["updated_at"] for p in pages.values() if p["updated_at"]), default=None)
    if not whole:
        newest = max(filter(None, [newest, st.get("newest")]), default=None)
    if whole:
        st["full_at"] = started
    log(f"Slack：{'全量' if whole else f'{cutoff} 起'} {points} 条消息 → {len(pages)} 个日页，改动 {written}，删除 {removed}")
    return dict(points=points, pages=len(pages), written=written, removed=removed, full=whole, newest=newest)


def slack_index():
    """{频道目录: {日期: None}}——和大脑 Slack 页面清单同一个形状（后面只用到日期）。"""
    out = {}
    base = slack_root()
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir():
                days = {p.stem: None for p in d.glob("*.json")}
                if days:
                    out[d.name] = days
    return out


def slack_candidates(numbers):
    """出现过这些数字（链接编号、单号）的 Slack 日页 → [(频道目录, 日期)]。数字不是 3~8 位的就逐页全扫。"""
    want = {str(x) for x in numbers if str(x)}
    if not want:
        return []
    if any(not NUMBER_RUN.fullmatch(x) for x in want):
        return sorted((p.parent.name, p.stem) for p in slack_root().glob("*/*.json"))
    pages = (_read(numbers_path()) or {}).get("pages") or {}
    return sorted(tuple(k.rsplit("/", 1)) for k, runs in pages.items() if want & set(runs.split()))


def slack_page_text(d, day):
    rec = _read(slack_root() / d / f"{day}.json") or {}
    return (rec.get("data") or {}).get("content") or ""


# ───────────────────────── 邮件、工单 ─────────────────────────
def email_threads(points):
    """FeedAI 的邮件 → {slug: 线程页}（分组、排序、格式照搬大脑 feedai_qdrant_sync.sync_gmail）。"""
    threads = defaultdict(list)
    count = 0
    for point in points:
        count += 1
        payload = point.get("payload") or {}
        meta = payload.get("metadata") or {}
        key = str(meta.get("thread_id") or meta.get("id") or meta.get("parent_id") or point["id"])
        threads[key].append({"meta": meta, "content": payload.get("page_content", "")})
    out = {}
    for thread_id, rows in threads.items():
        rows.sort(key=lambda r: (str(r["meta"].get("date") or ""), int(r["meta"].get("document_id") or 0),
                                 int(r["meta"].get("chunk_index") or 0)))
        latest = rows[-1]["meta"]
        title = latest.get("subject") or f"Gmail thread {thread_id}"
        date = max((str(r["meta"].get("date") or "") for r in rows), default="")
        by_doc = defaultdict(list)
        doc_meta = {}
        for row in rows:
            meta = row["meta"]
            doc = str(meta.get("document_id") or meta.get("gmail_id") or meta.get("id"))
            by_doc[doc].append((int(meta.get("chunk_index") or 0), row["content"]))
            doc_meta[doc] = meta
        lines = ["---", "type: email", f"title: {_safe(str(title))}", f"date: {_safe(date)}",
                 f"thread_id: {_safe(thread_id)}", "source: feedai-email", "---", "", f"# {redact(title)}", ""]
        for doc, chunks in sorted(by_doc.items(), key=lambda item: str(doc_meta[item[0]].get("date") or "")):
            meta = doc_meta[doc]
            lines += [f"## {redact(meta.get('date') or '')} · {redact(meta.get('from') or '')}", "",
                      f"- To: {redact(meta.get('to') or '')}", f"- Gmail ID: `{redact(meta.get('gmail_id') or '')}`", "",
                      merge_chunks(chunks), ""]
        out["emails/" + _file_name(thread_id).lower()] = dict(
            title=str(title).strip(), text="\n".join(lines),  # 大脑的页面标题去掉了首尾空白
            frontmatter={"date": date, "source": "feedai-email", "thread_id": thread_id},
            newest=max((str(r["meta"].get("updated_at") or "") for r in rows), default="") or None)
    return out, count


def ticket_pages(points):
    """FeedAI 的 HubSpot 工单 → {slug: 工单页}（格式照搬大脑 feedai_qdrant_sync.sync_hubspot）。"""
    tickets = {}
    count = 0
    for point in points:
        count += 1
        payload = point.get("payload") or {}
        ticket = payload.get("ticket") or {}
        ident = str(ticket.get("id") or ticket.get("hs_object_id") or point["id"])
        tickets[ident] = ticket
    out = {}
    for ident, ticket in tickets.items():
        title = ticket.get("subject") or f"HubSpot ticket {ident}"
        created = str(ticket.get("hs_created_at") or "")
        updated = str(ticket.get("hs_updated_at") or "")
        date = updated or created
        owner = ticket.get("owner") or {}
        lines = [
            "---", "type: note", f"title: {_safe(str(title))}", f"date: {_safe(date)}",
            f"hubspot_created_at: {_safe(created)}", f"hubspot_updated_at: {_safe(updated)}",
            f"hubspot_ticket_id: {_safe(ident)}", "tags: [hubspot, ticket]", "source: feedai-hubspot",
            "---", "", f"# {redact(title)}", "",
            f"- Status: {redact(ticket.get('status') or '')}",
            f"- Priority: {redact(ticket.get('ticket_priority_advanced') or ticket.get('ticket_priority') or '')}",
            f"- Pipeline: {redact(ticket.get('pipeline') or '')}",
            f"- Stage: {redact(ticket.get('pipeline_stage') or '')}",
            f"- Owner: {redact((str(owner.get('first_name') or '') + ' ' + str(owner.get('last_name') or '')).strip())}",
            f"- Created (建单时间, UTC): {redact(created)}",
            f"- Updated (更新时间, UTC): {redact(updated)}",
            "", "## Content", "", redact(ticket.get("content") or ""),
        ]
        notes = ticket.get("notes") or []
        if notes:
            lines += ["", "## Notes", ""]
            for note in notes:
                lines.append("- " + redact(note if isinstance(note, str) else json.dumps(note, ensure_ascii=False, sort_keys=True)))
        out["tickets/" + _file_name(ident).lower()] = dict(
            title=str(title).strip(), text="\n".join(lines) + "\n",  # 大脑的页面标题去掉了首尾空白
            frontmatter={"date": date, "source": "feedai-hubspot", "hubspot_ticket_id": ident,
                         "hubspot_created_at": created, "hubspot_updated_at": updated},
            newest=updated or created or None)
    return out, count


def _write_pages(kind, pages, started):
    """写邮件 / 工单副本（字段同 raw/brain_* 缓存）；内容没变的不动。上游已经没有的删掉。→ (改动数, 删除数)"""
    d = kind_dir(kind)
    written, keep = 0, set()
    for slug, pg in pages.items():
        name = safe_name(slug) + ".json"
        keep.add(name)
        body = _body(pg["text"])
        have = _read(d / name)
        if have and have.get("compiled_truth") == body and have.get("title") == pg["title"] \
                and have.get("frontmatter") == pg["frontmatter"]:
            continue
        write_json(d / name, dict(queried_at=started, source="feedai", slug=slug, title=pg["title"],
                                  updated_at=pg["newest"], frontmatter=pg["frontmatter"], compiled_truth=body))
        written += 1
    removed = 0
    if d.exists():
        for path in d.glob("*.json"):
            if path.name not in keep:
                path.unlink()
                removed += 1
    return written, removed


def cached(kind):
    """邮件 / 工单副本，全部读出来。"""
    d = kind_dir(kind)
    return [x for x in (_read(p) for p in sorted(d.glob("*.json"))) if x] if d.exists() else []


def refresh_emails(st, full=False, log=print):
    started = now_z()
    threads, points = email_threads(scroll(EMAIL_COLLECTION))
    if not threads:
        raise RuntimeError("FeedAI 一封邮件都没返回，不动已有副本")
    written, removed = _write_pages("emails", threads, started)
    people = None
    if written or removed or not directory_path().exists():  # 通讯录（认出 Slack 里哪些人是公司外的）：邮件没变就不重算（全读一遍要半分钟）
        from . import brainpages  # 循环引用，用到时再导入
        people = brainpages.build_directory(pages=cached("emails"), path=directory_path())
    newest = max((t["newest"] for t in threads.values() if t["newest"]), default=None)
    log(f"邮件：{points} 条 → {len(threads)} 个线程，改动 {written}，删除 {removed}")
    return dict(points=points, pages=len(threads), written=written, removed=removed, newest=newest, directory=people)


def refresh_tickets(st, full=False, log=print):
    started = now_z()
    tickets, points = ticket_pages(scroll(TICKET_COLLECTION, payload={"include": ["ticket"]}))
    if not tickets:
        raise RuntimeError("FeedAI 一张工单都没返回，不动已有副本")
    written, removed = _write_pages("tickets", tickets, started)
    newest = max((t["newest"] for t in tickets.values() if t["newest"]), default=None)
    log(f"工单：{len(tickets)} 张，改动 {written}，删除 {removed}")
    return dict(points=points, pages=len(tickets), written=written, removed=removed, newest=newest)


def tickets_matching(relevant):
    """正文（含标题）让 relevant() 返回真的工单副本，最近更新的在前。"""
    out = [pg for pg in cached("tickets") if relevant((pg.get("title") or "") + "\n" + (pg.get("compiled_truth") or ""))]
    out.sort(key=lambda pg: pg.get("updated_at") or "", reverse=True)
    return out


# ───────────────────────── 会议 ─────────────────────────
def _meeting_index():
    return (_read(meeting_index_path()) or {}).get("meetings") or {}


def _save_meeting_index(idx):
    write_json(meeting_index_path(), {"saved_at": now_z(), "meetings": idx})


def _store_meeting(idx, key, *, slug, title, when, host, origin, source_id, text, fid=None, sentences=True):
    """写一场会议的副本（字段同 raw/meetings 缓存）并更新清单。→ new / changed / same"""
    title = (title or "").strip()  # 大脑的页面标题去掉了首尾空白（会议记录按标题认，差一个空格就当成新的）
    old = idx.get(key)
    name = safe_name(slug) + ".json"
    if old and old.get("file") and old["file"] != name:  # 同一场会换了来源或标题：旧文件删掉
        (kind_dir("meetings") / old["file"]).unlink(missing_ok=True)
    path = kind_dir("meetings") / name
    body = _body(text)
    have = _read(path)
    now = now_z()
    if have and have.get("compiled_truth") == body and have.get("title") == title:
        state = "same"
    else:
        write_json(path, dict(queried_at=now, slug=slug, title=title, updated_at=now, source_id=source_id,
                              origin=origin, datetime=when or None, host=host or None, fireflies_id=fid,
                              compiled_truth=body))
        state = "changed" if have else "new"
    m = _DAY.search(slug)
    idx[key] = dict(slug=slug, file=name, origin=origin, fid=fid, date=m.group(0) if m else "",
                    queried_at=now, sentences=sentences)
    return state


def _ff_list(days):
    """Fireflies 直连能看到的会（days=None 是全部历史），自动翻页。"""
    frm = to = None
    if days:
        now = dt.datetime.now(dt.timezone.utc)
        frm = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows, skip = [], 0
    while True:
        v = {"limit": 50, "skip": skip}
        if frm:
            v["fromDate"], v["toDate"] = frm, to
        page = _gql(LIST_QUERY, v).get("transcripts") or []
        rows += page
        if len(page) < 50:
            return rows
        skip += 50
        time.sleep(FIREFLIES_SLEEP)


def _direct_fireflies(idx, backfill, tally, log):
    """Fireflies 直连（qa@feedmob.com 这个账号看得到的会）：新的、最近几天要复查的、本地只有补来版本的，取详情重写。"""
    rows = _ff_list(None if backfill else FIREFLIES_DAYS)
    tally["fireflies_listed"] = len(rows)
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=FIREFLIES_DAYS)).strftime("%Y-%m-%d")
    for i, r in enumerate(rows, 1):
        fid = r.get("id")
        if not fid:
            continue
        have = idx.get(fid)
        if have and have.get("origin") == "fireflies" and have.get("sentences"):
            if (have.get("date") or "") < recent or _age(have.get("queried_at")) < FIREFLIES_RECHECK:
                continue
        t = _gql(DETAIL_QUERY, {"transcriptId": fid}).get("transcript")
        time.sleep(FIREFLIES_SLEEP)
        if not t:
            tally["fireflies_missing"] += 1
            continue
        _, text = render_page(t)
        date_iso = (t.get("dateString") or "")[:10] or "1970-01-01"
        title = t.get("title") or "untitled"
        tally["fireflies_" + _store_meeting(
            idx, fid, slug=f"meetings/{date_iso}-{slugify(title)}-{fid[-6:].lower()}", title=title.replace('"', "'"),
            when=t.get("dateString") or "", host=t.get("host_email") or "", origin="fireflies", source_id="fireflies",
            text=text, fid=fid, sentences=bool(t.get("sentences")))] += 1
        if i % 50 == 0:
            _save_meeting_index(idx)
            log(f"  Fireflies 直连：{i}/{len(rows)}")


def _fill_meetings(idx, st, backfill, tally, log):
    """FeedAI、Femini 里有、本地还没有的会补进来（规则同大脑 extra_feedai.export_meetings：Femini 有逐句就用 Femini，
    否则用 FeedAI 的文本；摘要取 FeedAI）。补过的不再改（和大脑一致）。"""
    feedai = scan_feedai_fireflies()
    fm = Femini(_femini_token())
    if backfill or not st.get("femini_backfilled"):
        first_id, gap = 1, MEETING_GAP_STOP
    else:
        first_id = max(1, max(st.get("femini_max_id") or 0, KNOWN_MAX_MEETING_ID) - MEETING_LOOKBACK)
        gap = MEETING_DAILY_GAP_STOP
    femini, scanned = scan_femini_meetings(fm, first_id, gap)
    st["femini_max_id"] = max(st.get("femini_max_id") or 0, scanned["max_id"])
    tally.update(feedai_meetings=len(feedai), femini_meetings=len(femini), femini_bad_records=scanned["bad"])
    targets = sorted((f for f in set(feedai) | set(femini) if f not in idx),
                     key=lambda f: (feedai.get(f, {}).get("date") or femini.get(f, {}).get("meeting_date") or ""),
                     reverse=True)
    tally["fill_targets"] = len(targets)
    for i, fid in enumerate(targets, 1):
        ref, rec = feedai.get(fid), femini.get(fid)
        try:
            detail = None
            if rec and (rec.get("utterance_count") or 0) > 0:
                detail = fm.call("get_fireflies_meeting", {"id": rec["id"]}).get("fireflies_meeting") or {}
                if not detail.get("utterances"):
                    detail = None
            summary = transcript = ""
            missing = 0
            if ref:
                summary, transcript, missing = fetch_feedai_text(ref, summary_only=detail is not None)
        except FeminiFatal:
            raise
        except Exception as e:  # noqa: BLE001 - 单场失败不影响其它
            tally["fill_error"] += 1
            log(f"  会议 {fid} 取数失败：{e!r}"[:200])
            continue
        title = (ref or {}).get("title") or (rec or {}).get("title") or ""
        date_iso = utc_iso((ref or {}).get("date")) or utc_iso((rec or {}).get("meeting_date"))
        duration = (ref or {}).get("duration")
        if duration is None and rec:
            duration = rec.get("duration")
        body = []
        if summary:
            body += ["## Summary", "", summary, ""]
        if detail:
            body += ["---", "", "## 全文转录", ""] + femini_lines(detail.get("utterances") or []) + [""]
            participants, origin, timed = rec.get("participants") or [], "femini-fireflies", True
        elif transcript.strip():
            body += ["---", "", "## 全文转录（FeedAI 文本，无逐句时间）", "", transcript, ""]
            if missing:
                body += [f"[note: {missing} transcript chunk(s) were not available in FeedAI]", ""]
            participants, origin, timed = (rec or {}).get("participants") or [], "feedai-fireflies", False
        else:
            tally["fill_empty"] += 1
            continue
        text = _truncate(meeting_page(fid=fid, title=title, date_iso=date_iso, duration=duration, origin=origin, body=body,
                                      participants=participants, timed=timed, femini_id=(rec or {}).get("id")))
        tally["fill_" + _store_meeting(
            idx, fid, slug=f"meetings/{date_iso[:10] or '1970-01-01'}-{cjk_slug(title)}-{fid[-6:].lower()}",
            title=one_line(title or "untitled").translate(_LINE_SEPS), when=date_iso, host="", origin=origin,
            source_id="fireflies", text=text, fid=fid)] += 1
        if i % 50 == 0:
            _save_meeting_index(idx)
            log(f"  补会议：{i}/{len(targets)}")


def _gong(idx, tally):
    """历史 Gong 通话（FeedAI 里的存量，不再增加）。"""
    calls = defaultdict(list)
    for point in scroll(GONG_COLLECTION, batch=200):
        payload = point.get("payload") or {}
        meta = payload.get("metadata") or {}
        call_id = str(meta.get("call_id") or "")
        if call_id:
            calls[call_id].append((int(meta.get("chunk_index") or 0), str(payload.get("page_content") or ""), meta))
    tally["gong_calls"] = len(calls)
    for call_id in sorted(calls):
        name, text, start, title = render_gong(call_id, calls[call_id])
        tally["gong_" + _store_meeting(
            idx, f"gong:{call_id}", slug="gong/" + name[:-3], title=one_line(title).translate(_LINE_SEPS), when=start,
            host="", origin="gong", source_id="gong", text=_truncate(text))] += 1


def refresh_meetings(st, full=False, log=print):
    backfill = full or not st.get("fireflies_backfilled")
    if backfill and not full:
        raise RuntimeError("会议历史还没拉过：先在服务器上跑 python3 -m replay.feeds refresh --kinds meetings --full")
    idx = _meeting_index()
    tally, errors = Counter(), []
    direct_ok = False
    try:
        _direct_fireflies(idx, backfill, tally, log)
        direct_ok = True
        if backfill:
            st["fireflies_backfilled"] = True
    except Exception as e:  # noqa: BLE001 - 记下，先把已拉到的存住
        errors.append(f"Fireflies 直连：{str(e)[:150]}")
    _save_meeting_index(idx)
    # 补全公司的会要排在直连之后：直连失败时先不补，免得直连能拿到的会被别的版本先占上（和大脑的先后一致）
    if direct_ok or st.get("fireflies_backfilled"):
        try:
            _fill_meetings(idx, st, backfill, tally, log)
            if backfill or not st.get("femini_backfilled"):
                st["femini_backfilled"] = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"补会议（FeedAI / Femini）：{str(e)[:150]}")
        _save_meeting_index(idx)
    if backfill or not st.get("gong_done"):
        try:
            _gong(idx, tally)
            st["gong_done"] = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"Gong：{str(e)[:150]}")
        _save_meeting_index(idx)
    log(f"会议：共 {len(idx)} 场；" + "，".join(f"{k} {v}" for k, v in sorted(tally.items())))
    if errors:
        raise RuntimeError("；".join(errors))
    newest = max((v.get("date") or "" for v in idx.values() if v.get("origin") != "gong"), default="") or None
    return dict(tally, meetings=len(idx), newest=newest)


def meetings_in(start, end):
    """日期在 [start, end] 的会议（同大脑模式 sync_meetings 的返回形状）。"""
    idx = _meeting_index()
    lo, hi = start.isoformat(), end.isoformat()
    want = [dict(slug=v["slug"], updated_at=v.get("queried_at")) for v in idx.values() if lo <= (v.get("date") or "") <= hi]
    return want, dict(listed=len(idx), in_window=len(want), fetched=0, failed=0, source="direct")


# ───────────────────────── 刷新 ─────────────────────────
RUNNERS = {"slack": refresh_slack, "emails": refresh_emails, "tickets": refresh_tickets, "meetings": refresh_meetings}


@contextlib.contextmanager
def _lock():
    root().mkdir(parents=True, exist_ok=True)
    with open(root() / ".refresh.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _fresh_enough(kind, st):
    if st.get("ok") and _age(st.get("as_of")) < MAX_AGE[kind]:
        return True
    return st.get("ok") is False and _age(st.get("failed_at")) < RETRY_AFTER


def refresh(kinds=None, force=False, full=False, log=print):
    """按需刷新各类副本。→ {类别: ok / skipped / error}"""
    out = {}
    with _lock():  # 别的刷新在跑就等它跑完；跑完后多半已经够新，直接跳过
        state = read_state()
        for kind in kinds or KINDS:
            st = dict(state.get(kind) or {})
            if not (force or full) and _fresh_enough(kind, st):
                out[kind] = "skipped"
                continue
            started, t0 = now_z(), time.time()
            try:
                info = RUNNERS[kind](st, full=full, log=log)
                st.update(info)
                st.update(ok=True, as_of=started, finished_at=now_z(), seconds=round(time.time() - t0, 1), error=None)
                out[kind] = "ok"
            except Exception as e:  # noqa: BLE001 - 一类失败不影响其它类，已有副本不动
                st.update(ok=False, failed_at=now_z(), seconds=round(time.time() - t0, 1),
                          error=f"{type(e).__name__}: {str(e)[:300]}")
                out[kind] = "error"
                log(f"{kind} 刷新失败：{st['error']}")
            state[kind] = st
            write_json(state_path(), state)
    return out


def ensure_fresh(log=print, timeout=REFRESH_TIMEOUT):
    """流水线取数前调用：在子进程里刷新一次（够新就跳过；内存随子进程退出释放）。刷新失败或超时就先用已有副本，
    并在返回里写明；副本从没初始化过才报错（不能拿不全的数据生成页面）。"""
    if not ready():
        raise RuntimeError("直连数据还没有初始化：先在服务器上跑 python3 -m replay.feeds refresh --full")
    warning = None
    try:
        proc = subprocess.run([sys.executable, "-m", "replay.feeds", "refresh"], cwd=str(util.ROOT),
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            warning = ((proc.stderr or proc.stdout) or "").strip()[-300:]
    except subprocess.TimeoutExpired:
        warning = f"刷新超过 {timeout // 60} 分钟，先用已有副本"
    st = read_state()
    info = {k: dict(as_of=(st.get(k) or {}).get("as_of"), ok=(st.get(k) or {}).get("ok")) for k in KINDS}
    failed = [k for k in KINDS if info[k]["ok"] is False]
    if warning or failed:
        info["warning"] = warning or ("刷新失败的来源：" + "、".join(failed) + "（先用已有副本）")
        log(info["warning"])
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description="回放的直连数据副本（FeedAI / Fireflies / Femini）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("refresh", help="刷新副本（够新的跳过）")
    p.add_argument("--kinds", default=",".join(KINDS), help="逗号分隔：" + ", ".join(KINDS))
    p.add_argument("--force", action="store_true", help="不管多新都刷新")
    p.add_argument("--full", action="store_true", help="全量：Slack 全部重读，会议把历史全部拉一遍（第一次用）")
    sub.add_parser("status", help="看各类副本数据截至几点、有没有出错")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(dict(enabled=enabled(), ready=ready(), state=read_state()), ensure_ascii=False, indent=1))
        return 0
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        parser.error("未知类别：" + ", ".join(bad))
    out = refresh(kinds, force=args.force, full=args.full, log=lambda *a: print(*a, flush=True))
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 1 if "error" in out.values() else 0


if __name__ == "__main__":
    sys.exit(main())
