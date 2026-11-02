/* Shared helpers: DOM building, API calls, formatting, and the store. */
const TOKEN = document.querySelector('meta[name="subway-token"]').content;

/* Everything goes through text nodes, so agent-written text can never become markup. */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, kids);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  append(el, kids);
  return el;
}

function applyAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "class") el.setAttribute("class", v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
}

function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
}

const ICONS = {
  overview: "M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10",
  usage: "M12 3a9 9 0 1 0 9 9h-9V3zM15 3.5A9 9 0 0 1 20.5 9H15V3.5z",
  workers: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M17 4a4 4 0 0 1 0 7M18 14a7 7 0 0 1 4 7",
  threads: "M3 4h18v13H8l-5 4zM7 9h10M7 13h7",
  leases: "M6 10V7a6 6 0 0 1 12 0v3M5 10h14v11H5zM12 14v3",
  ledger: "M6 3h12v18H6zM9 8h6M9 12h6M9 16h4",
  terminals: "M4 5h16v14H4zM7 10l3 3-3 3M13 16h4",
  models: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5",
  bell: "M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 21h4",
  theme: "M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
};
export function icon(name) {
  return s("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    s("path", { d: ICONS[name] }));
}

export async function api(path, body) {
  const opts = { headers: { "X-Subway-Token": TOKEN }, cache: "no-store" };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

/* Pushed by the server the moment a worker or permission changes. Polling stays as the fallback. */
export async function listen(onEvent) {
  for (;;) {
    try {
      const res = await fetch("/api/events", { headers: { "X-Subway-Token": TOKEN }, cache: "no-store" });
      const reader = res.body.getReader();
      for (;;) {
        const { done } = await reader.read();
        if (done) break;
        onEvent();
      }
    } catch (e) { /* The connection state already shows this. */ }
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
  }
}

export function duration(secs) {
  if (secs < 60) return `${Math.max(0, Math.round(secs))}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const hours = Math.floor(m / 60);
  if (hours < 24) return `${hours}h ${m % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
export function tokens(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}
export const pct = (v) => `${Number.isInteger(v) ? v : Number(v).toFixed(1)}%`;

/* Elements carrying these attributes count on their own, without a re-render. */
export function ticking(tag, attrs, epoch, format) {
  const el = h(tag, { ...attrs, "data-epoch": epoch, "data-format": format });
  paintTick(el, Date.now() / 1000);
  return el;
}
function paintTick(el, now) {
  const at = Number(el.dataset.epoch);
  el.textContent = el.dataset.format === "age" ? `${duration(now - at)} ago` : duration(at - now);
}
export function tickAll() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-epoch]").forEach((el) => paintTick(el, now));
}

/* Minimal store: a slice only notifies its subscribers when its data really changed. */
const state = {};
const subscribers = new Map();
const lastJson = new Map();
export const get = (key) => state[key];
export function subscribe(key, fn) {
  if (!subscribers.has(key)) subscribers.set(key, new Set());
  subscribers.get(key).add(fn);
  return () => subscribers.get(key).delete(fn);
}
export function set(key, value) {
  const json = JSON.stringify(value);
  state[key] = value;
  if (lastJson.get(key) === json) return;
  lastJson.set(key, json);
  (subscribers.get(key) || []).forEach((fn) => fn(value));
}

let toastTimer = 0;
export function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

export async function act(path, body) {
  try {
    const res = await api(path, body);
    toast(res.result || "Done");
    document.dispatchEvent(new Event("subway:refresh"));
    return res;
  } catch (e) {
    toast(e.message);
    return null;
  }
}

export function field(label, control, full) {
  const id = `f${Math.random().toString(36).slice(2, 9)}`;
  control.id = id;
  return h("div", { class: `field${full ? " full" : ""}` }, h("label", { for: id }, label), control);
}

export function modal(title, body, headExtra) {
  const dlg = h("dialog", { "aria-label": title });
  const close = h("button", { class: "btn", type: "button", on: { click: () => dlg.close() } }, "Close");
  dlg.append(h("div", { class: "dialog-head" }, h("div", { class: "card-title wrap" }, title), h("div", { class: "btns" }, headExtra, close)),
    h("div", { class: "dialog-body" }, body));
  // Close on a click on the backdrop: press and release both outside the dialog box, so a text
  // selection that ends outside, or a click on the dialog's own padding, keeps it open.
  const outside = (e) => {
    const r = dlg.getBoundingClientRect();
    return e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom);
  };
  let pressedOutside = false;
  dlg.addEventListener("pointerdown", (e) => { pressedOutside = outside(e); });
  dlg.addEventListener("click", (e) => { if (pressedOutside && outside(e)) dlg.close(); pressedOutside = false; });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

export function ask(title, label) {
  return new Promise((resolve) => {
    const input = h("input", { autocomplete: "off" });
    let value = null;
    const form = h("form", { class: "form", on: { submit: (e) => { e.preventDefault(); value = input.value.trim(); dlg.close(); } } },
      field(label, input, true), h("div", { class: "btns field full" }, h("button", { class: "btn primary", type: "submit" }, "OK")));
    const dlg = modal(title, form);
    dlg.addEventListener("close", () => resolve(value));
    input.focus();
  });
}

export function providerColour(id) {
  if (!id) return "var(--gold)";
  const cleanId = String(id).toLowerCase().trim();
  if (typeof document !== "undefined" && document.documentElement) {
    const val = getComputedStyle(document.documentElement).getPropertyValue(`--series-${cleanId}`);
    if (val && val.trim()) return `var(--series-${cleanId})`;
  }
  if (["claude", "antigravity", "opencode", "copilot"].includes(cleanId)) {
    return `var(--series-${cleanId})`;
  }
  let hash = 0;
  for (let i = 0; i < cleanId.length; i++) {
    hash = ((hash << 5) - hash) + cleanId.charCodeAt(i);
    hash |= 0;
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 65%, 55%)`;
}
