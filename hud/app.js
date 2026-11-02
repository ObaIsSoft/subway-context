/* Shell, routing and polling. */
import { api, duration, get, h, icon, listen, set, subscribe, tickAll, toast, tokens } from "./lib.js";
import { hasModelEdits, ledger, ledgerPage, leasesPage, modelsPage, openLogs, overview, renderLedgerEntries, terminalsPage, terminalsState, threads, threadsPage, usagePage, workersPage } from "./views.js";
import { helpPage } from "./help.js";

const POLL_MS = 1000;
const FEED_MS = 3000;
const PAGES = {
  overview: { label: "Overview", sub: "What needs you, and what's running" },
  threads: { label: "Threads", sub: "Chats across connected coding agents" },
  terminals: { label: "Terminals", sub: "Commands run in terminals your user has shared" },
  usage: { label: "Usage", sub: "Limits and tokens, as reported by each tool" },
  workers: { label: "Workers", sub: "Delegated agents and their permissions" },
  models: { label: "Models", sub: "How each model is ranked, and where that came from" },
  leases: { label: "Leases", sub: "Who holds which file" },
  ledger: { label: "Ledger", sub: "Shared decisions across agents" },
  help: { label: "Help", sub: "Guides, terms, and commands for Subway Context" },
};

const nav = {};
const content = h("main", { class: "main", id: "content" });
const heading = h("h1", { class: "page-title" });
const subheading = h("div", { class: "page-sub" });
const linkDot = h("span", { class: "dot" });
const linkText = h("span", { class: "num" }, "Connecting");
const alertBar = h("div", { class: "alert-bar", role: "alert", hidden: true }, "The Subway server isn't responding, so what's shown may be out of date.");
const pageBody = h("div", { style: "display:flex;flex-direction:column;gap:inherit" });

function currentPage() {
  const name = location.hash.slice(1);
  return PAGES[name] ? name : "overview";
}

function buildShell() {
  const rail = h("nav", { class: "rail", "aria-label": "Sections" }, h("div", { class: "rail-brand", "aria-hidden": "true" }, "S"),
    Object.entries(PAGES).filter(([key]) => key !== "help").map(([key, page]) => {
      const badge = h("span", { class: "nav-badge", hidden: true });
      const item = h("button", { class: "nav-item", type: "button", on: { click: () => { location.hash = key; } } }, icon(key), page.label, badge);
      nav[key] = { item, badge };
      return item;
    }));
  const helpBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "Help", on: { click: () => { location.hash = "help"; } } }, "?");
  const theme = h("button", { class: "icon-btn", type: "button", "aria-label": "Change theme", on: { click: cycleTheme } }, icon("theme"));
  content.append(
    h("header", { class: "topbar" },
      h("div", {}, h("div", { class: "title-mark" }, h("b", {}, "Subway"), " Context"), heading, subheading),
      h("div", { class: "top-actions" }, h("span", { class: "pill", role: "status" }, linkDot, linkText), helpBtn, theme)),
    alertBar, pageBody);
  document.getElementById("root").append(h("div", { class: "shell" }, rail, content));
  document.body.append(h("div", { class: "toast", id: "toast", role: "status", hidden: true }));
}

const THEMES = ["auto", "dark", "light"];
function storedTheme() {
  try { return localStorage.getItem("subway-theme") || "auto"; } catch (e) { return "auto"; }
}
function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
}
function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("subway-theme", next); } catch (e) { /* Private windows can refuse storage. */ }
  applyTheme(next);
}

