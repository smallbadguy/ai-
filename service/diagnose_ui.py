"""「异常排查」面板。2026-11-03 起不再插进回放页面（回放页面界面不改），改成单独的页面 /c/<活动>/diagnose（page()），入口在首页每一行。

只在打开某个活动的回放时可用，排查的也是这个活动。面板通过 /c/<活动>/diagnose/... 接口取数（见 service/server.py）。
"""
import json

CSS = r"""
#dg-root{position:fixed;inset:0;z-index:2147483600;display:none;font:13px/1.55 -apple-system,"PingFang SC","Hiragino Sans GB",system-ui,"Segoe UI",sans-serif;color:#F4F2FA;-webkit-font-smoothing:antialiased}
#dg-root.open{display:block}
#dg-root *{box-sizing:border-box}
#dg-back{position:absolute;inset:0;background:rgba(4,4,10,.62);backdrop-filter:blur(2px)}
#dg-panel{position:absolute;top:0;right:0;bottom:0;width:min(1020px,100vw);background:#0C0D17;border-left:1px solid rgba(255,255,255,.09);display:flex;flex-direction:column;box-shadow:-24px 0 80px rgba(0,0,0,.45)}
#dg-head{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,.08);background:linear-gradient(180deg,#14122A,#0C0D17)}
#dg-head h2{margin:0;font-size:16px;letter-spacing:-.2px}#dg-head small{color:#8E8AA6;display:block;font-size:11.5px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
#dg-head .sp{flex:1}
.dg-tabs{display:flex;gap:3px;background:#151628;border-radius:10px;padding:3px}
.dg-tabs button,.dg-x{font-family:inherit;font-weight:600;font-size:12px;white-space:nowrap;color:#ABA5C0;background:none;border:0;border-radius:8px;padding:6px 11px;cursor:pointer}
.dg-tabs button.cur{background:#6A4AF2;color:#fff}
.dg-x{font-size:18px;line-height:1;padding:4px 9px}.dg-x:hover{background:rgba(255,255,255,.08);color:#fff}
#dg-body{flex:1;overflow:auto;padding:16px 18px 40px}
.dg-card{border:1px solid rgba(255,255,255,.08);background:#11121F;border-radius:14px;padding:14px 15px;margin-bottom:12px}
.dg-card h3{margin:0 0 10px;font-size:13.5px}
.dg-form{display:grid;grid-template-columns:150px minmax(0,1fr) 92px auto;gap:9px;align-items:end}
.dg-form label{display:block;font-size:11px;color:#8E8AA6;margin-bottom:4px}
.dg-form input,.dg-form select{width:100%;font:inherit;color:#F4F2FA;background:#0A0B14;border:1px solid rgba(255,255,255,.12);border-radius:9px;padding:9px 10px}
.dg-form input:focus,.dg-form select:focus{outline:2px solid #A795FF;outline-offset:1px}
.dg-btn{font-family:inherit;font-weight:700;font-size:12.5px;color:#fff;background:linear-gradient(135deg,#765CF0,#5540D6);border:0;border-radius:9px;padding:10px 14px;cursor:pointer;white-space:nowrap}
.dg-btn:disabled{opacity:.45;cursor:not-allowed}.dg-btn.ghost{background:#1B1C2E;border:1px solid rgba(255,255,255,.1);font-weight:600;padding:6px 10px}
.dg-req{color:#E7BA52}
.dg-hint{color:#8E8AA6;font-size:11.5px;margin:8px 0 0}
.dg-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.dg-chip{font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;padding:4px 8px;border-radius:99px;background:#1B1C2E;border:1px solid rgba(255,255,255,.08);color:#C9C4DD;cursor:pointer}
.dg-chip.high{border-color:rgba(255,107,107,.5);color:#FF9B9B}.dg-chip.medium{border-color:rgba(231,186,82,.45);color:#F0CE7E}
.dg-runs{display:flex;flex-direction:column;gap:6px}
.dg-run{display:grid;grid-template-columns:92px minmax(0,1fr) auto;gap:10px;align-items:center;padding:8px 10px;border-radius:10px;background:#0E0F1B;border:1px solid rgba(255,255,255,.06);cursor:pointer}
.dg-run:hover,.dg-run.cur{border-color:#6A4AF2}
.dg-run b{font:600 12px ui-monospace,monospace}.dg-run span{color:#ABA5C0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dg-tag{display:inline-block;font-family:inherit;font-weight:600;font-size:10.5px;padding:2px 7px;border-radius:99px;background:#1F2033;color:#C9C4DD;white-space:nowrap}
.dg-tag.ok{background:rgba(76,201,140,.15);color:#7FE0B0}.dg-tag.warn{background:rgba(231,186,82,.15);color:#F0CE7E}.dg-tag.bad{background:rgba(255,107,107,.15);color:#FF9B9B}.dg-tag.info{background:rgba(105,207,224,.14);color:#9FE3EE}.dg-tag.v{background:rgba(139,115,255,.18);color:#C4B5FD}
.dg-stages{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 2px}
.dg-stage{font-size:11px;padding:3px 9px;border-radius:99px;background:#16172A;color:#6F6B88}.dg-stage.done{color:#7FE0B0}.dg-stage.now{background:#6A4AF2;color:#fff}
.dg-banner{border-radius:12px;padding:11px 13px;margin-bottom:12px;border:1px solid}
.dg-banner.confirmed{background:rgba(76,201,140,.08);border-color:rgba(76,201,140,.3)}.dg-banner.not_confirmed{background:rgba(255,107,107,.07);border-color:rgba(255,107,107,.28)}.dg-banner.insufficient{background:rgba(231,186,82,.07);border-color:rgba(231,186,82,.28)}
.dg-note{color:#F0CE7E;font-size:12px;margin:0 0 6px}
.dg-mv{padding:9px 14px;display:flex;flex-wrap:wrap;gap:7px;align-items:center;border-bottom:1px solid rgba(255,255,255,.06);background:#0F1020;font-size:11.5px;color:#8E8AA6}
.dg-mv input{flex:1 1 180px;min-width:0;font:inherit;color:#F4F2FA;background:#0A0B14;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:5px 9px}
.dg-mv button.on{background:#6A4AF2;border-color:#6A4AF2;color:#fff}.dg-mv .by{flex:1 0 100%;color:#ABA5C0}.dg-mv .err{flex:1 0 100%;color:#FF9B9B}
.dg-f{border:1px solid rgba(255,255,255,.09);border-radius:14px;margin-bottom:12px;background:#10111D;overflow:hidden}
.dg-fh{padding:12px 14px;display:flex;flex-wrap:wrap;gap:7px;align-items:center;border-bottom:1px solid rgba(255,255,255,.06);background:#131427}
.dg-fh strong{font-size:14px;flex:1 1 260px}
.dg-fb{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0}
.dg-col{padding:12px 14px;border-right:1px solid rgba(255,255,255,.06);min-width:0}.dg-col:last-child{border-right:0}
.dg-col h4{margin:0 0 8px;font-size:11.5px;color:#A795FF;text-transform:none;letter-spacing:.2px}
.dg-kv{margin:0 0 6px}.dg-kv em{font-style:normal;color:#8E8AA6;font-size:11px;display:block}
.dg-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;word-break:break-word}
.dg-q{border-left:2px solid #6A4AF2;padding:3px 0 3px 9px;margin:6px 0;color:#DAD6EA;word-break:break-word}
.dg-q small{display:block;color:#8E8AA6;font-size:10.5px}
.dg-ft{padding:9px 14px;border-top:1px solid rgba(255,255,255,.06);display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:#8E8AA6;font-size:11.5px}
.dg-ft .sp{flex:1}
.dg-list{margin:0;padding:0;list-style:none}.dg-list li{padding:5px 0;border-bottom:1px dashed rgba(255,255,255,.06)}.dg-list li:last-child{border-bottom:0}
details.dg-d{border:1px solid rgba(255,255,255,.08);border-radius:12px;margin-bottom:10px;background:#0F101C}
details.dg-d summary{cursor:pointer;padding:10px 13px;font-weight:600;font-size:12.5px;color:#DAD6EA}
details.dg-d .in{padding:2px 13px 12px}
.dg-ev{display:grid;grid-template-columns:52px 130px minmax(0,1fr);gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:12px}
.dg-ev .src{font:600 10.5px ui-monospace,monospace;color:#A795FF}.dg-ev .t{color:#8E8AA6;font-size:11px}
.dg-ev .x{color:#C9C4DD;word-break:break-word;white-space:pre-wrap;max-height:9.5em;overflow:auto}
table.dg-t{width:100%;border-collapse:collapse;font-size:12px}table.dg-t th{text-align:left;color:#8E8AA6;font-weight:600;font-size:11px;padding:6px 7px;border-bottom:1px solid rgba(255,255,255,.08)}
table.dg-t td{padding:7px;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top;color:#C9C4DD}
.dg-stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
.dg-stat div{background:#11121F;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:10px 12px}.dg-stat b{display:block;font-size:20px}.dg-stat span{color:#8E8AA6;font-size:11px}
.dg-empty{color:#8E8AA6;text-align:center;padding:18px}
.dg-err{color:#FF9B9B}
a.dg-a{color:#C4B5FD;text-decoration:none}a.dg-a:hover{text-decoration:underline}
.dg-spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.2);border-top-color:#A795FF;border-radius:50%;animation:dgs 1s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes dgs{to{transform:rotate(360deg)}}
@media(max-width:760px){#dg-head{flex-wrap:wrap;gap:8px;padding:12px 14px}#dg-head>div:first-child{flex:1 1 calc(100% - 52px);min-width:0}#dg-head .sp{display:none}.dg-x{order:2}.dg-tabs{order:3}#dg-body{padding:12px 12px 32px}.dg-form{grid-template-columns:1fr 1fr}.dg-form .wide{grid-column:1/-1}.dg-fb{grid-template-columns:1fr}.dg-col{border-right:0;border-bottom:1px solid rgba(255,255,255,.06)}.dg-ev{grid-template-columns:48px minmax(0,1fr)}.dg-ev .t{grid-column:2}.dg-ev .x{grid-column:1/-1}#dg-head small{max-width:56vw}}
"""

