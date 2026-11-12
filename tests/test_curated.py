"""Curated publication is offline, bilingual, verified and separate from automatic data."""
import ast
import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from replay import build as replay_build
from replay import curated
from replay import scene as replay_scene


def fixture():
    return {
        "schema_version": 1, "slug": "chime", "campaign_id": "5023", "version": "v1",
        "title": {"zh": "投放启动", "en": "Campaign launch"},
        "scope": {"zh": "历史记录", "en": "Historical record"},
        "outcome": {"zh": "恢复时间未核实", "en": "Resumption time is unverified"},
        "private_context": "DO_NOT_PUBLISH_PRIVATE_CONTEXT",
        "people": [
            {"id": "lead", "name": "Case Lead", "short": "Lead", "color": "#34d399", "role": {"zh": "投放协调", "en": "Coordination"}, "internal_account": "DO_NOT_PUBLISH_ACCOUNT"},
            {"id": "ops", "name": "Case Ops", "short": "Ops", "color": "#f472b6", "role": {"zh": "投放执行", "en": "Execution"}},
        ],
        "events": [
            {"id": "prepare", "ts": "2026-07-13T10:00:00Z", "actor": "lead", "to": ["ops"], "src": "slack", "phase": "prepare", "action": {"zh": "准备投放", "en": "Prepare launch"}, "title": {"zh": "安排投放", "en": "Arrange launch"}, "text": {"zh": "协调投放准备", "en": "Coordinate campaign preparation"}, "internal_margin": "DO_NOT_PUBLISH_MARGIN", "evidence": {"status": "verified", "ref": "private://DO_NOT_PUBLISH_REFERENCE", "at": "2026-07-13T10:00:00Z", "quote": "DO_NOT_PUBLISH_RAW_QUOTE"}},
            {"id": "pause", "ts": "2026-07-14T10:00:00Z", "actor": "lead", "to": [], "src": "slack", "phase": "pause", "action": {"zh": "暂停投放", "en": "Pause delivery"}, "title": {"zh": "暂时暂停", "en": "Temporary pause"}, "text": {"zh": "等待进一步确认", "en": "Awaiting confirmation"}, "evidence": {"status": "verified", "ref": "private://source", "at": "2026-07-14T10:00:00Z", "quote": "checked"}},
        ],
    }


def original_fixture():
    document = fixture()
    document["playback"] = "original"
    originals = ["Hi Ops. Please check the campaign. Keep this second sentence too.", "ticket对IPM变化进行了检查并给出了结论"]
    for i, event in enumerate(document["events"]):
        text = originals[i]
        event["evidence"]["original_text"] = text
        event["original"] = dict(text=text, at=event["ts"], speaker=event["actor"], speaker_label="Recorded speaker", language="en" if i == 0 else "zh", sha256=hashlib.sha256(text.encode()).hexdigest(), translation={"zh": text if i else "请检查活动。第二句也要保留。", "en": text if not i else "The ticket checked the IPM change and provided a conclusion."})
    return document


