"""按活动刷新：找出活动下的链接 → 逐条链接取 GitHub → 逐条链接取后台与沟通记录 → 合并生成两版页面。

- 沟通记录（Slack、邮件、HubSpot 工单、会议）：REPLAY_SOURCE=direct 时先把直连副本刷新到最新（replay/feeds.py，
  直接读 FeedAI / Fireflies / Femini），再从副本里找；否则照旧经过公司大脑；
- 同一个活动同时只跑一个（文件锁，进程之间也生效）；
- 每一步的结果、耗时、失败原因都写进 state/<编号>.json，服务重启后还在；
- 任一步失败都不会覆盖上一次成功生成的页面（页面是最后一步才原子替换的）；
- 「AI 原料」「进行中事项」「业务版」这几步（replay/corpus.py、progress.py、business.py）失败只记录，不影响页面；
- GitHub 没有凭证时只算「跳过」，沿用已有的 GitHub 数据，不算失败。
"""
import contextlib
import fcntl
import time
import traceback

from . import build, business, campaign, collect_mcp, corpus, feeds, github, jobqueue, pi_runner, progress, scope
from .util import STATE, now_z, read_json, valid_number, write_json

STATE.mkdir(parents=True, exist_ok=True)


def state_path(n, vendor=None):
    n = valid_number(n)
    vendor = scope.valid_vendor(vendor) if vendor is not None else scope.vendor_for(n)
    return STATE / n / "vendors" / vendor / "state.json" if vendor else STATE / f"{n}.json"


def read_state(n, vendor=None):
    return read_json(state_path(n, vendor), {}) or {}


def _step(res, name, fn):
    t0 = time.time()
    try:
        out = fn()
        res["steps"].append(dict(step=name, ok=True, seconds=round(time.time() - t0, 1), info=out))
        return out
    except Exception as e:  # 取数/生成中的任何错误都记下来，不让服务崩
        res["steps"].append(dict(step=name, ok=False, seconds=round(time.time() - t0, 1),
                                 error=f"{type(e).__name__}: {str(e)[:300]}", trace=traceback.format_exc()[-600:]))
        raise


@contextlib.contextmanager
def _server_turn():
    """不调 AI 的活儿（取数据、整理原料、生成页面）整台服务器排队，一次只做一个活动（2026-10-31 用户定：
    能放进队列的放队列，放不进的服务器串行跑）。批量、每天的刷新、首页刷新都排这一个队。
    AI 那几步不占这个位置：一个活动在队列里等答案时，别的活动可以接着取数据。进程退出时自动让出。"""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "server-work.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _each(links, fn, status, key):
    """对活动下的每条链接取数。GitHub 没有凭证 = 全部跳过；个别链接失败不拖垮整个活动，但全部失败要报错。"""
    infos, errors = {}, {}
    for n in links:
        try:
            infos[n] = fn(n, log=lambda *_: None)
        except Exception as e:
            errors[n] = str(e)[:200]
    if errors and not infos:
        raise RuntimeError("全部链接取数失败：" + "；".join(f"{k}: {v}" for k, v in list(errors.items())[:3]))
    sts = [i.get("status") for i in infos.values() if isinstance(i, dict) and "status" in i]
    status[key] = {"status": "skipped" if sts and all(x == "skipped" for x in sts) else "ok", "links": len(links),
                   "failed": errors}
    return status[key]


def _ask(n, step):
    """AI 调用走哪里：AI_EXECUTOR=queue 时出题到外包队列（外部 agent 或自家处理方来做，见 replay/jobqueue.py），
    否则照旧直接调网关（None = 各步骤用默认的 progress.chat）。"""
    return jobqueue.asker(n, step) if jobqueue.enabled() else None


