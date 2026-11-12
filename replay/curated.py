"""Build a fixed, bilingual replay from a manually reviewed file. Never collects data."""
import argparse
import hashlib
import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path

from .scene import scene_document
from .util import ROOT, has_han

TEMPLATE = ROOT / "src" / "template.html"
PHASES = {
    "prepare": ("投放准备", "Preparation"), "check": ("执行确认", "Checks"),
    "launch": ("报告上线", "Launch reported"), "pause": ("临时暂停", "Temporary pause"),
    "test": ("测试反馈", "Test feedback"), "followup": ("后续跟踪", "Follow-up"),
    "review": ("效果核对", "Performance review"), "decision": ("业务决定", "Business decision"),
    "optimize": ("投放调整", "Campaign adjustment"), "budget": ("预算管理", "Budget management"),
    "creative": ("素材与审批", "Creatives and review"), "client": ("客户反馈", "Client feedback"),
}
CHAPTERS = {
    "launch": ("启动与客户目标", "Launch and client goals"),
    "measurement": ("核对效果", "Checking the results"),
    "improve": ("优化与公平比较", "Optimization and fair comparison"),
    "pacing": ("预算与投放节奏", "Budget and pacing"),
    "feedback": ("客户反馈与下一步", "Client feedback and next steps"),
}
SCOPES = {
    "case": ("本活动", "This campaign"),
    "related": ("关联 LG 投放", "Related LG campaigns"),
    "client": ("客户整体沟通", "Client-wide discussion"),
}
NARRATIVE = ("context", "decision", "result", "excerpt")
UI = {
    "zh": dict(edition="业务精选", participants="参与者", duties="本案分工", timeline="业务过程", event="当前进展", handoff="相关协作", outcome="这段回放的记录范围", play="播放", pause="暂停", previous="上一步", next="下一步", timezone="北京时间", utc="UTC", version="精选版", checked="来源已核对", source="依据", conversation="业务沟通", reporting="运营跟踪", period="历史回放", step="步", roles="按本案实际协作展示", scope="当前节点", overview="总览", follow="跟随当前人物", language="English", time_note="按沟通记录的时间回放", ready="3D 场景加载中…"),
    "en": dict(edition="Business replay", participants="Participants", duties="Role in this case", timeline="Business timeline", event="Current progress", handoff="Collaboration", outcome="What this replay records", play="Play", pause="Pause", previous="Previous", next="Next", timezone="Beijing time", utc="UTC", version="Curated", checked="Source checked", source="Source", conversation="Business conversation", reporting="Operational follow-up", period="Historical replay", step="steps", roles="Contributions in this replay", scope="Current step", overview="Overview", follow="Follow current person", language="Chinese", time_note="Times follow the recorded messages", ready="Loading the 3D scene…"),
}
UI["zh"].update(original="原始发言", translation="中文参考译文", commentary="整理说明（非原话）", links_hidden="仅隐去链接及附件地址", chapters="业务阶段", context="当时的情况", decision="决定与执行", result="反馈与后续", excerpt="沟通节选 · 摘译", scope_note="当前活动为主线，关联投放和客户整体沟通另作标记。")
UI["en"].update(original="Original message", translation="English reference translation", commentary="Editorial notes (not original speech)", links_hidden="Links and attachment addresses omitted", chapters="Business stages", context="The situation", decision="Decision and action", result="Feedback and next steps", excerpt="Conversation extract", scope_note="The main campaign, related LG work and client-wide discussions are marked separately.")


def _localized(value, field):
    if not isinstance(value, dict) or any(not isinstance(value.get(lang), str) or not value[lang].strip() for lang in ("zh", "en")):
        raise ValueError(f"{field} needs non-empty zh and en text")
    if has_han(value["en"]):
        raise ValueError(f"{field}: English text contains Chinese characters")


