"""Run one ordinary Pi session per campaign scope. No task queue, custom loop or end gate."""
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import business, changes, feeds, progress, scope, util
from .pi_tools import FEMINI_READ

ENGINE = "pi-online-v3"
MODEL = "minimax-m3.1-flash"


def enabled():
    return (os.environ.get("REPLAY_ENGINE") or feeds._env_file("pi.env").get("REPLAY_ENGINE", "rules")) == "pi"


def previous_run(cid):
    """上一次已发布的调查（增量更新的起点），以发布记录为准。"""
    runs = util.camp_dir(cid) / "pi-runs"
    name = (util.read_json(util.camp_dir(cid) / "pi-published.json", {}) or {}).get("run")
    if name and (runs / name / "work/output/report.json").is_file():
        return runs / name
    return None  # 只从真正发布过的结果接着做；失败或没发布的草稿不当起点


def _evidence_files(doc):
    if isinstance(doc, dict):
        f = doc.get("file")
        if isinstance(f, str) and f.startswith("results/") and ".." not in f:
            yield f
        for v in doc.values():
            yield from _evidence_files(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _evidence_files(v)


def seed_update(run, prev, brief):
    """增量更新：把上次的结果（改回草稿）、工作记录和被引用的查询结果放进这次的工作目录。"""
    work, old = run / "work", prev / "work"
    report = util.read_json(old / "output/report.json")
    if not isinstance(report, dict):
        return False
    for f in set(_evidence_files(report)):
        src = old / f
        if src.is_file():
            (work / f).write_bytes(src.read_bytes())
    notes = work / "seed/previous_notes"
    notes.mkdir(parents=True, exist_ok=True)
    for n in (old / "notes").glob("*.md"):
        (notes / n.name).write_bytes(n.read_bytes())
    # 上一轮留下的"没查完"挪到 previous_remaining：增量更新只要求查完上次之后的新时间段，旧缺口不挡完成
    coverage = dict(report.get("coverage") or {})
    old_gaps = [x for x in (coverage.get("remaining") or []) if str(x).strip()]
    coverage["previous_remaining"] = old_gaps + [x for x in (coverage.get("previous_remaining") or []) if x not in old_gaps]
    coverage["remaining"] = []
    report = {**report, "status": "draft", "coverage": coverage}
    from .pi_report import ensure_ids
    ensure_ids(report)  # 上次的条目补上编号（M001…），这次用编号增改
    util.write_json(work / "output/report.json", report)
    prev_brief = util.read_json(old / "seed/brief.json", {}) or {}
    brief.update(mode="update", previous_run=prev.name, previous_as_of=prev_brief.get("as_of"))
    return True


def new_since(material, since):
    """刷新用：程序初筛资料里，上次之后的新记录（三类都给，不再按关键词二次筛；初筛的判断只作参考）。"""
    if not since:
        return []
    label = {"records": "初筛判为本活动", "client_background": "初筛判为客户背景", "excluded": "初筛判为排除"}
    out = []
    for key, what in label.items():
        for r in material.get(key) or []:
            if isinstance(r, dict) and str(r.get("ts") or "") >= str(since):
                att = r.get("attribution") if isinstance(r.get("attribution"), dict) else {}
                out.append({**{k: r.get(k) for k in ("id", "kind", "ts", "channel", "subject", "thread", "ticket", "who", "to", "text") if r.get(k)},
                            "initial_screening": what, "screening_reason": att.get("reason", "")})
    return sorted(out, key=lambda x: str(x.get("ts")))


def wants_full():
    return (os.environ.get("REPLAY_PI_MODE") or feeds._env_file("pi.env").get("REPLAY_PI_MODE")) == "full"


def prepare(cid, full=False):
    base = util.camp_dir(cid)
    material = util.read_json(base / "corpus.json")
    if not material:
        raise RuntimeError("缺少第一轮取数和初筛资料")
    run = base / "pi-runs" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    work = run / "work"
    for name in ("seed", "results", "notes", "output", "tmp"):
        (work / name).mkdir(parents=True, exist_ok=True)
    os.chmod(run, 0o700)
    brief = {"campaign_id":str(cid),"vendor_id":scope.vendor_for(cid),"as_of":util.now_z(),
             "terms":material["terms"],"window":material.get("window"),
             "campaign":util.read_json(base/"campaign.json"),
             "initial_screening_is_not_ground_truth":True,"mode":"full"}
    prev = None if full else previous_run(cid)
    if prev is not None and not seed_update(run, prev, brief):
        brief["mode"] = "full"
    util.write_json(work / "seed/brief.json", brief)
    for key, name in (("records","accepted"),("client_background","pending"),("excluded","excluded")):
        util.write_json(work / f"seed/{name}.json", material.get(key, []))
    if brief["mode"] == "update":
        util.write_json(work / "seed/new_since_last.json", new_since(material, brief.get("previous_as_of")))
    util.write_json(work / "seed/changes.json", changes.param_changes(cid))
    handoffs = business.handoff_events(cid, log=lambda *_:None)
    for i, h in enumerate(handoffs, 1):h["id"] = f"H{i:04d}"
    util.write_json(work / "seed/handoffs.json", handoffs)
    util.write_json(work / "seed/metrics.json", util.read_json(base / "metrics.json", {}))
    for link in material["terms"].get("links", []):
        util.write_json(work / f"seed/github-{link}.json", util.read_json(util.raw_dir(link)/"github.json",{}))
    util.write_json(work / "mcp-read-tools.json", FEMINI_READ)
    (work / "seed/README.md").write_text(
        "# 第一轮资料，仅供参考\n\nbrief.json：活动、渠道、链接、同客户活动、日期。\n"
        "accepted.json：旧规则入选；pending.json：客户背景/待确认；excluded.json：旧规则排除摘要。"
        "三者不是最终判断，其他资料仍可在线查询。\nchanges.json：后台批准调整；handoffs.json：工单交接；"
        "metrics.json：投放数据；github-*.json：已展开和未展开的GitHub线索。\n"
        "new_since_last.json（只有刷新才有）：上次之后程序拉到的新记录，按时间排好；初筛判断只作参考。\n"
        "查询结果保存到 results/ 仅用于本次上下文和引用，不是全量资料库。\n", encoding="utf-8")
    return run


MAX_CONTINUES = 5
CONTINUE_MIN_SECONDS = 120


def unfinished(path, update=False):
    """结果还没写完的原因（空串表示已完成）。只看有没有写完，不评判内容。
    刷新（增量更新）没有 Goal：写出来、标了 complete 就算完，不看「还没查完」清单。"""
    from .pi_result import load_report
    try:
        report = load_report(path)
    except ValueError as exc:  # 写坏了的 JSON：让它在同一会话里修好，而不是整个运行报错
        return f"结果文件损坏（{exc}）"
    if not isinstance(report, dict):
        return "结果里还什么都没写"
    if not report.get("moments"):
        return "结果里还没有任何关键节点"
    if not str(report.get("state_brief") or "").strip():
        return "还没写现状总结 state_brief"
    if report.get("status") != "complete":
        return "还没有调用 report_finish 标记完成"
    remaining = [str(x) for x in ((report.get("coverage") or {}).get("remaining") or []) if str(x).strip()]
    if remaining and not update:  # Goal 条件 2：还有没查完的范围就不算完成（权限/接口问题应写 not_available）
        return "Goal 还没达成：coverage.remaining 里还有没查完的范围（" + "；".join(remaining) + "）"
    return ""


MAX_REVIEWS = 2
REVIEW_MIN_SECONDS = 180


def reviews_on():
    return (os.environ.get("REPLAY_PI_REVIEW") or feeds._env_file("pi.env").get("REPLAY_PI_REVIEW") or "1") != "0"


def _one_session(folder):
    sessions = list(folder.glob("*.jsonl"))
    return sessions[0] if len(sessions) == 1 else None



def review_packet(run):
    work = run / "work"
    brief = util.read_json(work / "seed/brief.json", {}) or {}
    def note(name):
        p = work / "notes" / name
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else "（没有这份记录）"
        return text
    identity = {k: brief.get(k) for k in ("campaign_id", "vendor_id", "as_of", "window", "mode", "previous_as_of")}
    identity["terms"] = brief.get("terms")
    parts = [
        "## 任务身份（活动、渠道、链接、同客户其他活动）\n```json\n" + json.dumps(identity, ensure_ascii=False, indent=1) + "\n```",
        "## 作者的结果 output/report.json\n```json\n" + (work / "output/report.json").read_text(encoding="utf-8") + "\n```",
        "## 作者的工作记录 notes/progress.md\n" + note("progress.md"),
        "## 作者的证据记录 notes/evidence.md\n" + note("evidence.md"),
    ]
    return "\n\n".join(parts)


def review(run, rnd, run_once, worker_env):
    """审核 Agent：只有 review_verdict 一个工具，必须调用它才能结束。返回它的结论（没给出就是空 dict）。"""
    folder = run / f"review-{rnd}"
    folder.mkdir(parents=True, exist_ok=True)
    model = review_model()
    level = _setting("REPLAY_PI_REVIEW_THINKING")
    if model.startswith("openai-codex/"):
        home = subscription_home()
    else:
        home = folder / "config"
        home.mkdir(parents=True, exist_ok=True)
        models = util.read_json(pathlib.Path(worker_env.get("PI_CODING_AGENT_DIR") or run / "config") / "models.json")
        for m in models["providers"]["feedmob"]["models"]:
            m.pop("samplingParams", None)
            if level:
                m["samplingParams"] = {"reasoning_effort": level}
        util.write_json(home / "models.json", models)
        util.write_json(home / "settings.json", {"quietStartup": True})
    prompt = folder / "system.md"
    prompt.write_text((util.ROOT / "agent/review-prompt.md").read_text(encoding="utf-8") + "\n\n" + review_packet(run), encoding="utf-8")
    verdict = folder / "verdict.json"
    verdict.unlink(missing_ok=True)
    env = {**worker_env, "PI_CODING_AGENT_DIR": str(home.resolve()), "REPLAY_REVIEW_OUT": str(verdict.resolve())}
    env.pop("PI_FEMINI_TOKEN", None)
    runtime = pathlib.Path(os.environ.get("REPLAY_PI_RUNTIME", util.ROOT.parent / "runtime"))
    cmd = [str(runtime / "pi/bin/pi"), "-p", "--mode", "json", *model_args(model, level or "medium"),
           "--system-prompt", str(prompt.resolve()), "--session-dir", str((folder / "session").resolve()),
           "--no-approve", "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-extensions",
           "-e", str(util.ROOT / "agent/review.ts"), "--tools", "review_verdict", "--offline",
           "请审核系统提示里附带的材料，然后调用 review_verdict 给出结论。"]
    run_once(cmd, env, "review")
    return util.read_json(verdict, {}) or {}


def rejection_message(rnd, verdict, deadline):
    lines = [f"审核打回（第 {rnd} 轮）。审核意见：{verdict.get('summary') or ''}", "需要处理的问题："]
    for i, s in enumerate(verdict.get("suggestions") or [], 1):
        lines.append(f"{i}. [{s.get('target')}] {s.get('problem')} → {s.get('suggestion')}")
    left = max(0, int((deadline - time.monotonic()) // 60))
    lines.append(f"请逐条处理（需要就在线补查），用 note_add 记下每条怎么处理的，用 report_update/report_add/report_delete 修改结果，"
                 f"处理完调用 report_finish 并结束。还剩约 {left} 分钟。")
    return "\n".join(lines)


QUOTA_PROBE_SECONDS = 300


def quota_blocked(events_file, since=0):
    """这一次 Pi 进程（从事件文件的 since 位置起）最后一条模型回复，是不是因为额度用完（429 / usage limit）而中断。"""
    try:
        with open(events_file, "rb") as f:
            f.seek(0, 2); f.seek(max(since, f.tell() - 400000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(tail):
        try:event = json.loads(line)
        except ValueError:continue
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            m = event["message"]
            return m.get("stopReason") == "error" and any(k in str(m.get("errorMessage") or "") for k in ("usage limit", "429", "RateLimit"))
    return False


def quota_ok():
    model = author_model()
    if model.startswith("openai-codex/"):  # 订阅模型：用 Pi 发一句最短的话试试
        runtime = pathlib.Path(os.environ.get("REPLAY_PI_RUNTIME", util.ROOT.parent / "runtime"))
        env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "PATH", "LANG")}
        env.update(PATH=str(runtime / "node/bin") + ":" + env.get("PATH", "/usr/bin:/bin"),
                   PI_CODING_AGENT_DIR=str(subscription_home()), PI_SKIP_VERSION_CHECK="1", PI_TELEMETRY="0")
        try:
            p = subprocess.run([str(runtime / "pi/bin/pi"), "-p", "--no-tools", "--no-extensions", "--no-context-files",
                                "--no-skills", "--model", model, "reply ok"], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=180)
            return p.returncode == 0 and bool(p.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False
    import urllib.request
    ai = progress.load_ai()
    base = ai["AI_BASE_URL"].rstrip("/")
    base += "" if base.endswith("/v1") else "/v1"
    body = {"model": MODEL, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
    req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + ai["AI_API_KEY"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not yet"
        return False


def wait_for_quota(max_hours=None):
    """额度用完时等它恢复（每 5 分钟试一次）；返回等了多少秒，超过上限返回 None。"""
    limit = float(max_hours or os.environ.get("REPLAY_PI_QUOTA_WAIT_HOURS") or feeds._env_file("pi.env").get("REPLAY_PI_QUOTA_WAIT_HOURS") or 6) * 3600
    began = time.monotonic()
    while not quota_ok():
        if time.monotonic() - began > limit:
            return None
        time.sleep(QUOTA_PROBE_SECONDS)
    return time.monotonic() - began


def _setting(key, default=""):
    return os.environ.get(key) or feeds._env_file("pi.env").get(key) or default


def author_model():
    """干活的 Agent 用哪个模型：feedmob/minimax-m3.1-flash（公司网关，默认）或 openai-codex/gpt-6-luna（ChatGPT 订阅）。"""
    return _setting("REPLAY_PI_MODEL", "feedmob/" + MODEL)


def review_model():
    return _setting("REPLAY_PI_REVIEW_MODEL", author_model())


def subscription_home():
    """ChatGPT 订阅的登录凭据只放一份（secrets/pi-agent/auth.json），所有用订阅模型的运行共用这个目录，
    令牌刷新只写这一处，避免各运行各拷一份后旧令牌失效。"""
    home = pathlib.Path(_setting("REPLAY_PI_AGENT_DIR", str(progress.SECRETS / "pi-agent")))
    if not (home / "auth.json").exists():
        raise RuntimeError("还没有登录 ChatGPT 订阅（secrets/pi-agent/auth.json 不存在）")
    return home


def model_args(model, level):
    """--model 参数及思考程度：订阅模型用 Pi 自带的 --thinking；公司网关的 MiniMax 走 models.json 里的请求参数。"""
    args = ["--model", model]
    if model.startswith("openai-codex/") and level:
        args += ["--thinking", level]
    return args


def thinking_level():
    return os.environ.get("REPLAY_PI_THINKING") or feeds._env_file("pi.env").get("REPLAY_PI_THINKING") or "low"


def system_prompt(run, update):
    """按模式拼系统提示词：完整调查带 Goal、不带「增量更新」一节；刷新去掉整段 Goal（用户定的：刷新不要 Goal，免得模型死磕查全、重核旧内容）。"""
    text = (util.ROOT / "agent/online-prompt.md").read_text(encoding="utf-8")
    drop = "## Goal" if update else "## 增量更新"
    start = text.index("\n" + drop) + 1
    end = text.find("\n## ", start)
    text = text[:start] + (text[end + 1:] if end >= 0 else "")
    path = run / "system-prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def is_update(run):
    return (util.read_json(run / "work/seed/brief.json", {}) or {}).get("mode") == "update"


def configure(run):
    work = (run / "work").resolve()
    model = author_model()
    home = subscription_home() if model.startswith("openai-codex/") else run / "config"
    home.mkdir(exist_ok=True)
    ai = progress.load_ai()
    base_url = ai["AI_BASE_URL"].rstrip("/")
    if not base_url.endswith("/v1"):base_url += "/v1"
    providers={}
    if model.startswith("openai-codex/"):
        # Pi 内置目录给订阅版 Luna 写的是 27.2 万上下文；实际支持约 100 万，这里按设置覆盖（默认 100 万）。
        providers["openai-codex"]={"modelOverrides":{model.split("/",1)[1]:{"contextWindow":int(_setting("REPLAY_PI_CONTEXT","1000000"))}}}
    util.write_json(home/"models.json", {"providers":{**providers,"feedmob":{
        "baseUrl":base_url,"api":"openai-completions","apiKey":"$PI_FLASH_KEY",
        "compat":{"supportsDeveloperRole":False,"supportsStore":False,"supportsReasoningEffort":False,"maxTokensField":"max_tokens"},
        # 思考程度只通过请求参数 reasoning_effort 传给网关；不开 Pi 的 reasoning 模式。
        # 开了以后 Pi 会把每轮的思考内容回传，公司网关转给 MiniMax 时会报 invalid params (2013)（2026-11-02 4973 实测）。
        "models":[{"id":MODEL,"reasoning":False,"input":["text"],"contextWindow":1000000,"maxTokens":64000,
                   "samplingParams":{"reasoning_effort":thinking_level()}}]
    }}})
    util.write_json(home/"mcp.json", {"autoEnableCodemode":False,"mcpServers":{"femini":{
        "url":feeds.setting("FEMINI_MCP_URL", "https://assistant.feedmob.ai/mcp"),
        "headers":{"Authorization":"Bearer ${PI_FEMINI_TOKEN}"},"timeout":180,
        "exposure":"hidden","toolExposure":{name:"direct" for name in FEMINI_READ}
    }}})
    util.write_json(home/"settings.json", {"quietStartup":True})
    runtime = pathlib.Path(os.environ.get("REPLAY_PI_RUNTIME", util.ROOT.parent/"runtime"))
    env = {k:v for k,v in os.environ.items() if k in ("HOME","USER","LOGNAME","PATH","LANG")}
    env.update({"PATH":str(runtime/"node/bin")+":"+env.get("PATH","/usr/bin:/bin"),
        "REPLAY_SECRETS":str(progress.SECRETS),"PYTHONPATH":str(util.ROOT),
        "REPLAY_PI_WORK":str(work),"REPLAY_PI_APP":str(util.ROOT),"REPLAY_PI_PYTHON":sys.executable,
        "PI_CODING_AGENT_DIR":str(home.resolve()),"PI_SKIP_VERSION_CHECK":"1","PI_OFFLINE":"1","PI_TELEMETRY":"0",
        "PI_FLASH_KEY":ai["AI_API_KEY"],"PI_FEMINI_TOKEN":feeds._femini_token(),"TMPDIR":str(work/"tmp")})
    if not env["PI_FEMINI_TOKEN"]:raise RuntimeError("云端 Femini 凭据缺失")
    tools="read,ls,write,edit,feedai_api,github_api,fireflies_api,report_find,report_add,report_update,report_delete,report_set,report_finish,note_add,mcp__femini__*"
    cmd=[str(runtime/"pi/bin/pi"),"-p","--mode","json",*model_args(model,thinking_level()),
         "--system-prompt",str(system_prompt(run, is_update(run)).resolve()),"--session-dir",str((run/"session").resolve()),
         "--no-approve","--no-context-files","--no-skills","--no-prompt-templates","--no-extensions",
         "-e","builtin:mcp","-e",str(util.ROOT/"agent/online.ts"),"--tools",tools,"--offline"]
    return cmd,env


def first_pass():
    return (os.environ.get("REPLAY_PI_TEST_MODE") or feeds._env_file("pi.env").get("REPLAY_PI_TEST_MODE")) == "1"


def execute(cid, log=print, resume_run=None, full=None):
    previous={}
    if resume_run is None:
        run=prepare(cid, full=wants_full() if full is None else full)
    else:
        run=pathlib.Path(resume_run).resolve()
        if run.parent != (util.camp_dir(cid)/"pi-runs").resolve():raise ValueError("只能继续本活动、本渠道的已有运行")
        previous=util.read_json(run/"run.json",{})
        if previous.get("status")=="running":raise RuntimeError("先结束原进程再继续同一会话")
        sessions=list((run/"session").glob("*.jsonl"))
        if len(sessions)!=1:raise RuntimeError("不能唯一确定原 Pi 会话")
    base_cmd,env=configure(run)
    cmd=list(base_cmd)
    if resume_run is not None:
        cmd += ["--session",str(sessions[0])]
        cmd.append("继续同一个任务。运行配置已纠正为网关支持的100万上下文，在线接口参数兼容已修复。已有会话、查询结果和notes都完整保留，不要从头重新调查。先读旧notes/findings.md，然后根据已有会话与证据补写notes/progress.md和notes/evidence.md，记录已查范围、分页游标/已读行号、关键原话与JSON路径、归属判断和剩余缺口。立刻把已确定的关键节点和事项用 report_add 写进结果，后续查实一条写一条；只补确有缺口的部分，不重复翻全套旧材料。拿不准的写unresolved，不编造。写完现状总结后调用 report_finish 并正常结束。")
    elif (util.read_json(run/"work/seed/brief.json",{}) or {}).get("mode")=="update":
        cmd.append("这是刷新，按提示词「增量更新」一节做：先用 report_find(brief=true) 看现状总结和现有条目，再读 seed/new_since_last.json，按时间上线查 previous_as_of 之后的新情况；查实一条就用 report_add/report_update 写一条，最后更新现状总结并调用 report_finish。")
    else:
        cmd.append("完成 seed/brief.json 指定的整个业务调查。初筛只是示例，请在线查证和补齐；查实一条就用 report_add 写一条，线索随手用 note_add 记下；全部写完后写现状总结并调用 report_finish。")
    stats={"engine":ENGINE,"model":author_model(),"review_model":review_model(),"started_at":previous.get("started_at") or util.now_z(),
           "turns":previous.get("turns",0),"tools":previous.get("tools",{}),"tool_errors":previous.get("tool_errors",0),
           "usage":previous.get("usage") or {"input":0,"output":0,"cacheRead":0,"cacheWrite":0},
           "resumptions":previous.get("resumptions",0)+int(resume_run is not None)}
    if previous:util.write_json(run/f"attempt-{stats['resumptions']}.json",previous)
    util.write_json(run/"run.json",{**stats,"status":"running"})
    started=time.monotonic()
    limit=os.environ.get("REPLAY_PI_MAX_MINUTES") or feeds._env_file("pi.env").get("REPLAY_PI_MAX_MINUTES") or "120"
    remaining=float(limit)*60-float(previous.get("seconds",0))
    deadline=time.monotonic()+max(1,remaining)
    cmd[-1]=cmd[-1]+f"（本次运行还剩约 {int(max(1,remaining)//60)} 分钟。）"  # 第一轮还没有工具结果，开工这句话里先给总时长
    current={"proc":None}
    try:
        mode="a" if resume_run is not None else "w"
        with open(run/"events.jsonl",mode,encoding="utf-8",buffering=1) as events, open(run/"stderr.log",mode,encoding="utf-8") as err:
            def run_once(command, environ, phase="author"):
                """跑一次 Pi 进程直到它结束；返回退出码，到总时长上限返回 "timeout"。耗时按阶段（作者/审核）累计到 run.json。"""
                began=time.monotonic()
                current["events_at"]=events.tell()
                if phase=="author":  # 截止时间（墙钟秒）：扩展在每次工具结果末尾附上剩余时间
                    environ={**environ,"REPLAY_PI_DEADLINE":f"{time.time()+max(0,deadline-time.monotonic()):.0f}"}
                proc=subprocess.Popen(command,cwd=run/"work",env=environ,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=err,
                                      text=True,encoding="utf-8",errors="replace",start_new_session=True)
                current["proc"]=proc
                def consume():
                    for line in proc.stdout:
                        events.write(line)
                        try:event=json.loads(line)
                        except ValueError:continue
                        if event.get("type")=="message_end" and event.get("message",{}).get("role")=="assistant":
                            stats["turns"]+=1
                            for k in stats["usage"]:stats["usage"][k]+=int(event["message"].get("usage",{}).get(k) or 0)
                        if event.get("type")=="tool_execution_end":
                            name=event.get("toolName", "unknown")
                            stats["tools"][name]=stats["tools"].get(name,0)+1
                            stats["tool_errors"]+=int(bool(event.get("isError")))
                reader=threading.Thread(target=consume,daemon=True)
                reader.start()
                try:return proc.wait(timeout=max(1,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                    return "timeout"
                finally:
                    reader.join(timeout=10)
                    phases=stats.setdefault("phase_seconds",{})
                    phases[phase]=round(phases.get(phase,0)+time.monotonic()-began,1)

            def work(command):
                """作者 Agent 干活；没写完就停下时，在同一会话里提醒它接着做（最多 MAX_CONTINUES 次）。
                因模型额度用完而中断时，等额度恢复后在同一会话接着做；等待时间不算进总时长，也不算提醒次数。"""
                nonlocal deadline
                attempt=0
                while attempt<=MAX_CONTINUES:
                    code=run_once(command, env)
                    if code==0 and quota_blocked(run/"events.jsonl",current.get("events_at",0)) and stats.get("quota_waits",0)<6:
                        waited=wait_for_quota()
                        if waited is None:raise RuntimeError("模型额度长时间未恢复；旧页面保留")
                        deadline+=waited
                        stats["quota_waits"]=stats.get("quota_waits",0)+1
                        stats["quota_wait_seconds"]=round(stats.get("quota_wait_seconds",0)+waited,1)
                        session=_one_session(run/"session")
                        if session is None:return
                        command=list(base_cmd)+["--session",str(session),
                            "刚才因为模型额度用完中断了，现在已经恢复。先读 notes/progress.md，从中断处接着做，不要从头重查；"
                            "已经确认的内容及时用 report_add 写进结果，写完现状总结后调用 report_finish 再结束。"]
                        continue
                    attempt+=1
                    if code=="timeout":
                        if not (first_pass() and (run/"work/output/report.json").exists()):
                            raise RuntimeError("Pi 本次运行超时；旧页面保留")
                        stats["timed_out"]=True  # 首轮测试：到时间就展示已写好的草稿
                        return
                    if code:raise RuntimeError(f"Pi 未正常结束（退出码 {code}），日志保留在本次运行目录")
                    update=is_update(run)
                    gap=unfinished(run/"work/output/report.json", update)
                    left=deadline-time.monotonic()
                    if not gap or attempt>MAX_CONTINUES or left<CONTINUE_MIN_SECONDS:return
                    session=_one_session(run/"session")
                    if session is None:return
                    stats["continues"]=stats.get("continues",0)+1
                    first="第一步：现在就用 report_add 把已经查实的关键节点和进行中事项写进结果（笔记里记过的也补进去），不要再先查资料。"
                    if update:  # 刷新没有 Goal：只提醒把结果写完，不催它查全
                        command=list(base_cmd)+["--session",str(session),
                            f"结果还没写完：{gap}。把这次查到的新内容用 report_add/report_update 写进结果，更新现状总结，然后调用 report_finish 结束；"
                            f"不用重新核对旧内容，也不用补上一轮的旧缺口。还剩约 {int(left//60)} 分钟。"]
                        continue
                    command=list(base_cmd)+["--session",str(session),
                        f"Goal 还没有达成：{gap}。{first}然后按 notes/progress.md 的剩余缺口继续查、继续补，分页要翻到最后一页；"
                        f"提示词里 Goal 的全部条件都满足（coverage.remaining 为空）后，写好现状总结并调用 report_finish 结束。还剩约 {int(left//60)} 分钟。"]

            work(cmd)
            if not (run/"work/output/report.json").exists():raise RuntimeError("Pi 已结束但未输出回放文件（已提醒续做仍没有写出）")
            # 审核：作者写完（status=complete）后，审核 Agent 只用一个工具给出「通过 / 打回+建议」；
            # 打回就把建议交回作者的同一会话去改，再审。最多 MAX_REVIEWS 轮；仍不通过也照常展示，结论记在 run.json。
            update_mode=is_update(run)
            if update_mode:stats["review_skipped"]="update"  # 增量更新不审核（用户定的）：只补新时间段，求快
            for rnd in range(1,MAX_REVIEWS+1):
                if update_mode or not reviews_on() or stats.get("timed_out") or unfinished(run/"work/output/report.json"):break
                if deadline-time.monotonic()<REVIEW_MIN_SECONDS:break
                verdict=review(run, rnd, run_once, env)
                if not verdict.get("decision") and quota_blocked(run/"events.jsonl",current.get("events_at",0)):  # 审核时额度用完：等恢复后重审这一轮
                    waited=wait_for_quota()
                    if waited is None:break
                    deadline+=waited
                    stats["quota_wait_seconds"]=round(stats.get("quota_wait_seconds",0)+waited,1)
                    verdict=review(run, rnd, run_once, env)
                stats.setdefault("reviews",[]).append({"round":rnd,"decision":verdict.get("decision"),
                    "summary":str(verdict.get("summary") or ""),"suggestions":verdict.get("suggestions") or []})
                if verdict.get("decision")!="reject":break
                if rnd==MAX_REVIEWS:
                    stats["review_failed"]=True
                    break
                session=_one_session(run/"session")
                if session is None:break
                work(list(base_cmd)+["--session",str(session),rejection_message(rnd,verdict,deadline)])
        # 发布前程序只核对格式和原话是否存在；内容好坏由上面的审核 Agent 判断。
        from .pi_result import publish
        live=util.read_json(util.out_dir(cid)/"business.case.zh.json",{}) or {}
        if stats.get("review_failed") and (live.get("meta") or {}).get("engine")==ENGINE:
            # 审核两轮仍没通过、线上已有一版 V3：保留旧页面，结论只记后台（不让更差的结果替换掉能看的版本）
            stats.update(status="kept_previous")
            return {"engine":ENGINE,"run":run.name,"kept_previous":True}
        result=publish(cid,run/"work")
        stats.update(status="completed",result=result)
        util.write_json(util.camp_dir(cid)/"pi-published.json",{"run":run.name,"published_at":util.now_z(),
                        "mode":(util.read_json(run/"work/seed/brief.json",{}) or {}).get("mode")})
        return {"engine":ENGINE,"run":run.name,**result}
    except Exception:
        stats["status"]="failed"
        raise
    finally:
        proc=current["proc"]
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        stats.update(finished_at=util.now_z(),seconds=round(time.monotonic()-started+float(previous.get("seconds",0)),1))
        util.write_json(run/"run.json",stats)
        util.write_json(util.camp_dir(cid)/"pi-latest.json",{"run":run.name,**stats})
        log(f"Pi {stats.get('status')}，{stats['turns']} 轮，{stats['seconds']} 秒")