def reserve(n):
    """先占住活动锁；网页任务排队时也不允许外进程抢跑。调用方负责释放。"""
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / f"{valid_number(n)}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def refresh(n, log=print, vendor=None, *, _reserved_lock=None):
    with scope.use(n, vendor):
        if _reserved_lock is not None:
            fcntl.flock(_reserved_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _refresh_locked(valid_number(n))
        return _refresh_variant(n, log)


def _refresh_variant(n, log=print):
    n = valid_number(n)
    lock = reserve(n)
    if lock is None:
        return dict(number=n, ok=False, busy=True, error="这个活动正在刷新")
    try:
        return _refresh_locked(n)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _refresh_locked(n):
    prev = read_state(n)
    res = dict(number=n, vendor_id=scope.vendor_for(n), started_at=now_z(), steps=[], ok=False)
    write_json(state_path(n), {**prev, "running": True, "started_at": res["started_at"]})
    status = {}
    try:
        with _server_turn():  # 取数据、整理原料：整台服务器一次一个活动
            doc = _step(res, "活动与链接", lambda: campaign.discover(n, log=lambda *_: None))
            links = doc["links"]
            _step(res, "GitHub", lambda: _each(links, github.collect, status, "github"))
            direct = feeds.enabled()
            if direct:  # 先把 Slack、邮件、工单、会议的直连副本刷新到最新（够新就跳过；刷新失败就用已有副本并记下）
                _step(res, "更新沟通记录", lambda: feeds.ensure_fresh(log=lambda *_: None))
            _step(res, "后台与沟通记录" if direct else "后台与大脑", lambda: _each(links, collect_mcp.collect, status, "links"))
            _step(res, "渠道清单", lambda: campaign.update_inventory(n))
            try:  # 每日投放数据（证据图用）取不到不影响页面
                _step(res, "每日投放数据", lambda: collect_mcp.collect_metrics(n, log=lambda *_: None))
            except Exception:
                pass
            try:  # AI 原料（全部沟通的整理）取不到，不影响页面生成；失败原因记在这一步里
                _step(res, "AI 原料", lambda: corpus.build(n, log=lambda *_: None))
                material = True
            except Exception:
                material = False
        if pi_runner.enabled():
            if not material:
                raise RuntimeError("第一轮资料准备失败，不能启动 Pi；旧页面保留")
            _step(res, "智能体业务回放", lambda: pi_runner.execute(n, log=lambda *_: None))
        elif material and progress.configured():  # 旧规则路径保留用于回退；Pi 不走分题队列
            try:
                _step(res, "进行中事项", lambda: progress.build(n, log=lambda *_: None, ask=_ask(n, "进行中事项")))
                _step(res, "业务版", lambda: business.build(n, log=lambda *_: None, ask=_ask(n, "业务版")))
            except Exception:
                pass
        with _server_turn():
            _step(res, "生成页面", lambda: build.build(n, status))
        res["ok"] = True
    except Exception as e:
        res["error"] = str(e)[:300]
    res["finished_at"] = now_z()
    final = dict(number=n, running=False, last=res)
    if res["ok"]:
        final["last_ok"] = res
        final["campaign"] = (read_json(build.out_dir(n) / "build.json") or {}).get("campaign")
    else:
        final["last_ok"] = prev.get("last_ok")
        final["campaign"] = prev.get("campaign")
    write_json(state_path(n), final)
    return res


def _prior_status(prev):
    """上一次真正取数时记下的 GitHub、后台与大脑状态（详细版「数据来源」用）。只补 AI 时不重新取数，就沿用它。"""
    status = {}
    for run in (prev.get("last"), prev.get("last_ok")):
        for key, val in ((run or {}).get("carried_status") or {}).items():  # 上一次也是只补 AI：用它沿用的那份
            status.setdefault(key, val)
        for step in (run or {}).get("steps") or []:
            key = {"GitHub": "github", "后台与大脑": "links", "后台与沟通记录": "links"}.get(step.get("step"))
            if key and key not in status and step.get("ok") and isinstance(step.get("info"), dict):
                status[key] = step["info"]
    return status


def fill_ai(n, log=print, vendor=None, *, resume_run=None):
    with scope.use(n, vendor):
        return _fill_variant(n, log, resume_run=resume_run)


def _fill_variant(n, log=print, *, resume_run=None):
    """只补 AI 两步（进行中事项、业务版）和页面：取数、原料都用已有的，不重新跑（用户定的规矩：跑过的不整个重跑，缺哪步补哪步）。
    和 refresh 一样加锁、记 state（首页显示处理中，结果记进 last / last_ok）。AI 调用照样按开关走队列。"""
    n = valid_number(n)
    lock = open(STATE / f"{n}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return dict(number=n, ok=False, busy=True, error="这个活动正在刷新")
    try:
        return _fill_locked(n, resume_run=resume_run)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _fill_locked(n, *, resume_run=None):
    from .util import camp_dir
    prev = read_state(n)
    res = dict(number=n, vendor_id=scope.vendor_for(n), started_at=now_z(), steps=[], ok=False, mode="fill_ai")
    write_json(state_path(n), {**prev, "running": True, "started_at": res["started_at"]})
    try:
        if not (camp_dir(n) / "corpus.json").exists():
            raise RuntimeError("还没有 AI 原料，要跑完整流程")
        ai_ok = True
        if pi_runner.enabled():
            _step(res, "智能体业务回放", lambda: pi_runner.execute(n, log=lambda *_: None, resume_run=resume_run))
        else:
            try:
                _step(res, "进行中事项", lambda: progress.build(n, log=lambda *_: None, ask=_ask(n, "进行中事项")))
                _step(res, "业务版", lambda: business.build(n, log=lambda *_: None, ask=_ask(n, "业务版")))
            except Exception:
                ai_ok = False
        res["carried_status"] = _prior_status(prev)
        with _server_turn():
            _step(res, "生成页面", lambda: build.build(n, res["carried_status"]))
        res["ok"] = ai_ok
        if not ai_ok:
            res["error"] = "AI 步骤没做完（原因见各步）"
    except Exception as e:
        res["error"] = str(e)[:300]
    res["finished_at"] = now_z()
    final = dict(number=n, running=False, last=res)
    if res["ok"]:
        final["last_ok"] = res
        final["campaign"] = (read_json(build.out_dir(n) / "build.json") or {}).get("campaign") or prev.get("campaign")
    else:
        final["last_ok"] = prev.get("last_ok")
        final["campaign"] = prev.get("campaign")
    write_json(state_path(n), final)
    return res


def known_numbers():
    """已经登记过的编号：有取数记录或有生成结果的。"""
    nums = set()
    from .util import OUT, RAW
    for base in (RAW / "campaigns", OUT, STATE):
        if base.exists():
            for p in base.iterdir():
                stem = p.stem if p.is_file() else p.name
                if stem.isdigit() and 1 <= len(stem) <= 9:
                    nums.add(stem)
    return sorted(nums)


def refreshable_numbers():
    """定时刷新（service/refresh.py 不带编号时）要处理的活动：成功生成过页面的。从没成功过的不重复尝试。"""
    from .util import OUT
    return [n for n in known_numbers() if (OUT / n / "replay.en.html").exists() or read_state(n).get("last_ok")]
