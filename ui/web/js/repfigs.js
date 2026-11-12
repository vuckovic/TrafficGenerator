// Figures F1-F12 of one run (report page) and the comparison of 2-5 runs (X4), from figures.json.
import { t, num } from "./i18n.js";
import { lineFig, heatFig, timelineFig, barsSvg, card, el, C, PALETTE, secs, mmss, download } from "./figs.js";

const ms = (v) => (v === null || v === undefined ? "—" : num(v, v < 10 ? 1 : 0) + " ms");
const pct = (v) => (v === null || v === undefined ? "—" : num(v, v < 10 && v % 1 ? 1 : 0) + " %");
const rate = (v) => (v === null || v === undefined ? "—" : num(v) + "/s");
const pairs = (xs, ys) => xs.map((x, i) => [x, ys[i]]);

function note(fid, title, text) {
  const k = card(fid, title, "");
  k.body.append(el("p", { class: "small", style: "margin:0", text }));
  k.sec.classList.add("fig-empty");
  return k.sec;
}

/** all figures of one run into `host` */
export function renderFigures(host, f, phaseName) {
  host.innerHTML = "";
  if (!f) return;
  const phases = (f.f9.phases || []).map((p) => [phaseName({ type: p[0], step: (p[3] || {}).step }), p[1], p[2]]);
  const add = (sec) => host.append(sec);

  // F1 session establishment
  const f1 = lineFig({ fid: "F1", name: "F1-sessions", title: t("fig.f1"), caption: t("fig.f1.cap"), zoom: true, phases,
    series: [{ name: t("fig.f1.established"), color: C.teal, pts: f.f1.curve, step: true }],
    xfmt: mmss, cursorFmt: (x) => "t = " + secs(x), yfmt: (v) => num(v), yint: true, xlabel: t("fig.time"), ylabel: t("fig.f1.sessions"), ymax: f.f1.configured * 1.05,
    hmarks: [{ y: f.f1.configured, label: t("fig.f1.configured", { n: f.f1.configured }), color: C.grey }] });
  if (f.f1.setup_ms.length) {
    const box = el("div", { class: "fig-svg" });
    box.innerHTML = barsSvg(f.f1.setup_ms.slice(0, 12).map(([n, v]) => [n, v, C.teal]), (v) => ms(v), { title: t("fig.f1.setup"), labelW: 70 });
    f1.querySelector(".fig-below").append(el("h3", { class: "lbl", style: "margin:4px 0 0", text: t("fig.f1.setup") }), box);
  }
  add(f1);

  // F2 initial convergence
  const f2s = [{ name: t("fig.f2.all"), color: C.teal, pts: pairs(f.f2.t, f.f2.all) }];
  if (f.f2.slowest_name) f2s.push({ name: t("fig.f2.slowest", { r: f.f2.slowest_name }), color: C.orange, dash: "5 4", pts: pairs(f.f2.t, f.f2.slowest) });
  add(lineFig({ fid: "F2", name: "F2-convergence", title: t("fig.f2"), caption: t("fig.f2.cap"), zoom: true, series: f2s,
    xfmt: (v) => secs(v), yfmt: (v) => num(v) + " %", ymin: 0, ymax: 102, xlabel: t("fig.f2.x"), ylabel: t("fig.f2.y"),
    marks: f.f2.initial_s !== null ? [{ x: f.f2.initial_s, label: secs(f.f2.initial_s), color: C.ink }] : [],
    read: (x, v) => `t = ${secs(x)} · ${t("fig.f2.all")} ${pct(v[0])}` + (v[1] !== undefined && v[1] !== null ? ` · ${f.f2.slowest_name} ${pct(v[1])}` : "") }));

  // F3 latency CDF
  const a3 = f.f3.announce, w3 = f.f3.withdraw;
  if (a3.n || w3.n) {
    const s3 = [];
    if (a3.n) s3.push({ name: t("fig.f3.announce"), color: C.teal, pts: a3.pts });
    if (w3.n) s3.push({ name: t("fig.f3.withdraw"), color: C.violet, pts: w3.pts });
    add(lineFig({ fid: "F3", name: "F3-latency-cdf", title: t("fig.f3"), caption: t("fig.f3.cap", { n: num(a3.n) }), xlog: true, zeroBefore: true, series: s3, cursorLabel: t("fig.f3.cursor"),
      xfmt: (v) => (v >= 1000 ? num(v / 1000) + " s" : num(v, v < 1 ? 2 : 0) + " ms"), yfmt: (v) => num(v, 1), ymin: 0, ymax: 1, xlabel: t("fig.f3.x"), ylabel: t("fig.f3.y"),
      marks: [a3.p50 ? { x: a3.p50, label: "p50", color: C.teal } : null, a3.p99 ? { x: a3.p99, label: "p99", color: C.orange } : null].filter(Boolean),
      cursorAt: a3.p50 || undefined,
      read: (x, v) => t("fig.f3.read", { ms: ms(x), a: v[0] === null || v[0] === undefined ? "—" : num(100 * v[0], 1), w: v[1] === null || v[1] === undefined ? "—" : num(100 * v[1], 1) }) }));
  } else add(note("F3", t("fig.f3"), t("fig.none")));

  // F4 latency over time vs offered load
  const r4 = f.f4.rows;
  add(lineFig({ fid: "F4", name: "F4-latency-load", title: t("fig.f4"), caption: t("fig.f4.cap"), zoom: true, phases, y2: true, logToggle: true, gap: 5,
    series: [{ name: "p50", color: C.teal, pts: r4.map((r) => [r[0], r[1]]) }, { name: "p99", color: C.orange, pts: r4.map((r) => [r[0], r[2]]) },
      { name: t("chart.offered"), color: C.grey, dash: "4 3", axis: "r", pts: r4.map((r) => [r[0], r[3]]) }],
    xfmt: mmss, yfmt: (v) => num(v, v < 1 ? 2 : 0) + " ms", y2fmt: (v) => short(v) + "/s", xlabel: t("fig.time"), ylabel: t("fig.latency"), y2label: t("fig.offered_s"),
    read: (x, v) => `t = ${mmss(x)} · p50 ${ms(v[0])} · p99 ${ms(v[1])} · ${t("chart.offered")} ${rate(v[2])}` }));

  // F5 throughput knee (stepped-load runs)
  if (f.f5.stepped) {
    const p5 = f.f5.pts;
    add(lineFig({ fid: "F5", name: "F5-knee", title: t("fig.f5"), caption: t("fig.f5.cap"), y2: true, diag: true, xlog: true, ylog: true,
      series: [{ name: t("fig.f5.delivered"), color: C.teal, pts: p5.map((p) => [p[0], p[1]]), dots: true },
        { name: "p99", color: C.orange, axis: "r", pts: p5.map((p) => [p[0], p[2]]) }],
      cursorLabel: t("fig.f5.cursor"), xfmt: short, yfmt: short, y2fmt: (v) => num(v) + " ms", xlabel: t("fig.f5.x"), ylabel: t("fig.f5.y"), y2label: "p99",
      read: (x, v) => t("fig.f5.read", { o: num(x), d: v[0] === null ? "—" : num(v[0]), p: v[0] ? num(100 * v[0] / x) : "—", l: ms(v[1]) }) }));
  } else add(note("F5", t("fig.f5"), t("fig.f5.none")));

  // F6 backlog
  add(lineFig({ fid: "F6", name: "F6-backlog", title: t("fig.f6"), caption: t("fig.f6.cap"), zoom: true, phases, y2: true,
    series: [{ name: t("fig.f6.backlog"), color: C.orange, pts: f.f6.rows.map((r) => [r[0], r[1]]) }, { name: t("fig.f6.held"), color: C.teal, axis: "r", pts: f.f6.rows.map((r) => [r[0], r[2]]) }],
    xfmt: mmss, yfmt: short, y2fmt: short, yint: true, y2int: true, xlabel: t("fig.time"), ylabel: t("fig.f6.y"), y2label: t("fig.f6.held") }));

  // F7 heatmap
  if (f.f7.rows.length) add(heatFig({ fid: "F7", name: "F7-heatmap", title: t("fig.f7"), caption: t("fig.f7.cap", { n: f.f7.rows.length }), rows: f.f7.rows, cols: f.f7.cols_s,
    xfmt: (v) => secs(v), cursorLabel: t("fig.f7.cursor") }));

  // F8 correctness
  const k8 = card("F8", t("fig.f8"), t("fig.f8.cap"));
  const bad = f.f8.rows.filter((r) => r[3] || r[4]);
  const tot = f.f8.rows.reduce((a, r) => [a[0] + r[1], a[1] + r[2], a[2] + r[3], a[3] + r[4], a[4] + r[5]], [0, 0, 0, 0, 0]);
  k8.body.append(el("p", { class: bad.length ? "bad" : "ok", style: "margin:0;font-weight:600", text: bad.length ? t("fig.f8.bad", { n: bad.length }) : t("fig.f8.ok", { n: f.f8.rows.length }) }));
  const tb = el("tbody");
  (bad.length ? bad.slice(0, 30) : f.f8.rows.slice(0, 6)).forEach((r) => tb.append(el("tr", {}, ...r.map((v, i) => el("td", { class: "td mono" + ((i === 3 || i === 4) && v ? " bad" : ""), text: i ? num(v) : v })))));
  if (!bad.length && f.f8.rows.length > 6) tb.append(el("tr", {}, el("td", { class: "td small", colspan: "6", text: t("fig.f8.more", { n: f.f8.rows.length - 6 }) })));
  tb.append(el("tr", {}, el("td", { class: "td", style: "font-weight:600", text: t("fig.total") }), ...tot.map((v, i) => el("td", { class: "td mono" + ((i === 2 || i === 3) && v ? " bad" : ""), style: "font-weight:600", text: num(v) }))));
  k8.body.append(el("div", { class: "tbl-box" }, el("table", {}, el("thead", {}, el("tr", {}, ...["fig.f8.router", "fig.f8.expected", "fig.f8.received", "fig.f8.missing", "fig.f8.extra", "fig.f8.looped"].map((x) => el("th", { class: "th", text: t(x) })))), tb)));
  k8.tools.append(el("button", { class: "btn btn-sm noprint", type: "button", text: "CSV", onclick: () => download([f.f8.cols.join(",")].concat(f.f8.rows.map((r) => r.join(","))).join("\n") + "\n", "text/csv", "F8-correctness.csv") }));
  add(k8.sec);

  // F9 event timeline
  const x1 = Math.max(f.end_s || 0, ...(f.f9.phases.map((p) => p[2] || 0)));
  const lanes = [
    { name: t("fig.f9.ours"), marks: f.f9.sessions.map((e) => [e[0], e[2] === "up" ? C.green : e[2] === "down" ? C.red : C.grey, "c", `${e[1]} ${t("fig.f9." + e[2])}${e[3] ? " (" + e[3] + ")" : ""}`]) },
    { name: t("fig.f9.dut"), marks: f.f9.dut_sessions.map((e) => [e[0], C.blue, "c", t("fig.f9.dut_est", { n: e[1] })]) },
    { name: t("fig.f9.events"), marks: f.f9.events.filter((e) => e[1] !== "phase").map((e) => [e[0], e[2] === "error" ? C.red : e[2] === "warning" ? C.amber : C.grey, "sq", e[3]]) },
  ];
  add(timelineFig({ fid: "F9", name: "F9-timeline", title: t("fig.f9"), caption: t("fig.f9.cap"), phases, lanes, x0: Math.min(0, ...f.f9.phases.map((p) => p[1])), x1: x1 || 1 }));

  // F10 DUT resources
  if (f.f10.rows.length) {
    const r10 = f.f10.rows;
    add(lineFig({ fid: "F10", name: "F10-dut-resources", title: t("fig.f10"), caption: t("fig.f10.cap"), zoom: true, phases, y2: true,
      series: [{ name: t("fig.f10.mem"), color: C.teal, pts: r10.map((r) => [r[0], r[1]]) }, { name: "CPU", color: C.orange, pts: r10.map((r) => [r[0], r[2]]) },
        { name: t("fig.f10.sessions"), color: C.grey, dash: "4 3", axis: "r", step: true, pts: r10.map((r) => [r[0], r[3]]) }],
      xfmt: mmss, yfmt: (v) => num(v) + " %", y2fmt: (v) => num(v), y2int: true, ymin: 0, ymax: 100, xlabel: t("fig.time"), ylabel: "%", y2label: t("fig.f10.sessions") }));
  } else add(note("F10", t("fig.f10"), t("fig.f10.none")));

  add(note("F11", t("fig.f11"), t("fig.f11.none")));

  // F12 generator health
  const nodes = Object.keys(f.f12.nodes || {});
  add(lineFig({ fid: "F12", name: "F12-generator", title: t("fig.f12"), caption: t("fig.f12.cap"), zoom: true, phases, y2: true, gap: 5,
    series: [...nodes.map((n, i) => ({ name: n, color: PALETTE[i % PALETTE.length], pts: pairs(f.f12.t, f.f12.nodes[n]) })),
      { name: t("fig.f12.lag"), color: C.ink, dash: "2 3", axis: "r", pts: pairs(f.f12.t, f.f12.send_lag_p99_ms) }],
    xfmt: mmss, yfmt: (v) => num(v) + " %", y2fmt: (v) => num(v, 1) + " ms", ymin: 0, ymax: 100, xlabel: t("fig.time"), ylabel: t("fig.f12.y"), y2label: t("fig.f12.lag"),
    hmarks: [{ y: 70, label: t("fig.f12.limit"), color: C.amber }] }));
}
function short(v) { return Math.abs(v) >= 1e6 ? num(v / 1e6, 1) + "M" : Math.abs(v) >= 1000 ? num(v / 1000, v % 1000 ? 1 : 0) + "k" : num(v); }

