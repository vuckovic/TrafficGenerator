// Test scheme tab (P3c-2, docs/09 §9; controls K1-K12 of doc 04).
// Passwords typed here stay in this page's memory and go to the controller with the request only.
import { t, num, lang } from "./i18n.js";
import { get, post } from "./api.js";

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
  }
  kids.flat().forEach((k) => k !== null && k !== undefined && e.append(k));
  return e;
};
const NS = "http://www.w3.org/2000/svg";
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export const STEPS = ["test", "smoke", "series", "scale"];
const COLS = ["conn", "smoke", "series_device", "series_zero", "scale"];
const COLORS = ["#2A6FDB", "#D9661F", "#0F8C9C", "#7A4FC4", "#B8860B", "#3A8F3A"];
const INK = "#16191D", MUTED = "#545A61", LINE = "#8A9097", TEAL = "#0E6E66", BLUE = "#1F5F99";

const X = {
  duts: {}, topo: null, plan: {}, doc: null, on: {}, pw: {}, test: {}, steps: new Set(STEPS),
  view: 0, cur: null, k1: true, err: "", ctx: null, planTimer: null,
};

export function schemeActive() { return !!(X.doc && X.doc.state === "running"); }
export function schemeDoc() { return X.doc; }
const colorOf = (dev) => COLORS[Math.max(0, Object.keys(X.duts).indexOf(dev)) % COLORS.length];

// ------------------------------------------------------------------ load
export async function initScheme(ctx) {
  X.ctx = ctx;
  $("k1Toggle").addEventListener("click", () => { X.k1 = !X.k1; renderK1(); });
  $("schPause").addEventListener("input", schedulePlan);
  $("schStart").addEventListener("click", startScheme);
  $("schStop").addEventListener("click", stopScheme);
  $("cmpCsv").addEventListener("click", () => download(compareCsv(), "text/csv", `tgen-scheme-${(X.doc || {}).id || "compare"}.csv`));
  $("cmpMd").addEventListener("click", () => download(compareMd(), "text/markdown", `tgen-scheme-${(X.doc || {}).id || "compare"}.md`));
  $("topoSvg").addEventListener("click", () => download(svgFile($("topoBox").querySelector("svg")), "image/svg+xml", "tgen-topology.svg"));
  $("topoPng").addEventListener("click", () => png($("topoBox").querySelector("svg"), "tgen-topology.png"));
  $("kneeSvg").addEventListener("click", () => download(svgFile($("kneeBox").querySelector("svg")), "image/svg+xml", "tgen-knee.svg"));
  $("kneePng").addEventListener("click", () => png($("kneeBox").querySelector("svg"), "tgen-knee.png"));
  $("kneeCur").addEventListener("input", (e) => { X.cur = parseFloat(e.target.value); renderKnee(); });
}

export async function loadScheme(duts, health) {
  X.duts = duts;
  Object.keys(duts).forEach((n) => { if (X.on[n] === undefined) X.on[n] = true; });
  try { X.topo = await get("/api/topology"); } catch (e) { X.topo = null; }
  try {
    const id = health && health.scheme ? health.scheme.id : ((await get("/api/schemes"))[0] || {}).id;
    X.doc = id ? await get(`/api/schemes/${id}`) : null;
  } catch (e) { X.doc = null; }
  await refreshPlan();
  renderScheme();
}

export function onSchemeMessage(m) {
  X.doc = m.scheme;
  renderScheme();
}

function selected() { return Object.keys(X.duts).filter((n) => X.on[n]); }
function schedulePlan() { clearTimeout(X.planTimer); X.planTimer = setTimeout(async () => { await refreshPlan(); renderSetup(); }, 300); }
async function refreshPlan() {
  const devices = Object.keys(X.duts);
  if (!devices.length) { X.plan = {}; return; }
  try { X.plan = await post("/api/schemes/plan", { devices, steps: [...X.steps], pause_s: pause() }); } catch (e) { X.plan = {}; }
}
function pause() { const v = Number($("schPause").value); return Number.isFinite(v) && v >= 0 ? Math.min(600, v) : 30; }

// ------------------------------------------------------------------ render
export function renderScheme() {
  if (!$("schDevs")) return;
  renderK1(); renderTopo(); renderSetup(); renderProgress(); renderCompare(); renderKnee();
}

function renderK1() {
  $("k1body").hidden = !X.k1;
  $("k1Toggle").setAttribute("aria-expanded", String(X.k1));
  $("k1Toggle").textContent = t(X.k1 ? "scheme.k1.hide" : "scheme.k1.show");
}

