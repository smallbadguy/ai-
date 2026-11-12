"""回放服务：默认只监听本机端口，经 SSH 隧道访问。

- GET  /                    首页：下拉选择活动、已生成的活动列表、刷新状态
- GET  /c/<活动编号>/en|zh      业务版（AI 整理；还没生成时给出说明页）
- GET  /c/<活动编号>/en|zh/detail  详细版（程序自动生成，含工单与核对信息）
- GET  /c/<活动编号>/en|zh/progress  旧地址：跳到业务版里的「进行中事项」页签（没有业务版就提示还没生成）
- GET  /healthz            健康检查（始终不需要登录）
- GET  /auth/login 等       Google 登录（只有设了 REPLAY_AUTH=google 才启用，见 service/auth.py）
- GET  /showcase/chime/en|zh   人工精选版，只读取独立发布的固定页面
- GET  /c/<活动编号>/status     该活动的刷新状态（JSON）
- GET  /status              所有活动的状态（JSON）
- GET  /rows.json           首页「已生成的活动」表格的最新几行（JSON；首页有处理中的活动时每 5 秒取一次）
- POST /add                 选好活动后立即取数生成（活动必须在下拉清单里）
- POST /c/<活动编号>/refresh    刷新一个活动
- GET  /queue               AI 任务队列状态页（处理方、各活动的任务、每道题的经过；要登录）；/queue/<题号> 一道题；/queue.json
- /agent-api/v1/…          外部 agent 领题、交答案的接口（不走 Google 登录，用各自的钥匙），见 service/agent_api.py
异常排查（单独页面 /c/<活动编号>/diagnose，入口在首页每一行；回放页面里不放；按活动排查，见 replay/diagnose.py、service/diagnose_ui.py）：
- GET  /c/<活动编号>/diagnose                   排查页面
- GET  /c/<活动编号>/diagnose/runs              本活动的排查记录
- GET  /c/<活动编号>/diagnose/run/<日期>/<编号>  一次排查的结果（页面轮询）
- GET  /c/<活动编号>/diagnose/scan              最近 60 天有信号的日期（不用 AI，缓存 1 小时）
- POST /c/<活动编号>/diagnose                   开始排查：date（必选）、q（描述，可选）、range（1/3/7/14 天）
- POST /c/<活动编号>/diagnose/run/<日期>/<编号>/verify  人工验证一条结论：index、verdict（consistent / partial / contradict）、note（可选）
排查完的结论在排查日后第 3 / 7 / 14 / 30 天回访：后台自动回访默认关（REPLAY_DIAG_FOLLOWUP=1 才开，开了每 30 分钟看一次到期的）；关着时手动 python3 -m replay diagnose-followup。
人工验证过的结论，回访时 MiniMax 不再验证；每条结论都有人工验证后不再回访。
- GET  /diagnose/dict                          字典、统计、回归集、改动记录

环境变量：REPLAY_PORT（默认 8791）、REPLAY_BIND（默认 127.0.0.1）；登录相关见 service/auth.py。
所有取数都是只读；刷新在后台线程里跑，同时最多跑 2 个编号；排查在后台线程里跑，同时最多 2 个。
"""
import fcntl
import html
import http.server
import json
import os
import pathlib
import re
import sys
import threading
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from replay import campaign, curate, diagnose, jobqueue, knowledge, pipeline, scope  # noqa: E402
from replay.util import OUT, STATE, camp_dir, out_dir, read_json, valid_number  # noqa: E402
from service import agent_api, auth, diagnose_ui, queue_page, vendor_ui  # noqa: E402
from service import versions as version_sites  # noqa: E402
from service import ai_status  # noqa: E402
from service.ratelimit import RateLimiter  # noqa: E402

AUTH = auth.Auth(auth.load_config())
CURATED_ROOT = pathlib.Path(os.environ.get("REPLAY_CURATED_ROOT", str(ROOT / "showcase")))

RUNNING = set()
# campaign id -> {vendor key or "" for all channels: "queued" | "running"}
# RUNNING remains the base campaign-id set for compatibility with old callers.
RUNNING_VENDORS = {}
LOCK = threading.Lock()
SLOTS = threading.Semaphore(2)
LIMITS = RateLimiter()
LIST_LOADING = {"on": False, "error": None}


def ensure_list():
    """活动清单不存在或超过一天就在后台重新取；同时只取一次。"""
    with LOCK:
        if LIST_LOADING["on"] or not campaign.list_is_stale():
            return
        LIST_LOADING["on"] = True

    def work():
        try:
            campaign.refresh_list()
            LIST_LOADING["error"] = None
        except Exception as e:
            LIST_LOADING["error"] = str(e)[:200]
        finally:
            LIST_LOADING["on"] = False

    threading.Thread(target=work, daemon=True).start()


def _root_lock_busy(n):
    """活动锁由pipeline持有，所有vendor共用；尊重其他服务进程的锁。"""
    path = STATE / f"{valid_number(n)}.lock"
    if not path.exists():
        return False
    with open(path, "a") as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(lockfile, fcntl.LOCK_UN)
            return False


def running_phase(n, vendor=None, state=None):
    """当前服务线程的排队/运行状态；也识别持有活动锁的外部pipeline。"""
    key = vendor or ""
    with LOCK:
        variants = RUNNING_VENDORS.get(n)
        if variants is not None:
            return variants.get(key)
    if state and state.get("running") and _root_lock_busy(n):
        return "running"
    return None


def campaign_busy(n):
    """任何vendor占用同一个campaign锁时都不应再启动第二个任务。"""
    with LOCK:
        if n in RUNNING:
            return True
    return _root_lock_busy(n)


def start_refresh(n, vendor=None):
    """后台刷新一个活动scope；同campaign的其他scope不能并发运行。"""
    n = valid_number(n)
    vendor = vendor_ui.normalize_vendor(vendor)
    with LOCK:
        if n in RUNNING:
            return False
        reserved = pipeline.reserve(n)
        if reserved is None:
            return False
        RUNNING.add(n)
        RUNNING_VENDORS[n] = {vendor or "": "queued"}

    def work():
        try:
            with SLOTS:
                with LOCK:
                    if n in RUNNING_VENDORS:
                        RUNNING_VENDORS[n][vendor or ""] = "running"
                vendor_ui.refresh_pipeline(n, vendor, reserved_lock=reserved)
        finally:
            with LOCK:
                RUNNING.discard(n)
                RUNNING_VENDORS.pop(n, None)
            fcntl.flock(reserved, fcntl.LOCK_UN)
            reserved.close()

    try:
        threading.Thread(target=work, daemon=True).start()
    except BaseException:
        with LOCK:
            RUNNING.discard(n)
            RUNNING_VENDORS.pop(n, None)
        fcntl.flock(reserved, fcntl.LOCK_UN)
        reserved.close()
        raise
    return True


DIAG_RUNNING = set()
DIAG_PAGE = re.compile(r"/c/(\d{1,9})/diagnose")  # 同一组参数正在跑就不重复开；同时跑几个不设上限（用户定）


def start_diagnose(cid, date, query, rng):
    """建排查记录并在后台跑；同一组参数正在跑就不重复开。→ 记录"""
    run, _ = diagnose.prepare(cid, date, query, rng)
    key = (cid, run["date"], run["id"])
    with LOCK:  # 记录显示没跑完、但本进程里没在跑（比如服务重启过）→ 重新开跑
        if key in DIAG_RUNNING:
            return run
        DIAG_RUNNING.add(key)

    def work():
        try:
            diagnose.run(cid, run["date"], run["id"], log=lambda *_: None)
        finally:
            with LOCK:
                DIAG_RUNNING.discard(key)

    threading.Thread(target=work, daemon=True).start()
    return run


