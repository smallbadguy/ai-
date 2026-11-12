"""Adapt Pi's final JSON to the existing renderer after its normal exit.
Only structural validation; quotes are used as written (no verification), no keyword attribution.
"""
import datetime as dt
import hashlib
import json
import pathlib
import re

from . import build, business, feeds, progress, scope, util

ENGINE = "pi-online-v3"


def pointer(doc, value):
    if not value:return doc
    if not isinstance(value,str) or not value.startswith("/"):raise ValueError("Invalid JSON pointer")
    for part in value[1:].split("/"):
        key=part.replace("~1","/").replace("~0","~")
        doc=doc[int(key)] if isinstance(doc,list) else doc[key]
    return doc


def strings(doc):
    if isinstance(doc,str):yield doc
    elif isinstance(doc,dict):
        for v in doc.values():yield from strings(v)
    elif isinstance(doc,list):
        for v in doc:yield from strings(v)
    elif doc is not None:yield str(doc)


def evidence(work, spec):
    """原话照 AI 写的用，程序不核对（用户 2026-11-03 定的：只在提示词里要求写原文，后台不核对）。"""
    if not isinstance(spec,dict) or not str(spec.get("quote") or "").strip():raise ValueError("缺少原话")
    return str(spec["quote"]).strip()


def _leaves(doc, path):
    if isinstance(doc,str):yield path,doc
    elif isinstance(doc,dict):
        for k,v in doc.items():yield from _leaves(v,path+"/"+str(k).replace("~","~0").replace("/","~1"))
    elif isinstance(doc,list):
        for i,v in enumerate(doc):yield from _leaves(v,f"{path}/{i}")


def stamp(value):
    s=str(value or "")
    day=bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}",s))
    parsed=dt.datetime.fromisoformat(s.replace("Z","+00:00"))
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"day" if day else "sec"


def bilingual(obj, pairs, strict=True):
    for zh,en in pairs:
        has_zh=isinstance(obj.get(zh),str) and obj[zh].strip()
        has_en=isinstance(obj.get(en),str) and obj[en].strip()
        if strict and not has_zh:raise ValueError("缺少 "+zh)
        if strict and not has_en:raise ValueError("缺少英文："+en)
        # 宽松模式：缺一种语言就用另一种补上，页面上不出现「未填写」之类的字样
        if not has_zh:obj[zh]=obj[en] if has_en else ""
        if not has_en:obj[en]=obj[zh] if has_zh else ""
        if strict and util.has_han(obj[en]):raise ValueError("英文含汉字："+en)


def repair_json(text, limit=300):
    """模型常在中文里直接写英文双引号（如 确认"spend 录入正常"），导致 JSON 写坏。
    逐个把出错位置之前那个未转义的引号转义，最多修 limit 处；修不好返回 None。"""
    for _ in range(limit):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            i=text.rfind('"',0,exc.pos)
            while i>0 and text[i-1]=="\\":i=text.rfind('"',0,i-1)
            if i<=0:return None
            text=text[:i]+'\\"'+text[i+1:]
    return None


def load_report(path):
    """读结果文件；写坏的 JSON 先尝试自动修复（修复后的内容写回原文件，原文件另存 .broken）。"""
    path=pathlib.Path(path)
    if not path.exists():return None
    text=path.read_text(encoding="utf-8")
    try:return json.loads(text)
    except json.JSONDecodeError:pass
    fixed=repair_json(text)
    if fixed is None:raise ValueError("output/report.json 不是合法的 JSON，自动修复也没有成功")
    path.with_suffix(".json.broken").write_text(text,encoding="utf-8")
    path.write_text(json.dumps(fixed,ensure_ascii=False,indent=1),encoding="utf-8")
    return fixed


