"""AI 任务队列的状态页（要公司 Google 账号登录）：/queue 总览、/queue/<题号> 一道题的完整经过。时间按北京时间显示。"""
import datetime as dt
import html

from replay import jobqueue as Q

BJ = dt.timezone(dt.timedelta(hours=8))
EVENT_LABELS = {
    "created": "出题", "claimed": "领取", "input": "取题", "heartbeat": "续期", "reinstated": "超时后续上",
    "expired": "超时", "requeued": "重新排队", "rejected": "退回（不合格）", "accepted": "收下",
    "late_accepted": "收下（超时后交回）", "late_ignored": "迟到，未采用", "superseded": "停止（已有人交了）",
    "released": "放弃", "internal_only": "转自家处理方", "failed": "失败", "cancelled": "作废", "agent_paused": "处理方暂停",
}
END_LABELS = {
    None: "进行中", "submitted": "交回，已收下", "submitted_late": "超时后交回，已收下", "expired": "超时", "released": "放弃",
    "superseded": "已有人先交", "rejected": "交满次数仍不合格", "cancelled": "题目作废",
}

CSS = """:root{color-scheme:dark;--bg:#070812;--panel:#10111d;--line:rgba(255,255,255,.09);--ink:#f4f2fa;--dim:#aaa7bc;--muted:#77758d;--link:#c4b5fd;
--font:-apple-system,"PingFang SC","Hiragino Sans GB",system-ui,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.5 var(--font)}
main{width:min(1280px,calc(100% - 32px));margin:0 auto;padding:28px 0}a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:0 0 10px}.sub{color:var(--muted);margin:0 0 18px}
section{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:16px;margin-bottom:14px}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.filters a{border:1px solid var(--line);border-radius:8px;padding:4px 10px;color:var(--dim)}
.filters a.cur{color:var(--ink);border-color:var(--link)}.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);color:var(--dim);vertical-align:top}td.k{color:var(--muted);white-space:nowrap;width:140px}
td b{color:var(--ink);font-weight:600}.mono{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.empty{color:var(--muted);text-align:center;padding:16px}
small{color:var(--muted)}"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def bj(s, with_date=True):
    """UTC 的 ISO 时间 → 北京时间（月-日 时:分:秒）。"""
    if not s:
        return "—"
    try:
        t = Q._parse(s).astimezone(BJ)
    except (ValueError, TypeError):
        return esc(s)
    return t.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def minutes(seconds):
    if seconds is None:
        return "—"
    return f"{seconds / 60:.1f} 分钟"


def tokens(n):
    return f"{(n or 0) / 1000:.0f}k" if n else "—"


def _page(title, body):
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="30"><title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>')


def _agent_state(a, now):
    if not a["enabled"]:
        return "已停用"
    if a.get("holding"):
        h = a["holding"]
        return f'处理中 <a class="mono" href="/queue/{esc(h["job_id"])}">{esc(h["job_id"])}</a><small>（截止 {bj(h["expires_at"], False)}）</small>'
    if a["paused_until"] and a["paused_until"] > now:
        return f"暂停至 {bj(a['paused_until'], False)}"
    return "空闲"


def overview(snap, names, state=None):
    """/queue：处理方、各活动的任务、最近的任务。"""
    now = snap["now"]
    c = snap["counts"]
    total = sum(c.values())
    links = [(None, f"全部 {total}")] + [(s, f"{Q.STATE_LABELS[s]} {c[s]}") for s in Q.STATES]
    filters = "".join(f'<a href="/queue{"?state=" + s if s else ""}"{" class=cur" if s == state else ""}>{esc(label)}</a>' for s, label in links)

    rows = []
    for a in snap["agents"]:
        avg = minutes(a["busy_seconds"] / a["done_count"]) if a["done_count"] else "—"
        who = esc(a["name"]) + (" <small>（自家）</small>" if a["internal"] else "")
        rows.append(f"<tr><td><b>{who}</b><br><small>{esc(a['note'])}</small></td><td>{esc(a['model']) or '—'}</td><td>{tokens(a['context_tokens'])}</td>"
                    f"<td>{_agent_state(a, now)}</td><td>{a['done_count']}</td><td>{a['timeout_count']}</td><td>{a['rejected_count']}</td>"
                    f"<td>{a['late_count']}</td><td>{a['released_count']}</td><td>{avg}</td><td>{bj(a['last_seen_at'])}</td></tr>")
    agents = "".join(rows) or '<tr><td colspan="11" class="empty">还没有登记处理方</td></tr>'

    camp_rows = []
    for cid, cnt in sorted(snap["campaigns"].items(), key=lambda kv: -(kv[1]["queued"] + kv[1]["leased"])):
        name = names.get(cid) or ""
        camp_rows.append(f'<tr><td><a href="/queue?campaign={esc(cid)}">{esc(name or cid)}</a><br><small class="mono">#{esc(cid)}</small></td>'
                         + "".join(f"<td>{cnt[s]}</td>" for s in Q.STATES) + "</tr>")
    camps = "".join(camp_rows) or '<tr><td colspan="6" class="empty">最近两天没有出题</td></tr>'

    job_rows = []
    for j in snap["jobs"]:
        ev = j.get("last_event") or {}
        last = f"{bj(ev.get('at'))} {esc(EVENT_LABELS.get(ev.get('kind'), ev.get('kind') or ''))}" + (f"<br><small>{esc(ev.get('detail'))}</small>" if ev.get("detail") else "")
        handler = j["lease_agent"] or j["result_agent"] or "—"
        flag = "<br><small>只给自家处理方</small>" if j["internal_only"] and j["state"] in ("queued", "leased") else ""
        job_rows.append(
            f'<tr><td><a class="mono" href="/queue/{esc(j["id"])}">{esc(j["id"])}</a></td>'
            f'<td>{esc(names.get(j["campaign"] or "", "") or j["campaign"] or "—")}<br><small class="mono">#{esc(j["campaign"] or "—")}</small></td>'
            f'<td>{esc(Q.KIND_LABELS.get(j["kind"], j["kind"]))}</td><td>{tokens(j["input_tokens"])}</td>'
            f'<td><b>{esc(Q.STATE_LABELS.get(j["state"], j["state"]))}</b>{flag}</td><td>{esc(handler)}</td>'
            f'<td>{bj(j["created_at"])}</td><td>{bj(j["lease_expires_at"]) if j["state"] == "leased" else "—"}</td>'
            f'<td>{bj(j["data_as_of"])}</td><td>{j["attempts"]} / {j["failures"]}</td><td>{last}</td></tr>')
    jobs = "".join(job_rows) or '<tr><td colspan="11" class="empty">没有符合条件的任务</td></tr>'

    body = f"""<h1>AI 任务队列</h1>
