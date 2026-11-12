// Latency vs offered load (design: value cursor + slider, readout line, 5/2/1 min zoom, window slider).
import { t, num } from "./i18n.js";

const TEAL = "#0E6E66", ORANGE = "#C2410C", GREY = "#8A9097", VIOLET = "#5B4FC4";

function ago(s) {
  const v = Math.round(s);
  if (v <= 0) return t("chart.now");
  return "−" + Math.floor(v / 60) + ":" + String(v % 60).padStart(2, "0");
}

export class LatencyChart {
  // t0 (s): label the time axis as elapsed m:ss from t0 (report page) instead of "ago" (live view)
  constructor(host, readout, curSlider, winSlider, zoomButtons, t0 = null) {
    this.t0 = t0;
    this.host = host; this.readout = readout; this.cur = curSlider; this.win = winSlider; this.zb = zoomButtons;
    this.data = [[], [], [], []];
    this.span = 300; this.pan = null;   // pan = right edge (null = follow the newest data)
    this.cursorX = null;                // null = follow the newest point
    const opts = {
      width: Math.max(300, host.clientWidth), height: 240, legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 8 }, x: true, y: false },
      scales: { x: { time: false }, ms: { auto: true, range: (u, mn, mx) => [0, Math.max(50, mx * 1.1)] },
                rate: { auto: true, range: (u, mn, mx) => [0, Math.max(10, mx * 1.15)] } },
      series: [{},
        { label: "p50", scale: "ms", stroke: TEAL, width: 2.5, spanGaps: true },
        { label: "p99", scale: "ms", stroke: ORANGE, width: 2.5, spanGaps: true },
        { label: "offered", scale: "rate", stroke: GREY, width: 2, dash: [4, 3] }],
      axes: [
        { stroke: "#545A61", grid: { stroke: "#ECEEEA" }, font: "10px IBM Plex Mono", values: (u, vals) => vals.map((v) => this.tl(v)) },
        { scale: "ms", stroke: "#545A61", grid: { stroke: "#ECEEEA" }, font: "10px IBM Plex Mono", values: (u, vals) => vals.map((v) => num(v) + " ms"), size: 64 },
        { scale: "rate", side: 1, stroke: "#545A61", grid: { show: false }, font: "10px IBM Plex Mono", values: (u, vals) => vals.map((v) => num(v) + "/s"), size: 64 }],
      hooks: { setCursor: [(u) => this.onCursor(u)] },
    };
    this.u = new uPlot(opts, this.data, host);
    new ResizeObserver(() => { const w = host.clientWidth; if (w > 0) this.u.setSize({ width: w, height: 240 }); }).observe(host);
    zoomButtons.forEach((b) => b.addEventListener("click", () => {
      this.span = +b.dataset.span;
      zoomButtons.forEach((x) => x.classList.toggle("zon", x === b));
      this.redraw();
    }));
    this.win.addEventListener("input", () => {
      const [lo, hi] = this.extent();
      const maxRight = hi, minRight = Math.min(hi, lo + this.span);
      const right = minRight + (maxRight - minRight) * (+this.win.value / 100);
      this.pan = +this.win.value >= 100 ? null : right;
      this.redraw();
    });
    this.cur.addEventListener("input", () => {
      const [a, b] = this.window();
      this.cursorX = +this.cur.value >= 100 && this.pan === null ? null : a + (b - a) * (+this.cur.value / 100);
      this.placeCursor(); this.read();
    });
    host.addEventListener("mouseleave", () => { this.read(); });
  }
  tl(v) {
    if (this.t0 === null) return ago(this.last() - v);
    const s = Math.max(0, Math.round(v - this.t0));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  last() { const x = this.data[0]; return x.length ? x[x.length - 1] : 0; }
  extent() { const x = this.data[0]; return x.length ? [x[0], x[x.length - 1]] : [0, 1]; }
  window() {
    const [lo, hi] = this.extent();
    const right = this.pan === null ? hi : this.pan;
    return [Math.max(lo, right - this.span), Math.max(right, lo + 1)];
  }
  setData(xs, p50, p99, off) { this.data = [xs, p50, p99, off]; this.redraw(); }
  redraw() {
    this.u.setData(this.data, false);
    const [a, b] = this.window();
    this.u.setScale("x", { min: Math.max(a, b - this.span), max: b });
    const [lo, hi] = this.extent();
    this.win.disabled = hi - lo <= this.span;
    this.placeCursor(); this.read();
  }
  idxAt(x) {
    const xs = this.data[0]; if (!xs.length) return null;
    let i = xs.findIndex((v) => v >= x); if (i < 0) i = xs.length - 1;
    return i;
  }
  placeCursor() {
    const xs = this.data[0]; if (!xs.length) return;
    const x = this.cursorX === null ? xs[xs.length - 1] : this.cursorX;
    const left = this.u.valToPos(x, "x");
    if (left >= 0 && left <= this.u.bbox.width / devicePixelRatio) this.u.setCursor({ left, top: 20 });
  }
  onCursor(u) { if (u.cursor.idx !== null && u.cursor.idx !== undefined) this.read(u.cursor.idx); }
  read(idx) {
    const xs = this.data[0];
    if (!xs.length) { this.readout.textContent = ""; return; }
    if (idx === undefined) idx = this.cursorX === null ? xs.length - 1 : this.idxAt(this.cursorX);
    const [a, b] = this.window();
    this.cur.value = String(Math.round(100 * (xs[idx] - a) / Math.max(1, b - a)));
    const v = (s) => (this.data[s][idx] === null || this.data[s][idx] === undefined ? "—" : num(this.data[s][idx]));
    this.readout.textContent = `t = ${this.tl(xs[idx])} · p50 ${v(1)} ms · p99 ${v(2)} ms · ${t("chart.offered")} ${v(3)} ${t("unit.prefixes_s")}`;
  }
}
