"""命令行：python3 -m replay <命令> <活动编号>

  list              刷新全部在跑的活动清单（下拉列表用）
  discover  C       找出活动下有哪些链接
  collect-gh  C     取 GitHub（本机用 gh；服务器有令牌时用令牌）
  collect-mcp C     取后台与大脑（需要 REPLAY_SECRETS）
  build       C     由已取到的原始数据生成中英文页面（不联网）
  refresh     C     依次：discover → collect-gh → collect-mcp → build
  diagnose    C 日期 [范围天数] [描述…]   异常排查（spend 录入），如 diagnose 2570 2026-10-03 3 18894 暂停后还在录 spend
  dict-build        P0 建字典：从 GitHub 的 spend 单子挖错误模式并进字典（knowledge/）
  diagnose-regress  用当前字典重跑回归集（不受 6 小时间隔限制）
  diagnose-followup 回访所有到期的排查（服务里每 30 分钟自动做一次）
  dict-curate       字典自动整理：MiniMax 审候选（保留 / 合并 / 删掉），回归集变差就回滚（不受 6 小时间隔限制）
C 是活动（campaign）编号；活动下的每条链接会自动逐条取数。
"""
import json
import sys

from . import campaign, collect_mcp, github
from .util import valid_number


def main(argv):
    if len(argv) >= 2 and argv[1] == "list":
        print(campaign.refresh_list())
        return 0
    if len(argv) >= 2 and argv[1] == "dict-build":
        from . import dict_build
        dict_build.build()
        return 0
    if len(argv) >= 2 and argv[1] == "diagnose-regress":
        from . import diagnose
        print(json.dumps(diagnose.regress(force=True), ensure_ascii=False, indent=1))
        return 0
    if len(argv) >= 2 and argv[1] == "dict-curate":
        from . import curate
        print(json.dumps(curate.run(force=True), ensure_ascii=False, indent=1))
        return 0
    if len(argv) >= 2 and argv[1] == "diagnose-followup":
        from . import diagnose
        for cid, date, rid in diagnose.due_followups():
            r = diagnose.followup(cid, date, rid)
            print(cid, date, rid, (r or {}).get("followup"), [c["action"] for c in (r or {}).get("learning", [])][-3:])
        return 0
    if len(argv) >= 4 and argv[1] == "diagnose":
        from . import diagnose
        rng = int(argv[4]) if len(argv) >= 5 and argv[4].isdigit() else diagnose.DEFAULT_RANGE
        query = " ".join(argv[5:] if len(argv) >= 5 and argv[4].isdigit() else argv[4:])
        run, _ = diagnose.prepare(argv[2], argv[3], query, rng)
        r = diagnose.run(argv[2], run["date"], run["id"])
        print(json.dumps({k: r.get(k) for k in ("id", "stage", "error", "learning")}, ensure_ascii=False))
        print(json.dumps((r.get("cause") or {}).get("summary_zh"), ensure_ascii=False))
        return 0 if r.get("stage") == "done" else 1
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, c = argv[1], valid_number(argv[2])
    if cmd == "discover":
        print(campaign.discover(c))
    elif cmd == "collect-gh":
        for n in campaign.links(c):
            print(n, github.collect(n))
    elif cmd == "collect-mcp":
        for n in campaign.links(c):
            print(n, collect_mcp.collect(n))
    elif cmd == "build":
        from . import build
        print(build.build(c))
    elif cmd == "refresh":
        from . import pipeline
        res = pipeline.refresh(c)
        print(res)
        return 0 if res["ok"] else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
