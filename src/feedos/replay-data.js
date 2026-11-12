/* Replay-case adapter for the embedded 3D scene. REPLAY_CASE is injected by replay/scene.py. */
(function (g) {
  "use strict";
  const C = g.REPLAY_CASE || {};
  const people = C.people || [], events = C.events || [], lang = C.lang || "en";
  const PRESENT = !!(C.meta && C.meta.presentation_mode === true);
  const sourceNames = (C.presentation && C.presentation.sourceLabels) || Object.fromEntries((C.sources || []).map((s) => [s.id, s.name]));
  const PU = (C.presentation && C.presentation.ui) || {}, ZH = lang === "zh";
  // Where a back-office change was discussed: its own caption line, never next to the person who made the change (someone else may have said it).
  const discussionNote = (e) => {
    if (!PRESENT || !Array.isArray(e.discussion)) return "";
    const head = (PU.discussionTitle || "") + (ZH ? "\uFF1A" : ": ");
    if (!e.discussion.length) return head + (PU.discussionNone || "");
    return head + e.discussion.map((d) => `${d.tag} · ${d.who} · ${d.when}${ZH ? "\uFF1A\u300C" : ": \u201C"}${d.quote}${ZH ? "\u300D" : "\u201D"}`).join(ZH ? "\uFF1B" : "; ");
  };
  const hex = (value) => {
    if (/^#[0-9a-f]{6}$/i.test(value || "")) return value;
    const cv = document.createElement("canvas"), cx = cv.getContext("2d");
    cx.fillStyle = "#7c5cff"; cx.fillStyle = value || "#7c5cff";
    const rgb = cx.fillStyle.match(/\d+/g);
    if (!rgb || rgb.length < 3) return "#7c5cff";
    return "#" + rgb.slice(0, 3).map((n) => (+n).toString(16).padStart(2, "0")).join("");
  };
  const avatarInitials = (p) => {
    let name = String(p.name || p.short || p.id || "?").trim();
    name = name.replace(/^0x(?=[a-z0-9])/i, "").replace(/^fmb[-_]/i, "");
    if (/[\u3400-\u9fff]/.test(name)) return Array.from(name)[0] || "?";
    const words = name.split(/[\s._-]+/).filter(Boolean);
    return (words.length > 1 ? words.slice(0, 2).map((word) => Array.from(word)[0]).join("") : Array.from(words[0] || "?")[0]).toUpperCase();
  };
  const xml = (value) => String(value).replace(/[&<>\"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&apos;" })[ch]);
  const avatar = (p, initials) => {
    const color = hex(p.color), size = initials.length > 1 ? 34 : 48;
    // Canvas source cropping needs explicit intrinsic dimensions, not only a viewBox.
    return "data:image/svg+xml," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="48" fill="${color}"/><text x="48" y="61" font-size="${size}" font-weight="700" text-anchor="middle" fill="#fff" font-family="sans-serif">${xml(initials)}</text></svg>`);
  };
  const CAST = {};
  people.forEach((p) => {
    const bot = p.kind !== "human", initials = avatarInitials(p);
    CAST[p.id] = { n: p.short || p.name || p.id, full: p.name || p.short || p.id, avatar: initials, ai: bot ? 1 : 0,
      c: hex(p.color), role: p.role || p.tag || (bot ? "Bot" : "Person"), blurb: p.role || "", photo: avatar(p, initials), dev: bot ? "rack" : "laptop" };
  });
  const AI_IDS = people.filter((p) => p.kind !== "human").map((p) => p.id);
  const HU_IDS = people.filter((p) => p.kind === "human").map((p) => p.id);
  const ALL = people.map((p) => p.id);
  const recipients = events.flatMap((e) => Array.isArray(e.to) ? e.to : e.to ? [e.to] : []);
  const phoneId = recipients.find((id) => HU_IDS.includes(id)) || HU_IDS[0] || null;
  if (phoneId && CAST[phoneId]) CAST[phoneId].dev = "phone";
  const labels = { ask: ["Ask", "提问"], create: ["Create", "新建"], assign: ["Assign", "指派"], chat: ["Message", "发言"], notify: ["Notify", "通知"], comment: ["Comment", "评论"], label: ["Label", "改标签"], pr: ["Pull request", "提交代码"], review: ["Review", "评审"], merge: ["Merge", "合并"], close: ["Close", "关闭"], change: ["Change", "调整"] };
  const sources = C.sources || [];
  const statusText = { ok: ["✓", "Connected"], limited: ["◐", "Limited"], blocked: ["⛔", "Blocked"], auth: ["🔑", "Needs authorization"], plan: ["○", "Planned"] };
  const ST = {};
  Object.keys(statusText).forEach((k) => { ST[k] = [statusText[k][0], lang === "zh" ? ({ ok: "已连接", limited: "受限", blocked: "受阻", auth: "需授权", plan: "计划中" })[k] : statusText[k][1]]; });
  const CONN = sources.map((s) => ({ k: s.id, n: s.name, s: PRESENT ? "ok" : s.status === "planned" ? "plan" : s.status }));
  const sourceKinds = { github: "dish", slack: "cab", hubspot: "cab", femini: "drum", email: "cab", meeting: "cab" };
  const positions = { github: [-16.6, 9.2], slack: [-16.6, 3.1], email: [-16.6, -3.1], meeting: [-16.6, -9.2], hubspot: [16.6, 9.2], femini: [16.6, 3.1] };
  const sceneSources = sources.map((s) => {
    const [x, z] = positions[s.id] || [s.id === "github" ? -16.6 : 16.6, -3.2];
    return { k: s.id, kind: sourceKinds[s.id] || "cab", type: s.name, x, z, st: PRESENT ? "ok" : s.status === "planned" ? "plan" : s.status };
  });
  const actionName = (e) => PRESENT ? (e.presentation_tag || sourceNames[e.src] || e.title || "") : e.original ? ((C.ui && C.ui.original) || "Original message") : e.action || (labels[e.kind] || [e.kind || "Event", e.kind || "事件"])[lang === "zh" ? 1 : 0];
  const job = { id: String(C.meta && C.meta.no || "replay"), title: C.meta && C.meta.title || "Campaign replay", discreteClock: true, steps: events.map((e) => ({
    a: e.actor, sk: actionName(e), t: new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", { timeZone: "Asia/Shanghai", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: lang !== "zh" }).format(new Date(e.ts)), min: Date.parse(e.ts) / 60000, f: false, needs: [],
    txt: PRESENT ? (e.presentation_context || e.text || e.title || "") : "",
    calls: (e.srcs && e.srcs.length ? e.srcs : [e.src]).filter((s) => sceneSources.some((x) => x.k === s)).map((s) => ({
      s, tn: PRESENT ? (sourceNames[s] || s) : actionName(e), st: "ok", m: "r", n: 1
    })),
    replayTo: e.to || [], replayTitle: e.quote || (e.original ? e.original.text : e.title || ""), replayQuote: PRESENT ? (e.quote || (e.original && e.original.text) || null) : e.original ? e.original.text : null, replaySpeaker: PRESENT ? null : e.original ? e.original.speaker_label : null, replaySource: PRESENT ? "" : e.source_detail || "", replayContext: PRESENT ? (e.presentation_context || "") : "", replayNote: discussionNote(e)
  })) };
  g.REPLAY_LANG = lang;
  g.REPLAY_PRESENTATION = PRESENT;
  g.CAST = CAST; g.AI_IDS = AI_IDS; g.HU_IDS = HU_IDS; g.ALL = ALL;
  g.ST = ST; g.SK = {}; g.CONN = CONN; g.HEALTH = [];
  g.chip = (key) => { const item = ST[key] || ["", key]; return `<span class="st ${key}"><span aria-hidden="true">${item[0]}</span>${item[1]}</span>`; };
  g.say = (s) => String(s == null ? "" : s);
  g.avatarSrc = (id) => CAST[id] ? CAST[id].photo : "";
  g.REPLAY_SCENE = { sources: sceneSources, aiIds: AI_IDS, humanSeats: null, hubId: AI_IDS[0] || HU_IDS[0] || null, phoneId };
  g.REPLAY_CLOCK_FMT = (minutes) => {
    const date = new Date(minutes * 60000), tz = g.REPLAY_TZ || "Asia/Shanghai";
    const parts = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", { timeZone: tz, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: lang !== "zh" }).format(date);
    return parts;
  };
  g.JOBS = [job];
  g.REPLAY_SCENE_JOB = job;
  g.REPLAY_SCENE_LABELS = { sourceNames, presentation: PRESENT };
})(window);