JS = r"""
(function(){
const CID=__CID__, LANG=__LANG__, NAME=__NAME__, Z=LANG==='zh';
const T=/*ZH*/{
 title:'异常排查', sub:'Spend 录入异常 · 只排查当前活动', run:'排查', dict:'字典', date:'异常日期', what:'发生了什么 / Click URL / Line item',
 ph:'例如：23660 spend 未录入，或 line item 匹配失败', range:'前后范围', days:' 天', start:'开始排查', need:'请先选择异常日期（必选）',
 chips:'最近 60 天程序发现异常信号的日期（点一下填入）', scanning:'正在扫描最近 60 天的数据…', noscan:'最近 60 天没有发现异常信号', history:'本活动的排查记录', nohist:'还没有排查记录',
 stage:{queued:'排队中',collect:'收集证据',cause:'分析原因',status:'修复状态 / GitHub 验证',learn:'更新字典',done:'完成',error:'出错'},
 sym:{confirmed:'现象已证实',not_confirmed:'未发现异常（现象未证实）',insufficient:'数据不足'},
 conf:{high:'把握高',medium:'把握中',low:'把握低'},
 ver:{consistent:'验证：一致',partial:'验证：部分一致',contradict:'验证：矛盾',none:'暂无验证依据'}, basis:{config:'之后的后台改动',later:'之后的沟通记录',github:'GitHub',manual:'人工验证'}, pend:'待验证 · 下次自动回访 ', unver:'到期未找到验证依据',  followup:'自动回访', fudone:'已回访 ', fuleft:'待回访 ', funone:'不需要回访（历史日期，之后的记录已经齐了）', funof:'没有结论，不需要回访',
 bounds:'可选日期：', outrange:'这一天没有数据，可选范围：',
 mv:'人工验证', mvv:{consistent:'一致（确认）',partial:'部分一致',contradict:'矛盾（否决）'}, mvph:'备注 / 依据（可选）', mvby:'人工验证：', mvhint:'你验证后 MiniMax 不再验证这条', mvfail:'提交失败：',
 fix:{fixed:'已修复',handled_pending_data:'已处理，待数据确认',in_progress:'处理中',no_action:'未见处理'},
 data:{abnormal:'数据仍异常',normal:'数据已正常',unknown:'数据无法判断'},
 role:{changed_config:'提交配置改动',approved_config:'批准配置改动',entered_spend:'录入 / 修改 spend',reported:'报告问题',fixed:'修复',asked:'提出请求'},
 cat:{config:'配置错误',upstream:'上游 / 外部',system:'系统缺陷',manual:'人工操作'},
 c1:'错误配置', c2:'操作人证据', c3:'相关工单及修复状态', setting:'设置项', actual:'实际', expected:'应为', noops:'没有可核对的操作人记录', notk:'没有相关 HubSpot 工单',
 gh:'验证依据', fixev:'修复动作', datast:'数据现状', signals:'程序信号', evidence:'证据清单（原因阶段，不含 GitHub）', ghlist:'验证阶段读取的记录（之后的后台改动 C / 沟通 L / GitHub G）', later:'',
 learn:'这次排查对字典的影响', nolearn:'字典没有变化', confirm:'确认', reject:'否决', newe:'新模式', summary:'结论', notfound:'没有找到能解释异常的原因（宁可不挂错）', dropped:'被程序丢掉的结论',
 cites:'证据', sig:'信号', kind:{spend_when_paused:'暂停后仍录入',spend_missing:'有投放无 spend',zero_with_delivery:'录入为 0',margin_mismatch:'利润率不一致',gross_rate_mismatch:'gross 与单价不符',net_rate_mismatch:'net 与单价不符',net_cost_ratio_shift:'net/成本比例异常',spend_jump:'spend 突变'},
 elapsed:'已用时', sec:' 秒', open:'打开', running:'正在排查，通常 2–6 分钟，可以关掉面板稍后回来看。', failed:'排查失败：', retry:'重新排查',
 dsum:'字典概况', entries:'条目', reg:'回归集', regnone:'回归集还没有案例（被自动验证为一致的排查会自动加入）', hit:'命中率', comp:'完整率', clog:'最近改动', st:{seed:'种子',candidate:'候选',confirmed:'正式',deprecated:'停用',merged:'已合并'}, cur:{merge:'自动整理：合并到 ',drop:'自动整理：删除 — ',keep:'自动整理：保留 — '},
 col:{id:'条目',cat:'类别',st:'状态',src:'来源',stat:'确认 / 部分 / 否决 / 未验证',ph:'说法'}, srcs:'证据来源', links:'链接', window:'窗口'
}/*/ZH*//*EN*/{
 title:'Anomaly diagnosis', sub:'Spend entry anomalies · this campaign only', run:'Diagnose', dict:'Dictionary', date:'Anomaly date', what:'What happened / Click URL / Line item',
 ph:'e.g. 23660 spend not entered, or line item mapping failed', range:'Window', days:' days', start:'Start', need:'Pick the anomaly date first (required)',
 chips:'Dates with data signals in the last 60 days (click to use)', scanning:'Scanning the last 60 days…', noscan:'No data signals in the last 60 days', history:'Diagnoses for this campaign', nohist:'No diagnoses yet',
 stage:{queued:'Queued',collect:'Collecting evidence',cause:'Analysing causes',status:'Fixes / GitHub check',learn:'Updating dictionary',done:'Done',error:'Failed'},
 sym:{confirmed:'Symptom confirmed',not_confirmed:'No anomaly found (symptom not confirmed)',insufficient:'Insufficient data'},
 conf:{high:'High confidence',medium:'Medium confidence',low:'Low confidence'},
 ver:{consistent:'Verified: consistent',partial:'Verified: partial',contradict:'Verified: contradicts',none:'No verification yet'}, basis:{config:'later config change',later:'later messages',github:'GitHub',manual:'Manual'}, pend:'Pending · next automatic check ', unver:'No verification found in time',  followup:'Automatic follow-up', fudone:'checked ', fuleft:'next ', funone:'No follow-up needed (past date, later records already available)', funof:'No conclusions, so nothing to follow up',
 bounds:'Selectable dates: ', outrange:'No data on that date. Selectable range: ',
 mv:'Manual check', mvv:{consistent:'Consistent (confirm)',partial:'Partial',contradict:'Contradicts (reject)'}, mvph:'Note / evidence (optional)', mvby:'Verified by ', mvhint:'Once you verify, MiniMax will not check this one again', mvfail:'Failed: ',
 fix:{fixed:'Fixed',handled_pending_data:'Handled, data pending',in_progress:'In progress',no_action:'No action seen'},
 data:{abnormal:'Data still abnormal',normal:'Data now normal',unknown:'Data unclear'},
 role:{changed_config:'Submitted config change',approved_config:'Approved config change',entered_spend:'Entered / edited spend',reported:'Reported it',fixed:'Fixed it',asked:'Requested'},
 cat:{config:'Misconfiguration',upstream:'Upstream / external',system:'System defect',manual:'Manual operation'},
 c1:'Misconfiguration', c2:'Operator evidence', c3:'Tickets & fix status', setting:'Setting', actual:'Actual', expected:'Expected', noops:'No verifiable operator record', notk:'No related HubSpot ticket',
 gh:'Verification evidence', fixev:'Fix actions', datast:'Data now', signals:'Data signals', evidence:'Evidence (cause stage, no GitHub)', ghlist:'Records read for verification (later config C / messages L / GitHub G)', later:'',
 learn:'Effect on the dictionary', nolearn:'No dictionary change', confirm:'Confirm', reject:'Reject', newe:'New pattern', summary:'Conclusion', notfound:'No cause found that explains the anomaly', dropped:'Findings dropped by the checks',
 cites:'Evidence', sig:'Signals', kind:{spend_when_paused:'Spend while paused',spend_missing:'Delivery without spend',zero_with_delivery:'Zero entered',margin_mismatch:'Margin mismatch',gross_rate_mismatch:'Gross ≠ rate',net_rate_mismatch:'Net ≠ rate',net_cost_ratio_shift:'Net/cost ratio off',spend_jump:'Spend jump'},
 elapsed:'Elapsed', sec:' s', open:'Open', running:'Running, usually 2–6 minutes. You can close the panel and come back.', failed:'Diagnosis failed: ', retry:'Run again',
 dsum:'Dictionary overview', entries:'Entries', reg:'Regression set', regnone:'No regression cases yet (diagnoses automatically verified as consistent are added)', hit:'Hit rate', comp:'Complete rate', clog:'Recent changes', st:{seed:'Seed',candidate:'Candidate',confirmed:'Confirmed',deprecated:'Deprecated',merged:'Merged'}, cur:{merge:'Auto-curated: merged into ',drop:'Auto-curated: dropped — ',keep:'Auto-curated: kept — '},
 col:{id:'Entry',cat:'Category',st:'Status',src:'Source',stat:'Confirmed / partial / rejected / unverified',ph:'Phrases'}, srcs:'Evidence sources', links:'Links', window:'Window'
}/*/EN*/;
const API='/c/'+CID+'/diagnose';
const root=document.createElement('div'); root.id='dg-root';
root.innerHTML='<div id="dg-back"></div><div id="dg-panel" role="dialog" aria-modal="true" aria-labelledby="dg-h"><div id="dg-head"><div><h2 id="dg-h"></h2><small></small></div><span class="sp"></span><div class="dg-tabs"><button data-t="run"></button><button data-t="dict"></button></div><button class="dg-x" aria-label="close">×</button></div><div id="dg-body"></div></div>';
document.body.appendChild(root);
const body=root.querySelector('#dg-body');
root.querySelector('#dg-h').textContent=T.title; root.querySelector('#dg-head small').textContent=T.sub+' · '+NAME;
const tabs=root.querySelectorAll('.dg-tabs button'); tabs[0].textContent=T.run; tabs[1].textContent=T.dict;
let tab='run', cur=null, timer=null, scanData=null, runs=[], dictData=null;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function L(o,k){return o&&(Z?(o[k+'_zh']||o[k+'_en']):(o[k+'_en']||o[k+'_zh']))||''}
function ts(s){return s?String(s).replace('T',' ').replace(/:\d\dZ$/,'').replace('Z',''):''}
function tag(cls,txt){return '<span class="dg-tag '+cls+'">'+esc(txt)+'</span>'}
function open(){root.classList.add('open'); document.documentElement.style.overflow='hidden'; render(); if(location.hash!=='#diagnose') history.replaceState(null,'','#diagnose')}
function close(){root.classList.remove('open'); document.documentElement.style.overflow=''; clearTimeout(timer); if(location.hash==='#diagnose') history.replaceState(null,'',location.pathname+location.search)}
root.querySelector('#dg-back').onclick=close; root.querySelector('.dg-x').onclick=close;
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&root.classList.contains('open'))close()});
tabs.forEach(b=>b.onclick=()=>{tab=b.dataset.t; render()});
window.dgOpen=open;
async function j(url,opt){const r=await fetch(url,Object.assign({credentials:'same-origin',headers:{'Accept':'application/json'}},opt||{}));const d=await r.json().catch(()=>({error:r.statusText}));if(!r.ok)throw new Error(d.error||r.statusText);return d}
function yday(){return new Date(Date.now()-864e5).toISOString().slice(0,10)}
let bnd=null;
function bad(v){return !!(v&&bnd&&((bnd.min&&v<bnd.min)||(bnd.max&&v>bnd.max)))}
function applyBounds(){const d=root.querySelector('#dg-date'); if(!d||!bnd)return; if(bnd.min)d.min=bnd.min; if(bnd.max)d.max=bnd.max;
 const h=root.querySelector('#dg-bounds'); if(h)h.textContent=T.bounds+(bnd.min||'…')+' ~ '+(bnd.max||'…'); d.dispatchEvent(new Event('input'))}
function render(){tabs.forEach(b=>b.classList.toggle('cur',b.dataset.t===tab)); if(tab==='dict')return renderDict(); renderRun()}
function formHtml(){
 const r=cur||{}; const rng=[1,3,7,14].map(n=>'<option value="'+n+'"'+((r.range||3)==n?' selected':'')+'>'+n+T.days+'</option>').join('');
 return '<div class="dg-card"><form class="dg-form" id="dg-form" novalidate>'+
 '<div><label for="dg-date">'+T.date+' <span class="dg-req">*</span></label><input type="date" id="dg-date" required max="'+((bnd&&bnd.max)||yday())+'"'+(bnd&&bnd.min?' min="'+bnd.min+'"':'')+' value="'+esc(r.date||'')+'"></div>'+
 '<div class="wide"><label for="dg-q">'+T.what+'</label><input id="dg-q" maxlength="500" autocomplete="off" placeholder="'+esc(T.ph)+'" value="'+esc(r.query||'')+'"></div>'+
 '<div><label for="dg-r">'+T.range+'</label><select id="dg-r">'+rng+'</select></div>'+
 '<div><button class="dg-btn" id="dg-go">'+T.start+'</button></div></form><p class="dg-hint dg-err" id="dg-msg" hidden></p>'+
 '<p class="dg-hint" id="dg-bounds"></p><p class="dg-hint" id="dg-chips-h">'+(scanData?(Object.keys(scanData.days||{}).length?T.chips:T.noscan):'<span class="dg-spin"></span>'+T.scanning)+'</p><div class="dg-chips" id="dg-chips"></div></div>';
}
function chips(){
 const box=root.querySelector('#dg-chips'); if(!box||!scanData)return; const days=Object.keys(scanData.days||{}).sort().reverse().slice(0,40);
 box.innerHTML=days.map(d=>{const s=scanData.days[d];const sev=s.some(x=>x.severity==='high')?'high':(s.some(x=>x.severity==='medium')?'medium':'');
  const tip=s.map(x=>L(x,'text')).join('\n');return '<span class="dg-chip '+sev+'" data-d="'+d+'" title="'+esc(tip)+'">'+d.slice(5)+' · '+s.length+'</span>'}).join('');
 box.querySelectorAll('.dg-chip').forEach(c=>c.onclick=()=>{const d=root.querySelector('#dg-date');d.value=c.dataset.d;d.dispatchEvent(new Event('input'))});
}
function bindForm(){
 const f=root.querySelector('#dg-form'), msg=root.querySelector('#dg-msg'), d=root.querySelector('#dg-date'), go=root.querySelector('#dg-go');
 const upd=()=>{const b=bad(d.value); go.disabled=!d.value||b; if(b){msg.textContent=T.outrange+(bnd.min||'…')+' ~ '+(bnd.max||'…'); msg.hidden=false; msg.dataset.r='1'}else if(msg.dataset.r){msg.hidden=true; msg.dataset.r=''}}; d.addEventListener('input',upd); d.addEventListener('change',upd); upd();
 f.onsubmit=async e=>{e.preventDefault(); msg.hidden=true; if(!d.value){msg.textContent=T.need;msg.hidden=false;d.focus();return} if(bad(d.value)){upd();d.focus();return}
  go.disabled=true; const fd=new URLSearchParams({date:d.value,q:root.querySelector('#dg-q').value,range:root.querySelector('#dg-r').value,lang:LANG});
  try{const r=await j(API,{method:'POST',body:fd,headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}}); await loadRun(r.date,r.id)}catch(err){msg.textContent=err.message;msg.hidden=false;go.disabled=false}};
}
function histHtml(){
 return '<div class="dg-card" id="dg-hist"><h3>'+T.history+'</h3>'+(runs.length?'<div class="dg-runs">'+runs.map(r=>{
  const v=r.verdicts||{}; let vt=Object.keys(v).filter(k=>k!=='none').map(k=>tag(k==='consistent'?'ok':(k==='contradict'?'bad':'v'),T.ver[k]+(v[k]>1?' ×'+v[k]:''))).join(' ');
  if(r.pending&&!vt)vt=tag('info',T.pend.split(' · ')[0]);
  return '<div class="dg-run'+(cur&&cur.id===r.id&&cur.date===r.date?' cur':'')+'" data-d="'+esc(r.date)+'" data-i="'+esc(r.id)+'"><b>'+esc(r.date)+'</b><span title="'+esc(r.query)+'">'+esc(r.query||'—')+' · ±'+esc(r.range)+T.days+(L(r,'summary')?' · '+esc(L(r,'summary')):'')+'</span><span>'+(r.stage==='done'?vt||tag('',T.stage.done):tag(r.stage==='error'?'bad':'info',T.stage[r.stage]||r.stage))+'</span></div>'}).join('')+'</div>':'<p class="dg-empty">'+T.nohist+'</p>')+'</div>';
}
async function renderRun(){
 body.innerHTML=formHtml()+'<div id="dg-res"></div>'+histHtml(); bindForm(); chips(); bindHist(); if(cur)showRun();
 if(bnd)applyBounds();
 if(!scanData){j(API+'/scan').then(d=>{scanData=d;bnd={min:d.first_data||null,max:d.last_data||null};applyBounds();const h=root.querySelector('#dg-chips-h');if(h)h.textContent=Object.keys(d.days||{}).length?T.chips:T.noscan;chips()}).catch(()=>{const h=root.querySelector('#dg-chips-h');if(h)h.textContent=T.noscan})}
 j(API+'/runs').then(d=>{runs=d.runs||[]; const box=root.querySelector('#dg-hist'); if(box){box.outerHTML=histHtml(); bindHist()}}).catch(()=>{});
}
function bindHist(){body.querySelectorAll('.dg-run').forEach(el=>el.onclick=()=>loadRun(el.dataset.d,el.dataset.i))}
async function loadRun(date,id){clearTimeout(timer); try{cur=await j(API+'/run/'+date+'/'+id)}catch(e){return} if(tab!=='run'){tab='run'} renderRun(); poll()}
function poll(){clearTimeout(timer); if(cur&&!['done','error'].includes(cur.stage))timer=setTimeout(async()=>{try{cur=await j(API+'/run/'+cur.date+'/'+cur.id); showRun(); if(['done','error'].includes(cur.stage)){j(API+'/runs').then(d=>{runs=d.runs||[];const box=root.querySelector('#dg-hist');if(box){box.outerHTML=histHtml();bindHist()}})}}catch(e){} poll()},4000)}
function stages(r){const order=['queued','collect','cause','status','learn','done']; const i=order.indexOf(r.stage);
 return '<div class="dg-stages">'+order.map((s,k)=>'<span class="dg-stage '+(r.stage==='error'?'':(k<i?'done':(k===i?'now':'')))+'">'+(k===i&&r.stage!=='done'?'<span class="dg-spin"></span>':'')+T.stage[s]+'</span>').join('')+'</div>'}
function quote(c,ev){const e=ev[c.id]||{}; return '<div class="dg-q">'+esc(c.quote||(Z&&e.text_zh?e.text_zh:e.text)||'').slice(0,600)+'<small>'+esc(c.id)+' · '+esc((e.src||c.src||'').toUpperCase())+' · '+esc(ts(e.ts||c.ts))+(e.actor?' · '+esc(e.actor):'')+(e.where?' · '+esc(e.where):'')+((c.link||e.url)?' · <a class="dg-a" target="_blank" rel="noopener" href="'+esc(c.link||e.url)+'">'+T.open+'</a>':'')+'</small></div>'}
function verTag(chk,fu,l){const v=chk.verdict; let t=tag(v==='consistent'?'ok':(v==='contradict'?'bad':(v==='partial'?'warn':'')),T.ver[v]);
 if(v!=='none')t+=' '+(chk.basis||(chk.github&&chk.github.length?['github']:[])).map(b=>tag('v',T.basis[b]||b)).join(' ');
 else if(fu&&!fu.final&&(fu.pending||[]).length)t=tag('info',T.pend+fu.pending[0].slice(5));
 else if(fu&&fu.final)t=tag('',T.unver);
 return t}
function mvRow(r,i){const m=(r.manual||{})[i]||null;
 return '<div class="dg-mv" data-i="'+i+'"><b>'+T.mv+'</b>'+['consistent','partial','contradict'].map(v=>'<button type="button" class="dg-btn ghost'+(m&&m.verdict===v?' on':'')+'" data-v="'+v+'">'+T.mvv[v]+'</button>').join('')+
  '<input maxlength="500" placeholder="'+esc(T.mvph)+'" value="'+esc(m?m.note:'')+'">'+
  (m?'<span class="by">'+T.mvby+esc(m.by||'—')+' · '+esc(ts(m.at))+'</span>':'<span class="by">'+T.mvhint+'</span>')+'</div>'}
function bindMv(box){box.querySelectorAll('.dg-mv').forEach(row=>row.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
 const r=cur; if(!r)return; row.querySelectorAll('button').forEach(x=>x.disabled=true); row.querySelectorAll('.err').forEach(x=>x.remove());
 try{const fd=new URLSearchParams({index:row.dataset.i,verdict:b.dataset.v,note:row.querySelector('input').value,lang:LANG});
  cur=await j(API+'/run/'+r.date+'/'+r.id+'/verify',{method:'POST',body:fd,headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}});
  showRun(); j(API+'/runs').then(d=>{runs=d.runs||[];const hb=root.querySelector('#dg-hist');if(hb){hb.outerHTML=histHtml();bindHist()}}).catch(()=>{})}
 catch(e){row.querySelectorAll('button').forEach(x=>x.disabled=false); row.insertAdjacentHTML('beforeend','<span class="err">'+T.mvfail+esc(e.message)+'</span>')}}))}
function entryName(id){if(id==='new')return T.newe; const e=(dictData&&dictData.entries||[]).find(x=>x.id===id); return e?L(e,'name')||id:id}
function showRun(){
 const box=root.querySelector('#dg-res'); if(!box||!cur)return; const r=cur, ev={}; (r.evidence||[]).forEach(e=>ev[e.id]=e); const recs=(r.verify||{}).records||[].concat((r.verify||{}).github||[],(r.verify||{}).later||[]); recs.forEach(e=>ev[e.id]=e); const fu=r.followup||null; const lr=r.learned||{};
 let h='<div class="dg-card"><h3>'+esc(r.date)+(r.query?' · '+esc(r.query):'')+' · ±'+esc(r.range)+T.days+'</h3>'+stages(r);
 if(r.stage==='error')h+='<p class="dg-err">'+T.failed+esc(r.error)+'</p>';
 else if(r.stage!=='done')h+='<p class="dg-hint">'+T.running+'</p>';
 if(r.window)h+='<p class="dg-hint">'+T.window+' '+esc(r.window.lo)+' → '+esc(r.window.hi)+' · '+T.links+' '+esc((r.focus||[]).join(', '))+(r.sources?' · '+T.srcs+' '+esc(Object.entries(r.sources).map(([k,v])=>k+' '+v).join(' / ')):'')+(r.timings&&r.timings.total?' · '+T.elapsed+' '+Math.round(r.timings.total)+T.sec:'')+'</p>';
 (r.notes||[]).forEach(n=>h+='<p class="dg-note">⚠ '+esc(Z?n.zh:n.en)+'</p>');
 h+='</div>';
 const c=r.cause;
 if(c){
  const s=c.symptom||{}; h+='<div class="dg-banner '+esc(s.status)+'"><b>'+esc(T.sym[s.status]||'')+'</b> — '+esc(L(s,'explain'))+'</div>';
  if(L(c,'summary'))h+='<div class="dg-card"><h3>'+T.summary+'</h3>'+esc(L(c,'summary'))+'</div>';
  if(!(c.findings||[]).length)h+='<div class="dg-card dg-empty">'+T.notfound+'</div>';
  (c.findings||[]).forEach((f,i)=>{
   const chk=((r.verify||{}).checks||[])[i], fx=(r.fix||[])[i];
   const cat=f.entry==='new'?((f.new_entry||{}).category||''):((dictData&&(dictData.entries||[]).find(x=>x.id===f.entry))||{}).category;
   h+='<div class="dg-f"><div class="dg-fh"><strong>'+esc(L(f,'title')||entryName(f.entry))+'</strong>'+(cat?tag('v',T.cat[cat]||cat):'')+tag('',entryName(f.entry))+(f.link?tag('info','#'+f.link):'')+tag(f.confidence==='high'?'ok':(f.confidence==='low'?'warn':''),T.conf[f.confidence]||'')+
    (chk?verTag(chk,fu,lr[i]):'')+(fx?tag(fx.status==='fixed'?'ok':(fx.status==='no_action'?'bad':'warn'),T.fix[fx.status]):'')+'</div>'+(chk?mvRow(r,i):'');
   if(L(f,'explain'))h+='<div style="padding:10px 14px 0;color:#DAD6EA">'+esc(L(f,'explain'))+(f.dates&&f.dates.length?' <span class="dg-mono" style="color:#8E8AA6">('+esc(f.dates.join(', '))+')</span>':'')+'</div>';
   const m=f.misconfig||{};
   h+='<div class="dg-fb"><div class="dg-col"><h4>'+T.c1+'</h4>'+(m.setting?'<p class="dg-kv"><em>'+T.setting+'</em><span class="dg-mono">'+esc(m.setting)+'</span></p>':'')+((L(m,'actual')||m.actual)?'<p class="dg-kv"><em>'+T.actual+'</em>'+esc(L(m,'actual')||m.actual)+'</p>':'')+((L(m,'expected')||m.expected)?'<p class="dg-kv"><em>'+T.expected+'</em>'+esc(L(m,'expected')||m.expected)+'</p>':'')+(L(m,'explain')?'<p class="dg-kv">'+esc(L(m,'explain'))+'</p>':'')+'</div>';
   h+='<div class="dg-col"><h4>'+T.c2+'</h4>'+((f.operators||[]).length?(f.operators||[]).map(o=>'<div class="dg-q"><b>'+esc(o.name)+'</b> · '+esc(T.role[o.role]||o.role)+(L(o,'note')?'<br>'+esc(L(o,'note')):'')+'<small>'+esc(o.evidence)+' · '+esc(o.src.toUpperCase())+' · '+esc(ts(o.ts))+(o.url?' · <a class="dg-a" target="_blank" rel="noopener" href="'+esc(o.url)+'">'+T.open+'</a>':'')+'</small></div>').join(''):'<p class="dg-hint">'+T.noops+'</p>')+'</div>';
   h+='<div class="dg-col"><h4>'+T.c3+'</h4>'+((f.tickets||[]).length?'<ul class="dg-list">'+f.tickets.map(t=>'<li>HubSpot #'+esc(t.ticket)+' «'+esc(t.title)+'» '+tag(/closed|resolved|done|complete|\u5b8c\u6210|\u5173\u95ed/i.test(t.status||'')?'ok':'warn',t.status||'—')+(t.owner?' · '+esc(t.owner):'')+'</li>').join('')+'</ul>':'<p class="dg-hint">'+T.notk+'</p>');
   if(fx)h+='<p class="dg-kv"><em>'+T.datast+'</em>'+tag(fx.data==='normal'?'ok':(fx.data==='abnormal'?'bad':''),T.data[fx.data])+(fx.data_signals&&fx.data_signals.length?' <span class="dg-mono">'+esc(fx.data_signals.join(', '))+'</span>':'')+'</p>';
   if(chk&&chk.fix&&chk.fix.length)h+='<p class="dg-kv"><em>'+T.fixev+'</em></p>'+chk.fix.slice(0,4).map(x=>quote(x,ev)).join('');
   const sup=chk?(chk.support||chk.github||[]):[]; if(sup.length)h+='<p class="dg-kv"><em>'+T.gh+(L(chk,'reason')?' — '+esc(L(chk,'reason')):'')+'</em></p>'+sup.slice(0,3).map(x=>quote(x,ev)).join('');
   h+='</div></div>';
   h+='<details class="dg-d" style="margin:0;border:0;border-top:1px solid rgba(255,255,255,.06);border-radius:0"><summary>'+T.cites+' ('+(f.evidence||[]).length+')'+((f.signals||[]).length?' · '+T.sig+' '+esc(f.signals.join(', ')):'')+'</summary><div class="in">'+(f.evidence||[]).map(x=>quote(x,ev)).join('')+'</div></details>';
   h+='</div>';
  });
  if((c.dropped||[]).length)h+='<details class="dg-d"><summary>'+T.dropped+' ('+c.dropped.length+')</summary><div class="in"><ul class="dg-list">'+c.dropped.map(d=>'<li>'+esc(d.title)+' — '+esc(d.reason)+'</li>').join('')+'</ul></div></details>';
 }
 if(r.learning)h+='<details class="dg-d"'+(r.learning.length?' open':'')+'><summary>'+T.learn+'</summary><div class="in">'+(r.learning.length?'<ul class="dg-list">'+r.learning.map(x=>'<li><span class="dg-mono">'+esc(x.entry)+'</span> · '+esc(x.action)+'</li>').join('')+'</ul>':'<p class="dg-hint">'+T.nolearn+'</p>')+'</div></details>';
 if((r.signals||[]).length)h+='<details class="dg-d"><summary>'+T.signals+' ('+r.signals.length+')</summary><div class="in"><table class="dg-t"><tbody>'+r.signals.map((s,i)=>'<tr><td class="dg-mono">S'+i+'</td><td>'+tag(s.severity==='high'?'bad':(s.severity==='medium'?'warn':''),T.kind[s.kind]||s.kind)+'</td><td>'+esc(L(s,'text'))+'</td></tr>').join('')+'</tbody></table></div></details>';
 const evl=(list,title)=>list&&list.length?'<details class="dg-d"><summary>'+title+' ('+list.length+')</summary><div class="in">'+list.map(e=>'<div class="dg-ev"><span class="src">'+esc(e.id)+'<br>'+esc(e.src)+'</span><span class="t">'+esc(ts(e.ts))+(e.actor?'<br>'+esc(e.actor):'')+(e.where?'<br>'+esc(e.where):'')+(e.url?'<br><a class="dg-a" target="_blank" rel="noopener" href="'+esc(e.url)+'">'+T.open+'</a>':'')+'</span><span class="x">'+esc(Z&&e.text_zh?e.text_zh:e.text)+'</span></div>').join('')+'</div></details>':'';
 if(fu&&r.stage==='done')h+='<p class="dg-hint">'+T.followup+(Z?'：':': ')+(!((r.cause||{}).findings||[]).length?T.funof:fu.final&&!(fu.done||[]).length&&!(fu.pending||[]).length?T.funone:((fu.done||[]).length?T.fudone+esc(fu.done.join(', '))+' ':'')+((fu.pending||[]).length?T.fuleft+esc(fu.pending.join(', ')):''))+'</p>';
 h+=evl(r.evidence,T.evidence)+evl(recs,T.ghlist);
 box.innerHTML=h; bindMv(box);
 const hist=body.querySelectorAll('.dg-run'); hist.forEach(el=>el.classList.toggle('cur',el.dataset.i===r.id&&el.dataset.d===r.date));
 if(!dictData)j('/diagnose/dict').then(d=>{dictData=d; if(tab==='run')showRun()}).catch(()=>{});
}
async function renderDict(){
 body.innerHTML='<div class="dg-empty"><span class="dg-spin"></span></div>';
 try{dictData=await j('/diagnose/dict')}catch(e){body.innerHTML='<p class="dg-err">'+esc(e.message)+'</p>';return}
 const d=dictData, s=d.summary||{}, t=s.totals||{}, reg=(d.regression||{}).runs||[], last=reg[reg.length-1];
 let h='<div class="dg-stat"><div><b>'+(s.entries||0)+'</b><span>'+T.entries+'</span></div>'+Object.entries(s.by_status||{}).map(([k,v])=>'<div><b>'+v+'</b><span>'+esc(T.st[k]||k)+'</span></div>').join('')+
  '<div><b>'+(t.confirmed||0)+' / '+(t.rejected||0)+'</b><span>'+T.confirm+' / '+T.reject+'</span></div>'+(last?'<div><b>'+Math.round(last.hit_rate*100)+'%</b><span>'+T.reg+' '+T.hit+' ('+last.cases+')</span></div>':'')+'</div>';
 h+='<div class="dg-card"><h3>'+T.reg+'</h3>'+(reg.length?'<table class="dg-t"><thead><tr><th>time</th><th>cases</th><th>'+T.hit+'</th><th>'+T.comp+'</th></tr></thead><tbody>'+reg.slice().reverse().slice(0,10).map(x=>'<tr><td>'+esc(ts(x.at))+'</td><td>'+x.cases+'</td><td>'+Math.round(x.hit_rate*100)+'%</td><td>'+Math.round(x.complete_rate*100)+'%</td></tr>').join('')+'</tbody></table>':'<p class="dg-hint">'+T.regnone+'</p>')+'</div>';
 h+='<div class="dg-card"><h3>'+T.dict+'</h3><div style="overflow-x:auto"><table class="dg-t"><thead><tr><th>'+T.col.id+'</th><th>'+T.col.cat+'</th><th>'+T.col.st+'</th><th>'+T.col.src+'</th><th>'+T.col.stat+'</th><th>'+T.col.ph+'</th></tr></thead><tbody>'+
  (d.entries||[]).map(e=>'<tr><td><b>'+esc(L(e,'name'))+'</b><br><span class="dg-mono" style="color:#8E8AA6">'+esc(e.id)+'</span><br><span style="color:#ABA5C0">'+esc(L(e,'desc'))+'</span>'+(e.curation?'<br><span style="color:#9FE3EE;font-size:11px">'+esc(T.cur[e.curation.action]||'')+(e.curation.action==='merge'?esc(entryName(e.curation.into))+' — ':'')+esc(e.curation.reason||'')+'</span>':'')+'</td><td>'+esc(T.cat[e.category]||e.category)+'</td><td>'+tag(e.status==='confirmed'?'ok':(e.status==='deprecated'?'bad':(e.status==='candidate'?'warn':(e.status==='merged'?'info':''))),T.st[e.status]||e.status)+'</td><td>'+esc(e.source)+'</td><td class="dg-mono">'+[e.stats.confirmed,e.stats.partial,e.stats.rejected,e.stats.unverified].join(' / ')+'</td><td style="max-width:240px">'+esc((e.phrases||[]).slice(0,6).join(' · '))+'</td></tr>').join('')+'</tbody></table></div></div>';
 h+='<details class="dg-d"><summary>'+T.clog+'</summary><div class="in"><ul class="dg-list">'+(d.changelog||[]).map(c=>'<li><span class="dg-mono">'+esc(ts(c.ts))+'</span> · '+esc(c.actor)+' · '+esc(c.note)+' <span class="dg-mono" style="color:#8E8AA6">'+esc((c.entries||[]).join(', '))+'</span></li>').join('')+'</ul></div></details>';
 body.innerHTML=h;
}
if(location.hash==='#diagnose')open();
})();
"""


