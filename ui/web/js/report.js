// Reports tab (P3c-3, X1, X5, X6): the run report from report.json, the stored live samples and the run record.
// Figures F1-F12, comparison (X4) and PDF come with P4.
import { t, num, lang } from "./i18n.js";
import { get, post, api } from "./api.js";
import { LatencyChart } from "./chart.js";
import { runFacts, fmtDur } from "./history.js";

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

const R = { id: null, run: null, rep: null, ticks: [], files: [], chart: null, list: [], ctx: null };

export function initReport(ctx) {
  R.ctx = ctx;
  $("repPick").addEventListener("change", (e) => { location.hash = "#reports/" + e.target.value; });
  $("repRegen").addEventListener("click", regenerate);
  $("repNotesSave").addEventListener("click", saveNotes);
  $("repNotes").addEventListener("input", () => { $("repNotesState").textContent = ""; });
  document.querySelectorAll("[data-rspan]").forEach((b) => b.addEventListener("click", () => {
    if (!R.chart) return;
    R.chart.span = +b.dataset.rspan;
    document.querySelectorAll("[data-rspan]").forEach((x) => x.classList.toggle("zon", x === b));
    R.chart.redraw();
  }));
}

/** called by the router for #reports or #reports/<run id> */
export async function openReport(id) {
  try { R.list = await get("/api/runs?limit=200"); } catch (e) { R.list = []; }
  if (!id) id = (R.list.find((r) => r.state === "done") || R.list[0] || {}).id;
  $("repErr").hidden = true;
  if (!id) { $("repEmpty").hidden = false; $("repPage").hidden = true; return; }
  $("repEmpty").hidden = true; $("repPage").hidden = false;
  if (R.id !== id) { R.id = id; R.rep = null; R.ticks = []; }
  try {
    R.run = await get(`/api/runs/${id}`);
  } catch (e) { $("repErr").hidden = false; $("repErr").textContent = e.message; return; }
  const [rep, ticks, files] = await Promise.all([
    get(`/api/runs/${id}/report`).catch(() => null), get(`/api/runs/${id}/ticks`).catch(() => []), get(`/api/runs/${id}/files`).catch(() => [])]);
  R.rep = rep; R.ticks = ticks.map((x) => x.data || x); R.files = files;
  $("repNotes").value = R.run.notes || ""; $("repNotesState").textContent = "";
  renderReport();
}

const pct = (v) => (v === null || v === undefined ? "—" : num(v, v < 10 ? 1 : 0) + " %");
const ms = (v) => (v === null || v === undefined ? "—" : num(v, v < 10 ? 1 : 0));
const sec = (v) => (v === null || v === undefined ? "—" : num(v, v < 10 ? 2 : 1) + " s");

