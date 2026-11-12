// TrafficGenerator web interface, P3c-1: shell, login, Benchmark tab, live view (docs/09).
import { loadTexts, t, num, apply, setLang, lang, languages, onChange } from "./i18n.js";
import { api, get, post, onUnauthorized, ApiError } from "./api.js";
import { LatencyChart } from "./chart.js";

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
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

const ROLES = ["route_server", "route_reflector", "router"];
const PLATFORMS = [["mikrotik", "dut.platform.mikrotik"], ["cisco", "dut.platform.cisco"], ["bird-ssh", "dut.platform.bird"], ["frr", "dut.platform.frr"]];
const PRESET_KEY = { quick_check: "quick", initial_convergence: "initial", throughput_knee: "knee", resilience: "resilience", full_ixp_day: "ixp_day" };
const PHASE_TYPES = ["establish", "announce", "steady", "churn", "flap", "withdraw", "teardown"];
const FLAP_KINDS = [["reset", "scenario.flap_type.reset"], ["silent", "scenario.flap_type.silent"], ["withdraw_all", "scenario.flap_type.mass_withdraw"]];
const RUN_ACTIVE = ["created", "preparing", "running", "finishing"];

const S = {
  duts: {}, presets: {}, cluster: [], device: "custom", role: "route_server", platform: "cisco",
  phases: [], sel: 0, preset: "quick_check",
  run: null, ticks: [], routers: [], routerState: {}, showAllRouters: false, events: [],
  lastTick: null, dutResult: null, portSpark: [], ws: null, wsRetry: 0,
};

// ------------------------------------------------------------------ boot
async function boot() {
  await loadTexts();
  langMenus();
  apply();
  onChange(() => { renderAll(); });
  onUnauthorized(() => showLogin());
  $("loginForm").addEventListener("submit", login);
  try { await get("/api/me"); await startApp(); } catch (e) { showLogin(); }
}

function showLogin() {
  if (S.ws) { S.ws.onclose = null; S.ws.close(); S.ws = null; }
  $("app").hidden = true; $("login").hidden = false;
  $("loginSub").textContent = t("ui.login.sub", { host: location.host });
  $("pw").focus();
}

async function login(ev) {
  ev.preventDefault();
  const err = $("loginErr"); err.hidden = true;
  try {
    await post("/api/login", { password: $("pw").value });
    $("pw").value = "";
    await startApp();
  } catch (e) {
    err.hidden = false;
    if (e.status === 429) { const m = /(\d+) min/.exec(e.message); err.textContent = t("ui.login.blocked", { min: m ? m[1] : "15" }); }
    else err.textContent = t("ui.login.wrong");
  }
}

let started = false;
async function startApp() {
  $("login").hidden = true; $("app").hidden = false;
  if (!started) { started = true; wire(); }
  const [duts, presets, health] = await Promise.all([get("/api/duts"), get("/api/presets"), get("/api/health")]);
  S.duts = duts; S.presets = presets;
  const first = Object.keys(duts)[0];
  if (!S.phases.length) { pickPreset("quick_check"); pickDevice(first || "custom"); }
  $("hostLine").textContent = location.host;
  await refreshCluster();
  if (health.run) await loadRun(health.run.id); else { const runs = await get("/api/runs?limit=1"); if (runs[0]) await loadRun(runs[0].id); }
  connectWs();
  route();
  renderAll();
}