/** comparison of 2-5 runs: overlaid F2, F3, F5, F10 (spec §12) */
export function renderCompare(host, runs) {
  host.innerHTML = "";
  const col = (i) => PALETTE[i % PALETTE.length];
  const lbl = (r) => `${r.label}`;
  host.append(lineFig({ fid: "F2", name: "compare-F2", title: t("fig.f2"), caption: t("fig.cmp.f2"),
    series: runs.filter((r) => r.f).map((r, i) => ({ name: lbl(r), color: col(i), pts: pairs(r.f.f2.t, r.f.f2.all) })),
    xfmt: (v) => secs(v), yfmt: (v) => num(v) + " %", ymin: 0, ymax: 102, xlabel: t("fig.f2.x"), ylabel: t("fig.f2.y") }));
  host.append(lineFig({ fid: "F3", name: "compare-F3", title: t("fig.f3"), caption: t("fig.cmp.f3"), xlog: true, zeroBefore: true, cursorLabel: t("fig.f3.cursor"),
    series: runs.filter((r) => r.f && r.f.f3.announce.n).map((r, i) => ({ name: lbl(r), color: col(i), pts: r.f.f3.announce.pts })),
    xfmt: (v) => (v >= 1000 ? num(v / 1000) + " s" : num(v, v < 1 ? 2 : 0) + " ms"), yfmt: (v) => num(v, 1), ymin: 0, ymax: 1, xlabel: t("fig.f3.x"), ylabel: t("fig.f3.y") }));
  const k5 = runs.filter((r) => r.f && r.f.f5.stepped);
  host.append(k5.length ? lineFig({ fid: "F5", name: "compare-F5", title: t("fig.f5"), caption: t("fig.cmp.f5"), diag: true, xlog: true, ylog: true, cursorLabel: t("fig.f5.cursor"),
    series: k5.map((r, i) => ({ name: lbl(r), color: col(runs.indexOf(r)), dots: true, pts: r.f.f5.pts.map((p) => [p[0], p[1]]) })),
    xfmt: short, yfmt: short, xlabel: t("fig.f5.x"), ylabel: t("fig.f5.y") }) : note("F5", t("fig.f5"), t("fig.f5.none")));
  const k10 = runs.filter((r) => r.f && r.f.f10.rows.length);
  host.append(k10.length ? lineFig({ fid: "F10", name: "compare-F10", title: t("fig.f10"), caption: t("fig.cmp.f10"),
    series: k10.map((r) => ({ name: lbl(r), color: col(runs.indexOf(r)), pts: r.f.f10.rows.map((x) => [x[0], x[1]]) })),
    xfmt: mmss, yfmt: (v) => num(v) + " %", ymin: 0, ymax: 100, xlabel: t("fig.time"), ylabel: t("fig.f10.mem") }) : note("F10", t("fig.f10"), t("fig.f10.none")));
}
