"""Build the self-contained 3D scene document embedded in each replay HTML."""
import json
import re
from .util import ROOT

FEEDOS = ROOT / "src" / "feedos"
VENDOR = ROOT / "src" / "vendor"
ZH_SCENE_TEXT = {
    "Camera presets": "镜头视角", "What the shapes in the scene mean": "场景图形说明",
    "Show or hide the names floating in the scene (L)": "显示或隐藏场景中的名称（L）",
    "Flatten the room into a 2D plan of who read what, or lift it back into 3D (P)": "切换为数据读取关系平面图，或恢复 3D 场景（P）",
    "Click again for the other side": "再次点击切换到另一侧", "Camera: ": "视角：",
    "Scene key": "场景图例", "Data layer": "数据来源", "Event log": "事件记录",
    "Laptop or phone": "笔记本或手机", "Cabinet with a plug": "带插头的柜体",
    "Database drum": "数据库圆桶", "Drum": "数据库圆桶", "Dish": "卫星天线", "Rack": "机柜", "Spire": "事件塔",
    ": a person at work. The phone screen shows the current replay step.": "：同事工作位。手机屏幕显示当前回放步骤。",
    ": an AI agent, with its portrait above.": "：机器人，头像显示在上方。",
    ": an MCP server. Plug seated = connected; plug raised with a padlock = needs authorization.": "：MCP 服务。插头插入表示已连接；插头抬起并带锁表示需要授权。",
    ": a database (read-only).": "：只读数据库。", ": an external API.": "：外部 API。",
    ": the event log. One tile per step and per tool call.": "：事件记录塔。每个步骤和每次系统调用各对应一个方块。",
    "Packets leave the speaker, hit a server (it flashes in the result colour) and land as a tile on the spire. Amber packets carry the proposal to the people who must decide.": "光包从发起人飞向系统（系统会按结果颜色闪烁），再落到事件塔上。琥珀色光包表示事项传递给需要处理的人。",
    "Portrait ring: spinning arc = working, dashed amber = waiting for a decision, red = had blocked calls, violet glow = lesson drafted (a hypothesis, not stored).": "人物环：旋转表示正在处理，琥珀色虚线表示等待，红色表示调用受阻，紫色发光表示已形成建议（尚未保存）。",
    "A replay of one recorded run: nothing here is live, and no write tool was called.": "这是对已记录活动的回放：没有实时操作，也没有调用写入功能。",
    "AI agent": "机器人", "Human": "同事", "Overview": "总览", "People": "人员",
    "Follow speaker": "跟随当前人物", "Follow": "跟随", "Top": "俯视", "Key": "图例", "Labels": "标签", "2D plan": "2D 平面图",
    "STATUS": "状态", "Status": "状态", "Connected": "已连接", "Limited": "受限", "Blocked calls": "受阻调用", "Blocked": "受阻",
    "Needs authorization": "需要授权", "Coming soon": "即将推出", "Planned": "计划中",
    "Working": "处理中", "Waiting": "等待中", "Lesson drafted": "已拟定总结", "Done": "完成",
    "No write tools called": "未调用写入工具", " write tool calls": " 次写入工具调用",
    " write tool call": " 次写入工具调用", "No activity yet": "暂无活动记录", "Standing by": "待命",
    "Activity": "活动", "The council data did not load.": "活动数据未能加载。",
    "The three.js library did not load, so there is no 3D scene here.": "3D 组件未能加载。",
    "This browser did not give the page WebGL.": "当前浏览器未提供 WebGL。",
    "The 3D view is not available here": "3D 视图当前不可用",
    "The rest of the console works; this is the same replay as text.": "其他内容仍可使用；这里展示同一份回放。",
    "Where the data came from": "数据从哪里来", "one line per member and server": "每位成员和系统各一行",
    "servers, one log, people": "系统、事件记录和人员", "Approved": "已批准",
    "Returned with note": "已退回并附说明",
}


def _translate_scene(text: str) -> str:
    for source, target in ZH_SCENE_TEXT.items():
        text = text.replace(source, target)
    return text