function dur(s) {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${Math.max(1, m)} min`;
}
const fmtSize = (r, p) => `${num(r)} × ${num(p)}`;

function renderSetup() {
  const running = schemeActive();
  const tb = $("schDevs"); tb.innerHTML = "";
  const names = Object.keys(X.duts);
  if (!names.length) tb.append(el("tr", {}, el("td", { class: "td", colspan: "5", text: t("scheme.no_devices") })));
  names.forEach((n) => {
    const d = X.duts[n], r = X.test[n];
    const port = [(X.topo && (X.topo.devices.find((x) => x.profile === n) || {}).interface) || null, d.link_mbps ? (d.link_mbps >= 1000 ? num(d.link_mbps / 1000) + " Gb/s" : num(d.link_mbps) + " Mb/s") : null].filter(Boolean).join(" · ");
    const pw = el("input", { class: "inp pwin", type: "password", autocomplete: "off", placeholder: d.password_set ? t("scheme.pw_env") : t("scheme.password.placeholder"),
      "aria-label": t("scheme.col.pw") + " · " + d.label, disabled: running || null, oninput: (e) => { X.pw[n] = e.target.value; } });
    pw.value = X.pw[n] || "";
    let status = null;
    if (r === "busy") status = el("span", { class: "small", text: t("scheme.testing") });
    else if (r) status = el("span", { style: `display:inline-flex;align-items:center;gap:6px;font-size:12px;color:${r.ok ? "#1E7B3A" : "#B42318"};white-space:normal` },
      el("span", { class: "dot", style: `background:${r.ok ? "#1E7B3A" : "#B42318"}` }),
      el("span", { class: r.ok ? "mono" : "", style: `color:${r.ok ? "#23403C" : "#B42318"}`, text: r.ok ? "OK · " + (((r.resources || {}).summary || "").split(",")[0]) : (r.error || "").slice(0, 160) }));
    tb.append(el("tr", {},
      el("td", { class: "td" }, el("input", { type: "checkbox", checked: X.on[n] || null, disabled: running || null, "aria-label": d.label, style: "accent-color:#0E6E66;width:18px;height:18px;margin:0",
        onchange: (e) => { X.on[n] = e.target.checked; renderSetup(); } })),
      el("td", { class: "td" }, el("span", { style: "display:flex;flex-direction:column;gap:2px" }, el("span", { style: "font-weight:600", text: d.label }),
        el("span", { class: "mono", style: "font-size:12px;color:#545A61", text: `${d.peer_ip} · ssh ${d.ssh_host}` }))),
      el("td", { class: "td mono", style: "font-size:12px", text: port || "—" }),
      el("td", { class: "td" }, pw),
      el("td", { class: "td" }, el("span", { style: "display:flex;gap:10px;align-items:center" },
        el("button", { class: "btn btn-sm", type: "button", text: t("scheme.test"), disabled: running || r === "busy" || null, onclick: () => testDevice(n) }), status))));
  });
  // sizes from the file
  const sz = $("schSizes"); sz.innerHTML = "";
  names.forEach((n) => {
    const p = X.plan[n]; if (!p) return;
    const per = lang().startsWith("sr") ? "/s по рутеру" : "/s per router";
    const mr = p.series.mrai.map((v) => v === "default" ? (p.device_mrai_s ? t("scheme.mrai.device_s", { s: num(p.device_mrai_s) }) : (X.duts[n].adapter === "mikrotik" ? t("scheme.mrai.routeros") : t("scheme.mrai.device"))) : num(v) + " s").join(" · ");
    sz.append(el("tr", {}, el("td", { class: "td", style: "font-weight:600", text: p.label }),
      el("td", { class: "td mono", text: fmtSize(p.smoke.routers, p.smoke.prefixes) }),
      el("td", { class: "td mono", style: "white-space:normal", text: `${fmtSize(p.series.routers, p.series.prefixes)} · ${p.series.rates.map((r) => num(r, r % 1 ? 1 : 0)).join(" · ")} ${per}` }),
      el("td", { class: "td", style: "white-space:normal", text: mr }),
      el("td", { class: "td mono", style: "white-space:normal", text: p.scale.map(([r, q]) => `${num(r)}×${num(q)}`).join(" → ") || "—" })));
  });
  // steps
  const st = $("schSteps"); st.innerHTML = "";
  STEPS.forEach((s, i) => st.append(el("label", { style: "display:flex;gap:10px;align-items:flex-start;min-height:44px;padding:6px 0" },
    el("input", { type: "checkbox", checked: X.steps.has(s) || null, disabled: running || null, style: "accent-color:#0E6E66;width:18px;height:18px;margin:2px 0 0",
      onchange: (e) => { if (e.target.checked) X.steps.add(s); else X.steps.delete(s); schedulePlan(); } }),
    el("span", { style: "display:flex;flex-direction:column;gap:2px" }, el("span", { style: "font-weight:600", text: `${i + 1} · ${t(`scheme.step${i + 1}`)}` }),
      el("span", { class: "small", text: t(`scheme.step${i + 1}.hint`) })))));
  $("schPause").disabled = running; $("schStopFail").disabled = running;
  // estimate
  const sel = selected();
  const total = sel.reduce((a, n) => a + ((X.plan[n] || {}).estimate_total_s || 0), 0);
  const left = running ? X.doc.remaining_s : total;
  $("schEst").textContent = t("scheme.estimate.value", { left: dur(left), total: dur(running ? sumPlan(X.doc.plan) : total) });
  $("schEstPer").textContent = sel.length ? t("scheme.per_device", { list: (running ? X.doc.devices : sel).map((n) => `${(X.plan[n] || X.duts[n] || {}).label || n} ${dur(((running ? X.doc.plan : X.plan)[n] || {}).estimate_total_s)}`).join(" · ") }) : "";
  $("schErr").hidden = !X.err; $("schErr").textContent = X.err;
  const busyRun = X.ctx && X.ctx.runActive();
  $("schStart").disabled = running || busyRun || !sel.length || !X.steps.size;
  $("schStop").disabled = !running;
}
const sumPlan = (p) => Object.values(p || {}).reduce((a, x) => a + (x.estimate_total_s || 0), 0);

async function testDevice(n) {
  X.test[n] = "busy"; renderSetup();
  try { X.test[n] = await post(`/api/duts/${encodeURIComponent(n)}/test`, { password: X.pw[n] || null }); }
  catch (e) { X.test[n] = { ok: false, error: e.message }; }
  renderSetup();
}

async function startScheme() {
  X.err = "";
  const sel = selected();
  if (!sel.length || !X.steps.size) { X.err = t("scheme.pick_one"); renderSetup(); return; }
  const missing = sel.find((n) => !X.duts[n].password_set && !X.pw[n]);
  if (missing) { X.err = t("scheme.need_pw", { dev: X.duts[missing].label, env: missing.replace(/[^A-Za-z0-9]/g, "_").toUpperCase() }); renderSetup(); return; }
  $("schStart").disabled = true;
  try {
    const r = await post("/api/schemes", { devices: sel.map((n) => ({ profile: n, password: X.pw[n] || null })), steps: STEPS.filter((s) => X.steps.has(s)),
      pause_s: pause(), stop_on_failure: $("schStopFail").checked });
    X.doc = await get(`/api/schemes/${r.id}`);
    if (X.ctx) X.ctx.changed();
  } catch (e) { X.err = e.status === 409 ? (e.message.includes("scheme") ? e.message : t("ui.start.busy")) : e.message; }
  renderScheme();
}

export async function stopScheme() {
  if (!schemeActive()) return;
  if (!window.confirm(t("scheme.stop_confirm"))) return;
  try { await post(`/api/schemes/${X.doc.id}/stop`); } catch (e) { X.err = e.message; renderSetup(); }
}

// ------------------------------------------------------------------ K9 progress
function infoText(c) {
  const i = c.info;
  if (!i) return c.detail || (c.state === "waiting" ? "—" : "");
  switch (i.k) {
    case "text": return i.v || "";
    case "size": return i.v || fmtSize(i.r, i.p);
    case "missing": return t("scheme.detail.missing", { s: i.s === null || i.s === undefined ? "—" : num(i.s, 1), m: i.m === null || i.m === undefined ? "—" : num(i.m) });
    case "rate": return t("scheme.detail.rate", { r: num(i.r, i.r % 1 ? 1 : 0), i: i.i, n: i.n });
    case "knee": return t("scheme.knee_at", { r: num(i.r, i.r % 1 ? 1 : 0) });
    case "noknee": return t("scheme.no_knee", { r: num(i.r, i.r % 1 ? 1 : 0) });
    case "routeros": return t("scheme.mrai.routeros");
    case "stopped": return t("scheme.detail.stopped");
    case "no_run": return t("scheme.detail.no_run");
    case "limit": return t("scheme.detail.limit");
    case "first_failed": return t("scheme.detail.first_failed");
    case "skip_conn": return t("scheme.detail.skip_conn");
    case "skip_smoke": return t("scheme.detail.skip_smoke");
    default: return c.detail || "";
  }
}

function renderProgress() {
  const tb = $("schProg"); tb.innerHTML = "";
  const d = X.doc;
  $("schLast").textContent = d ? t("scheme.last", { id: d.id, state: t(`scheme.state.${d.state === "done" ? "passed" : d.state === "running" ? "running" : d.state}`), when: new Date(d.created_ms).toLocaleString(lang()) }) : "";
  if (!d) { tb.append(el("tr", {}, el("td", { class: "td", colspan: "6", style: "white-space:normal;color:#545A61", text: t("scheme.none_yet") }))); return; }
  d.devices.forEach((n) => {
    const row = el("tr", {}, el("td", { class: "td", style: "font-weight:600", text: d.results[n].label }));
    COLS.forEach((col) => {
      const c = d.progress[n][col];
      const cell = el("span", { class: "cell" }, el("span", { class: "chip " + c.state, text: t(`scheme.state.${c.state}`) }), el("span", { class: "small", text: infoText(c) }));
      if (c.state === "running") cell.append(el("a", { href: "#benchmark", class: "small", text: t("scheme.live_view") + " →" }));
      else if (c.run && c.state !== "waiting") cell.append(el("a", { href: `/api/runs/${c.run}/files/summary.txt`, target: "_blank", class: "small mono", text: c.run }));
      row.append(el("td", { class: "td" }, cell));
    });
    tb.append(row);
  });
}

// ------------------------------------------------------------------ K10 comparison
const CMP = [["device", "scheme.compare.col.device", null], ["mrai", "scheme.compare.col.mrai", "scheme.compare.col.mrai.tip"],
  ["smoke", "scheme.compare.col.smoke", "scheme.compare.col.smoke.tip"], ["sust", "scheme.compare.col.sust_rate", "scheme.compare.col.sust_rate.tip"],
  ["knee", "scheme.compare.col.knee", "scheme.compare.col.knee.tip"], ["deliv", "scheme.compare.col.sust_deliv", "scheme.compare.col.sust_deliv.tip"],
  ["out", "scheme.compare.col.max_out", "scheme.compare.col.max_out.tip"], ["scale", "scheme.compare.col.largest", "scheme.compare.col.largest.tip"],
  ["mem", "scheme.compare.col.min_mem", "scheme.compare.col.min_mem.tip"]];

function mraiText(r, adapter) {
  if (r.mrai === "default") return r.mrai_s ? t("scheme.mrai.device_s", { s: num(r.mrai_s) }) : (adapter === "mikrotik" ? t("scheme.mrai.routeros") : t("scheme.mrai.device"));
  return num(r.mrai, r.mrai % 1 ? 1 : 0) + " s";
}
function compareRows() {
  const d = X.doc; if (!d) return [];
  return (d.compare || []).map((r) => {
    const ad = (d.results[r.device] || {}).adapter;
    const rate = (v) => num(v, v % 1 ? 1 : 0);
    const reached = r.knee_rate !== null && r.knee_rate !== undefined;
    const hasSeries = r.sustainable_rate !== null && r.sustainable_rate !== undefined || reached;
    return { dev: r.device, raw: r, device: r.label || r.device, mrai: mraiText(r, ad),
      smoke: r.smoke_conv_s !== null && r.smoke_conv_s !== undefined ? t("scheme.detail.missing", { s: num(r.smoke_conv_s, 1), m: num(r.smoke_missing) }) : "—",
      sust: r.sustainable_rate === null || r.sustainable_rate === undefined ? "—" : (!reached && r.sustainable_rate === r.top_rate ? "≥ " : "") + rate(r.sustainable_rate) + " /s",
      knee: reached ? rate(r.knee_rate) + " /s" : hasSeries ? t("scheme.not_reached") : "—",
      deliv: r.sustainable_deliveries_s === null || r.sustainable_deliveries_s === undefined ? "—" : (reached ? "" : "≥ ") + num(r.sustainable_deliveries_s),
      out: r.max_output_s ? num(r.max_output_s) : "—", scale: r.largest_scale ? r.largest_scale.replace(/(\d{4,})/g, (m) => num(+m)) : "—",
      mem: r.min_free_mem_pct === null || r.min_free_mem_pct === undefined ? "—" : num(r.min_free_mem_pct) + " %" };
  });
}
function renderCompare() {
  const h = $("cmpHead"); h.innerHTML = "";
  CMP.forEach(([, l, tip]) => h.append(el("th", { class: "th" }, tip ? el("span", { class: "hint", tabindex: "0" }, l ? t(l) : "", el("span", { class: "tt", role: "tooltip", text: t(tip) })) : t(l))));
  const b = $("cmpBody"); b.innerHTML = "";
  const rows = compareRows();
  if (!rows.length) { b.append(el("tr", {}, el("td", { class: "td", colspan: String(CMP.length), style: "color:#545A61", text: "—" }))); }
  rows.forEach((r) => b.append(el("tr", {}, CMP.map(([k], i) => i === 0
    ? el("td", { class: "td" }, el("span", { style: "display:inline-flex;align-items:center;gap:8px;font-weight:600" }, el("span", { class: "sw", style: `background:${colorOf(r.dev)}` }), r.device))
    : el("td", { class: "td" + (k === "mrai" ? "" : " mono"), style: k === "deliv" ? "font-weight:600" : null, text: r[k] })))));
  $("cmpCsv").disabled = $("cmpMd").disabled = !rows.length;
}
function compareCsv() {
  const q = (s) => `"${String(s).replace(/"/g, '""')}"`;
  return [CMP.map(([, l]) => q(t(l))).join(",")].concat(compareRows().map((r) => CMP.map(([k]) => q(r[k])).join(","))).join("\n") + "\n";
}
function compareMd() {
  const d = X.doc || {};
  const head = "| " + CMP.map(([, l]) => t(l)).join(" | ") + " |\n|" + CMP.map(() => "---").join("|") + "|\n";
  return `# ${t("scheme.compare")} · ${d.id || ""}\n\n` + head + compareRows().map((r) => "| " + CMP.map(([k]) => String(r[k]).replace(/\|/g, "\\|")).join(" | ") + " |").join("\n") + "\n";
}