FOLLOWUP_EVERY = 1800


def followup_on():
    """后台自动回访和字典自动整理会自己调 AI：默认关（用户定：不自动跑 AI），REPLAY_DIAG_FOLLOWUP=1 才开。
    关着时也可以手动跑：python3 -m replay diagnose-followup / dict-curate。"""
    return pipeline.feeds.setting("REPLAY_DIAG_FOLLOWUP", "0") == "1"


def followup_loop():
    """巡检：到期的排查自动回访（找排查日之后的后台改动、沟通记录、GitHub，验证还没有依据的结论并学习），再整理字典。"""
    import time
    while True:
        try:
            for cid, date, rid in diagnose.due_followups():
                diagnose.followup(cid, date, rid, log=lambda *_: None)
            curate.maybe(log=lambda *_: None)   # 字典自动整理（候选有变化且距上次 ≥ 6 小时才真的跑）
        except Exception as e:
            sys.stderr.write(f"followup: {str(e)[:200]}\n")
        time.sleep(FOLLOWUP_EVERY)


def public_run(r):
    """给页面的排查结果：去掉内部字段。"""
    return {k: v for k, v in (r or {}).items() if not k.startswith("_") and k != "trace"}


def dictionary_payload():
    doc = knowledge.load()
    return dict(summary=knowledge.summary(doc), categories=doc.get("categories"), entries=doc["entries"],
                regression=read_json(knowledge.regression_path()) or {"runs": []}, changelog=knowledge.changelog(40))


NAV_CSS = (
    "#rp-nav{position:fixed;right:12px;top:3px;z-index:2147483000;display:flex;gap:2px;align-items:center;"
    "padding:3px;border-radius:14px;background:linear-gradient(170deg,#141126,#0B0A15);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08),0 12px 40px rgba(0,0,0,.35);"
    "font:700 12px/1 var(--ui,system-ui,-apple-system,'Segoe UI',sans-serif);max-width:calc(100vw - 16px);overflow-x:auto;white-space:nowrap}"
    "#rp-nav a{color:#ABA5C0;text-decoration:none;padding:7px 11px;border-radius:11px}"
    "#rp-nav a:hover{background:rgba(255,255,255,.07);color:#F4F2FA}"
    "#rp-nav a.cur{background:#6A4AF2;color:#fff;box-shadow:0 6px 20px rgba(124,92,255,.35)}"
    "#rp-nav i{width:1px;height:14px;background:rgba(255,255,255,.15);margin:0 4px}"
)

# 导航条不占版面：贴在右侧，竖向对齐到页面里的一行空位（业务版是顶栏，详细版是「回放状态」那一行）
NAV_JS = (
    "(function(){var n=document.getElementById('rp-nav'),a=document.querySelector('.app');if(!n||!a)return;"
    "var row=a.classList.contains('presentation-mode')?document.getElementById('top'):(document.getElementById('charter')||document.getElementById('top'));"
    "if(!row||!row.offsetHeight)return;"
    # 这一行收窄到导航条左边为止（用外边距不用内边距：放不下时内容在行内收起，不会钻到导航条底下）
    "function place(){row.style.marginRight='0px';var r=row.getBoundingClientRect();"
    "row.style.marginRight=Math.max(0,r.right-n.getBoundingClientRect().left+10)+'px';"
    "n.style.top=Math.max(2,r.top+(Math.min(r.height,48)-n.offsetHeight)/2)+'px'}"  # 顶栏折成两行时（活动切换在第二行）对齐第一行
    "place();addEventListener('resize',place);setTimeout(place,400)})();"
)


def scoped_out_dir(n, vendor=None):
    """Resolve a page directory without changing legacy root paths."""
    if vendor is None:
        return OUT / valid_number(n)
    return scope.path(OUT / valid_number(n), n, vendor)


def scoped_page(n, vendor, filename):
    return scoped_out_dir(n, vendor) / filename


def click_urls(n):
    """活动下的 click URL ID。业务同事认的是它（后台里通常一个活动一条），不认后台的活动编号。还没取过活动信息时为空。"""
    return [str(x) for x in (campaign.info(n) or {}).get("links") or []]


def built_campaigns():
    """已经生成了页面的活动（页面顶部的活动切换栏用）：编号、名称、click URL ID、有没有业务版 / 详细版。按名称排。"""
    names = {str(c["id"]): c["name"] for c in campaign.load_list().get("campaigns", [])}
    out = []
    for n in pipeline.known_numbers():
        root = scoped_out_dir(n)
        business, detail = (root / "business.en.html").exists(), (root / "replay.en.html").exists()
        if business or detail:
            out.append(dict(id=n, name=names.get(n) or known_name(n) or n, links=click_urls(n), business=business, detail=detail))
    return sorted(out, key=lambda c: (c["name"].casefold(), c["id"]))


def known_name(n):
    """不在后台「在用」清单里的活动（如已停用）用上次记下的名称，页面顶部的活动切换栏才不会只显示编号。"""
    return pipeline.read_state(n).get("campaign") or ((campaign.info(n) or {}).get("campaign") or {}).get("name") or ""


# 后台已停用的活动：首页那一行、两版页面顶栏的活动名后面都标「已停用」，悬停看说明（2026-11-01 用户要求：停用了要有提示）。
# 状态取自后台最近一次的活动信息（campaign.inactive），靠文字区分，不只靠颜色。
STOPPED = {
    "zh": ("已停用", "FeedMob Admin 后台已停用此活动。回放保留全部历史记录，刷新时仍会补入新的沟通记录。"),
    "en": ("Deactivated", "This campaign is deactivated in FeedMob Admin. The replay keeps its full history, and refreshing still adds new communications."),
}
STOP_CSS = (".rp-stop{flex:none;font-size:11px;font-weight:700;line-height:16px;padding:1px 8px;border-radius:99px;"
            "color:#E7BA52;box-shadow:inset 0 0 0 1px rgba(231,186,82,.55);white-space:nowrap;cursor:help}")


def stop_tag(n, lang):
    """页面顶栏的「已停用」：页面是生成好的独立 HTML，返回时在活动名后面补上——已生成的页面不用重做，状态跟着后台走。"""
    if not campaign.inactive(n):
        return ""
    label, tip = STOPPED["zh" if lang == "zh" else "en"]
    js = ("(function(){var b=document.querySelector('#top .room b');if(!b||document.querySelector('#top .rp-stop'))return;"
          "var s=document.createElement('span');s.className='rp-stop';s.textContent=" + json.dumps(label, ensure_ascii=False)
          + ";s.title=" + json.dumps(tip, ensure_ascii=False) + ";b.insertAdjacentElement('afterend',s)})();")
    return "<style>" + STOP_CSS + "</style><script>" + js.replace("</", "<\\/") + "</script>"


def stopped(n, z):
    """首页那一行活动名后面的「已停用」。"""
    if not campaign.inactive(n):
        return ""
    label, tip = STOPPED["zh" if z else "en"]
    return f'<span class="stop" title="{esc(tip)}">{label}</span>'


def nav_bar(n, lang, current, vendor=None):
    """每个页面右上角的导航条：首页 / 业务版 / 详细版 / 中英文切换。只列已经生成的页面。
    进行中事项不在这里单列：业务版里本来就有这个页签，底部条也有按钮。"""
    z = lang == "zh"
    other = "en" if z else "zh"
    suffix = {"business": "", "detail": "/detail"}
    query = vendor_ui.scope_query(vendor)
    root = scoped_out_dir(n, vendor)
    items = [(f"/?lang={lang}", "⌂ 首页" if z else "⌂ Home", False)]
    if (root / "business.en.html").exists():
        items.append((f"/c/{n}/{lang}{query}", "业务版" if z else "Business", current == "business"))
    if (root / "replay.en.html").exists():
        items.append((f"/c/{n}/{lang}/detail{query}", "详细版" if z else "Detail", current == "detail"))
    links = "".join(f'<a href="{href}"{" class=cur" if cur else ""}>{label}</a>' for href, label, cur in items)
    switch = f'<i></i><a href="/c/{n}/{other}{suffix[current]}{query}">{"English" if z else "中文"}</a>'
    return f'<nav id="rp-nav"><style>{NAV_CSS}</style>{links}{switch}</nav><script>{NAV_JS}</script>'