<p class="sub">时间为北京时间 · 每 30 秒自动刷新 · <a href="/">返回首页</a> · <a href="/agent-api/v1/protocol">处理方接入说明</a></p>
<nav class="filters">{filters}</nav>
<section><h2>处理方</h2><div class="table-wrap"><table><thead><tr><th>名称</th><th>模型</th><th>一次能读</th><th>状态</th><th>完成</th><th>超时</th><th>被退回</th><th>迟到</th><th>放弃</th><th>平均用时</th><th>最近来过</th></tr></thead><tbody>{agents}</tbody></table></div></section>
<section><h2>各活动最近两天的任务</h2><div class="table-wrap"><table><thead><tr><th>活动</th>{''.join(f'<th>{Q.STATE_LABELS[s]}</th>' for s in Q.STATES)}</tr></thead><tbody>{camps}</tbody></table></div></section>
<section><h2>任务</h2><div class="table-wrap"><table><thead><tr><th>题号</th><th>活动</th><th>类型</th><th>大小</th><th>状态</th><th>处理方</th><th>出题</th><th>截止</th><th>原料截至</th><th>领取 / 失败</th><th>最近动态</th></tr></thead><tbody>{jobs}</tbody></table></div></section>"""
    return _page("AI 任务队列", body)


def detail(d, names):
    """/queue/<题号>：一道题的字段、每次领取、完整时间线。"""
    j = d["job"]
    name = names.get(j["campaign"] or "", "")
    facts = [
        ("状态", f"<b>{esc(Q.STATE_LABELS.get(j['state'], j['state']))}</b>" + ("（只给自家处理方）" if j["internal_only"] else "")),
        ("活动", f'{esc(name or "—")} <small class="mono">#{esc(j["campaign"] or "—")}</small>'),
        ("类型", esc(Q.KIND_LABELS.get(j["kind"], j["kind"])) + (f"（{esc(j['step'])}）" if j["step"] else "")),
        ("大小", f"{j['input_chars']} 字，约 {j['input_tokens']} token"),
        ("答案要求", f"一个 JSON 对象，含字段 {esc(j['expect_key'])}" if j["expect_key"] else "一个 JSON 对象"),
        ("出题时间", bj(j["created_at"])),
        ("原料取数时间", bj(j["data_fetched_at"])),
        ("原料截至", f"{bj(j['data_as_of'])} <small>（材料里最新一条记录）</small>"),
        ("领取 / 失败次数", f"{j['attempts']} / {j['failures']}"),
    ]
    if j["state"] == "done":
        facts += [("交回", f"{bj(j['done_at'])}，由 {esc(j['result_agent'])}" + (f"（{esc(j['result_model'])}）" if j["result_model"] else "")),
                  ("答案长度", f"{j['result_chars']} 字")]
    if j["error"]:
        facts.append(("原因", esc(j["error"])))
    fact_rows = "".join(f'<tr><td class="k">{k}</td><td>{v}</td></tr>' for k, v in facts)
    lease_rows = "".join(
        f'<tr><td class="mono">{esc(lease["id"])}</td><td>{esc(lease["agent"])}</td><td>{bj(lease["claimed_at"])}</td><td>{bj(lease["input_fetched_at"])}</td>'
        f'<td>{bj(lease["last_heartbeat_at"])}</td><td>{bj(lease["expires_at"])}</td><td>{bj(lease["ended_at"])}</td>'
        f'<td>{esc(END_LABELS.get(lease["end_reason"], lease["end_reason"]))}</td><td>{lease["submits"]}</td></tr>' for lease in d["leases"]) \
        or '<tr><td colspan="9" class="empty">还没有人领过</td></tr>'
    event_rows = "".join(
        f'<tr><td>{bj(e["at"])}</td><td><b>{esc(EVENT_LABELS.get(e["kind"], e["kind"]))}</b></td><td>{esc(e["agent"] or "—")}</td>'
        f'<td>{esc(e["detail"] or "")}</td></tr>' for e in d["events"])
    body = f"""<h1>任务 <span class="mono">{esc(j['id'])}</span></h1>
<p class="sub">时间为北京时间 · <a href="/queue">返回队列</a></p>
<section><table><tbody>{fact_rows}</tbody></table></section>
<section><h2>每次领取</h2><div class="table-wrap"><table><thead><tr><th>领取号</th><th>处理方</th><th>领取</th><th>取题</th><th>最近续期</th><th>截止</th><th>结束</th><th>结果</th><th>交了几次</th></tr></thead><tbody>{lease_rows}</tbody></table></div></section>
<section><h2>经过</h2><div class="table-wrap"><table><thead><tr><th>时间</th><th>动作</th><th>处理方</th><th>说明</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>"""
    return _page(f"任务 {j['id']}", body)
