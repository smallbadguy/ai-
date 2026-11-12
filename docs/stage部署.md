# stage 环境部署（replay-stage）

和正式站（`replay.feedmob.it.com`，端口 8791）完全独立：另一个目录、另一个端口、另一份凭证，互不影响。

## 前置
1. DNS：`replay-stage` A 记录 → `52.15.214.192`，灰云（用户已确认自己加）。
2. Google Cloud 控制台：OAuth 客户端（Web 应用）里加授权重定向 URI
   `https://replay-stage.feedmob.it.com/auth/google/callback`，把客户端 JSON 下载为
   `~/ai-2734-replay-stage/secrets/google-oauth-client.json`（权限 600）。
   没放这个文件时服务「关门」：除 `/healthz` 外一律 503。

## 步骤（服务器 feedmob1，用户 ubuntu）
```
mkdir -p ~/ai-2734-replay-stage/{app,secrets}
rsync -a --exclude .git --exclude secrets ./ feedmob1:~/ai-2734-replay-stage/app/
cp ~/ai-2734-replay/secrets/{femini.env,github.env,mcp-credentials.json} ~/ai-2734-replay-stage/secrets/
sudo cp deploy/ai-2734-replay-stage*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-2734-replay-stage.service ai-2734-replay-stage-refresh.timer
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-before-stage-$(date +%Y%m%d-%H%M%S)
# 把 deploy/Caddyfile.stage.snippet 追加进 Caddyfile 后：
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

**2026-11-02 起（用户定）：stage 再次关闭每日自动更新**，`ai-2734-replay-stage-refresh.timer` 已 disable。
11-01 下午打开时补跑的那一轮让它跑完（后半段因为改了停用活动的处理，用临时单元 `ai-2734-replay-stage-refresh-resume` 接着跑），
之后不再自动更新，已生成的活动由业务同事在首页按需刷新。原因：一次只更新一个活动，平均 18 分钟一个，104 个跑一轮要 30 小时左右，一天跑不完。
AI 处理仍全部走队列，沟通记录直接读上游；后台批量（`service/queue_batch.py`）保持停用。
开启：`sudo systemctl enable --now ai-2734-replay-stage-refresh.timer`（`Persistent=true`，错过执行时间后启用会马上补跑一轮）；
关闭：`sudo systemctl disable --now ai-2734-replay-stage-refresh.timer`（不会中断已经开始的这一轮）。

**2026-10-31 起（用户定）：沟通记录不再经过公司大脑**，直接读 FeedAI / Fireflies / Femini，见 `docs/直连数据源.md`。新装 stage 时：
把 `secrets/feeds.env` 拷过来（或从 feedmob3 的 `/opt/ken-team-brain/env/gbrain.env` 取 `QDRANT_URL`、`QDRANT_API_KEY`、`FIREFLIES_API_KEY`，权限 600），
先在 app 目录下跑一次 `REPLAY_SECRETS=... python3 -m replay.feeds refresh --full`（约 45 分钟），再在 `feeds.env` 里加 `REPLAY_SOURCE=direct`、
确认首页没有活动在生成后重启网页服务。回退：删掉这一行再重启（旧的大脑缓存都还在）。

## 验收
- `curl -s 127.0.0.1:8792/healthz` 有响应
- 未登录访问 `https://replay-stage.feedmob.it.com/` 跳到 Google
- 非 feedmob.com 邮箱被拒；登录后回到原页面
- 首页每个活动有「业务版」「详细版」两个入口；正式站与 V11 链接不受影响
