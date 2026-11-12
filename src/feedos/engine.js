/* FeedOS replay engine (classic script, global `Council`). Needs data.js first (CAST, ALL, SK, say) and jobs/jobs.js (window.JOBS).
 *
 * One rule: every panel renders Council.stateAt(tl, t, decision). No timers, no Math.random, no DOM here, so the console and the
 * video capture (window.__renderAt) show exactly the same thing for the same t.
 *
 *   const tl = Council.buildTimeline(job)          // job = one entry of window.JOBS (jobs/job-NNN.json)
 *   const st = Council.stateAt(tl, t, decision)    // decision = null | {type:"approve"|"return", at:<seconds>, note?:string}
 *
 * Time t is replay seconds from 0 to tl.duration (the gate opens at tl.gateAt). After a decision the scripted consequences
 * (job.after_approval) play from decision.at. Read the shape of the state object in stateAt() below. */
(function (g) {
  "use strict";
  const K = { lead: .6, head: .6, textOnly: 1.0, stagger: .55, run: .9, find: .9, propExtra: .9, gap: .35, after: 1.7, tail: 1.2 };
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

  function minutes(label) {
    if (typeof label === "number" && Number.isFinite(label)) return label;
    const m = /(\d+):(\d+)\s*(AM|PM)/i.exec(label || "");
    if (!m) return null;
    let h = (+m[1]) % 12; if (m[3].toUpperCase() === "PM") h += 12;
    let x = h * 60 + (+m[2]); if (x < 12 * 60) x += 24 * 60; // AM belongs to the morning after
    return x;
  }
  function fmt(min) {
    if (typeof g.REPLAY_CLOCK_FMT === "function") return g.REPLAY_CLOCK_FMT(min);
    min = Math.round(min) % 1440; let h = Math.floor(min / 60); const m = min % 60;
    const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
    return h + ":" + String(m).padStart(2, "0") + " " + ap;
  }
  const calls = (st) => st.calls || [];
  const nOf = (c) => c.n || 1;
  /* A write-mode call counts as a write only when it was not denied: a blocked call never changed anything. */
  const isWrite = (x) => !!x.w && x.st !== "blocked";

  const timedStep = (st) => typeof st.rs === "number" && typeof st.re === "number";
  function callWindows(st, start, end) {
    const cs = calls(st);
    const timed = cs.some((c) => typeof c.rs === "number");
    if (timed) {
      return cs.map((c, j) => {
        const s = typeof c.rs === "number" ? c.rs : start;
        const e = typeof c.re === "number" ? c.re : Math.min(end, s + K.run);
        return { j, start: s, end: Math.max(e, s + 0.05), n: nOf(c), st: c.st, s: c.s, tn: c.tn, w: c.m === "w" };
      });
    }
    const usable = Math.max(end - start - Math.min(0.35, (end - start) * 0.15), 0.05);
    const slot = cs.length ? usable / cs.length : usable;
    return cs.map((c, j) => {
      const s = start + j * slot;
      return { j, start: s, end: Math.min(start + usable, s + Math.max(0.05, Math.min(K.run, slot * 0.85))), n: nOf(c), st: c.st, s: c.s, tn: c.tn, w: c.m === "w" };
    });
  }
  /** Static timing of a job: when every step, call and finding appears.
   *  Steps with rs/re keep the wall-clock overlaps. The others play in order. */
  function buildTimeline(job) {
    const steps = []; let t = K.lead;
    const wall = job.steps.some(timedStep);
    job.steps.forEach((st, i) => {
      if (wall) {
        const start = typeof st.rs === "number" ? st.rs : t;
        const end = Math.max(typeof st.re === "number" ? st.re : start + 0.2, start + 0.05);
        const cl = callWindows(st, start, end);
        steps.push({ i, a: st.a, start, end, calls: cl, findAt: st.f ? end : null, min: typeof st.min === "number" ? st.min : minutes(st.t), prop: !!st.prop, timed: true });
        t = Math.max(t, end + K.gap);
        return;
      }
      const cs = calls(st), n = cs.length, head = n ? K.head : K.textOnly;
      const cl = cs.map((c, j) => { const s = t + K.head + j * K.stagger; return { j, start: s, end: s + K.run, n: nOf(c), st: c.st, s: c.s, tn: c.tn, w: c.m === "w" }; });   // w = write-mode call; whether it counts as a write is decided by isWrite()
      const lastEnd = n ? cl[n - 1].end : t + head;
      const findAt = st.f ? lastEnd + .1 : null;
      let end = (findAt != null ? findAt + K.find : lastEnd + .2); if (st.prop) end += K.propExtra;
      steps.push({ i, a: st.a, start: t, end, calls: cl, findAt, min: typeof st.min === "number" ? st.min : minutes(st.t), prop: !!st.prop, timed: false });
      t = end + K.gap;
    });
    const last = steps[steps.length - 1];
    const lastEnd = steps.reduce((max, step) => Math.max(max, step.end), 0);
    const gateAt = job.steps.length && job.steps[job.steps.length - 1].prop ? last.end : null;
    const duration = (gateAt != null ? gateAt : lastEnd) + K.tail;
    // scrubber marks and a histogram (events per bin)
    const marks = [];
    steps.forEach((S) => {
      marks.push({ t: S.start, kind: "step", a: S.a, i: S.i });
      S.calls.forEach((c) => marks.push({ t: c.start, kind: "call", a: S.a, i: S.i, st: c.st, n: c.n }));
      if (S.findAt != null) marks.push({ t: S.findAt, kind: "finding", a: S.a, i: S.i, k: job.steps[S.i].k || "" });
    });
    if (gateAt != null) marks.push({ t: gateAt, kind: "gate" });
    const totals = { calls: 0, ok: 0, limited: 0, blocked: 0, writes: 0 };
    steps.forEach((S) => S.calls.forEach((c) => { totals.calls += c.n; totals[c.st] += c.n; if (isWrite(c)) totals.writes += c.n; }));
    return { job, steps, gateAt, duration, marks, totals, K };
  }

  function histogram(tl, bins) {
    const h = new Array(bins).fill(0);
    tl.marks.forEach((m) => { const b = clamp(Math.floor(m.t / tl.duration * bins), 0, bins - 1); h[b] += m.kind === "call" ? 1 : 2; });
    return h;
  }

  /** Time the playhead should jump to for a given step index (for "jump to" buttons). */
  const stepStart = (tl, i) => tl.steps[i].start;

  const CRITERIA = [
    { id: "evidence", label: "Evidence pulled", hint: "At least one read returned data" },
    { id: "second", label: "Independent source checked", hint: "A system other than Femini and its database returned data" },
    { id: "gaps", label: "Gaps named", hint: "At least one call was limited or blocked, and the run says so" },
    { id: "premortem", label: "Pre-mortem filed", hint: "Critic argued how it fails" },
    { id: "guard", label: "No writes made", hint: "No write tool was called without a human decision" },
    { id: "decision", label: "Human decision", hint: "A person approved or returned the proposal" }
  ];

  /** Everything the UI needs at replay time t. */
  function stateAt(tl, t, dec) {
    const job = tl.job, K_ = tl.K;
    const c = { calls: 0, ok: 0, limited: 0, blocked: 0, writes: 0, inflight: 0, delivered: 0, dead: 0, offset: 0 };
    const okServers = new Set();
    const first = {}; const mstate = {}; ALL.forEach((id) => { mstate[id] = "idle"; });
    const lessons = { used: [], drafted: [], stored: [] };   // lesson ids by what has happened so far in this run (job fields lu, la, ls)
    const addL = (k, ids) => (ids || []).forEach((id) => { if (lessons[k].indexOf(id) < 0) lessons[k].push(id); });
    const steps = tl.steps.map((S) => {
      const step = job.steps[S.i];
      const shown = t >= S.start, done = t >= S.end, active = shown && !done;
      const cl = S.calls.map((x) => {
        const started = t >= x.start, finished = t >= x.end;
        if (started) c.offset += x.n;
        if (finished) { c.calls += x.n; c[x.st] += x.n; if (x.st === "ok") okServers.add(x.s); if (x.st === "blocked") c.dead += x.n; else c.delivered += x.n; if (isWrite(x)) c.writes += x.n; }
        else if (started) c.inflight += x.n;
        return { j: x.j, status: finished ? "done" : started ? "running" : "queued", result: x.st, u: clamp((t - x.start) / (x.end - x.start), 0, 1), n: x.n, s: x.s, tn: x.tn, w: isWrite(x) };
      });
      const findingShown = S.findAt != null && t >= S.findAt;
      if (shown) addL("used", step.lu);
      if (done) addL("drafted", step.la);
      if (shown) { first[S.a] = first[S.a] == null ? S.start : first[S.a]; mstate[S.a] = "done"; }
      if (active) mstate[S.a] = "working";
      else if (done && cl.some((x) => x.result === "blocked" || x.result === "limited")) mstate[S.a] = "blocked";
      const agentWork = !step.interaction_id;
      return { i: S.i, a: S.a, sk: step.sk, shown, active, done, typing: S.timed ? active && agentWork : active && t < S.start + K_.head && S.calls.length > 0,
               calls: cl, findingShown, prop: S.prop, start: S.start, end: S.end };
    });
    // human gate
    const gateOpen = tl.gateAt != null && t >= tl.gateAt;
    const decided = dec && gateOpen && t >= dec.at ? dec.type : null;
    const propStep = job.steps[job.steps.length - 1];
    const needs = (propStep && propStep.needs) || [];
    if (gateOpen && !decided) needs.forEach((id) => { mstate[id] = "waiting"; });
    if (decided) needs.forEach((id) => { mstate[id] = "done"; });
    // Optional recorded follow-up events after a review decision
    const after = [];
    if (decided === "approve") {
      (job.after_approval || []).forEach((st, k) => {
        const start = dec.at + .5 + k * K_.after, end = start + K_.after - .3;
        const shown = t >= start, done = t >= end, active = shown && !done;
        const lesson = !!st.mem;            // a drafted lesson (hypothesis, not stored): the member ring shows "learned"
        after.push({ k, a: st.a, sk: st.sk, shown, active, done, start, end, lesson });
        if (shown) addL("stored", st.ls);
        if (shown) mstate[st.a] = (lesson && done) ? "learned" : (active ? "working" : "done");
      });
    }
    if (decided === "return" && needs.length) { mstate[needs[0]] = "working"; }
    // charter (derived, honest: unmet criteria stay visible)
    const met = {
      evidence: c.ok > 0,
      second: [...okServers].some((sv) => sv !== "femini" && sv !== "postgres"),   // Femini and its Postgres are the same data: not independent
      gaps: c.limited + c.blocked > 0,
      premortem: false, guard: c.writes === 0, decision: !!decided
    };
    const charter = CRITERIA.map((x) => ({ id: x.id, label: x.label, hint: x.hint, met: !!met[x.id] }));
    // clock: smooth between the recorded step labels
    let min = tl.steps[0].min;
    for (let i = 0; i < tl.steps.length; i++) {
      const S = tl.steps[i], N = tl.steps[i + 1];
      if (t >= S.start) { min = S.min; if (!job.discreteClock && N && N.min != null && S.min != null) min = S.min + clamp((t - S.start) / (N.start - S.start), 0, 1) * (N.min - S.min); }
    }
    if (gateOpen && decided) min = (tl.steps[tl.steps.length - 1].min || min) + (t - dec.at) * .5;
    const ticket = propStep && propStep.prop ? { id: propStep.pid || "P-1", title: propStep.ptitle || "Proposal",
      state: decided === "approve" ? "approved" : decided === "return" ? "returned" : gateOpen ? "needs review" : steps[steps.length - 1].shown ? "drafting" : "not started" } : null;
    const active = steps.find((s) => s.active) || after.find((s) => s.active) || null;
    return { t, duration: tl.duration, clock: fmt(min || 0), steps, after, counters: c, mstate, charter, lessons,
             charterMet: charter.filter((x) => x.met).length, gate: { open: gateOpen, decided, needs, note: dec && dec.note || "", at: tl.gateAt, decidedAt: decided ? dec.at : null },
             ticket, active: active ? active.a : null, done: t >= tl.duration && (tl.gateAt == null || !!decided) };
  }

  /** "2 ok, 2 limited" for one step's calls (n-aware, zero counts left out). */
  function countsOf(step) {
    const n = { ok: 0, limited: 0, blocked: 0 };
    calls(step).forEach((x) => { n[x.st] += nOf(x); });
    return ["ok", "limited", "blocked"].filter((k) => n[k]).map((k) => n[k] + " " + k).join(", ");
  }

  /** Tasks of one member at a replay state: what they did, are doing, wait for, or are blocked on (from the job + the skill statuses). */
  function tasksOf(tl, st, id) {
    const job = tl.job, out = [], add = (s, t, note) => out.push({ st: s, t: say(t), note: note ? say(note) : "" });
    st.steps.filter((s) => s.a === id).forEach((s) => {
      const step = job.steps[s.i];
      const label = step.prop ? "Assemble " + (step.pid || "the proposal") + " for " + (step.needs || []).map((x) => CAST[x] ? CAST[x].n : x).join(" and ") : step.sk;
      const nc = calls(step).reduce((a, x) => a + nOf(x), 0);
      const sk = (SK[id] || []).find((x) => x.n === step.sk);   // a skill that is only planned or demo-only is said so on the board
      const note = step.prop ? (s.done ? "Waiting for the human decision" : "")
        : nc ? nc + " call" + (nc > 1 ? "s" : "") + (s.done ? ": " + countsOf(step) : "")
        : sk && sk.s === "plan" ? "Scripted message; the skill is planned" : sk && sk.s === "demo" ? "Scripted message in the replay" : "";
      out.push({ st: s.done ? "done" : s.active ? "now" : "next", t: say(label), note: say(note) });
    });
    const needs = st.gate.needs;
    if (needs.indexOf(id) >= 0 && st.ticket) {
      if (st.gate.decided) add("done", "Review " + st.ticket.id, st.gate.decided === "approve" ? "Approved" : "Returned with a note");
      else if (st.gate.open) add("now", "Review " + st.ticket.id + " and decide", "Review the recorded proposal and decide");
      else add("next", "Review " + st.ticket.id);
    }
    if (st.gate.decided === "return" && needs[0] === id && st.ticket) add("now", "Revise " + st.ticket.id + " from the note", st.gate.note);
    st.after.filter((s) => s.a === id).forEach((s) => add(s.done ? "done" : s.active ? "now" : "next", s.sk, s.lesson ? "Hypothesis, not stored" : "Scripted; nothing is executed"));
    // standing items from the skill list: honest backlog and blockers
    (SK[id] || []).forEach((s) => {
      if (s.s === "blocked" || s.s === "auth" || s.s === "soon" || s.s === "plan") add("blocked", s.n, s.s === "auth" ? "Needs authorization" : s.s === "soon" ? "Tool not released yet" : s.s === "plan" ? "Planned, not built" : "Not enabled for this account");
    });
    (SK[id] || []).filter((s) => s.s === "ready" && !s.ran && !job.steps.some((x) => x.a === id && x.sk === s.n)).slice(0, 2).forEach((s) => add("next", s.n, "Ready, not run yet"));
    // dedupe by title+status
    const seen = new Set(); return out.filter((x) => { const k = x.st + "|" + x.t; if (seen.has(k)) return false; seen.add(k); return true; });
  }

  /** Counts for headlines and the video: n-aware, so a "x4" chip counts as 4 calls. */
  function summary(job) {
    const tl = buildTimeline(job), ids = (k) => [].concat.apply([], job.steps.concat(job.after_approval || []).map((s) => s[k] || []));
    return { calls: tl.totals.calls, ok: tl.totals.ok, limited: tl.totals.limited, blocked: tl.totals.blocked, writes: tl.totals.writes, entries: job.steps.reduce((a, s) => a + calls(s).length, 0),
             lessonsUsed: ids("lu").length, lessonsDrafted: ids("la").length };
  }

  g.Council = { K, CRITERIA, buildTimeline, stateAt, tasksOf, histogram, stepStart, summary, fmt, minutes,
    jobs: () => (g.JOBS || []), job: (id) => (g.JOBS || []).find((j) => j.id === id) || null };
})(typeof window !== "undefined" ? window : globalThis);
