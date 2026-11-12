// DUT logs tab (P5, L1-L6): the run's DUT log, parsed, aligned to our clock and evaluated.
import { t, num } from "./i18n.js";
import { get, api } from "./api.js";

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
const SEV = { error: "#B42318", warning: "#A15C07", info: "#545A61" };
const L = { id: null, doc: null, runs: [], tab: "ev", ctx: null };
const ACTIVE = ["created", "preparing", "running", "finishing"];

const evName = (e) => t("dl.ev." + e);
const ts = (v) => (v === null || v === undefined ? "—" : (v < 0 ? "−" : "") + `${Math.floor(Math.abs(v) / 60)}:${(Math.abs(v) % 60).toFixed(1).padStart(4, "0")}`);

export function initDutLogs(ctx) {
  L.ctx = ctx;
  $("dlRun").addEventListener("change", (e) => { location.hash = "#logs/" + e.target.value; });
  $("dlOffMode").addEventListener("change", () => { $("dlOff").disabled = $("dlOffMode").value === "auto"; });
  $("dlApply").addEventListener("click", apply);
  $("dlUpload").addEventListener("change", upload);
  $("dlTabEv").addEventListener("click", () => { L.tab = "ev"; renderTable(); });
  $("dlTabUn").addEventListener("click", () => { L.tab = "un"; renderTable(); });
  ["dlQ", "dlFEv", "dlFSev", "dlFPh"].forEach((id) => $(id).addEventListener("input", renderTable));
}

export async function openDutLogs(id) {
  try { L.runs = (await get("/api/runs?limit=200")).filter((r) => !ACTIVE.includes(r.state)); } catch (e) { L.runs = []; }
  if (!id) id = (L.runs[0] || {}).id;
  $("dlErr").hidden = true;
  if (!id) { show(false, t("report.none")); return; }
  L.id = id;
  try { L.doc = await get(`/api/runs/${id}/dutlog`); } catch (e) { L.doc = null; $("dlErr").hidden = false; $("dlErr").textContent = e.message; }
  renderDutLogs();
}

function show(ok, text) {
  const v = document.querySelector('[data-view="logs"]');
  [...v.children].forEach((x) => { if (!["dlEmpty", "dlErr"].includes(x.id) && !x.classList.contains("row-sb")) x.hidden = !ok; });
  $("dlEmpty").hidden = ok; $("dlEmptyText").textContent = text || "";
}

