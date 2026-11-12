// Nodes & settings tab (P3c-3, S1, S3, S6).
import { t, num } from "./i18n.js";
import { get, post, api } from "./api.js";

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

const G = { nodes: [], limits: null, defaults: null, ctx: null };

export function initSettings(ctx) {
  G.ctx = ctx;
  $("nodesRefresh").addEventListener("click", loadSettings);
  $("limSave").addEventListener("click", saveLimits);
  $("limDefaults").addEventListener("click", () => { if (G.defaults) fillLimits(G.defaults); });
  $("pwForm").addEventListener("submit", changePassword);
}

export async function loadSettings() {
  try { G.nodes = (await get("/api/cluster")).nodes; } catch (e) { G.nodes = []; }
  try { const l = await get("/api/settings/limits"); G.limits = l.limits; G.defaults = l.defaults; fillLimits(G.limits); } catch (e) { /* login */ }
  renderNodes();
}

export function renderNodes(nodes) {
  if (nodes) G.nodes = nodes;
  const b = $("nodesBody"); if (!b) return;
  b.innerHTML = "";
  G.nodes.forEach((n) => {
    const h = n.host || {}, l = h.link || {};
    const link = l.speed_mbps ? `${num(l.speed_mbps)} Mb/s${l.duplex ? " · " + l.duplex : ""}${l.state && l.state !== "up" ? " · " + l.state : ""}` : (l.state || "—");
    const eee = l.eee === true ? el("span", { class: "warn", style: "font-weight:600", text: t("common.on") + " ⚠" }) : l.eee === false ? el("span", { class: "ok", text: t("common.off") }) : "—";
    const ram = h.mem_total_mb ? `${num(h.mem_avail_mb / 1024, 1)} / ${num(h.mem_total_mb / 1024, 1)} GB` : "—";
    b.append(el("tr", {},
      el("td", { class: "td mono", text: n.node }),
      el("td", { class: "td " + (n.reachable ? "ok" : "bad"), title: n.error || null, text: n.reachable ? t("common.yes") : t("common.no") }),
      el("td", { class: "td mono", text: n.version || "—" }),
      el("td", { class: "td", text: n.state || "—" }),
      el("td", { class: "td mono", text: link }),
      el("td", { class: "td" }, eee),
      el("td", { class: "td mono", text: h.cpu_pct !== undefined ? num(h.cpu_pct, 0) + " %" + (n.cores ? ` · ${n.cores}c` : "") : "—" }),
      el("td", { class: "td mono", text: ram }),
      el("td", { class: "td mono", text: h.temp_c !== undefined && h.temp_c !== null ? num(h.temp_c, 1) + " °C" : "—" }),
      el("td", { class: "td mono", text: n.clock_offset_ms !== undefined ? num(n.clock_offset_ms) + " ms" : "—" }),
      el("td", { class: "td" }, el("button", { class: "btn btn-sm", type: "button", text: t("settings.restart_agent"), disabled: !n.reachable || G.ctx.busy() || null,
        onclick: () => restart(n.node) }))));
  });
}

async function restart(node) {
  if (!window.confirm(t("settings.restart.confirm", { node }))) return;
  const m = $("nodesMsg");
  try { await post(`/api/nodes/${node}/restart`); m.textContent = t("settings.restart.done", { node }); setTimeout(loadSettings, 4000); }
  catch (e) { m.textContent = e.status === 409 ? t("settings.restart.busy") : e.message; }
}

function fillLimits(l) {
  $("limRouters").value = l.max_routers; $("limPrefixes").value = l.max_prefixes_total; $("limDuration").value = l.max_duration_s;
  $("limRate").value = l.max_rate_mbps; $("limAllow").value = (l.subnet_allow || []).join(", ");
  $("limMsg").textContent = "";
}

async function saveLimits() {
  const n = (id) => Number($(id).value.replace(/\s/g, ""));
  const body = { max_routers: n("limRouters"), max_prefixes_total: n("limPrefixes"), max_duration_s: n("limDuration"), max_rate_mbps: n("limRate"),
    subnet_allow: $("limAllow").value.split(",").map((x) => x.trim()).filter(Boolean) };
  const m = $("limMsg"); m.className = "small";
  try { const r = await api("PUT", "/api/settings/limits", body); G.limits = r.limits; fillLimits(r.limits); m.textContent = t("settings.saved"); m.classList.add("ok"); }
  catch (e) { m.textContent = e.status === 409 ? t("settings.restart.busy") : e.message; m.classList.add("bad"); }
}

async function changePassword(ev) {
  ev.preventDefault();
  const m = $("pwMsg"); m.className = "small";
  if ($("pwNew").value !== $("pwNew2").value) { m.textContent = t("settings.pw.differ"); m.classList.add("bad"); return; }
  try {
    await post("/api/password", { current: $("pwCur").value, new: $("pwNew").value });
    ["pwCur", "pwNew", "pwNew2"].forEach((id) => { $(id).value = ""; });
    m.textContent = t("settings.pw.done"); m.classList.add("ok");
  } catch (e) { m.textContent = e.status === 403 ? t("settings.pw.wrong") : e.message; m.classList.add("bad"); }
}