def with_nav(page, n, lang, current, vendor=None):
    """把导航条放进页面。页面本身是独立的 HTML，没有 </body> 时原样返回。

    只加原有的导航条和「已停用」标记，不往回放页面里加别的东西（2026-11-02 用户要求：回放页面的界面不能改，
    曾经加在顶栏下面的状态栏已撤掉；刷新在首页那一行）。"""
    text = page.decode("utf-8") if isinstance(page, bytes) else page
    i = text.rfind("</body>")
    if i < 0:
        return page
    return (text[:i] + stop_tag(n, lang) + nav_bar(n, lang, current, vendor) + text[i:]).encode("utf-8")


def esc(x):
    return html.escape(str(x if x is not None else ""))


def fmt_utc(ts):
    """给人看的时间：2026-11-01T09:08:19Z → 2026-11-01 09:08 UTC（不露 ISO 格式）。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", str(ts or ""))
    return f"{m.group(1)} {m.group(2)} UTC" if m else (esc(ts) + " UTC" if ts else "—")


FIRST_RUN = {"zh": "首次生成要先让 AI 通读全部记录，通常需要 10 分钟到 1 个半小时（记录越多越久）。",
             "en": "The first build has the AI read every record, usually 10 minutes to 1.5 hours (more records take longer)."}
# 2026-11-02 用户定：stage 不再每天自动更新，首页也不再有「刷新全部」；要看最新内容就点那个活动的「刷新」（实测一个活动平均十几分钟）
MANUAL_REFRESH = {"zh": "回放不会自动更新：需要最新内容时，点该活动的「刷新」，通常 10 到 30 分钟。",
                  "en": "Replays do not update automatically. Press Refresh on a campaign when you need the latest; it usually takes 10 to 30 minutes."}


# 首页轮询：有处理中的行时每 5 秒刷新表格；后台标签页暂停轮询。
POLL_JS = """(() => {
  const body = document.getElementById('rows'), count = document.getElementById('built-count');
  let timer = 0;
  const schedule = () => { if (!timer && body.querySelector('tr[data-running]')) timer = setTimeout(poll, 5000); };
  function poll() {
    timer = 0;
    if (document.hidden) return;
    fetch('/rows.json?lang=__LANG__', {cache: 'no-store'})
      .then(r => r.status === 401 ? null : r.ok ? r.json() : {})
      .then(d => { if (d === null) return; if (d.rows) { body.innerHTML = d.rows; count.textContent = d.count; } schedule(); }, schedule);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
  schedule();
})();"""

# 首页样式：组件用 Bootstrap 5.3（service/static，本服务器提供，不连外网）；这里只调品牌色和深色背景。回放页面不用它。
HOME_CSS = (
    ":root,[data-bs-theme=dark]{--bs-body-bg:#0c0d18;--bs-body-bg-rgb:12,13,24;--bs-body-color:#e8e6f2;"
    "--bs-secondary-color:rgba(232,230,242,.62);--bs-border-color:rgba(255,255,255,.1);--bs-primary:#7c5cff;"
    "--bs-primary-rgb:124,92,255;--bs-link-color:#b9a9ff;--bs-link-color-rgb:185,169,255;--bs-link-hover-color:#ddd4ff;"
    "--bs-link-hover-color-rgb:221,212,255;--bs-focus-ring-color:rgba(124,92,255,.35)}"
    "body{background:radial-gradient(70% 45% at 50% -8%,rgba(124,92,255,.16),transparent 70%),var(--bs-body-bg)}"
    ".btn-primary{--bs-btn-bg:#6a4af2;--bs-btn-border-color:#6a4af2;--bs-btn-hover-bg:#5b3de0;--bs-btn-hover-border-color:#5b3de0;"
    "--bs-btn-active-bg:#5034c9;--bs-btn-active-border-color:#5034c9;--bs-btn-disabled-bg:#6a4af2;"
    "--bs-btn-disabled-border-color:#6a4af2;--bs-btn-focus-shadow-rgb:124,92,255}"
    ".btn-outline-primary{--bs-btn-color:#c3b6ff;--bs-btn-border-color:rgba(124,92,255,.55);--bs-btn-hover-color:#fff;"
    "--bs-btn-hover-bg:#6a4af2;--bs-btn-hover-border-color:#6a4af2;--bs-btn-active-bg:#5034c9;"
    "--bs-btn-active-border-color:#5034c9;--bs-btn-disabled-color:rgba(232,230,242,.4);"
    "--bs-btn-disabled-border-color:rgba(255,255,255,.12)}"
    ".card{--bs-card-bg:rgba(20,21,34,.92);--bs-card-border-color:rgba(255,255,255,.08)}"
    ".form-control,.form-select{background-color:#161727}"
    ".table{--bs-table-bg:transparent;--bs-table-hover-bg:rgba(255,255,255,.03)}"
    ".table thead th{font-size:.75rem;font-weight:600;color:var(--bs-secondary-color)}"
    "td small{display:block;margin-top:2px;font-size:.78rem;color:var(--bs-secondary-color)}"
    "td .sep{margin:0 .4rem;color:var(--bs-secondary-color)}.table a{text-decoration:none}.table a:hover{text-decoration:underline}"
    ".brand-mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(145deg,#c4b5fd,#7c5cff 60%,#3d8bff);"
    "display:grid;place-items:center;flex:none}.brand-mark i{width:11px;height:11px;border-radius:3px;background:#fff}"
    ".stop{display:inline-block;margin-left:.5rem;padding:0 .45rem;border-radius:99px;font-size:.72rem;font-weight:600;"
    "line-height:1.3rem;color:#e7ba52;box-shadow:inset 0 0 0 1px rgba(231,186,82,.5);vertical-align:1px;cursor:help}"
    ".count{color:var(--bs-secondary-color);font-size:.85rem}.empty{text-align:center;color:var(--bs-secondary-color);padding:2rem 0}"
)


def version_channel(n, vendor, lang):
    """版本页里「活动 + 渠道」的渠道写法，和首页一致（如 Vizio #23505；整个活动就列 click URL 号）。"""
    if vendor is None:
        return " ".join("#" + x for x in click_urls(n))
    try:
        chan = next((c for c in vendor_ui.options(n) if c["id"] == vendor), None)
    except Exception:
        chan = None
    return vendor_ui.option_label(chan, lang) if chan else f"#{vendor}"


def campaign_names():
    return {str(c["id"]): c["name"] for c in campaign.load_list().get("campaigns", [])}


def ai_tasks(n, st, z):
    """处理中的项目：这次刷新出到外包队列的 AI 任务做完了几道（没走队列就不显示）。"""
    try:
        c = jobqueue.campaign_counts(n, since=st.get("started_at"))
    except Exception:
        return ""
    total = sum(c.values())
    if not total:
        return ""
    text = f"AI 任务 {c['done']}/{total} 已完成" if z else f"AI tasks: {c['done']}/{total} done"
    return f"<small>{text}</small>"


def has_pages(n, vendor=None):
    root = scoped_out_dir(n, vendor)
    return (root / "business.en.html").exists() or (root / "replay.en.html").exists()


def project_scopes(n):
    """一个活动在首页占几行：每个「活动 + 渠道」一行（2026-11-02 用户定，不做「全部渠道」合并版）。
    多渠道的活动每个渠道是一个独立项目（vendor 范围）；只有一个渠道（或还不知道渠道）的活动就是活动本身。"""
    try:
        chans = vendor_ui.options(n)
    except Exception:
        chans = []
    if len(chans) >= 2:
        return [(c["id"], c) for c in chans]
    return [(None, chans[0] if chans else None)]


def projects():
    """首页列出的项目：生成过页面、有运行记录或正在处理的。只留着后台资料、没跑过的不列（清空重来后首页就是空的）。"""
    out = []
    for n in set(pipeline.known_numbers()) | RUNNING:
        for vendor, chan in project_scopes(n):
            status = vendor_ui.scope_status(n, vendor, running_phase, root=scoped_out_dir(n, vendor))
            if status["business"] or status["detail"] or status["state"] or status["phase"]:
                out.append((n, vendor, chan, status))
    return out


def status_cell(n, status, z):
    st = status["state"] or {}
    last, ok = st.get("last"), st.get("last_ok")
    if status["phase"] == "queued":
        return f'<span class="badge text-bg-secondary">{"排队中" if z else "Queued"}</span>'
    if status["running"]:
        return f'<span class="badge text-bg-primary">{"处理中" if z else "Processing"}</span>' + ai_tasks(n, st, z)
    if last and not last.get("ok"):
        note = (("继续显示上次成功的页面" if status["detail"] else "尚无可查看页面") if z
                else ("Showing the last successful page" if status["detail"] else "No page is available yet"))
        return f'<span class="badge text-bg-warning">{"未正常结束" if z else "Did not complete"}</span><small>{note}</small>'
    if ok:
        return f'{"已更新" if z else "Updated"}<small>{fmt_utc(ok.get("finished_at"))}</small>'
    if status["business"] or status["detail"]:
        return "已生成" if z else "Generated"
    return ("尚未生成" if z else "Not generated") + f'<small>{FIRST_RUN["zh" if z else "en"]}</small>'


def project_row(n, vendor, chan, status, name, lang):
    z = lang == "zh"
    sub = vendor_ui.option_label(chan, lang) if chan else " ".join("#" + x for x in click_urls(n))
    query = vendor_ui.scope_query(vendor)

    def edition(ready, suffix):
        if not ready:
            return f'<span class="text-body-secondary">{"尚未生成" if z else "Not ready"}</span>'
        return (f'<a href="/c/{n}/en{suffix}{query}">English</a><span class="sep">·</span>'
                f'<a href="/c/{n}/zh{suffix}{query}">{"中文" if z else "Chinese"}</a>')

    active = bool(status["running"] or status["phase"])
    busy = active or campaign_busy(n)
    title = "" if active or not busy else f' title="{"同一活动的其他渠道正在处理" if z else "Another channel of this campaign is processing"}"'
    btn = (f'<form method="post" action="/c/{n}/refresh" class="m-0"><input type="hidden" name="lang" value="{lang}">'
           f'<input type="hidden" name="vendor" value="{esc(vendor or "")}">'
           f'<button class="btn btn-sm btn-outline-primary"{" disabled" if busy else ""}{title}>{"刷新" if z else "Refresh"}</button></form>')
    diag = f'<a class="small" href="/c/{n}/diagnose?lang={lang}">{"异常排查" if z else "Diagnose"}</a>'
    return (f'<tr{" data-running" if busy else ""}><td><b>{esc(name)}</b>{stopped(n, z)}'
            f'{f"<small>{esc(sub)}</small>" if sub else ""}</td>'
            f'<td>{edition(status["business"], "")}</td><td>{edition(status["detail"], "/detail")}</td>'
            f'<td>{status_cell(n, status, z)}</td><td class="text-end"><div class="d-flex gap-2 justify-content-end align-items-center">{diag}{btn}</div></td></tr>')


def table_rows(lang="zh"):
    """首页「已生成的回放」表格：(行的 HTML, 个数文字)。一行一个「活动 + 渠道」。首页和轮询（/rows.json）共用。"""
    z = lang == "zh"
    names = campaign_names()
    items = []
    for n, vendor, chan, status in projects():
        name = pipeline.read_state(n).get("campaign") or names.get(n) or known_name(n) or n
        busy = bool(status["running"] or status["phase"]) or campaign_busy(n)
        # 正在生成 / 刷新的排在最上面，业务同事不用往下翻找进度（2026-10-31 PA 反馈）；其余按活动名、渠道名排
        items.append(((not busy, name.casefold(), ((chan or {}).get("name") or "").casefold()), n, vendor, chan, status, name))
    items.sort(key=lambda item: item[0])
    rows = "".join(project_row(n, vendor, chan, status, name, lang) for _, n, vendor, chan, status, name in items)
    if not rows:
        rows = f'<tr><td colspan="5" class="empty">{"还没有生成过活动回放。" if z else "No campaign replays have been generated yet."}</td></tr>'
    generated = sum(1 for item in items if item[4]["business"] or item[4]["detail"])
    return rows, f"{generated} 个已生成" if z else f"{generated} available"


def options(camps, lang="zh"):
    """下拉选项；名字重复的活动后面补编号区分。"""
    from collections import Counter
    dup = Counter(c["name"] for c in camps)
    suffix = "（#{}）" if lang == "zh" else " (#{})"
    return [(c["id"], c["name"] if dup[c["name"]] == 1 else c["name"] + suffix.format(c["id"])) for c in camps]


def index_page(msg="", lang="zh", campaign_name="", user=""):
    ensure_list()
    z = lang == "zh"
    lst = campaign.load_list()
    camps = lst.get("campaigns", [])
    t = {
        "title": "Campaign 回放" if z else "Campaign replay",
        "intro": "选择活动和渠道，系统会汇集相关记录并生成中英文回放。" if z else "Choose a campaign and channel to build a bilingual replay from its related records.",
        "select": "选择活动" if z else "Choose a campaign",
        "filter": "按名称筛选活动" if z else "Filter campaigns by name",
        "create": "生成回放" if z else "Build replay",
        "channel": "渠道" if z else "Channel",
        "coverage": vendor_ui.COVERAGE["zh" if z else "en"],
        "list": "已生成的回放" if z else "Generated replays",
        "campaign": "活动 / 渠道" if z else "Campaign / channel",
        "business": "业务版" if z else "Business",
        "detail": "详细版" if z else "Detail",
        "status": "状态" if z else "Status",
        "action": "操作" if z else "Action",
        "count": f"{len(camps)} 个活动" if z else f"{len(camps)} campaigns",
        "list_time": f"活动清单更新于 {fmt_utc(lst.get('queried_at'))}" if z else f"Campaign list updated {fmt_utc(lst.get('queried_at'))}",
        "loading": "活动清单正在加载，请稍后刷新。" if z else "Loading the campaign list. Refresh this page shortly.",
        "no_list": "暂时无法读取活动清单，请稍后重试。" if z else "The campaign list is temporarily unavailable. Please try again shortly.",
        "waiting": FIRST_RUN["zh"] + MANUAL_REFRESH["zh"] if z else FIRST_RUN["en"] + " " + MANUAL_REFRESH["en"],
        "queue": "AI 运行状态" if z else "AI run status",
        "started": f"已开始生成「{esc(campaign_name)}」。稍后刷新查看。" if z else f"Building “{esc(campaign_name)}”. Refresh this page in a little while.",
        "started_refresh": "已开始刷新该回放。" if z else "Refresh started.",
        "processing": "该活动已有生成任务，当前请求未重复启动。" if z else "A replay task is already active for this campaign; no second task was started.",
        "rate_limited": "操作太频繁了，请稍后再试（同一个活动两次刷新至少隔 10 分钟，每人每天最多生成 20 个新活动）。" if z
        else "Too many requests. Please try again later (at least 10 minutes between refreshes of one campaign, 20 new campaigns per person per day).",
        "choose_error": "请先选择一个活动。" if z else "Choose a campaign first.",
        "choose_channel": "该活动有多个渠道，请先选择渠道。" if z else "This campaign has several channels. Choose one first.",
        "missing_error": "该活动不在当前清单中，请重新选择。" if z else "That campaign is not in the current list. Please choose another.",
    }
    msg_text = t.get(msg, "")
    rows, built = table_rows(lang)
    opts = vendor_ui.safe_json([[i, name] for i, name in options(camps, lang)])
    vendor_words = vendor_ui.safe_json({
        "choose": "选择渠道" if z else "Choose a channel", "loading": "读取中" if z else "Loading", "none": "—",
        "paused": "（已暂停）" if z else " (Paused)", "pending": "（状态待确认）" if z else " (Status pending)"})
    if camps:
        picker = f"""<form method="post" action="/add" class="row g-2">
  <input type="hidden" name="lang" value="{lang}">
  <div class="col-lg-3"><label class="visually-hidden" for="flt">{t['filter']}</label><input class="form-control" id="flt" placeholder="{t['filter']}" autocomplete="off"></div>
  <div class="col-lg-4"><label class="visually-hidden" for="sel">{t['select']}</label><select name="campaign_id" id="sel" class="form-select" required></select></div>
  <div class="col-lg-3"><label class="visually-hidden" for="new-vendor">{t['channel']}</label><select class="form-select" name="vendor" id="new-vendor" title="{esc(t['coverage'])}" disabled><option value="">—</option></select></div>
  <div class="col-lg-2 d-grid"><button class="btn btn-primary" id="create">{t['create']}</button></div>
</form>
<div class="form-text mt-2">{t['list_time']}</div>
<script>
const ALL = {opts}, sel = document.getElementById('sel'), flt = document.getElementById('flt');
const vendorSel = document.getElementById('new-vendor'), createBtn = document.getElementById('create');
const VW = {vendor_words};
let vendorTimer = 0, vendorReq = 0;
function vendorLabel(v) {{ const ids = (v.links || []).map(x => '#' + x).join(' '); return v.name + (ids ? ' ' + ids : '') + (v.active === false ? VW.paused : v.active === null ? VW.pending : ''); }}
function addOption(value, text) {{ const o = document.createElement('option'); o.value = value; o.textContent = text; vendorSel.appendChild(o); return o; }}
function setVendors(list, loading) {{
  vendorSel.textContent = '';
  list = list || [];
  if (loading) {{ addOption('', VW.loading); vendorSel.disabled = true; createBtn.disabled = true; return; }}
  if (list.length >= 2) {{
    const hint = addOption('', VW.choose); hint.disabled = true; hint.selected = true;
    list.forEach(v => addOption(v.id, vendorLabel(v)));
    vendorSel.disabled = false; vendorSel.required = true;
  }} else {{
    addOption('', list.length ? vendorLabel(list[0]) : VW.none);
    vendorSel.disabled = true; vendorSel.required = false;
  }}
  createBtn.disabled = false;
}}
function loadVendors() {{
  const cid = sel.value, req = ++vendorReq;
  if (!cid) {{ setVendors([]); return; }}
  setVendors([], true);
  const get = refresh => fetch('/c/' + encodeURIComponent(cid) + '/vendors.json' + (refresh ? '?refresh=1' : ''), {{cache: 'no-store'}})
    .then(r => {{ if (!r.ok) throw Error(); return r.json(); }});
  get(false).then(d => (d.options || []).length ? d : get(true))
    .then(d => {{ if (req === vendorReq) setVendors(d.options || []); }})
    .catch(() => {{ if (req === vendorReq) setVendors([]); }});
}}
function scheduleVendors() {{ clearTimeout(vendorTimer); vendorTimer = setTimeout(loadVendors, 300); }}
function fill() {{
  const q = flt.value.trim().toLowerCase();
  const hit = ALL.filter(o => !q || o[1].toLowerCase().includes(q));
  sel.textContent = '';
  hit.forEach(o => {{ const option = document.createElement('option'); option.value = o[0]; option.textContent = o[1]; sel.appendChild(option); }});
  scheduleVendors();
}}
sel.addEventListener('change', scheduleVendors);
flt.addEventListener('input', fill);
fill();
</script>"""
    elif LIST_LOADING["on"]:
        picker = f'<p class="text-body-secondary mb-0">{t["loading"]}</p>'
    else:
        picker = f'<p class="text-warning mb-0">{t["no_list"]}</p>'
    switch = (f'<div class="btn-group btn-group-sm" role="group">'
              f'<a class="btn btn-outline-secondary{" active" if z else ""}" href="/?lang=zh"{" aria-current=page" if z else ""}>{"中文" if z else "Chinese"}</a>'
              f'<a class="btn btn-outline-secondary{"" if z else " active"}" href="/?lang=en"{"" if z else " aria-current=page"}>English</a></div>')
    who = f'<div class="small text-body-secondary mt-2">{esc(user)} · <a href="/auth/logout">{"退出" if z else "Sign out"}</a></div>' if user else ""
    # 三套独立服务：V1、V2 由 Caddy 转到各自的服务（/v1/、/v2/），V3 就是本服务。
    vers = [("V1", f"/v1/?lang={lang}"), ("V2", f"/v2/?lang={lang}"), ("V3", f"/?lang={lang}")]
    who += (f'<div class="small text-body-secondary mt-1">{"各版本" if z else "Versions"}: '
            + '<span class="sep"> · </span>'.join(f'<a href="{href}">{name}</a>' for name, href in vers) + '</div>')
    return f"""<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t['title']}</title>
<link rel="stylesheet" href="/static/bootstrap.min.css">
<style>{HOME_CSS}</style></head><body data-bs-theme="dark">
<main class="container py-4" style="max-width:1140px">
<header class="d-flex justify-content-between align-items-start gap-3 mb-4">
  <div class="d-flex align-items-center gap-3"><span class="brand-mark"><i></i></span><div><h1 class="h4 mb-1">{t['title']}</h1><p class="text-body-secondary mb-0">{t['intro']}</p></div></div>
  <div class="text-end">{switch}{who}</div>
</header>
{f'<div class="alert alert-info py-2" role="status">{msg_text}</div>' if msg_text else ''}
<section class="card mb-4"><div class="card-body">
  <div class="d-flex justify-content-between align-items-baseline mb-3"><h2 class="h6 mb-0">{t['select']}</h2><span class="count">{t['count']}</span></div>
  {picker}
</div></section>
<section class="card"><div class="card-body">
  <div class="d-flex justify-content-between align-items-baseline mb-2"><h2 class="h6 mb-0">{t['list']}</h2><span class="count" id="built-count">{built}</span></div>
  <div class="table-responsive"><table class="table table-hover align-middle mb-0">
    <thead><tr><th>{t['campaign']}</th><th>{t['business']}</th><th>{t['detail']}</th><th>{t['status']}</th><th class="text-end">{t['action']}</th></tr></thead>
    <tbody id="rows">{rows}</tbody></table></div>
</div></section>
<footer class="small text-body-secondary mt-3">{t['waiting']}{'' if z else ' '}{t['coverage']} · <a href="/ai?lang={lang}">{t['queue']}</a></footer>
</main><script>{POLL_JS.replace("__LANG__", lang)}</script></body></html>"""


_STATIC = {}


def bootstrap_css():
    """首页用的 Bootstrap 5.3.3（MIT，service/static 里的本地副本）。只在首页用，回放页面不引。"""
    if "bootstrap" not in _STATIC:
        _STATIC["bootstrap"] = (ROOT / "service" / "static" / "bootstrap.min.css").read_bytes()
    return _STATIC["bootstrap"]


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None, cache="no-store"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for k, v in (headers or {}).items():
            for item in (v if isinstance(v, list) else [v]):
                self.send_header(k, item)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to="/"):
        self._send(303, "", headers={"Location": to})

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _diagnose_get(self, route):
        """异常排查的 GET 接口和页面；不是这类路径返回 False。"""
        page = DIAG_PAGE.fullmatch(route)
        if page:  # 单独的排查页面（回放页面里不放入口和面板，2026-11-03 用户定）
            n = page.group(1)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            lang = "zh" if (q.get("lang", ["zh"])[0] or "").lower() == "zh" else "en"
            name = ((campaign.info(n) or {}).get("campaign") or {}).get("name") or campaign_names().get(n) or n
            self._send(200, diagnose_ui.page(n, lang, name))
            return True
        if route == "/diagnose/dict":
            self._json(200, dictionary_payload())
            return True
        m = re.fullmatch(r"/c/(\d{1,9})/diagnose/(runs|scan|run/(\d{4}-\d{2}-\d{2})/([0-9a-f]{10}))", route)
        if not m:
            return False
        n = m.group(1)
        try:
            if m.group(2) == "runs":
                self._json(200, {"runs": diagnose.list_runs(n)})
            elif m.group(2) == "scan":
                self._json(200, diagnose.scan(n, log=lambda *_: None))
            else:
                r = diagnose.read_run(n, m.group(3), m.group(4))
                self._json(200, public_run(r)) if r else self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})
        return True

    def _diagnose_post(self, route, body, lang, user=""):
        z = lang == "zh"
        val = lambda k: (body.get(k) or [""])[0]  # noqa: E731
        v = re.fullmatch(r"/c/(\d{1,9})/diagnose/run/(\d{4}-\d{2}-\d{2})/([0-9a-f]{10})/verify", route)
        if v:  # 人工验证一条结论
            try:
                r, changes = diagnose.manual_verify(v.group(1), v.group(2), v.group(3), val("index"), val("verdict"), val("note"), user or "")
                if changes:  # 字典有变化：整理 / 回归集可能要调 AI，放后台，不拖住这次请求
                    threading.Thread(target=diagnose._maybe_regress, args=(changes, lambda *_: None), daemon=True).start()
                self._json(200, public_run(r))
            except ValueError as e:
                self._json(400, {"error": str(e)[:200]})
            except Exception as e:
                self._json(500, {"error": str(e)[:200]})
            return True
        m = re.fullmatch(r"/c/(\d{1,9})/diagnose", route)
        if not m:
            return False
        n = m.group(1)
        try:
            if not val("date"):
                self._json(400, {"error": "请先选择异常日期（必选）" if z else "The anomaly date is required"})
                return True
            r = start_diagnose(n, val("date"), val("q"), val("range") or diagnose.DEFAULT_RANGE)
            self._json(200, {"id": r["id"], "date": r["date"], "stage": r["stage"]})
        except ValueError as e:
            self._json(400, {"error": str(e)[:200]})
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})
        return True

    def _file(self, path, edition=None, nav=None):
        if not path.exists():
            self._send(404, "页面还没有生成，请回首页点「刷新」。")
            return
        body = path.read_bytes()
        if nav:
            body = with_nav(body, *nav)
        self._send(200, body, headers={"X-Replay-Edition": edition} if edition else None)

    def _scope_status(self, n, vendor):
        return vendor_ui.scope_status(n, vendor, running_phase, root=scoped_out_dir(n, vendor))

    def _status_document(self, n, vendor):
        status = self._scope_status(n, vendor)
        state = status["state"]
        return {**state, "running": status["running"], "phase": status["phase"],
                "campaign_busy": campaign_busy(n), "business": status["business"],
                "detail": status["detail"], "last_ok": status["last_ok"]}

    def _resolve_vendor(self, n, values):
        if values is not None and len(values) != 1:
            self._send(400, "invalid vendor scope")
            return False, None
        raw = values[0] if values else None
        try:
            return True, vendor_ui.validate_vendor(n, raw)
        except vendor_ui.InvalidVendor:
            self._send(400, "invalid vendor scope")
        except vendor_ui.UnknownVendor:
            self._send(404, "vendor not found for this campaign")
        return False, None

    def _not_generated(self, n, lang, vendor, edition):
        z = lang == "zh"
        status = self._scope_status(n, vendor)
        if status["phase"] == "queued":
            note = "当前范围正在排队。" if z else "This scope is queued."
        elif status["running"]:
            note = "当前范围正在处理中。" if z else "This scope is processing."
        elif campaign_busy(n):
            note = "该活动的其他范围有任务，当前范围未启动。" if z else "Another scope has an active task; this scope was not started."
        else:
            note = "此渠道尚未生成该版本。" if z else "This channel does not have this edition yet."
        label = ("业务版" if edition == "business" else "详细版") if z else ("Business edition" if edition == "business" else "Detail edition")
        vendor_field = f'<input type="hidden" name="vendor" value="{esc(vendor)}">' if vendor else '<input type="hidden" name="vendor" value="">'
        disabled = "disabled" if campaign_busy(n) or status["running"] or status["phase"] == "queued" else ""
        button = ("生成回放" if z else "Build replay")
        body = f'''<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{"尚未生成" if z else "Not generated"} · {label}</title><style>body{{margin:0;background:#070812;color:#f4f2fa;font:15px/1.6 system-ui,-apple-system,sans-serif}}main{{max-width:680px;margin:12vh auto;padding:24px;border:1px solid rgba(255,255,255,.12);border-radius:14px;background:#10111d}}p{{color:#aaa7bc}}a{{color:#c4b5fd}}button{{border:0;border-radius:8px;padding:9px 14px;background:#5540d6;color:white;font:inherit;font-weight:700}}button:disabled{{opacity:.55}}form{{display:inline-block;margin-right:12px}}</style></head><body>
<main><h1>{"尚未生成" if z else "Not generated"}</h1><p>{label}：{note}</p><form method="post" action="/c/{n}/refresh"><input type="hidden" name="lang" value="{lang}">{vendor_field}<button {disabled}>{button}</button></form><a href="/?lang={lang}">{"返回首页" if z else "Back to home"}</a></main></body></html>'''
        self._send(404, with_nav(body, n, lang, edition, vendor))

    def _choose_channel(self, n, lang, edition, chans):
        """多渠道的活动不做合并版（2026-11-02 用户定）：没带渠道的地址（如以前分享的链接）先选渠道。"""
        z = lang == "zh"
        name = pipeline.read_state(n).get("campaign") or campaign_names().get(n) or known_name(n) or n
        suffix, page_name = ("/detail", "replay") if edition == "detail" else ("", "business")
        items = []
        for c in chans:
            ready = (scoped_out_dir(n, c["id"]) / f"{page_name}.en.html").exists()
            state = ("已生成" if z else "Generated") if ready else ("尚未生成" if z else "Not generated")
            items.append(f'<li><a href="/c/{n}/{lang}{suffix}{vendor_ui.scope_query(c["id"])}">'
                         f'{esc(vendor_ui.option_label(c, lang))}</a><span>{state}</span></li>')
        title = "选择渠道" if z else "Choose a channel"
        note = ("该活动有多个渠道，每个渠道单独生成回放。" if z
                else "This campaign has several channels; each channel has its own replay.")
        body = f'''<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {esc(name)}</title><style>body{{margin:0;background:#070812;color:#f4f2fa;font:15px/1.6 system-ui,-apple-system,sans-serif}}main{{max-width:680px;margin:12vh auto;padding:24px;border:1px solid rgba(255,255,255,.12);border-radius:14px;background:#10111d}}h1{{font-size:20px;margin:0 0 4px}}p{{color:#aaa7bc;margin:0 0 14px}}ul{{list-style:none;padding:0;margin:0 0 18px}}li{{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.08)}}li span{{color:#aaa7bc}}a{{color:#c4b5fd;text-decoration:none}}a:hover{{text-decoration:underline}}</style></head><body>
<main><h1>{title}</h1><p>{esc(name)}</p><p>{note}</p><ul>{"".join(items)}</ul><a href="/?lang={lang}">{"返回首页" if z else "Back to home"}</a></main></body></html>'''
        self._send(200, body)

    def _business(self, n, lang, vendor=None):
        page = scoped_page(n, vendor, f"business.{lang}.html")
        if page.is_file():
            self._send(200, with_nav(page.read_bytes(), n, lang, "business", vendor), headers={"X-Replay-Edition": "business"})
            return
        if vendor is not None:
            self._not_generated(n, lang, vendor, "business")
            return
        self._send(404, auth.page(
            "业务版尚未生成", "The business edition has not been generated",
            "业务版还没有生成，请返回首页使用生成按钮。",
            "The business edition is not ready yet. Return to the home page to build it.",
            f"/?lang={lang}", "返回首页", "Back to home", lang))

    def _guard(self, route, query, post=False):
        """登录闸门。返回当前登录的邮箱（没启用登录时是空串）；返回 None 表示这个请求已经回应完，调用方不要再处理。"""
        if route == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return None
        if not AUTH.enabled:
            return ""
        if not AUTH.ready:
            self._send(503, auth.unavailable_page())
            return None
        cookie = self.headers.get("Cookie")
        if route.startswith("/auth/"):
            if post and route != "/auth/logout":
                self._send(405, "method not allowed", headers={"Allow": "GET"})
            else:
                self._auth_route(route, query, cookie)
            return None
        origin = self.headers.get("Origin")
        if post and origin and not AUTH.origin_ok(origin):
            self._send(403, "forbidden")
            return None
        user = AUTH.user(cookie)
        if user:
            return user
        if post and "/diagnose" in route:
            self._json(401, {"error": "login required"})
        elif post:
            self._redirect("/auth/login?next=%2F")
        elif route.endswith("/status") or route == "/rows.json" or ("/diagnose" in route and not DIAG_PAGE.fullmatch(route)):
            self._send(401, json.dumps({"error": "login required"}), "application/json; charset=utf-8")
        else:
            self._redirect("/auth/login?" + urllib.parse.urlencode({"next": self.path}))
        return None

    def _auth_route(self, route, query, cookie):
        if route == "/auth/login":
            wanted = (query.get("next") or ["/"])[0]
            if AUTH.user(cookie):
                self._redirect(auth.safe_next(wanted))
            else:
                status, headers, body = AUTH.login(wanted)
                self._send(status, body, headers=headers)
        elif route == "/auth/google/callback":
            status, headers, body = AUTH.callback(query, cookie)
            self._send(status, body, headers=headers)
        elif route == "/auth/logout":
            status, headers, body = AUTH.logout()
            self._send(status, body, headers=headers)
        elif route == "/auth/check":
            # Caddy forward_auth 用：/v1/、/v2/ 两套独立服务进门前先在这里核对同一个公司登录。
            method = (self.headers.get("X-Forwarded-Method") or "GET").upper()
            origin = self.headers.get("Origin")
            user = AUTH.user(cookie)
            if not user:
                if method in ("GET", "HEAD"):
                    wanted = auth.safe_next(self.headers.get("X-Forwarded-Uri") or "/")
                    self._redirect("/auth/login?" + urllib.parse.urlencode({"next": wanted}))
                else:
                    self._send(401, "login required", "text/plain; charset=utf-8")
            elif method not in ("GET", "HEAD") and origin and not AUTH.origin_ok(origin):
                self._send(403, "forbidden", "text/plain; charset=utf-8")
            else:
                self._send(200, "ok", "text/plain; charset=utf-8", headers={"X-Replay-User": user})
        else:
            self._send(404, "not found")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route, q = parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if route.startswith("/agent-api/"):  # 外部 agent：用各自的钥匙，不走 Google 登录
            agent_api.handle(self, "GET", route, q)
            return
        user = self._guard(route, q)
        if user is None:
            return
        if route == "/":
            lang = (q.get("lang", ["en"])[0] or "en").lower()
            lang = "zh" if lang == "zh" else "en"
            self._send(200, index_page(q.get("msg", [""])[0], lang, q.get("name", [""])[0], user))
            return
        vm = re.fullmatch(r"/(v\d{1,2})(?:/c/(\d{1,9})/(en|zh)(/detail)?)?/?", route)
        if vm:
            all_versions = version_sites.load()
            version, n, lang, detail = vm.groups()
            cfg = all_versions.get(version)
            if not cfg:
                self._send(404, "not found")
                return
            if not n:
                lang = "zh" if (q.get("lang", ["zh"])[0] or "").lower() == "zh" else "en"
                self._send(200, version_sites.index(version, cfg, all_versions, lang, css=HOME_CSS,
                                                    names=lambda n, _names=campaign_names(): _names.get(n) or known_name(n),
                                                    channel=version_channel))
                return
            vendor_values = q.get("vendor", [])
            if len(vendor_values) > 1:
                self._send(400, "invalid vendor")
                return
            vendor = vendor_values[0] if vendor_values and vendor_values[0] else None
            page = version_sites.page(cfg, n, vendor, f"{'replay' if detail else 'business'}.{lang}.html")
            if page is None:
                self._send(404, "这个版本没有这个页面。" if lang == "zh" else "This version has no such page.")
                return
            self._send(200, page.read_bytes(), headers={"X-Replay-Version": version})
            return
        if route in ("/showcase/chime", "/showcase/chime/"):
            self._redirect("/showcase/chime/en")
            return
        curated = re.fullmatch(r"/showcase/chime/(en|zh)/?", route)
        if curated:
            lang = curated.group(1)
            versions = q.get("v", [])
            if versions:
                if len(versions) != 1 or not re.fullmatch(r"\d{8}-v\d+", versions[0]):
                    self._send(404, "Unknown curated replay version.")
                    return
                page = CURATED_ROOT / "chime" / "releases" / versions[0] / f"replay.{lang}.html"
            else:
                page = CURATED_ROOT / "chime" / "current" / f"replay.{lang}.html"
            if not page.is_file():
                self._send(404, "Chime 精选版尚未发布。" if lang == "zh" else "The curated Chime replay is not yet published.")
                return
            self._send(200, page.read_bytes(), headers={"X-Replay-Edition": "curated"})
            return
        if route == "/static/bootstrap.min.css":
            self._send(200, bootstrap_css(), "text/css; charset=utf-8", cache="private, max-age=86400")
            return
        if self._diagnose_get(route):
            return
        if route == "/campaigns.json":
            self._send(200, json.dumps(built_campaigns(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if route == "/rows.json":
            rows, built = table_rows("zh" if (q.get("lang", ["en"])[0] or "").lower() == "zh" else "en")
            self._send(200, json.dumps({"rows": rows, "count": built}, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if route in ("/ai", "/ai/"):
            lang = "zh" if (q.get("lang", ["en"])[0] or "").lower() == "zh" else "en"
            names = campaign_names()
            self._send(200, ai_status.list_page(lang, css=HOME_CSS, name=lambda n: names.get(n) or known_name(n),
                                                channel=version_channel))
            return
        if route == "/ai/run":
            lang = "zh" if (q.get("lang", ["en"])[0] or "").lower() == "zh" else "en"
            cid, vendor, name = (q.get("c") or [""])[0], (q.get("vendor") or [""])[0] or None, (q.get("run") or [""])[0]
            run = ai_status.find_run(cid, vendor, name)
            if run is None:
                self._send(404, "没有这次运行" if lang == "zh" else "Run not found", "text/plain; charset=utf-8")
                return
            names = campaign_names()
            self._send(200, ai_status.run_page(cid, vendor, run, lang, css=HOME_CSS,
                                               name=lambda n: names.get(n) or known_name(n), channel=version_channel))
            return
        if route.startswith("/queue"):  # V3 不再用任务队列：旧的队列页改为指向 AI 运行状态
            self._redirect("/ai")
            return
        if route in ("/queue", "/queue/", "/queue.json"):
            state = (q.get("state") or [""])[0]
            camp = (q.get("campaign") or [""])[0]
            snap = jobqueue.snapshot(state=state if state in jobqueue.STATES else None, campaign=camp if camp.isdigit() else None)
            if route == "/queue.json":
                self._send(200, json.dumps(snap, ensure_ascii=False), "application/json; charset=utf-8")
            else:
                self._send(200, queue_page.overview(snap, campaign_names(), state if state in jobqueue.STATES else None))
            return
        mq = re.fullmatch(r"/queue/([JL][0-9A-F]{8,24})", route)
        if mq:
            found = jobqueue.job_detail(mq.group(1))
            if not found:
                self._send(404, "没有这道题")
            else:
                self._send(200, queue_page.detail(found, campaign_names()))
            return
        vendors = re.fullmatch(r"/c/(\d{1,9})/vendors\.json/?", route)
        if vendors:
            n = vendors.group(1)
            refresh_values = q.get("refresh", [])
            if len(refresh_values) > 1:
                self._send(400, "invalid refresh flag")
                return
            refresh = bool(refresh_values and refresh_values[0].lower() in ("1", "true"))
            try:
                found = vendor_ui.options(n, refresh=refresh)
                scopes = {"all": self._scope_status(n, None)}
                for item in found:
                    scopes[item["id"]] = self._scope_status(n, item["id"])
                busy = campaign_busy(n)
                public_scopes = {key: {"business": val["business"], "detail": val["detail"],
                                       "running": val["running"], "phase": val["phase"],
                                       "campaign_busy": busy, "last_ok": val["last_ok"]}
                                 for key, val in scopes.items()}
            except Exception:
                self._send(502, vendor_ui.safe_json({"error": "vendor list unavailable"}), "application/json; charset=utf-8")
                return
            self._send(200, vendor_ui.safe_json({"options": found, "scopes": public_scopes}), "application/json; charset=utf-8")
            return
        if route == "/status":
            data = {n: {**pipeline.read_state(n), "running": n in RUNNING} for n in pipeline.known_numbers()}
            self._send(200, vendor_ui.safe_json(data), "application/json; charset=utf-8")
            return
        m = re.fullmatch(r"/c/(\d{1,9})/(en|zh|status)(/detail|/progress)?/?", route)
        if not m or (m.group(2) == "status" and m.group(3)):
            self._send(404, "not found")
            return
        n, what, detail = m.groups()
        valid, vendor = self._resolve_vendor(n, q.get("vendor"))
        if not valid:
            return
        if vendor is None and what != "status":
            chans = vendor_ui.options(n)
            if len(chans) >= 2:
                self._choose_channel(n, what, "detail" if detail == "/detail" else "business", chans)
                return
        if what == "status":
            self._send(200, vendor_ui.safe_json(self._status_document(n, vendor)), "application/json; charset=utf-8")
        elif detail == "/progress":
            if scoped_page(n, vendor, "business.en.html").exists():
                self._redirect(f"/c/{n}/{what}{vendor_ui.scope_query(vendor)}#t=progress")
            else:
                self._business(n, what, vendor)
        elif detail:
            page = scoped_page(n, vendor, f"replay.{what}.html")
            if vendor is not None and not page.is_file():
                self._not_generated(n, what, vendor, "detail")
            else:
                self._file(page, "detail", nav=(n, what, "detail", vendor))
        else:
            self._business(n, what, vendor)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route.startswith("/agent-api/"):
            agent_api.handle(self, "POST", route, urllib.parse.parse_qs(parsed.query))
            return
        user = self._guard(route, {}, post=True)
        if user is None:
            return
        if route.rstrip("/") == "/showcase/chime" or route.startswith("/showcase/chime/"):
            self._send(405, "The curated replay is published manually.", headers={"Allow": "GET"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        cap = 10000 if "/diagnose" in route else 2000  # 排查描述最多 500 字，编码后可能有几千字节
        body = urllib.parse.parse_qs(self.rfile.read(min(length, cap)).decode("utf-8", "ignore"), keep_blank_values=True)
        lang = (body.get("lang") or ["en"])[0].lower()
        lang = "zh" if lang == "zh" else "en"
        if self._diagnose_post(route, body, lang, user):
            return
        if route == "/add":
            if len(body.get("campaign_id", [])) != 1:
                self._redirect(f"/?lang={lang}&msg=choose_error")
                return
            try:
                n = valid_number(body["campaign_id"][0])
            except ValueError:
                self._redirect(f"/?lang={lang}&msg=choose_error")
                return
            c = campaign.find(n)
            if not c:
                self._redirect(f"/?lang={lang}&msg=missing_error")
                return
            valid, vendor = self._resolve_vendor(n, body.get("vendor"))
            if not valid:
                return
            if vendor is None and len(vendor_ui.options(n)) >= 2:  # 多渠道的活动必须选渠道，不生成合并版
                self._redirect(f"/?lang={lang}&msg=choose_channel")
                return
            if n in RUNNING or _root_lock_busy(n):
                self._redirect(f"/?lang={lang}&msg=processing")
                return
            if not LIMITS.allow_add(user, n):
                self._redirect(f"/?lang={lang}&msg=rate_limited")
                return
            if not (start_refresh(n, vendor) if vendor else start_refresh(n)):
                self._redirect(f"/?lang={lang}&msg=processing")
                return
            query = urllib.parse.urlencode({"lang": lang, "msg": "started", "name": c["name"]})
            self._redirect("/?" + query)
        else:
            m = re.fullmatch(r"/c/(\d{1,9})/refresh", route)
            if not m:
                self._send(404, "not found")
                return
            n = m.group(1)
            valid, vendor = self._resolve_vendor(n, body.get("vendor"))
            if not valid:
                return
            if vendor is None and len(vendor_ui.options(n)) >= 2:
                self._redirect(f"/?lang={lang}&msg=choose_channel")
                return
            if n in RUNNING or _root_lock_busy(n):
                self._redirect(f"/?lang={lang}&msg=processing")
                return
            if not LIMITS.allow_refresh(n):
                self._redirect(f"/?lang={lang}&msg=rate_limited")
                return
            if not (start_refresh(n, vendor) if vendor else start_refresh(n)):
                self._redirect(f"/?lang={lang}&msg=processing")
                return
            self._redirect(f"/?lang={lang}&msg=started_refresh")

    def log_message(self, fmt, *args):
        line = re.sub(r"(/auth/google/callback)\?\S*", r"\1?...", fmt % args)  # 授权码不写进日志
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), line))


def main():
    if AUTH.enabled:
        sys.stderr.write("login: Google, " + ("ready" if AUTH.ready else "CLOSED (" + AUTH.cfg.error + ")") + "\n")
    ensure_list()
    if followup_on():
        threading.Thread(target=followup_loop, daemon=True).start()
    bind = os.environ.get("REPLAY_BIND", "127.0.0.1")
    port = int(os.environ.get("REPLAY_PORT", "8791"))
    http.server.ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
