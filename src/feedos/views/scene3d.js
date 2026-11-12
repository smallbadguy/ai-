/* 3D replay scene (three.js r128). Adapted from the upstream council scene; global FeedViews.scene3d.
 *
 * Contract (docs/console-v8-spec.md section 5): mount(host, ctx), update(state, ctx), resize(), renderNow(), destroy().
 * Every pixel of the scene is a pure function of Council.stateAt(tl, t, decision) plus the camera preset: member rings come from
 * state.mstate, packets from state.steps[i].calls[j].u, tiles from the call status, counters and the gate from state.gate / state.after.
 * Ambient motion (spire turn, LED blink, dust) is derived from state.t, never from a wall clock, so the console and the video capture
 * (window.__renderAt) agree frame for frame. The only wall-clock motion is the camera UI: eased preset transitions, the click highlight
 * and an optional idle auto-orbit, all disabled in capture mode and with reduced motion.
 *
 * host.dataset.mode: "embedded" (toolbar, key, caption) | "capture" (no toolbar; badge legend, caption, scripted camera path). host.dataset.cap = "off" hides the step card.
 * Extra API: setPreset(name,{instant}) with name overview|data|people|top|follow (data twice flips the side); setCameraPath(fn|null) where
 * fn(t, state) returns {pos:[x,y,z], target:[x,y,z], fov?} (overrides the presets and the capture path; null restores them); setAutoOrbit(bool);
 * whenReady() (portraits and fonts loaded: await it before the first capture frame). Optional ctx fields: ctx.decision = {type, at} (lets the
 * approve/return packets time themselves when state.gate.decidedAt is absent), ctx.dim (true = dim everyone but ctx.selected; an array or Set of ids
 * dims those), ctx.autoOrbit = false. Needs data.js, engine.js, ui.js, css/brand.css.
 * Names always come from CAST[id].n and portraits from avatarSrc(id) (people.local.js changes both). */