def _one_language(js, lang):
    """脚本里只留当前语言的界面文字（英文页不能出现汉字）。"""
    drop = ("/*EN*/", "/*/EN*/") if lang == "zh" else ("/*ZH*/", "/*/ZH*/")
    i, j = js.index(drop[0]), js.index(drop[1]) + len(drop[1])
    js = js[:i] + js[j:]
    return js.replace("/*ZH*/", "").replace("/*/ZH*/", "").replace("/*EN*/", "").replace("/*/EN*/", "")


def panel(n, lang, name):
    """插进页面的面板（样式 + 脚本）。数据都走接口，这里只放活动编号、语言、名称和这一种语言的界面文字。"""
    js = (_one_language(JS, lang).replace("__CID__", json.dumps(str(n))).replace("__LANG__", json.dumps(lang))
          .replace("__NAME__", json.dumps(name or "", ensure_ascii=False).replace("</", "<\\/")))
    return f"<style>{CSS}</style><script>{js}</script>"


def page(n, lang, name):
    """单独的排查页面：面板铺满整页并直接打开；关闭（×、点空白、Esc）回首页。"""
    z = lang == "zh"
    title = ("异常排查 · " if z else "Diagnose · ") + str(name or n)
    home = f"/?lang={lang}"
    esc = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")  # noqa: E731
    return f"""<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>html,body{{margin:0;background:#070812;color:#F4F2FA}}#dg-panel{{width:100%!important}}#dg-back{{display:none}}</style>
</head><body>{panel(n, lang, name)}<script>
(function(){{if(!window.dgOpen)return;window.dgOpen();var r=document.getElementById('dg-root'),home=function(){{location.href={json.dumps(home)}}};
var x=r&&r.querySelector('.dg-x');if(x)x.onclick=home;document.addEventListener('keydown',function(e){{if(e.key==='Escape')home()}},true);}})();
</script></body></html>"""
