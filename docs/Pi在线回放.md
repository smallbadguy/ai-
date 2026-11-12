# Pi 在线回放（v3 第一期）

## 实际链路

1. 仍由原流水线取第一轮数据、做关键词初筛，作为示例。
2. 每个「活动 + 渠道」启动一个独立 Pi 会话（MiniMax `minimax-m3.1-flash`）。
3. Pi 直接连接云端 Femini MCP；通过薄工具直接调用 FeedAI/Qdrant、GitHub、Fireflies API。自主查询、翻页、看上下文、判断归属、选择/合并后台调整、整理业务过程和进行中事项。
4. 写 `output/report.json`，正常结束。**无提交结束工具、无拦截续跑、无第二个审核 Agent、无外包分题队列。**
5. 程序只检查结果能否渲染、引用原话是否存在，再用原模板生成中英文业务版。失败保留旧页，不再用关键词归属规则删 AI 的结论。详细版仍走原程序逻辑。

没有全文镜像、检索库、自动全库抓取工具，也没有接回大脑 MCP。`results/` 仅记录当前会话实际查过的响应，用于上下文和引用。

## 文件

- `agent/online-prompt.md`：完整提示词、业务背景、工具说明、调查方法和 JSON 约定。
- `agent/online.ts`：Pi 扩展；三个在线 API 工具、MCP 查询回执、文件边界。没有自写 Agent loop。
- `replay/pi_tools.py`：只读 API 请求；Femini 只读工具精确白名单。FeedAI 集合范围由云端凭据决定，不局限于旧初筛清单。
- `replay/pi_runner.py`：每次准备初筛示例，复用云端凭据，启动 Pi 并保存会话与用量。
- `replay/pi_result.py`：正常结束后格式/原话核对与原模板适配，不另起审核流程。
- `tests/test_pi_online.py`：在线请求透传、禁止写入、范围隔离、原话、失败保页、自然结束等测试。

## 云端配置

所有凭据沿用回放服务器 `~/ai-2734-replay-stage/secrets/`，详见 [云端凭据清单](云端数据源与凭据搬运.md)。
只新增非敏感选择文件 `secrets/pi.env`：`REPLAY_ENGINE=pi`。未启用时保留旧规则路径用于回退，不自动因安装 Pi 就启用。

- Pi：`~/ai-2734-replay-stage/runtime/pi/bin/pi`，1.5.0。
- Node：同级 `runtime/node/bin`，22.23.4，不改变系统 Node。
- 每次配置在运行目录的 `config/`，只保存环境变量引用，不保存明文 key。
- 明确指定 Flash，不继承旧 `ai.env` 中的 `minimax-m3`，也不继承 `AI_EXECUTOR=queue`。
- 原生 MCP 默认为 hidden，具体读取工具设为 direct；未知或新增工具不会自动放行。
- API 只允许读取操作；不是简单禁止 HTTP POST，因为读取接口也使用 POST。
- Agent 只能读当前工作目录，写 `notes/` 和 `output/report.json`，不能改初筛和查询回执、不能读取凭据、不能执行 shell。

## 每次任务的记录

路径：`raw/campaigns/<cid>/[vendors/<partner_id>/]pi-runs/<时间-随机编号>/`。

- `work/seed/`：原第一轮资料，不是最终归属结论。
- `work/results/`：本次实际在线请求的响应；Agent 不可改写。
- `work/notes/`：Agent 笔记。
- `work/output/report.json`：最终结果。
- `session/`：Pi 完整上下文；`events.jsonl`：工具与模型事件；`stderr.log`：运行错误。
- `run.json`：耗时、工具次数、token 用量、成功/失败。
- `previous/`：本次替换前的业务版和 AI 结果。

同一活动仍共用活动锁；不同渠道不得同时改同一活动。Pi 使用自己的正常循环和上下文管理，程序默认仅有 120 分钟的运行保护上限，超时记失败而不是冒充完成。

## 发布与回退

stage 已于 2026-11-02 切换新生成入口。业务页面只有各自正常生成成功后才成为 v3；未刷新的旧页不会伪标 v3。
v2 发布前快照：`~/ai-2734-replay-stage/backups/20261102T153204Z-v2-before-pi-25ec7d4/`。

回退先处理正在运行的任务，不能让旧进程随后覆盖恢复的数据：停止接受新生成，等待任务完成或取得明确中断授权；保存当前 v3 结果，再恢复 v2 标签代码和该范围的页面/AI 结果。不要覆盖网页登录凭据、队列 agent keys 或未涉及的活动；每日刷新 timer 保持关闭。切 `REPLAY_ENGINE=rules` 只影响新任务，不会自动取消已经开始的 Pi 会话。

## 一期能力边界

原话存在不等于业务判断一定正确：归属理由、摘要、负责人和完成状态主要由 Agent 判断。第二个独立审核 Agent 尚未实现，不能称为已通过业务审核。
Fireflies 账号非管理员；部分上游记录只有摘要或数据不完整；搜索有分页和字段类型限制。Agent 应在 coverage 标明这些限制，不把首次搜索未命中解释成没有资料。