(function (g) {
  "use strict";
  if (typeof window === "undefined") return;
  const FV = (g.FeedViews = g.FeedViews || {});

  /* ------------------------------------------------------------------ helpers */
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v), lerp = (a, b, k) => a + (b - a) * k;
  const sstep = (a, b, x) => { const k = clamp((x - a) / (b - a), 0, 1); return k * k * (3 - 2 * k); };
  const hexn = (h) => parseInt(String(h).replace("#", ""), 16);
  const bright = (h, k) => { const r = (h >> 16) & 255, gg = (h >> 8) & 255, b = h & 255, m = (c) => Math.round(c + (255 - c) * k); return (m(r) << 16) | (m(gg) << 8) | m(b); };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const sayFn = (s) => (typeof say === "function" ? say(s) : s);
  const strip = (h) => String(h || "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim().replace(/(\bfell\s+|\band\s+)-(\d)/g, "$1$2");
  const firstSentence = (s) => { const m = s.match(/^.*?[.!?](?=\s|$)/); return m ? m[0] : s; };
  const castN = (id) => (typeof CAST !== "undefined" && CAST[id] ? CAST[id].n : id);
  const stWord = (k) => (typeof ST !== "undefined" && ST[k] ? ST[k][1] : k);
  const stIcon = (k) => (typeof ST !== "undefined" && ST[k] ? ST[k][0] : "");
  const joinNames = (ids) => { const n = ids.map(castN); return n.length > 1 ? n.slice(0, -1).join(", ") + " and " + n[n.length - 1] : n[0] || ""; };

  /* brand palette (css/brand.css tokens) */
  const C = {
    bg: 0x05060f, violet: 0x7c5cff, lilac: 0xc4b5fd, blue: 0x3d8bff, good: 0x34d399, warn: 0xfbbf24, serious: 0xf87171, faint: 0x8a83a3,
    metal: 0x35335a, metal2: 0x4b4879, panel: 0x2c2a4a, blade: 0x0a0914, dim: 0x57527a, human: 0x3b4a44
  };
  const STC = { ok: C.good, limited: C.warn, blocked: C.serious, auth: C.blue, soon: C.blue, plan: C.faint, gated: C.lilac, ready: C.lilac };
  const callCol = (r) => STC[r] || C.warn;
  const STATE_WORD = { idle: "", working: "Working", waiting: "Waiting", blocked: "Blocked calls", learned: "Lesson drafted", done: "Done" };
  const LESSON = "Lesson drafted";
  const skLabel = (sk) => (/^(learned|lesson (recorded|filed))$/i.test(String(sk || "")) ? LESSON : sk);
  const noWrites = (n) => (n ? n + (n === 1 ? " write tool call" : " write tool calls") : "No write tools called");
  /** Scripted consequences are shown, not carried out: "Scheduled ..." from older job files reads "Would schedule ... (scripted)". */
  function scripted(h) {
    let s = String(h == null ? "" : h);
    const lead = /^(\s*(?:<[^>]+>\s*)*)Scheduled\b/;
    if (lead.test(s)) s = s.replace(lead, "$1Would schedule");
    if (/\bWould schedule\b/.test(s) && !/scripted/i.test(s)) s = s.replace(/^((?:[^.<]|<[^>]*>)*?[^.\s<>])(\.)(?=\s|<|$)/, "$1 (scripted)$2");
    return s;
  }

  /* data layer: where each source stands in the room; kind decides the shape (cab = MCP server, drum = database, dish = external API) */
  /* Server build: only backends this server drives appear in the room. */
  const SCENE_CONFIG = typeof REPLAY_SCENE !== "undefined" ? REPLAY_SCENE : {};
  const PRESENT = typeof REPLAY_PRESENTATION !== "undefined" && REPLAY_PRESENTATION;
  const SRCS = SCENE_CONFIG.sources || [];
  const SLOT = SCENE_CONFIG.aiIds || AI_IDS.slice();   /* council seats, cast order */
  const HUB_ID = SCENE_CONFIG.hubId || SLOT[0] || ALL[0] || null;
  const PHONE_ID = SCENE_CONFIG.phoneId || HU_IDS.find((id) => CAST[id] && CAST[id].dev === "phone") || null;
  const AGENT_X = SCENE_CONFIG.agentX || SLOT.map((_, i) => {
    const side = i % 2 ? 1 : -1, rank = Math.floor(i / 2) + 1;
    return side * (2.5 + rank * 3.5);
  });
  const HX = SCENE_CONFIG.humanSeats || Object.fromEntries(HU_IDS.map((id, i) => {
    const cols = Math.min(6, HU_IDS.length), row = Math.floor(i / cols), col = i % cols;
    const count = Math.min(cols, HU_IDS.length - row * cols);
    return [id, [(col - (count - 1) / 2) * 5.2, 12 + row * 4.2]];
  }));
  const layoutBounds = (points) => {
    const ps = points.filter((p) => Array.isArray(p) && Number.isFinite(+p[0]) && Number.isFinite(+p[1]));
    if (!ps.length) ps.push([0, 0]);
    const xs = ps.map((p) => +p[0]), zs = ps.map((p) => +p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
    return { x: (minX + maxX) / 2, z: (minZ + maxZ) / 2, ex: (maxX - minX) / 2, ez: (maxZ - minZ) / 2 };
  };
  const ROOM_BOUNDS = layoutBounds([
    ...SLOT.map((_, i) => { const x = AGENT_X[i]; return [x, -9 + 0.035 * x * x]; }),
    ...HU_IDS.map((id) => HX[id]), ...SRCS.map((s) => [s.x, s.z])
  ]);
  const PEOPLE_BOUNDS = layoutBounds(HU_IDS.map((id) => HX[id]));
  const PRESETS = {
    overview: { p: [ROOM_BOUNDS.x, 14.5, ROOM_BOUNDS.z + 34.5], t: [ROOM_BOUNDS.x, 3.5, ROOM_BOUNDS.z], ex: Math.max(21.5, ROOM_BOUNDS.ex + 12), ey: Math.max(7.2, ROOM_BOUNDS.ez + 8) },
    data: { p: [-4, 10, 17], t: [-17, 2.5, -1], ex: 11, ey: 5.2 },
    data2: { p: [4, 10, 17], t: [17, 2.5, -1], ex: 11, ey: 5.2 },
    people: { p: [PEOPLE_BOUNDS.x, 9, PEOPLE_BOUNDS.z + 24], t: [PEOPLE_BOUNDS.x, 4.6, PEOPLE_BOUNDS.z], ex: Math.max(11, PEOPLE_BOUNDS.ex + 7), ey: Math.max(8.4, PEOPLE_BOUNDS.ez + 6) },
    top: { p: [ROOM_BOUNDS.x, 40, ROOM_BOUNDS.z + 7], t: [ROOM_BOUNDS.x, 0, ROOM_BOUNDS.z], ex: Math.max(19, ROOM_BOUNDS.ex + 12), ey: Math.max(14.5, ROOM_BOUNDS.ez + 6) }
  };
  const VIEWS = [["overview", "Overview", "1"], ["data", "Data layer", "2"], ["people", "People", "3"], ["top", "Top", "4"], ["follow", "Follow speaker", "5"]];
  const viewName = (v) => PRESENT && v[0] === "data" ? (REPLAY_LANG === "zh" ? "协作方式" : "Collaboration") : v[1];
  const viewTitle = (v) => PRESENT && v[0] === "data" ? (REPLAY_LANG === "zh" ? "查看协作渠道一侧" : "View collaboration channels") : "Camera: " + v[1] + " (" + v[2] + ")" + (v[0] === "data" ? ". Click again for the other side" : "");
  /* Rise (2D -> 3D). rise 0 is the flat floor plan seen from straight above with a narrow lens (it reads like a 2D who-talked-to-whom graph:
     portraits as discs, floor edges from each member to every server it called, counts on the edges); rise 1 is the 3D room. The camera tilts
     over the first 80% of the rise and the racks, cabinets and the log spire stand up over the last 75%, so the plan visibly lifts into the room. */
  const FLAT = { t: [0, 0, 0.4], ex: 21.8, ez: 15.0, fov: 24 }, BOW = 0.5;   // BOW: edges and packets in the plan bend toward the log, so a line never runs through another member
  const riseCam = (k) => sstep(0, 0.8, k), riseObj = (k) => sstep(0.25, 1, k);
  const RISE_MS = 1900, FLOOR_Y = 0.55;

  const _mc = document.createElement("canvas").getContext("2d"), _tw = new Map();
  /** Width in px of a string in the label fonts (cached; cleared when web fonts finish loading). */
  function tw(str, kind, scale) {
    const k = kind + "|" + scale + "|" + str; let w = _tw.get(k); if (w != null) return w;
    if (kind === "mono") w = str.length * 0.6 * 10 * scale; else { _mc.font = "700 " + ((kind === "src" ? 11.5 : 12) * scale).toFixed(1) + "px Lato, ui-sans-serif, system-ui, sans-serif"; w = _mc.measureText(str).width; }
    _tw.set(k, w); return w;
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => _tw.clear());

  /* ------------------------------------------------------------------ styles (s3d- prefix; tokens from css/brand.css with fallbacks) */
  const CSS = `
.s3d{--u:1;position:relative;width:100%;height:100%;min-height:200px;overflow:hidden;background:#05060F;color:var(--ink,#F4F2FA);font-family:var(--f-ui,"Lato",ui-sans-serif,system-ui,sans-serif);font-size:13px;line-height:1.4;isolation:isolate;-webkit-user-select:none;user-select:none}
.s3d *{box-sizing:border-box}
.s3d-cv{position:absolute;inset:0;touch-action:none;cursor:grab}
.s3d-cv:active{cursor:grabbing}
.s3d-cv:focus-visible{outline:2px solid var(--lilac,#C4B5FD);outline-offset:-3px}
.s3d-cv canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.s3d[data-mode="capture"] .s3d-cv{pointer-events:none;cursor:none}
.s3d-vig{position:absolute;inset:0;pointer-events:none;z-index:1;background:radial-gradient(125% 100% at 50% 44%,transparent 55%,rgba(5,6,15,.72) 100%),linear-gradient(to bottom,rgba(5,6,15,.5),transparent 13%,transparent 76%,rgba(5,6,15,.55))}
.s3d-lbs{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:2}
.s3d-nolabels .s3d-lbs{display:none}
.s3d-lb{position:absolute;left:0;top:0;white-space:nowrap;text-align:center;line-height:1.15;will-change:transform;opacity:0}
.s3d[data-mode="embedded"] .s3d-lb{padding:1px 6px;border-radius:8px;background:rgba(5,6,15,.4)}   /* a flat plate keeps the text readable over bright rings and desks; none in capture, where anti-aliased corners would cost frame-exact pixels */
.s3d-lb b{display:block;font-size:calc(12px*var(--u));font-weight:700;color:var(--ink,#F4F2FA);text-shadow:0 1px 8px #05060F,0 0 3px #05060F,0 0 2px #05060F}
.s3d-lb span{display:block;font-family:var(--f-mono,ui-monospace,Menlo,monospace);font-size:calc(10px*var(--u));color:var(--dim,#ABA5C0);text-shadow:0 1px 6px #05060F,0 0 2px #05060F,0 0 2px #05060F}
.s3d-lb.min span{display:none}
.s3d-lb.lead::after{content:"";position:absolute;left:50%;top:100%;width:1px;height:var(--lead,0px);background:linear-gradient(to bottom,rgba(196,181,253,.7),rgba(196,181,253,.15))}
.s3d-lb.sel b{color:var(--lilac,#C4B5FD)}
.s3d-lb-src b{font-size:calc(11.5px*var(--u))}
.s3d-lb-src.l{text-align:right}.s3d-lb-src.r{text-align:left}
.s3d-lb-log b{color:var(--lilac,#C4B5FD)}
.s3d-dot{display:inline-block;width:calc(8px*var(--u));height:calc(8px*var(--u));margin-right:5px;border-radius:50%;vertical-align:0;background:currentColor;flex:none}
.s3d-dot.ok{color:#34D399}
.s3d-dot.limited{color:#FBBF24;background:linear-gradient(90deg,currentColor 50%,transparent 50%);box-shadow:inset 0 0 0 1.5px currentColor}
.s3d-dot.auth,.s3d-dot.soon{color:#3D8BFF;background:transparent;box-shadow:inset 0 0 0 2px currentColor}
.s3d-dot.blocked{color:#F87171;border-radius:2px}
.s3d-dot.plan{color:#8A83A3;background:transparent;border:1.5px dashed currentColor}
.s3d-pf{position:absolute;left:0;top:0;pointer-events:none;will-change:transform;opacity:0}
.s3d-pf img{display:block;width:100%;height:100%;border-radius:50%;object-fit:cover;background:#171428}
.s3d-pfb{position:absolute;right:-8%;bottom:-8%;width:31%;height:31%;border-radius:50%;display:grid;place-items:center;border:2px solid #fff}
.s3d-pfb.ai{background:#7C5CFF}.s3d-pfb.hu{background:#3B4A44}.s3d-pfb svg{width:62%;height:62%}
/* toolbar */
.s3d-bar{position:absolute;left:10px;right:10px;top:10px;z-index:4;display:flex;align-items:center;gap:8px;pointer-events:none;min-width:0}
.s3d-bar>*{pointer-events:auto}
.s3d[data-mode="capture"] .s3d-bar,.s3d[data-mode="capture"] .s3d-key{display:none}
.s3d-chips{display:flex;gap:2px;padding:3px;border-radius:99px;background:rgba(13,12,25,.74);box-shadow:inset 0 0 0 1px var(--line-2,rgba(255,255,255,.15));-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);overflow-x:auto;scrollbar-width:none;min-width:0;max-width:100%}
.s3d-chips::-webkit-scrollbar{display:none}
.s3d-chip{border:0;background:transparent;color:var(--dim,#ABA5C0);border-radius:99px;padding:5px 12px;font-size:12px;font-weight:700;white-space:nowrap;cursor:pointer;flex:none}
.s3d-chip:hover{color:var(--ink,#F4F2FA)}
.s3d-chip[aria-pressed="true"]{background:var(--violet-bg,rgba(124,92,255,.16));color:var(--lilac,#C4B5FD);box-shadow:inset 0 0 0 1px rgba(196,181,253,.5)}
.s3d-tools{margin-left:auto;display:flex;gap:6px;flex:none}
.s3d-tb{display:inline-flex;align-items:center;gap:6px;border:0;border-radius:99px;padding:6px 12px;font-size:12px;font-weight:700;background:rgba(13,12,25,.74);color:var(--ink,#F4F2FA);box-shadow:inset 0 0 0 1px var(--line-2,rgba(255,255,255,.15));-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);cursor:pointer;white-space:nowrap}
.s3d-tb:hover{background:rgba(31,27,54,.85)}
.s3d-tb[aria-pressed="true"]{color:var(--lilac,#C4B5FD);box-shadow:inset 0 0 0 1px rgba(196,181,253,.5)}
/* key (legend of shapes) */
.s3d-key{position:absolute;top:52px;right:10px;z-index:4;width:min(292px,calc(100% - 20px));max-height:calc(100% - 52px - 112px);overflow:auto;padding:12px 14px 12px;border-radius:14px;background:rgba(13,12,25,.9);box-shadow:inset 0 0 0 1px var(--line-2,rgba(255,255,255,.15)),0 16px 50px rgba(0,0,0,.55);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);scrollbar-width:thin}
.s3d-key[hidden]{display:none}
.s3d-key h3{margin:0 0 8px;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint,#8A83A3);font-weight:700}
.s3d-key ul{list-style:none;margin:0 0 10px;padding:0;display:flex;flex-direction:column;gap:7px;font-size:12px;color:var(--dim,#ABA5C0)}
.s3d-key li{display:grid;grid-template-columns:26px 1fr;gap:9px;align-items:center}
.s3d-key li svg{color:var(--lilac,#C4B5FD);width:26px;height:22px}
.s3d-key li b{color:var(--ink,#F4F2FA);font-weight:700}
.s3d-key .sts{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 8px}
.s3d-key p{margin:0 0 6px;font-size:11.5px;color:var(--dim,#ABA5C0);line-height:1.4}
/* capture badge legend */
.s3d-badges{position:absolute;right:calc(16px*var(--u));top:calc(14px*var(--u));z-index:4;display:none;align-items:center;gap:calc(14px*var(--u));padding:calc(6px*var(--u)) calc(12px*var(--u));border-radius:99px;background:rgba(13,12,25,.7);box-shadow:inset 0 0 0 1px var(--line-2,rgba(255,255,255,.15));font-size:calc(12px*var(--u));color:var(--dim,#ABA5C0);pointer-events:none}
.s3d[data-mode="capture"] .s3d-badges{display:flex;will-change:transform}   /* its own layer: rasterised once, identical in every frame */
.s3d-badges span{display:inline-flex;align-items:center;gap:calc(6px*var(--u))}
.s3d-bdg{display:inline-grid;place-items:center;width:calc(17px*var(--u));height:calc(17px*var(--u));border-radius:50%}
.s3d-bdg svg{width:calc(10px*var(--u));height:calc(10px*var(--u))}
.s3d-bdg.ai{background:#7C5CFF}.s3d-bdg.hu{background:#3B4A44}
/* caption strip */
.s3d-cap{position:absolute;left:50%;transform:translateX(-50%);bottom:10px;z-index:3;width:min(calc(860px*var(--u)),calc(100% - 20px));height:calc(86px*var(--u));padding:calc(9px*var(--u)) calc(14px*var(--u)) calc(8px*var(--u));border-radius:calc(14px*var(--u));background:rgba(13,12,25,.82);box-shadow:inset 0 0 0 1px var(--line-2,rgba(255,255,255,.15)),0 12px 40px rgba(0,0,0,.5);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);pointer-events:none;overflow:hidden}
 .s3d-cap.has-source{height:calc(114px*var(--u))}
.s3d-cr1{display:flex;align-items:center;gap:calc(9px*var(--u));min-width:0;height:calc(30px*var(--u))}
.s3d-avslot{display:inline-flex;flex:none}
.s3d-cr1 .av{width:calc(28px*var(--u));height:calc(28px*var(--u))}
.s3d-cr1 .av .badge{width:calc(13px*var(--u));height:calc(13px*var(--u));right:-3px;bottom:-3px}
.s3d-cr1 .av .badge svg{width:calc(7px*var(--u));height:calc(7px*var(--u))}
.s3d-ct{font-size:calc(15px*var(--u));font-weight:700;white-space:nowrap}
.s3d-cs{font-size:calc(11px*var(--u));font-weight:700;padding:1px calc(9px*var(--u));border-radius:6px;background:var(--violet-bg,rgba(124,92,255,.16));color:var(--lilac,#C4B5FD);white-space:nowrap}
.s3d-cc{display:flex;gap:calc(6px*var(--u));min-width:0;overflow:hidden}
.s3d-cc code{font-family:var(--f-mono,ui-monospace,Menlo,monospace);font-size:calc(10.5px*var(--u));padding:0 calc(6px*var(--u));border-radius:5px;background:rgba(0,0,0,.35);box-shadow:inset 0 0 0 1px var(--line,rgba(255,255,255,.08));white-space:nowrap;line-height:1.7}
.s3d-cc code.ok{color:#34D399}.s3d-cc code.limited{color:#FBBF24}.s3d-cc code.blocked{color:#F87171}.s3d-cc code.running{color:var(--dim,#ABA5C0)}
.s3d-cm{margin-left:auto;font-family:var(--f-mono,ui-monospace,Menlo,monospace);font-size:calc(11px*var(--u));color:var(--faint,#8A83A3);white-space:nowrap;padding-left:calc(8px*var(--u))}
.s3d-cd{margin-top:calc(4px*var(--u));font-size:calc(12.5px*var(--u));color:var(--dim,#ABA5C0);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .s3d-csource{display:flex;align-items:center;gap:calc(7px*var(--u));min-width:0;margin-top:calc(5px*var(--u));font-size:calc(10.5px*var(--u));line-height:1.25;white-space:nowrap;overflow:hidden;color:var(--faint,#8A83A3)}
 .s3d-csource b{flex:none;color:var(--cyan,#22D3EE);font-size:calc(9.5px*var(--u));font-weight:700;letter-spacing:.04em}
 .s3d-csource span{min-width:0;overflow:hidden;text-overflow:ellipsis}
.s3d-cd b{color:var(--ink,#F4F2FA)}
.s3d-dots{display:flex;gap:3px;margin-bottom:calc(5px*var(--u))}
.s3d-dots i{width:calc(14px*var(--u));height:calc(3px*var(--u));border-radius:2px;background:rgba(255,255,255,.15)}
.s3d-dots i.on{background:#7C5CFF}.s3d-dots i.cur{background:#C4B5FD;box-shadow:0 0 8px #7C5CFF}.s3d-dots i.gate{background:#FBBF24}
.s3d[data-mode="capture"] .s3d-cap{width:min(calc(980px*var(--u)),calc(100% - 40px));bottom:calc(12px*var(--u))}
.s3d-narrow .s3d-cc,.s3d-narrow .s3d-cm{display:none}
.s3d-narrow .s3d-cap{height:calc(80px*var(--u))}.s3d-narrow .s3d-cap.has-source{height:calc(116px*var(--u))}
.s3d-narrow .s3d-ct{font-size:calc(13.5px*var(--u))}
.s3d-narrow .s3d-cd{font-size:calc(11.5px*var(--u))}
.s3d-narrow .s3d-chip{padding:5px 10px;font-size:11.5px}
.s3d-narrow .s3d-tb span{display:none}
.s3d-narrow .s3d-lb b{font-size:calc(10.5px*var(--u))}
.s3d-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
/* fallback (no WebGL / three.js missing) */
.s3d-fb{position:absolute;inset:0;z-index:5;overflow:auto;padding:20px 22px;background:#05060F}
.s3d-fb[hidden]{display:none}
.s3d-fb h2{margin:0 0 4px;font-size:16px}
.s3d-fb h3{margin:14px 0 6px}
.s3d-fb p{margin:0 0 12px;color:var(--dim,#ABA5C0);max-width:60ch}
.s3d-fb .fbc{margin:0 0 14px;padding:10px 12px;border-radius:12px;background:var(--surface,#0D0C19);box-shadow:inset 0 0 0 1px var(--line,rgba(255,255,255,.08));font-size:13px}
.s3d-fb ul{list-style:none;margin:0 0 12px;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px 14px}
.s3d-fb li{display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--dim,#ABA5C0)}
.s3d-fb li b{color:var(--ink,#F4F2FA)}
.s3d-fb li i{font-style:normal;margin-left:auto;font-family:var(--f-mono,ui-monospace,Menlo,monospace);font-size:11px;color:var(--faint,#8A83A3)}
/* rise chip: which picture this is, 2D plan or 3D room (shown in capture once a rise path is used, or with ctx.riseChip) */
.s3d-rchip{position:absolute;left:calc(16px*var(--u));top:calc(62px*var(--u));z-index:4;display:none;height:calc(34px*var(--u));min-width:calc(330px*var(--u))}
.s3d-rchip.on{display:block}
.s3d-rchip span{position:absolute;left:0;top:0;display:inline-flex;align-items:center;gap:calc(9px*var(--u));height:100%;padding:0 calc(14px*var(--u)) 0 calc(6px*var(--u));border-radius:99px;background:rgba(13,12,25,.78);box-shadow:inset 0 0 0 1px rgba(196,181,253,.38);font-size:calc(13px*var(--u));font-weight:700;color:var(--ink,#F4F2FA);white-space:nowrap}
.s3d-rchip i{font-style:normal;display:inline-grid;place-items:center;height:calc(24px*var(--u));padding:0 calc(9px*var(--u));border-radius:99px;background:linear-gradient(120deg,#C4B5FD,#7C5CFF 55%,#3D8BFF);color:#fff;font-family:var(--f-mono,ui-monospace,Menlo,monospace);font-size:calc(12px*var(--u));letter-spacing:.04em}
.s3d-rchip em{font-style:normal;color:var(--dim,#ABA5C0);font-weight:400}
.s3d-notb .s3d-bar,.s3d-notb .s3d-key{display:none}
.s3d-nocap .s3d-cap{display:none}
/* capture: no CSS animation anywhere; the working ring of the caption portrait turns with replay time instead (--s3d-ang) */
.s3d[data-mode="capture"] *,.s3d[data-mode="capture"] *::before,.s3d[data-mode="capture"] *::after{animation:none!important;transition:none!important}
.s3d[data-mode="capture"] .av[data-s="working"]::after{transform:rotate(var(--s3d-ang,0deg))}
.s3d[data-mode="capture"] .s3d-lb,.s3d[data-mode="capture"] .s3d-pf{will-change:auto}   /* no compositor layers: text rasterises the same way in every frame */
@media (prefers-reduced-motion:reduce){.s3d *{transition:none!important;animation:none!important}}
`;
  function injectCSS() {
    if (document.getElementById("s3d-css")) return;
    const s = document.createElement("style"); s.id = "s3d-css"; s.textContent = CSS; (document.head || document.documentElement).appendChild(s);
  }

  /* ------------------------------------------------------------------ the legend of shapes */
  const ICON = (p) => `<svg viewBox="0 0 26 22" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
  const IC = {
    laptop: ICON('<rect x="5" y="3" width="16" height="11" rx="1.5"/><path d="M2 18h22"/>'),
    rack: ICON('<rect x="6" y="2" width="14" height="18" rx="1.5"/><path d="M9 6h8M9 10h8M9 14h8"/>'),
    cab: ICON('<rect x="6" y="7" width="14" height="13" rx="1.5"/><path d="M10 7V3M16 7V3M9 3h8"/><circle cx="13" cy="14" r="2" fill="currentColor"/>'),
    drum: ICON('<ellipse cx="13" cy="5" rx="8" ry="2.6"/><path d="M5 5v11c0 1.5 3.6 2.6 8 2.6s8-1.1 8-2.6V5"/><path d="M5 10.5c0 1.5 3.6 2.6 8 2.6s8-1.1 8-2.6"/>'),
    dish: ICON('<path d="M4 6c0 7 5 11 12 10C15 9 11 5 4 6z"/><path d="M12 12l4 6M9 20h8"/>'),
    spire: ICON('<rect x="9" y="2" width="8" height="18" rx="4"/><path d="M9 7h8M9 12h8M9 16h8" opacity=".6"/>')
  };
  function keyHTML() {
    const chip = (k) => (typeof window.chip === "function" ? window.chip(k) : `<span class="st ${k}"><span aria-hidden="true">${esc(stIcon(k))}</span>${esc(stWord(k))}</span>`);
    const L = [
      [IC.laptop, "<b>Laptop or phone</b>: a person at work. The phone screen shows the current replay step."],
      [IC.rack, "<b>Rack</b>: an AI agent, with its portrait above."],
      [IC.cab, "<b>Cabinet with a plug</b>: an MCP server. Plug seated = connected; plug raised with a padlock = needs authorization."],
      [IC.drum, "<b>Drum</b>: a database (read-only)."],
      [IC.dish, "<b>Dish</b>: an external API."],
      [IC.spire, "<b>Spire</b>: the event log. One tile per step and per tool call."]
    ];
    return `<h3>Scene key</h3><ul>${L.map((l) => `<li>${l[0]}<span>${l[1]}</span></li>`).join("")}</ul>
<h3>Status</h3><div class="sts">${["ok", "limited", "blocked", "auth", "plan"].map(chip).join("")}</div>
<p>Packets leave the speaker, hit a server (it flashes in the result colour) and land as a tile on the spire. Amber packets carry the proposal to the people who must decide.</p>
<p>Portrait ring: spinning arc = working, dashed amber = waiting for a decision, red = had blocked calls, violet glow = lesson drafted (a hypothesis, not stored).</p>
<p>A replay of one recorded run: nothing here is live, and no write tool was called.</p>`;
  }
  const AISVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .8l1.1 3.1 3.1 1.1-3.1 1.1L6 9.2 4.9 6.1 1.8 5l3.1-1.1z" fill="#fff"/></svg>';
  const HUSVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="4" r="2.1" fill="#fff"/><path d="M2 10.6c0-2.2 1.8-3.5 4-3.5s4 1.3 4 3.5z" fill="#fff"/></svg>';

  /* ------------------------------------------------------------------ camera path for capture: pure function of (t, state) */
  const POSES = {
    overview: { p: [0, 17, 43], t: [0, 3.4, 0] },
    overview2: { p: [3, 14, 39], t: [0, 3.4, 1] },
    left: { p: [4, 12.5, 33], t: [-10, 3.2, -2] },
    left2: { p: [7, 11.8, 32], t: [-8.5, 3.2, -2] },
    right: { p: [-4, 12.5, 33], t: [10, 3.2, -2] },
    right2: { p: [-7, 11.8, 32], t: [8.5, 3.2, -2] },
    people: { p: [3, 10.5, 34], t: [0, 4.2, 5] },
    people2: { p: [1.5, 10.2, 33], t: [0, 4.3, 6] },
    wide: { p: [0, 13, 38], t: [0, 3.4, 2] }
  };
  function hermite(keys, t, out) {
    const n = keys.length;
    if (n === 1) { for (let c = 0; c < 6; c++) out[c] = keys[0].v[c]; return out; }
    if (t <= keys[0].t) { for (let c = 0; c < 6; c++) out[c] = keys[0].v[c]; return out; }
    if (t >= keys[n - 1].t) { for (let c = 0; c < 6; c++) out[c] = keys[n - 1].v[c]; return out; }
    let k = 0; while (k < n - 2 && t > keys[k + 1].t) k++;
    const k0 = keys[Math.max(0, k - 1)], k1 = keys[k], k2 = keys[k + 1], k3 = keys[Math.min(n - 1, k + 2)];
    const h = k2.t - k1.t, u = (t - k1.t) / h, u2 = u * u, u3 = u2 * u;
    const h00 = 2 * u3 - 3 * u2 + 1, h10 = u3 - 2 * u2 + u, h01 = -2 * u3 + 3 * u2, h11 = u3 - u2;
    for (let c = 0; c < 6; c++) {
      const m1 = k === 0 ? 0 : (k2.v[c] - k0.v[c]) / (k2.t - k0.t);
      const m2 = k + 1 >= n - 1 ? 0 : (k3.v[c] - k1.v[c]) / (k3.t - k1.t);
      out[c] = h00 * k1.v[c] + h10 * h * m1 + h01 * k2.v[c] + h11 * h * m2;
    }
    return out;
  }
  const poseV = (name) => POSES[name].p.concat(POSES[name].t);
  /** Key frames for the scripted camera: overview, then the side of the data layer each step reads from, then the people at the gate. */
  function pathKeys(tl, state) {
    const G = tl.gateAt != null ? tl.gateAt : tl.duration;
    const sideOf = (s) => { const o = SRCS.find((x) => x.k === s); return !o ? 0 : o.x < -1 ? -1 : o.x > 1 ? 1 : 0; };
    const runs = [];
    const overlapsOther = (S) => tl.steps.some((O) => O !== S && S.start < O.end && O.start < S.end);
    tl.steps.forEach((S) => {
      let sd = 0;
      if (overlapsOther(S)) sd = 0;
      else if (S.calls.length) { const ss = S.calls.map((c) => sideOf(c.s)), L = ss.filter((v) => v < 0).length, R = ss.filter((v) => v > 0).length; sd = L && !R ? -1 : R && !L ? 1 : 0; }
      const last = runs[runs.length - 1];
      if (last && last.side === sd) last.end = S.end; else runs.push({ side: sd, start: S.start, end: S.end });
    });
    const keys = [{ t: 0, v: poseV("overview") }];
    let lastT = 0;
    const add = (t, name) => { if (t < lastT + 0.35) return; keys.push({ t, v: poseV(name) }); lastT = t; };
    runs.forEach((r, i) => {
      if (r.end > G - 0.2) return;
      if (r.side === 0) { if (r.end - r.start > 2.6 && i > 0) { add(r.start + 0.5, "overview2"); } return; }
      const nm = r.side < 0 ? "left" : "right";
      add(r.start + 0.55, nm);
      if (r.end - r.start > 4) add(r.end - 0.5, nm + "2");
    });
    add(Math.max(lastT + 0.4, G - 3.2), "overview2");
    add(Math.max(lastT + 0.4, G - 0.6), "people");
    const A = state && state.after && state.after.length ? state.after[0].start : G + 2.5;
    add(Math.max(lastT + 0.4, A - 0.9), "people2");
    add(Math.max(lastT + 0.4, A + 1.8), "wide");
    add(lastT + 40, "wide");
    return keys;
  }

  /* ------------------------------------------------------------------ one mounted instance */
  let cur = null;

  function createInstance(host, ctx0) {
    injectCSS();
    let ctx = ctx0 || {};
    let state = null, builtTl = null, destroyed = false;
    let W = 0, H = 0, U = 1, narrow = false, mode = "embedded", reduced = false, playbackSpeed = 1, playbackActive = false;
    let G3 = null;            // three.js graph (null when WebGL is unavailable)
    let fbReason = "";
    let preset = "overview", dataSide = "data", labelsOff = false, keyOpen = false, keyTouched = false;
    let customPath = null, autoOrbit = true;
    let riseK = 1, riseTw = null, riseUsed = false, curRise = 1;   // 1 = 3D room, 0 = flat 2D plan; riseTw animates it (embedded only)
    let hoverId = null, hlP = -1, hlId = null;
    let raf = 0, tween = null, lastInteract = 0, orbit = null, lastTick = 0, followId = null, idleTimer = 0;
    let inRender = false, settingPose = false;
    const readyPromises = [];

    /* ----- DOM ----- */
    const root = el("div", "s3d"); root.setAttribute("data-mode", "embedded"); if (PRESENT) root.classList.add("s3d-presentation");
    const cv = el("div", "s3d-cv"); cv.tabIndex = 0; cv.setAttribute("role", "img");
    const sceneLabel = PRESENT
       ? (REPLAY_LANG === "zh" ? `3D 场景：${HU_IDS.length} 位参与者与 ${SRCS.length} 类协作方式。按 1 至 5 切换视角，按方向键旋转。` : `3D scene: ${HU_IDS.length} participants and ${SRCS.length} collaboration channels. Use 1 to 5 for camera presets and the arrow keys to turn the view.`)
       : typeof REPLAY_LANG === "string" && REPLAY_LANG === "zh"
         ? `3D 场景：${AI_IDS.length} 个机器人机柜围绕事件塔，前方有 ${HU_IDS.length} 位同事，两侧有 ${SRCS.length} 个数据来源。按 1 至 5 切换视角，按方向键旋转。`
         : `3D scene: ${AI_IDS.length} bots on racks around the event log, ${HU_IDS.length} people at desks in front and ${SRCS.length} data sources on both sides. Use 1 to 5 for camera presets and the arrow keys to turn the view.`;
     cv.setAttribute("aria-label", sceneLabel);
    const vig = el("div", "s3d-vig"); vig.setAttribute("aria-hidden", "true");
    const lbs = el("div", "s3d-lbs"); lbs.setAttribute("aria-hidden", "true");
    const bar = el("div", "s3d-bar");
    const chips = el("div", "s3d-chips"); chips.setAttribute("role", "toolbar"); chips.setAttribute("aria-label", "Camera presets");
    VIEWS.forEach((v) => { const b = el("button", "s3d-chip", esc(viewName(v))); b.type = "button"; b.dataset.view = v[0]; b.title = viewTitle(v); b.setAttribute("aria-pressed", "false"); chips.appendChild(b); });
    const tools = el("div", "s3d-tools");
    const bKey = el("button", "s3d-tb", '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M6.2 6.2a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.6-.9 1.3M8 11.6v.1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg><span>Key</span>');
    bKey.type = "button"; bKey.setAttribute("aria-pressed", "false"); bKey.title = "What the shapes in the scene mean";
    const bLab = el("button", "s3d-tb", '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3h12v7H8l-3 3v-3H2z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg><span>Labels</span>');
    bLab.type = "button"; bLab.setAttribute("aria-pressed", "true"); bLab.title = "Show or hide the names floating in the scene (L)";
    const bFlat = el("button", "s3d-tb", '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 5.5 8 2.5l6 3-6 3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M2 9l6 3 6-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg><span>2D plan</span>');
    bFlat.type = "button"; bFlat.setAttribute("aria-pressed", "false"); bFlat.title = "Flatten the room into a 2D plan of who read what, or lift it back into 3D (P)";
    tools.appendChild(bFlat); tools.appendChild(bKey); tools.appendChild(bLab);
    if (PRESENT) { bFlat.hidden = true; bKey.hidden = true; }
    bar.appendChild(chips); bar.appendChild(tools);
    const key = el("aside", "s3d-key", keyHTML()); key.hidden = true; key.setAttribute("aria-label", "Scene key");
    const badges = el("div", "s3d-badges", `<span><i class="s3d-bdg ai">${AISVG}</i>AI agent</span><span><i class="s3d-bdg hu">${HUSVG}</i>Human</span>`); if (PRESENT) badges.hidden = true;
    const cap = el("div", "s3d-cap"); cap.setAttribute("aria-hidden", "true");
    const sr = el("div", "s3d-sr"); sr.setAttribute("role", "status"); sr.setAttribute("aria-live", "polite");
    const fb = el("section", "s3d-fb"); fb.hidden = true; fb.setAttribute("role", "region"); fb.setAttribute("aria-label", "3D view unavailable");
    const rchip = el("div", "s3d-rchip", '<span class="r2"><i>2D</i>Who read what <em>· one line per member and server</em></span><span class="r3"><i>3D</i>Where the data came from <em>· servers, one log, people</em></span>'); rchip.setAttribute("aria-hidden", "true"); if (PRESENT) rchip.hidden = true;
    const r2 = rchip.firstChild, r3 = rchip.lastChild;
    [cv, vig, lbs, bar, key, badges, rchip, cap, sr, fb].forEach((n) => root.appendChild(n));
    host.appendChild(root);

    const modeNow = () => (host.dataset.mode === "capture" || ctx.capture ? "capture" : "embedded");
    const isCapture = () => mode === "capture";
    const renderPixelRatio = () => Math.min(window.devicePixelRatio || 1, playbackActive && playbackSpeed >= 3 ? 1.25 : playbackActive && playbackSpeed >= 2 ? 1.5 : 2);
    function setPlaybackQuality(speed, playing) {
      playbackSpeed = clamp(Number(speed) || 1, 1, 3); playbackActive = !!playing;
      if (!G3 || isCapture()) return;
      const ratio = renderPixelRatio();
      if (Math.abs(G3.renderer.getPixelRatio() - ratio) > 0.01) { G3.renderer.setPixelRatio(ratio); G3.renderer.setSize(W, H, false); drawNow(); }
    }

    /* ----- three.js availability ----- */
    function glOK() {
      try { const c = document.createElement("canvas"); const gl = c.getContext("webgl2") || c.getContext("webgl"); if (!gl) return false; const x = gl.getExtension("WEBGL_lose_context"); if (x) x.loseContext(); return true; } catch (e) { return false; }
    }
    if (typeof THREE === "undefined") fbReason = "The three.js library did not load, so there is no 3D scene here.";
    else if (typeof CAST === "undefined" || typeof Council === "undefined") fbReason = "The council data did not load.";
    else if (!glOK()) fbReason = "This browser did not give the page WebGL.";
    else { try { G3 = buildGraph(); } catch (err) { G3 = null; fbReason = "The 3D scene could not start (" + (err && err.message ? err.message : "WebGL error") + ")."; if (typeof console !== "undefined") console.warn("[scene3d]", err); } }

    /* ================================================================== the 3D graph */
    function buildGraph() {
      const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, powerPreference: "high-performance" });
      renderer.setPixelRatio(renderPixelRatio());
      renderer.setClearColor(C.bg, 1);
      cv.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      scene.background = new THREE.Color(C.bg);
      scene.fog = new THREE.FogExp2(C.bg, 0.0135);
      const cam = new THREE.PerspectiveCamera(40, 1, 0.1, 260);
      const ctl = (typeof THREE.OrbitControls === "function") ? new THREE.OrbitControls(cam, renderer.domElement) : null;
      if (ctl) { ctl.enableDamping = false; ctl.maxPolarAngle = Math.PI * 0.495; ctl.minDistance = 7; ctl.maxDistance = 78; ctl.screenSpacePanning = false; ctl.rotateSpeed = 0.7; }
      const V = (x, y, z) => new THREE.Vector3(x, y, z);
      const glowMats = [];

      /* lights: violet rim, cool key, warm lilac fill on the people */
      scene.add(new THREE.HemisphereLight(0xb8afff, 0x14122a, 0.8));
      const key1 = new THREE.DirectionalLight(0xffffff, 1.0); key1.position.set(12, 26, 20); scene.add(key1);
      const rim = new THREE.DirectionalLight(C.violet, 0.9); rim.position.set(-18, 12, -22); scene.add(rim);
      const warm = new THREE.PointLight(0xe6deff, 0.5, 40, 2); warm.position.set(0, 7, 17); scene.add(warm);
      const spireLight = new THREE.PointLight(C.violet, 1.4, 34, 2); spireLight.position.set(0, 5.5, 0); scene.add(spireLight);

      const M = {
        metal: new THREE.MeshStandardMaterial({ color: C.metal, roughness: 0.42, metalness: 0.55 }),
        metal2: new THREE.MeshStandardMaterial({ color: C.metal2, roughness: 0.5, metalness: 0.45 }),
        panel: new THREE.MeshStandardMaterial({ color: C.panel, roughness: 0.6, metalness: 0.3 }),
        blade: new THREE.MeshStandardMaterial({ color: C.blade, roughness: 0.7, metalness: 0.2 }),
        dim: new THREE.MeshStandardMaterial({ color: C.dim, roughness: 0.7, metalness: 0.2 }),
        glass: new THREE.MeshStandardMaterial({ color: C.violet, transparent: true, opacity: 0.1, roughness: 0.08, metalness: 0.1, side: THREE.DoubleSide, depthWrite: false, envMapIntensity: 1.6 })
      };

      /* procedural textures (canvas): no network, no images */
      const cTex = (w, h, draw) => { const c = document.createElement("canvas"); c.width = w; c.height = h; draw(c.getContext("2d"), w, h); const t = new THREE.CanvasTexture(c); t.anisotropy = 4; return t; };
      const radial = (stops) => cTex(128, 128, (g2) => { const r = g2.createRadialGradient(64, 64, 0, 64, 64, 64); stops.forEach((s) => r.addColorStop(s[0], s[1])); g2.fillStyle = r; g2.fillRect(0, 0, 128, 128); });
      const glowTex = radial([[0, "rgba(255,255,255,1)"], [0.25, "rgba(255,255,255,.65)"], [0.6, "rgba(255,255,255,.12)"], [1, "rgba(255,255,255,0)"]]);
      const orbTex = radial([[0, "rgba(255,255,255,1)"], [0.22, "rgba(255,255,255,1)"], [0.34, "rgba(255,255,255,.55)"], [0.6, "rgba(255,255,255,.1)"], [1, "rgba(255,255,255,0)"]]);
      const blobTex = radial([[0, "rgba(0,0,0,.85)"], [0.5, "rgba(0,0,0,.4)"], [1, "rgba(0,0,0,0)"]]);
      const ringTex = cTex(256, 256, (g2) => { g2.strokeStyle = "#fff"; g2.lineWidth = 11; g2.beginPath(); g2.arc(128, 128, 118, 0, Math.PI * 2); g2.stroke(); });
      const dashTex = cTex(256, 256, (g2) => { g2.strokeStyle = "#fff"; g2.lineWidth = 11; g2.setLineDash([26, 17]); g2.beginPath(); g2.arc(128, 128, 118, 0, Math.PI * 2); g2.stroke(); });
      const thinTex = cTex(256, 256, (g2) => { g2.strokeStyle = "#fff"; g2.lineWidth = 4; g2.beginPath(); g2.arc(128, 128, 122, 0, Math.PI * 2); g2.stroke(); });
      const arcTex = cTex(256, 256, (g2) => { for (let i = 0; i < 48; i++) { g2.strokeStyle = `rgba(255,255,255,${Math.pow(i / 47, 1.6)})`; g2.lineWidth = 15; g2.lineCap = "round"; g2.beginPath(); g2.arc(128, 128, 118, i / 48 * Math.PI * 1.45, (i + 1.6) / 48 * Math.PI * 1.45); g2.stroke(); } });
      /* environment: a dark studio with violet soft boxes, so metal and glass have something to reflect */
      (function () {
        const c = document.createElement("canvas"); c.width = 512; c.height = 256; const g2 = c.getContext("2d");
        const gr = g2.createLinearGradient(0, 0, 0, 256); gr.addColorStop(0, "#3a3470"); gr.addColorStop(0.46, "#16142a"); gr.addColorStop(0.5, "#0b0a18"); gr.addColorStop(1, "#040408"); g2.fillStyle = gr; g2.fillRect(0, 0, 512, 256);
        [[40, 34, 150, 34, "#ffffff", 0.95], [260, 22, 190, 26, "#dcd6ff", 0.85], [120, 86, 110, 14, "#c4b5fd", 0.7], [400, 70, 80, 50, "#ffe6c4", 0.55], [0, 60, 512, 3, "#9d8cff", 0.4]].forEach((b) => { g2.globalAlpha = b[5]; g2.fillStyle = b[4]; g2.fillRect(b[0], b[1], b[2], b[3]); });
        const tex = new THREE.CanvasTexture(c); tex.mapping = THREE.EquirectangularReflectionMapping;
        try { const pm = new THREE.PMREMGenerator(renderer); scene.environment = pm.fromEquirectangular(tex).texture; pm.dispose(); } catch (e) { /* the scene still renders without reflections */ }
      })();

      /* floor: faded violet grid, ring marks, soft reflection of everything above it */
      const floorMat = new THREE.ShaderMaterial({
        transparent: true, depthWrite: false, extensions: { derivatives: true },
        uniforms: { uBase: { value: new THREE.Vector3(0.016, 0.014, 0.046) }, uLine: { value: new THREE.Vector3(0.2, 0.17, 0.5) }, uAcc: { value: new THREE.Vector3(0.52, 0.4, 1.0) }, uA: { value: 0.9 }, uT: { value: 0 }, uRing: { value: 9.8 }, uFlat: { value: 0 } },
        vertexShader: "varying vec2 vP;void main(){vec4 w=modelMatrix*vec4(position,1.);vP=w.xz;gl_Position=projectionMatrix*viewMatrix*w;}",
        fragmentShader: "precision highp float;varying vec2 vP;uniform vec3 uBase,uLine,uAcc;uniform float uA,uT,uRing,uFlat;" +
          "float grid(vec2 p,float s,float w){vec2 q=abs(fract(p/s-.5)-.5)/max(fwidth(p/s),vec2(1e-4));return 1.-min(min(q.x,q.y)/w,1.);}" +
          "float band(float r,float c,float w){return 1.-smoothstep(0.,w,abs(r-c));}" +
          "void main(){float r=length(vP);float fade=1.-smoothstep(12.,50.,r);" +
          "float g1=grid(vP,2.,1.1),g2=grid(vP,10.,1.5);" +
          "float rings=band(r,uRing,.12)*.9+band(r,uRing+.9,.05)*.4+band(r,16.4,.06)*.5+band(r,3.1,.08)*.8+band(r,25.,.05)*.25;" +
          "float ph=mod(uT*2.4,44.);float pulse=exp(-pow((r-ph)*.7,2.))*.5*(1.-smoothstep(10.,40.,r));" +
          "float core=exp(-r*r*.012);" +
          "float fl=1.-.6*uFlat;vec3 col=uBase+uLine*(g1*.4+g2*.55)*fade+uAcc*(rings*(.55+.4*fade)*fl+pulse*fl+core*.35*(1.-.75*uFlat));" +
          "float a=uA*(.38+.62*(1.-smoothstep(14.,52.,r)));a=clamp(a+(g1*.4+g2*.5)*fade*.12+rings*.18+core*.1,0.,1.);gl_FragColor=vec4(col,a);}"
      });
      const floor = new THREE.Mesh(new THREE.PlaneGeometry(170, 170), floorMat); floor.rotation.x = -Math.PI / 2; floor.renderOrder = 1; scene.add(floor);
      const refl = new THREE.Group(); refl.scale.set(1, -1, 1); scene.add(refl);
      const risers = [];   // everything that stands on the floor: flattened toward the floor plan when rise < 1
      const place = (o) => { scene.add(o); refl.add(o.clone(true)); risers.push(o); return o; };

      /* small builders */
      const gcache = {};   // identical shapes share one geometry (the racks and cabinets repeat many parts)
      const gbox = (w, h, d) => gcache["b" + w + "," + h + "," + d] || (gcache["b" + w + "," + h + "," + d] = new THREE.BoxGeometry(w, h, d));
      const gcyl = (rt, rb, h, s) => gcache["c" + rt + "," + rb + "," + h + "," + s] || (gcache["c" + rt + "," + rb + "," + h + "," + s] = new THREE.CylinderGeometry(rt, rb, h, s));
      const box = (w, h, d, m, x, y, z) => { const o = new THREE.Mesh(gbox(w, h, d), m); o.position.set(x || 0, y || 0, z || 0); return o; };
      const cyl = (rt, rb, h, s, m, x, y, z) => { const o = new THREE.Mesh(gcyl(rt, rb, h, s), m); o.position.set(x || 0, y || 0, z || 0); return o; };
      const bas = (c, o) => new THREE.MeshBasicMaterial(Object.assign({ color: c }, o || {}));
      const lineEdges = (mesh, col, op) => { const l = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: op == null ? 0.5 : op })); mesh.add(l); return l; };
      const glowMat = (p) => { const m = new THREE.MeshBasicMaterial(Object.assign({ transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, fog: false }, p)); glowMats.push(m); return m; };
      const decal = (x, z, r, color, op) => { const m = glowMat({ map: glowTex, color, opacity: op }); const o = new THREE.Mesh(new THREE.PlaneGeometry(r * 2, r * 2), m); o.rotation.x = -Math.PI / 2; o.position.set(x, 0.03, z); o.renderOrder = 2; scene.add(o); return { mesh: o, mat: m, base: op }; };
      const blob = (x, z, r, op) => { const m = new THREE.MeshBasicMaterial({ map: blobTex, transparent: true, opacity: op, depthWrite: false, fog: false }); const o = new THREE.Mesh(new THREE.PlaneGeometry(r * 2, r * 2), m); o.rotation.x = -Math.PI / 2; o.position.set(x, 0.025, z); o.renderOrder = 2; scene.add(o); return o; };
      const halo = (pos, color, size) => { const m = new THREE.SpriteMaterial({ map: glowTex, color, transparent: true, depthWrite: false, fog: false, blending: THREE.AdditiveBlending, opacity: 0.6 }); const s = new THREE.Sprite(m); s.position.copy(pos); s.scale.set(size, size, 1); s.renderOrder = 6; scene.add(s); return { s, m, size }; };
      const conduits = [];   // floor spokes to the log: brighter in the flat plan, where they are the graph's hub edges
      const conduit = (ax, az, bx, bz, col, op) => {
        const dx = bx - ax, dz = bz - az, len = Math.hypot(dx, dz);
        const o = new THREE.Mesh(new THREE.BoxGeometry(len, 0.01, 0.07), glowMat({ color: col, opacity: op, fog: true })); o.position.set((ax + bx) / 2, 0.045, (az + bz) / 2); o.rotation.y = -Math.atan2(dz, dx); o.renderOrder = 2; scene.add(o);
        const w = new THREE.Mesh(new THREE.BoxGeometry(len, 0.01, 0.5), glowMat({ color: col, opacity: op * 0.18, fog: true })); w.position.copy(o.position); w.rotation.copy(o.rotation); w.renderOrder = 2; scene.add(w);
        conduits.push({ m: o.material, base: op }, { m: w.material, base: op * 0.18 });
      };

      /* ---- labels (HTML overlay) ---- */
      const labels = [];
      function label(kind, id, pos, off, pri) {
        const e = el("div", "s3d-lb s3d-lb-" + kind); lbs.appendChild(e);
        const o = { el: e, kind, id, pos: pos.clone(), off: off.clone(), pri, html: "", nm: "", sb: "", dot: false, h1: 28, h0: 14, sx: 0, sy: 0, vis: false, min: false, tx: "", op: -1, sel: false, ax: kind === "src" ? (pos.x < 0 ? -1 : 1) : 0 };
        labels.push(o); return o;
      }
      function setLabel(o, name, sub, dot) {
        const lines = Array.isArray(sub) ? sub : sub ? [sub] : [];
        const html = `<b>${dot ? `<i class="s3d-dot ${dot}"></i>` : ""}${esc(name)}</b>${lines.map((x) => `<span>${esc(x)}</span>`).join("")}`;
        if (o.html === html) return; o.html = html; o.el.innerHTML = html;
        o.nm = name; o.sb = lines.reduce((a, x) => (x.length > a.length ? x : a), ""); o.dot = !!dot; o.h0 = 15; o.h1 = 15 + 13 * lines.length;
      }

      /* ---- spire: the event log ---- */
      const spire = new THREE.Group(), spireH = 9.4;
      const foot = () => { const f = new THREE.Group(); f.add(cyl(2.7, 2.9, 0.32, 48, M.metal2, 0, 0.16, 0)); const br = new THREE.Mesh(new THREE.TorusGeometry(2.45, 0.05, 8, 72), bas(C.violet)); br.rotation.x = Math.PI / 2; br.position.y = 0.34; f.add(br); return f; };
      spire.add(foot());
      const glassCyl = new THREE.Mesh(new THREE.CylinderGeometry(1.78, 1.78, spireH - 0.4, 56, 1, true), M.glass); glassCyl.position.y = spireH / 2 + 0.1; spire.add(glassCyl);
      const ribMat = new THREE.MeshStandardMaterial({ color: C.metal2, roughness: 0.3, metalness: 0.7 });
      for (let i = 0; i < 12; i++) { const a = i / 12 * Math.PI * 2; spire.add(box(0.05, spireH - 0.2, 0.05, ribMat, Math.cos(a) * 1.8, spireH / 2 + 0.1, Math.sin(a) * 1.8)); }
      const ringMat = bas(C.lilac, { transparent: true, opacity: 0.75 });
      [0.6, 3.2, 6.0, spireH - 0.1].forEach((y) => { const r = new THREE.Mesh(new THREE.TorusGeometry(1.8, 0.035, 6, 64), ringMat); r.rotation.x = Math.PI / 2; r.position.y = y; spire.add(r); });
      const coreMat = new THREE.MeshStandardMaterial({ color: C.violet, emissive: C.violet, emissiveIntensity: 0.7, roughness: 0.3 });
      spire.add(cyl(0.13, 0.13, spireH - 0.4, 12, coreMat, 0, spireH / 2 + 0.1, 0));
      const crown = new THREE.Mesh(new THREE.OctahedronGeometry(0.55, 0), new THREE.MeshStandardMaterial({ color: C.lilac, emissive: C.violet, emissiveIntensity: 0.9, roughness: 0.2, metalness: 0.4, transparent: true, opacity: 0.92 })); crown.position.y = spireH + 0.9; spire.add(crown);
      const crownRing = new THREE.Mesh(new THREE.TorusGeometry(0.95, 0.02, 6, 48), bas(C.lilac)); crownRing.rotation.x = Math.PI / 2; crownRing.position.y = spireH + 0.9; spire.add(crownRing);
      scene.add(spire); risers.push(spire);
      const spireR = new THREE.Group(); spireR.add(foot()); refl.add(spireR);   // the floor mirrors only the foot: a full mirror would read as a beam toward the camera
      const spireHalo = halo(V(0, spireH + 0.9, 0), C.violet, 5);
      const spireDecal = decal(0, 0, 6.2, C.violet, 0.35);
      const logLabel = label("log", null, V(0, spireH + 1.9, 0), V(0, 0, 0), 60);
      const tileGroup = new THREE.Group(); spire.add(tileGroup);
      let TILES = [], tileMeshes = [], tileW = [], tileIx = {}, SCHED = [];

      function rebuildTimeline(tl) {
        TILES.forEach((t) => { tileGroup.remove(t.mesh); t.mesh.geometry.dispose(); t.mat.dispose(); });
        TILES = []; tileMeshes = []; tileW = []; tileIx = {}; SCHED = [];
        buildEdges(tl);
        if (!tl) return;
        const hueOf = (a) => hexn(CAST[a] ? CAST[a].c : "#7C5CFF");
        tl.steps.forEach((S, i) => {
          const last = S.calls.length ? S.calls[S.calls.length - 1].end : S.start + 1.0;
          const land = Math.min(S.end + 0.3, S.findAt != null ? S.findAt + 0.5 : S.calls.length ? last + 0.3 : S.start + 0.95);
          SCHED[i] = { land, fly0: land - 0.65 };
          S.calls.forEach((c, j) => { tileIx[i + "." + j] = TILES.length; TILES.push({ kind: "call", i, j, t: c.end, col: callCol(c.st), n: c.n }); });
          tileIx[i + ".s"] = TILES.length; TILES.push({ kind: S.prop ? "gate" : "step", i, j: -1, t: land, col: S.prop ? C.warn : hueOf(S.a), n: 1 });
        });
        tileIx.decision = TILES.length; TILES.push({ kind: "decision", i: -1, j: -1, t: 0, col: C.good, n: 1 });
        const N = TILES.length;
        TILES.forEach((tile, k) => {
          const a = k * 0.83 + 0.35, y = 1.05 + k * (7.1 / N);
          const mat = new THREE.MeshStandardMaterial({ color: tile.col, emissive: tile.col, emissiveIntensity: 0.7, roughness: 0.35, metalness: 0.2 });
          const w = tile.kind === "step" ? 0.62 : tile.kind === "gate" || tile.kind === "decision" ? 0.78 : 0.46 + 0.07 * (Math.min(tile.n, 4) - 1);
          const m = new THREE.Mesh(new THREE.BoxGeometry(w, 0.2, 0.3), mat);
          m.position.set(Math.cos(a) * 1.52, y, Math.sin(a) * 1.52); m.rotation.y = Math.PI / 2 - a; m.userData = { tile: k }; m.visible = false;
          tileGroup.add(m); tile.mesh = m; tile.mat = mat; tileMeshes.push(m); tileW.push(new THREE.Vector3());
        });
      }

      /* ---- members: portraits above racks (agents) and desks (people) ---- */
      const members = {}, agentPos = {}, rackTop = {};
      const MS = () => (narrow && !isCapture() ? 1.25 : 1);
      function drawPortrait(m, which, img) {
        const cv2 = which === "front" ? m.front.cv : m.back.cv, g2 = cv2.getContext("2d"), flip = which === "back", id = m.id, c = CAST[id];
        g2.clearRect(0, 0, 256, 256); g2.save(); if (flip) { g2.translate(0, 256); g2.scale(1, -1); }
        g2.save(); g2.beginPath(); g2.arc(128, 128, 100, 0, Math.PI * 2); g2.clip();
        let drew = false;
        if (img) {
          const s = Math.min(img.naturalWidth || img.width, img.naturalHeight || img.height), sx = ((img.naturalWidth || img.width) - s) / 2, sy = ((img.naturalHeight || img.height) - s) * 0.12;
          try { g2.drawImage(img, sx, sy, s, s, 28, 28, 200, 200); drew = true; } catch (e) { drew = false; }
        }
        if (!drew) { const initials = String(c.avatar || c.n || id); g2.fillStyle = c.c; g2.fillRect(0, 0, 256, 256); g2.fillStyle = "#fff"; g2.font = `700 ${initials.length > 1 ? 68 : 88}px sans-serif`; g2.textAlign = "center"; g2.textBaseline = "middle"; g2.fillText(initials, 128, 134); }
        g2.restore();
        g2.lineWidth = 5; g2.strokeStyle = c.c; g2.beginPath(); g2.arc(128, 128, 102, 0, Math.PI * 2); g2.stroke();
        g2.fillStyle = c.ai ? "#7C5CFF" : "#3B4A44"; g2.beginPath(); g2.arc(208, 208, 31, 0, Math.PI * 2); g2.fill(); g2.lineWidth = 6; g2.strokeStyle = "#fff"; g2.stroke(); g2.fillStyle = "#fff";
        if (c.ai) { g2.beginPath(); [[208, 189], [213.5, 202.5], [227, 208], [213.5, 213.5], [208, 227], [202.5, 213.5], [189, 208], [202.5, 202.5]].forEach((p, i) => (i ? g2.lineTo(p[0], p[1]) : g2.moveTo(p[0], p[1]))); g2.closePath(); g2.fill(); }
        else { g2.beginPath(); g2.arc(208, 200, 6.5, 0, Math.PI * 2); g2.fill(); g2.beginPath(); g2.moveTo(194.5, 222); g2.quadraticCurveTo(208, 203, 221.5, 222); g2.closePath(); g2.fill(); }
        g2.restore();
      }
      /** A photo from file:// would taint the texture canvas (and make WebGL throw): test on a scratch canvas first, then show the photo as an HTML overlay instead. */
      function imgSafe(img) {
        try { const c = document.createElement("canvas"); c.width = c.height = 2; const g2 = c.getContext("2d"); g2.drawImage(img, 0, 0, 2, 2); g2.getImageData(0, 0, 1, 1); return true; } catch (e) { return false; }
      }
      function loadPortrait(m) {
        const src = typeof avatarSrc === "function" ? avatarSrc(m.id) : "avatars/" + m.id + ".jpg";
        m.src = src;
        const p = new Promise((res) => {
          let tries = 0, cors = /^https?:/i.test(src);
          const load = () => {
          const img = new Image();
          img.referrerPolicy = "no-referrer";
          if (cors) img.crossOrigin = "anonymous";
          img.onload = () => {
            if (m.src !== src) { res(); return; }
            if (imgSafe(img)) { drawPortrait(m, "front", img); drawPortrait(m, "back", img); m.dom = false; } else { m.dom = true; m.pfImg.referrerPolicy = "no-referrer"; m.pfImg.src = src; }
            m.front.tex.needsUpdate = m.back.tex.needsUpdate = true; m.loaded = true; res(); if (G3) { schedule(); if (state && !destroyed) { try { drawNow(); } catch (e) { /* the next update redraws */ } } }   // repaint now: a paused replay would otherwise keep the initials
          };
          img.onerror = () => {
            if (m.src !== src) { res(); return; }
            if (cors) { cors = false; load(); return; }   // a remote portrait without CORS still shows as an HTML overlay
            if (++tries > 5) { m.loaded = true; res(); return; }
            setTimeout(load, 250 * tries);
          };
          img.src = src;
          };
          load();
        });
        readyPromises.push(p);
      }
      function syncPortraits() {
        Object.keys(members).forEach((id) => {
          const m = members[id];
          const src = typeof avatarSrc === "function" ? avatarSrc(m.id) : "avatars/" + m.id + ".jpg";
          if (m.src !== src) loadPortrait(m);
        });
      }
      function makeMember(id, p, size) {
        const mk = () => { const c = document.createElement("canvas"); c.width = c.height = 256; const tex = new THREE.CanvasTexture(c); tex.anisotropy = 4; return { cv: c, tex }; };
        const m = { id, size, base: p.clone(), front: mk(), back: mk(), name: "", dom: false, loaded: false, src: "", curSize: size, pf: el("span", "s3d-pf") };
        m.pfImg = el("img"); m.pfImg.alt = ""; m.pfImg.draggable = false; m.pf.appendChild(m.pfImg);
        m.pf.appendChild(el("i", "s3d-pfb " + (CAST[id].ai ? "ai" : "hu"), CAST[id].ai ? AISVG : HUSVG)); lbs.appendChild(m.pf);
        m.pfImg.addEventListener("error", () => { m.dom = false; });
        drawPortrait(m, "front", null); drawPortrait(m, "back", null); m.front.tex.needsUpdate = m.back.tex.needsUpdate = true;
        const sp = (map, order, add, op) => { const mat = new THREE.SpriteMaterial({ map, transparent: true, depthWrite: false, fog: false, opacity: op == null ? 1 : op, blending: add ? THREE.AdditiveBlending : THREE.NormalBlending }); const s = new THREE.Sprite(mat); s.position.copy(p); s.renderOrder = order; scene.add(s); return s; };
        m.glow = sp(glowTex, 3, true, 0); m.ring = sp(ringTex, 4, false, 0.7); m.arc = sp(arcTex, 5, false, 0); m.pic = sp(m.front.tex, 6, false); m.sel = sp(thinTex, 7, false, 0); m.pulse = sp(thinTex, 7, true, 0);
        m.pic.userData.id = id;
        m.rp = sp(m.back.tex, 0, false, 0.2); m.rg = sp(glowTex, 0, true, 0); m.rp.material.fog = true;
        members[id] = m; loadPortrait(m); return m;
      }
      function buildRack(id) {
        const g2 = new THREE.Group(), col = hexn(CAST[id].c);
        const ledSets = [0, 1, 2].map((i) => ({ m: bas([col, C.good, 0xe8f0ec][i]), base: new THREE.Color([bright(col, 0.25), C.good, 0xe8f0ec][i]) }));
        g2.leds = ledSets;
        g2.add(box(1.8, 0.14, 1.5, M.metal2, 0, 0.07, 0)); g2.add(box(1.5, 3.05, 1.2, M.metal, 0, 1.67, 0)); g2.add(box(1.34, 2.8, 0.05, M.panel, 0, 1.7, 0.62));
        for (let k = 0; k < 7; k++) {
          const y = 0.5 + k * 0.38;
          g2.add(box(1.24, 0.28, 0.04, M.blade, 0, y + 0.14, 0.65)); g2.add(box(0.1, 0.07, 0.03, ledSets[0].m, -0.5, y + 0.14, 0.675)); g2.add(box(0.1, 0.07, 0.03, ledSets[k % 2 ? 1 : 2].m, -0.34, y + 0.14, 0.675));
          for (let v = 0; v < 4; v++) g2.add(box(0.3, 0.018, 0.02, M.dim, 0.22, y + 0.07 + v * 0.045, 0.675));
        }
        g2.add(box(1.52, 0.07, 1.22, bas(col), 0, 3.22, 0));
        const em = new THREE.Mesh(new THREE.TorusGeometry(0.55, 0.03, 8, 40), bas(bright(col, 0.4))); em.rotation.x = Math.PI / 2; em.position.y = 3.45; g2.add(em);
        const cone = new THREE.Mesh(new THREE.CylinderGeometry(0.78, 0.5, 0.7, 28, 1, true), glowMat({ color: col, opacity: 0.1, side: THREE.DoubleSide })); cone.position.y = 3.85; g2.add(cone);
        lineEdges(g2.children[1], bright(col, 0.3), 0.28);
        return g2;
      }
      SLOT.forEach((id, i) => {
        const x = AGENT_X[i], z = -9 + 0.035 * x * x;
        const g2 = buildRack(id); g2.position.set(x, 0, z); g2.rotation.y = Math.atan2(-x * 0.8, 14 - z);
        place(g2); blob(x, z, 2.1, 0.55);
        const pc = hexn(CAST[id].c), pool = decal(x, z, 3.0, pc, 0.22);
        const L = Math.hypot(x, z), ux = -x / L, uz = -z / L; conduit(x + ux * 1.7, z + uz * 1.7, -ux * 3.2, -uz * 3.2, pc, 0.16);
        const p = V(x, 5.4, z); agentPos[id] = p; rackTop[id] = V(x, 3.4, z);
        const m = makeMember(id, p, id === HUB_ID ? 1.95 : 1.7); m.pool = pool; m.rack = g2; m.label = label("agent", id, p, V(0, m.size * 0.5 + 0.15, 0), 40);
      });

      /* ---- people: laptops and one phone; screens mirror each member's current event ---- */
      const screens = {};
      function screenCanvas(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h; const t = new THREE.CanvasTexture(c); t.anisotropy = 4; return { cv: c, tex: t, sig: "" }; }
      function drawLaptop(sc, id, st) {
        const c = sc.cv, g2 = c.getContext("2d"), w = c.width, h = c.height, hue = CAST[id].c;
        g2.fillStyle = "#0b0a18"; g2.fillRect(0, 0, w, h); g2.fillStyle = hue; g2.fillRect(0, 0, w, 20);
        g2.fillStyle = "#fff"; g2.globalAlpha = 0.9; g2.font = "700 12px sans-serif"; g2.textBaseline = "alphabetic"; g2.fillText(st.title, 8, 14); g2.globalAlpha = 1;
        g2.fillStyle = "rgba(255,255,255,.12)"; for (let i = 0; i < 4; i++) g2.fillRect(10, 36 + i * 17, 110 + ((i * 37) % 70), 8);
        [0.5, 0.8, 0.35, 0.9, 0.6, 0.75].forEach((v, i) => { g2.fillStyle = i === 2 ? "#F87171" : "#7C5CFF"; g2.fillRect(150 + i * 14, 150 - v * 80, 9, v * 80); });
        g2.fillStyle = st.col; g2.globalAlpha = 0.2; g2.fillRect(8, 118, 128, 40); g2.globalAlpha = 1; g2.strokeStyle = st.col; g2.lineWidth = 2; g2.strokeRect(8, 118, 128, 40);
        g2.fillStyle = "#fff"; g2.font = "700 12px sans-serif"; g2.fillText(st.line1, 15, 136); g2.font = "11px sans-serif"; g2.globalAlpha = 0.85; g2.fillText(st.line2, 15, 151); g2.globalAlpha = 1;
        sc.tex.needsUpdate = true;
      }
      function drawPhone(sc, st) {
        const c = sc.cv, g2 = c.getContext("2d"), w = c.width;
        const zh = REPLAY_LANG === "zh";
        g2.fillStyle = "#0b0a18"; g2.fillRect(0, 0, w, c.height); g2.fillStyle = "#3F3A66"; g2.fillRect(0, 0, w, 26); g2.fillStyle = "#fff"; g2.font = "700 13px sans-serif"; g2.textBaseline = "alphabetic"; g2.fillText(zh ? "活动" : "Activity", 12, 18);
        g2.fillStyle = "rgba(255,255,255,.14)"; g2.fillRect(12, 40, w - 24, 10); g2.fillRect(12, 58, w - 60, 8); g2.fillRect(12, 74, w - 40, 8);
        g2.fillStyle = st.col; g2.globalAlpha = 0.22; g2.fillRect(10, 98, w - 20, 108); g2.globalAlpha = 1; g2.strokeStyle = st.col; g2.lineWidth = 2; g2.strokeRect(10, 98, w - 20, 108);
        g2.fillStyle = "#fff"; g2.font = "700 14px sans-serif"; g2.fillText(st.line1, 18, 120); g2.font = "12px sans-serif"; g2.globalAlpha = 0.85; g2.fillText(st.line2, 18, 140); g2.fillText(st.line3 || "", 18, 156); g2.globalAlpha = 1;
        g2.fillStyle = st.col; g2.fillRect(18, 170, 96, 26); g2.fillStyle = "#08071a"; g2.font = "700 12px sans-serif"; g2.fillText(zh ? "回放" : "Replay", 28, 188);
        g2.fillStyle = "#C4B5FD"; g2.font = "700 11px sans-serif"; g2.fillText(zh ? "只读回放" : "Read-only replay", 12, 250); g2.fillText(zh ? "没有实时操作" : "No live actions", 12, 266);
        sc.tex.needsUpdate = true;
      }
      ALL.filter((id) => !CAST[id].ai).forEach((id) => {
        const [x, z] = HX[id], g2 = new THREE.Group(), col = hexn(CAST[id].c);
        g2.add(box(4.3, 0.13, 2.0, M.metal2, 0, 0.92, 0)); [-1.9, 1.9].forEach((dx) => g2.add(box(0.14, 0.92, 1.6, M.metal, dx, 0.46, 0)));
        const strip = new THREE.Mesh(new THREE.BoxGeometry(4.0, 0.03, 0.03), bas(col)); strip.position.set(0, 0.88, 0.97); g2.add(strip);
        if (CAST[id].dev === "phone") {
          const sc = screenCanvas(160, 300); sc.cv.width = 160; screens[id] = sc;
          const ph = new THREE.Group(); ph.add(box(1.15, 2.1, 0.09, M.metal, 0, 1.1, 0));
          const scr = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 1.88), new THREE.MeshBasicMaterial({ map: sc.tex, fog: false })); scr.position.set(0, 1.1, 0.052); ph.add(scr);
          ph.add(box(0.7, 0.09, 0.7, M.metal2, 0, 0.04, -0.25)); ph.position.set(0, 1.0, 0.1); ph.rotation.x = -0.2; g2.add(ph);
          g2.add(box(1.5, 0.06, 1.0, M.metal, -1.35, 0.995, 0.15)); g2.add(box(1.5, 0.05, 1.0, M.panel, -1.35, 1.05, 0.15));
        } else {
          const sc = screenCanvas(256, 176); screens[id] = sc;
          const lap = new THREE.Group(); lap.add(box(1.8, 0.07, 1.2, M.metal, 0, 0, 0));
          const deck = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.8), new THREE.MeshBasicMaterial({ map: cTex(128, 64, (gg, w, h) => { gg.fillStyle = "#141228"; gg.fillRect(0, 0, w, h); gg.fillStyle = "#2c2848"; for (let r = 0; r < 4; r++) for (let c2 = 0; c2 < 14; c2++) gg.fillRect(3 + c2 * 8.8, 4 + r * 14, 7, 10); }), fog: false }));
          deck.rotation.x = -Math.PI / 2; deck.position.set(0, 0.04, 0.1); lap.add(deck);
          const lid = new THREE.Group(); lid.position.set(0, 0.04, -0.58); lid.rotation.x = -0.34; lid.add(box(1.8, 1.2, 0.05, M.metal, 0, 0.6, 0));
          const scr = new THREE.Mesh(new THREE.PlaneGeometry(1.64, 1.04), new THREE.MeshBasicMaterial({ map: sc.tex, fog: false })); scr.position.set(0, 0.6, 0.03); lid.add(scr); lap.add(lid); lap.position.set(0, 0.985, 0); g2.add(lap);
        }
        g2.position.set(x, 0, z); g2.rotation.y = x < 0 ? 0.14 : x > 0 ? -0.14 : 0;
        place(g2); blob(x, z, 3.0, 0.5); decal(x, z, 3.6, col, 0.2); conduit(0, z - 1.4, x * 0.3, z * 0.3, col, 0.14);
        const p = V(x, id === PHONE_ID ? 4.4 : 3.7, z); agentPos[id] = p;
        const m = makeMember(id, p, 1.55); m.label = label("human", id, p, V(0, m.size * 0.5 + 0.1, 0), 50);
      });

      /* ---- data layer: MCP cabinets (plug + beacon + padlock), database drums, dishes ---- */
      const srcs = {}, srcPos = {};
      function buildCab(o, col) {
        const g2 = new THREE.Group(), connected = o.st === "ok" || o.st === "limited";
        g2.add(box(1.8, 0.14, 1.45, M.metal2, 0, 0.07, 0));
        const bod = box(1.5, 2.7, 1.15, M.metal, 0, 1.49, 0); g2.add(bod); lineEdges(bod, col, 0.3);
        const doorMat = new THREE.MeshStandardMaterial({ color: 0x000000, roughness: 0.55, metalness: 0.3, emissive: col, emissiveIntensity: 0 });
        g2.add(box(1.3, 2.4, 0.05, doorMat, 0, 1.5, 0.6));
        for (let v = 0; v < 9; v++) g2.add(box(0.62, 0.035, 0.02, M.blade, -0.18, 0.55 + v * 0.2, 0.635));
        const plateMat = bas(col); g2.add(box(0.9, 0.07, 0.02, plateMat, 0, 2.55, 0.635));
        const leds = [0, 1, 2].map((i) => { const m = bas(col); g2.add(box(0.1, 0.1, 0.02, m, 0.5, 2.15 - i * 0.22, 0.64)); return m; });
        g2.add(cyl(0.27, 0.3, 0.12, 20, M.metal2, 0, 2.9, 0));
        const plug = new THREE.Group(); plug.position.set(0, connected ? 3.15 : 3.5, 0);
        plug.add(box(0.46, 0.3, 0.34, M.metal2, 0, 0.15, 0)); const pm = bas(col); [-0.1, 0.1].forEach((dx) => plug.add(box(0.06, 0.22, 0.05, pm, dx, -0.09, 0))); plug.add(box(0.3, 0.04, 0.04, pm, 0, 0.31, 0)); g2.add(plug);
        g2.add(new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3([V(0, 3.45, 0), V(0, 4.2, -0.1), V(0, 4.4, -0.7), V(0, 3.2, -1.3), V(0, 1, -1.2), V(0, 0.1, -1.05)]), 30, 0.04, 6), M.dim));
        const bm = bas(col), bc = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), bm); bc.position.set(-0.55, 2.95, 0.4); g2.add(bc);
        if (o.st === "auth") { const lock = new THREE.Group(); lock.add(box(0.3, 0.24, 0.08, bas(col), 0, 0, 0)); const sh = new THREE.Mesh(new THREE.TorusGeometry(0.1, 0.025, 6, 16, Math.PI), bas(col)); sh.position.y = 0.12; lock.add(sh); lock.position.set(0, 1.0, 0.66); g2.add(lock); }
        g2.parts = { doorMat, plateMat, leds, bm, rims: [], head: null };
        return g2;
      }
      function buildDrum(o, col) {
        const g2 = new THREE.Group(), rims = [];
        g2.add(cyl(1.15, 1.2, 0.14, 40, M.metal2, 0, 0.07, 0));
        for (let k = 0; k < 3; k++) {
          const y = 0.38 + k * 0.58, pl = cyl(1.0, 1.0, 0.46, 40, M.metal, 0, y, 0); g2.add(pl); lineEdges(pl, col, 0.18);
          const rm = bas(col), rg = new THREE.Mesh(new THREE.TorusGeometry(1.0, 0.032, 6, 56), rm); rg.rotation.x = Math.PI / 2; rg.position.y = y + 0.23; g2.add(rg); rims.push(rm);
          for (let v = 0; v < 3; v++) g2.add(box(0.5, 0.03, 0.02, M.blade, 0, y - 0.08 + v * 0.075, 0.99));
        }
        g2.add(cyl(0.55, 0.6, 0.1, 28, M.metal2, 0, 2.1, 0));
        const bm = bas(col), bc = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), bm); bc.position.set(0, 2.25, 0); g2.add(bc);
        g2.parts = { rims, bm, doorMat: null, plateMat: null, leds: [], head: null };
        return g2;
      }
      function buildDish(o, col) {
        const g2 = new THREE.Group();
        g2.add(cyl(0.8, 0.9, 0.16, 30, M.metal2, 0, 0.08, 0)); g2.add(cyl(0.09, 0.12, 1.7, 10, M.metal, 0, 1.0, 0));
        const head = new THREE.Group(); head.position.y = 1.9;
        const dm = new THREE.MeshStandardMaterial({ color: 0x8f89b0, roughness: 0.28, metalness: 0.7, side: THREE.DoubleSide });
        const dish = new THREE.Mesh(new THREE.SphereGeometry(1.35, 40, 12, 0, Math.PI * 2, 0, 0.62), dm); dish.rotation.x = -Math.PI / 2 + 0.5; head.add(dish);
        const arm = cyl(0.025, 0.025, 1.0, 6, M.metal, 0, 0, 0.45); arm.rotation.x = Math.PI / 2 - 0.5; head.add(arm);
        const bm = bas(col), fd = new THREE.Mesh(new THREE.SphereGeometry(0.09, 10, 8), bm); fd.position.set(0, 0.45, 0.7); head.add(fd);
        g2.add(head); g2.parts = { head, bm, rims: [], leds: [], doorMat: null, plateMat: null };
        return g2;
      }
      SRCS.forEach((o) => {
        const conn = typeof CONN !== "undefined" ? CONN.find((x) => x.k === o.k) : null;
        const hrow = !conn && typeof HEALTH !== "undefined" ? HEALTH.find((h) => o.k.indexOf(String(h[0]).toLowerCase().replace(/[^a-z]/g, "")) === 0) : null;
        o.st = conn ? conn.s : hrow ? hrow[1] : "plan";
        const col = STC[o.st] || C.faint;
        const g2 = o.kind === "cab" ? buildCab(o, col) : o.kind === "drum" ? buildDrum(o, col) : buildDish(o, col);
        g2.position.set(o.x, 0, o.z); g2.rotation.y = Math.atan2(-o.x, -o.z);
        scene.add(g2); const rg = g2.clone(true); refl.add(rg); risers.push(g2);
        const headR = o.kind === "dish" ? rg.children[rg.children.length - 1] : null;
        blob(o.x, o.z, 2.2, 0.55); const dim = o.st === "plan";
        const pool = decal(o.x, o.z, 3.1, col, dim ? 0.07 : 0.24); conduit(o.x * 0.88, o.z * 0.88, o.x * 0.3, o.z * 0.3, col, dim ? 0.07 : 0.2);
        const top = o.kind === "dish" ? 2.4 : o.kind === "drum" ? 2.6 : 3.5; srcPos[o.k] = V(o.x, top, o.z);
        const hl = halo(V(o.x, top - 0.3, o.z), col, 1.5);
        const ping = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.0, 56), glowMat({ color: col, opacity: 0, side: THREE.DoubleSide })); ping.rotation.x = -Math.PI / 2; ping.position.set(o.x, 0.06, o.z); ping.renderOrder = 3; scene.add(ping);
        const lab = label("src", o.k, V(o.x, top, o.z), V(0, 0.55, 0), 20); lab.el.classList.add(o.x < 0 ? "l" : "r");
        srcs[o.k] = { g: g2, rg, headR, parts: g2.parts, pool, halo: hl, ping, col, st: o.st, o, baseCol: new THREE.Color(col), flashCol: new THREE.Color(), label: lab, dx: o.x < 0 ? -34 : 34, top };
      });

      /* ---- floor edges (who read what, from which system): one per member/server pair with calls in this job ---- */
      let EDGES = [];
      const WORST = { ok: 1, limited: 2, blocked: 3 };
      function numTex() { const c = document.createElement("canvas"); c.width = 160; c.height = 80; const t = new THREE.CanvasTexture(c); t.anisotropy = 4; return { cv: c, tex: t, n: -1, col: "" }; }
      function drawNum(nt, n, col) {
        if (nt.n === n && nt.col === col) return; nt.n = n; nt.col = col;
        const g2 = nt.cv.getContext("2d"); g2.clearRect(0, 0, 160, 80);
        g2.fillStyle = "rgba(9,8,22,.92)"; g2.strokeStyle = col; g2.lineWidth = 4;
        g2.beginPath(); if (g2.roundRect) g2.roundRect(6, 10, 148, 60, 30); else g2.rect(6, 10, 148, 60); g2.fill(); g2.stroke();
        g2.fillStyle = "#F4F2FA"; g2.font = "700 40px \"JetBrains Mono\", Menlo, monospace"; g2.textAlign = "center"; g2.textBaseline = "middle"; g2.fillText("×" + n, 80, 42);
        nt.tex.needsUpdate = true;
      }
      function clearEdges() {   // sprites share one geometry: only meshes dispose theirs
        EDGES.forEach((e) => { [e.core, e.wide, e.num].forEach((o) => { scene.remove(o); if (o.geometry && !o.isSprite) o.geometry.dispose(); }); [e.cm, e.wm].forEach((m) => { const k = glowMats.indexOf(m); if (k >= 0) glowMats.splice(k, 1); m.dispose(); }); e.nt.tex.dispose(); e.num.material.dispose(); });
        EDGES = [];
      }
      /* a flat ribbon along a quadratic curve on the floor (a -> c -> b), trimmed at both ends so it stops short of the portrait and the cabinet */
      function ribbon(ax, az, cx, cz, bx, bz, w, n, t0, t1) {
        const pos = new Float32Array((n + 1) * 6), idx = [];
        for (let i = 0; i <= n; i++) {
          const u = t0 + (t1 - t0) * i / n, v = 1 - u, x = v * v * ax + 2 * v * u * cx + u * u * bx, z = v * v * az + 2 * v * u * cz + u * u * bz;
          const dx = 2 * v * (cx - ax) + 2 * u * (bx - cx), dz = 2 * v * (cz - az) + 2 * u * (bz - cz), L = Math.hypot(dx, dz) || 1, nx = -dz / L * w / 2, nz = dx / L * w / 2;
          pos.set([x + nx, 0, z + nz, x - nx, 0, z - nz], i * 6);
          if (i < n) { const k = i * 2; idx.push(k, k + 1, k + 2, k + 1, k + 3, k + 2); }
        }
        const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(pos, 3)); g.setIndex(idx); return g;
      }
      function buildEdges(tl) {
        clearEdges(); if (!tl) return;
        const by = {};
        tl.steps.forEach((S, i) => S.calls.forEach((c, j) => { const key = S.a + ">" + c.s; (by[key] = by[key] || { a: S.a, s: c.s, calls: [] }).calls.push([i, j]); }));
        Object.keys(by).forEach((key) => {
          const e = by[key], m = members[e.a], so = srcs[e.s]; if (!m || !so) return;
          const ax = m.base.x, az = m.base.z, bx = so.o.x, bz = so.o.z, L = Math.hypot(bx - ax, bz - az); if (L < 3) return;
          const cx = (ax + bx) / 2 * (1 - BOW), cz = (az + bz) / 2 * (1 - BOW), t0 = Math.min(0.3, 1.5 / L), t1 = 1 - Math.min(0.3, 1.8 / L);
          e.cm = glowMat({ color: C.lilac, opacity: 0, fog: true, side: THREE.DoubleSide }); e.wm = glowMat({ color: C.lilac, opacity: 0, fog: true, side: THREE.DoubleSide });
          e.core = new THREE.Mesh(ribbon(ax, az, cx, cz, bx, bz, 0.16, 40, t0, t1), e.cm); e.core.position.y = 0.075; e.core.renderOrder = 2; scene.add(e.core);
          e.wide = new THREE.Mesh(ribbon(ax, az, cx, cz, bx, bz, 0.9, 40, t0, t1), e.wm); e.wide.position.y = 0.07; e.wide.renderOrder = 2; scene.add(e.wide);
          e.nt = numTex(); e.num = new THREE.Sprite(new THREE.SpriteMaterial({ map: e.nt.tex, transparent: true, depthWrite: false, depthTest: false, fog: false, opacity: 0 }));
          e.num.position.set(0.25 * ax + 0.5 * cx + 0.25 * bx, 0.5, 0.25 * az + 0.5 * cz + 0.25 * bz); e.num.scale.set(1.9, 0.95, 1); e.num.renderOrder = 9; e.num.visible = false; scene.add(e.num);
          EDGES.push(e);
        });
      }

      /* ---- dust: seeded, drifting with replay time ---- */
      const DN = 240, dustPos = new Float32Array(DN * 3), dustBase = [];
      (function () { let s = 20261003; const rnd = () => { s |= 0; s = s + 0x6D2B79F5 | 0; let t = Math.imul(s ^ s >>> 15, 1 | s); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; for (let i = 0; i < DN; i++) dustBase.push([(rnd() - 0.5) * 64, rnd() * 15, (rnd() - 0.5) * 46, 0.12 + rnd() * 0.28, rnd() * 6.28]); })();
      const dustGeo = new THREE.BufferGeometry(); dustGeo.setAttribute("position", new THREE.BufferAttribute(dustPos, 3));
      const dust = new THREE.Points(dustGeo, new THREE.PointsMaterial({ map: glowTex, size: 0.28, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0.5, color: 0xd9d0ff, sizeAttenuation: true, fog: false }));
      dust.frustumCulled = false; scene.add(dust);

      /* ---- packet pool: orb with halo, core and a six-sprite trail, placed on a quadratic arc ---- */
      const pool = []; let poolN = 0;
      const mkSp = (tex, size, op, order) => { const m = new THREE.SpriteMaterial({ map: tex, color: 0xffffff, transparent: true, depthWrite: false, depthTest: false, fog: false, blending: THREE.AdditiveBlending, opacity: op }); const s = new THREE.Sprite(m); s.visible = false; s.renderOrder = order; s.userData.sz = size; s.userData.op = op; scene.add(s); return s; };
      function newPacket() { const p = { halo: mkSp(glowTex, 2.8, 0.42, 8), head: mkSp(orbTex, 1.7, 1, 8), core: mkSp(orbTex, 0.8, 1, 9), trail: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => mkSp(orbTex, 1.4 - i * 0.14, 0.7 - i * 0.07, 8)), on: false }; p.core.material.color.setHex(0xffffff); pool.push(p); return p; }
      const tA = V(0, 0, 0), tB = V(0, 0, 0), tM = V(0, 0, 0), tP = V(0, 0, 0), tP2 = V(0, 0, 0);
      const bez = (u, out) => { const w = 1 - u; return out.set(w * w * tA.x + 2 * w * u * tM.x + u * u * tB.x, w * w * tA.y + 2 * w * u * tM.y + u * u * tB.y, w * w * tA.z + 2 * w * u * tM.z + u * u * tB.z); };
      function packet(a, b, p, col, lift, size) {
        if (p <= 0 || p >= 1) return;
        const o = pool[poolN] || newPacket(); poolN++;
        tA.copy(a); tB.copy(b); tM.copy(tA).add(tB).multiplyScalar(0.5); tM.y += (lift + tA.distanceTo(tB) * 0.12) * Math.max(0.08, liftK);
        const bw = BOW * (1 - liftK); tM.x -= tM.x * bw; tM.z -= tM.z * bw;   // in the plan the arc bends toward the log, like the floor edges
        const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2, uu = lerp(p, e, 0.45);
        const fade = Math.min(1, p * 8, (1 - p) * 8 + 0.15), swell = 1 + 0.3 * Math.sin(Math.PI * p);
        bez(uu, tP); o.on = true;
        [o.halo, o.head, o.core].forEach((s, i) => { s.position.copy(tP); const z = s.userData.sz * size * swell; s.scale.set(z, z, 1); s.visible = true; s.material.opacity = i === 0 ? s.userData.op * fade : fade; if (i < 2) s.material.color.setHex(col); });
        o.trail.forEach((s, i) => { const ut = Math.max(0, uu - (i + 1) * 0.04); bez(ut, tP2); s.position.copy(tP2); const z = s.userData.sz * size * swell; s.scale.set(z, z, 1); s.visible = uu > (i + 1) * 0.04; s.material.opacity = s.userData.op * fade; s.material.color.setHex(col); });
      }
      function packetsEnd() { for (let i = poolN; i < pool.length; i++) { const o = pool[i]; if (o.on) { o.on = false; o.halo.visible = o.head.visible = o.core.visible = false; o.trail.forEach((s) => (s.visible = false)); } } poolN = 0; }

      /* ---- camera framing ---- */
      let reserve = { t: 0, b: 0, r: 0 };
      function applyView() {
        const dy = (reserve.t - reserve.b) / 2, dx = -reserve.r / 2;
        if (Math.abs(dy) < 0.5 && Math.abs(dx) < 0.5) cam.clearViewOffset(); else cam.setViewOffset(W, H, -dx, -dy, W, H);
        cam.updateProjectionMatrix();
      }
      function fovFor() { const a = W / Math.max(1, H); return a < 0.9 ? 58 : a < 1.3 ? 48 : 40; }
      function freeBox() { return { w: Math.max(160, W - reserve.r), h: Math.max(160, H - reserve.t - reserve.b) }; }
      function fitDist(pos, tgt, ex, ey) {
        const f = freeBox(), fovT = Math.tan(cam.fov * Math.PI / 360), dir = pos.clone().sub(tgt), base = dir.length();
        const d = Math.max(base, ex * H / (f.w * fovT), ey * H / (f.h * fovT)); dir.setLength(d); return tgt.clone().add(dir);
      }
      function presetPose(name) {
        const p = PRESETS[name]; if (!p) return null;
        let pos = V(p.p[0], p.p[1], p.p[2]); const tgt = V(p.t[0], p.t[1], p.t[2]); let ex = p.ex, ey = p.ey;
        if (name === "overview" && W / Math.max(1, H) < 0.9) { pos.set(0, 21, 32); ex = 15.6; ey = 9; }   // portrait: crop the data layer, keep the racks and people readable
        if (ex) pos = fitDist(pos, tgt, ex, ey);
        return { pos, tgt };
      }
      /* the flat plan: straight down onto the floor, narrow lens; tall frames turn the plan a quarter so its long side runs down the screen */
      function flatPose() {
        const f = freeBox(), ft = Math.tan(FLAT.fov * Math.PI / 360), tall = f.w / Math.max(1, f.h) < 0.8;
        const ex = tall ? FLAT.ez : FLAT.ex, ez = tall ? FLAT.ex : FLAT.ez;
        const d = Math.max(ex * H / (f.w * ft), ez * H / (f.h * ft)) * 1.02, th = tall ? Math.PI / 2 : 0, ph = 0.002;
        const tgt = V(FLAT.t[0], FLAT.t[1], FLAT.t[2]);
        return { pos: tgt.clone().add(V(Math.sin(ph) * Math.sin(th) * d, Math.cos(ph) * d, Math.sin(ph) * Math.cos(th) * d)), tgt };
      }
      /* camera blend along a tilt: target moves straight, the offset turns on a sphere (radius eased in log space), so the plan tips up into the room */
      const sA = new THREE.Spherical(), sB = new THREE.Spherical(), sC = new THREE.Spherical();
      function blendPose(a, b, e) {
        if (e >= 1) return b; if (e <= 0) return a;
        const tgt = a.tgt.clone().lerp(b.tgt, e);
        sA.setFromVector3(a.pos.clone().sub(a.tgt)); sB.setFromVector3(b.pos.clone().sub(b.tgt));
        let dth = sB.theta - sA.theta; if (dth > Math.PI) dth -= 2 * Math.PI; if (dth < -Math.PI) dth += 2 * Math.PI;
        sC.set(Math.exp(lerp(Math.log(Math.max(1e-3, sA.radius)), Math.log(Math.max(1e-3, sB.radius)), e)), lerp(sA.phi, sB.phi, e), sA.theta + dth * e);
        return { pos: tgt.clone().add(V(0, 0, 0).setFromSpherical(sC)), tgt };
      }
      /* where an embedded rise lands: the current preset (overview unless a preset was chosen), framed with the 3D lens */
      function basePoseForRise() { const nm = PRESETS[preset] ? preset : "overview", keep = cam.fov; cam.fov = fovFor(); const p = presetPose(nm); cam.fov = keep; return p; }
      function restPoseFor(k) { return blendPose(flatPose(), basePoseForRise(), riseCam(k)); }
      function workingIds(st) {
        if (!st || !st.mstate) return [];
        return Object.keys(st.mstate).filter((id) => st.mstate[id] === "working");
      }
      function speakerOf(st) {
        const ids = workingIds(st);
        if (ids.length > 1) return null;
        if (ids.length === 1) return ids[0];
        if (st && st.active && agentPos[st.active]) return st.active;
        return (ctx.selected && agentPos[ctx.selected]) ? ctx.selected : HUB_ID;
      }
      function followPose(id) {
        if (id == null) return presetPose("overview");
        const ap = agentPos[id] || agentPos[HUB_ID] || V(0, 4, 0), hu = ap.z > 5;
        const tgt = V(ap.x * 0.55, 3.2, hu ? 6 : -1.5), pos = V(ap.x * 0.4 + (ap.x < 0 ? -8 : 8), hu ? 8.5 : 10, hu ? 28 : 31);
        return { pos: fitDist(pos, tgt, 9.5, 5), tgt };
      }
      const pathOut = [0, 0, 0, 0, 0, 0], lastTg = V(0, 3, 0);
      function scriptedPose(st, r) {
        if (r) {
          const p = r.pos || r.p, t = r.target || r.tgt || r.t;
          if (p && t) { cam.position.set(p[0], p[1], p[2]); const tg = V(t[0], t[1], t[2]); if (r.fov) cam.fov = r.fov; cam.lookAt(tg); if (ctl) ctl.target.copy(tg); lastTg.copy(tg); return true; }
        }
        if (!isCapture() || !builtTl) return false;
        hermite(pathKeys(builtTl, st), st.t, pathOut);
        const tg = V(pathOut[3], pathOut[4], pathOut[5]), dir = V(pathOut[0], pathOut[1], pathOut[2]).sub(tg); lastTg.copy(tg);
        /* fit the frame to the aspect: wide frames keep the authored distance, square and tall ones frame a narrower slice (and tall ones look more from above) */
        const asp = W / Math.max(1, H), ft = Math.tan(cam.fov * Math.PI / 360);
        let k = Math.max(1, (1.78 * Math.tan(20 * Math.PI / 180)) / (asp * ft));
        if (asp < 1.5) { const ex = lerp(11, 20, clamp((asp - 0.55) / 0.95, 0, 1)); k = clamp(ex / (asp * ft) / Math.max(1, dir.length()), 0.55, 2.2); }
        if (asp < 0.9) dir.y *= 1 + (0.9 - asp) * 1.6;
        dir.multiplyScalar(k); cam.position.copy(tg).add(dir); cam.lookAt(tg); if (ctl) ctl.target.copy(tg);
        return true;
      }

      /* ---- picking ---- */
      const ray = new THREE.Raycaster(), m2 = new THREE.Vector2();
      function pick(e) {
        const r = renderer.domElement.getBoundingClientRect(); m2.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(m2, cam);
        const sp = Object.values(members).map((m) => m.pic);   // picked even when the photo is an HTML overlay (file://) and the sprite itself is hidden
        const h1 = ray.intersectObjects(sp)[0]; if (h1) return { id: h1.object.userData.id };
        const tl2 = tileMeshes.filter((m) => m.visible); const h2 = ray.intersectObjects(tl2)[0];
        if (h2) return { tile: h2.object.userData.tile };
        return null;
      }

      /* ---- the frame: everything below is a pure function of (state, ctx, camera pose) ---- */
      const tmpV = V(0, 0, 0), p3 = V(0, 0, 0);
      const easeBack = (u) => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(u - 1, 3) + c1 * Math.pow(u - 1, 2); };
      const dimSet = (id) => { const d = ctx.dim; if (!d) return false; if (d === true) return !!ctx.selected && ctx.selected !== id; if (Array.isArray(d)) return d.indexOf(id) >= 0; if (d.has) return d.has(id); return false; };
      function decidedAt(st) {
        if (!st.gate.decided) return null;
        if (st.gate.decidedAt != null) return st.gate.decidedAt;
        if (ctx.decision && ctx.decision.at != null) return ctx.decision.at;
        if (st.after && st.after.length) return st.after[0].start - 0.5;
        return null;
      }
      const callCounts = {};

      let liftK = 1;   // packet arcs flatten with the room
      function apply(st) {
        const t = st.t, amb = reduced ? 0 : t, tl = builtTl, cap_ = isCapture();
        /* a custom camera path is evaluated once per frame; it may carry rise (the film's 2D -> 3D shot) */
        let cp = null; if (customPath) { try { cp = customPath(t, st) || null; } catch (e) { cp = null; } }
        const kR = cp && cp.rise != null ? clamp(+cp.rise, 0, 1) : riseK, eo = riseObj(kR), ec = riseCam(kR), flat = 1 - eo;
        if (cp && cp.rise != null && kR < 1) riseUsed = true;
        curRise = kR; liftK = eo;
        { const sy = Math.max(0.012, eo); risers.forEach((g) => { g.scale.y = sy; }); refl.scale.y = -sy; refl.visible = eo > 0.04; }
        Object.keys(members).forEach((id) => { agentPos[id].y = lerp(FLOOR_Y, members[id].base.y, eo); });
        Object.keys(srcs).forEach((k) => { const o = srcs[k]; srcPos[k].y = lerp(0.5, o.top, eo); o.halo.s.position.y = lerp(0.35, o.top - 0.3, eo); o.label.pos.y = srcPos[k].y; });
        spireHalo.s.position.y = lerp(0.6, spireH + 0.9, eo); logLabel.pos.y = lerp(1.0, spireH + 1.9, eo);
        conduits.forEach((c) => { c.m.opacity = c.base * (1 + 1.5 * flat); });
        dust.material.opacity = 0.5 * eo; dust.visible = eo > 0.02;
        scene.fog.density = lerp(0.0032, 0.0135, ec);
        floorMat.uniforms.uT.value = amb; floorMat.uniforms.uFlat.value = flat;
        spire.rotation.y = amb * 0.16; spireR.rotation.y = spire.rotation.y; crown.rotation.y = amb * 0.9; crown.rotation.x = 0.4; spire.updateMatrixWorld(true);

        /* event-log tiles: one per step and per call, from the call status */
        let revealed = 0, lastPulse = 0;
        const dAt = decidedAt(st);
        TILES.forEach((tile, k) => {
          let vis = false, landT = 0;
          if (tile.kind === "call") { const c = st.steps[tile.i] && st.steps[tile.i].calls[tile.j]; vis = !!c && c.status === "done"; landT = tile.t; }
          else if (tile.kind === "decision") { vis = !!st.gate.decided; landT = dAt != null ? dAt + 0.9 : -99; }
          else { vis = t >= tile.t; landT = tile.t; }
          const m = tile.mesh; m.getWorldPosition(tileW[k]);
          const f = t - landT, sc = vis ? clamp(easeBack(clamp(f / 0.5, 0, 1)), 0.01, 1.12) : 0;
          if (tile.kind === "decision") { tile.mat.color.setHex(st.gate.decided === "return" ? C.warn : C.good); tile.mat.emissive.setHex(st.gate.decided === "return" ? C.warn : C.good); }
          m.visible = vis; m.scale.setScalar(Math.max(0.001, sc));
          if (vis) { revealed++; tile.mat.emissiveIntensity = 0.7 + (f < 1.4 ? 1.7 * Math.pow(1 - f / 1.4, 2) : 0); if (f >= 0 && f < 0.7) lastPulse = Math.max(lastPulse, 1 - f / 0.7); }
        });
        const hf = lerp(0.42, 1, eo); coreMat.emissiveIntensity = 0.7 + 0.8 * lastPulse; spireHalo.s.scale.setScalar((5 + 2.2 * lastPulse) * hf); spireHalo.m.opacity = (0.35 + 0.4 * lastPulse) * hf; spireLight.intensity = 1.3 + 1.2 * lastPulse; spireDecal.mat.opacity = spireDecal.base * lerp(0.4, 1, eo);
        if (PRESENT) setLabel(logLabel, REPLAY_LANG === "zh" ? "业务时间线" : "Campaign timeline", [revealed + (REPLAY_LANG === "zh" ? " 个节点" : " moments")]);
        else { const c = st.counters, gaps = c.limited + c.blocked; setLabel(logLabel, "Event log", [revealed + (revealed === 1 ? " entry" : " entries"), c.calls + (c.calls === 1 ? " call" : " calls") + " · " + gaps + " limited or blocked", "writes: " + c.writes]); }

        /* members: portrait, ring, glow, selection */
        const sel = ctx.selected, SC = MS();
        Object.keys(members).forEach((id) => {
          const o = members[id], ms = st.mstate[id] || "idle", working = ms === "working", hue = hexn(CAST[id].c);
          const isSel = sel === id, dimmed = dimSet(id);
          const col = ({ idle: bright(hue, -0), working: bright(hue, 0.25), waiting: C.warn, blocked: C.serious, learned: C.lilac, done: hue })[ms];
          const sz = o.size * SC * (working ? 1.1 : 1) * (hoverId === id ? 1.06 : 1) * lerp(1.22, 1, ec);   // the flat plan's nodes are a little larger
          const bob = amb ? Math.sin(amb * 1.3 + id.length * 1.7) * 0.07 * eo : 0, pos = o.base, py = agentPos[id].y;
          o.curSize = sz;
          const pa = dimmed ? 0.5 : 1;
          o.pic.position.set(pos.x, py + bob, pos.z); o.pic.scale.set(sz, sz, 1); o.pic.material.opacity = pa; o.pic.visible = !o.dom;
          o.ring.position.copy(o.pic.position); o.ring.scale.setScalar(sz * 0.95); o.ring.material.map = ms === "waiting" ? dashTex : ringTex; o.ring.material.color.setHex(col);
          o.ring.material.opacity = (ms === "idle" ? 0.45 : ms === "done" ? 0.7 : 0.95) * pa;
          o.ring.material.rotation = ms === "waiting" ? amb * 0.5 : 0;
          o.arc.position.copy(o.pic.position); o.arc.scale.setScalar(sz); o.arc.material.color.setHex(col); o.arc.material.opacity = working ? 1 : 0; o.arc.material.rotation = -amb * 3.2;
          const pulse = working ? 0.75 + 0.25 * Math.sin(amb * 7) : 0, gst = ms === "waiting" ? 0.3 + 0.18 * Math.sin(amb * 3.2) : ms === "learned" ? 0.55 : ms === "blocked" ? 0.25 : 0;
          o.glow.position.copy(o.pic.position); o.glow.scale.setScalar(sz * (working ? 2.5 + 0.2 * Math.sin(amb * 7) : 2.1)); o.glow.material.color.setHex(working ? bright(hue, 0.2) : col); o.glow.material.opacity = (working ? 0.85 * pulse + 0.15 : gst) * pa;
          o.sel.position.copy(o.pic.position); o.sel.scale.setScalar(sz * 1.18); o.sel.material.opacity = isSel ? 0.95 : 0; o.sel.material.color.setHex(0xffffff);
          const hp = hlId === id && hlP >= 0 && hlP < 1 ? hlP : -1;
          o.pulse.position.copy(o.pic.position); o.pulse.scale.setScalar(sz * (1.15 + hp * 0.9)); o.pulse.material.opacity = hp >= 0 ? 0.9 * (1 - hp) : 0; o.pulse.material.color.setHex(C.lilac);
          o.rp.position.set(pos.x, -(py + bob), pos.z); o.rp.scale.set(sz, sz, 1); o.rg.position.copy(o.rp.position); o.rg.scale.copy(o.glow.scale); o.rg.material.color.copy(o.glow.material.color); o.rg.material.opacity = o.glow.material.opacity * 0.4;
          o.rp.material.opacity = 0.2 * pa;
          if (o.rack) { const rate = working ? 14 : 3.2; o.rack.leds.forEach((l, i) => { const f = 0.35 + 0.65 * (Math.sin(amb * rate * (1 + i * 0.37) + id.length + i * 2) > 0.15 ? 1 : 0.25); l.m.color.copy(l.base).multiplyScalar(working ? f * 1.15 : f * 0.7); }); o.pool.mat.opacity = o.pool.base * (working ? 2.4 : 1); }
          /* labels: name from CAST (people.local.js changes it), tag, state word */
          const c = CAST[id], word = STATE_WORD[ms];
          const kind = PRESENT ? c.role : c.ai ? "AI agent" : "Human" + (c.dev === "phone" ? " · phone" : c.dev === "laptop" ? " · laptop" : "");
          setLabel(o.label, c.n, PRESENT ? kind : kind + (word ? " · " + word : ""));
          o.label.off.y = sz * 0.5 + 0.14; o.label.sel = isSel; o.label.dim = dimmed;
          if (o.name !== c.n) { o.name = c.n; }
          const src = typeof avatarSrc === "function" ? avatarSrc(id) : o.src; if (src !== o.src) { o.loaded = false; loadPortrait(o); }
        });

        /* servers: flash in the colour of the latest call that reached them; counters of finished calls */
        Object.keys(srcs).forEach((k) => (callCounts[k] = { n: 0, l: 0, b: 0 }));
        const flash = {};
        st.steps.forEach((S, i) => {
          S.calls.forEach((c, j) => {
            const tc = tl && tl.steps[i] && tl.steps[i].calls[j]; if (!tc) return;
            const arr = tc.start + (tc.end - tc.start) * 0.5, since = t - arr;
            if (c.status === "done") { const cc = callCounts[c.s]; if (cc) { cc.n += c.n; if (c.result === "limited") cc.l += c.n; if (c.result === "blocked") cc.b += c.n; } }
            if (since >= 0 && since < 1.6 && (!flash[c.s] || arr >= flash[c.s].arr)) flash[c.s] = { arr, since, col: callCol(c.result) };
          });
        });
        Object.keys(srcs).forEach((k) => {
          const o = srcs[k], f = flash[k], amt = f ? Math.pow(1 - f.since / 1.6, 1.6) : 0;
          if (f) { o.flashCol.setHex(f.col); o.ping.material.color.setHex(f.col); const pu = clamp(f.since / 1.3, 0, 1); o.ping.scale.setScalar(0.7 + pu * 3.4); o.ping.material.opacity = pu < 1 ? 0.9 * (1 - pu) : 0; } else o.ping.material.opacity = 0;
          const base = o.st === "ok" ? 0.9 : o.st === "limited" ? 0.55 + 0.35 * Math.sin(amb * 2.4) : o.st === "auth" ? (Math.sin(amb * 1.6) > 0 ? 0.9 : 0.35) : 0.25;
          const b = Math.max(base, amt), c = f && amt > 0.02 ? o.flashCol : o.baseCol;
          o.parts.bm.color.copy(c).multiplyScalar(0.4 + 0.6 * b);
          o.halo.s.scale.setScalar(o.halo.size * (1 + amt * 1.8)); o.halo.m.opacity = (o.st === "plan" ? 0.12 : 0.4) + 0.5 * amt; o.halo.m.color.copy(c);
          o.pool.mat.opacity = o.pool.base * (1 + amt * 3.5); if (amt > 0.02) o.pool.mat.color.copy(o.flashCol); else o.pool.mat.color.setHex(o.col);
          if (o.parts.doorMat) { o.parts.doorMat.emissive.copy(c); o.parts.doorMat.emissiveIntensity = amt * 0.85; o.parts.plateMat.color.copy(c).multiplyScalar(0.5 + 0.5 * b); o.parts.leds.forEach((m, i) => { const ff = 0.3 + 0.7 * (Math.sin(amb * (3 + i * 1.7) + k.length) > 0.1 ? 1 : 0.2); m.color.copy(c).multiplyScalar(o.st === "plan" ? 0.25 : ff * (0.5 + 0.5 * b)); }); }
          o.parts.rims.forEach((m, i) => m.color.copy(c).multiplyScalar(o.st === "plan" ? 0.3 : 0.45 + 0.55 * Math.max(amt, 0.5 + 0.5 * Math.sin(amb * 2 + i * 1.3))));
          if (o.parts.head) { const sway = amb ? Math.sin(amb * 0.5 + k.length) * 0.35 : 0; o.parts.head.rotation.y = sway; if (o.headR) o.headR.rotation.y = sway; }
          const cc = callCounts[k], conn = typeof CONN !== "undefined" ? CONN.find((x) => x.k === k) : null;
          const nm = PRESENT ? ((REPLAY_SCENE_LABELS && REPLAY_SCENE_LABELS.sourceNames && REPLAY_SCENE_LABELS.sourceNames[k]) || (conn && conn.n) || k) : (typeof UI !== "undefined" && UI.SRC_LABEL && UI.SRC_LABEL[k]) || (conn && conn.n) || k;
          const sub = PRESENT ? o.o.type : o.o.type + " · " + stWord(o.st), line3 = PRESENT ? "" : cc.n ? " · " + cc.n + (cc.n === 1 ? " call" : " calls") + (cc.b ? ", " + cc.b + " blocked" : "") + (cc.l ? ", " + cc.l + " limited" : "") : "";
          setLabel(o.label, nm, sub + line3, PRESENT ? null : o.st); o.label.el.title = PRESENT ? nm : nm + ": " + stWord(o.st);
        });

        /* floor edges: lit from a pair's first call, coloured by its worst result so far, pulsing while a call runs; counts read in the flat plan */
        EDGES.forEach((e) => {
          let n = 0, w = 0, run = false;
          e.calls.forEach((ij) => { const c = st.steps[ij[0]] && st.steps[ij[0]].calls[ij[1]]; if (!c) return; if (c.status === "done") { n += c.n; w = Math.max(w, WORST[c.result] || 1); } else if (c.status === "running") run = true; });
          const lit = n > 0 || run, col = w === 3 ? C.serious : w === 2 ? C.warn : C.lilac, pul = run ? 0.5 + 0.5 * Math.sin(amb * 9) : 0, dm = dimSet(e.a) ? 0.4 : 1;
          e.cm.color.setHex(col); e.wm.color.setHex(col);
          e.cm.opacity = lit ? (0.16 + 0.74 * flat + 0.3 * pul) * dm : 0; e.wm.opacity = lit ? (0.03 + 0.22 * flat + 0.18 * pul) * dm : 0;
          const no = n > 0 ? clamp(flat * 1.3 - 0.15, 0, 1) * dm : 0; e.num.visible = no > 0.01; e.num.material.opacity = no;
          if (n > 0) drawNum(e.nt, n, "#" + col.toString(16).padStart(6, "0"));
        });

        /* packets: member to server (request) to spire tile (result), straight from the call progress u */
        st.steps.forEach((S, i) => {
          const sc = SCHED[i], from = agentPos[S.a]; if (!from) return;
          S.calls.forEach((c, j) => {
            if (c.status !== "running") return;
            const sp = srcPos[c.s] || tmpV.set(0, 3, 0), tk = tileW[tileIx[i + "." + j]]; if (!tk) return;
            const m = Math.min(c.n, 4), hue = bright(hexn(CAST[S.a].c), 0.5);
            for (let q = 0; q < m; q++) {
              const d = m > 1 ? 0.18 * q / (m - 1) : 0, u = (c.u - d) / (1 - d);
              if (u <= 0 || u >= 1) continue;
              if (u < 0.5) packet(from, sp, u / 0.5, hue, 1.8, 1); else packet(sp, tk, (u - 0.5) / 0.5, callCol(c.result), 2.4, 1.05);
            }
          });
          const tk = tileW[tileIx[i + ".s"]];
          if (sc && tk && t >= sc.fly0 && t < sc.land) packet(from, tk, (t - sc.fly0) / (sc.land - sc.fly0), bright(hexn(CAST[S.a].c), 0.5), 2.2, 1.1);
          const record = builtTl && builtTl.job.steps[i], recipients = record && record.replayTo || [];
          if (S.active && recipients.length) {
            const progress = clamp((t - S.start) / Math.max(0.05, S.end - S.start), 0, 1);
            recipients.forEach((id) => { if (agentPos[id]) packet(from, agentPos[id], progress, bright(hexn(CAST[S.a].c), 0.35), 3.0, 1.1); });
          }
        });
        if (tl && tl.gateAt != null) {
          const pi = tl.steps.findIndex((S) => S.prop), pk = tileW[tileIx[pi + ".s"]], g0 = Math.max((SCHED[pi] ? SCHED[pi].land : 0) + 0.1, tl.gateAt - 1.05), g1 = tl.gateAt - 0.1;
          if (pk && t >= g0 && t < g1) st.gate.needs.forEach((id, q) => { if (agentPos[id]) packet(pk, agentPos[id], (t - g0) / (g1 - g0), C.warn, 2.2 + q * 0.2, 1.1); });
          if (pk && st.gate.decided && dAt != null) {
            const dk = tileW[tileIx.decision];
            if (st.gate.decided === "approve") {
              st.gate.needs.forEach((id, q) => { if (agentPos[id]) packet(agentPos[id], pk, (t - dAt - q * 0.12) / 0.9, C.good, 2.4, 1.15); });
              st.after.forEach((a) => { const to = agentPos[a.a]; if (to) packet(pk, to, (t - (a.start - 0.3)) / 1.2, (a.lesson || /^(learned|lesson drafted)$/i.test(a.sk)) ? C.lilac : C.good, 2.5, 1.2); });
            } else {
              const sender = st.gate.needs.find((id) => agentPos[id]) || st.gate.needs[st.gate.needs.length - 1];
              if (agentPos[sender] && agentPos[HUB_ID]) packet(agentPos[sender], agentPos[HUB_ID], (t - dAt) / 1.3, C.warn, 3.4, 1.2);
            }
          }
        }
        packetsEnd();

        /* screens mirror each member's current event */
        const clip = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));
        const screenState = (id, followCurrent = false) => {
          const shown = (st.steps || []).filter((x) => (followCurrent || x.a === id) && x.shown);
          const current = shown.length ? shown[shown.length - 1] : null;
          const step = current && builtTl ? builtTl.job.steps[current.i] : null;
          const active = !!(current && current.active), zh = REPLAY_LANG === "zh";
          const title = step ? step.sk : zh ? "活动" : "Activity";
          const line1 = step ? (step.replayTitle || step.sk) : zh ? "尚无活动" : "No activity yet";
          const line2 = step && typeof step.min === "number" && typeof REPLAY_CLOCK_FMT === "function" ? REPLAY_CLOCK_FMT(step.min) : zh ? "等待中" : "Standing by";
          return { current, active, title, line1, line2 };
        };
        if (PHONE_ID && screens[PHONE_ID]) {
          const scr = screens[PHONE_ID], item = screenState(PHONE_ID, true);
          const sig = [item.active, item.title, item.line1, item.line2].join("|");
          if (scr.sig !== sig) {
            scr.sig = sig;
            const col = item.active ? "#FBBF24" : item.current ? "#34D399" : "#8A83A3";
            drawPhone(scr, { col, line1: clip(item.title, 17), line2: clip(item.line1, 20), line3: clip(item.line2, 22) });
          }
        }
        ALL.filter((id) => !CAST[id].ai && id !== PHONE_ID).forEach((id) => {
          const scr = screens[id]; if (!scr) return;
          const item = screenState(id), sig = [item.active, item.title, item.line1, item.line2].join("|");
          if (scr.sig === sig) return; scr.sig = sig;
          const col = item.active ? "#FBBF24" : item.current ? "#34D399" : "#8A83A3";
          drawLaptop(scr, id, { title: clip(item.title, 22), col, line1: clip(item.line1, 20), line2: clip(item.line2, 20) });
        });

        /* dust drifts with replay time */
        for (let i = 0; i < DN; i++) { const b = dustBase[i], y = (b[1] + (amb ? amb * b[3] : 0)) % 15; dustPos[i * 3] = b[0] + (amb ? Math.sin(amb * 0.25 + b[4]) * 0.8 : 0); dustPos[i * 3 + 1] = y + 0.2; dustPos[i * 3 + 2] = b[2]; }
        dustGeo.attributes.position.needsUpdate = true;

        /* camera: scripted path in capture mode or with a custom path; presets and orbit controls otherwise */
        cam.fov = fovFor();
        const posed = scriptedPose(st, cp);
        if (posed && ec < 1) { const b = blendPose(flatPose(), { pos: cam.position.clone(), tgt: lastTg.clone() }, ec); cam.position.copy(b.pos); cam.lookAt(b.tgt); if (ctl) ctl.target.copy(b.tgt); }
        else if (!posed && kR < 1) { const b = restPoseFor(kR); cam.position.copy(b.pos); cam.lookAt(b.tgt); if (ctl) ctl.target.copy(b.tgt); }   // embedded rise: the pose is a pure function of rise
        else if (!posed && preset === "follow" && !cap_) {
          const id = speakerOf(st);
          if (id !== followId) { followId = id; goTo(followPose(id), false); }
        }
        cam.fov = lerp(FLAT.fov, cam.fov, ec);
        applyView(); cam.updateMatrixWorld(); cam.matrixWorldInverse.copy(cam.matrixWorld).invert();
      }

      /* ---- labels: project, then place without collisions. Two passes: first every name (staggered up when crowded), then the second
             line is added wherever it still fits. Portraits, the toolbar band and the caption band are obstacles; a server or log label
             that has no free spot is hidden rather than overlapped. ---- */
      const placed = [];
      function layoutLabels(st) {
        const fovT = Math.tan(cam.fov * Math.PI / 360), pxu = (d) => (H / (2 * fovT)) / Math.max(d, 0.5);
        labels.forEach((l) => {
          const mem = l.id && members[l.id] && (l.kind === "agent" || l.kind === "human") ? members[l.id] : null;
          if (mem) p3.copy(mem.pic.position); else p3.copy(l.pos).add(l.off);
          const wv = tmpV.copy(p3).applyMatrix4(cam.matrixWorldInverse); const depth = -wv.z; p3.project(cam);
          l.vis = depth > 0.5 && p3.z < 1 && p3.x > -1.2 && p3.x < 1.2 && p3.y > -1.2 && p3.y < 1.2;
          l.sx = (p3.x + 1) / 2 * W; l.sy = (1 - p3.y) / 2 * H; l.depth = cam.position.distanceTo(l.pos);
          if (mem) l.sy -= mem.curSize * 0.5 * pxu(depth) * 0.98 + 3 * U;
        });
        placed.length = 0;
        Object.keys(members).forEach((id) => {
          const m = members[id]; p3.copy(m.pic.position); const wv = tmpV.copy(p3).applyMatrix4(cam.matrixWorldInverse), depth = -wv.z; p3.project(cam);
          if (depth <= 0.5 || p3.z >= 1) return; const r = m.curSize * 0.46 * pxu(depth);
          placed.push({ x: (p3.x + 1) / 2 * W, y: (1 - p3.y) / 2 * H + r, w: 2 * r, h: 2 * r, own: null });
        });
        if (reserve.t > 0) placed.push({ x: W / 2, y: reserve.t - 2, w: W, h: reserve.t - 2, own: null });        // toolbar band
        if (reserve.b > 0) placed.push({ x: W / 2, y: H, w: W, h: reserve.b - 6, own: null });                    // caption band
        if (reserve.r > 0) placed.push({ x: W - reserve.r / 2, y: H, w: reserve.r, h: H, own: null });             // the docked key
        const order = labels.filter((l) => l.vis && !labelsOff).sort((a, b) => ((b.sel ? 100 : 0) + b.pri) - ((a.sel ? 100 : 0) + a.pri) || a.depth - b.depth);
        labels.forEach((l) => { if (!l.vis || labelsOff) setOp(l, 0); });
        const lh = 14 * U, kind = (l) => (l.kind === "src" ? "src" : "n");
        const free = (cx, y, w, h, own) => {
          if (cx - w / 2 < 2 || cx + w / 2 > W - 2 || y - h < 0 || y > H) return false;
          for (let i = 0; i < placed.length; i++) { const r = placed[i]; if (r.own === own) continue; if (cx - w / 2 < r.x + r.w / 2 + 2 && cx + w / 2 > r.x - r.w / 2 - 2 && y - h < r.y && y > r.y - r.h) return false; }
          return true;
        };
        const dims = (l) => { const dotW = l.dot ? 8 * U + 5 : 0, w0 = tw(l.nm, kind(l), U) + dotW + 6, w1 = l.sb ? Math.max(w0, tw(l.sb, "mono", U) + 6) : w0; return { w0, w1, h0: l.h0 * U, h1: l.h1 * U }; };
        /* pass 1: names */
        order.forEach((l) => {
          const d = dims(l); l.d = d; l.ok = false;
          const gap0 = l.ax ? 14 : 0, side = l.kind === "src" || l.kind === "log";
          const cxOf = (w, gap) => clamp(l.ax ? l.sx + l.ax * (gap + w / 2) : l.sx, w / 2 + 4, W - w / 2 - 4);
          const tries = l.ax ? [[0, 0], [0, 70], [-lh, 0], [lh, 0], [0, 140], [-2 * lh, 0]] : [[0, 0], [-lh, 0], [-2 * lh, 0], [lh, 0], [-3 * lh, 0], [2 * lh, 0]];
          for (const [sh, extra] of tries) {
            const gap = gap0 + extra * U, cx = cxOf(d.w0, gap);
            if (l.kind === "src" && !l.sel && Math.abs(cx - (l.sx + l.ax * (gap + d.w0 / 2))) > 36 * U) continue;   // a cabinet cropped by the screen edge: its label would drift away from it
            if (free(cx, l.sy + sh, d.w0, d.h0, l)) { l.cx = cx; l.y = l.sy + sh; l.shift = sh; l.gap = gap; l.ok = true; break; }
          }
          if (!l.ok && !side) { l.cx = cxOf(d.w0, gap0); l.y = l.sy; l.shift = 0; l.gap = gap0; l.ok = true; }           // a name is never dropped
          if (!l.ok && l.sel) { l.cx = cxOf(d.w0, gap0); l.y = l.sy; l.shift = 0; l.gap = gap0; l.ok = true; }
          if (l.ok) placed.push({ x: l.cx, y: l.y, w: d.w0, h: d.h0, own: l }); else setOp(l, 0);
        });
        /* pass 2: add the second line where there is room */
        const nameOnly = (l) => l.kind === "src" && narrow && !isCapture() && preset !== "data" && preset !== "data2";
        order.forEach((l) => {
          if (!l.ok) return; const d = l.d; l.min = true;
          if (l.sb && !nameOnly(l)) {
            const cx = clamp(l.ax ? l.sx + l.ax * (l.gap + d.w1 / 2) : l.sx, d.w1 / 2 + 4, W - d.w1 / 2 - 4);
            if (free(cx, l.y, d.w1, d.h1, l) && !(l.kind === "src" && !l.sel && Math.abs(cx - (l.sx + l.ax * (l.gap + d.w1 / 2))) > 36 * U)) {
              l.min = false; l.cx = cx; const r = placed.find((q) => q.own === l); if (r) { r.x = cx; r.w = d.w1; r.h = d.h1; }
            }
          }
        });
        order.forEach((l) => {
          if (!l.ok) return;
          l.el.classList.toggle("min", l.min); if (l.selNow !== l.sel) { l.selNow = l.sel; l.el.classList.toggle("sel", !!l.sel); }
          const lead = l.shift < 0 && !l.ax ? -l.shift : 0; if (l.lead !== lead) { l.lead = lead; l.el.classList.toggle("lead", lead > 0); l.el.style.setProperty("--lead", lead + "px"); }
          const tx = `translate(${Math.round(l.cx)}px,${Math.round(l.y)}px) translate(-50%,-100%)`; if (l.tx !== tx) { l.tx = tx; l.el.style.transform = tx; }
          setOp(l, clamp(1.5 - l.depth / 56, 0.45, 1) * (l.dim ? 0.55 : 1));
        });
        /* DOM portrait fallback (file:// taints the canvas, so the photo cannot be a WebGL texture) */
        Object.keys(members).forEach((id) => {
          const m = members[id]; if (!m.dom) { if (m.pfOn) { m.pf.style.opacity = 0; m.pfOn = false; } return; }
          p3.copy(m.pic.position); const wv = tmpV.copy(p3).applyMatrix4(cam.matrixWorldInverse), depth = -wv.z; p3.project(cam);
          if (depth <= 0.5 || p3.z >= 1) { m.pf.style.opacity = 0; m.pfOn = false; return; }
          const d = m.curSize * pxu(depth) * 200 / 256, x = (p3.x + 1) / 2 * W, y = (1 - p3.y) / 2 * H;
          m.pf.style.width = m.pf.style.height = d.toFixed(1) + "px"; m.pf.style.transform = `translate(${(x - d / 2).toFixed(1)}px,${(y - d / 2).toFixed(1)}px)`;
          m.pf.style.opacity = dimSet(id) ? 0.5 : 1; m.pfOn = true;
        });
      }
      function setOp(l, o) { const v = Math.round(o * 100) / 100; if (l.op !== v) { l.op = v; l.el.style.opacity = v; } }

      function resizeGraph() {
        renderer.setPixelRatio(renderPixelRatio()); renderer.setSize(W, H, false);
        cam.aspect = W / Math.max(1, H); cam.fov = fovFor(); cam.updateProjectionMatrix();
      }
      function goTo(pose, instant) {
        if (!pose) return;
        if (instant || reduced || isCapture()) { settingPose = true; cam.position.copy(pose.pos); if (ctl) { ctl.target.copy(pose.tgt); ctl.update(); } else cam.lookAt(pose.tgt); settingPose = false; tween = null; return; }
        tween = { t0: performance.now(), dur: 1250, fp: cam.position.clone(), ft: ctl ? ctl.target.clone() : V(0, 3, 0), tp: pose.pos, tt: pose.tgt }; schedule();
      }
      return { renderer, scene, cam, ctl, apply, layoutLabels, resizeGraph, rebuildTimeline, syncPortraits, pick, presetPose, followPose, speakerOf, restPoseFor, setReserve: (r) => { reserve = r; }, goTo, members, srcs, agentPos, tileMeshes: () => tileMeshes, tiles: () => TILES, floorMat, setHover: (id) => { hoverId = id; } };
    }

    /* ================================================================== caption, key, fallback */
    let capSig = "", srSig = "";
    function captionModel(st) {
      const job = (ctx.job) || (builtTl && builtTl.job) || null, n = builtTl ? builtTl.steps.length : (st.steps || []).length;
      const dots = (cur, gate, done) => `<span class="s3d-dots" aria-hidden="true">${(st.steps || []).map((s, i) => `<i class="${gate && i === n - 1 ? "gate" : i === cur ? "cur" : s.done || done ? "on" : ""}"></i>`).join("")}</span>`;
      const g = st.gate;
      const shownAfter = (st.after || []).filter((a) => a.shown);
      if (g.decided) {
        const a = shownAfter[shownAfter.length - 1];
        if (g.decided === "approve") {
          const aj = a && job && job.after_approval ? job.after_approval[a.k] : null;
          return { key: "ap|" + (a ? a.k : -1) + "|" + (a && a.done ? 1 : 0), head: "Approved", html: `${dots(n - 1, false, true)}<div class="s3d-cr1"><b class="s3d-ct">${a ? esc(castN(a.a)) : "Approved"}</b>${a ? `<span class="s3d-cs">${esc(sayFn(skLabel(a.sk)))}</span>` : ""}<span class="s3d-cc"><code class="${st.counters.writes ? "limited" : "ok"}">${esc(stIcon(st.counters.writes ? "limited" : "ok"))} ${esc(noWrites(st.counters.writes))}</code></span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${esc(strip(sayFn(aj && aj.txt ? scripted(aj.txt) : aj && aj.mem ? "Hypothesis, not stored: " + String(aj.mem).replace(/^<b>Lesson[^<]*<\/b>\s*/i, "") : "Approved by " + joinNames(g.needs) + ". Scripted follow-ups only; no ad-platform change was made.")))}</div>`, sr: "Approved by " + joinNames(g.needs) + (a ? ". " + castN(a.a) + ": " + a.sk : "") };
        }
        return { key: "ret", head: "Returned", html: `${dots(n - 1, false, true)}<div class="s3d-cr1"><b class="s3d-ct">Returned with a note</b><span class="s3d-cs">${esc(st.ticket ? st.ticket.id : "")}</span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${g.note ? "<b>“" + esc(g.note) + "”</b> " : ""}${esc(joinNames(g.needs))} revises ${esc(st.ticket ? st.ticket.id : "the proposal")} from the note.</div>`, sr: "Returned with a note" };
      }
      if (g.open) {
        const ps = job && job.steps[job.steps.length - 1];
        return { key: "gate", head: "Gate", html: `${dots(n - 1, true, false)}<div class="s3d-cr1"><b class="s3d-ct">Waiting for ${esc(joinNames(g.needs))}</b><span class="s3d-cs">${esc(st.ticket ? st.ticket.id : "Gate")}</span><span class="s3d-cc"><code class="running">${esc(stIcon("gated"))} waiting for a human decision</code></span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${esc(strip(sayFn(ps && ps.ptitle ? ps.ptitle : "Approve or return the proposal.")))}</div>`, sr: "Waiting for " + joinNames(g.needs) };
      }
      const workers = (st.steps || []).filter((s) => s.active);
      if (workers.length > 1) {
        const names = [];
        workers.forEach((s) => { const n = castN(s.a); if (names.indexOf(n) < 0) names.push(n); });
        const label = names.join(", ");
        const k = workers[workers.length - 1].i;
        return { key: "par|" + workers.map((s) => s.i).join(",") + "|" + workers.map((s) => s.calls.filter((c) => c.status !== "queued").length).join(","), head: names.length + " working", html: `${dots(k, false, false)}<div class="s3d-cr1"><b class="s3d-ct">${esc(label)}</b><span class="s3d-cs">working together</span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${esc(label)} are working at the same time.</div>`, sr: label + " are working.", live: k };
      }
      let k = workers.length === 1 ? workers[0].i : -1; if (k < 0) (st.steps || []).forEach((s, i) => { if (s.shown) k = i; });
      if (k < 0) { return { key: "intro", head: "Ready", html: `${dots(-1, false, false)}<div class="s3d-cr1"><b class="s3d-ct">${esc(job ? job.id + " · " + job.title : "Council ready")}</b><span class="s3d-cs">Read-only</span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${esc(job ? strip(sayFn(job.summary || "")) : "")}</div>`, sr: "Council ready" }; }
      const S = st.steps[k], step = job ? job.steps[k] : null, tlS = builtTl ? builtTl.steps[k] : null;
      if (step && typeof step.replayQuote === "string") {
        const av = '<span class="s3d-avslot" data-av="' + esc(S.a) + '"></span>';
        const source = typeof step.replaySource === "string" ? step.replaySource.trim() : "";
        const sourceLine = source ? `<div class="s3d-csource"><b>${REPLAY_LANG === "zh" ? "来源" : "SOURCE"}</b><span>${esc(source)}</span></div>` : "";
        const contextLine = PRESENT && step.replayContext ? `<div class="s3d-context">${esc(step.replayContext)}</div>` : "";
        return { key: "quote|" + k, head: "Step " + (k + 1) + " of " + n, verbatim: true, hasSource: !!source,
          html: `${dots(k, false, false)}<div class="s3d-cr1">${av}<b class="s3d-ct">${esc(castN(S.a))}</b><span class="s3d-cs">${esc(step.sk)}</span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd" data-original-speech="true">${esc(step.replayQuote)}</div>${contextLine}${sourceLine}`,
          sr: castN(S.a) + ": " + step.replayQuote + (step.replayContext ? ". " + step.replayContext : "") + (source ? ". " + (REPLAY_LANG === "zh" ? "来源" : "Source") + ": " + source : ""), live: k };
      }
      const calls = S.calls.map((c, j) => ({ c, j })).filter((x) => x.c.status !== "queued");
      const chipTxt = (x) => PRESENT ? (((REPLAY_SCENE_LABELS && REPLAY_SCENE_LABELS.sourceNames && REPLAY_SCENE_LABELS.sourceNames[x.c.s]) || x.c.tn || x.c.s)) : x.c.s + "." + x.c.tn + " " + (x.c.status === "done" ? stIcon(x.c.result) + " " + (x.c.result === "ok" ? "ok" : x.c.result) : "… running");
      const capW = isCapture() ? Math.min(980 * U, W - 40) : Math.min(860 * U, W - 20);
      let avail = capW - 28 * U - 37 * U - tw(castN(S.a), "n", U * 1.25) - tw(sayFn(S.sk), "n", U * 0.92) - 18 * U - 9 * U * 3 - 100 * U;
      const shown = []; let used = 0;
      for (let q = calls.length - 1; q >= 0; q--) { const w = tw(chipTxt(calls[q]), "mono", U * 1.1) + 18 * U; if (used + w > avail - (q > 0 ? 56 * U : 0) && shown.length) break; shown.unshift(calls[q]); used += w; }
      const more = calls.length - shown.length;
      const cc = shown.map((x) => `<code class="${PRESENT ? "source" : x.c.status === "done" ? x.c.result : "running"}">${esc(chipTxt(x))}</code>`).join("") + (more > 0 ? `<code class="running">+${more} more</code>` : "");
      const body = step ? (S.findingShown && step.f ? step.f : step.prop ? "" : step.txt || step.f || "") : "";
      const desc = step && step.prop ? (step.pid || "") + " · " + (step.ptitle || "Proposal") + ". Needs " + joinNames(step.needs || []) + "." : firstSentence(strip(sayFn(body)));
      const av = '<span class="s3d-avslot" data-av="' + esc(S.a) + '"></span>';
      const sig = k + "|" + shown.map((x) => x.j + x.c.status + x.c.result).join(",") + "|" + more + "|" + (S.findingShown ? 1 : 0);
      return { key: "s|" + sig + "|" + st.mstate[S.a], head: "Step " + (k + 1) + " of " + n, html: `${dots(k, false, false)}<div class="s3d-cr1">${av}<b class="s3d-ct">${esc(castN(S.a))}</b><span class="s3d-cs">${esc(sayFn(S.sk))}</span><span class="s3d-cc">${cc}</span><span class="s3d-cm">${esc(st.clock)}</span></div><div class="s3d-cd">${esc(desc)}</div>${PRESENT && step && step.replayNote ? `<div class="s3d-context">${esc(step.replayNote)}</div>` : ""}`, sr: "Step " + (k + 1) + " of " + n + ". " + castN(S.a) + ", " + S.sk + ". " + shown.map((x) => x.c.s + " " + x.c.tn + " " + (x.c.status === "done" ? x.c.result : "running")).join("; "), live: k };
    }
    const avs = {};
    function avatarNode(id, ms) {
      let a = avs[id]; const src = typeof avatarSrc === "function" ? avatarSrc(id) : "";
      if (!a || a.src !== src) { const t = document.createElement("div"); t.innerHTML = typeof UI !== "undefined" && UI.avatar ? UI.avatar(id, "sm", ms) : ""; const n = t.firstChild; if (n && n.querySelector) { const im = n.querySelector("img"); if (im) { im.loading = "eager"; im.decoding = "sync"; let n = 0; im.addEventListener("error", () => { if (++n <= 4) setTimeout(() => { im.src = src; }, 250 * n); }); } } a = avs[id] = { el: n, src }; }
      if (a.el && a.el.dataset && a.el.dataset.s !== ms) a.el.dataset.s = ms;
      return a.el;
    }
    function renderCaption(st) {
      cap.style.setProperty("--s3d-ang", ((st.t * 140) % 360).toFixed(2) + "deg");
      const m = captionModel(st), mk = m.key + "|" + st.clock;
      cap.classList.toggle("has-source", !!m.hasSource);
      if (mk !== capSig) { capSig = mk; cap.innerHTML = m.html; }
      if (m.verbatim) cap.removeAttribute("aria-hidden");
      const slot = cap.querySelector(".s3d-avslot");
      if (slot) { const id = slot.dataset.av, n = avatarNode(id, (st.mstate && st.mstate[id]) || "idle"); if (n && n.parentNode !== slot) { slot.appendChild(n); } }
      const sk = (m.live != null ? "s" + m.live : m.key); if (sk !== srSig) { srSig = sk; sr.textContent = m.sr; }
    }
    function renderFallback(st) {
      if (!fbReason) return;
      fb.hidden = false; cv.style.display = "none"; bar.style.display = "none"; cap.style.display = "none"; vig.style.display = "none"; lbs.style.display = "none";
      const rows = (typeof ALL !== "undefined" ? ALL : []).map((id) => { const c = CAST[id], w = st && st.mstate ? STATE_WORD[st.mstate[id]] || "Idle" : ""; return `<li><b>${esc(c.n)}</b> ${c.ai ? "AI agent" : "Human"}<i>${esc(w)}</i></li>`; }).join("");
      const m = st ? captionModel(st) : null;
      if (PRESENT) {
        const title = REPLAY_LANG === "zh" ? "3D画面暂不可用" : "3D view is unavailable";
        const peopleTitle = REPLAY_LANG === "zh" ? "参与团队" : "People involved";
        const channelsTitle = REPLAY_LANG === "zh" ? "协作方式" : "Ways of working";
        const peopleRows = ALL.map((id) => `<li><b>${esc(CAST[id].n)}</b> ${esc(CAST[id].role || "")}</li>`).join("");
        const channelRows = SRCS.map((o) => `<li><b>${esc((REPLAY_SCENE_LABELS.sourceNames || {})[o.k] || o.t)}</b></li>`).join("");
        fb.innerHTML = `<h2>${title}</h2><p>${esc(fbReason)}</p>${m ? `<div class="fbc">${m.html}</div>` : ""}<h3 class="lbl">${peopleTitle}</h3><ul>${peopleRows}</ul><h3 class="lbl">${channelsTitle}</h3><ul>${channelRows}</ul>`;
        return;
      }
      fb.innerHTML = `<h2>The 3D view is not available here</h2><p>${esc(fbReason)} The rest of the console works; this is the same replay as text.</p>${m ? `<div class="fbc">${m.html}</div>` : ""}<h3 class="lbl">Council</h3><ul>${rows}</ul><h3 class="lbl">Data layer</h3><ul>${SRCS.map((o) => { const conn = typeof CONN !== "undefined" ? CONN.find((x) => x.k === o.k) : null; const nm = (typeof UI !== "undefined" && UI.SRC_LABEL && UI.SRC_LABEL[o.k]) || o.k; return `<li><b>${esc(nm)}</b> ${esc(o.type)}<i>${esc(conn ? stWord(conn.s) : "")}</i></li>`; }).join("")}</ul>`;
    }

    /* ================================================================== sizes, modes, camera glue */
    function syncMode() {
      const m = modeNow(); if (m === mode && root.getAttribute("data-mode") === m) return;
      mode = m; root.setAttribute("data-mode", m); cv.tabIndex = m === "capture" ? -1 : 0;
      if (m === "capture") { tween = null; if (G3 && G3.ctl) G3.ctl.enabled = false; } else if (G3 && G3.ctl) G3.ctl.enabled = !customPath && riseK >= 1 && !riseTw;
      measure(true);
    }
    function measure(force) {
      const w = host.clientWidth | 0, h = host.clientHeight | 0; if (!w || !h) { W = H = 0; return false; }   // hidden host: nothing to draw until it has a size again
      const changed = w !== W || h !== H; W = w; H = h; if (changed) capSig = "";
      const wasNarrow = narrow; narrow = W < 560; root.classList.toggle("s3d-narrow", narrow && !isCapture());
      if (narrow !== wasNarrow || force) chips.querySelectorAll(".s3d-chip").forEach((b) => { const v = VIEWS.find((x) => x[0] === b.dataset.view); b.textContent = narrow ? (v[0] === "data" ? (PRESENT ? (REPLAY_LANG === "zh" ? "协作" : "Collab") : "Data") : v[0] === "follow" ? "Follow" : viewName(v)) : viewName(v); });
      U = isCapture() ? clamp(W / lerp(640, 1280, clamp(((W / H) - 0.7) / 0.8, 0, 1)), 0.8, 1.8) : narrow ? 0.95 : 1;   // capture: text scales with the frame (1920x1080 -> 1.5, 1080x1920 -> 1.7)
      root.style.setProperty("--u", U.toFixed(3));
      /* reserve the strips we draw ourselves, so the framing centres in the free area */
      const quoteMode = typeof REPLAY_CASE !== "undefined" && REPLAY_CASE.meta && REPLAY_CASE.meta.playback === "original";
      const capH = quoteMode ? Math.min(220, H * 0.43) + 24 : (narrow && !isCapture() ? 80 : 86) * U + 24 * (isCapture() ? U : 1);
      const rightKey = keyOpen && !narrow && W >= 1500 ? 304 : 0;
      const noTb = ctx.toolbar === false; root.classList.toggle("s3d-notb", noTb); root.classList.toggle("s3d-nocap", host.dataset.cap === "off");   // cap=off: no step card (the film burns its own captions there); the lane stays reserved
      const r = { t: isCapture() ? 0 : noTb ? 44 : 52, b: capH + 6, r: rightKey };   // without our toolbar the host usually puts its own chip there
      key.style.cssText = ""; key.classList.toggle("s3d-docked", !!rightKey);
      if (G3) { G3.setReserve(r); if (changed || force) G3.resizeGraph(); }
      return changed || force;
    }
    function setPresetClass() { chips.querySelectorAll(".s3d-chip").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === preset || (b.dataset.view === "data" && (preset === "data" || preset === "data2"))))); }
    function setPreset(name, opts) {
      opts = opts || {};
      if (name === "data") { dataSide = preset === "data" || preset === "data2" ? (dataSide === "data" ? "data2" : "data") : "data"; preset = dataSide; if (opts.side === 2) preset = "data2"; if (opts.side === 1) preset = "data"; }
      else if (name === "data2") { preset = "data2"; dataSide = "data2"; }
      else preset = name;
      if (preset === "free") { setPresetClass(); return; }
      lastInteract = performance.now(); orbit = null; followId = null; setPresetClass();
      if (riseK < 1 || riseTw) { setRise(1, opts.instant ? { animate: false } : {}); return; }
      if (G3 && !isCapture()) { if (preset === "follow") { followId = G3.speakerOf(state); G3.goTo(G3.followPose(followId), !!opts.instant); } else G3.goTo(G3.presetPose(preset), !!opts.instant); drawNow(); }
    }
    /* ----- rise (2D plan <-> 3D room). Embedded: animated with the wall clock like the camera presets; capture and reduced motion jump. ----- */
    const riseTarget = () => (riseTw ? riseTw.to : riseK);
    function afterRise() {
      if (G3 && G3.ctl) G3.ctl.enabled = !customPath && !isCapture() && riseK >= 1;
      if (riseK >= 1 && G3 && G3.ctl && !isCapture()) { const p = G3.presetPose(PRESETS[preset] ? preset : "overview"); if (p) G3.goTo(p, true); }
      lastInteract = performance.now(); orbit = null; setRiseClass();
    }
    function setRiseClass() { const flatNow = riseTarget() < 1; bFlat.setAttribute("aria-pressed", String(flatNow)); const sp = bFlat.querySelector("span"); if (sp) sp.textContent = flatNow ? "Lift to 3D" : "2D plan"; }
    function setRise(k, opts) {
      opts = opts || {}; k = clamp(+k || 0, 0, 1); if (k < 1) riseUsed = true;
      if (!PRESETS[preset]) preset = "overview";
      tween = null; orbit = null; followId = null; if (G3 && G3.ctl) G3.ctl.enabled = false;
      const anim = opts.animate !== false && !reduced && !isCapture() && !!G3 && visible();
      if (!anim || Math.abs(k - riseK) < 1e-3) { riseTw = null; riseK = k; afterRise(); drawNow(); return; }
      riseTw = { t0: performance.now(), dur: (opts.ms || RISE_MS) * Math.max(0.4, Math.abs(k - riseK)), from: riseK, to: k };
      setRiseClass(); schedule();
    }
    function renderRiseChip() {
      const on = (isCapture() && riseUsed) || !!ctx.riseChip; rchip.classList.toggle("on", on); if (!on) return;
      const a = sstep(0.47, 0.55, curRise), o2 = (Math.round((1 - a) * 100) / 100) + "", o3 = (Math.round(a * 100) / 100) + "";
      if (r2.style.opacity !== o2) r2.style.opacity = o2; if (r3.style.opacity !== o3) r3.style.opacity = o3;
    }
    function visible() { return W > 0 && H > 0; }
    let inDraw = false;
    function drawNow() {
      if (destroyed || !state || inDraw) return; inDraw = true;
      try {
        syncMode(); reduced = !!(ctx.reduced) || (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
        if (!W || !H) measure(true);
        if (!visible()) return;
        const tl = ctx.tl || (ctx.job && typeof Council !== "undefined" ? Council.buildTimeline(ctx.job) : null);
        renderCaption(state);
        if (tl !== builtTl) { builtTl = tl; if (G3) G3.rebuildTimeline(tl); }
        if (!G3) { renderFallback(state); return; }
        if (G3.syncPortraits) G3.syncPortraits();
        if ((ctx.toolbar === false) !== root.classList.contains("s3d-notb")) measure(true);
        inRender = true; G3.apply(state); G3.renderer.render(G3.scene, G3.cam); G3.layoutLabels(state); inRender = false;
        renderRiseChip();
      } catch (err) {
        inRender = false; if (typeof console !== "undefined") console.error("[scene3d]", err);
        if (G3) { G3 = null; fbReason = "The 3D scene stopped after an error (" + (err && err.message ? err.message : "unknown") + ")."; try { renderFallback(state); } catch (e2) { /* ignore */ } }
      }
      finally { inDraw = false; inRender = false; }
    }

    /* ----- the camera animator: preset transitions, click highlight, idle auto-orbit (embedded only, never in capture) ----- */
    function schedule() { if (!raf && !destroyed) raf = requestAnimationFrame(tick); }
    function tick(now) {
      raf = 0; if (destroyed || !G3 || isCapture()) return;
      let again = false, need = false; const ctl = G3.ctl, cam = G3.cam;
      if (riseTw) {
        const u = clamp((now - riseTw.t0) / riseTw.dur, 0, 1), e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
        riseK = lerp(riseTw.from, riseTw.to, e); need = true;
        if (u >= 1) { riseK = riseTw.to; riseTw = null; afterRise(); } else again = true;
      }
      if (tween) {
        const u = clamp((now - tween.t0) / tween.dur, 0, 1), k = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
        settingPose = true; cam.position.lerpVectors(tween.fp, tween.tp, k); if (ctl) { ctl.target.lerpVectors(tween.ft, tween.tt, k); ctl.update(); } else cam.lookAt(tween.tt); settingPose = false;
        if (u >= 1) tween = null; else again = true; need = true;
      }
      if (hlP >= 0) { hlP = (now - hlT0) / 900; if (hlP >= 1) hlP = -1; else again = true; need = true; }
      const idle = autoOrbit && !reduced && !tween && !riseTw && riseK >= 1 && !customPath && preset === "overview" && ctx.autoOrbit !== false && document.visibilityState === "visible" && visible() && now - lastInteract > 9000;
      const canIdle = autoOrbit && !reduced && !tween && !riseTw && riseK >= 1 && !customPath && preset === "overview" && ctx.autoOrbit !== false && visible();
      if (canIdle && !idle && !idleTimer && document.visibilityState === "visible") { idleTimer = setTimeout(() => { idleTimer = 0; schedule(); }, Math.max(60, 9000 - (now - lastInteract) + 30)); }
      if (idle && ctl) {
        if (!orbit) orbit = { t0: now, p: cam.position.clone(), tg: ctl.target.clone() };
        if (now - lastTick >= 40) {
          const ang = 0.22 * Math.sin((now - orbit.t0) / 1000 * 0.32), c = Math.cos(ang), s = Math.sin(ang), dx = orbit.p.x - orbit.tg.x, dz = orbit.p.z - orbit.tg.z;
          settingPose = true; cam.position.set(orbit.tg.x + dx * c - dz * s, orbit.p.y, orbit.tg.z + dx * s + dz * c); ctl.target.copy(orbit.tg); ctl.update(); settingPose = false; need = true; lastTick = now;
        }
        again = true;
      } else if (!idle) orbit = null;
      if (need) drawNow();
      if (again) schedule();
    }
    let hlT0 = 0;

    /* ----- events ----- */
    const onChips = (e) => { const b = e.target.closest(".s3d-chip"); if (b) setPreset(b.dataset.view); };
    chips.addEventListener("click", onChips);
    bKey.addEventListener("click", () => { keyOpen = !keyOpen; keyTouched = true; key.hidden = !keyOpen; bKey.setAttribute("aria-pressed", String(keyOpen)); measure(true); drawNow(); });
    bFlat.addEventListener("click", () => { setRise(riseTarget() < 1 ? 1 : 0); });
    bLab.addEventListener("click", () => { labelsOff = !labelsOff; root.classList.toggle("s3d-nolabels", labelsOff); bLab.setAttribute("aria-pressed", String(!labelsOff)); drawNow(); });
    let down = null;
    const onDown = (e) => { down = [e.clientX, e.clientY]; lastInteract = performance.now(); orbit = null; };
    const onUp = (e) => {
      lastInteract = performance.now();
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5 || !G3 || isCapture()) { down = null; return; } down = null;
      const h = G3.pick(e); if (!h) return;
      if (h.id) { hlId = h.id; hlT0 = performance.now(); hlP = 0; if (ctx.select) ctx.select(h.id); schedule(); }
      else if (h.tile != null) { const tile = G3.tiles()[h.tile]; if (tile && tile.kind !== "decision" && ctx.seek) ctx.seek(Math.max(0, tile.t)); }
    };
    const onMove = (e) => { if (e.buttons || !G3 || isCapture()) return; const h = G3.pick(e), id = h && h.id ? h.id : null; cv.style.cursor = h ? "pointer" : ""; if (id !== hoverId) { hoverId = id; G3.setHover(id); drawNow(); } };
    const onDbl = (e) => { if (!G3 || isCapture()) return; const h = G3.pick(e); if (h && h.id && ctx.openMember) ctx.openMember(h.id); };
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isCapture()) return;
      const k = e.key;
      if (k >= "1" && k <= "5") { setPreset(VIEWS[+k - 1][0]); e.preventDefault(); e.stopPropagation(); }
      else if ((k === "l" || k === "L")) { bLab.click(); e.stopPropagation(); }
      else if ((k === "p" || k === "P")) { bFlat.click(); e.stopPropagation(); }
      else if (G3 && G3.ctl && (k === "ArrowLeft" || k === "ArrowRight" || k === "ArrowUp" || k === "ArrowDown" || k === "+" || k === "=" || k === "-")) {
        e.preventDefault(); e.stopPropagation(); const cam = G3.cam, tg = G3.ctl.target, off = cam.position.clone().sub(tg);
        if (k === "+" || k === "=" || k === "-") off.multiplyScalar(k === "-" ? 1.12 : 0.89); else { const a = (k === "ArrowLeft" ? 1 : k === "ArrowRight" ? -1 : 0) * 0.14, c = Math.cos(a), s = Math.sin(a); off.set(off.x * c - off.z * s, off.y + (k === "ArrowUp" ? -1.2 : k === "ArrowDown" ? 1.2 : 0), off.x * s + off.z * c); }
        cam.position.copy(tg).add(off); settingPose = true; G3.ctl.update(); settingPose = false; preset = "free"; setPresetClass(); tween = null; lastInteract = performance.now(); drawNow();
      }
    };
    cv.addEventListener("pointerdown", onDown); cv.addEventListener("pointerup", onUp); cv.addEventListener("pointermove", onMove); cv.addEventListener("dblclick", onDbl); cv.addEventListener("keydown", onKey);
    if (G3 && G3.ctl) {
      G3.ctl.addEventListener("start", () => { if (preset !== "free") { preset = "free"; tween = null; setPresetClass(); } lastInteract = performance.now(); orbit = null; });
      G3.ctl.addEventListener("change", () => { if (settingPose || inRender || inDraw) return; drawNow(); });
      G3.ctl.addEventListener("end", () => { lastInteract = performance.now(); orbit = null; });
    }
    const onCtxLost = (e) => { e.preventDefault(); fbReason = "The graphics context was lost. It comes back by itself in most browsers; if not, reload the page."; if (state) renderFallback(state); };
    const onCtxBack = () => { fbReason = ""; fb.hidden = true; cv.style.display = ""; bar.style.display = ""; cap.style.display = ""; vig.style.display = ""; lbs.style.display = ""; drawNow(); };
    if (G3) { G3.renderer.domElement.addEventListener("webglcontextlost", onCtxLost); G3.renderer.domElement.addEventListener("webglcontextrestored", onCtxBack); }
    let ro = null; if (typeof ResizeObserver !== "undefined") { ro = new ResizeObserver(() => { if (destroyed) return; if (measure(false)) { if (G3 && !isCapture() && riseK >= 1 && !riseTw) { if (preset === "follow") G3.goTo(G3.followPose(followId || G3.speakerOf(state)), true); else if (preset !== "free") G3.goTo(G3.presetPose(preset), true); } drawNow(); } }); ro.observe(host); }
    const mq = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : null;
    const onMq = () => { reduced = !!(ctx.reduced) || (mq && mq.matches); drawNow(); }; if (mq && mq.addEventListener) mq.addEventListener("change", onMq);
    if (document.fonts && document.fonts.load) ["700 12px Lato", "400 12px Lato", "400 10px \"JetBrains Mono\""].forEach((f) => readyPromises.push(document.fonts.load(f).then(() => { _tw.clear(); capSig = ""; if (!destroyed) drawNow(); }).catch(() => {})));
    if (document.fonts && document.fonts.ready) readyPromises.push(document.fonts.ready.then(() => { _tw.clear(); capSig = ""; if (!destroyed) drawNow(); }));

    /* ----- init ----- */
    mode = modeNow(); root.setAttribute("data-mode", mode);
    if (!keyTouched && host.clientWidth >= 1500 && mode !== "capture") { keyOpen = true; key.hidden = false; bKey.setAttribute("aria-pressed", "true"); }
    measure(true); setPresetClass();
    if (G3) { const pz = G3.presetPose("overview"); if (pz) G3.goTo(pz, true); }
    lastInteract = performance.now();
    if (G3 && G3.ctl && typeof matchMedia === "function" && matchMedia("(pointer:coarse)").matches) { cv.style.touchAction = "pan-y"; G3.renderer.domElement.style.touchAction = "pan-y"; }

    const inst = {
      host, root,
      update(st, c) { if (destroyed) return; if (c) ctx = c; state = st; if (!W) measure(true); drawNow(); if (G3 && !raf) { if (!isCapture() && !reduced && autoOrbit) schedule(); } },
      setPlaybackQuality,
      resize() { measure(true); if (G3 && !isCapture() && riseK >= 1 && !riseTw) { if (preset === "follow") G3.goTo(G3.followPose(followId || G3.speakerOf(state)), true); else if (preset !== "free") G3.goTo(G3.presetPose(preset), true); } drawNow(); },
      renderNow() { drawNow(); },
      setPreset, getPreset: () => preset,
      setCameraPath(fn) { customPath = typeof fn === "function" ? fn : null; if (G3 && G3.ctl) G3.ctl.enabled = !customPath && !isCapture() && riseK >= 1 && !riseTw; tween = null; drawNow(); },
      setRise, getRise: () => riseK,
      setAutoOrbit(v) { autoOrbit = !!v; },
      whenReady() { return Promise.all(readyPromises); },
      info() { return { webgl: !!G3, reason: fbReason, mode, preset, W, H, narrow, reduced, labels: G3 ? Object.keys(G3.members).length : 0 }; },
      three() { return G3; },
      destroy() {
        destroyed = true; if (raf) cancelAnimationFrame(raf); raf = 0; if (idleTimer) clearTimeout(idleTimer); idleTimer = 0;
        if (ro) ro.disconnect(); if (mq && mq.removeEventListener) mq.removeEventListener("change", onMq);
        cv.removeEventListener("pointerdown", onDown); cv.removeEventListener("pointerup", onUp); cv.removeEventListener("pointermove", onMove); cv.removeEventListener("dblclick", onDbl); cv.removeEventListener("keydown", onKey);
        if (G3) { try { if (G3.ctl) G3.ctl.dispose(); G3.renderer.domElement.removeEventListener("webglcontextlost", onCtxLost); G3.renderer.domElement.removeEventListener("webglcontextrestored", onCtxBack); G3.scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) { const ms = Array.isArray(o.material) ? o.material : [o.material]; ms.forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); }); } }); G3.renderer.dispose(); if (G3.renderer.forceContextLoss) G3.renderer.forceContextLoss(); } catch (e) { /* ignore */ } }
        if (root.parentNode) root.parentNode.removeChild(root);
      }
    };
    return inst;
  }

  /* ------------------------------------------------------------------ public view */
  const view = {
    id: "scene3d", label: "3D",
    presets: VIEWS.map((v) => v[0]),
    mount(host, ctx) { if (cur) { try { cur.destroy(); } catch (e) { /* ignore */ } cur = null; } cur = createInstance(host, ctx); if (view._pendingPath) { cur.setCameraPath(view._pendingPath); } if (view._pendingRise) { cur.setRise(view._pendingRise[0], Object.assign({}, view._pendingRise[1], { animate: false })); view._pendingRise = null; } },
    update(state, ctx) { if (cur) cur.update(state, ctx); },
    setPlaybackQuality(speed, playing) { if (cur) cur.setPlaybackQuality(speed, playing); },
    resize() { if (cur) cur.resize(); },
    renderNow() { if (cur) cur.renderNow(); },
    destroy() { if (cur) { cur.destroy(); cur = null; } },
    setPreset(name, opts) { if (cur) cur.setPreset(name, opts); },
    getPreset() { return cur ? cur.getPreset() : null; },
    setCameraPath(fn) { if (cur) cur.setCameraPath(fn); else view._pendingPath = fn; },
    setRise(k, opts) { if (cur) cur.setRise(k, opts); else view._pendingRise = [k, opts]; },
    getRise() { return cur ? cur.getRise() : view._pendingRise ? view._pendingRise[0] : 1; },
    setAutoOrbit(v) { if (cur) cur.setAutoOrbit(v); },
    whenReady() { return cur ? cur.whenReady() : Promise.resolve(); },
    info() { return cur ? cur.info() : null; },
    _three() { return cur ? cur.three() : null; }
  };
  FV.scene3d = view;
})(typeof window !== "undefined" ? window : globalThis);
