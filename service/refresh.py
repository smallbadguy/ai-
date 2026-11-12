"""命令行刷新入口，给 systemd 定时器使用：先更新活动清单，再刷新全部已生成过的活动（一个失败不影响其他）。

用法：python3 service/refresh.py            刷新全部
      python3 service/refresh.py 5165 ...   只刷新指定活动（编号）
      python3 service/refresh.py --fill-ai 5165 ...   只补 AI 两步和页面，不重新取数（缺业务版的活动用）
全部成功返回 0，有任何一个失败返回 1。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from replay import campaign, pipeline  # noqa: E402
from replay.util import valid_number  # noqa: E402


def main(argv):
    fill = "--fill-ai" in argv[1:]
    args = [x for x in argv[1:] if x != "--fill-ai"]
    nums = [valid_number(x) for x in args] or ([] if fill else pipeline.refreshable_numbers())
    bad = 0
    if not args and not fill:
        try:
            print("LIST", campaign.refresh_list(), flush=True)
        except Exception as e:  # 清单更新失败不影响已生成活动的刷新
            print(f"LIST FAIL {str(e)[:200]}", flush=True)
            bad += 1
    for n in nums:
        res = pipeline.fill_ai(n) if fill else pipeline.refresh(n)
        tag = "OK " if res["ok"] else ("BUSY" if res.get("busy") else "FAIL")
        print(f"{tag} {n} {res.get('error', '')}".strip(), flush=True)
        bad += 0 if res["ok"] else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
