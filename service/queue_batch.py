"""用外包队列把还没做完的活动跑完。

2026-10-31 用户定：全部活动都开始，AI 那几步放进队列慢慢做；放不进队列的（取数据、整理原料、生成页面）服务器上一次一个活动地做
（pipeline._server_turn 管：批量、每天的刷新、首页刷新都排同一个队）。自家处理方 8 个（10-31 下午用户定，凌晨是 2 个），外部 agent 一起领题。

- 只缺业务版的（有原料、有详细版）：只补 AI 两步和页面，不重新取数（refresh.py --fill-ai）；从没生成过的：完整跑（refresh.py <编号>）。
- 活动编号从大到小排好先后，第 k 个活动出的题优先级是 k（AI_QUEUE_PRIORITY=k）：数字越小越先派，先开始的活动先做完，
  页面一个个出来，不会全部拖到最后。首页刷新、每天刷新的题（优先级 0）始终排在批量前面。
- 同时开着的活动最多 LANES 个（QUEUE_BATCH_LANES，默认 8）：服务器只有 2 核、8G 内存，还跑着公司的网关，每个开着的活动
  占 60MB 上下，187 个一起开会把机器撑爆。一个做完自动接下一个；开着的活动够多，队列里一直有题给各处理方领。
- 每道题最多等 QUEUE_BATCH_WAIT_HOURS（默认 48）小时：排在后面的活动要等前面的做完，不能用平时的 6 小时。
- 轮到某个活动时再看一眼：已经有业务版的、正在处理的跳过。只跑一遍，最后列出没做成的。
在 app 目录下运行（要带上服务用的环境变量 REPLAY_SECRETS、REPLAY_MCP_CREDENTIALS）：
  setsid -f python3 -u service/queue_batch.py > ../logs/queue_batch.log 2>&1 < /dev/null
每个活动一行 START / END；结束标志 QUEUE-BATCH-DONE。
"""
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from replay import pipeline  # noqa: E402
from replay.util import OUT, RAW, camp_dir  # noqa: E402

LANES = int(os.environ.get("QUEUE_BATCH_LANES", "8"))
WAIT_HOURS = os.environ.get("QUEUE_BATCH_WAIT_HOURS", "48")


def log(*parts):
    """一行一次写完：几个活动同时跑时，日志行不会串在一起。"""
    line = " ".join([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *map(str, parts)])
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def done(n):
    return (OUT / n / "business.en.html").exists()


def plan():
    """还没做完的活动 → [(编号, fill | full)]，新的在前。"""
    ids = sorted((int(c["id"]) for c in json.loads((RAW / "campaign_list.json").read_text(encoding="utf-8"))["campaigns"]), reverse=True)
    out = []
    for i in ids:
        n = str(i)
        if done(n):
            continue
        has_detail = (OUT / n / "replay.en.html").exists()
        out.append((n, "fill" if has_detail and (camp_dir(n) / "corpus.json").exists() else "full"))
    return out


def run(item):
    rank, n, mode = item
    if done(n):
        log("SKIP", n, "（已经有业务版）")
        return n, mode, "skip"
    if pipeline.read_state(n).get("running"):
        log("SKIP", n, "（正在处理）")
        return n, mode, "skip"
    t = time.time()
    log("START", n, mode, f"rank={rank}")
    env = dict(os.environ, AI_EXECUTOR="queue", AI_QUEUE_PRIORITY=str(rank), AI_QUEUE_WAIT_HOURS=WAIT_HOURS)
    args = [sys.executable, "-u", "service/refresh.py"] + (["--fill-ai"] if mode == "fill" else []) + [n]
    p = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True)
    last = (p.stdout.strip().splitlines() or [p.stderr.strip()[-200:] or "no output"])[-1]
    log("END", n, mode, f"{(time.time() - t) / 60:.1f}min", last[:200], "| business ok" if done(n) else "| NO business")
    return n, mode, "ok" if last.startswith("OK") and done(n) else last[:200]


def main():
    todo = plan()
    fills = sum(1 for _, m in todo if m == "fill")
    log(f"START RUN: {len(todo)} campaigns（只补 AI {fills}，完整跑 {len(todo) - fills}），同时开着最多 {LANES} 个，"
        f"每道题最多等 {WAIT_HOURS} 小时，新的在前：" + " ".join(f"{n}:{m}" for n, m in todo[:8]) + " ...")
    with concurrent.futures.ThreadPoolExecutor(LANES) as pool:
        results = list(pool.map(run, [(i + 1, n, m) for i, (n, m) in enumerate(todo)]))
    bad = [(n, m, r) for n, m, r in results if r not in ("ok", "skip")]
    log(f"SUMMARY: ok {sum(r == 'ok' for _, _, r in results)}, skipped {sum(r == 'skip' for _, _, r in results)}, problems {len(bad)}")
    for n, m, r in bad:
        log("PROBLEM", n, m, r)
    log("QUEUE-BATCH-DONE")


if __name__ == "__main__":
    main()
