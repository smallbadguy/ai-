"""Read-only version sites: /v1, /v2, /v3 each list and serve the pages produced by one generation method.

Configured by <stage>/versions.json (not in git), e.g.
  {"v1": {"label_zh": "...", "label_en": "...", "out": "/path/to/backup/out"},
   "v3": {"label_zh": "...", "label_en": "...", "out": "live", "engine": "pi-online-v3"}}
"out": "live" means the current output directory; "engine" keeps only pages whose business case has that engine.
Nothing here writes data or starts generation.
"""
import html
import json
import os
import pathlib
import re

from replay.util import OUT

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = pathlib.Path(os.environ.get("REPLAY_VERSIONS_FILE") or ROOT.parent / "versions.json")
VENDOR = re.compile(r"(?:[0-9]{1,12}|v-[0-9a-f]{12})\Z")


def load():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if re.fullmatch(r"v\d{1,2}", k) and isinstance(v, dict) and v.get("out")}


def _root(cfg):
    return OUT if cfg["out"] == "live" else pathlib.Path(cfg["out"])


def _meta(folder):
    try:
        case = json.loads((folder / "business.case.zh.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        case = {}
    return case


def scopes(cfg):
    """[(campaign, vendor or None, folder, title)] for every scope with a business page in this version."""
    root, engine, found = _root(cfg), cfg.get("engine"), []
    if not root.is_dir():
        return found
    for camp in sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        folders = [(None, camp)] + [(v.name, v) for v in sorted((camp / "vendors").glob("*")) if v.is_dir() and VENDOR.match(v.name)]
        for vendor, folder in folders:
            if not (folder / "business.zh.html").is_file() and not (folder / "replay.zh.html").is_file():
                continue
            case = _meta(folder)
            if engine and (case.get("meta") or {}).get("engine") != engine:
                continue
            found.append((camp.name, vendor, folder, case.get("title") or case.get("name") or ""))
    return found


def page(cfg, n, vendor, name):
    folder = _root(cfg) / n
    if vendor is not None:
        if not VENDOR.match(vendor):
            return None
        folder = folder / "vendors" / vendor
    if cfg.get("engine") and (_meta(folder).get("meta") or {}).get("engine") != cfg["engine"]:
        return None
    path = folder / name
    return path if path.is_file() else None


def index(version, cfg, all_versions, lang, css="", names=None, channel=None):
    """Version home page in the same look as the main home page (same Bootstrap theme, header, card and table).
    names: {campaign id: name}; channel(n, vendor, lang) -> channel label such as "Vizio #23505"."""
    z = lang == "zh"
    esc = html.escape
    names = names or {}
    label = cfg.get("label_zh" if z else "label_en") or version
    note = cfg.get("note_zh" if z else "note_en") or ""
    tabs = "".join(
        f'<a class="btn btn-outline-secondary{" active" if k == version else ""}" href="/{k}?lang={lang}"'
        f'{" aria-current=page" if k == version else ""}>{esc(k.upper())}</a>'
        for k in sorted(all_versions, key=lambda k: int(k[1:])))
    langs = (f'<a class="btn btn-outline-secondary{" active" if z else ""}" href="/{version}?lang=zh">{"中文" if z else "Chinese"}</a>'
             f'<a class="btn btn-outline-secondary{"" if z else " active"}" href="/{version}?lang=en">English</a>')
    items = []
    for n, vendor, folder, title in scopes(cfg):
        name = (names(n) if callable(names) else names.get(n)) or str(title) or n
        sub = (channel(n, vendor, lang) if channel else "") or (f"#{vendor}" if vendor else "")
        items.append((name.casefold(), sub.casefold(), n, vendor, folder, name, sub))
    items.sort()
    rows = []
    for _, _, n, vendor, folder, name, sub in items:
        q = f"?vendor={vendor}" if vendor else ""

        def edition(stem, suffix):
            if not (folder / f"{stem}.en.html").is_file() and not (folder / f"{stem}.zh.html").is_file():
                return f'<span class="text-body-secondary">{"未生成" if z else "Not available"}</span>'
            parts = []
            for lg, word in (("en", "English"), ("zh", "中文" if z else "Chinese")):
                if (folder / f"{stem}.{lg}.html").is_file():
                    parts.append(f'<a href="/{version}/c/{n}/{lg}{suffix}{q}">{word}</a>')
            return '<span class="sep">·</span>'.join(parts)
        rows.append(f'<tr><td><b>{esc(name)}</b>{f"<small>{esc(sub)}</small>" if sub else ""}</td>'
                    f'<td>{edition("business", "")}</td><td>{edition("replay", "/detail")}</td></tr>')
    empty = f'<tr><td colspan="3" class="empty">{"这个版本还没有回放。" if z else "No replays in this version yet."}</td></tr>'
    count = f"{len(items)} 个回放" if z else f"{len(items)} replays"
    title = "Campaign 回放" if z else "Campaign replay"
    return f"""<!doctype html><html lang="{'zh-CN' if z else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(version.upper())} · {title}</title>
<link rel="stylesheet" href="/static/bootstrap.min.css">
<style>{css}</style></head><body data-bs-theme="dark">
<main class="container py-4" style="max-width:1140px">
<header class="d-flex justify-content-between align-items-start gap-3 mb-4">
  <div class="d-flex align-items-center gap-3"><span class="brand-mark"><i></i></span><div><h1 class="h4 mb-1">{title} · {esc(version.upper())}</h1><p class="text-body-secondary mb-0">{esc(label)}</p></div></div>
  <div class="text-end"><div class="btn-group btn-group-sm" role="group">{tabs}</div> <div class="btn-group btn-group-sm ms-2" role="group">{langs}</div>
  <div class="small text-body-secondary mt-2"><a href="/?lang={lang}">{"返回生成首页" if z else "Back to the build page"}</a></div></div>
</header>
<section class="card"><div class="card-body">
  <div class="d-flex justify-content-between align-items-baseline mb-2"><h2 class="h6 mb-0">{"本版本的回放" if z else "Replays in this version"}</h2><span class="count">{count}</span></div>
  <div class="table-responsive"><table class="table table-hover align-middle mb-0">
    <thead><tr><th>{"活动 / 渠道" if z else "Campaign / channel"}</th><th>{"业务版" if z else "Business"}</th><th>{"详细版" if z else "Detail"}</th></tr></thead>
    <tbody>{''.join(rows) or empty}</tbody></table></div>
</div></section>
<footer class="small text-body-secondary mt-3">{esc(note)}</footer>
</main></body></html>"""
