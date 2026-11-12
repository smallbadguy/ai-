# Campaign 回放

在下拉列表里选一个 campaign（活动），系统去 GitHub、后台（Femini）、HubSpot、Slack 里找这个活动的全部记录，按时间排成一段可回放的案卷：谁提出、谁接了谁的单子、谁评审、谁合并。每个活动生成中英文两版独立页面，可以在首页随时刷新；要不要每天定时刷新看部署（正式站开着，stage 2026-11-02 起关闭，见 `docs/stage部署.md`）。

**数据源、工具和实现方案，见 `docs/实现方案.md`**（面向同事的方案文档）。事件格式见 `docs/事件格式.md`。

**异常排查（spend 录入异常）**：打开任一活动的回放页，点右上角「异常排查」。选异常日期（必选），可填现象 / click URL / line item 和前后范围，系统用 MiniMax 按字典归因，输出错误配置、操作人证据、相关工单及修复状态；之后用排查日之后的后台改动、沟通记录和 GitHub 自动验证并回访，字典由程序和 MiniMax 自动学习、自动整理，不需要人。见 [`docs/异常排查.md`](docs/异常排查.md)。

## 使用（服务器 feedmob1）

- 访问地址：https://replay.feedmob.it.com/ （Caddy 反向代理，**没有登录**，知道域名的人都能打开；HTTPS 证书自动续期；页面带禁止搜索引擎收录）。服务本身只监听 `127.0.0.1:8791`，不直接对外。
- 临时不走域名时也可以：`ssh -L 8791:127.0.0.1:8791 feedmob1`，然后打开 http://127.0.0.1:8791/ 。
- 站点配置在 feedmob1 的 `/etc/caddy/Caddyfile` 末尾的 `replay.feedmob.it.com` 一段（改动前的备份：`/etc/caddy/Caddyfile.bak-before-replay-*`）。要重新加账号密码：在那一段里加 `basicauth { 用户名 哈希 }`（这台 Caddy 是 2.6，指令写 `basicauth`；哈希用 `caddy hash-password` 生成），校验后 `sudo systemctl reload caddy`。
- 首页：从下拉列表选一个活动（可先在旁边的框里输入关键字筛选）→「生成回放」；下面「已生成的活动」里每个活动有英文/中文链接、刷新状态和「刷新」按钮（2026-11-02 起没有「刷新全部」，要逐个刷新）。
- 下拉列表是后台里全部在跑的活动（约 224 个），每天自动更新；只能选清单里的活动，不用填任何编号。
- 新活动第一次生成要几十秒到几分钟（要按天读相关 Slack 频道），之后刷新更快（已读的日期有缓存）。
- 取数失败时页面保持上一次成功的版本，首页写明失败原因；从没成功过的活动不参与每天的自动刷新。
- 定时刷新：`ai-2734-replay-refresh.timer`，每天 03:30 UTC 运行 `service/refresh.py`（先更新活动清单，再刷新全部成功过的活动，一个失败不影响其他）。
- systemd 单元模板在 `deploy/`；线上实际为 `/etc/systemd/system/` 下的系统级服务，使用 `sudo systemctl` 管理。
- 路径：代码 `~/ai-2734-replay/app`；凭证 `~/ai-2734-replay/secrets`（600 权限，不入库）。
- Chime 人工精选版：`/showcase/chime/en`、`/showcase/chime/zh`，只读独立发布的固定页面，不取数、不自动刷新，未发布时不会回退到自动版。数据原则和发布目录见 [精选版说明](docs/精选版.md)。
- 同步代码时排除 `raw/`、`out/`、`state/`、`showcase/`、`knowledge/` 和凭证目录；不要用代码发布覆盖自动版数据、人工精选成品或线上学到的异常字典。

### GitHub 这一路

服务器自己取 GitHub：令牌放在 `~/ai-2734-replay/secrets/github.env`（内容 `GITHUB_TOKEN=...`，600 权限，不入库）。目前用的是本机 gh 登录的那把令牌，带 `repo`、`workflow` 权限（能读也能写），代码里只发读取请求（GET）。以后建议换成只读令牌：新建后覆盖这个文件即可，不用改代码。没有令牌时 GitHub 这一步会「跳过」、沿用已有数据，页面「核对」里会写明。

## 命令行