class TestCurated(unittest.TestCase):
    def setUp(self):
        self.document = fixture()

    def test_public_allowlist_removes_private_material_in_both_languages(self):
        zh = curated.public_case(self.document, "zh")
        en = curated.public_case(self.document, "en")
        for case in (zh, en):
            encoded = json.dumps(case, ensure_ascii=False)
            self.assertNotIn("DO_NOT_PUBLISH", encoded)
            self.assertNotIn("private://", encoded)
            self.assertEqual(case["meta"]["edition"], "curated")
            self.assertEqual(len(case["people"]), 2)
        identity = lambda case: [(e["id"], e["ts"], e["actor"], e["to"], e["phase"]) for e in case["events"]]
        self.assertEqual(identity(zh), identity(en))
        self.assertEqual(en["events"][1]["phase_name"], "Temporary pause")
        self.assertFalse(curated.has_han(json.dumps(en, ensure_ascii=False)))

    def test_invalid_evidence_people_order_and_translation_are_rejected(self):
        changes = [
            lambda d: d["events"][0]["evidence"].update(status="unverified"),
            lambda d: d["events"][0]["evidence"].pop("quote"),
            lambda d: d["events"][0]["evidence"].update(at="2026-07-13T09:00:00Z"),
            lambda d: d["events"][0].update(actor="unknown"),
            lambda d: d["events"][0].update(to=["unknown"]),
            lambda d: d["events"][0].update(phase="resumed-without-evidence"),
            lambda d: d["events"][0]["title"].update(en="中文"),
            lambda d: d["events"][0]["text"].update(en=""),
            lambda d: d["events"].reverse(),
            lambda d: d["events"][1].update(id=d["events"][0]["id"]),
            lambda d: d.update(version="../../out/5023"),
            lambda d: d["events"][0].update(source_detail={"zh": "Slack", "en": "https://private.example/message"}),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                doc = copy.deepcopy(self.document)
                change(doc)
                with self.assertRaises(ValueError):
                    curated.validate(doc)

    def test_reviewed_narrative_chapters_and_scope_are_localized_without_private_fields(self):
        event = self.document["events"][0]
        event.update(chapter="measurement", scope_level="related", src="meeting", phase="review", excerpt_speaker="ops")
        for field in curated.NARRATIVE:
            event[field] = {"zh": "已核对的业务内容", "en": "Reviewed business content"}
        event["private_notes"] = "DO_NOT_PUBLISH_NARRATIVE_NOTES"
        for lang in ("zh", "en"):
            case = curated.public_case(self.document, lang)
            public = case["events"][0]
            self.assertEqual(public["scope_level"], "related")
            self.assertEqual(public["chapter"], "measurement")
            self.assertEqual(public["excerpt_speaker"], "ops")
            self.assertEqual(public["context"], event["context"][lang])
            self.assertIn("measurement", [c["id"] for c in case["chapters"]])
            self.assertIn("meeting", [s["id"] for s in case["sources"]])
            self.assertNotIn("DO_NOT_PUBLISH", json.dumps(case))
            self.assertNotIn("private_notes", public)
        self.assertEqual(curated.public_case(self.document, "en")["events"][0]["scope_name"], "Related LG campaigns")

    def test_invalid_or_incomplete_narrative_is_rejected(self):
        for field in curated.NARRATIVE:
            self.document["events"][0][field] = {"zh": "业务内容", "en": "Business content"}
        changes = [
            lambda e: e.pop("decision"),
            lambda e: e["excerpt"].update(en="未翻译"),
            lambda e: e.update(excerpt_speaker="unknown"),
            lambda e: e.update(chapter="unknown"),
            lambda e: e.update(scope_level="all_same_campaign"),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                document = copy.deepcopy(self.document)
                change(document["events"][0])
                with self.assertRaises(ValueError):
                    curated.validate(document)

    def test_original_words_are_not_changed_by_interface_language(self):
        document = original_fixture()
        cases = [curated.public_case(document, lang) for lang in ("zh", "en")]
        for case in cases:
            for i, event in enumerate(case["events"]):
                self.assertEqual(event["original"]["text"], document["events"][i]["evidence"]["original_text"])
                self.assertNotEqual(event["original"]["text"], event["text"])
                self.assertNotIn("original_text", event)
                self.assertNotIn("evidence", event)
        self.assertEqual(cases[0]["events"][1]["original"]["text"], cases[1]["events"][1]["original"]["text"])
        page = curated.render(document, "en")
        data = json.loads(re.search(r'<script id="case-data" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(data["events"][1]["original"]["text"], "ticket对IPM变化进行了检查并给出了结论")
        self.assertFalse(curated.has_han(page))

    def test_shared_template_keeps_four_panels_and_separates_original_translation_and_notes(self):
        document = original_fixture()
        document["events"][0]["src"] = "email"
        document["events"][0]["source_detail"] = {"zh": "合作伙伴邮件 · LG投放往来 · 2026-07-13", "en": "Partner email · LG campaign thread · Jul 13, 2026"}
        document["events"][0]["evidence"]["ref"] = "brain:ken-gmail:emails/3869"
        document["events"][1]["src"] = "ticket"
        document["events"][1]["evidence"]["ref"] = "https://github.com/feed-mob/tracking_admin/issues/22851#issuecomment-123"
        case = curated.standard_case(document, "zh")
        self.assertEqual(case["events"][0]["src"], "email")
        self.assertEqual(case["events"][0]["source_detail"], "合作伙伴邮件 · LG投放往来 · 2026-07-13")
        self.assertEqual(case["events"][0]["ref"], case["events"][0]["source_detail"])
        self.assertEqual(case["events"][0]["quote"], document["events"][0]["original"]["text"])
        self.assertEqual(case["events"][0]["translation"], document["events"][0]["original"]["translation"]["zh"])
        self.assertIsNone(case["events"][0]["text"])
        self.assertIn(curated.public_case(document, "zh")["events"][0]["title"], case["events"][0]["commentary"])
        self.assertEqual(case["events"][1]["src"], "github")
        self.assertEqual(case["events"][1]["kind"], "comment")
        self.assertEqual(next(s for s in case["sources"] if s["id"] == "email")["status"], "ok")
        page = curated.render(document, "zh")
        self.assertIn('<aside class="panel roster" id="roster">', page)
        self.assertIn('<aside class="panel evid" id="evid">', page)
        self.assertIn('<aside class="panel insp" id="insp">', page)
        self.assertIn('name="robots" content="noindex,nofollow"', page)
        data = json.loads(re.search(r'<script id="case-data" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(data["events"][0]["quote"], document["events"][0]["original"]["text"])
        self.assertIn("translation", data["events"][0])
        self.assertNotIn("evidence", data["events"][0])

    def test_demo_mode_can_hide_status_banner_without_changing_template_default(self):
        case = curated.standard_case(original_fixture(), "zh")
        default_page = replay_build.render(case, "zh")
        self.assertNotIn("hide_charter", case["meta"])
        self.assertIn("回放状态", default_page)
        case["meta"]["hide_charter"] = True
        page = replay_build.render(case, "zh")
        data = json.loads(re.search(r'<script id="case-data" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertTrue(data["meta"]["hide_charter"])
        self.assertIn("if (M.hide_charter === true)", page)
        self.assertIn(".app.no-charter #charter{display:none}", page)

    def test_presentation_layout_follows_the_original_console(self):
        # 原版：右栏只有人物详情，证据是中栏底部的一条，顶部有一排活动（JOB）切换；左右栏宽度同原版 252 / 330，字体同原版且内嵌
        case = curated.standard_case(original_fixture(), "zh")
        page = replay_build.render(case, "zh")
        self.assertIn('<div class="right-stack">', page)
        self.assertIn('.app.presentation-mode #evid,.app.presentation-mode .right-stack>.resize-handle{display:none}', page)
        self.assertIn('.app.presentation-mode .main{grid-template-columns:252px minmax(0,1fr) 330px}', page)
        self.assertIn('@font-face{font-family:"Lato"', page)
        self.assertIn('@font-face{font-family:"JetBrains Mono"', page)
        self.assertNotIn("fonts.googleapis.com", page)
        self.assertIn('<div class="evstrip" id="evstrip" hidden></div>', page)
        self.assertIn('<span class="picker" id="picker" hidden></span>', page)   # 原版的 JOB 切换在顶栏里：这里是同一广告主的活动编号
        self.assertIn('.top>.picker{flex:1 1 100%}', page)          # 宽屏始终在第二行，不随活动数量挤回第一行
        self.assertIn('.top>.picker{order:2;flex:1 1 0;min-width:200px}', page)  # 保留原有窄屏排布
        self.assertIn("fetch('/campaigns.json'", page)
        # 来源标签保留原版布局；读过但没收录事件的来源也打勾，不与未读混淆
        self.assertIn(".srcs{display:flex;flex-wrap:wrap;", page)
        self.assertNotIn("height:21px;overflow:hidden", page)
        self.assertIn("SRC_IDS.filter(id => !listed.some(s => s.id === id)).map(id => sourceReadChip(id) || noneChip(id))", page)
        # 计数格子和利润率图跟着栏宽走：格子分行、图按真实宽度画，拖动栏宽也会重画
        self.assertIn("function fitMail()", page)
        self.assertIn("new ResizeObserver(fitMail)", page)
        self.assertNotIn("function chartHTML()", page)

    def test_presentation_content_pane_shrinks_to_content_and_scrolls_only_when_needed(self):
        case = curated.standard_case(original_fixture(), "zh")
        page = replay_build.render(case, "zh")
        self.assertIn('.app.presentation-mode .pane.fit-content{flex:0 1 auto;min-height:0}', page)
        self.assertIn("pane.classList.toggle('fit-content', PRESENT && !(S.tab === 'council' && S.view === '3d'))", page)
        self.assertIn('function renderCenterBody()', page)
        self.assertIn('fitPresentationPane();', page)
        self.assertIn('.app.presentation-mode .pane.fit-content .thr{padding-bottom:18px}', page)

    def test_presentation_navigation_cannot_shrink_with_long_content(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                case = curated.standard_case(original_fixture(), lang)
                page = replay_build.render(case, lang)
                self.assertIn('.app.presentation-mode .tabs,.app.presentation-mode .sub{flex-shrink:0}', page)
                self.assertIn('.pane{flex:1;min-height:0;overflow:auto;', page)
                self.assertIn('.app.presentation-mode .pane.fit-content{flex:0 1 auto;min-height:0}', page)

    def test_presentation_3d_caption_shrinks_and_scrolls_long_text_inside_card(self):
        case = curated.standard_case(original_fixture(), "zh")
        case["meta"]["presentation_mode"] = True
        scene = replay_scene.scene_document(case, "zh")
        self.assertIn('.s3d-presentation .s3d-cap,.s3d-presentation .s3d-cap.has-source{height:auto!important;max-height:min(220px,43%);display:flex;flex-direction:column;pointer-events:auto}', scene)
        self.assertIn('.s3d-presentation .s3d-cd{display:block!important;flex:0 1 auto;min-height:0;', scene)
        self.assertIn('overflow:auto!important;overflow-wrap:anywhere;', scene)
        self.assertIn('.s3d-presentation .s3d-context{flex:0 1 auto;min-height:0;max-height:72px;overflow:auto;', scene)

    def test_footer_team_button_opens_people_without_losing_replay_position(self):
        if not shutil.which("node"):
            self.skipTest("Node is needed for the footer navigation check")
        code = r"""
const vm = require('vm'), input = JSON.parse(process.argv[1]);
const calls = [], state = { i: 18, tab: 'council', view: '3d', focus: 'ops', playing: true };
let listener;
const box = { S: state, PRESENT: input.present,
  $(selector) {
    if (selector === '#wait') return { addEventListener(type, fn) { listener = fn; } };
    if (selector === '#tabs [data-t="acc"]') return { focus() { calls.push('focus'); } };
    throw Error('Unexpected selector: ' + selector);
  },
  pause() { state.playing = false; calls.push('pause'); },
  renderCenter() { calls.push('center'); }, updRoster() { calls.push('roster'); },
  writeHash() { calls.push('hash'); }, renderInsp() { calls.push('details'); }
};
vm.runInNewContext(input.listener, box);
listener({ target: { closest(selector) { return input.button && selector === '#det' ? {} : null; } } });
console.log(JSON.stringify({ state, calls }));
"""
        for lang in ("zh", "en"):
            case = curated.standard_case(original_fixture(), lang)
            page = replay_build.render(case, lang)
            match = re.search(r"  \$\('#wait'\)\.addEventListener\('click', e => \{.*?\n  \}\);", page, re.S)
            self.assertIsNotNone(match, "Footer click handler is missing")
            for present, button in ((True, True), (False, True), (True, False)):
                with self.subTest(lang=lang, present=present, button=button):
                    payload = {"listener": match.group(0), "present": present, "button": button}
                    result = subprocess.run(["node", "-e", code, json.dumps(payload)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    actual = json.loads(result.stdout)
                    state = actual["state"]
                    self.assertEqual(state["i"], 18)
                    self.assertEqual(state["view"], "3d")
                    if present and button:
                        self.assertEqual(state["tab"], "acc")
                        self.assertFalse(state["playing"])
                        self.assertIsNone(state["focus"])
                        self.assertEqual(actual["calls"], ["pause", "center", "roster", "hash", "focus", "details"])
                    else:
                        self.assertEqual(state["tab"], "council")
                        self.assertTrue(state["playing"])
                        self.assertEqual(state["focus"], None if button else "ops")
                        self.assertEqual(actual["calls"], ["details"] if button else [])

    def test_summary_speaker_or_time_cannot_replace_verified_original(self):
        changes = [
            lambda e: e["original"].update(text=e["text"]["en"]),
            lambda e: e["original"].update(at="2026-07-13T10:00:01Z"),
            lambda e: e["original"].update(speaker="ops"),
            lambda e: e["original"].update(sha256="incorrect"),
            lambda e: e.pop("original"),
            lambda e: e["original"].update(language="unknown"),
        ]
        for change in changes:
            document = original_fixture()
            change(document["events"][0])
            with self.subTest(change=changes.index(change)), self.assertRaises(ValueError):
                curated.validate(document)

    def test_original_redaction_keeps_words_but_removes_private_addresses(self):
        raw = "Please check <https://private.example/ticket/123|ticket>. Second sentence unchanged. <https://private.example/1|private.example/1> [Files](https://private.example/files) <img src='https://private.example/a.png'>"
        display = curated.display_original(raw)
        self.assertIn("Please check ticket [link omitted]. Second sentence unchanged.", display)
        self.assertIn("Files [link omitted]", display)
        self.assertIn("[attachment]", display)
        self.assertNotIn("private.example", display)
        self.assertNotIn("https://", display)
        self.assertEqual(curated.display_original("ticket对IPM变化进行了检查并给出了结论"), "ticket对IPM变化进行了检查并给出了结论")

    def test_scene_adapter_uses_original_message_not_editorial_title(self):
        if not shutil.which("node"):
            self.skipTest("Node is needed for the scene adapter check")
        document = original_fixture()
        document["events"][0]["source_detail"] = {"zh": "Slack · #chime · 2026-07-13", "en": "Slack · #chime · Jul 13, 2026"}
        case = curated.public_case(document, "en")
        code = r"""
const fs=require('fs'),vm=require('vm'),input=JSON.parse(process.argv[2]);
const box={REPLAY_CASE:input,document:{createElement(){throw Error('Colors must be explicit');}}};
box.window=box;
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),box);
console.log(JSON.stringify(box.REPLAY_SCENE_JOB.steps.map(s=>({quote:s.replayQuote,title:s.replayTitle,label:s.sk,source:s.replaySource}))));
"""
        result = subprocess.run(["node", "-e", code, str(curated.ROOT / "src/feedos/replay-data.js"), json.dumps(case)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = json.loads(result.stdout)
        for i, step in enumerate(steps):
            self.assertEqual(step["quote"], case["events"][i]["original"]["text"])
            self.assertEqual(step["title"], step["quote"])
            self.assertEqual(step["label"], "Original message")
            self.assertNotEqual(step["label"], case["events"][i]["title"])
        self.assertEqual(steps[0]["source"], "Slack · #chime · Jul 13, 2026")

    def test_release_is_offline_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root / "reviewed.json", root / "releases/v1"
            source.write_text(json.dumps(self.document, ensure_ascii=False))
            source_before = source.read_bytes()
            with patch("urllib.request.urlopen", side_effect=AssertionError("No network allowed")):
                manifest = curated.build(source, output)
            self.assertEqual(manifest["events"], 2)
            self.assertEqual(manifest["source_sha256"], hashlib.sha256(source_before).hexdigest())
            pages = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(FileExistsError):
                curated.build(source, output)
            self.assertEqual(pages, {p.name: p.read_bytes() for p in output.iterdir()})
            self.assertEqual(source.read_bytes(), source_before)
            self.assertFalse((root / "out").exists())
            self.assertFalse((root / "raw").exists())
            self.assertFalse((root / "state").exists())

    def test_unverified_document_creates_no_release(self):
        self.document["events"][0]["evidence"]["status"] = "pending"
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "source.json", Path(temp) / "release"
            source.write_text(json.dumps(self.document))
            with self.assertRaises(ValueError):
                curated.build(source, output)
            self.assertFalse(output.exists())

    def test_builder_does_not_import_automatic_collectors(self):
        tree = ast.parse(Path(curated.__file__).read_text())
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        for name in modules:
            self.assertFalse(set(name.split(".")) & {"build", "pipeline", "campaign", "github", "collect_mcp", "events"}, name)

    def test_pause_renders_selected_step_even_when_animation_frames_are_delayed(self):
        if not shutil.which("node"):
            self.skipTest("Node is needed for the delayed-frame playback check")
        bootstrap = curated.ROOT / "src/feedos/bootstrap.js"
        code = r"""
const fs=require('fs'),vm=require('vm');
const timeline={steps:[{start:0,end:1},{start:2,end:3}]};
let last=null;
const view={mount(){},resize(){},setPlaybackQuality(){},update(state){last=state.t;}};
const sandbox={document:{getElementById(){return {};}},parent:{postMessage(){}},
requestAnimationFrame(){return 1;},cancelAnimationFrame(){},performance:{now(){return 0;}},
UI:{SRC_LABEL:{}},REPLAY_SCENE_LABELS:{sourceNames:{}},REPLAY_SCENE_JOB:{},
Council:{buildTimeline(){return timeline;},stateAt(tl,t){return {t};}},
FeedViews:{scene3d:view},addEventListener(){}};
sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
sandbox.setReplayPlayback(3,true);
sandbox.setReplayStep(1);
sandbox.setReplayPlayback(3,false);
console.log(JSON.stringify({last}));
"""
        result = subprocess.run(["node", "-e", code, str(bootstrap)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["last"], 3)

    def test_both_html_documents_are_private_safe_offline_and_valid_javascript(self):
        for lang in ("zh", "en"):
            page = curated.render(self.document, lang)
            self.assertNotIn("DO_NOT_PUBLISH", page)
            self.assertNotIn("private://", page)
            self.assertNotIn("__SCENE_DATA__", page)
            self.assertNotIn("fonts.googleapis.com", page)
            self.assertNotIn("<script src=", page)
            self.assertIn('content="noindex,nofollow"', page)
            case = json.loads(re.search(r'<script id="case-data" type="application/json">(.*?)</script>', page, re.S).group(1))
            scene = json.loads(re.search(r'<script id="scene-data" type="application/json">(.*?)</script>', page, re.S).group(1))
            self.assertEqual(case["events"][0]["action"], self.document["events"][0]["action"][lang])
            self.assertNotIn("DO_NOT_PUBLISH", scene)
            if lang == "en":
                self.assertFalse(curated.has_han(page))
            if not shutil.which("node"):
                continue
            blocks = re.findall(r'<script>(.*?)</script>', page, re.S) + re.findall(r'<script>(.*?)</script>', scene, re.S)
            with tempfile.TemporaryDirectory() as temp:
                for number, block in enumerate(blocks):
                    script = Path(temp) / f"{number}.js"
                    script.write_text(block)
                    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