def adapt(cid, work, test_mode=False):
    work=pathlib.Path(work)
    report=load_report(work/"output/report.json")
    if not isinstance(report,dict):raise ValueError("结果不是 JSON 对象")
    if not test_mode and report.get("status") != "complete":raise ValueError("结果仍为草稿，旧页面保留")
    warnings,fixed=[],[]
    def drop(what):
        """严格模式直接报错（旧页面保留）；首轮宽松模式跳过这一条、记到后台。"""
        if not test_mode:raise ValueError(what)
        warnings.append("跳过："+what)
    def read_evidence(spec):
        try:
            quote=evidence(work,spec)
            if isinstance(spec,dict) and "pointer_fixed" in spec:
                fixed.append({"file":spec.get("file"),"from":spec.get("pointer"),"to":spec["pointer_fixed"]})
            return quote
        except (OSError,ValueError,TypeError,KeyError,IndexError) as exc:
            if not test_mode:raise
            warnings.append(str(exc))
            return str((spec or {}).get("quote") or "")
    brief=util.read_json(work/"seed/brief.json")
    if str(brief["campaign_id"])!=str(cid) or brief.get("vendor_id")!=scope.vendor_for(cid):raise ValueError("结果的活动/渠道不一致")
    terms=brief["terms"]
    bilingual(report,[("title_zh","title_en"),("summary_zh","summary_en")],strict=not test_mode)
    if not isinstance(report.get("moments"),list) or not report["moments"]:raise ValueError("没有业务节点")
    chapters=[]
    for i,ch in enumerate(report.get("chapters") or [],1):
        bilingual(ch,[("name_zh","name_en")],strict=not test_mode);dt.date.fromisoformat(ch["start"])
        chapters.append({**ch,"id":f"c{i}"})
    chapters.sort(key=lambda c:c["start"])
    if not chapters:
        if not test_mode:raise ValueError("缺少阶段")
        first=min((str(m.get("ts") or "")[:10] for m in report["moments"] if m.get("ts")),default=util.now_z()[:10])
        chapters=[{"id":"c1","name_zh":"全过程","name_en":"Overview","start":first}]
    candidates,events=[],[]
    for i,m in enumerate(report["moments"],1):
        bilingual(m,[("action_zh","action_en"),("context_zh","context_en")],strict=not test_mode)
        if m.get("kind") not in ("slack","email","hubspot","meeting","github"):drop(f"节点{i}来源未知");continue
        if not m.get("speaker"):drop(f"节点{i}缺少发言人");continue
        if not m.get("belongs_because"):
            if not test_mode:raise ValueError("节点缺少归属说明")
            m["belongs_because"]=""
        quote=read_evidence(m.get("evidence"))
        if not test_mode and len(quote)>business.MAX_QUOTE:raise ValueError("原话太长")
        try:ts,prec=stamp(m["ts"])
        except (KeyError,TypeError,ValueError):drop(f"节点{i}时间无法识别");continue
        c={"id":f"C{i:04d}","fp":hashlib.sha256(json.dumps(m["evidence"],sort_keys=True).encode()).hexdigest()[:20],
           "kind":m["kind"],"ts":ts,"prec":prec,"speaker":m["speaker"],"quote":quote,"source":m.get("source",""),
           "scope":m.get("scope","case"),"why":m["belongs_because"],"anchored":False,"evidence":m["evidence"]}
        if c["scope"] not in business.SCOPES:
            if not test_mode:raise ValueError("未知归属范围")
            c["scope"]="case"
        candidates.append(c)
        events.append({**{k:m.get(k) for k in ("action_zh","action_en","context_zh","context_en","translation","to")},
                       "candidate":c["id"],"chapter":next((ch["id"] for ch in reversed(chapters) if ch["start"]<=ts[:10]),chapters[0]["id"])})
    plan={**report,"chapters":chapters,"events":events}
    chosen=business.select(candidates,plan,log=lambda *_:None,strict=not test_mode)[2]
    if not candidates:raise ValueError("没有可用的业务节点")
    if len(chosen)!=len(candidates):
        if not test_mode:raise ValueError("业务节点格式或译文不完整，拒绝静默丢弃")
        warnings.append(f"有 {len(candidates)-len(chosen)} 个节点格式不完整，未展示")
    for p in report.get("people",[]):bilingual(p,[("role_zh","role_en")],strict=not test_mode)
    for w in report.get("workstreams",[]):bilingual(w,[("title_zh","title_en"),("text_zh","text_en")],strict=not test_mode)
    def selected(file,key):
        index={str(x["id"]):x for x in util.read_json(work/"seed"/file,[])}
        ids=[str(i) for i in report.get(key,[])]
        if len(ids)!=len(set(ids)) or any(i not in index for i in ids):
            if not test_mode:raise ValueError(key+" 含未知或重复编号")
            warnings.append(key+" 含未知或重复编号，已忽略这些编号")
            ids=list(dict.fromkeys(i for i in ids if i in index))
        return [index[i] for i in ids]
    backend=[]
    handoffs=selected("handoffs.json","handoffs")
    changes={str(c["id"]):c for c in util.read_json(work/"seed/changes.json",[])}
    used=set()
    for choice in report.get("backend_changes",[]):
        ids=[str(i) for i in choice["ids"]] if isinstance(choice,dict) else [str(choice)]
        if not ids or len(ids)!=len(set(ids)) or any(i not in changes or i in used for i in ids):
            drop("backend_changes 含未知或重复编号");continue
        used.update(ids)
        if len(ids)==1:
            backend.append(changes[ids[0]])
            continue
        bilingual(choice,[("title_zh","title_en"),("context_zh","context_en")],strict=not test_mode)
        rows=[changes[i] for i in ids]
        owners={c["user"] for c in rows}
        # A grouped row is an AI summary of several real operations, not a fabricated single delta.
        handoffs.append({**choice,"ts":max(c["ts"] for c in rows),"system":"femini",
                         "actor":next(iter(owners)) if len(owners)==1 else "FeedMob Admin",
                         "src":"femini","kind":"change","tag_zh":"FeedMob Admin 后台","tag_en":"FeedMob Admin"})
    for f in report.get("facts",[]):
        bilingual(f,[("title_zh","title_en"),("context_zh","context_en")],strict=not test_mode);read_evidence(f.get("evidence"))
        try:ts,prec=stamp(f["ts"])
        except (KeyError,TypeError,ValueError):drop("后台事实时间无法识别");continue
        handoffs.append({**f,"ts":ts,"prec":prec,"system":"femini","actor":f.get("actor") or "FeedMob Admin",
                         "src":"femini","kind":"change","tag_zh":"FeedMob Admin 后台","tag_en":"FeedMob Admin"})
    items=[]
    today=dt.datetime.now(dt.timezone.utc).date()
    for i,item in enumerate(report.get("open_items",[]),1):
        bilingual(item,[("topic","topic_en"),("state_note","state_note_en"),("next_step","next_step_en")],strict=not test_mode)
        if item.get("scope") not in ("this_campaign","client_general","unclear") or item.get("status") not in progress.STATUSES:
            drop(f"事项{i}状态或范围不合法");continue
        if item.get("owner_basis") not in progress.BASIS:
            if not test_mode:raise ValueError("负责人依据不合法")
            item["owner_basis"]="none"
        ev=[]
        for e in item.get("evidence",[]):
            if not isinstance(e,dict) or not e.get("speaker"):drop(f"事项{i}的一条证据缺少发言人");continue
            try:ts,_=stamp(e["ts"])
            except (KeyError,TypeError,ValueError):drop(f"事项{i}的一条证据时间无法识别");continue
            ev.append({**e,"quote":read_evidence(e),"ts":ts})
        if not ev:drop(f"事项{i}没有可用的原话证据");continue
        last=max(e["ts"][:10] for e in ev);age=(today-dt.date.fromisoformat(last)).days
        items.append({**item,"id":f"pi-{i}","evidence":ev,"last_activity":last,"age_days":age,"stale":age>progress.STALE_DAYS,
                      "next_owner":"待确认" if item["owner_basis"]=="none" else item.get("next_owner") or "待确认"})
    prog={"engine":ENGINE,"built_at":util.now_z(),"items":items,"closed":[],"campaign":brief.get("campaign"),"as_of":brief["as_of"]}
    cases=business.assemble(cid,terms,candidates,plan,backend,today,log=lambda *_:None,handoffs=handoffs,progress_doc=prog,strict=not test_mode)
    for lang,case in cases.items():
        case["meta"].update(engine=ENGINE,generation_version="v3",attribution_version=ENGINE,first_pass_test=bool(test_mode))
        # 首轮测试、引用待核对等情况只记在后台（business.json 的 first_pass_test / reference_notes），不写到页面上
    state={"engine":ENGINE,"built_at":util.now_z(),"attribution_version":ENGINE,"candidates":candidates,"plan":plan,
           "first_pass_test":bool(test_mode),"reference_notes":warnings,"reference_fixed":fixed,
           "coverage":report.get("coverage"),"unresolved":report.get("unresolved",[])}
    return cases,state,prog