```bash
python3 -m replay list            # 刷新全部在跑的活动清单（下拉列表用）
python3 -m replay discover    C   # 找出活动 C 下面有哪些链接
python3 -m replay collect-gh  C   # GitHub（有令牌用令牌，否则用本机 gh），活动下每条链接逐条取
python3 -m replay collect-mcp C   # 后台 + 大脑（需要 REPLAY_SECRETS 指向凭证目录）
python3 -m replay build       C   # 由已取到的原始数据生成 out/C/ 下的两版页面（不联网）
python3 -m replay refresh     C   # 依次：以上各步
python3 service/refresh.py [C…]   # 刷新指定活动；不带编号则先更新清单，再刷新全部成功过的活动
python3 -m replay diagnose C 日期 [范围天数] [描述…]   # 异常排查（同首页每一行的「异常排查」页面）
python3 -m replay dict-build                     # P0：从 GitHub 的 spend 单子建字典
python3 -m replay diagnose-regress               # 用当前字典重跑回归集
python3 -m replay diagnose-followup              # 回访到期的排查（服务里的自动回访默认关，REPLAY_DIAG_FOLLOWUP=1 才开）
python3 -m replay dict-curate                    # 字典自动整理（服务里定期自动做）
python3 -m unittest discover -s tests
```

## 目录

```
replay/            按活动的通用流程
  campaign.py        活动清单（下拉列表）；一个活动下面有哪些链接
  github.py          按链接编号搜 GitHub 单子，拉指派/标签/评论/代码合并请求
  collect_mcp.py     后台（利润率等调整）与大脑（HubSpot 工单、相关 Slack 频道按天全量）
  events.py          各来源 → 统一事件（文字全部来自固定模板）；一个活动下多条链接的记录合在一起
  people.py          人物对照（config/people.json）；没见过的账号自动登记为「未核对」
  build.py           组装数据、生成 out/N/replay.{zh,en}.html
  scene.py           把原版 3D 场景、依赖与活动数据封装进离线单文件页面
  pipeline.py        刷新流水线：文件锁防并发、状态落盘（state/）、失败不覆盖旧页面
  spend_checks.py    异常排查：spend 录入信号（程序逐天核数）
  diagnose.py        异常排查流程：取证据（原因阶段不读 GitHub）→ MiniMax 归因 → 修复状态 / GitHub 验证 → 学习
  diagnose_ai.py     异常排查的提示词和程序把关
  knowledge.py       异常字典（种子 + 线上学到的）、学习规则、回归集
  dict_build.py      P0：从 GitHub 建字典
  curate.py          字典自动整理：MiniMax 审候选（保留 / 合并 / 删掉），回归集兜底
service/server.py  多案子网页服务；service/refresh.py 定时入口；service/diagnose_ui.py 回放页里的异常排查面板
config/spend_anomaly_seed.json  异常字典种子（真实案例）；线上字典在 knowledge/（不入库）
config/people.json 同一个人在各系统里的账号
src/template.html  两版共用的界面（HTML + CSS + JS）
src/feedos/        3D 场景、时间线引擎、动态人员/来源适配器
translate_ui.py    界面文字的英文替换表
sources.py、slack_source.py  读原始记录的公共小工具（GitHub 原始记录、Slack 日页解析、时间换算）
collect/mcp_client.py        只读的 MCP 客户端（后台、大脑）
raw/ out/ state/   原始记录（raw/links 按链接、raw/campaigns 按活动）/ 生成的页面（out/<活动编号>）/ 刷新状态（都不入库）
```

## 边界

- **全程只读**：GitHub 只发 GET；HubSpot 和 Slack 只读公司大脑（Brain）的只读副本，不打开 Slack；后台只用 Femini 的查询类工具。没有向任何系统写入内容。
- 事件只收录**明确写了该活动下链接编号或相关单号**的记录；没写编号的相关聊天不会出现。
- 英文页的原话：原文是英文才显示；原文是中文就不显示、不翻译，只注明「原文是中文」。
- 标签含义（Code Review、PR、Self Check、PA Production Verify、QA、enhancement needed）按常见走法推断，页面里已注明。
- 3D 场景和依赖全部本地内嵌到单文件页面，不加载外部字体或脚本；回放数据不会发送到其他系统。

## 已知缺口

- HubSpot 没有逐项状态变更历史，只有建单、更新时间和现状。
- Slack 副本每天凌晨同步一次，更晚的聊天看不到（页面里写明同步到什么时候）。
- 账号对不上同事的，按账号名显示并标「未核对」，可补进 `config/people.json`。
- 邮件没有接入。
