// Texts from ui/i18n/labels.csv (GET /api/i18n). English is the fallback.
let DATA = { languages: [], texts: {} };
let LANG = "en";
const listeners = [];

function stored() { try { return localStorage.getItem("tgen.lang"); } catch (e) { return null; } }
function store(v) { try { localStorage.setItem("tgen.lang", v); } catch (e) { /* private window */ } }

export async function loadTexts() {
  const r = await fetch("/api/i18n");
  DATA = await r.json();
  const tags = DATA.languages.map((l) => l.tag);
  const pref = stored();
  if (pref && tags.includes(pref)) LANG = pref;
  else LANG = (navigator.language || "en").toLowerCase().startsWith("sr") && tags.includes("sr-Cyrl") ? "sr-Cyrl" : "en";
}

export const lang = () => LANG;
export const languages = () => DATA.languages;
export const meta = () => DATA.languages.find((l) => l.tag === LANG) || {};

export function t(key, vars) {
  const row = DATA.texts[key];
  let s = row ? (row[LANG] || row.en || key) : key;
  if (vars) for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
  return s;
}

/** number with the language's grouping and decimal sign */
export function num(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const m = meta(), grp = m.number_group === undefined ? " " : m.number_group, dec = m.decimal || ".";
  const [i, f] = Number(v).toFixed(digits).split(".");
  const neg = i.startsWith("-");
  const body = (neg ? i.slice(1) : i).replace(/\B(?=(\d{3})+(?!\d))/g, grp === " " ? " " : grp);
  return (neg ? "−" : "") + body + (f ? dec + f : "");
}

export function apply(root = document) {
  document.documentElement.lang = meta().html || LANG;
  root.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  root.querySelectorAll("[data-t-title]").forEach((el) => { el.title = t(el.dataset.tTitle); });
  root.querySelectorAll("[data-t-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.tAria)); });
  root.querySelectorAll("[data-coming]").forEach((el) => { el.textContent = t("ui.coming", { step: el.dataset.coming }); });
}

export function setLang(tag) { LANG = tag; store(tag); apply(); listeners.forEach((f) => f()); }
export function onChange(f) { listeners.push(f); }