def publish(cid, work, test_mode=None):
    if test_mode is None:
        test_mode=feeds._env_file("pi.env").get("REPLAY_PI_TEST_MODE")=="1"
    cases,state,prog=adapt(cid,work,test_mode=test_mode)
    # Render BOTH languages before touching published files. Invalid output leaves the old page intact.
    outputs={}
    od=util.out_dir(cid)
    for lang,case in cases.items():
        outputs[od/f"business.{lang}.html"]=build.render(case,lang).encode()
        outputs[od/f"business.case.{lang}.json"]=json.dumps(case,ensure_ascii=False).encode()
    outputs[util.camp_dir(cid)/"business.json"]=json.dumps(state,ensure_ascii=False).encode()
    outputs[util.camp_dir(cid)/"progress.json"]=json.dumps(prog,ensure_ascii=False).encode()
    backup=pathlib.Path(work).parent/"previous"
    backup.mkdir(exist_ok=True)
    old={p:p.read_bytes() if p.exists() else None for p in outputs}
    for i,(p,data) in enumerate(old.items()):
        if data is not None:(backup/f"{i}-{p.name}").write_bytes(data)
    def put(p,data):
        p.parent.mkdir(parents=True,exist_ok=True)
        tmp=p.with_name(p.name+".pi.tmp");tmp.write_bytes(data);tmp.replace(p)
    installed=[]
    try:
        for p,data in outputs.items():put(p,data);installed.append(p)
    except Exception:
        for p in reversed(installed):
            if old[p] is None:p.unlink(missing_ok=True)
            else:put(p,old[p])
        raise
    return {"moments":len(state["candidates"]),"events":len(cases["zh"]["events"]),"open_items":len(prog["items"]),
            "first_pass_test":bool(test_mode),"reference_notes":len(state["reference_notes"]),
            "reference_fixed":len(state["reference_fixed"])}