def display_original(raw):
    """Keep the actual words; omit private link targets, never replace speech with a summary."""
    text = unescape(raw)
    text = re.sub(r"<img\b[^>]*>|!\[[^\]]*\]\([^)]*\)", "[attachment]", text, flags=re.I)
    def link(match):
        label = match.group(1)
        is_address = re.match(r"https?://|[a-z0-9.-]+\.[a-z]{2,}(?:[/:?#]|$)", label or "", flags=re.I)
        return (label + " " if label and not is_address else "") + "[link omitted]"
    text = re.sub(r"\[([^\]]*)\]\(https?://[^)]*\)", link, text)
    text = re.sub(r"<https?://[^|>]+\|([^>]*)>", link, text)
    text = re.sub(r"<https?://[^>]+>|https?://[^\s<>\]\)]+", "[link omitted]", text)
    return text.strip()


def validate(document):
    if document.get("schema_version") != 1 or document.get("slug") != "chime" or str(document.get("campaign_id")) != "5023":
        raise ValueError("Expected the reviewed Chime 5023 document, schema version 1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}", document.get("version", "")):
        raise ValueError("Invalid version")
    for field in ("title", "scope", "outcome"):
        _localized(document.get(field), field)
    people = document.get("people") or []
    ids = [p.get("id") for p in people]
    if not ids or len(set(ids)) != len(ids) or any(not isinstance(i, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", i) for i in ids):
        raise ValueError("Invalid or duplicate participant IDs")
    for person in people:
        _localized(person.get("role"), "person.role")
        if not all(isinstance(person.get(k), str) and person[k].strip() and not has_han(person[k]) for k in ("name", "short")):
            raise ValueError("Participant names must be shared by both languages")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", person.get("color", "")):
            raise ValueError("Participant colors must be explicit hex values")
    seen, previous = set(), None
    events = document.get("events") or []
    if not events:
        raise ValueError("A curated replay needs reviewed events")
    for event in events:
        eid = event.get("id")
        if not isinstance(eid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", eid) or eid in seen:
            raise ValueError("Invalid or duplicate event ID")
        seen.add(eid)
        stamp = event.get("ts", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp):
            raise ValueError("Event timestamps must use UTC with seconds")
        current = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if previous and current < previous:
            raise ValueError("Events must be in chronological order")
        previous = current
        if event.get("actor") not in ids or not isinstance(event.get("to"), list) or any(p not in ids for p in event["to"]):
            raise ValueError("Unknown event participant")
        if event.get("phase") not in PHASES or event.get("src") not in {"slack", "github", "meeting", "ticket", "email"}:
            raise ValueError("Unknown curated event phase or source")
        for field in ("action", "title", "text"):
            _localized(event.get(field), "event." + field)
        if "source_detail" in event:
            _localized(event["source_detail"], "event.source_detail")
            if any(re.search(r"https?://|\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", event["source_detail"][lang], re.I) for lang in ("zh", "en")):
                raise ValueError("Public source details cannot contain direct URLs or email addresses")
        if event.get("chapter", "launch") not in CHAPTERS or event.get("scope_level", "case") not in SCOPES:
            raise ValueError("Unknown business chapter or scope")
        if any(field in event for field in NARRATIVE):
            for field in NARRATIVE:
                _localized(event.get(field), "event." + field)
            if event.get("excerpt_speaker", event["actor"]) not in ids:
                raise ValueError("Unknown excerpt speaker")
        evidence = event.get("evidence") or {}
        if evidence.get("status") != "verified" or evidence.get("at") != stamp or not evidence.get("ref") or not evidence.get("quote"):
            raise ValueError("Every published event needs a verified reference, quote and matching timestamp")
        original = event.get("original")
        if original is not None:
            if not isinstance(original, dict):
                raise ValueError("Original speech must be an object")
            raw = evidence.get("original_text")
            if not isinstance(raw, str) or not raw.strip() or original.get("text") != raw:
                raise ValueError("Original speech must equal the independently retained source message")
            if original.get("sha256") != hashlib.sha256(raw.encode()).hexdigest() or original.get("at") != stamp:
                raise ValueError("Original speech checksum or timestamp does not match")
            if original.get("speaker") != event["actor"] or not original.get("speaker_label"):
                raise ValueError("Original speech has a different speaker")
            if original.get("language") not in {"zh", "en"}:
                raise ValueError("Unknown original language")
            _localized(original.get("translation"), "original.translation")
    if document.get("playback") == "original" and any(not e.get("original") for e in events):
        raise ValueError("Original-message replay cannot contain summary-only steps")


def public_case(document, lang):
    """Explicit allowlist: approved original speech is distinct from private evidence and notes."""
    if lang not in ("zh", "en"):
        raise ValueError("Unsupported language")
    validate(document)
    ui = UI[lang]
    case = {
        "lang": lang, "ui": ui,
        "meta": dict(no=str(document["campaign_id"]), client="Chime", title=document["title"][lang], edition="curated", version=document["version"], scope=document["scope"][lang], outcome=document["outcome"][lang], playback=document.get("playback", "narrative")),
        "people": [dict(id=p["id"], name=p["name"], short=p["short"], color=p["color"], kind="human", role=p["role"][lang]) for p in document["people"]],
        "sources": [dict(id="slack", name=ui["conversation"], status="ok"), dict(id="github", name=ui["reporting"], status="ok"), dict(id="meeting", name="业务会议" if lang == "zh" else "Business meeting", status="ok"), dict(id="ticket", name="业务工单" if lang == "zh" else "Business ticket", status="ok"), dict(id="email", name="邮件" if lang == "zh" else "Email", status="planned")],
        "chapters": [dict(id=key, name=names[lang == "en"]) for key, names in CHAPTERS.items() if any(e.get("chapter", "launch") == key for e in document["events"])],
        "events": [],
    }
    for event in document["events"]:
        chapter, scope = event.get("chapter", "launch"), event.get("scope_level", "case")
        public = dict(id=event["id"], key=event["id"], ts=event["ts"], actor=event["actor"], to=event["to"], src=event["src"], srcs=[event["src"]], kind="chat", phase=event["phase"], phase_name=PHASES[event["phase"]][lang == "en"], action=event["action"][lang], title=event["title"][lang], text=event["text"][lang], chapter=chapter, chapter_name=CHAPTERS[chapter][lang == "en"], scope_level=scope, scope_name=SCOPES[scope][lang == "en"])
        for field in NARRATIVE:
            if field in event:
                public[field] = event[field][lang]
        if "source_detail" in event:
            public["source_detail"] = event["source_detail"][lang]
        if "excerpt" in event:
            public["excerpt_speaker"] = event.get("excerpt_speaker", event["actor"])
        if "original" in event:
            original = event["original"]
            text = display_original(original["text"])
            public["original"] = dict(text=text, language=original["language"], speaker_label=original["speaker_label"], translation=original["translation"][lang], redacted=text != unescape(original["text"]).strip())
        case["events"].append(public)
    # Real speech keeps its source language on either UI; all editorial/UI English must remain translated.
    english_check = dict(case, events=[{**e, "original": {k: v for k, v in e.get("original", {}).items() if k not in {"text", "speaker_label"}}} for e in case["events"]])
    if lang == "en" and has_han(json.dumps(english_check, ensure_ascii=False)):
        raise ValueError("English interface or editorial text contains Chinese characters")
    return case


def standard_case(document, lang):
    """Adapt verified curated records to the automatic replay's shared data contract."""
    reviewed = public_case(document, lang)
    z = lang == "zh"
    source_names = {
        "github": ("GitHub 工单" if z else "GitHub tickets"),
        "hubspot": ("HubSpot 工单" if z else "HubSpot tickets"),
        "slack": ("Slack 消息" if z else "Slack messages"),
        "meeting": ("会议记录" if z else "Meeting records"),
        "email": ("邮件往来" if z else "Email correspondence"),
    }
    source_for = {}
    for event in document["events"]:
        src = event["src"]
        if src == "ticket":
            ref = str((event.get("evidence") or {}).get("ref", "")).lower()
            if "github.com/" in ref:
                src = "github"
            elif "hubspot" in ref:
                src = "hubspot"
            else:
                raise ValueError(f"Cannot identify ticket system for {event['id']}")
        if src not in source_names:
            raise ValueError(f"Unsupported source {src!r} for {event['id']}")
        source_for[event["id"]] = src

    counts = {source: sum(mapped == source for mapped in source_for.values()) for source in source_names}
    descriptions = {
        "github": (("已核对的 GitHub 工单记录", "工单评论及相关业务记录", "只纳入已核对并选入当前预览的记录") if z else
                   ("Verified GitHub ticket records", "Ticket comments and related case records", "Only reviewed records in this current preview are included")),
        "hubspot": (("已核对的 HubSpot 工单记录", "客户请求及运营跟进", "只纳入已核对并选入当前预览的记录") if z else
                    ("Verified HubSpot ticket records", "Client requests and operational follow-up", "Only reviewed records in this current preview are included")),
        "slack": (("Slack 内部沟通", "频道和小组消息", "只纳入已核对并选入当前预览的记录") if z else
                  ("Internal Slack conversations", "Channel and small-group messages", "Only reviewed records in this current preview are included")),
        "meeting": (("会议记录", "发言原文及时间", "只纳入已核对并选入当前预览的记录") if z else
                    ("Meeting records", "Recorded speech and timestamps", "Only reviewed records in this current preview are included")),
        "email": (("邮件往来", "与合作伙伴的沟通", "当前旧版预览尚未纳入邮件；完整补齐前不代表最终覆盖范围") if z else
                  ("Email correspondence", "Partner communications", "Email is not yet in this legacy preview; this is not the final coverage")),
    }
    sources = []
    for source, name in source_names.items():
        head, point, limit = descriptions[source]
        sources.append(dict(id=source, name=name, status="ok" if counts[source] else "planned", head=head,
                            pts=[point], lim=limit if counts[source] else ("当前预览未纳入此来源" if z else "Not included in this preview")))

    chapter_ids = []
    for event in reviewed["events"]:
        chapter = event.get("chapter", "launch")
        if chapter not in chapter_ids:
            chapter_ids.append(chapter)
    act_for = {chapter: f"a{i + 1}" for i, chapter in enumerate(chapter_ids)}
    acts = []
    for chapter in chapter_ids:
        indices = [i for i, event in enumerate(reviewed["events"]) if event.get("chapter", "launch") == chapter]
        start, end = reviewed["events"][indices[0]]["ts"], reviewed["events"][indices[-1]]["ts"]
        acts.append(dict(id=act_for[chapter], name=CHAPTERS[chapter][1 if not z else 0], span=f"{start} – {end}"))

    people = []
    for person in reviewed["people"]:
        people.append(dict(id=person["id"], name=person["name"], short=person["short"], kind="human",
                           color=person["color"], role=person["role"], tag="发言人" if z else "Speaker",
                           accounts="按来源记录核对" if z else "Checked against source records",
                           evidence="原话、发言人及时间已核对" if z else "Original words, speaker, and time verified",
                           conf="高" if z else "High"))

    events = []
    raw_events = {event["id"]: event for event in document["events"]}
    for event in reviewed["events"]:
        raw = raw_events[event["id"]]
        src = source_for[event["id"]]
        original = event.get("original")
        commentary_parts = [event.get("title"), event.get("text")]
        commentary = "\n".join(part for part in commentary_parts if isinstance(part, str) and part.strip())
        source_detail = event.get("source_detail")
        events.append(dict(
            key=event["id"], id=event["id"], ts=event["ts"], src=src, srcs=[src],
            kind="comment" if src in {"github", "hubspot"} else "chat", actor=event["actor"], who=None,
            to=event["to"], action=event["action"], title=event["action"], quote=original["text"] if original else None,
            translation=original["translation"] if original else None,
            translation_label=reviewed["ui"].get("translation"),
            commentary=commentary or None, commentary_label=reviewed["ui"].get("commentary"),
            text=None, meta=[], link=None, ref=source_detail or f"{source_names[src]} · {event['id']}", source_detail=source_detail, ticket=None,
            holders=None, clear=[], stage=event.get("phase"), margin=None, baton=False, compact=False,
            prec="sec", note=None, flag=None, flag_text=None, act=act_for[event.get("chapter", "launch")],
            original=original,
        ))

    verified_count = len(events)
    original_count = sum(event.get("original") is not None for event in events)
    summary = ((f"此页面仅用于核对统一模板，沿用当前 {verified_count} 条旧精选事件，其中 {original_count} 条有逐字原话；邮件等来源尚待补齐，不代表完整资料范围。") if z else
               (f"This page only checks the shared template and reuses {verified_count} legacy curated events, including {original_count} verbatim originals. Email and other sources remain to be added; this is not the complete record."))
    checks = [dict(level="info", title="当前是未发布的模板预览" if z else "Unpublished template preview",
                   text="当前沿用旧版精选数据；邮件及其他来源待补齐后再统一整理。" if z else "This uses the legacy curated data; email and other sources will be unified before selection."),
              dict(level="warn", title="资料范围尚不完整" if z else "Source coverage is incomplete",
                   text="未纳入的来源明确标为待补充；不能据此判断最终内容或业务结论。" if z else "Missing sources are marked as not included; do not treat this as final coverage or a business conclusion.")]
    charter = [dict(t="原话与留档逐条核验" if z else "Originals checked against retained records", ok=original_count == len(events)),
               dict(t="发言人和时间已核对" if z else "Speakers and timestamps verified", ok=original_count == len(events)),
               dict(t="翻译与整理说明分开" if z else "Translations and notes separated", ok=True),
               dict(t="邮件及合作方往来待补" if z else "Partner email still to be added", ok=counts["email"] > 0),
               dict(t="只读预览，尚未发布" if z else "Read-only preview, not published", ok=True)]
    return dict(
        lang=lang, ui=reviewed["ui"],
        meta=dict(no=str(document["campaign_id"]), client="Chime", title=reviewed["meta"]["title"],
                  campaign=document["title"][lang], campaign_id=document["campaign_id"], partner="—", mmp="—",
                  country="—", os="—", edition="统一模板预览" if z else "Shared-template preview", playback=document.get("playback", "narrative")),
        charter=charter, sources=sources, evidence_rows=[[source_names[source], f"{counts[source]} 条已纳入" if z else f"{counts[source]} included"] if counts[source]
                                                        else [source_names[source], "待补充" if z else "Not yet included"] for source in source_names],
        summary=summary, watch=dict(label="未发布预览" if z else "UNPUBLISHED PREVIEW",
                                    head="当前仅用于模板对齐" if z else "Shared-template check only",
                                    text="完成邮件、Slack、会议和工单材料统一后再做精选。" if z else "Selection follows after email, Slack, meeting, and ticket records are unified."),
        people=people, acts=acts, stages=[dict(id=key, name=PHASES[key][1 if not z else 0]) for key in PHASES],
        tickets={}, events=events, checks=checks,
    )


def _json_script(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render(document, lang):
    case = standard_case(document, lang)
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("<head>", '<head>\n<meta name="robots" content="noindex,nofollow">', 1)
    if lang == "en":
        from translate_ui import english_ui
        page = english_ui(page)
    page = page.replace("__CASE_NO__", str(case["meta"]["no"]))
    page = page.replace("__HTML_LANG__", "zh-CN" if lang == "zh" else "en")
    page = page.replace("__CASE_DATA__", _json_script(case))
    page = page.replace("__SCENE_DATA__", _json_script(scene_document(case, lang)))
    if lang == "en" and has_han(page):
        raise ValueError("English page contains Chinese characters")
    return page


def build(source, output):
    source, output = Path(source), Path(output)
    source_bytes = source.read_bytes()
    document = json.loads(source_bytes)
    pages = {lang: render(document, lang) for lang in ("zh", "en")}
    # A release is immutable; neither ordinary refreshes nor this command overwrite it.
    output.mkdir(parents=True, exist_ok=False)
    for lang, page in pages.items():
        (output / f"replay.{lang}.html").write_text(page, encoding="utf-8")
    manifest = dict(version=document["version"], campaign_id=document["campaign_id"], events=len(document["events"]), people=len(document["people"]), source_sha256=hashlib.sha256(source_bytes).hexdigest(), pages={lang: hashlib.sha256(page.encode()).hexdigest() for lang, page in pages.items()})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Manually reviewed bilingual JSON")
    parser.add_argument("--out", required=True, help="New, unpublished release directory")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.out), indent=2))


if __name__ == "__main__":
    main()
