// Pi supplies the agent loop. This extension exposes read-only data APIs, the result/note tools, and protects files.
// The AI never reads or writes the result JSON directly: report_* tools (replay/pi_report.py) assemble it (quotes are not verified).
// Output size: we add no limits of our own. Our API tools show the saved file the same way Pi's read does
// (Pi's standard 50KB / 2000 lines per view, full response in results/); page sizes are whatever the upstream API allows.
import { Type } from "typebox";
import { truncateHead, formatSize, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES } from "@earendil-works/pi-coding-agent";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const work = fs.realpathSync(process.env.REPLAY_PI_WORK!);
const app = process.env.REPLAY_PI_APP!;
const python = process.env.REPLAY_PI_PYTHON || "python3";
const allowed: string[] = JSON.parse(fs.readFileSync(path.join(work, "mcp-read-tools.json"), "utf8"));
const apiNames = new Set(["feedai_api", "github_api", "fireflies_api"]);
const nativeNames = new Set(allowed.map(n => `mcp__femini__${n}`));
const reportNames = new Set(["report_find", "report_add", "report_update", "report_delete", "report_set", "report_finish", "note_add"]);
// 查到就写：连续查询这么多次都没往结果或笔记里写东西，就在查询结果后面提醒一句（只提醒，不拦）。用户 2026-11-03 定的规则。
const REMIND_AFTER = 40;  // 用户定：至少 40 次查询都没写才提醒；之后每再查 40 次提醒一次，不是每次都插
let queriesSinceWrite = 0;
// 本次运行的截止时间（程序每次启动 Pi 时传入，额度等待会顺延）；按整分钟报，格式固定
const deadline = Number(process.env.REPLAY_PI_DEADLINE || 0);
const timeLeft = () => deadline ? Math.max(0, Math.floor((deadline * 1000 - Date.now()) / 60000)) : null;
const reminder = () => queriesSinceWrite >= REMIND_AFTER && queriesSinceWrite % REMIND_AFTER === 0
  ? `\n\n[提醒：你已经连续查询 ${queriesSinceWrite} 次，没有往结果或笔记里写任何东西。上下文会被压缩，没写下来的发现一定会丢。先用 report_add 写下已经查实的节点和事项、用 note_add 记下线索，再继续查。]`
  : "";

function real(p: string): string {
  let current = path.resolve(work, p);
  const missing: string[] = [];
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) throw new Error("Invalid path");
    missing.unshift(path.basename(current)); current = parent;
  }
  return path.join(fs.realpathSync(current), ...missing);
}
function below(p: string, root: string) { return p === root || p.startsWith(root + path.sep); }
function snapshot(id: string, tool: string, data: unknown, ok = true): string {
  const name = crypto.createHash("sha256").update(id).digest("hex").slice(0, 24);
  const relative = `results/${name}.json`;
  fs.mkdirSync(path.join(work, "results"), {recursive: true});
  fs.writeFileSync(path.join(work, relative), JSON.stringify({ok, tool, at: new Date().toISOString(), data}, null, 2));
  return relative;
}

