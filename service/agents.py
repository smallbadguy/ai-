"""登记、查看、调整外包处理方（agent）。在 stage 服务器的 app 目录下运行：

  python3 service/agents.py add <名字> --context 200000 [--model 模型名] [--note 备注]
        登记一个外部处理方，打印它的钥匙（只显示这一次，库里只存摘要；丢了就用 token 换一把）
  python3 service/agents.py list                      列出全部处理方和它们的状态
  python3 service/agents.py set <名字> [--context N] [--model M] [--note T]   改登记信息（比如换了模型、上下文变了）
  python3 service/agents.py disable <名字>            停用（领不到题，钥匙也不能用）
  python3 service/agents.py enable <名字>             恢复
  python3 service/agents.py token <名字>              换一把新钥匙，旧的立即失效
  python3 service/agents.py unpause <名字>            解除「连续超时暂停」

--context 是这个处理方一次能读的 token 数（模型的上下文，比如 200000 或 1000000）。派题时按它判断读不读得下，不切题。
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from replay import jobqueue as Q  # noqa: E402


def show(agents):
    if not agents:
        print("还没有登记处理方")
        return
    for a in agents:
        state = "已停用" if not a["enabled"] else (f"处理中 {a['holding']['job_id']}" if a.get("holding") else
                                                  (f"暂停到 {a['paused_until']} UTC" if a["paused_until"] else "空闲"))
        kind = "自家" if a["internal"] else "外部"
        print(f"{a['name']:<24} {kind} 一次能读 {a['context_tokens']:>8} token  模型 {a['model'] or '-':<24} {state}"
              f"  完成 {a['done_count']} 超时 {a['timeout_count']} 退回 {a['rejected_count']} 迟到 {a['late_count']} 放弃 {a['released_count']}"
              f"  最近来过 {a['last_seen_at'] or '-'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="登记、查看、调整外包处理方（agent）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="登记外部处理方，打印钥匙")
    add.add_argument("name")
    add.add_argument("--context", type=int, required=True, help="一次能读多少 token（模型上下文）")
    add.add_argument("--model", default="")
    add.add_argument("--note", default="")
    sub.add_parser("list", help="列出处理方")
    st = sub.add_parser("set", help="改登记信息")
    st.add_argument("name")
    st.add_argument("--context", type=int)
    st.add_argument("--model")
    st.add_argument("--note")
    for cmd in ("disable", "enable", "token", "unpause"):
        sub.add_parser(cmd).add_argument("name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            token = Q.add_agent(args.name, args.context, model=args.model, note=args.note)
            print(f"已登记 {args.name}（一次能读 {args.context} token）。钥匙只显示这一次，请交给这个处理方保存：")
            print(token)
        elif args.cmd == "list":
            show(Q.list_agents())
        elif args.cmd == "set":
            Q.update_agent(args.name, context_tokens=args.context, model=args.model, note=args.note)
            print("已更新")
        elif args.cmd in ("disable", "enable"):
            Q.update_agent(args.name, enabled=args.cmd == "enable")
            print("已停用" if args.cmd == "disable" else "已恢复")
        elif args.cmd == "token":
            print(f"{args.name} 的新钥匙（旧的已失效，只显示这一次）：")
            print(Q.rotate_token(args.name))
        elif args.cmd == "unpause":
            Q.update_agent(args.name, unpause=True)
            print("已解除暂停")
    except ValueError as e:
        print(f"没有做：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
