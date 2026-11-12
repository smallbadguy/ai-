/* Shared UI helpers (classic script, global `UI`). Needs data.js first. Markup matches css/brand.css. */
(function (g) {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const AISVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .8l1.1 3.1 3.1 1.1-3.1 1.1L6 9.2 4.9 6.1 1.8 5l3.1-1.1z" fill="#fff"/></svg>';
  const HUSVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="4" r="2.1" fill="#fff"/><path d="M2 10.6c0-2.2 1.8-3.5 4-3.5s4 1.3 4 3.5z" fill="#fff"/></svg>';
  /** Portrait with state ring + AI/human badge. size: "" | "sm" | "lg" | "xl". state: idle|working|waiting|blocked|learned|done */
  function avatar(id, size, state) {
    const c = CAST[id];
    return `<span class="av ${size || ""}" data-s="${state || "idle"}" style="--hue:${c.c}"><img src="${esc(avatarSrc(id))}" alt="${esc(c.full)}, ${c.ai ? "AI agent" : "human"}" width="96" height="96" loading="${UI.eager ? "eager" : "lazy"}" decoding="${UI.eager ? "sync" : "async"}"><span class="badge ${c.ai ? "ai" : "hu"}" title="${c.ai ? "AI agent" : "Human"}">${c.ai ? AISVG : HUSVG}</span></span>`;
  }
  const kindTag = (id) => CAST[id].ai ? '<span class="tag ai">AI agent</span>' : '<span class="tag">Human</span>';
  const SRC_LABEL = { femini: "Femini", postgres: "Postgres (read-only)", appsflyer: "AppsFlyer", tiktok: "TikTok Ads", beeswax: "Beeswax", singular: "Singular", meta: "Meta Ads", openai_ads: "OpenAI Ads", fireflies: "Fireflies", lessons: "Lesson store", notion: "Notion", mobius: "Mobius", github: "GitHub", hubspot: "HubSpot (via Femini)" };
  /* The FeedOS mark (feedos.org's logo: four wave-step petals and a glowing core; file: css/feedos-mark.svg). markSVG() inlines it with its own
   * gradient ids, so several copies can live on one page; opts.petals(i) may return a transform per petal and opts.core an extra attribute string
   * for the core (the film's brand beat animates both). */
  const MARK_PETAL = "M14.0 -18.9 L32.8 -18.9 L32.5 -13.0 L31.9 -10.5 L31.6 -7.8 L29.6 -0.8 L27.9 2.9 L27.6 4.2 L25.0 8.6 L24.6 9.7 L21.6 13.4 L19.8 15.0 L16.8 16.9 L15.0 17.2 L13.5 18.0 L9.6 18.6 L-10.6 18.6 L-11.6 18.9 L-32.2 18.9 L-32.7 18.6 L-32.0 15.0 L-31.2 13.4 L-30.0 8.0 L-29.2 6.4 L-28.9 4.2 L-28.2 3.1 L-28.0 1.0 L-27.2 -0.5 L-27.0 -2.4 L-26.2 -3.9 L-26.0 -5.8 L-25.1 -8.4 L-24.0 -10.6 L-19.9 -14.8 L-15.9 -16.9 L-12.4 -17.8 L8.0 -18.1 L8.9 -18.4 L13.4 -18.4 L13.9 -18.8Z", MARK_R = 38;
  let markN = 0;
  function markSVG(opts) {
    opts = opts || {}; const k = "fm" + (++markN), sz = opts.size ? ` width="${opts.size}" height="${opts.size}"` : "";
    const pt = (i) => (opts.petals ? opts.petals(i) : "") || "";
    return `<svg class="${opts.cls || "fmark"}" viewBox="-86 -86 172 172"${sz} aria-hidden="true"><defs><linearGradient id="${k}g" gradientUnits="userSpaceOnUse" x1="-80" y1="-80" x2="80" y2="80"><stop offset="0" stop-color="#C4B5FD"/><stop offset=".5" stop-color="#7C5CFF"/><stop offset="1" stop-color="#3D8BFF"/></linearGradient><radialGradient id="${k}c"><stop offset="0" stop-color="#fff"/><stop offset=".55" stop-color="#C4B5FD"/><stop offset="1" stop-color="#7C5CFF"/></radialGradient></defs>` +
      `<g fill="url(#${k}g)">${[0, 90, 180, 270].map((a, i) => `<g class="fm-p" ${pt(i)}><path d="${MARK_PETAL}" transform="rotate(${a}) translate(0 -${MARK_R})"/></g>`).join("")}</g><circle class="fm-c" r="12.2" fill="url(#${k}c)" ${opts.core || ""}/></svg>`;
  }
  g.UI = { eager: false, esc, AISVG, HUSVG, avatar, kindTag, SRC_LABEL, MARK_PETAL, MARK_R, markSVG };
})(typeof window !== "undefined" ? window : globalThis);