function focusInside() {
  const el = document.activeElement;
  return !!el && pageBody.contains(el) && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

function render() {
  const d = get("status");
  const page = currentPage();
  for (const [key, val] of Object.entries(nav)) {
    if (!val) continue;
    const { item } = val;
    if (key === page) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  heading.textContent = PAGES[page].label;
  subheading.textContent = PAGES[page].sub;
  if (!d && page !== "help") return;
  if (page === "threads") loadThreads();
  if (page === "terminals") loadTerminals();
  if (focusInside()) return; // Never wipe out what someone is typing.
  const open = [...pageBody.querySelectorAll("details[open] summary")].map((x) => x.closest(".card")?.querySelector(".provider-name")?.textContent);
  const views = {
    overview: () => overview(d), usage: () => usagePage(d), workers: () => workersPage(d),
    threads: () => threadsPage(d, threads.chats, () => loadThreads(true), threads.error),
    terminals: () => terminalsPage(d, terminalsState.rows, () => loadTerminals(true), terminalsState.error, terminalsState.loading, terminalsState.loadedAt > 0 && !terminalsState.error && (Date.now() - terminalsState.loadedAt > 20000)),
    models: () => modelsPage(d),
    leases: () => leasesPage(d), ledger: () => ledgerPage(d, switchTrack),
    help: () => helpPage(),
  };
  const pageTop = window.scrollY;
  const out = views[page]();
  pageBody.replaceChildren(...[out].flat(Infinity).filter(Boolean));
  window.scrollTo(0, pageTop);
  pageBody.querySelectorAll("details").forEach((det) => {
    if (open.includes(det.closest(".card")?.querySelector(".provider-name")?.textContent)) det.open = true;
  });
}

async function loadFeed() {
  try {
    const d = await api(`/api/ledger?name=${encodeURIComponent(ledger.track)}`);
    ledger.entries = d.entries || [];
    set("feed", ledger.entries);
  } catch (e) { /* The connection state already shows this. */ }
}

async function loadThreads(force = false) {
  if (threads.loading || (!force && Date.now() - threads.loadedAt < 30000)) return;
  threads.loading = true;
  try {
    const data = await api("/api/threads?limit=200");
    threads.chats = data.chats || [];
    threads.loadedAt = Date.now();
    threads.error = "";
  } catch (e) {
    threads.error = String(e);
    threads.loadedAt = Date.now();
  } finally {
    threads.loading = false;
    if (currentPage() === "threads") render();
  }
}

async function loadTerminals(force = false) {
  if (terminalsState.loading || (!force && Date.now() - terminalsState.loadedAt < 15000)) return;
  terminalsState.loading = true;
  try {
    const data = await api("/api/terminals");
    terminalsState.rows = data.terminals || [];
    terminalsState.loadedAt = Date.now();
    terminalsState.error = "";
  } catch (e) {
    terminalsState.error = String(e);
    terminalsState.loadedAt = Date.now();
  } finally {
    terminalsState.loading = false;
    if (currentPage() === "terminals") render();
  }
}

function switchTrack(name) {
  ledger.track = name;
  ledger.entries = [];
  render();
  loadFeed();
}

function updateBadges(d) {
  const waiting = (d.permissions || []).length + (d.workers || []).filter((w) => w.status === "pending_approval").length;
  const { badge } = nav.workers;
  badge.hidden = !waiting;
  badge.textContent = waiting;
  document.title = waiting ? `(${waiting}) Subway Context` : "Subway Context";
}

let inFlight = null;
function refresh() {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    const t0 = performance.now();
    try {
      await api("/api/ping");
      const ping = Math.round(performance.now() - t0);
      const d = await api("/api/status");
      const { timestamp, time_iso: _iso, ...stable } = d;
      /* The rest of the page only needs a re-render when the data, or the minute, changes. */
      set("status", d);
      set("slice", { ...stable, minute: Math.floor(timestamp / 60) });
      linkDot.className = "dot ok";
      linkText.textContent = `Ping ${ping} ms · v${d.version}`;
      alertBar.hidden = true;
      pingLowLimits(d.settings?.low_limits || []);
      pingBudgets(d.settings?.budgets || []);
    } catch (e) {
      linkDot.className = "dot bad";
      linkText.textContent = "Disconnected";
      alertBar.textContent = "The Subway server isn't responding, so what's shown may be out of date.";
      alertBar.hidden = false;
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

/* Pings once per limit window at each level, then stays quiet until it resets. */
function pingLowLimits(items) {
  let seen = {};
  try { seen = JSON.parse(localStorage.getItem("subway-pings") || "{}"); } catch (e) { seen = {}; }
  for (const item of items) {
    const level = item.left_pct <= 10 ? 10 : 20; /* Once at 20%, and once more at 10%. */
    const key = `${item.provider}|${item.pool}|${item.label}|${Math.round(item.resets_at || 0)}|${level}`;
    if (seen[key]) continue;
    seen[key] = 1;
    const reset = item.resets_at ? ` Resets in ${duration(item.resets_at - Date.now() / 1000)}.` : "";
    toast(`${item.provider}: ${item.label} limit at ${Math.round(item.left_pct)}%.${reset}`);
  }
  try { localStorage.setItem("subway-pings", JSON.stringify(seen)); } catch (e) { /* Private windows can refuse storage. */ }
}

export function pingBudgets(data) {
  const items = Array.isArray(data) ? data : (data?.settings?.budgets || []);
  let seen = {};
  try { seen = JSON.parse(localStorage.getItem("subway-pings") || "{}"); } catch (e) { seen = {}; }
  for (const b of items) {
    if (b.status !== "warning" && b.status !== "reached") continue;
    const key = `budget|${b.scope}|${b.status}|${b.limit}`;
    if (seen[key]) continue;
    seen[key] = 1;
    const x = tokens(b.used || 0);
    const y = tokens(b.limit || 0);
    if (b.status === "warning") {
      toast(`Budget almost gone: ${b.scope} at ${x} of ${y}. Add more on Overview.`);
    } else if (b.status === "reached") {
      toast(`Budget reached: ${b.scope} at ${x} of ${y}. Work has stopped. Add more on Overview.`);
    }
  }
  try { localStorage.setItem("subway-pings", JSON.stringify(seen)); } catch (e) { /* Private windows can refuse storage. */ }
}

async function statusLoop() {
  if (!document.hidden) await refresh();
  setTimeout(statusLoop, POLL_MS);
}
async function feedLoop() {
  if (!document.hidden && currentPage() === "ledger") await loadFeed();
  setTimeout(feedLoop, FEED_MS);
}

let lastTracksJson = null;
let lastWorkersSliceJson = null;
let lastPage = null;

function updateFeedInPlace() {
  const feed = pageBody.querySelector(".feed");
  if (!feed) {
    render();
    return;
  }
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const feedTop = feed.scrollTop;
  const newChildren = renderLedgerEntries(ledger.entries);
  feed.replaceChildren(...[newChildren].flat(Infinity).filter(Boolean));
  feed.scrollTop = atBottom ? feed.scrollHeight : feedTop;
}

buildShell();
applyTheme(storedTheme());
subscribe("slice", (slice) => {
  const page = currentPage();
  const pageChanged = page !== lastPage;
  lastPage = page;
  if (page === "models") {
    if (pageChanged || !hasModelEdits()) {
      render();
    }
  } else if (page === "ledger") {
    const tracks = (slice && slice.tracks) || {};
    const tracksSummary = JSON.stringify(Object.entries(tracks).map(([k, v]) => [k, v?.count || 0]).sort());
    if (tracksSummary !== lastTracksJson) {
      lastTracksJson = tracksSummary;
      render();
    }
  } else if (page === "workers") {
    const workersSlice = {
      workers: slice?.workers,
      permissions: slice?.permissions,
      settings: slice?.settings,
      history: slice?.history,
    };
    const workersJson = JSON.stringify(workersSlice);
    if (openLogs.size > 0 && workersJson === lastWorkersSliceJson) {
      // Data unchanged and inline log open: skip re-rendering
    } else {
      lastWorkersSliceJson = workersJson;
      render();
    }
  } else {
    render();
  }
  updateBadges(get("status") || {});
});
subscribe("feed", () => {
  if (currentPage() === "ledger") updateFeedInPlace();
});
window.addEventListener("hashchange", () => {
  lastTracksJson = null;
  lastWorkersSliceJson = null;
  render();
  if (currentPage() === "ledger") loadFeed();
  if (currentPage() === "threads") loadThreads();
});
document.addEventListener("subway:rerender", () => { render(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
document.addEventListener("subway:refresh", refresh);
setInterval(tickAll, 1000);
render();
statusLoop();
feedLoop();
listen(refresh);
