// History tab (P3c-3, H1-H3, H5): every run, sortable and filterable; open report, re-run, delete.
import { t, num, lang } from "./i18n.js";
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

const H = { runs: [], sort: "time", dir: -1, ctx: null, loaded: false };
const COLS = [["time", "history.col.time"], ["name", "history.col.name"], ["type", "history.col.type"], ["dut", "history.col.dut"],
  ["scale", "history.col.scale"], ["duration", "history.col.duration"], ["key", "history.col.key_metric"], ["verdict", "history.col.verdict"], ["actions", null]];
const ACTIVE = ["created", "preparing", "running", "finishing"];

export function initHistory(ctx) {
  H.ctx = ctx;
  ["hisQ", "hisType", "hisVerdict"].forEach((id) => $(id).addEventListener("input", renderHistory));
}

export async function loadHistory() {
  try { H.runs = await get("/api/runs?limit=500"); H.loaded = true; } catch (e) { H.runs = []; }
  renderHistory();
}

/** fields of one run as shown in the table (also used by the report page) */
export function runFacts(r) {
  const sc = r.scenario || {}, d = sc.dut || {}, ro = sc.routers || {}, s = r.summary || {};
  const scheme = /^s-\d{8}-\d{6} /.test(r.name || "") ? r.name.split(" ")[0] : null;
  const dutLabel = (d.profile && H.ctx && H.ctx.duts()[d.profile] ? H.ctx.duts()[d.profile].label : null) || d.profile || d.ip || "—";
  const dur = r.ended_ms && (r.started_ms || r.created_ms) ? (r.ended_ms - (r.started_ms || r.created_ms)) / 1000 : null;
  let verdict = "other";
  if (r.state === "done" && s.gate_ok === true) verdict = "pass";
  else if (r.state === "done" && s.gate_ok === false) verdict = "fail";
  return { scheme, dutLabel, dur, verdict, routers: ro.total, prefixes: ro.prefixes, conv: s.initial_convergence_s, p99: s.latency_p99_ms };
}

export function fmtDur(s) {
  if (s === null || s === undefined) return "—";
  s = Math.round(s);
  return s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

function verdictCell(r, f) {
  if (ACTIVE.includes(r.state)) return el("span", { class: "chip running", text: t("history.state.running") });
  if (r.state === "stopped") return el("span", { class: "chip stopped", text: t("history.state.stopped") });
  if (r.state === "failed") return el("span", { class: "chip failed", text: t("history.state.failed"), title: r.error || "" });
  if (f.verdict === "pass") return el("span", { class: "chip passed", text: t("verdict.pass") });
  if (f.verdict === "fail") return el("span", { class: "chip failed", text: t("verdict.fail") });
  return el("span", { class: "chip na", text: "—" });
}

function sortKey(r, f, col) {
  switch (col) {
    case "time": return r.created_ms;
    case "name": return (r.name || "").toLowerCase();
    case "type": return f.scheme ? 1 : 0;
    case "dut": return f.dutLabel.toLowerCase();
    case "scale": return (f.routers || 0) * (f.prefixes || 0);
    case "duration": return f.dur || 0;
    case "key": return f.conv === null || f.conv === undefined ? Infinity : f.conv;
    case "verdict": return { pass: 0, fail: 1, other: 2 }[f.verdict];
    default: return 0;
  }
}

export function renderHistory() {
  if (!$("hisBody")) return;
  const head = $("hisHead"); head.innerHTML = "";
  COLS.forEach(([k, l]) => {
    if (!l) { head.append(el("th", { class: "th" })); return; }
    const on = H.sort === k;
    head.append(el("th", { class: "th", "aria-sort": on ? (H.dir > 0 ? "ascending" : "descending") : null },
      el("button", { type: "button", title: t("history.sort"), style: "all:unset;cursor:pointer;display:inline-flex;gap:4px;align-items:center",
        onclick: () => { if (H.sort === k) H.dir = -H.dir; else { H.sort = k; H.dir = k === "time" ? -1 : 1; } renderHistory(); } },
      t(l), on ? (H.dir > 0 ? "▲" : "▼") : "")));
  });
  const q = $("hisQ").value.trim().toLowerCase(), ty = $("hisType").value, vd = $("hisVerdict").value;
  const rows = H.runs.map((r) => [r, runFacts(r)]).filter(([r, f]) =>
    (!q || [r.id, r.name, f.dutLabel].join(" ").toLowerCase().includes(q)) &&
    (!ty || (ty === "scheme") === !!f.scheme) && (!vd || f.verdict === vd));
  rows.sort((a, b) => { const x = sortKey(a[0], a[1], H.sort), y = sortKey(b[0], b[1], H.sort); return (x < y ? -1 : x > y ? 1 : 0) * H.dir; });
  $("hisCount").textContent = t("history.count", { n: rows.length, m: H.runs.length });
  const body = $("hisBody"); body.innerHTML = "";
  if (!rows.length) body.append(el("tr", {}, el("td", { class: "td", colspan: String(COLS.length), style: "color:#545A61", text: H.loaded ? t("history.empty") : t("common.loading") })));
  rows.forEach(([r, f]) => {
    const key = [f.conv !== null && f.conv !== undefined ? t("history.key.conv", { s: num(f.conv, 1) }) : null,
      f.p99 !== null && f.p99 !== undefined ? t("history.key.p99", { ms: num(f.p99) }) : null].filter(Boolean).join(" · ") || "—";
    const active = ACTIVE.includes(r.state);
    body.append(el("tr", {},
      el("td", { class: "td mono", style: "font-size:12px" }, el("span", { style: "display:flex;flex-direction:column" }, new Date(r.created_ms).toLocaleString(lang()), el("span", { class: "small", text: r.id }))),
      el("td", { class: "td", style: "white-space:normal;min-width:160px", text: f.scheme ? r.name.slice(f.scheme.length + 1) : r.name }),
      el("td", { class: "td" }, f.scheme ? el("a", { href: "#scheme", class: "small", text: t("history.type.scheme") + " " + f.scheme }) : t("history.type.single")),
      el("td", { class: "td", style: "white-space:normal", text: f.dutLabel }),
      el("td", { class: "td mono", text: f.routers ? `${num(f.routers)} × ${num(f.prefixes)}` : "—" }),
      el("td", { class: "td mono", text: fmtDur(f.dur) }),
      el("td", { class: "td mono", text: key }),
      el("td", { class: "td" }, verdictCell(r, f)),
      el("td", { class: "td" }, el("span", { style: "display:flex;gap:6px" },
        el("a", { class: "btn btn-sm", href: `#reports/${r.id}`, style: "display:inline-flex;align-items:center;text-decoration:none", text: t("history.open") }),
        el("button", { class: "btn btn-sm", type: "button", title: t("history.rerun.tip"), text: t("history.rerun"), disabled: !r.scenario || null,
          onclick: () => H.ctx.rerun(r) }),
        el("button", { class: "btn btn-sm", type: "button", text: t("history.delete"), disabled: active || null, style: "color:#B42318",
          onclick: () => del(r) })))));
  });
}

async function del(r) {
  if (!window.confirm(t("history.delete.confirm") + "\n" + r.id)) return;
  try { await api("DELETE", `/api/runs/${r.id}`); H.runs = H.runs.filter((x) => x.id !== r.id); renderHistory(); }
  catch (e) { window.alert(e.status === 409 ? t("history.delete.active") : e.message); }
}