export default function(pi: any) {
  pi.on("tool_call", async (event: any) => {
    const name = event.toolName, input = event.input || {};
    if (apiNames.has(name) || nativeNames.has(name) || reportNames.has(name)) return;
    if (!["read", "ls", "write", "edit"].includes(name)) return {block:true, reason:"Only the configured read-only data tools are available."};
    try {
      const target = real(input.path || ".");
      if (!below(target, work)) return {block:true, reason:"Read files inside this run's workspace only. Use online tools for source data."};
      // 结果只能通过 report_* 工具读写，不直接碰文件
      if (below(target, path.join(work, "output"))) return {block:true, reason:"结果不能直接读写，用 report_find 查看、report_add / report_update / report_delete / report_set 修改。"};
      if (["write", "edit"].includes(name) && !below(target, path.join(work, "notes"))) {
        return {block:true, reason:"只有 notes/ 可以写（建议用 note_add）。查询结果和 seed 资料只读。"};
      }
    } catch {return {block:true, reason:"Invalid file path."};}
  });

  pi.on("tool_result", async (event: any) => {
    let content = event.content || [];
    if (nativeNames.has(event.toolName)) {
      let data = event.structuredContent;
      if (data === undefined) {
        const texts = content.filter((c:any)=>c.type === "text").map((c:any)=>c.text);
        const joined = texts.join("\n");
        try {data=JSON.parse(joined);} catch {data={content:joined};}
      }
      const file = snapshot(event.toolCallId, event.toolName, data, !event.isError);
      queriesSinceWrite++;
      content = [...content, {type:"text", text:`Saved as ${file}.${reminder()}`}];
    }
    // 每次工具结果末尾附剩余时间。只追加在新结果上，系统提示词和之前的内容不变，缓存照常命中。
    const left = timeLeft();
    if (left === null) return nativeNames.has(event.toolName) ? {content} : undefined;
    return {content: [...content, {type:"text", text:`[剩余时间约 ${left} 分钟]`}]};
  });

  // 结果与笔记工具：参数交给 replay.pi_report，程序组装 JSON（不核对原话）
  const reportTool = (name: string, op: string, description: string, parameters: any) => pi.registerTool({
    name, label: name, description, parameters,
    async execute(_id: string, args: any, signal: any) {
      const run = await pi.exec(python, ["-m", "replay.pi_report", JSON.stringify({...args, op, work})], {cwd: app, signal, timeout: 300000});
      let answer: any;
      try {answer = JSON.parse(run.stdout);} catch {return {content:[{type:"text", text:"结果工具没有正常返回：" + (run.stderr || "").slice(-500)}], details:{}, isError:true};}
      if (!answer.ok) return {content:[{type:"text", text:answer.error}], details:{}, isError:true};
      if (op !== "find") queriesSinceWrite = 0;
      return {content:[{type:"text", text:answer.text}], details:{}};
    }
  });
  const Data = Type.Record(Type.String(), Type.Any());
  reportTool("report_find", "find",
    "查看当前结果。不带参数：总览（状态、数量、每条一行）。id=M001/F001/I001：这一条的全部字段。筛选：type=moment/fact/item；text 关键词；pattern 正则（像 grep，不分大小写）；where 按字段（如 {\"speaker\":\"Liwei\"}）；since/until 日期。brief=true 同时给出现状总结。",
    Type.Object({id:Type.Optional(Type.String()), type:Type.Optional(Type.String()), text:Type.Optional(Type.String()),
                 pattern:Type.Optional(Type.String()), where:Type.Optional(Type.Record(Type.String(), Type.String())),
                 since:Type.Optional(Type.String()), until:Type.Optional(Type.String()), brief:Type.Optional(Type.Boolean())}));
  reportTool("report_add", "add",
    "查实就加。一条：type=moment（关键节点）/fact（后台事实）/item（进行中事项）+ data（字段见系统提示「结果字段」）。一批：items=[{type,data},...]，一次加多条，逐条返回结果。",
    Type.Object({type:Type.Optional(Type.String()), data:Type.Optional(Data), items:Type.Optional(Type.Array(Type.Object({type:Type.String(), data:Data})))}));
  reportTool("report_update", "update",
    "按编号修改：一条用 id + data（只写要改的字段）；一批用 items=[{id,data},...]。",
    Type.Object({id:Type.Optional(Type.String()), data:Type.Optional(Data), items:Type.Optional(Type.Array(Type.Object({id:Type.String(), data:Data})))}));
  reportTool("report_delete", "delete", "按编号删除一条，reason 写为什么删（归属错、重复、后来证实不对等）。",
    Type.Object({id:Type.String(), reason:Type.Optional(Type.String())}));
  reportTool("report_set", "set",
    "写结果的整体部分：field=title_zh/title_en/summary_zh/summary_en/state_brief/chapters/people/workstreams/backend_changes/handoffs/unresolved/coverage，value 为对应内容（会整体替换）。",
    Type.Object({field:Type.String(), value:Type.Any()}));
  reportTool("report_finish", "finish", "全部写完后调用，标记完成。一个节点都没有、或还没写现状总结时会说明还缺什么。",
    Type.Object({}));
  reportTool("note_add", "note",
    "随手记笔记：kind=progress（查到哪、下一步、哪些来源已查完）或 evidence（线索、待追的事、判断依据）。追加到 notes/，上下文压缩后还能读回来。",
    Type.Object({text:Type.String(), kind:Type.Optional(Type.String())}));

  const definitions = [
    {name:"feedai_api", description:"Direct read of FeedAI's Qdrant API. No local mirror and no campaign filtering. collections lists available data; info gives schema/indexes; scroll returns one page and next_page_offset; retrieve takes body.ids; query is Qdrant's read-only query endpoint. Pass native Qdrant JSON in body. Some collections forbid filtering on unindexed fields: inspect info first. Collections: slack_api_messages_v3, email_documents_v3, hubspot_tickets_v3, fireflies_call_transcripts_v3, fireflies_call_transcripts, gong_call_transcripts, github_issues. Paginate with body.offset; do not assume a first page is complete.",
      parameters:Type.Object({operation:Type.String(),collection:Type.Optional(Type.String()),body:Type.Optional(Type.Record(Type.String(),Type.Any()))})},
    {name:"github_api",description:"Direct GitHub REST GET for feed-mob repos. path examples: search/issues; repos/feed-mob/feedmob/issues/14718; .../issues/14718/comments; .../issues/14718/events; .../pulls/123. params are native query parameters (q, page, per_page; GitHub itself allows up to 100). No writes. Each call returns a single page, never silently merges or filters records.",
      parameters:Type.Object({path:Type.String(),params:Type.Optional(Type.Record(Type.String(),Type.Any()))})},
    {name:"fireflies_api",description:"Read Fireflies directly with the existing account. operation=list uses variables {fromDate,toDate,limit,skip} (Fireflies itself allows up to 50 per page); operation=detail uses id from a list result and returns full transcript sentences. No mutations. Account visibility is not company-wide; other meetings may be available through Femini or FeedAI.",
      parameters:Type.Object({operation:Type.String(),id:Type.Optional(Type.String()),variables:Type.Optional(Type.Record(Type.String(),Type.Any()))})},
  ];
  for (const def of definitions) pi.registerTool({...def, label:def.name,
    async execute(id:string,args:any,signal:any) {
      const run=await pi.exec(python,["-m","replay.pi_tools",JSON.stringify({tool:def.name,args})],{cwd:app,signal,timeout:300000});
      let answer:any;
      try {answer=JSON.parse(run.stdout);} catch {throw new Error("API adapter did not return JSON; no data was accepted.");}
      if (!answer.ok) return {content:[{type:"text",text:answer.error}],details:{},isError:true};
      const file=snapshot(id,def.name,answer.data);
      queriesSinceWrite++;
      // Show the saved file itself, cut exactly like Pi's read tool, so "offset" continues in the same file.
      const t=truncateHead(fs.readFileSync(path.join(work,file),"utf8"),{maxLines:DEFAULT_MAX_LINES,maxBytes:DEFAULT_MAX_BYTES});
      const more=t.truncated?`\n\n[Showing lines 1-${t.outputLines} of ${t.totalLines} (${formatSize(t.outputBytes)} of ${formatSize(t.totalBytes)}). Full response is in ${file}; read it with offset=${t.outputLines+1} to continue.]`:"";
      return {content:[{type:"text",text:`Saved as ${file}\n${t.content}${more}${reminder()}`}],details:{file,truncation:t.truncated?{shownLines:t.outputLines,totalLines:t.totalLines}:null}};
    }
  });
}