def _clean_brand(text: str, lang: str) -> str:
    text = re.sub(r"(?im)^\s*@import\b[^\n]*(?:\n|$)", "", text)
    text = re.sub(r"feed.?os", "Replay", text, flags=re.I)
    if lang == "en":
        text = re.sub(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]+", "", text)
    return text


PRESENTATION_SCENE_CSS = """
.s3d-presentation .s3d-key,.s3d-presentation .s3d-badges,.s3d-presentation .s3d-rchip{display:none!important}
.s3d-presentation .s3d-cap,.s3d-presentation .s3d-cap.has-source{height:auto!important;max-height:min(220px,43%);display:flex;flex-direction:column;pointer-events:auto}
.s3d-presentation .s3d-cd{display:block!important;flex:0 1 auto;min-height:0;white-space:pre-wrap!important;overflow:auto!important;overflow-wrap:anywhere;text-overflow:clip;font-size:13px;line-height:1.5;max-height:none;user-select:text}
.s3d-presentation .s3d-context{flex:0 1 auto;min-height:0;max-height:72px;overflow:auto;margin-top:6px;padding-top:5px;border-top:1px solid rgba(255,255,255,.12);font-size:11px;line-height:1.4;color:#ABA5C0}
.s3d-presentation .s3d-csource{display:none!important}
.s3d-presentation .s3d-cc code{font-family:var(--f-ui,system-ui,sans-serif);font-size:10px}
"""


def scene_document(case: dict, lang: str) -> str:
    """Return an HTML-escaped srcdoc attribute containing all offline scene assets."""
    payload = dict(case)
    payload["lang"] = lang
    data = json.dumps(payload, ensure_ascii=lang == "en", separators=(",", ":")).replace("</", "<\\/")
    assets = {
        "three": (VENDOR / "three.min.js").read_text(encoding="utf-8"),
        "orbit": (VENDOR / "OrbitControls.js").read_text(encoding="utf-8"),
        "adapter": (FEEDOS / "replay-data.js").read_text(encoding="utf-8"),
        "engine": (FEEDOS / "engine.js").read_text(encoding="utf-8"),
        "ui": (FEEDOS / "ui.js").read_text(encoding="utf-8"),
        "scene": (FEEDOS / "views" / "scene3d.js").read_text(encoding="utf-8"),
        "bootstrap": (FEEDOS / "bootstrap.js").read_text(encoding="utf-8"),
    }
    scripts = []
    for name, code in assets.items():
        clean = _clean_brand(code, lang)
        if lang == "zh" and name in {"ui", "scene", "bootstrap"}:
            clean = _translate_scene(clean)
            if name == "scene":
                clean = clean.replace('"Step " +', '"步骤 " +')
                clean = clean.replace('" of " + n', '" / " + n')
                clean = clean.replace('" entries"', '" 条记录"').replace('" entry"', '" 条记录"')
                clean = clean.replace('" calls"', '" 次调用"').replace('" call"', '" 次调用"')
                clean = clean.replace('" limited or blocked"', '" 项受限或受阻"')
                clean = clean.replace('"writes: "', '"写入： "')
        safe_code = clean.replace("</script", "<\\/script")
        scripts.append("<script>" + safe_code + "</script>")
    scripts = "\n".join(scripts)
    css = "\n".join(_clean_brand((FEEDOS / "css" / name).read_text(encoding="utf-8"), lang) for name in ("brand.css", "console.css"))
    if case.get("meta", {}).get("presentation_mode") is True:
        css += "\n" + PRESENTATION_SCENE_CSS
    if case.get("meta", {}).get("playback") == "original":
        css += "\n.s3d .s3d-cap{height:min(220px,43%)!important;pointer-events:auto}.s3d .s3d-cd{display:block!important;white-space:pre-wrap!important;overflow:auto!important;overflow-wrap:anywhere;text-overflow:clip;font-size:14px;line-height:1.7;max-height:calc(100% - 43px);user-select:text}.s3d .s3d-cm{display:block!important}"
    doc = f'''<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{css}\nhtml,body{{width:100%;height:100%;margin:0;overflow:hidden;background:#05060f}}#scene-host{{position:absolute;inset:0;min-width:0;min-height:0}}</style></head><body><div id="scene-host" data-mode="embedded"></div><script>window.REPLAY_CASE={data};</script>{scripts}</body></html>'''
    return doc