export function renderDutLogs() {
  if (!$("dlRun") || !L.id) return;
  const sel = $("dlRun"); sel.innerHTML = "";
  L.runs.forEach((r) => sel.append(el("option", { value: r.id, text: `${r.id} · ${r.name || ""}`.slice(0, 80) })));
  sel.value = L.id;
  $("dlReport").href = `#reports/${L.id}`;
  const d = L.doc;
  if (!d || d.missing) {
    show(true);
    const why = d && d.error ? d.error : d && d.has_log ? t("dl.regenerate") : t("dl.none");
    $("dlMeta").textContent = L.id;
    renderFiles({ files: [], uploads: (d && d.uploads) || [] });
    $("dlTiles").innerHTML = ""; $("dlFirst").hidden = true;
    ["dlPhHead", "dlPhBody", "dlUnexp", "dlEvBody", "dlEvHead"].forEach((x) => { $(x).innerHTML = ""; });
    $("dlAgree").textContent = ""; $("dlOffUsed").textContent = why; $("dlCount").textContent = "";
    return;
  }
  show(true);
  $("dlMeta").textContent = `${L.id} · ${d.platform || "—"}`;
  renderFiles(d);
  // settings
  const st = d.settings || {};
  $("dlPlatform").value = st.platform || "auto";
  const manual = typeof st.offset === "number";
  $("dlOffMode").value = manual ? "manual" : "auto";
  $("dlOff").disabled = !manual; $("dlOff").value = manual ? st.offset : d.offset_s;
  $("dlOffUsed").textContent = t("dl.offset_used." + d.offset_source, { s: num(d.offset_s, 1), n: d.offset_pairs });
  // L6 summary tiles
  const unexp = d.unexpected_downs || [];
  const tiles = [["dl.t.events", num(d.events)], ["dl.t.unexpected", num(unexp.length), unexp.length ? "bad" : "ok"],
    ["dl.t.errors", num(Object.entries(d.counts || {}).filter(([k]) => ["hold_expired", "max_prefix_warning", "max_prefix_exceeded", "resource", "error"].includes(k)).reduce((a, [, v]) => a + v, 0))],
    ["dl.t.unclassified", num(d.unclassified)]];
  $("dlTiles").innerHTML = "";
  tiles.forEach(([k, v, c]) => $("dlTiles").append(el("div", { class: "kpi" }, el("span", { class: "lbl", text: t(k) }), el("span", { class: "kv " + (c || ""), text: v }))));
  const fe = d.first_error;
  $("dlFirst").hidden = !fe;
  if (fe) $("dlFirst").textContent = t("dl.first_error", { ev: evName(fe.event), t: ts(fe.t_s), r: fe.router || "—", ph: fe.phase ? L.ctx.phaseName({ type: fe.phase }) : "—",
    rate: fe.offered_updates_s === null || fe.offered_updates_s === undefined ? "—" : num(fe.offered_updates_s) }) + (fe.detail ? ` — ${fe.detail}` : "");
  // by phase
  const evs = [...new Set(Object.values(d.by_phase || {}).flatMap((x) => Object.keys(x)))];
  const ph = $("dlPhHead"); ph.innerHTML = "";
  ph.append(el("th", { class: "th", text: t("report.col.phase") }), ...evs.map((e) => el("th", { class: "th", text: evName(e) })));
  const pb = $("dlPhBody"); pb.innerHTML = "";
  Object.entries(d.by_phase || {}).forEach(([p, c]) => pb.append(el("tr", {}, el("td", { class: "td", text: p === "—" ? t("dl.outside") : L.ctx.phaseName({ type: p }) }), ...evs.map((e) => el("td", { class: "td mono", text: c[e] ? num(c[e]) : "" })))));
  if (!evs.length) pb.append(el("tr", {}, el("td", { class: "td", text: "—" })));
  const ub = $("dlUnexp"); ub.innerHTML = "";
  if (!unexp.length) ub.append(el("tr", {}, el("td", { class: "td ok", text: t("dl.unexpected.none") })));
  unexp.forEach((u) => ub.append(el("tr", {}, el("td", { class: "td mono", text: ts(u[0]) }), el("td", { class: "td mono", text: u[1] || "—" }),
    el("td", { class: "td", style: "white-space:normal", text: u[2] || "" }), el("td", { class: "td", text: u[3] ? L.ctx.phaseName({ type: u[3] }) : "" }))));
  const ag = d.agreement || {};
  $("dlAgree").textContent = ag.routers ? t("dl.agree", { m: ag.up_counts_match, n: ag.routers }) : "—";
  // filters
  const fev = $("dlFEv"), prev = fev.value; fev.innerHTML = "";
  fev.append(el("option", { value: "", text: t("history.all") }), ...Object.keys(d.counts || {}).map((e) => el("option", { value: e, text: `${evName(e)} (${d.counts[e]})` })));
  fev.value = prev;
  const fph = $("dlFPh"), pv = fph.value; fph.innerHTML = "";
  fph.append(el("option", { value: "", text: t("history.all") }), ...Object.keys(d.by_phase || {}).map((p) => el("option", { value: p, text: p === "—" ? t("dl.outside") : L.ctx.phaseName({ type: p }) })));
  fph.value = pv;
  renderTable();
}

function renderFiles(d) {
  const b = $("dlFiles"); b.innerHTML = "";
  (d.files || []).forEach((f) => {
    const up = f.file.startsWith("dutlogs/");
    b.append(el("tr", {}, el("td", { class: "td mono", text: f.file.replace("dutlogs/", "") + (up ? "" : " · " + t("dl.collected")) }),
      el("td", { class: "td", text: f.platform || t("dl.unknown") }), el("td", { class: "td mono", text: num(f.lines) }),
      el("td", { class: "td mono", text: f.events === undefined ? "—" : num(f.events) }),
      el("td", { class: "td" }, up ? el("button", { class: "btn btn-sm", type: "button", text: t("dl.delete"), onclick: () => del(f.file.slice(8)) }) : null)));
  });
  if (!(d.files || []).length) b.append(el("tr", {}, el("td", { class: "td", colspan: "5", text: t("dl.none") })));
}