// ------------------------------------------------------------------ K11 knee chart
const VB = { w: 760, h: 330, xl: 64, xr: 740, yt: 16, yb: 286 };
function series() {
  return ((X.doc && X.doc.knee) || []).filter((s) => s.points.length).map((s) => ({ ...s, color: colorOf(s.device),
    dash: s.mrai === "default" && s.mrai_s ? "6 4" : "",
    name: `${s.label || s.device} · MRAI ${s.mrai === "default" ? (s.mrai_s ? num(s.mrai_s) + " s" : t("scheme.mrai.device")) : num(s.mrai) + " s"}${s.running ? " (" + t("scheme.state.running").toLowerCase() + ")" : ""}` }));
}
function renderKnee() {
  const box = $("kneeBox"), ser = series();
  const views = ["scheme.knee.view.delivered", "scheme.knee.view.tail", "scheme.knee.view.sustained"];
  const vg = $("kneeViews"); vg.innerHTML = ""; vg.setAttribute("aria-label", t("scheme.knee"));
  views.forEach((l, i) => vg.append(el("button", { type: "button", class: i === X.view ? "zb zon" : "zb", "aria-pressed": String(i === X.view), text: t(l), onclick: () => { X.view = i; renderKnee(); } })));
  box.innerHTML = "";
  ["kneeSvg", "kneePng"].forEach((id) => { $(id).disabled = !ser.length; });
  if (!ser.length) { box.append(el("p", { class: "small", style: "margin:0", text: t("scheme.empty_chart") })); $("kneeCurBox").hidden = true; $("kneeRead").hidden = true; $("kneeLegend").innerHTML = ""; return; }
  const all = X.view === 2 ? ser.map((x) => x.sustainable_deliveries_s).filter((v) => v > 0)
    : ser.flatMap((s) => s.points.flatMap((p) => (X.view === 0 ? [p[0], p[1]] : [p[0]])).filter((v) => v > 0));
  if (!all.length) all.push(10, 1000);
  const lo = Math.floor(Math.log10(Math.min(...all))), hi = Math.max(lo + 1, Math.log10(Math.max(...all) * 1.3));
  const fx = (v) => VB.xl + (Math.log10(v) - lo) / (hi - lo) * (VB.xr - VB.xl);
  const fyl = (v) => VB.yb - (Math.log10(Math.max(v, Math.pow(10, lo))) - lo) / (hi - lo) * (VB.yb - VB.yt);
  const smax = Math.max(45, ...ser.map((s) => s.limit_s * 1.25), ...ser.flatMap((s) => s.points.map((p) => (p[2] || 0) * 1.1)));
  const fys = (v) => VB.yb - Math.min(v, smax) / smax * (VB.yb - VB.yt);
  if (X.cur === null || X.cur < lo || X.cur > hi) X.cur = (lo + hi) / 2;
  const ticks = []; for (let e = lo; e <= Math.floor(hi); e++) ticks.push(Math.pow(10, e));
  const tl = (v) => (v >= 1000 ? num(v / 1000) + "k" : num(v));
  let s = `<svg xmlns="${NS}" viewBox="0 0 ${VB.w} ${VB.h}" role="img" aria-label="${esc(t(views[X.view]))}" font-family="IBM Plex Mono, monospace" font-size="11">`;
  s += `<rect x="0" y="0" width="${VB.w}" height="${VB.h}" fill="#FFFFFF"/>`;
  const txt = (x, y, str, anchor = "middle", extra = "") => `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}" fill="${MUTED}" ${extra}>${esc(str)}</text>`;
  const read = [];
  if (X.view < 2) {
    ticks.forEach((v) => { const x = fx(v); s += `<line x1="${x}" y1="${VB.yt}" x2="${x}" y2="${VB.yb}" stroke="#ECEEEA"/>` + txt(x, 302, tl(v)); });
    if (X.view === 0) {
      ticks.forEach((v) => { const y = fyl(v); s += `<line x1="${VB.xl}" y1="${y}" x2="${VB.xr}" y2="${y}" stroke="#ECEEEA"/>` + txt(58, y + 4, tl(v), "end"); });
      const top = Math.pow(10, hi);
      s += `<line x1="${fx(Math.pow(10, lo))}" y1="${fyl(Math.pow(10, lo))}" x2="${fx(top)}" y2="${fyl(top)}" stroke="${LINE}" stroke-width="1.2" stroke-dasharray="2 4"/>` + txt(fx(top) - 6, fyl(top) + 16, t("scheme.knee.diagonal"), "end");
    } else {
      const step = smax > 90 ? 20 : 10;
      for (let v = 0; v <= smax; v += step) { const y = fys(v); s += `<line x1="${VB.xl}" y1="${y}" x2="${VB.xr}" y2="${y}" stroke="#ECEEEA"/>` + txt(58, y + 4, num(v), "end"); }
      [...new Set(ser.map((x) => x.limit_s))].forEach((l) => { const y = fys(l); s += `<line x1="${VB.xl}" y1="${y}" x2="${VB.xr}" y2="${y}" stroke="${LINE}" stroke-width="1.2" stroke-dasharray="2 4"/>` + txt(VB.xr - 4, y - 5, t("scheme.knee.limit", { s: num(l) }), "end"); });
    }
    s += `<line x1="${VB.xl}" y1="${VB.yb}" x2="${VB.xr}" y2="${VB.yb}" stroke="${LINE}"/><line x1="${VB.xl}" y1="${VB.yt}" x2="${VB.xl}" y2="${VB.yb}" stroke="${LINE}"/>`;
    ser.forEach((x) => {
      const pts = x.points.filter((p) => p[0] > 0 && (X.view === 1 ? p[2] !== null : p[1] > 0)).map((p) => [fx(p[0]), X.view === 0 ? fyl(p[1]) : fys(p[2]), p[3], p]);
      s += `<polyline fill="none" stroke="${x.color}" stroke-width="2.5" ${x.dash ? `stroke-dasharray="${x.dash}"` : ""} points="${pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}"/>`;
      pts.forEach((p) => { s += p[2] ? `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="6" fill="#FFFFFF" stroke="#B42318" stroke-width="2"/>` : `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.5" fill="${x.color}"/>`; });
      // reading at the cursor: nearest measured point in log space within half a decade
      let best = null;
      x.points.forEach((p) => { const dd = Math.abs(Math.log10(p[0]) - X.cur); if (dd < 0.5 && (!best || dd < best.dd)) best = { dd, p }; });
      if (best) {
        const p = best.p, past = p[3] ? " · " + t("scheme.knee.past") : "";
        read.push([x.color, X.view === 0 ? t("scheme.knee.read", { n: x.name, o: num(p[0]), d: num(p[1]), p: p[1] ? num(100 * p[1] / p[0]) : "—" }) + past
          : t("scheme.knee.read_tail", { n: x.name, o: num(p[0]), s: p[2] === null ? "—" : num(p[2], 1), l: num(x.limit_s) }) + past]);
      }
    });
    const cx = fx(Math.pow(10, X.cur));
    s += `<line x1="${cx.toFixed(1)}" y1="${VB.yt}" x2="${cx.toFixed(1)}" y2="${VB.yb}" stroke="#5B4FC4" stroke-width="1.5"/>`;
    s += txt((VB.xl + VB.xr) / 2, 324, t("scheme.knee.ax1x"), "middle", 'font-family="IBM Plex Sans, sans-serif" font-size="12"');
    s += `<text x="14" y="${(VB.yt + VB.yb) / 2}" text-anchor="middle" fill="${MUTED}" font-family="IBM Plex Sans, sans-serif" font-size="12" transform="rotate(-90 14 ${(VB.yt + VB.yb) / 2})">${esc(t(X.view === 0 ? "scheme.knee.ax1y" : "scheme.knee.ax2y"))}</text>`;
  } else {
    const bars = ser.filter((x) => x.sustainable_deliveries_s);
    ticks.forEach((v) => { const x = fx(v); s += `<line x1="${x}" y1="${VB.yt}" x2="${x}" y2="${VB.yb}" stroke="#ECEEEA"/>` + txt(x, 302, tl(v)); });
    const bh = Math.min(22, (VB.yb - VB.yt - 10) / Math.max(1, bars.length) - 24);
    bars.forEach((x, i) => {
      const y = VB.yt + 22 + i * (bh + 26), w = fx(x.sustainable_deliveries_s) - VB.xl;
      s += `<text x="${VB.xl + 2}" y="${y - 5}" fill="${INK}" font-family="IBM Plex Sans, sans-serif" font-size="12">${esc(x.name)}</text>`;
      s += `<rect x="${VB.xl}" y="${y}" width="${Math.max(2, w).toFixed(1)}" height="${bh}" rx="3" fill="${x.color}" fill-opacity="${x.running ? 0.35 : 1}"/>`;
      s += txt(VB.xl + w + 8, y + bh / 2 + 4, (x.knee_rate === null || x.knee_rate === undefined ? "≥ " : "") + num(x.sustainable_deliveries_s), "start", `fill="${INK}"`);
    });
    s += `<line x1="${VB.xl}" y1="${VB.yb}" x2="${VB.xr}" y2="${VB.yb}" stroke="${LINE}"/>`;
    s += txt((VB.xl + VB.xr) / 2, 324, t("scheme.knee.ax3x"), "middle", 'font-family="IBM Plex Sans, sans-serif" font-size="12"');
  }
  s += "</svg>";
  box.innerHTML = s;
  const svg = box.querySelector("svg");
  svg.addEventListener("mousemove", (e) => {
    if (X.view === 2) return;
    const r = svg.getBoundingClientRect(); if (!r.width) return;
    const vx = (e.clientX - r.left) / r.width * VB.w; if (vx < VB.xl || vx > VB.xr) return;
    X.cur = lo + (vx - VB.xl) / (VB.xr - VB.xl) * (hi - lo); renderKnee();
  });
  const cur = $("kneeCur"); cur.min = String(lo); cur.max = String(hi); cur.value = String(X.cur);
  $("kneeCurLbl").textContent = num(Math.pow(10, X.cur)) + "/s";
  $("kneeCurBox").hidden = X.view === 2; $("kneeRead").hidden = X.view === 2;
  const ul = $("kneeRead"); ul.innerHTML = "";
  (read.length ? read : [[null, t("scheme.knee.none")]]).forEach(([c, l]) => ul.append(el("li", {}, c ? el("span", { class: "sw", style: `background:${c}` }) : null, l)));
  const lg = $("kneeLegend"); lg.innerHTML = "";
  ser.forEach((x) => { const sp = el("span", { style: "display:inline-flex;align-items:center;gap:6px" }); sp.innerHTML = `<svg width="22" height="6" aria-hidden="true"><line x1="0" y1="3" x2="22" y2="3" stroke="${x.color}" stroke-width="3" ${x.dash ? `stroke-dasharray="${x.dash}"` : ""}/></svg>`; sp.append(x.name); lg.append(sp); });
  lg.append(el("span", { text: "· " + t("scheme.knee.note") }));
}

