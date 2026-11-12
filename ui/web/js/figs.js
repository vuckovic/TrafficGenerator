// Report figures (P4, spec §12, design: docs/design Report): drawn as SVG so they print, export and
// compare cleanly. Every figure has a value cursor (mouse and slider) with a readout line; time
// figures have 1x/2x/4x zoom, a window slider and "zoom to a phase" (X2); series can be shown or
// hidden from the legend and linear/log switched where it makes sense (X2); each figure exports
// SVG, PNG and CSV (X3).
import { t, num } from "./i18n.js";

const NS = "http://www.w3.org/2000/svg";
export const C = { teal: "#0E6E66", orange: "#C2410C", violet: "#5B4FC4", grey: "#8A9097", blue: "#2A6FDB", amber: "#A15C07", red: "#B42318", green: "#1E7B3A", ink: "#16191D", muted: "#545A61" };
export const PALETTE = ["#0E6E66", "#C2410C", "#5B4FC4", "#2A6FDB", "#B8860B", "#3A8F3A"];
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
export const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
  }
  kids.flat().forEach((k) => k !== null && k !== undefined && e.append(k));
  return e;
};
export const mmss = (s) => { if (s === null || s === undefined) return "—"; const v = Math.max(0, Math.round(s)); return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, "0")}`; };
export const secs = (s) => (s === null || s === undefined ? "—" : s < 10 ? num(s, 2) + " s" : s < 120 ? num(s, 1) + " s" : mmss(s));

const W = 760, H = 300;
function niceTicks(a, b, n = 5) {
  const span = b - a; if (!(span > 0)) return [a];
  const step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = []; for (let v = Math.ceil(a / step) * step; v <= b + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function logTicks(a, b) { const o = []; for (let e = Math.floor(Math.log10(a)); e <= Math.ceil(Math.log10(b)); e++) o.push(Math.pow(10, e)); return o.filter((v) => v >= a * 0.999 && v <= b * 1.001); }
const short = (v) => (Math.abs(v) >= 1e6 ? num(v / 1e6, v % 1e6 ? 1 : 0) + "M" : Math.abs(v) >= 1000 ? num(v / 1000, v % 1000 ? 1 : 0) + "k" : num(v, Math.abs(v) < 10 && v % 1 ? (Math.abs(v) < 1 ? 3 : 1) : 0));

/** card shell shared by every figure */
export function card(fid, title, caption) {
  const tools = el("div", { class: "fig-tools" });
  const body = el("div", { class: "fig-body" });
  const below = el("div", { class: "fig-below" });
  const cap = el("p", { class: "small fig-cap", text: caption || "" });
  const sec = el("section", { class: "card fig", "data-fig": fid },
    el("div", { class: "row-sb" }, el("div", {}, el("div", { class: "fig-id", text: fid }), el("h2", { class: "h2", text: title })), tools),
    body, below, cap);
  return { sec, tools, body, below, cap };
}

function exportButtons(tools, getSvg, getCsv, name) {
  tools.append(
    el("button", { class: "btn btn-sm noprint", type: "button", text: "SVG", onclick: () => download(svgText(getSvg()), "image/svg+xml", name + ".svg") }),
    el("button", { class: "btn btn-sm noprint", type: "button", text: "PNG", onclick: () => png(getSvg(), name + ".png") }),
    getCsv ? el("button", { class: "btn btn-sm noprint", type: "button", text: "CSV", onclick: () => download(getCsv(), "text/csv", name + ".csv") }) : null);
}

/**
 * Line figure. opts: {fid, title, caption, name (file), series: [{name, color, dash, axis: "l"|"r", pts: [[x,y],...], step}],
 *  xlog, ylog (initial), logToggle, y2, xfmt, yfmt, y2fmt, xlabel, ylabel, y2label, ymin, ymax, zoom (time zoom + window),
 *  phases: [[label, x0, x1]], marks: [{x, label, color}], hmarks: [{y, label, color}], read: (x, vals) => text, cursorFmt}
 */
export function lineFig(o) {
  const k = card(o.fid, o.title, o.caption);
  const st = { zoom: 1, pan: 1, cur: null, hidden: new Set(), ylog: !!o.ylog, range: null };
  const allx = o.series.flatMap((s) => s.pts.map((p) => p[0])).filter((v) => v !== null && (!o.xlog || v > 0));
  const X0 = allx.length ? Math.min(...allx) : 0, X1 = allx.length ? Math.max(...allx) : 1;
  const L = 64, R = o.y2 ? W - 70 : W - 20, T = 16, B = 256;
  const svgBox = el("div", { class: "fig-svg" });
  k.body.append(svgBox);
  const readout = el("p", { class: "mono fig-read" });
  const curIn = el("input", { type: "range", min: "0", max: "1000", value: "500", "aria-label": t("chart.time_cursor") });
  const winIn = el("input", { type: "range", min: "0", max: "1000", value: "1000", "aria-label": t("chart.window_slider") });
  const legend = el("div", { class: "fig-legend" });
  const ctl = el("div", { class: "fig-ctl noprint" }, el("label", {}, el("span", { text: o.cursorLabel || t("chart.time_cursor") }), curIn));
  if (o.zoom) ctl.append(el("label", {}, el("span", { text: t("chart.window_slider") }), winIn));
  k.below.append(ctl, readout, legend);
  // tools: zoom, phase, log toggle, exports
  if (o.zoom) {
    const g = el("div", { class: "fig-zoom noprint", role: "group" });
    [1, 2, 4].forEach((z) => g.append(el("button", { type: "button", class: z === 1 ? "zb zon" : "zb", text: z + "×", onclick: (e) => {
      st.zoom = z; st.range = null; g.querySelectorAll("button").forEach((b) => b.classList.toggle("zon", b === e.target)); draw(); } })));
    k.tools.append(g);
    if (o.phases && o.phases.length) {
      const sel = el("select", { class: "inp noprint", style: "width:auto;height:36px", "aria-label": t("fig.phase_zoom") },
        el("option", { value: "", text: t("fig.whole_run") }), ...o.phases.map((p, i) => el("option", { value: String(i), text: `${i + 1} · ${p[0]}` })));
      sel.addEventListener("change", () => { const p = o.phases[+sel.value]; st.range = p ? [p[1], p[2] === null ? X1 : p[2]] : null; draw(); });
      k.tools.append(sel);
    }
  }
  if (o.logToggle) {
    const b = el("button", { type: "button", class: "zb noprint", text: st.ylog ? "log" : "lin", title: t("fig.lin_log") });
    b.addEventListener("click", () => { st.ylog = !st.ylog; b.textContent = st.ylog ? "log" : "lin"; draw(); });
    k.tools.append(b);
  }
  exportButtons(k.tools, () => svgBox.querySelector("svg"), () => {
    const rows = [["series", "x", "y"]]; o.series.forEach((s) => s.pts.forEach((p) => rows.push([s.name, p[0], p[1]])));
    return rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n") + "\n";
  }, o.name || o.fid);
  o.series.forEach((s, i) => {
    const b = el("button", { type: "button", class: "fig-lg", "aria-pressed": "true" });
    b.innerHTML = `<svg width="22" height="8" aria-hidden="true"><line x1="0" y1="4" x2="22" y2="4" stroke="${s.color}" stroke-width="3" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/></svg>`;
    b.append(s.name);
    b.addEventListener("click", () => { if (st.hidden.has(i)) st.hidden.delete(i); else st.hidden.add(i); b.setAttribute("aria-pressed", String(!st.hidden.has(i))); b.classList.toggle("off", st.hidden.has(i)); draw(); });
    legend.append(b);
  });
  const win = () => {
    if (st.range) return st.range;
    if (!o.zoom || st.zoom === 1) return [X0, X1];
    const span = (X1 - X0) / st.zoom, right = X0 + span + (X1 - X0 - span) * st.pan;
    return [right - span, right];
  };
  const fx = (v, a, b) => o.xlog ? L + (Math.log10(v) - Math.log10(a)) / (Math.log10(b) - Math.log10(a)) * (R - L) : L + (v - a) / (b - a || 1) * (R - L);
  const inv = (px, a, b) => o.xlog ? Math.pow(10, Math.log10(a) + (px - L) / (R - L) * (Math.log10(b) - Math.log10(a))) : a + (px - L) / (R - L) * (b - a);
  function valAt(s, x) {
    const p = s.pts; if (!p.length) return null;
    let i = 0; while (i < p.length && p[i][0] < x) i++;
    if (i === 0) return p[0][0] <= x ? p[0][1] : null;
    if (i >= p.length) return p[p.length - 1][1];
    if (s.step) return p[i - 1][1];
    const [x0, y0] = p[i - 1], [x1, y1] = p[i];
    if (y0 === null || y1 === null) return y1 ?? y0;
    return o.xlog ? y0 + (y1 - y0) * (Math.log10(x) - Math.log10(x0)) / (Math.log10(x1) - Math.log10(x0) || 1) : y0 + (y1 - y0) * (x - x0) / (x1 - x0 || 1);
  }
  function draw() {
    let [a, b] = win(); if (b <= a) b = a + 1e-6;
    if (st.cur === null || st.cur < a || st.cur > b) st.cur = o.cursorAt !== undefined && o.cursorAt >= a && o.cursorAt <= b ? o.cursorAt : (a + b) / 2;
    const vis = o.series.map((s, i) => ({ s, i })).filter(({ i }) => !st.hidden.has(i));
    const inWin = (s) => s.pts.filter((p) => p[0] >= a && p[0] <= b && p[1] !== null && p[1] !== undefined);
    const yv = vis.filter(({ s }) => s.axis !== "r").flatMap(({ s }) => inWin(s).map((p) => p[1]));
    const y2v = vis.filter(({ s }) => s.axis === "r").flatMap(({ s }) => inWin(s).map((p) => p[1]));
    let ya = o.ymin ?? 0, yb = o.ymax ?? (Math.max(...yv, 0) * 1.1 || 1);
    if (st.ylog) { const pos = yv.filter((v) => v > 0); ya = pos.length ? Math.pow(10, Math.floor(Math.log10(Math.min(...pos)))) : 0.1; yb = pos.length ? Math.pow(10, Math.ceil(Math.log10(Math.max(...pos) * 1.01))) : 10; if (yb <= ya) yb = ya * 10; }
    const y2a = 0, y2b = Math.max(...y2v, 0) * 1.15 || 1;
    const fy = (v) => st.ylog ? B - (Math.log10(Math.max(v, ya)) - Math.log10(ya)) / (Math.log10(yb) - Math.log10(ya)) * (B - T) : B - (v - ya) / (yb - ya || 1) * (B - T);
    const fy2 = (v) => B - (v - y2a) / (y2b - y2a || 1) * (B - T);
    let s = `<svg xmlns="${NS}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.title)}" font-family="IBM Plex Mono, monospace" font-size="11"><rect width="${W}" height="${H}" fill="#FFFFFF"/>`;
    // phase bands
    (o.phases || []).forEach((p, i) => {
      const x0 = Math.max(a, p[1]), x1 = Math.min(b, p[2] === null ? X1 : p[2]); if (x1 <= x0) return;
      s += `<rect x="${fx(x0, a, b).toFixed(1)}" y="${T}" width="${(fx(x1, a, b) - fx(x0, a, b)).toFixed(1)}" height="${B - T}" fill="${i % 2 ? "#F7F8F5" : "#FFFFFF"}"/>`;
      if (fx(x1, a, b) - fx(x0, a, b) > 40) s += `<text x="${(fx(x0, a, b) + 4).toFixed(1)}" y="${T + 11}" fill="${C.grey}" font-family="IBM Plex Sans, sans-serif">${esc(p[0])}</text>`;
    });
    const xt = o.xlog ? logTicks(a, b) : niceTicks(a, b, 6);
    xt.forEach((v) => { const x = fx(v, a, b); s += `<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${B}" stroke="#ECEEEA"/><text x="${x.toFixed(1)}" y="${B + 16}" text-anchor="middle" fill="${C.muted}">${esc((o.xfmt || short)(v))}</text>`; });
    const yt = st.ylog ? logTicks(ya, yb) : niceTicks(ya, yb, 5);
    yt.forEach((v) => { const y = fy(v); s += `<line x1="${L}" y1="${y.toFixed(1)}" x2="${R}" y2="${y.toFixed(1)}" stroke="#ECEEEA"/><text x="${L - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="${C.muted}">${esc((o.yfmt || short)(v))}</text>`; });
    if (o.y2 && y2v.length) niceTicks(y2a, y2b, 5).forEach((v) => { s += `<text x="${R + 6}" y="${(fy2(v) + 4).toFixed(1)}" fill="${C.muted}">${esc((o.y2fmt || short)(v))}</text>`; });
    s += `<line x1="${L}" y1="${B}" x2="${R}" y2="${B}" stroke="${C.grey}"/><line x1="${L}" y1="${T}" x2="${L}" y2="${B}" stroke="${C.grey}"/>`;
    (o.hmarks || []).forEach((m) => { if (m.y < ya || m.y > yb) return; const y = fy(m.y); s += `<line x1="${L}" y1="${y.toFixed(1)}" x2="${R}" y2="${y.toFixed(1)}" stroke="${m.color || C.grey}" stroke-dasharray="3 4"/><text x="${L + 4}" y="${(y - 4).toFixed(1)}" fill="${m.color || C.grey}">${esc(m.label)}</text>`; });
    (o.diag ? [1] : []).forEach(() => { const lo = Math.max(a, ya), hi = Math.min(b, yb); if (hi > lo) s += `<line x1="${fx(lo, a, b).toFixed(1)}" y1="${fy(lo).toFixed(1)}" x2="${fx(hi, a, b).toFixed(1)}" y2="${fy(hi).toFixed(1)}" stroke="${C.grey}" stroke-dasharray="2 4"/>`; });
    vis.forEach(({ s: se }) => {
      const f = se.axis === "r" ? fy2 : fy;
      const pts = se.pts.filter((p) => p[1] !== null && p[1] !== undefined && (!o.xlog || p[0] > 0));
      // include one point either side of the window so lines reach the edges
      const iw = pts.findIndex((p) => p[0] >= a), jw = pts.findIndex((p) => p[0] > b);
      const seg = pts.slice(Math.max(0, iw - 1), jw < 0 ? pts.length : jw + 1);
      if (!seg.length) return;
      let d = "";
      seg.forEach((p, i) => {
        const x = Math.min(R, Math.max(L, fx(p[0], a, b))), y = f(p[1]);
        if (se.step && i) d += ` L${x.toFixed(1)},${f(seg[i - 1][1]).toFixed(1)}`;
        d += `${i ? " L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
      });
      s += `<path d="${d}" fill="none" stroke="${se.color}" stroke-width="${se.width || 2.2}" ${se.dash ? `stroke-dasharray="${se.dash}"` : ""} stroke-linejoin="round"/>`;
      if (se.dots) seg.forEach((p) => { s += p[2] ? `<circle cx="${fx(p[0], a, b).toFixed(1)}" cy="${f(p[1]).toFixed(1)}" r="5.5" fill="#FFFFFF" stroke="${C.red}" stroke-width="2"/>` : `<circle cx="${fx(p[0], a, b).toFixed(1)}" cy="${f(p[1]).toFixed(1)}" r="3.2" fill="${se.color}"/>`; });
    });
    (o.marks || []).forEach((m) => { if (m.x < a || m.x > b) return; const x = fx(m.x, a, b); s += `<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${B}" stroke="${m.color || C.ink}" stroke-dasharray="2 3"/><text x="${(x + 4).toFixed(1)}" y="${B - 6}" fill="${m.color || C.ink}">${esc(m.label)}</text>`; });
    const cx = fx(st.cur, a, b);
    s += `<line x1="${cx.toFixed(1)}" y1="${T}" x2="${cx.toFixed(1)}" y2="${B}" stroke="${C.violet}" stroke-width="1.5"/>`;
    const vals = o.series.map((se, i) => (st.hidden.has(i) ? null : valAt(se, st.cur)));
    vals.forEach((v, i) => { if (v === null || v === undefined) return; const se = o.series[i]; const y = (se.axis === "r" ? fy2 : fy)(v); if (y >= T && y <= B) s += `<circle cx="${cx.toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="${se.color}" stroke="#FFFFFF" stroke-width="1.5"/>`; });
    if (o.xlabel) s += `<text x="${(L + R) / 2}" y="${H - 6}" text-anchor="middle" fill="${C.muted}" font-family="IBM Plex Sans, sans-serif" font-size="12">${esc(o.xlabel)}</text>`;
    if (o.ylabel) s += `<text x="12" y="${(T + B) / 2}" text-anchor="middle" fill="${C.muted}" font-family="IBM Plex Sans, sans-serif" font-size="12" transform="rotate(-90 12 ${(T + B) / 2})">${esc(o.ylabel)}</text>`;
    if (o.y2 && o.y2label) s += `<text x="${W - 8}" y="${(T + B) / 2}" text-anchor="middle" fill="${C.muted}" font-family="IBM Plex Sans, sans-serif" font-size="12" transform="rotate(90 ${W - 8} ${(T + B) / 2})">${esc(o.y2label)}</text>`;
    svgBox.innerHTML = s + "</svg>";
    const svg = svgBox.querySelector("svg");
    svg.addEventListener("mousemove", (e) => { const r = svg.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W; if (px < L || px > R) return; st.cur = inv(px, a, b); draw(); });
    curIn.value = String(Math.round(1000 * (cx - L) / (R - L)));
    winIn.disabled = !(o.zoom && st.zoom > 1 && !st.range);
    readout.textContent = o.read ? o.read(st.cur, vals) : `${(o.cursorFmt || o.xfmt || short)(st.cur)} · ` + o.series.map((se, i) => vals[i] === null || vals[i] === undefined ? null : `${se.name} ${(se.axis === "r" ? o.y2fmt || short : o.yfmt || short)(vals[i])}`).filter(Boolean).join(" · ");
  }
  curIn.addEventListener("input", () => { const [a, b] = win(); st.cur = inv(L + (R - L) * (+curIn.value / 1000), a, b); draw(); });
  winIn.addEventListener("input", () => { st.pan = +winIn.value / 1000; draw(); });
  draw();
  return k.sec;
}

/** heatmap: rows [[name, [values 0..100 per column], [down times]]], cols_s */
export function heatFig(o) {
  const k = card(o.fid, o.title, o.caption);
  const box = el("div", { class: "fig-svg" }); k.body.append(box);
  const read = el("p", { class: "mono fig-read" });
  const slider = el("input", { type: "range", min: "0", max: String(o.cols.length - 1), value: String(Math.floor(o.cols.length / 2)), "aria-label": o.cursorLabel });
  k.below.append(el("div", { class: "fig-ctl noprint" }, el("label", {}, el("span", { text: o.cursorLabel }), slider)), read);
  const rows = o.rows, nc = o.cols.length, L = 70, R = W - 10, T = 8, rh = Math.max(6, Math.min(18, 600 / Math.max(1, rows.length)));
  const Hh = T + rows.length * rh + 26, cw = (R - L) / nc;
  const shade = (v) => { if (v === null) return "#ECEEEA"; const a = Math.max(0, Math.min(1, v / 100)); const mix = (c1, c2) => Math.round(c1 + (c2 - c1) * a); return `rgb(${mix(236, 14)},${mix(238, 110)},${mix(234, 102)})`; };
  let col = +slider.value;
  function draw() {
    let s = `<svg xmlns="${NS}" viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${esc(o.title)}" font-family="IBM Plex Mono, monospace" font-size="10"><rect width="${W}" height="${Hh}" fill="#FFFFFF"/>`;
    rows.forEach((r, i) => {
      const y = T + i * rh;
      if (rh >= 9) s += `<text x="${L - 6}" y="${(y + rh * 0.75).toFixed(1)}" text-anchor="end" fill="${C.muted}">${esc(r[0])}</text>`;
      r[1].forEach((v, j) => {
        const down = (r[2] || []).some((d) => d <= o.cols[j] && (j === 0 || d > o.cols[j - 1]));
        s += `<rect x="${(L + j * cw + 0.5).toFixed(1)}" y="${(y + 0.5).toFixed(1)}" width="${(cw - 1).toFixed(1)}" height="${(rh - 1).toFixed(1)}" fill="${down ? "#F7C9A6" : shade(v)}"/>`;
      });
    });
    s += `<rect x="${(L + col * cw).toFixed(1)}" y="${T}" width="${cw.toFixed(1)}" height="${rows.length * rh}" fill="none" stroke="${C.ink}" stroke-width="1.5"/>`;
    [0, Math.floor(nc / 2), nc - 1].forEach((j) => { s += `<text x="${(L + (j + 0.5) * cw).toFixed(1)}" y="${T + rows.length * rh + 14}" text-anchor="middle" fill="${C.muted}">${esc(o.xfmt(o.cols[j]))}</text>`; });
    box.innerHTML = s + "</svg>";
    const svg = box.querySelector("svg");
    svg.addEventListener("mousemove", (e) => { const r = svg.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W; const j = Math.floor((px - L) / cw); if (j >= 0 && j < nc && j !== col) { col = j; slider.value = String(j); draw(); } });
    slider.value = String(col);
    const worst = rows.map((r) => [r[0], r[1][col]]).sort((a, b) => a[1] - b[1]).slice(0, 5);
    read.textContent = `${o.xfmt(o.cols[col])} · ` + worst.map(([n, v]) => `${n} ${v === null ? "—" : num(v, 0) + " %"}`).join(" · ");
  }
  slider.addEventListener("input", () => { col = +slider.value; draw(); });
  exportButtons(k.tools, () => box.querySelector("svg"), () => ["router," + o.cols.join(",")].concat(rows.map((r) => [r[0], ...r[1]].join(","))).join("\n") + "\n", o.name || o.fid);
  draw();
  return k.sec;
}

/** horizontal bars: items [[label, value, color?]] */
export function barsSvg(items, fmt, opts = {}) {
  const bh = 16, gap = 6, L = opts.labelW || 120, R = W - 90, T = 6;
  const Hh = T + items.length * (bh + gap) + 6;
  const max = Math.max(...items.map((x) => x[1] || 0), opts.min || 0) || 1;
  let s = `<svg xmlns="${NS}" viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${esc(opts.title || "")}" font-family="IBM Plex Mono, monospace" font-size="11"><rect width="${W}" height="${Hh}" fill="#FFFFFF"/>`;
  items.forEach(([lbl, v, col], i) => {
    const y = T + i * (bh + gap), w = Math.max(1, (v || 0) / max * (R - L));
    s += `<text x="${L - 8}" y="${y + 12}" text-anchor="end" fill="${C.muted}">${esc(lbl)}</text><rect x="${L}" y="${y}" width="${w.toFixed(1)}" height="${bh}" rx="3" fill="${col || C.teal}"/><text x="${(L + w + 6).toFixed(1)}" y="${y + 12}" fill="${C.ink}">${esc(fmt(v))}</text>`;
  });
  return s + "</svg>";
}

/** event timeline (F9): lanes of marks over one time axis, phases on top */
export function timelineFig(o) {
  const k = card(o.fid, o.title, o.caption);
  const box = el("div", { class: "fig-svg" }); k.body.append(box);
  const read = el("p", { class: "mono fig-read" });
  const curIn = el("input", { type: "range", min: "0", max: "1000", value: "0", "aria-label": t("chart.time_cursor") });
  const winIn = el("input", { type: "range", min: "0", max: "1000", value: "0", "aria-label": t("chart.window_slider") });
  k.below.append(el("div", { class: "fig-ctl noprint" }, el("label", {}, el("span", { text: t("chart.time_cursor") }), curIn), el("label", {}, el("span", { text: t("chart.window_slider") }), winIn)), read);
  const st = { zoom: 1, pan: 0, cur: o.x0 };
  const g = el("div", { class: "fig-zoom noprint", role: "group" });
  [1, 2, 4].forEach((z) => g.append(el("button", { type: "button", class: z === 1 ? "zb zon" : "zb", text: z + "×", onclick: (e) => { st.zoom = z; g.querySelectorAll("button").forEach((b) => b.classList.toggle("zon", b === e.target)); draw(); } })));
  k.tools.append(g);
  const L = 130, R = W - 16, lanes = o.lanes, LH = 34, T = 8, PH = 26;
  const Hh = T + PH + lanes.length * LH + 30;
  const win = () => { const span = (o.x1 - o.x0) / st.zoom; const a = o.x0 + (o.x1 - o.x0 - span) * st.pan; return [a, a + span]; };
  function draw() {
    const [a, b] = win();
    if (st.cur < a || st.cur > b) st.cur = a;
    const fx = (v) => L + (v - a) / (b - a || 1) * (R - L);
    let s = `<svg xmlns="${NS}" viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${esc(o.title)}" font-family="IBM Plex Mono, monospace" font-size="10"><rect width="${W}" height="${Hh}" fill="#FFFFFF"/>`;
    s += `<text x="${L - 8}" y="${T + 17}" text-anchor="end" fill="${C.muted}" font-family="IBM Plex Sans, sans-serif" font-size="11">${esc(t("report.phases"))}</text>`;
    o.phases.forEach((p, i) => {
      const x0 = Math.max(a, p[1]), x1 = Math.min(b, p[2] ?? o.x1); if (x1 <= x0) return;
      s += `<rect x="${fx(x0).toFixed(1)}" y="${T}" width="${(fx(x1) - fx(x0)).toFixed(1)}" height="${PH - 4}" fill="${["#CDE7E3", "#9FD3CB", "#E3F1EF", "#7FC4BA"][i % 4]}"/>`;
      if (fx(x1) - fx(x0) > 50) s += `<text x="${(fx(x0) + 5).toFixed(1)}" y="${T + 15}" fill="${C.ink}" font-family="IBM Plex Sans, sans-serif" font-size="11">${esc(p[0])}</text>`;
    });
    lanes.forEach((ln, i) => {
      const y = T + PH + i * LH + LH / 2;
      s += `<text x="${L - 8}" y="${y + 4}" text-anchor="end" fill="${C.muted}" font-family="IBM Plex Sans, sans-serif" font-size="11">${esc(ln.name)}</text><line x1="${L}" y1="${y}" x2="${R}" y2="${y}" stroke="#ECEEEA"/>`;
      ln.marks.filter((m) => m[0] >= a && m[0] <= b).forEach((m) => {
        s += m[2] === "sq" ? `<rect x="${(fx(m[0]) - 5).toFixed(1)}" y="${y - 5}" width="10" height="10" fill="${m[1]}"/>` : `<circle cx="${fx(m[0]).toFixed(1)}" cy="${y}" r="5" fill="${m[1]}"/>`;
      });
    });
    const yA = T + PH + lanes.length * LH + 4;
    s += `<line x1="${L}" y1="${yA}" x2="${R}" y2="${yA}" stroke="${C.grey}"/>`;
    niceTicks(a, b, 6).forEach((v) => { s += `<text x="${fx(v).toFixed(1)}" y="${yA + 14}" text-anchor="middle" fill="${C.muted}">${esc(mmss(v))}</text>`; });
    s += `<line x1="${fx(st.cur).toFixed(1)}" y1="${T}" x2="${fx(st.cur).toFixed(1)}" y2="${yA}" stroke="${C.violet}" stroke-width="1.5"/>`;
    box.innerHTML = s + "</svg>";
    const svg = box.querySelector("svg");
    svg.addEventListener("mousemove", (e) => { const r = svg.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W; if (px < L || px > R) return; st.cur = a + (px - L) / (R - L) * (b - a); draw(); });
    curIn.value = String(Math.round(1000 * (st.cur - a) / (b - a || 1)));
    winIn.disabled = st.zoom === 1;
    const tol = (b - a) / 60;
    const near = lanes.flatMap((ln) => ln.marks.filter((m) => Math.abs(m[0] - st.cur) <= tol).map((m) => m[3]));
    const ph = o.phases.find((p) => st.cur >= p[1] && st.cur <= (p[2] ?? o.x1));
    read.textContent = `t = ${mmss(st.cur)}` + (ph ? ` · ${ph[0]}` : "") + (near.length ? " · " + near.slice(0, 4).join("; ") + (near.length > 4 ? ` (+${near.length - 4})` : "") : "");
  }
  curIn.addEventListener("input", () => { const [a, b] = win(); st.cur = a + (b - a) * (+curIn.value / 1000); draw(); });
  winIn.addEventListener("input", () => { st.pan = +winIn.value / 1000; draw(); });
  exportButtons(k.tools, () => box.querySelector("svg"), () => ["lane,t_s,event"].concat(lanes.flatMap((ln) => ln.marks.map((m) => `"${ln.name}",${m[0]},"${String(m[3]).replace(/"/g, '""')}"`))).join("\n") + "\n", o.name || o.fid);
  draw();
  return k.sec;
}

// ------------------------------------------------------------------ files
export function svgText(svg) { return svg ? '<?xml version="1.0" encoding="UTF-8"?>\n' + svg.outerHTML : ""; }
export function download(text, type, name) {
  if (!text) return;
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a); a.click(); a.remove();
}
export function png(svg, name) {
  if (!svg) return;
  const vb = svg.viewBox.baseVal, img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas"); c.width = vb.width * 2; c.height = vb.height * 2;
    const g = c.getContext("2d"); g.scale(2, 2); g.drawImage(img, 0, 0, vb.width, vb.height);
    c.toBlob((b) => { const a = el("a", { href: URL.createObjectURL(b), download: name }); document.body.append(a); a.click(); a.remove(); });
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg.outerHTML);
}
