# 外包任务 · 处理方（agent）接入与注册

这份说明给要接进来的 agent 和它的维护人看。最新版本随时在这里：
`https://replay-stage.feedmob.it.com/agent-api/v1/protocol`（不需要钥匙就能打开）

## 这是什么
- 回放网站生成「业务版」「进行中事项」时，要调用很多次大模型。每一次调用就是一道**题**，放进队列。
- 你的 agent 每分钟来看一次：有题就领一道，做完交回。我们自己的处理方（用 MiniMax Flash）也是这样领题、交答案，规则一模一样，没有特权。
- 一道题 = 一段工作说明（system）+ 一段材料（user），要求交回**一个 JSON 对象**。题目自带全部材料，你不需要我们的代码、数据库或任何系统权限。
- 交回的答案会被核对：格式和字段当场查；摘录的原话会在原始记录里逐字核对，对不上的不会被采用。

## 一、注册：拿到钥匙
把下面这些发给管理员（Leo），管理员登记后给你一把钥匙。

| 要提供的 | 说明 | 例子 |
|---|---|---|
| 名字 | 英文字母、数字和 `_` `.` `-`，1~40 个字符，以字母或数字开头 | `hermes-a` |
| 模型 | 作答用的模型 | `claude-sonnet-4.5` |
| 一次能读多少 token | 模型的上下文窗口，派题按它判断，读不下的题不会派给你 | `200000`、`1000000` |
| 负责人 | 出了问题找谁 | 名字 |

- 钥匙以 `rq_` 开头，**只显示一次**，请自己保存好。丢了或泄露了，找管理员换一把，旧的马上失效。
- 上下文按模型的真实上限填就行。我们会自动打 85 折（留给你自己的说明和工具），再留 4 万 token 给思考和答案。
- 题目大小按「中日韩文字一个字算 1 个 token，其余每 3 个字符算 1 个」估算，宁可估多。所以派给你的题都在你读得下的范围里。
- 换了模型、上下文变了，告诉管理员改登记，不用换钥匙。

## 二、规矩
1. **一个处理方同时只能拿一道题。** 没交之前再来领，拿到的还是同一道（回应里 `again: true`）。
2. **截止时间 40 分钟。** 做得久就续期，每次续到「现在 + 40 分钟」；一次领取最长 60 分钟，到了就不能再续。
3. **过了截止时间没交，题目会重新排队给别人。** 你之后再交：
   - 题目还没人完成：照样收下（先到先得），会记一次超时；
   - 已经有人交了合格答案：回 409，你这份不用了。
4. **答案不合格会当场退回原因（422）。** 在题目还没完成之前改好再交，同一次领取最多交 7 次。
5. **连续超时 3 次会暂停领题 30 分钟。** 交上一个合格答案就清零。
6. **做不了就放弃（release），** 题目马上交给别人。
   - 因为限流、重启这类不是题目本身的问题放弃，请带上 `"retryable": true`，不算这道题失败。
   - 同一道题失败满 3 次，以后只交给我们自己的处理方。
7. 题目有先后：有人手动刷新、每天定时刷新出的题最先派；批量出的题按活动先后派，先开始的活动先做完。你不用管，领到哪道做哪道。
8. 所有时间都是 UTC，ISO 8601 格式，末尾带 Z。

## 三、每分钟的定时任务怎么写
```
1. POST /claim
   - job 为空：这次结束（reason 是 empty / nothing_fits / paused）。
   - 领到了：记下 job.id、job.lease_id、job.expires_at。
     again: true 表示这道题之前就是你的。如果上一轮还在做（比如另一个进程在跑），这一轮不要重复做。
2. GET /jobs/<job.id>/input?lease=<job.lease_id>   拿到 messages
3. 作答：system 是工作说明，user 是材料，按 system 的要求写出一个 JSON 对象。
   预计 40 分钟内做不完，就每隔几分钟 POST /jobs/<job.id>/heartbeat 续期。
4. POST /jobs/<job.id>/result
   - 200：收下了，结束。
   - 422：看 error、near 改好，用同一个 lease_id 再交。
   - 409：题目已完成或作废，丢掉。
```
- **建议：** 用一个本地锁文件防止两轮定时任务同时做同一道题；做好的答案在交上去之前别扔，接口一时连不上就隔一会儿再交（超时后交的合格答案照样算数）。
- **给 LLM agent 的定时任务提示词（可以照抄改）：**
  > 每分钟执行一次：用 curl 带请求头 `Authorization: Bearer <钥匙>` 调 `POST https://replay-stage.feedmob.it.com/agent-api/v1/claim`。如果返回的 job 是 null，就结束。否则下载 `GET .../jobs/<id>/input?lease=<lease_id>`，把 messages 里 system 的内容当作你这次的工作说明、user 的内容当作材料，严格按说明只写出一个 JSON 对象（摘录的原话必须一字不差地从材料里复制）。然后 `POST .../jobs/<id>/result`，请求体是 `{"lease_id": "<lease_id>", "output": <你的 JSON>, "model": "<你的模型名>"}`。如果回 422，按 error 改好再交；回 409 就结束。

## 四、接口
前缀：`https://replay-stage.feedmob.it.com/agent-api/v1`
每个请求都带 `Authorization: Bearer <钥匙>`。请求体和回应都是 JSON；回应里都有 `ok`、`message`（给人看的说明），出错时还有 `reason`。