// ------------------------------------------------------------------ K12 topology (one SVG, from GET /api/topology)
const ICON = {
  router: (c) => `<circle cx="22" cy="22" r="20" fill="${c}"/><g fill="none" stroke="#FFFFFF" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M22 6 V17 M18 10 L22 6 L26 10"/><path d="M22 38 V27 M18 34 L22 38 L26 34"/><path d="M6 22 H17 M13 18 L17 22 L13 26"/><path d="M38 22 H27 M31 18 L27 22 L31 26"/></g>`,
  switch: (c) => `<rect x="1" y="5" width="50" height="34" rx="5" fill="${c}"/><g fill="none" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15 H32 M28 11 L32 15 L28 19"/><path d="M42 29 H20 M24 25 L20 29 L24 33"/></g>`,
  pc: (c) => `<rect x="2" y="2" width="36" height="24" rx="3" fill="${c}"/><rect x="6" y="6" width="28" height="16" rx="1" fill="#DCEFFC"/><rect x="16" y="26" width="8" height="5" fill="${c}"/><rect x="9" y="31" width="22" height="3" rx="1.5" fill="${c}"/>`,
};
const charW = (size, mono) => size * (mono ? 0.6 : 0.56);
function wrap(text, width, size, mono) {
  const max = Math.max(4, Math.floor(width / charW(size, mono)));
  const words = String(text).split(" "), out = [];
  let line = "";
  words.forEach((w) => { if ((line + " " + w).trim().length > max && line) { out.push(line); line = w; } else line = (line + " " + w).trim(); });
  if (line) out.push(line);
  return out;
}
function devState(profile) {
  const d = X.doc;
  if (!d || !d.devices.includes(profile)) return null;
  if (d.state === "running" && d.current && d.current.device === profile) return "now";
  const cells = Object.values(d.progress[profile] || {});
  if (cells.some((c) => c.state === "waiting" || c.state === "running")) return d.state === "running" ? "waiting" : null;
  return "done";
}
function renderTopo() {
  const box = $("topoBox"), tp = X.topo;
  if (!tp) { box.innerHTML = ""; $("topoLegend").innerHTML = ""; return; }
  const W = 1312, parts = { lines: [], cards: [], labels: [] };
  const line = (x1, y1, x2, y2) => parts.lines.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${LINE}" stroke-width="2" stroke-linecap="round"/>`);
  // a card: icon + lines; lines = [{t, b (bold), m (mono), s (size), c (colour), chip:{t,bg,fg}}]
  function card(x, y, w, icon, iw, ih, lines, opt = {}) {
    const col = !!opt.col, pad = 12, lh = 18;
    const tw = col ? w - 2 * pad : w - 3 * pad - iw;
    const rows = [];
    lines.forEach((l) => wrap(l.t, tw - (l.chip ? charW(11, false) * l.chip.t.length + 26 : 0), l.s || 13, l.m).forEach((s, i, arr) => rows.push({ ...l, t: s, chip: i === arr.length - 1 ? l.chip : null })));
    const th = rows.length * lh;
    const h = Math.max(opt.h || 0, col ? ih + 8 + th + 2 * pad : Math.max(ih, th) + 2 * pad);
    const hl = opt.hl;
    let g = `<g><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10" fill="${hl === "now" ? "#EEF6F5" : "#FFFFFF"}" stroke="${hl === "now" ? TEAL : "#D9DCD6"}" stroke-width="${hl === "now" ? 2 : 1}"/>`;
    const ix = col ? x + (w - iw) / 2 : x + pad, iy = col ? y + pad : y + (h - ih) / 2;
    g += `<g transform="translate(${ix} ${iy})">${icon}</g>`;
    let ty = col ? iy + ih + 8 + 13 : y + (h - th) / 2 + 13;
    rows.forEach((r) => {
      const tx = col ? x + w / 2 : x + 2 * pad + iw;
      g += `<text x="${tx}" y="${ty}" text-anchor="${col ? "middle" : "start"}" font-size="${r.s || 13}" font-weight="${r.b ? 600 : 400}" fill="${r.c || INK}" font-family="${r.m ? "IBM Plex Mono, monospace" : "IBM Plex Sans, sans-serif"}">${esc(r.t)}</text>`;
      if (r.chip) {
        const cx = tx + charW(r.s || 13, r.m) * r.t.length + 8, cw = charW(11, false) * r.chip.t.length + 16;
        g += `<rect x="${cx}" y="${ty - 12}" width="${cw}" height="16" rx="8" fill="${r.chip.bg}"/><text x="${cx + cw / 2}" y="${ty}" text-anchor="middle" font-size="11" font-weight="600" fill="${r.chip.fg}" font-family="IBM Plex Sans, sans-serif">${esc(r.chip.t)}</text>`;
      }
      ty += lh;
    });
    parts.cards.push(g + "</g>");
    return h;
  }
  const lab = (x, y, s, anchor = "start") => parts.labels.push(`<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="12" fill="${MUTED}" font-family="IBM Plex Sans, sans-serif">${esc(s)}</text>`);
  const ctl = tp.nodes.find((n) => n.controller) || tp.nodes[0] || {};
  // left: gateway; middle: lab switch above the board's switch; right: the devices; bottom: the nodes
  const gwLines = [{ t: t("scheme.topo.gw"), b: 1 }];
  if (tp.gateway) gwLines.push({ t: t("scheme.topo.gw_ip", { ip: tp.gateway }), m: 1, s: 12 });
  if (tp.public) gwLines.push({ t: t("scheme.topo.public", { url: tp.public, node: ctl.name }), s: 12 });
  const lsY = 168, ls = { x: 520, w: 272 }, lsH = card(ls.x, lsY, ls.w, ICON.switch(BLUE), 52, 44, [{ t: t("scheme.topo.lab"), b: 1 }, { t: t("scheme.topo.lab2"), s: 12, c: MUTED }], { h: 88 });
  const gy = lsY + lsH / 2;
  const gwH = card(16, 0, 272, ICON.router("#5B6670"), 44, 44, gwLines, { h: 104 });
  parts.cards[parts.cards.length - 1] = parts.cards[parts.cards.length - 1].replace(/<g>/, `<g transform="translate(0 ${gy - gwH / 2})">`);
  // devices
  const dx = 960, dw = 336, dys = [];
  let y = 16;
  tp.devices.forEach((d) => {
    const st = devState(d.profile);
    const chip = st === "now" ? { t: t("scheme.topo.now"), bg: TEAL, fg: "#FFFFFF" } : st === "done" ? { t: t("scheme.topo.done"), bg: "#E6F4EA", fg: "#1E7B3A" } : st === "waiting" ? { t: t("scheme.topo.waiting"), bg: "#F3F4F1", fg: MUTED } : null;
    const speed = d.link_mbps ? (d.link_mbps >= 1000 ? `${num(d.link_mbps / 1000)} Gb/s` : `${num(d.link_mbps)} Mb/s`) : "";
    const h = card(dx, y, dw, ICON.router(st === "now" ? TEAL : BLUE), 44, 44, [
      { t: `${d.label} · DUT`, b: 1, chip }, { t: `${d.adapter} · AS ${d.asn}`, s: 12 }, { t: [d.interface, speed].filter(Boolean).join(" · ") || "—", s: 12 },
      { t: `BGP ${d.bgp_ip}`, m: 1, s: 12 }, { t: `SSH ${d.ssh_ip}`, m: 1, s: 12 }], { hl: st });
    dys.push(y + h / 2); y += h + 16;
  });
  const devBottom = y;
  // nodes
  const n = Math.max(1, tp.nodes.length), nw = Math.min(184, (W - 48 - (n - 1) * 32) / n), gap = n > 1 ? (W - 48 - n * nw) / (n - 1) : 0;
  const ny = Math.max(488, devBottom + 24), busy = ny - 36;
  const scY = Math.min(340, busy - 112), scH = card(ls.x, scY, ls.w, ICON.switch(BLUE), 52, 44, [{ t: t("scheme.topo.board", { name: tp.board_switch || "" }), b: 1 }, { t: t("scheme.topo.board2"), s: 12, c: MUTED }], { h: 76 });
  let nh = 0;
  const cxs = tp.nodes.map((nd, i) => {
    const x = 24 + i * (nw + gap);
    const role = nd.controller ? [{ t: t("scheme.topo.ctl"), s: 12, c: MUTED }, { t: t("scheme.topo.agent", { port: nd.agent_port }), s: 12, c: MUTED }] : [{ t: t("scheme.topo.agent", { port: nd.agent_port }), s: 12, c: MUTED }];
    nh = Math.max(nh, card(x, ny, nw, ICON.pc("#3A4047"), 40, 36, [{ t: nd.name, b: 1 }, { t: `${t("scheme.topo.mgmt")} ${nd.mgmt_ip}`, m: 1, s: 12 }, { t: `${t("scheme.topo.test")} ${nd.test_ip}`, m: 1, s: 12 }, ...role], { col: true, h: 152 }));
    return x + nw / 2;
  });
  // wires
  const lsx = ls.x + ls.w / 2;
  line(16 + 272, gy, ls.x, gy);
  line(lsx, lsY + lsH, lsx, scY);
  const bx = 900;
  line(ls.x + ls.w, gy, bx, gy);
  if (dys.length) { line(bx, Math.min(gy, ...dys), bx, Math.max(gy, ...dys)); dys.forEach((cy) => line(bx, cy, dx, cy)); }
  line(lsx, scY + scH, lsx, busy);
  if (cxs.length) { line(cxs[0], busy, cxs[cxs.length - 1], busy); cxs.forEach((cx) => line(cx, busy, cx, ny)); }
  lab(lsx + 12, lsY + lsH + 30, t("scheme.topo.eth0"));
  lab((16 + 272 + ls.x) / 2, gy - 12, tp.mgmt_net, "middle");
  lab(ls.x + ls.w + 8, gy + 20, tp.test_net);
  const H = ny + nh + 16;
  box.innerHTML = `<svg xmlns="${NS}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("scheme.topo.aria"))}"><rect x="0" y="0" width="${W}" height="${H}" fill="#FAFBF9"/>${parts.lines.join("")}${parts.cards.join("")}${parts.labels.join("")}</svg>`;
  const lg = $("topoLegend"); lg.innerHTML = "";
  [t("scheme.topo.leg1", { net: tp.test_net }), t("scheme.topo.leg2", { net: tp.mgmt_net }), t("scheme.topo.leg3", { ip: tp.router_first_ip }), tp.lab_switch ? null : t("scheme.topo.wiring")]
    .filter(Boolean).forEach((s) => lg.append(el("li", { text: s })));
}

// ------------------------------------------------------------------ exports
function svgFile(svg) { return svg ? '<?xml version="1.0" encoding="UTF-8"?>\n' + svg.outerHTML : ""; }
function download(text, type, name) {
  if (!text) return;
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a); a.click(); a.remove();
}
function png(svg, name) {
  if (!svg) return;
  const vb = svg.viewBox.baseVal, img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas"); c.width = vb.width * 2; c.height = vb.height * 2;
    const g = c.getContext("2d"); g.scale(2, 2); g.drawImage(img, 0, 0, vb.width, vb.height);
    c.toBlob((b) => { const a = el("a", { href: URL.createObjectURL(b), download: name }); document.body.append(a); a.click(); a.remove(); });
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg.outerHTML);
}