// ------------------------------------------------------------------ header
function langMenus() {
  $$(".langsel").forEach((box) => {
    box.innerHTML = "";
    const cur = languages().find((l) => l.tag === lang()) || languages()[0] || { flag: "gb" };
    const btn = el("button", { type: "button", class: "hbtn", "aria-haspopup": "menu", "aria-expanded": "false", title: t("lang.select.tip"), "aria-label": t("lang.select") + ": " + (cur.name || "") },
      el("img", { class: "flag", src: `/i18n/flags/${cur.flag}.svg`, alt: "" }),
      el("svg", {}));
    btn.lastChild.outerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 3.5 L5 6.5 L8 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    if (box.closest("#login")) { btn.style.color = "#16191D"; btn.style.borderColor = "#C9CDC6"; }
    const menu = el("div", { class: "menu", role: "menu", hidden: true });
    languages().forEach((l) => menu.append(el("button", { type: "button", role: "menuitemradio", "aria-checked": String(l.tag === lang()), lang: l.html,
      onclick: () => { setLang(l.tag); langMenus(); } }, el("img", { class: "flag", src: `/i18n/flags/${l.flag}.svg`, alt: "", style: "box-shadow:0 0 0 1px #D9DCD6" }), el("span", { text: l.name }))));
    btn.addEventListener("click", () => { menu.hidden = !menu.hidden; btn.setAttribute("aria-expanded", String(!menu.hidden)); });
    document.addEventListener("click", (e) => { if (!box.contains(e.target)) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
    box.append(btn, menu);
  });
}

function route() {
  const tab = (location.hash || "#benchmark").slice(1);
  $$("[data-view]").forEach((m) => { m.hidden = m.dataset.view !== tab; });
  $$("#tabs a").forEach((a) => { if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  if (tab === "settings") refreshCluster().then(renderNodes);
}

function stateWord(st) {
  return t({ created: "state.arming", preparing: "state.arming", running: "state.running", finishing: "state.analysing",
    done: "state.done", failed: "state.error", stopped: "ui.stopped" }[st] || "state.idle");
}
function fmtElapsed(ms) { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function renderStrip() {
  const box = $("runStrip"), r = S.run, st = S.strip || {};
  box.className = "runstrip";
  let text = null, spin = false, segs = [], time = "", links = [], closable = false;
  if (st.starting) { text = t("ui.strip.starting"); spin = true; }
  else if (st.startError) { text = t("ui.strip.start_failed", { error: st.startError }); box.classList.add("err"); closable = true; }
  else if (r && !S.stripClosed && (RUN_ACTIVE.includes(r.state) || S.stripRun === r.id)) {
    const n = (S.runPhases || []).length, cur = S.lastTick && S.lastTick.phase ? S.lastTick.phase.idx : -1;
    const since = r.started_ms || r.created_ms;
    time = since ? t("ui.strip.elapsed", { t: fmtElapsed((r.ended_ms || Date.now()) - since) }) : "";
    if (r.state === "created" || r.state === "preparing") { text = t("ui.strip.preparing"); spin = true; }
    else if (r.state === "running") {
      const p = (S.runPhases || [])[Math.max(0, cur)];
      text = t("ui.strip.running", { n: Math.max(1, cur + 1), m: n, phase: p ? phaseName(p) : "" }); spin = true;
      segs = (S.runPhases || []).map((x, i) => (i < cur ? "done" : i === cur ? "cur" : ""));
    } else if (r.state === "finishing") { text = t("ui.strip.finishing"); spin = true; segs = (S.runPhases || []).map(() => "done"); }
    else if (r.state === "done") {
      text = t("ui.strip.done"); box.classList.add("done"); closable = true; segs = (S.runPhases || []).map(() => "done");
      links = [[t("ui.summary"), `/api/runs/${r.id}/files/summary.txt`], [t("ui.report_json"), `/api/runs/${r.id}/report`]];
    } else if (r.state === "failed") { text = t("ui.strip.failed", { error: r.error || "" }); box.classList.add("err"); closable = true; }
    else if (r.state === "stopped") { text = t("ui.strip.stopped"); box.classList.add("stopped"); closable = true; }
    if (RUN_ACTIVE.includes(r.state)) S.stripRun = r.id;
    if (location.hash && location.hash !== "#benchmark" && RUN_ACTIVE.includes(r.state)) links.push([t("ui.strip.live"), "#benchmark"]);
  }
  box.hidden = text === null;
  document.title = (st.starting || (r && RUN_ACTIVE.includes(r.state)) ? "● " : "") + "TrafficGenerator";
  if (text === null) return;
  $("stripText").textContent = text; $("stripTime").textContent = time;
  $("stripSpin").hidden = !spin; $("stripClose").hidden = !closable;
  const sl = $("stripLinks"); sl.innerHTML = "";
  links.forEach(([l, h], i) => { if (i) sl.append(" · "); sl.append(el("a", { href: h, target: h.startsWith("#") ? null : "_blank", text: l })); });
  const sg = $("stripSegs"); sg.innerHTML = ""; sg.hidden = !segs.length;
  segs.forEach((c) => sg.append(el("span", { class: c })));
}

// fallback when the live connection is down: the strip and badge still follow the run
async function pollRun() {
  if (!S.run || !RUN_ACTIVE.includes(S.run.state) || (S.ws && S.ws.readyState === 1 && S.lastTickAt && Date.now() - S.lastTickAt < 4000)) return;
  try {
    const r = await get(`/api/runs/${S.run.id}`);
    const was = S.run.state; Object.assign(S.run, r);
    S.phaseAt = Object.fromEntries((r.phases || []).map((p) => [p.idx, p]));
    const running = (r.phases || []).filter((p) => !p.end_ms).map((p) => p.idx);
    const idx = running.length ? Math.min(...running) : (r.phases || []).length - 1;
    if (!S.lastTick || !S.lastTickAt || Date.now() - S.lastTickAt > 4000) S.lastTick = Object.assign({}, S.lastTick || {}, { phase: { idx, type: (S.runPhases[idx] || {}).type } });
    if (was !== r.state) { r.events && (S.events = r.events.slice(-200)); renderAll(); } else { renderBadge(); renderEvents(); }
  } catch (e) { /* login page or network */ }
}

function runActive() { return S.run && RUN_ACTIVE.includes(S.run.state); }

function renderBadge() {
  const r = S.run, b = $("badge"), dot = $("badgeDot"), txt = $("badgeText");
  b.className = "badge"; dot.style.background = "#8A9097";
  let s = t("state.idle");
  if (r) {
    const st = r.state;
    const el_s = r.started_ms ? Math.max(0, ((r.ended_ms || Date.now()) - r.started_ms) / 1000) : 0;
    const clock = `${Math.floor(el_s / 60)}:${String(Math.floor(el_s % 60)).padStart(2, "0")}`;
    if (st === "preparing" || st === "created") { s = t("state.arming"); b.classList.add("run"); dot.style.background = "#5FD3C5"; }
    else if (st === "running") {
      const ph = S.lastTick && S.lastTick.phase;
      const n = S.runPhases ? S.runPhases.length : 0;
      s = t("state.running") + (ph && ph.type ? " · " + t("run.phase_of", { phase: phaseName({ type: ph.type, step: ph.type === "churn" && S.runPhases && S.runPhases[ph.idx] && S.runPhases[ph.idx].step }), n: ph.idx + 1, m: n }) : "") + " · " + clock;
      b.classList.add("run"); dot.style.background = "#5FD3C5";
    } else if (st === "finishing") { s = t("state.analysing"); b.classList.add("run"); dot.style.background = "#5FD3C5"; }
    else if (st === "done") { s = t("state.done") + " · " + clock; b.classList.add("done"); dot.style.background = "#4CC38A"; }
    else if (st === "failed") { s = t("state.error"); b.classList.add("err"); dot.style.background = "#F97066"; }
    else if (st === "stopped") { s = t("ui.stopped") + " · " + clock; }
  }
  txt.textContent = s;
  renderStrip();
  $("startBtn").disabled = runActive() || !!(S.strip && S.strip.starting);
  $("stopBtn").disabled = !runActive();
  $$("[data-view=benchmark] section:first-child :is(input,select,button)").forEach((x) => {
    if (x.closest("#previewBox") || x.dataset.dut === "preview" || x.dataset.dut === "status" || x.dataset.dut === "test") return;
    x.disabled = runActive() || x.dataset.forceDisabled === "1";
  });
}

// ------------------------------------------------------------------ device under test
function dutProfile() { return S.device !== "custom" ? S.duts[S.device] : null; }
function platformOf() { return S.platform; }

function pickDevice(name) {
  S.device = name;
  const p = dutProfile();
  if (p) {
    S.platform = p.adapter; S.role = p.role || (p.adapter === "mikrotik" ? "router" : "route_server");
    $("dutIp").value = p.peer_ip || ""; $("dutAsn").value = p.asn || 64500; $("sshHost").value = p.ssh_host || p.peer_ip || "";
    $("sshPort").value = p.ssh_port || 22; $("sshUser").value = p.user || ""; $("maxRoutes").value = p.max_routes || "";
    $("guard").value = p.min_free_mem_pct || 0; $("auth").value = "password"; S.mraiMode = "default";
  } else {
    $("dutIp").value = ""; $("dutAsn").value = 64500; $("sshHost").value = ""; $("sshUser").value = ""; $("maxRoutes").value = ""; $("guard").value = 0;
  }
  S.dutResult = null;
  renderDut();
}

function renderDut() {
  const sel = $("device"); sel.innerHTML = "";
  for (const [name, p] of Object.entries(S.duts)) {
    const link = p.link_mbps >= 1000 ? `${p.link_mbps / 1000} Gb/s` : `${p.link_mbps} Mb/s`;
    sel.append(el("option", { value: name, text: `${p.label} · ${p.peer_ip} · ${link}` }));
  }
  sel.append(el("option", { value: "custom", text: t("dut.device.custom") }));
  sel.value = S.device;
  const p = dutProfile();
  $("pwState").textContent = $("auth").value === "key" ? t("dut.password.key") : p && p.password_set ? t("dut.password.set") : t("dut.password.unset");
  // role
  const rs = $("roleSeg"); rs.innerHTML = "";
  ROLES.forEach((r) => rs.append(el("label", { class: r === S.role ? "on" : "" },
    el("input", { type: "radio", name: "role", checked: r === S.role, onchange: () => { S.role = r; renderDut(); } }), t("dut.role." + r))));
  $("roleNote").textContent = t(`dut.role.${S.role}.explain`);
  // platform
  const ps = $("platSeg"); ps.innerHTML = "";
  PLATFORMS.forEach(([id, key]) => ps.append(el("label", { class: id === S.platform ? "on" : "" },
    el("input", { type: "radio", name: "plat", checked: id === S.platform, onchange: () => { S.platform = id; renderDut(); } }), t(key))));
  const notes = { mikrotik: ["dutcfg.note.rs_mikrotik"], cisco: ["dutcfg.note.rs_cisco", "dutcfg.note.running_only"], "bird-ssh": ["dutcfg.bird_include.tip"], frr: ["dutcfg.note.quagga"] };
  $("platNote").textContent = (notes[S.platform] || []).map((k) => t(k)).join(" ");
  $$("[data-only]").forEach((f) => {
    const o = f.dataset.only;
    f.hidden = !(o === "cisco" ? S.platform === "cisco" : o === "linux" ? ["bird-ssh", "frr"].includes(S.platform) : o === "bird" ? S.platform === "bird-ssh" : true);
  });
  // MRAI
  const m = $("mrai"); const isCisco = S.platform === "cisco"; m.innerHTML = "";
  if (isCisco) {
    const dflt = p && p.device_mrai_s !== undefined && p.device_mrai_s !== null ? `${p.device_mrai_s} s` : "IOS 30 s";
    m.append(el("option", { value: "default", text: t("dutcfg.mrai.default", { s: dflt }) }), el("option", { value: "0", text: t("dutcfg.mrai.zero") }), el("option", { value: "custom", text: t("dutcfg.mrai.custom") }));
    m.value = S.mraiMode || "default"; m.dataset.forceDisabled = "0";
  } else { m.append(el("option", { value: "na", text: t("dutcfg.mrai.na") })); m.dataset.forceDisabled = "1"; m.disabled = true; }
  $("mraiCustomBox").hidden = !(isCisco && m.value === "custom");
  // detected / status
  const res = S.dutResult;
  $("detected").textContent = res && res.resources ? (res.resources.summary || "").split(",")[0] : t("ui.dut.not_tested");
  renderBadge();
  splitLine();
}

function mraiValue() {
  if (S.platform !== "cisco") return null;
  const v = $("mrai").value;
  return v === "default" ? null : v === "0" ? 0 : +$("mraiCustom").value;
}

function intOr(id, dflt) { const v = $(id).value.trim(); if (v === "") return dflt; const n = Number(v); if (!Number.isFinite(n)) throw new Error(t("common.invalid") + ": " + id); return n; }

function dutSpec(password, enable) {
  const p = dutProfile();
  const d = {
    adapter: S.platform, ip: $("dutIp").value.trim() || null, ssh_host: $("sshHost").value.trim() || null,
    ssh_port: intOr("sshPort", 22), user: $("sshUser").value.trim() || null, asn: intOr("dutAsn", 64500), role: S.role,
    mrai: mraiValue(), configure: $("configure").checked, keep_config: $("after").value === "keep",
    poll_s: intOr("pollS", 10), max_routes: $("maxRoutes").value.trim() ? intOr("maxRoutes") : null,
    min_free_mem_pct: intOr("guard", 0),
  };
  if (p) d.profile = S.device;
  if (["bird-ssh", "frr"].includes(S.platform)) { d.sudo = $("sudo").checked; if (S.platform === "bird-ssh") d.bird_include = $("birdInc").value.trim() || null; }
  if (password) d.password = password;
  if (enable) d.enable = enable;
  return d;
}

function needPassword() {
  if ($("auth").value === "key") return false;
  const p = dutProfile();
  return !(p && p.password_set);
}

function askPassword(okLabel) {
  return new Promise((resolve) => {
    const dlg = $("pwDialog"), p = dutProfile();
    $("pwTitle").textContent = t("ui.start.password_title", { device: p ? p.label : $("dutIp").value || "DUT" });
    $("pwOk").textContent = okLabel; $("pwDut").value = "";
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? $("pwDut").value : null);
    dlg.showModal(); $("pwDut").focus();
  });
}

let sessionPw = null;   // kept in this page's memory only, for the buttons and the next run
async function dutButton(action) {
  const status = $("dutStatus"), dot = $("dutDot");
  let pw = null;
  if (action !== "preview" && needPassword()) {
    pw = sessionPw || await askPassword(t("common.ok"));
    if (pw === null) return;
    sessionPw = pw;
  }
  status.textContent = t("ui.dut.working"); dot.style.background = "#8A9097";
  try {
    const r = await post(`/api/dut/${action}`, { dut: dutSpec(pw, $("enable").value || null), routers: intOr("rTotal", 60) });
    if (!r.ok) { if (/auth|password|login/i.test(r.error || "")) sessionPw = null; throw new Error(r.error || "failed"); }
    dot.style.background = "#1E7B3A";
    if (action === "test") {
      S.dutResult = r;
      let s = t("ui.dut.status.ok", { summary: r.resources.summary || "", n: r.tgen_neighbours });
      if (r.port) s += " · " + t("ui.dut.status.port", { iface: r.port.interface, speed: r.port.speed_mbps, duplex: r.port.duplex || "" });
      status.textContent = s; renderDut();
    } else if (action === "preview") {
      $("previewBox").hidden = false; $("prevBtn").setAttribute("aria-expanded", "true");
      $("previewTitle").textContent = t("dutcfg.preview.title", { platform: r.platform });
      $("previewText").textContent = r.config; status.textContent = "";
    } else if (action === "push") status.textContent = t("ui.dut.status.pushed", { n: r.neighbours });
    else if (action === "remove") status.textContent = t("ui.dut.status.pushed", { n: r.tgen_neighbours });
    else if (action === "status") status.textContent = t("ui.dut.status.sessions", { n: r.established });
  } catch (e) {
    dot.style.background = "#B42318"; status.textContent = t("dutcfg.status.failed") + ": " + e.message;
  }
}

// ------------------------------------------------------------------ routers
function splitLine() {
  const total = Math.max(1, +$("rTotal").value || 0);
  const nodes = S.cluster.length ? S.cluster.map((n, i) => [n.node, i === 0 ? 0.7 : 1]) : [];
  if (!nodes.length) { $("rSplit").textContent = ""; return; }
  const w = nodes.reduce((a, n) => a + n[1], 0);
  const raw = nodes.map((n) => total * n[1] / w), out = raw.map(Math.floor);
  raw.map((r, i) => [r - out[i], i]).sort((a, b) => b[0] - a[0]).slice(0, total - out.reduce((a, b) => a + b, 0)).forEach(([, i]) => out[i]++);
  $("rSplit").textContent = t("ui.routers.split", { split: nodes.map((n, i) => `${n[0]} ${out[i]}`).join(" · ") }) + " · " + t("routers.placement.cmnode01");
}

function renderRouters() {
  const body = $("routersBody"); body.innerHTML = "";
  const rows = S.routers;
  if (!rows.length) {
    body.append(el("tr", {}, el("td", { class: "td", colspan: "6", style: "white-space:normal", text: t("ui.routers.at_start") })));
    $("routersFoot").textContent = ""; return;
  }
  const shown = S.showAllRouters ? rows : rows.slice(0, 12);
  shown.forEach((r) => {
    const st = S.routerState[r.name] || {};
    const state = st.state || "—";
    const cls = state === "Established" ? "ok" : state === "Idle" || state === "Stopped" ? "small" : "warn";
    body.append(el("tr", {}, el("td", { class: "td mono", text: r.name }), el("td", { class: "td", text: r.node }), el("td", { class: "td mono", text: String(r.asn) }),
      el("td", { class: "td mono", text: r.ip }), el("td", { class: "td mono", text: num(r.prefix_count) }), el("td", { class: "td" }, el("span", { class: cls, style: "font-weight:600", text: state }))));
  });
  const foot = $("routersFoot"); foot.innerHTML = "";
  const total = rows.reduce((a, r) => a + (r.prefix_count || 0), 0);
  foot.append(t("routers.showing", { n: shown.length, m: rows.length }) + " · ");
  if (rows.length > 12) foot.append(el("a", { href: "#benchmark", onclick: (e) => { e.preventDefault(); S.showAllRouters = !S.showAllRouters; renderRouters(); }, text: t("routers.show_all") }), " · ");
  foot.append(el("span", { class: "mono", text: `${num(total)} ${t("routers.col.prefixes").toLowerCase()}` }));
}

// ------------------------------------------------------------------ scenario
function pickPreset(key) {
  S.preset = key;
  const p = S.presets[key];
  if (p) S.phases = JSON.parse(JSON.stringify(p.phases));
  S.sel = 0;
}
function phaseName(p) {
  return t({ establish: "scenario.phase.establish", announce: "scenario.phase.initial_load", steady: "scenario.phase.steady",
    churn: p.step ? "scenario.phase.churn_stepped" : "scenario.phase.churn", flap: "scenario.phase.flap", withdraw: "ui.phase.withdraw",
    teardown: "scenario.phase.teardown" }[p.type] || p.type);
}
function phaseSub(p) {
  if (p.type === "establish") return p.per_s ? `${num(p.per_s)}/s` : t("scenario.all_at_once");
  if (p.type === "announce") return p.rate ? `${num(p.rate)} ${t("unit.prefixes_s")}` : t("scenario.max_rate");
  if (p.type === "steady") return `${num(p.duration_s)} s`;
  if (p.type === "churn") return p.step ? `${num(p.rate)}→${num(p.step.max)} /s` : `${num(p.rate)} /s`;
  if (p.type === "flap") return `${p.kind} · ${p.routers} · ${num(p.duration_s)} s`;
  return p.rate ? `${num(p.rate)} ${t("unit.prefixes_s")}` : t("scenario.withdraw_all");
}

function renderScenario() {
  const ps = $("preset"); ps.innerHTML = "";
  Object.keys(S.presets).forEach((k) => ps.append(el("option", { value: k, text: t("scenario.preset." + (PRESET_KEY[k] || k)) })));
  ps.append(el("option", { value: "custom", text: t("scenario.preset.custom") }));
  ps.value = S.preset;
  const np = $("newPhase"); np.innerHTML = "";
  PHASE_TYPES.forEach((ty) => np.append(el("option", { value: ty, text: phaseName({ type: ty }) })));
  np.value = np.value || "steady";
  const ol = $("phases"); ol.innerHTML = "";
  const viewing = S.run && S.runPhases && runActive();
  const list = viewing ? S.runPhases : S.phases;
  const cur = S.lastTick && S.lastTick.phase ? S.lastTick.phase.idx : -1;
  list.forEach((p, i) => {
    let state = "", cls = i === S.sel && !viewing ? "sel" : "";
    if (viewing) { state = i < cur ? t("scenario.phase.done") : i === cur ? t("scenario.phase.running") : t("scenario.phase.next"); cls += i < cur ? " done" : i === cur ? " running" : ""; }
    ol.append(el("li", {}, el("button", { type: "button", class: cls, "aria-pressed": String(i === S.sel), onclick: () => { if (!viewing) { S.sel = i; renderScenario(); } } },
      el("span", { class: "ph-state", text: `${i + 1}${state ? " · " + state : ""}` }), el("span", { style: "font-weight:600", text: phaseName(p) }), el("span", { class: "mono small", text: phaseSub(p) }))));
  });
  renderPhaseFields(viewing);
}

function renderPhaseFields(locked) {
  const box = $("phaseFields"); box.innerHTML = "";
  const p = S.phases[S.sel]; if (!p || locked) return;
  const field = (key, label, get, set, kind = "num") => {
    const id = "pf_" + key;
    let input;
    if (kind === "select") {
      input = el("select", { id, class: "inp", onchange: (e) => { set(e.target.value); S.preset = "custom"; renderScenario(); } });
      get().forEach(([v, k, on]) => input.append(el("option", { value: v, selected: on, text: t(k) })));
    } else input = el("input", { id, class: "inp mono", value: String(get()), onchange: (e) => { set(e.target.value); S.preset = "custom"; renderScenario(); } });
    box.append(el("div", { class: "field" }, el("label", { class: "lbl", for: id, text: t(label) }), input));
  };
  const n = (v) => Math.max(0, Number(v) || 0);
  if (p.type === "establish") { field("per_s", "scenario.sessions_per_s", () => p.per_s || 0, (v) => p.per_s = n(v)); field("timeout", "scenario.timeout", () => p.timeout_s || 0, (v) => p.timeout_s = n(v)); }
  if (p.type === "announce") { field("rate", "scenario.announce_rate", () => p.rate || 0, (v) => p.rate = n(v)); field("timeout", "scenario.timeout", () => p.timeout_s || 0, (v) => p.timeout_s = n(v)); }
  if (p.type === "steady") field("dur", "scenario.duration", () => p.duration_s || 60, (v) => p.duration_s = n(v));
  if (p.type === "churn") {
    field("rate", "scenario.start_rate", () => p.rate || 0, (v) => p.rate = n(v));
    field("add", "ui.step.add", () => (p.step ? p.step.add : 0), (v) => { const a = n(v); if (a) p.step = Object.assign({ every_s: 30, max: 2000 }, p.step, { add: a }); else delete p.step; });
    field("every", "ui.step.every", () => (p.step ? p.step.every_s : 30), (v) => { if (p.step) p.step.every_s = Math.max(1, n(v)); });
    field("max", "scenario.max", () => (p.step ? p.step.max : 0), (v) => { if (p.step) p.step.max = Math.max(1, n(v)); });
    field("dur", "scenario.duration", () => p.duration_s || 60, (v) => p.duration_s = n(v));
    field("mix", "scenario.mix", () => { const m = p.mix || { announce: 40, withdraw: 40, attr: 20 }; return `${m.announce} / ${m.withdraw} / ${m.attr}`; },
      (v) => { const [a, w, x] = v.split(/[\/,\s]+/).filter(Boolean).map(Number); p.mix = { announce: a || 0, withdraw: w || 0, attr: x || 0 }; }, "text");
  }
  if (p.type === "flap") {
    field("kind", "scenario.flap_type", () => FLAP_KINDS.map(([v, k]) => [v, k, v === p.kind]), (v) => p.kind = v, "select");
    field("routers", "ui.flap.count", () => p.routers || 1, (v) => p.routers = Math.max(1, n(v)));
    field("down", "ui.flap.down", () => p.down_s || 5, (v) => p.down_s = n(v));
    field("dur", "scenario.duration", () => p.duration_s || 60, (v) => p.duration_s = n(v));
  }
  if (p.type === "withdraw" || p.type === "teardown") { field("rate", "scenario.withdraw_rate", () => p.rate || 0, (v) => p.rate = n(v)); field("timeout", "scenario.timeout", () => p.timeout_s || 0, (v) => p.timeout_s = n(v)); }
}

function addPhase() {
  const ty = $("newPhase").value;
  const dflt = { establish: { per_s: 0 }, announce: { rate: 0 }, steady: { duration_s: 60 }, churn: { rate: 100, duration_s: 120, mix: { announce: 40, withdraw: 40, attr: 20 } },
    flap: { kind: "reset", routers: 1, down_s: 10, duration_s: 60 }, withdraw: { rate: 0 }, teardown: { rate: 0 } }[ty];
  S.phases.splice(S.sel + 1, 0, Object.assign({ type: ty }, dflt));
  S.sel += 1; S.preset = "custom"; renderScenario();
}
function delPhase() { if (S.phases.length > 1) { S.phases.splice(S.sel, 1); S.sel = Math.max(0, S.sel - 1); S.preset = "custom"; renderScenario(); } }

// ------------------------------------------------------------------ runs
async function start() {
  let pw = null;
  if ($("configure").checked && needPassword()) {
    pw = sessionPw || await askPassword(t("ui.start.go"));
    if (pw === null) return;
  }
  const sb = $("startBtn"); sb.disabled = true; sb.textContent = t("ui.starting");
  S.strip = { starting: true }; S.stripClosed = false; renderStrip();
  try {
    const scenario = {
      name: $("runName").value.trim() || "run", dut: dutSpec(pw, $("enable").value || null),
      routers: { total: intOr("rTotal", 60), prefixes: intOr("rPrefixes", 10000), per_update: intOr("rPerUpd", 0), seed: intOr("seed", 1), hold_time: intOr("rHold", 90) },
      phases: S.phases,
    };
    const r = await post("/api/runs", { scenario });
    S.ticks = []; S.routers = []; S.routerState = {}; S.events = []; S.lastTick = null; S.portSpark = [];
    S.stripClosed = false; S.strip = null;
    await loadRun(r.id);
    renderAll();
  } catch (e) {
    const msg = e.status === 409 ? t("ui.start.busy") : e.message;
    S.strip = { startError: msg };
    addEvent({ t_ms: Date.now(), kind: "run", severity: "error", text: t("ui.strip.start_failed", { error: msg }) });
  } finally {
    sb.textContent = t("run.start"); if (S.strip && S.strip.starting) S.strip = null;
    renderBadge();
  }
}
async function stop() { if (S.run) { try { await post(`/api/runs/${S.run.id}/stop`); } catch (e) { addEvent({ t_ms: Date.now(), severity: "error", text: e.message }); } } }

async function loadRun(id) {
  const r = await get(`/api/runs/${id}`);
  S.run = r; S.runPhases = (r.scenario && r.scenario.phases) || [];
  S.events = (r.events || []).slice(-200);
  S.phaseAt = Object.fromEntries((r.phases || []).map((p) => [p.idx, p]));
  try { S.ticks = (await get(`/api/runs/${id}/ticks`)).map((x) => x.data || x); } catch (e) { S.ticks = []; }
  S.lastTick = S.ticks.length ? S.ticks[S.ticks.length - 1] : null;
  await loadRouters();
}
async function loadRouters() {
  if (!S.run || ["created", "preparing"].includes(S.run.state)) return;   // routers.json is written while preparing
  try { S.routers = await get(`/api/runs/${S.run.id}/files/routers.json`); } catch (e) { S.routers = []; }
}

// ------------------------------------------------------------------ live
function connectWs() {
  if (S.ws) return;
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live");
  S.ws = ws;
  ws.onopen = () => { S.wsRetry = 0; $("wsBanner").hidden = true; };
  ws.onmessage = (m) => { try { onMessage(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  ws.onclose = (ev) => {
    S.ws = null;
    if (ev.code === 4401) { showLogin(); return; }
    $("wsBanner").hidden = false;
    setTimeout(connectWs, Math.min(10000, 500 * 2 ** S.wsRetry++));
  };
}

async function onMessage(m) {
  if (m.type === "tick") {
    if (!S.run || m.run !== S.run.id) { await loadRun(m.run); }
    S.ticks.push(m); if (S.ticks.length > 7200) S.ticks.shift();
    S.lastTick = m; S.lastTickAt = Date.now();
    (m.routers || []).forEach(([name, node, state, held]) => { S.routerState[name] = { state, held }; });
    if (S.run && RUN_ACTIVE.includes(S.run.state)) S.run.state = m.state;   // never revive a finished run
    renderLive(); renderRouters(); renderBadge();
    if (S.run && S.lastPhaseIdx !== (m.phase && m.phase.idx)) { S.lastPhaseIdx = m.phase && m.phase.idx; renderScenario(); }
  } else if (m.type === "state") {
    if (!S.run || m.run !== S.run.id) await loadRun(m.run);
    S.run.state = m.state;
    if (m.state === "running") { S.run.started_ms = S.run.started_ms || m.t_ms; await loadRouters(); renderRouters(); }
    if (!RUN_ACTIVE.includes(m.state)) { const r = await get(`/api/runs/${m.run}`); Object.assign(S.run, r); S.phaseAt = Object.fromEntries((r.phases || []).map((p) => [p.idx, p])); }
    renderAll();
  } else if (m.type === "event") {
    addEvent(m);
    if (m.kind === "phase" && S.run) { try { const r = await get(`/api/runs/${S.run.id}`); S.phaseAt = Object.fromEntries((r.phases || []).map((p) => [p.idx, p])); } catch (e) { /* ignore */ } }
  }
}

function addEvent(e) { S.events.push(e); if (S.events.length > 200) S.events.shift(); renderEvents(); }
function renderEvents() {
  const ul = $("events"); ul.innerHTML = "";
  const t0 = S.run && S.run.started_ms;
  S.events.slice().reverse().slice(0, 100).forEach((e) => {
    const rel = t0 ? Math.max(0, (e.t_ms - t0) / 1000) : null;
    const ts = rel !== null ? `${String(Math.floor(rel / 60)).padStart(2, "0")}:${(rel % 60).toFixed(1).padStart(4, "0")}` : new Date(e.t_ms).toLocaleTimeString();
    const sev = e.severity === "error" ? ["event.error", "#B42318"] : e.severity === "warning" ? ["event.warn", "#A15C07"] : e.kind === "phase" ? ["event.phase", "#545A61"] : ["event.info", "#1E7B3A"];
    ul.append(el("li", {}, el("span", { class: "mono", style: "color:#545A61", text: ts }), el("span", { style: `color:${sev[1]};font-weight:600`, text: t(sev[0]) }), el("span", { style: "overflow-wrap:anywhere", text: e.text })));
  });
}

let chart;
function renderLive() {
  const tk = S.lastTick, run = S.run;
  // run line
  const rl = $("runLine"); rl.innerHTML = "";
  if (!run) rl.textContent = t("ui.no_run");
  else {
    rl.append(t("ui.last_run", { id: run.id, state: stateWord(run.state) }));
    if (!RUN_ACTIVE.includes(run.state) && run.summary && run.summary.gate) {
      rl.append(" · ", el("a", { href: `/api/runs/${run.id}/files/summary.txt`, target: "_blank", text: t("ui.summary") }), " · ", el("a", { href: `/api/runs/${run.id}/report`, target: "_blank", text: t("ui.report_json") }));
    }
    if (run.error) rl.append(" · ", el("span", { class: "bad", text: run.error }));
  }
  // KPIs
  const total = S.routers.length || (tk ? Object.values(tk.sessions || {}).reduce((a, b) => a + b, 0) : 0);
  const est = tk ? (tk.sessions || {}).Established || 0 : 0;
  $("kSess").innerHTML = ""; $("kSess").append(tk ? num(est) : "—", el("span", { style: "font-size:15px;color:#545A61", text: tk ? "/" + num(total) : "" }));
  $("kSessSub").textContent = tk && total && est === total ? t("metric.sessions.all_established") : tk ? Object.entries(tk.sessions || {}).filter(([k]) => k !== "Established").map(([k, v]) => `${k} ${v}`).join(" · ") : "";
  $("kSessSub").className = "small" + (tk && total && est === total ? " ok" : "");
  let conv = run && run.summary && run.summary.initial_convergence_s;
  const annIdx = (S.runPhases || []).findIndex((p) => p.type === "announce");
  const ann = S.phaseAt && S.phaseAt[annIdx];
  if (conv === undefined || conv === null) {
    const done = S.ticks.find((x) => ann && x.t_ms >= ann.at_ms && total && x.converged >= total);
    if (done && ann) conv = (done.t_ms - ann.at_ms) / 1000;
  }
  $("kConv").textContent = conv !== undefined && conv !== null ? num(conv, 1) + " s" : "—";
  $("kConvSub").textContent = tk && total ? t("metric.routes_received", { pct: num(100 * (tk.converged || 0) / total) + " %" }) : t("ui.convergence.waiting");
  const lat = tk && tk.latency_ms;
  $("kLat").textContent = lat && lat.p99 !== null ? num(lat.p99) + " ms" : "—";
  $("kLat").style.color = lat && lat.p99 > 250 ? "#A15C07" : "";
  $("kLatSub").textContent = lat && lat.p50 !== null ? "p50 " + num(lat.p50) + " ms" : "";
  const rates = tk && tk.rates;
  $("kOut").textContent = rates && rates.rx_s !== null ? num(rates.rx_s) : "—";
  $("kOutSub").textContent = rates ? t("ui.updates_s", { n: num(rates.sent_s) }) : "";
  // chart
  if (!chart) chart = new LatencyChart($("latChart"), $("readout"), $("curSlider"), $("winSlider"), $$("[data-span]"));
  const xs = [], p50 = [], p99 = [], off = [];
  S.ticks.forEach((x) => { xs.push(x.t_ms / 1000); p50.push(x.latency_ms ? x.latency_ms.p50 : null); p99.push(x.latency_ms ? x.latency_ms.p99 : null); off.push(x.rates ? x.rates.sent_s : null); });
  chart.setData(xs, p50, p99, off);
  // port (M9)
  const pt = tk && tk.dut_port;
  $("portCard").hidden = !(pt && pt.reported);
  if (pt && pt.reported) {
    const speed = pt.speed_mbps >= 1000 ? `${num(pt.speed_mbps)} Mb/s` : `${num(pt.speed_mbps)} Mb/s`;
    $("portLine").textContent = [pt.interface, speed, pt.duplex ? t("ui.port.duplex", { d: pt.duplex === "full" ? (lang() === "en" ? "full" : "пун") : pt.duplex === "half" ? (lang() === "en" ? "half" : "полу") : pt.duplex }) : null, pt.up === false ? t("ui.port.down") : t("ui.port.up")].filter(Boolean).join(" · ");
    $("portUse").textContent = pt.use_pct !== null ? num(pt.use_pct, pt.use_pct < 10 ? 1 : 0) + " %" : "—";
    $("portMbps").textContent = t("live.port.of", { mbps: num(pt.mbps, 1), speed });
    const bar = $("portBar"); bar.style.width = Math.min(100, Math.max(0.5, pt.use_pct || 0)) + "%";
    bar.style.background = pt.use_pct >= 90 ? "#B42318" : pt.use_pct >= 70 ? "#A15C07" : "#0E6E66";
    const spark = S.ticks.slice(-300).map((x) => (x.dut_port ? x.dut_port.use_pct || 0 : 0));
    const mx = Math.max(1, ...spark);
    $("portSpark").setAttribute("points", spark.map((v, i) => `${(i * 300 / Math.max(1, spark.length - 1)).toFixed(1)},${(32 - v / mx * 26).toFixed(1)}`).join(" "));
    const errs = pt.errors || {}, nerr = Object.values(errs).reduce((a, b) => a + b, 0);
    const g = (k) => num(errs[k] || 0);
    $("portErr").textContent = nerr ? t("live.port.new_errors", { in: g("input_errors"), crc: g("crc"), drops: g("output_drops") }) : t("live.port.no_errors", { in: g("input_errors"), crc: g("crc"), drops: g("output_drops") });
    $("portErrLine").style.color = nerr ? "#A15C07" : "#1E7B3A"; $("portErrDot").style.background = nerr ? "#A15C07" : "#1E7B3A";
  }
  // generator health
  const gh = $("genHealth"); gh.innerHTML = "";
  const nodes = tk && tk.nodes && tk.nodes.length ? tk.nodes : S.cluster.map((n) => ({ node: n.node }));
  nodes.forEach((n) => {
    const cpu = n.cpu_pct;
    gh.append(el("span", { class: "mono", text: n.node }),
      el("span", { class: "bar" }, el("span", { class: "fill", style: `width:${Math.min(100, cpu || 0)}%;background:${cpu >= 90 ? "#B42318" : cpu >= 70 ? "#A15C07" : "#0E6E66"}` }), el("span", { class: "m70" }), el("span", { class: "m90" })),
      el("span", { class: "mono", text: cpu !== undefined && cpu !== null ? num(cpu) + " %" : "—" }),
      el("span", { class: "small", text: n.temp_c !== undefined && n.temp_c !== null ? num(n.temp_c) + " °C" : "" }));
  });
  // control-plane health (AC6)
  $("reserveLine").textContent = t("live.control_health.reserve", { cpu: "30 %", ram: num(4.8, 1) + " GB", bw: "20 Mbit/s" });
  const ch = tk && tk.controller;
  if (ch) {
    $("cpApi").textContent = num(ch.api_p95_ms) + " ms"; $("cpDelay").textContent = num(ch.ws_delay_ms / 1000, 1) + " s";
    const bad = ch.api_p95_ms > 500 || ch.ws_delay_ms > 2000 || ch.loop_lag_ms > 2000 || (ch.mem_available_mb !== null && ch.mem_available_mb < 4800);
    $("cpState").textContent = "● " + (bad ? t("ui.warn") : t("live.ok")); $("cpState").style.color = bad ? "#A15C07" : "#1E7B3A";
  }
  renderEvents();
}

async function refreshCluster() { try { S.cluster = (await get("/api/cluster")).nodes; } catch (e) { S.cluster = []; } }
function renderNodes() {
  const b = $("nodesBody"); b.innerHTML = "";
  S.cluster.forEach((n) => b.append(el("tr", {}, el("td", { class: "td mono", text: n.node }), el("td", { class: "td " + (n.reachable ? "ok" : "bad"), text: n.reachable ? t("common.yes") : t("common.no") }),
    el("td", { class: "td mono", text: n.version || "—" }), el("td", { class: "td", text: n.state || n.error || "—" }), el("td", { class: "td mono", text: n.clock_offset_ms !== undefined ? num(n.clock_offset_ms) + " ms" : "—" }))));
}

async function idleHealth() {
  if (runActive() || $("app").hidden) return;
  try {
    const h = await get("/api/health");
    const ch = h.health;
    $("cpApi").textContent = num(ch.api_p95_ms) + " ms"; $("cpDelay").textContent = num(ch.ws_delay_ms / 1000, 1) + " s";
    const bad = h.violations && h.violations.length;
    $("cpState").textContent = "● " + (bad ? t("ui.warn") : t("live.ok")); $("cpState").style.color = bad ? "#A15C07" : "#1E7B3A";
  } catch (e) { /* login page will show */ }
}

// ------------------------------------------------------------------ wiring
function renderAll() { renderDut(); renderScenario(); renderRouters(); renderLive(); renderBadge(); renderNodes(); }

function wire() {
  window.addEventListener("hashchange", route);
  $("device").addEventListener("change", (e) => pickDevice(e.target.value));
  $("auth").addEventListener("change", renderDut);
  $("mrai").addEventListener("change", (e) => { S.mraiMode = e.target.value; renderDut(); });
  $("rTotal").addEventListener("input", splitLine);
  $("preset").addEventListener("change", (e) => { if (e.target.value !== "custom") { pickPreset(e.target.value); renderScenario(); } });
  $("addPhase").addEventListener("click", addPhase);
  $("delPhase").addEventListener("click", delPhase);
  $$("[data-dut]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.dut === "preview" && !$("previewBox").hidden) { $("previewBox").hidden = true; b.setAttribute("aria-expanded", "false"); return; }
    dutButton(b.dataset.dut);
  }));
  $("copyCfg").addEventListener("click", () => { try { navigator.clipboard.writeText($("previewText").textContent); } catch (e) { /* not allowed */ } });
  $("dlCfg").addEventListener("click", () => {
    const a = el("a", { href: URL.createObjectURL(new Blob([$("previewText").textContent], { type: "text/plain" })), download: `tgen-${S.platform}.txt` });
    document.body.append(a); a.click(); a.remove();
  });
  $("startBtn").addEventListener("click", start);
  $("stopBtn").addEventListener("click", stop);
  $("logoutBtn").addEventListener("click", async () => { try { await post("/api/logout"); } catch (e) { /* ignore */ } sessionPw = null; showLogin(); });
  setInterval(() => { renderBadge(); }, 1000);
  setInterval(idleHealth, 5000);
  setInterval(pollRun, 2000);
  $("stripClose").addEventListener("click", () => { S.stripClosed = true; S.strip = null; renderStrip(); });
}

boot();