function renderTable() {
  const d = L.doc; if (!d || d.missing) return;
  $("dlTabEv").classList.toggle("zon", L.tab === "ev"); $("dlTabUn").classList.toggle("zon", L.tab === "un");
  $("dlTabEv").setAttribute("aria-selected", String(L.tab === "ev")); $("dlTabUn").setAttribute("aria-selected", String(L.tab === "un"));
  ["dlFEv", "dlFSev", "dlFPh"].forEach((id) => { $(id).parentElement.hidden = L.tab === "un"; });
  const q = $("dlQ").value.trim().toLowerCase();
  const h = $("dlEvHead"), b = $("dlEvBody"); h.innerHTML = ""; b.innerHTML = "";
  if (L.tab === "un") {
    h.append(...["logs.col.time", "dl.col.line"].map((k) => el("th", { class: "th", text: t(k) })));
    const rows = (d.unclassified_list || []).filter((u) => !q || u[1].toLowerCase().includes(q));
    $("dlCount").textContent = t("dl.count", { n: num(rows.length), m: num((d.unclassified_list || []).length) });
    rows.slice(0, 1500).forEach((u) => b.append(el("tr", {}, el("td", { class: "td mono", text: ts(u[0]) }), el("td", { class: "td mono", style: "white-space:pre-wrap;font-size:12px", text: u[1] }))));
    return;
  }
  h.append(...["logs.col.time", "logs.col.neighbour", "logs.col.event", "logs.col.detail", "logs.col.severity", "report.col.phase"].map((k) => el("th", { class: "th", text: t(k) })));
  const fe = $("dlFEv").value, fs = $("dlFSev").value, fp = $("dlFPh").value;
  const rows = (d.events_list || []).filter((e) => (!fe || e[2] === fe) && (!fs || e[4] === fs) && (!fp || (e[5] || "—") === fp) &&
    (!q || [e[1], e[3], e[2]].join(" ").toLowerCase().includes(q)));
  $("dlCount").textContent = t("dl.count", { n: num(rows.length), m: num((d.events_list || []).length) });
  rows.slice(0, 1500).forEach((e) => b.append(el("tr", {},
    el("td", { class: "td mono", text: ts(e[0]) }), el("td", { class: "td mono", text: e[1] || "—" }),
    el("td", { class: "td", text: evName(e[2]) }), el("td", { class: "td", style: "white-space:normal;max-width:520px", text: e[3] }),
    el("td", { class: "td" }, el("span", { style: `color:${SEV[e[4]]};font-weight:600`, text: e[4] })),
    el("td", { class: "td", text: e[5] ? L.ctx.phaseName({ type: e[5] }) : "—" }))));
}

async function apply() {
  const manual = $("dlOffMode").value === "manual";
  const off = Number(String($("dlOff").value).replace(",", "."));
  if (manual && !Number.isFinite(off)) { $("dlErr").hidden = false; $("dlErr").textContent = t("dl.offset.bad"); return; }
  busy(true);
  try { L.doc = await api("PUT", `/api/runs/${L.id}/dutlog/settings`, { platform: $("dlPlatform").value, offset: manual ? off : "auto" }); $("dlErr").hidden = true; }
  catch (e) { $("dlErr").hidden = false; $("dlErr").textContent = e.message; }
  busy(false); renderDutLogs();
}

async function upload(ev) {
  const files = [...ev.target.files]; ev.target.value = "";
  if (!files.length) return;
  busy(true);
  try {
    for (const f of files) {
      const r = await fetch(`/api/runs/${L.id}/dutlog/files?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f, credentials: "same-origin" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      L.doc = await r.json();
    }
    $("dlErr").hidden = true;
  } catch (e) { $("dlErr").hidden = false; $("dlErr").textContent = e.message; }
  busy(false); renderDutLogs();
}

async function del(name) {
  if (!window.confirm(t("dl.delete.confirm", { f: name }))) return;
  busy(true);
  try { L.doc = await api("DELETE", `/api/runs/${L.id}/dutlog/files/${encodeURIComponent(name)}`); } catch (e) { $("dlErr").hidden = false; $("dlErr").textContent = e.message; }
  busy(false); renderDutLogs();
}

function busy(on) {
  ["dlApply", "dlRun", "dlUpload", "dlPlatform", "dlOffMode"].forEach((id) => { $(id).disabled = on; });
  $("dlOffUsed").textContent = on ? t("dl.working") : $("dlOffUsed").textContent;
}