export function renderReport() {
  if (!R.run || $("repPage").hidden) return;
  const r = R.run, rep = R.rep, f = runFacts(r), sc = r.scenario || {};
  // picker
  const pick = $("repPick"); pick.innerHTML = "";
  if (!R.list.find((x) => x.id === r.id)) R.list.unshift(r);
  R.list.forEach((x) => pick.append(el("option", { value: x.id, text: `${x.id} · ${x.name || ""}`.slice(0, 80) })));
  pick.value = r.id;
  // header
  const state = { done: t("state.done"), failed: t("history.state.failed"), stopped: t("history.state.stopped") }[r.state] || t("history.state.running");
  $("repMeta").textContent = t("report.meta", { id: r.id, date: new Date(r.created_ms).toLocaleString(lang()), dur: fmtDur(f.dur), state })
    + (f.scheme ? " · " + t("report.scheme", { id: f.scheme }) : "");
  $("repTitle").textContent = t("report.title", { dut: (rep && rep.dut && rep.dut.profile ? rep.dut.label : null) || f.dutLabel,
    name: f.scheme ? r.name.slice(f.scheme.length + 1) : r.name, routers: num(f.routers), prefixes: num(f.prefixes) });
  const role = (rep && rep.dut && rep.dut.role) || (sc.dut || {}).role;
  $("repSub").textContent = t("report.sub", { role: role ? t("dut.role." + role) : "—", nodes: rep ? (rep.generator.nodes || []).length : "—",
    cv: rep ? rep.version.controller : "—", av: rep ? (rep.version.agents || []).join(", ") : "—" });
  $("repCsv").href = `/api/runs/${r.id}/report.csv`; $("repJson").href = `/api/runs/${r.id}/report`;
  $("repJson").setAttribute("download", `${r.id}-report.json`);
  $("repSummary").href = `/api/runs/${r.id}/files/summary.txt`;
  [$("repCsv"), $("repJson"), $("repSummary")].forEach((a) => { a.style.pointerEvents = rep ? "" : "none"; a.style.opacity = rep ? "" : ".5"; a.setAttribute("aria-disabled", String(!rep)); });
  $("repRegen").disabled = !rep || ["created", "preparing", "running", "finishing"].includes(r.state);
  $("repRegen").title = t("report.regenerate.tip");
  // verdict (the analysis gate)
  const gb = $("repGate"); gb.innerHTML = "";
  const gate = rep ? rep.gate : (r.summary || {}).gate;
  if (gate && gate.length) {
    gate.forEach((g) => {
      const v = g.passed === null ? ["info", "#545A61"] : g.passed ? ["pass", "#1E7B3A"] : ["fail", "#B42318"];
      gb.append(el("tr", {}, el("td", { class: "td", style: "white-space:normal", text: g.name }), el("td", { class: "td mono", style: "white-space:normal", text: g.detail }),
        el("td", { class: "td" }, el("span", { style: `display:inline-flex;align-items:center;gap:6px;font-weight:600;color:${v[1]}` }, el("span", { class: "dot", style: `background:${v[1]}` }), t("verdict." + v[0])))));
    });
    const bad = gate.filter((g) => g.passed === false).length;
    $("repGateNote").textContent = bad ? t("report.gate.fail", { n: bad }) : t("report.gate.ok");
  } else {
    gb.append(el("tr", {}, el("td", { class: "td", colspan: "3", style: "white-space:normal;color:#545A61", text: r.error || t("report.gate.none") })));
    $("repGateNote").textContent = "";
  }
  // key figures
  const kb = $("repKey"); kb.innerHTML = "";
  const row = (k, v) => kb.append(el("tr", {}, el("td", { class: "td", style: "white-space:normal", text: t(k) }), el("td", { class: "td mono", style: "white-space:normal", text: v })));
  if (rep) {
    const s = rep.sessions, c = rep.convergence, w = rep.withdraw || {}, d = rep.dut_output || {}, k = rep.correctness, hm = rep.controller_health_max || {};
    row("report.k.sessions", `${s.established}/${s.configured}` + (s.all_established_after_s !== null && s.all_established_after_s !== undefined ? ` · ${sec(s.all_established_after_s)}` : ""));
    row("report.k.setup", `${ms(s.setup_ms.p50)} / ${ms(s.setup_ms.p99)} ms`);
    row("report.k.conv", c.initial_s !== null && c.initial_s !== undefined ? `${sec(c.initial_s)} · ${c.receivers_converged}/${c.receivers}` : `— · ${c.receivers_converged}/${c.receivers}`);
    row("report.k.after", sec(c.after_last_send_s));
    row("report.k.withdraw", sec(w.convergence_s));
    row("report.k.output", d.prefixes_per_s ? num(d.prefixes_per_s) + " " + t("unit.prefixes_s") : "—");
    row("report.k.per_update", d.prefixes_per_update ? num(d.prefixes_per_update, 1) : "—");
    if (d.link_use_pct !== undefined) row("report.k.link", pct(d.link_use_pct) + (d.payload_mbps ? ` · ${num(d.payload_mbps, 1)} Mb/s` : ""));
    row("report.k.correct", `${num(k.missing_routes)} / ${num(k.extra_routes)}`);
    row("report.k.resets", num(s.resets));
    row("report.k.health", `${ms(hm.api_p95_ms)} ms · ${ms(hm.ws_delay_ms)} ms`);
  } else row("report.gate.none", "—");
  // latency
  const lb = $("repLat"); lb.innerHTML = "";
  if (rep) [["report.lat.announce", rep.latency_announce_ms], ["report.lat.withdraw", rep.latency_withdraw_ms]].forEach(([k, l]) => {
    if (!l) return;
    lb.append(el("tr", {}, el("td", { class: "td", text: t(k) }), ...["n", "p50", "p90", "p99", "max"].map((q) => el("td", { class: "td mono", text: q === "n" ? num(l.n) : ms(l[q]) }))));
  });
  const nb = $("repNodes"); nb.innerHTML = "";
  if (rep) (rep.generator.nodes || []).forEach((n) => nb.append(el("tr", {}, el("td", { class: "td mono", text: n.node }), el("td", { class: "td mono", text: num(n.routers) }),
    el("td", { class: "td mono", text: pct((rep.cpu_node_p95 || {})[n.node]) }), el("td", { class: "td mono", text: n.max_rss_mb ? num(n.max_rss_mb) + " MB" : "—" }))));
  // chart from the stored ticks
  const t0 = (r.started_ms || r.created_ms) / 1000;
  if (!R.chart) {
    R.chart = new LatencyChart($("repChart"), $("repRead"), $("repCur"), $("repWin"), [], t0);
    R.chart.span = 1e9;
  }
  R.chart.t0 = t0; R.chart.cursorX = null; R.chart.pan = null;
  const xs = [], p50 = [], p99 = [], off = [];
  R.ticks.forEach((x) => { xs.push(x.t_ms / 1000); p50.push(x.latency_ms ? x.latency_ms.p50 : null); p99.push(x.latency_ms ? x.latency_ms.p99 : null); off.push(x.rates ? x.rates.sent_s : null); });
  R.chart.setData(xs, p50, p99, off);
  // phases
  const pb = $("repPhases"); pb.innerHTML = "";
  (r.phases || []).forEach((p) => {
    const params = Object.entries(p.params || {}).filter(([k, v]) => v !== null && typeof v !== "object").map(([k, v]) => `${k} ${v}`).join(" · ");
    pb.append(el("tr", {}, el("td", { class: "td mono", text: String(p.idx + 1) }), el("td", { class: "td", text: R.ctx.phaseName({ type: p.type, step: (p.params || {}).step }) }),
      el("td", { class: "td mono", text: fmtDur((p.at_ms - (r.started_ms || r.created_ms)) / 1000) }),
      el("td", { class: "td mono", text: p.end_ms ? fmtDur((p.end_ms - p.at_ms) / 1000) : "—" }),
      el("td", { class: "td mono", style: "white-space:normal;font-size:12px", text: params || "—" })));
  });
  // DUT
  const db = $("repDut"); db.innerHTML = "";
  const dut = (rep && rep.dut) || r.dut || sc.dut || {};
  [["dut.role", dut.role ? t("dut.role." + dut.role) : null], ["DUT", [dut.label, dut.ip, dut.asn ? "AS " + dut.asn : null].filter(Boolean).join(" · ")],
    ["MRAI", dut.mrai === null || dut.mrai === undefined ? null : num(dut.mrai) + " s"], ["dutcfg.mem_guard", dut.min_free_mem_pct ? dut.min_free_mem_pct + " %" : null],
    ["live.port", dut.interface ? `${dut.interface}${dut.link_mbps ? " · " + num(dut.link_mbps) + " Mb/s" : ""}` : (dut.link_mbps ? num(dut.link_mbps) + " Mb/s" : null)]]
    .filter(([, v]) => v).forEach(([k, v]) => db.append(el("tr", {}, el("td", { class: "td", text: k.includes(".") ? t(k) : k }), el("td", { class: "td mono", style: "white-space:normal", text: v }))));
  // events and files
  const ev = $("repEvents"); ev.innerHTML = "";
  const st = r.started_ms || r.created_ms;
  (r.events || []).slice(-300).forEach((e) => {
    const rel = Math.max(0, (e.t_ms - st) / 1000);
    const c = e.severity === "error" ? "#B42318" : e.severity === "warning" ? "#A15C07" : "#545A61";
    ev.append(el("li", {}, el("span", { class: "mono", style: "color:#545A61", text: `${String(Math.floor(rel / 60)).padStart(2, "0")}:${(rel % 60).toFixed(1).padStart(4, "0")}` }),
      el("span", { style: `color:${c};overflow-wrap:anywhere`, text: e.text })));
  });
  const fl = $("repFiles"); fl.innerHTML = "";
  R.files.forEach((x) => fl.append(el("a", { href: `/api/runs/${r.id}/files/${encodeURIComponent(x.name)}`, target: "_blank", class: "mono", text: x.name })));
}

async function regenerate() {
  const b = $("repRegen"); b.disabled = true; $("repErr").hidden = true;
  try {
    await post(`/api/runs/${R.id}/regenerate`);
    R.rep = await get(`/api/runs/${R.id}/report`); R.run = await get(`/api/runs/${R.id}`);
    renderReport();
    $("repNotesState").textContent = t("report.regenerated");
  } catch (e) { $("repErr").hidden = false; $("repErr").textContent = e.message; }
  b.disabled = false;
}

async function saveNotes() {
  try { await api("PUT", `/api/runs/${R.id}/notes`, { notes: $("repNotes").value }); $("repNotesState").textContent = t("report.notes.saved"); }
  catch (e) { $("repNotesState").textContent = e.message; }
}