| 接口 | 请求体 | 回应要点 |
|---|---|---|
| `POST /claim` | `{}` | `job`：`id`、`lease_id`、`expires_at`、`kind_label`（哪一步的题）、`input_tokens`（大小估算）、`data_fetched_at`、`data_as_of`、`output`；没有题时 `job: null` 和 `reason` |
| `GET /jobs/<id>/input?lease=<lease_id>` | — | `messages`（题目本身）、`output.top_key` / `output.top_type_label`（答案要求）、`data_fetched_at`（这份材料是我们什么时候取的）、`data_as_of`（材料里最新一条记录的时间）、`input_fetched_at`（你第一次取题的时间）、`expires_at` |
| `POST /jobs/<id>/heartbeat` | `{"lease_id": "..."}` | `continue`（false = 题目已完成或作废，停下）、`expires_at`（新的截止时间）；`reassigned: true` = 你超时了、题目已给别人，做完的合格答案照样可以交（先到先得） |
| `POST /jobs/<id>/result` | `{"lease_id": "...", "output": <JSON 对象或它的文本>, "model": "模型名", "usage": {...}}` | 200 `accepted`；422 `error`、`near`、`submits_left`；409 `reason`（already_done / cancelled） |
| `POST /jobs/<id>/release` | `{"lease_id": "...", "reason": "原因", "retryable": true}` | 题目重新排队；`retryable` 不写就算这道题失败一次 |
| `GET /me` | — | 你的登记信息、正在做的题、完成 / 超时 / 被退回 / 迟到 / 放弃次数 |
| `GET /protocol` | — | 这份说明（不需要钥匙） |

## 五、作答要求
- 按 `system` 里的要求作答，题目的全部规则都写在那里。
- 只输出一个**标准的 JSON 对象**，不要 NaN、Infinity 这类值。外面包一层 \`\`\`json 代码块也可以，但不要夹别的文字。
- 必须有 `output.top_key` 这个字段，样子要符合 `output.top_type_label`，比如「一个列表，每一项是一个对象」。
- 说明里要求摘录的原话，必须一字不差地从材料里复制，不改写、不翻译、不拼接。对不上的原话会被丢掉，白做。

## 六、常见回应
| 情况 | 回应 | 怎么办 |
|---|---|---|
| 没有题 | 200，`job: null`，`reason: empty` | 下分钟再来 |
| 有题，但都不适合你 | 200，`job: null`，`reason: nothing_fits` | 下分钟再来（题太大，或只给别的处理方） |
| 被暂停 | 200，`job: null`，`reason: paused`，`paused_until` | 等到那个时间 |
| 钥匙不对 | 401 | 检查请求头 `Authorization: Bearer rq_...` |
| 处理方已停用 | 403 | 联系管理员 |
| 题号、领取号格式不对 | 400 | 用 claim 返回的 `id` 和 `lease_id`，原样带上 |
| 题号、领取号对不上 | 404 | 用 claim 返回的 `id` 和 `lease_id` |
| 题目已完成或作废 | 409 | 丢掉，不用再交 |
| 答案不合格 | 422 | 看 `error`、`near` 改好再交 |
| 这次领取交满 7 次 | 429 | 丢掉 |
| 题目原文已清理（题目结束 3 天后） | 410 | 丢掉 |
| 请求体超过 8 MB | 413 | — |
| 服务器忙或在重启（5xx、连不上） | — | 隔一会儿再试，做好的答案别扔 |

## 七、例子（curl）
```bash
KEY=rq_你的钥匙
BASE=https://replay-stage.feedmob.it.com/agent-api/v1
# 领题
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{}' $BASE/claim
# 取题（id、lease_id 换成上一步拿到的）
curl -s -H "Authorization: Bearer $KEY" "$BASE/jobs/<id>/input?lease=<lease_id>" > job.json
# 续期
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"lease_id": "<lease_id>"}' $BASE/jobs/<id>/heartbeat
# 交答案（output 可以直接放 JSON 对象）
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"lease_id": "<lease_id>", "output": {"picks": []}, "model": "my-model"}' $BASE/jobs/<id>/result
# 做不了，退回（限流这类不算题目失败）
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"lease_id": "<lease_id>", "reason": "rate limited", "retryable": true}' $BASE/jobs/<id>/release
```

## 八、管理员（登记、停用、看进度）
在 stage 服务器 `~/ai-2734-replay-stage/app` 目录下：
```bash
python3 service/agents.py add hermes-a --context 200000 --model claude-sonnet-4.5 --note "负责人：xxx"   # 打印钥匙，只显示这一次
python3 service/agents.py list                       # 全部处理方和状态
python3 service/agents.py set hermes-a --context 1000000 --model xxx   # 换了模型、上下文
python3 service/agents.py disable hermes-a           # 停用（钥匙也不能用了）；enable 恢复
python3 service/agents.py token hermes-a             # 换一把新钥匙，旧的立即失效
python3 service/agents.py unpause hermes-a           # 解除连续超时暂停
```
进度页（公司 Google 账号登录）：`https://replay-stage.feedmob.it.com/queue`
- 能看到每个处理方此刻在做哪道题、完成多少、超时多少。
- 点题号能看这道题的完整经过：谁几点领的、几点取的题、有没有续期、交了几次、收没收、为什么。
