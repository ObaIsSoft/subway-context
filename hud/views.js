/* One function per page. Each returns elements built from the latest status. */
import { h, api, act, ask, field, get, icon, modal, pct, ticking, toast, tokens } from "./lib.js";
import { lowPools, nextResets, providerCard, reachesLimit, spendChart } from "./usage.js";

let resetCycle = { index: 0, lastAdvanced: 0 };

const WORKER_STATE = {
  pending_approval: ["Awaiting approval", "gold"], launching: ["Launching", "gold"], running: ["Running", "gold"], stopping: ["Stopping", ""],
  completed: ["Completed", "ok"], failed: ["Failed", "bad"], timed_out: ["Timed out", "bad"], stopped: ["Stopped", ""], rejected: ["Rejected", ""],
  budget_stopped: ["Stopped at budget", "bad"],
};

/* ---- Permissions: the first thing on the page when an agent is waiting ---- */
export function permissionsCard(requests) {
  if (!requests.length) return null;
  const answer = (id, decision) => act("/api/answer_permission", { id, decision });
  return h("section", { class: "card", "aria-label": "Permission requests" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title" }, `${requests.length} permission request${requests.length > 1 ? "s" : ""} waiting`)),
    requests.map((r) => h("div", { class: "perm" },
      h("div", { class: "row-sub" }, `${r.role || r.worker_id} (${String(r.provider).toUpperCase()}) wants ${r.tool}`),
      h("div", { class: "perm-cmd mono" }, r.detail),
      h("div", { class: "btns" },
        h("button", { class: "btn primary", type: "button", on: { click: () => answer(r.id, "allow") } }, "Allow once"),
        h("button", { class: "btn", type: "button", title: (r.rules || []).join(", "), on: { click: () => answer(r.id, "allow_always") } }, "Always allow for this worker"),
        h("button", { class: "btn danger", type: "button", on: { click: () => answer(r.id, "deny") } }, "Deny")))));
}

/* ---- Permission mode ---- */
export function modeControl(settings) {
  const modes = settings.permission_modes || {};
  const current = settings.permission_mode;
  return h("section", { class: "card", "aria-labelledby": "mode-title" },
    h("div", {}, h("h2", { class: "card-title", id: "mode-title" }, "Worker permissions"),
      h("div", { class: "card-sub" }, "How spun agents get permission to act. You can override this per worker.")),
    h("div", { class: "seg", role: "group", "aria-labelledby": "mode-title" }, Object.entries(modes).map(([key, label]) =>
      h("button", { type: "button", "aria-pressed": String(key === current), on: { click: () => act("/api/settings", { permission_mode: key }) } }, label))),
    current === "auto_approve" ? h("div", { class: "err" }, "Workers will not ask before running commands.") : null);
}

export function settingsCard(settings) {
  const s = settings || {};
  const currentCompaction = s.compaction;
  const modes = [
    { key: "suggest", label: "Suggest" },
    { key: "auto", label: "Auto" },
    { key: "never", label: "Never" },
  ];

  const compactionHints = {
    suggest: "Warns once when a chat's context gets expensive, with a ready /compact line. You decide.",
    auto: "Warns the same way, and compacts OpenCode sessions itself. Claude Code compacts by itself near its limit.",
    never: "No warnings.",
  };
  const compactionHint = compactionHints[currentCompaction];

  const compactionSeg = h("div", { class: "seg", role: "group", "aria-labelledby": "settings-compaction-title" },
    modes.map(({ key, label }) =>
      h("button", {
        type: "button",
        "aria-pressed": String(currentCompaction === key),
        on: { click: () => act("/api/settings", { compaction: key }) },
      }, label)
    )
  );

  const compactAtVal = (s.compact_at_tokens != null && s.compact_at_tokens !== "") ? s.compact_at_tokens : 300000;
  const compactAtInput = h("input", {
    type: "number",
    id: "settings-compact-at",
    min: 100000,
    max: 1000000,
    step: 1000,
    value: compactAtVal,
    on: {
      change: (e) => {
        const n = Number(e.target.value);
        const at = Number.isFinite(n) ? Math.max(100000, Math.min(1000000, Math.round(n))) : 300000;
        e.target.value = at;
        return act("/api/settings", { compaction: currentCompaction || "suggest", compact_at_tokens: at });
      },
    },
  });

  const autostartCheck = h("input", {
    type: "checkbox",
    id: "hud-autostart",
    checked: Boolean(s.hud_autostart),
    on: { change: (e) => act("/api/settings", { hud_autostart: e.target.checked }) },
  });

  const openOnUseCheck = h("input", {
    type: "checkbox",
    id: "hud-open-on-use",
    checked: Boolean(s.hud_open_on_use),
    on: { change: (e) => act("/api/settings", { hud_open_on_use: e.target.checked }) },
  });

  return h("section", { class: "card", "aria-labelledby": "settings-title" },
    h("div", {},
      h("h2", { class: "card-title", id: "settings-title" }, "Settings"),
      h("div", { class: "card-sub" }, "Context compaction and startup behavior.")),
    h("div", { class: "field" },
      h("span", { class: "label", id: "settings-compaction-title" }, "Compaction"),
      compactionSeg,
      compactionHint ? h("div", { class: "card-sub" }, compactionHint) : null),
    h("div", { class: "field" },
      h("label", { class: "label", for: "settings-compact-at" }, "Compact at (tokens)"),
      compactAtInput,
      h("div", { class: "card-sub" }, "Ask for compaction near this context size (100,000 to 1,000,000).")),
    h("div", { class: "switches" },
      h("label", { class: "switch-row", for: "hud-autostart" },
        h("span", { class: "switch-label" }, "Start the HUD with Subway"),
        h("span", { class: "switch" }, autostartCheck, h("span", { class: "slider" }))),
      h("div", { class: "card-sub" }, "Starts the dashboard whenever a chat connected to Subway opens, so a reload never leaves it down."),
      h("label", { class: "switch-row", for: "hud-open-on-use" },
        h("span", { class: "switch-label" }, "Open the HUD when Subway is used"),
        h("span", { class: "switch" }, openOnUseCheck, h("span", { class: "slider" }))),
      h("div", { class: "card-sub" }, "Opens the dashboard in your editor the first time a chat uses Subway.")));
}

/* ---- Chat Context Card ---- */
export function chatContextCard(ctx) {
  if (!ctx || ctx.pct == null) return null;
  const segmentsTag = (ctx.segments && ctx.segments > 1)
    ? h("span", { class: "tag" }, `${ctx.segments} segments`)
    : null;
  const pctNum = Math.min(100, Math.max(0, Number(ctx.pct) || 0));
  const tokenStr = (ctx.tokens != null && ctx.window != null)
    ? `${tokens(ctx.tokens)} of ${tokens(ctx.window)}`
    : "";
  const barClass = pctNum >= 90 ? "bar-fill bad" : (pctNum >= 75 ? "bar-fill gold" : "bar-fill");

  return h("section", { class: "card", "aria-labelledby": "chat-context-title" },
    h("div", { class: "card-head" },
      h("h2", { class: "card-title", id: "chat-context-title" }, "This chat"),
      segmentsTag ? h("div", { class: "tags" }, segmentsTag) : null),
    h("div", { class: "context-gauge" },
      h("div", { class: "context-gauge-head" },
        h("span", { class: "context-gauge-pct" }, `${ctx.pct}%`),
        tokenStr ? h("span", { class: "context-gauge-tokens" }, tokenStr) : null),
      h("div", { class: "bar", role: "progressbar", "aria-valuenow": String(pctNum), "aria-valuemin": "0", "aria-valuemax": "100" },
        h("div", { class: barClass, style: `width: ${pctNum}%` }))));
}

/* ---- Overview ---- */
export function budgetsCard(data) {
  const budgets = Array.isArray(data) ? data : data?.settings?.budgets;
  if (!budgets || !budgets.length) return null;

  const rows = budgets.map((b) => {
    let tag = null;
    if (b.status === "warning") {
      tag = h("span", { class: "tag gold" }, "Almost gone");
    } else if (b.status === "reached") {
      tag = h("span", { class: "tag bad" }, "Reached, work stopped");
    }

    const hasLimit = b.limit && b.limit > 0;
    const pctVal = hasLimit ? Math.min(100, Math.max(0, b.pct != null ? b.pct : (b.used != null ? ((b.used / b.limit) * 100) : 0))) : (b.pct != null ? Math.min(100, Math.max(0, b.pct)) : 0);
    const tokenText = !hasLimit && b.pct == null
      ? "No budget set"
      : (b.used != null && hasLimit ? `${tokens(b.used)} of ${tokens(b.limit)} tokens` : pct(pctVal));
    const barFillClass = b.status === "reached" ? "bar-fill bad" : "bar-fill";
    const bar = h("div", { class: "bar" }, h("div", { class: barFillClass, style: `width: ${pctVal}%` }));

    const input = h("input", {
      type: "number",
      min: 0,
      placeholder: "Tokens",
      value: hasLimit ? b.limit : "",
    });

    let btn;
    const setBudget = async () => {
      const raw = input.value.trim();
      if (!raw || !/^\d+$/.test(raw)) {
        toast("Enter a token number");
        return;
      }
      const val = parseInt(raw, 10);
      if (btn) btn.disabled = true;
      input.disabled = true;
      try {
        const res = await api("/api/budget", { scope: b.scope, tokens: val });
        toast(res.result || `Budget for ${b.scope} set`);
        document.dispatchEvent(new Event("subway:refresh"));
      } catch (e) {
        toast(e.message);
      } finally {
        if (btn) btn.disabled = false;
        input.disabled = false;
      }
    };

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        setBudget();
      }
    });

    btn = h("button", {
      class: "btn primary",
      type: "button",
      on: { click: setBudget },
    }, "Set budget");

    const tags = tag ? h("div", { class: "tags" }, tag) : null;
    const head = h("div", { class: "budget-head" },
      h("span", { class: "row-title" }, b.scope),
      tags);

    const main = h("div", { class: "budget-main" },
      head,
      bar,
      h("div", { class: "row-sub num" }, tokenText));

    const form = h("div", { class: "budget-form" },
      input,
      btn);

    return h("div", { class: "row budget-row" }, main, form);
  });

  return h("section", { class: "card", "aria-labelledby": "budgets-title" },
    h("div", { class: "card-head" },
      h("h2", { class: "card-title", id: "budgets-title" }, "Budgets")),
    h("div", { class: "rows" }, rows));
}

export function overview(d, link) {
  const now = d.timestamp;
  const providers = d.quotas.providers;
  const workers = d.workers || [];
  const pending = workers.filter((w) => w.status === "pending_approval").length;
  const running = workers.filter((w) => w.status === "running").length;
  const waiting = (d.permissions || []).length;
  const resets = nextResets(providers, now);
  const low = [];
  for (const p of providers) {
    const pools = lowPools(p, now);
    if (pools.length) {
      const named = pools.filter(Boolean);
      low.push(named.length ? `${p.name} (${named.join(", ")})` : p.name);
    }
  }
  const needs = waiting + pending;
  const headline = needs ? `${needs} thing${needs > 1 ? "s" : ""} waiting on you` : running ? `${running} worker${running > 1 ? "s" : ""} running` : "All clear";
  const tokens5h = providers.reduce((sum, p) => sum + (p.tokens ? p.tokens["5h"].input + p.tokens["5h"].output : 0), 0);

  let resetSide = null;
  if (resets.length) {
    const nowMs = Date.now();
    if (!resetCycle.lastAdvanced) {
      resetCycle.lastAdvanced = nowMs;
    } else {
      const elapsed = nowMs - resetCycle.lastAdvanced;
      if (elapsed >= 5000) {
        const steps = Math.floor(elapsed / 5000);
        resetCycle.index = (resetCycle.index + steps) % resets.length;
        resetCycle.lastAdvanced += steps * 5000;
      }
    }
    const idx = resetCycle.index % resets.length;
    const cur = resets[idx];
    const counter = resets.length > 1 ? ` (${idx + 1} of ${resets.length})` : "";
    const fig = ticking("div", { class: "hero-figure" }, cur.at, "countdown");
    const note = h("div", { class: "hero-note" }, cur.label);
    const label = h("div", { class: "label" }, `Next limit reset${counter}`);
    resetSide = h("div", { class: "hero-side" }, label, fig, note);
    if (resets.length > 1) {
      const timer = setInterval(() => {
        if (!resetSide.isConnected) {
          clearInterval(timer);
          return;
        }
        resetCycle.index = (resetCycle.index + 1) % resets.length;
        resetCycle.lastAdvanced = Date.now();
        const nextCur = resets[resetCycle.index];
        fig.dataset.epoch = nextCur.at;
        note.textContent = nextCur.label;
        label.textContent = `Next limit reset (${resetCycle.index + 1} of ${resets.length})`;
      }, 5000);
    }
  }

  const kpi = (name, label, value) => h("div", { class: "kpi" }, h("div", { class: "kpi-top" }, icon(name), label), h("div", { class: "kpi-value" }, value));
  return [
    permissionsCard(d.permissions || []),
    h("section", { class: "hero" },
      h("div", {}, h("div", { class: "hero-kicker" }, `${providers.length} LLM tool${providers.length === 1 ? "" : "s"} detected`),
        h("div", { class: "hero-head" }, headline),
        h("div", { class: "hero-note" }, low.length ? `Under 10% left: ${low.join(", ")}` : "No limit is under 10%.")),
      resetSide),
    h("div", { class: "kpis" },
      kpi("workers", "Running workers", running), kpi("bell", "Waiting on you", needs),
      kpi("leases", "File leases", Object.keys(d.locks || {}).length), kpi("bolt", "Tokens, last 5h", tokens(tokens5h))),
    chatContextCard(d.chat_context),
    budgetsCard(d),
    h("div", { class: "grid overview-grid" },
      h("section", { class: "card" },
        h("div", {}, h("h2", { class: "card-title" }, "Token spend"), h("div", { class: "card-sub" }, "New input and output tokens per hour, last 24 hours")),
        spendChart(providers)),
      h("section", { class: "card" },
        h("h2", { class: "card-title" }, "Limits at a glance"),
        h("div", { class: "rows" }, compactLimits(providers, now)))),
  ];
}

function compactLimits(providers, now) {
  const rows = [];
  for (const p of providers) {
    for (const pool of p.pools || []) {
      const live = pool.windows.filter((w) => !w.disabled && typeof w.left_pct === "number" && !(w.resets_at && w.resets_at <= now));
      if (!live.length) continue;
      rows.push(h("div", { class: "legend-row" },
        h("span", { class: "legend-key wrap" }, p.pools.length > 1 ? `${p.name} · ${pool.name}` : p.name),
        h("span", { class: "legend-val" }, live.map((w) => `${w.key === "5h" ? "5h" : "Wk"} ${pct(Math.round(w.left_pct))}`).join("  "))));
    }
  }
  return rows.length ? rows : h("div", { class: "empty" }, "No tool has reported a limit.");
}

/* ---- Usage ---- */
export function usagePage(d) {
  return h("div", { class: "providers" }, d.quotas.providers.length
    ? d.quotas.providers.map((p) => providerCard(p, d.timestamp))
    : h("div", { class: "empty" }, "No LLM tools found on this machine."));
}

/* ---- Workers ---- */
function workerTags(w) {
  const [label, cls] = WORKER_STATE[w.status] || [w.status || "Unknown", ""];
  const tags = [h("span", { class: `tag ${cls}` }, label), h("span", { class: "tag" }, (w.provider || "").toUpperCase())];
  if (w.priority === "urgent") tags.push(h("span", { class: "tag bad" }, "URGENT"));
  if (w.verify && w.verify.status) {
    const vCls = w.verify.status === "PASS" ? "ok" : (w.verify.status === "FLAG" ? "gold" : "bad");
    tags.push(h("span", { class: `tag ${vCls}`, title: w.verify.line || "" }, w.verify.status));
  }
  if (w.partial_patch) tags.push(h("span", { class: "tag gold" }, "Partial patch kept"));
  if (w.verdict === "approve" || w.verdict === "reject") {
    tags.push(h("span", { class: `tag ${w.verdict === "approve" ? "ok" : "bad"}`, title: w.verdict_reason || "" },
      `${w.verdict === "approve" ? "Approved" : "Rejected"} by ${w.verdict_model || w.model || "reviewer"}`));
  }
  if (w.triggered_by) tags.push(h("span", { class: "tag" }, `Started by ${w.triggered_by}`));
  if (Array.isArray(w.triggered) && w.triggered.length) tags.push(h("span", { class: "tag" }, `Then: ${w.triggered.join(", ")}`));
  const modelTag = (w.model_used && w.model && w.model !== w.model_used)
    ? `${w.model} -> ${w.model_used}`
    : (w.model_used || w.model);
  if (modelTag && !["default", "inherit"].includes(modelTag)) tags.push(h("span", { class: "tag" }, modelTag));
  if (w.effort) tags.push(h("span", { class: "tag" }, `effort ${w.effort}`));
  if (w.skip_permissions) tags.push(h("span", { class: "tag bad" }, "Prompts off"));
  else if (w.permission_mode === "auto_safe") tags.push(h("span", { class: "tag" }, "Safe commands auto"));
  if (w.waiting_permission) tags.push(h("span", { class: "tag gold" }, "Waiting for permission"));
  if (w.exit_code !== null && w.exit_code !== undefined) tags.push(h("span", { class: `tag${w.exit_code ? " bad" : ""}` }, `exit ${w.exit_code}`));
  const b = w.budget || {};
  if (b.session_pct) tags.push(h("span", { class: "tag" }, `needs 5h ≥ ${b.session_pct}%`));
  if (b.weekly_pct) tags.push(h("span", { class: "tag" }, `needs weekly ≥ ${b.weekly_pct}%`));
  if (b.max_usd) tags.push(h("span", { class: "tag" }, `cap $${b.max_usd}`));
  if (b.max_tokens) tags.push(h("span", { class: "tag" }, `cap ${tokens(b.max_tokens)} tokens`));
  if (w.budget_spent && typeof w.budget_spent === "object") {
    for (const [k, v] of Object.entries(w.budget_spent)) {
      tags.push(h("span", { class: "tag" }, `${k}: ${v} pts spent`));
    }
  }
  const u = w.usage;
  if (u && (u.input || u.output)) tags.push(h("span", { class: "tag num" }, `${tokens(u.input)} in, ${tokens(u.output)} out`));
  return tags;
}

async function showLog(id) {
  const pre = h("pre", {}, "Loading…");
  modal(`Log: ${id}`, pre);
  try {
    const d = await api(`/api/worker_log?id=${encodeURIComponent(id)}`);
    pre.textContent = d.log || "(no output)";
  } catch (e) {
    pre.textContent = e.message;
  }
}

export const openLogs = new Set();
export const expandedWorkers = new Set();

function workerRow(w, past) {
  const actions = [];
  if (w.status === "pending_approval") {
    actions.push(h("button", { class: "btn primary", type: "button", on: { click: () => act("/api/approve_worker", { id: w.id }) } }, "Approve"));
    actions.push(h("button", { class: "btn danger", type: "button", on: { click: async () => {
      const reason = await ask("Reject worker", "Reason (optional)");
      if (reason !== null) act("/api/reject_worker", { id: w.id, reason });
    } } }, "Reject"));
  } else {
    actions.push(h("button", { class: "btn", type: "button", on: { click: () => act("/api/verify", { id: w.id }) } }, "Verify"));
    actions.push(h("button", { class: "btn", type: "button", on: { click: () => showLog(w.id) } }, past ? "Report" : "Log"));
    if (w.status === "running") actions.push(h("button", { class: "btn danger", type: "button", on: { click: () => act("/api/stop_worker", { id: w.id, reason: "Stopped from the HUD" }) } }, "Stop"));
  }
  const text = past && w.report ? w.report : w.prompt;
  const isExpanded = expandedWorkers.has(w.id);
  const isLogOpen = openLogs.has(w.id);

  const verifyLine = (w.verify && w.verify.line) ? w.verify.line : null;
  const isLong = Boolean(text && (text.length > 180 || text.includes("\n")));
  const hasExpandable = isLong || Boolean(verifyLine);

  const verifyLineEl = verifyLine
    ? h("div", { class: "row-sub mono", hidden: !isExpanded }, verifyLine)
    : null;

  let textEl = null;
  if (text) {
    const subEl = h("div", { class: `row-sub${!isExpanded && isLong ? " clamped" : ""}` }, text);
    if (hasExpandable) {
      const toggleBtn = h("button", {
        class: "link-btn",
        type: "button",
        on: {
          click: (e) => {
            e.stopPropagation();
            if (expandedWorkers.has(w.id)) expandedWorkers.delete(w.id);
            else expandedWorkers.add(w.id);
            const open = expandedWorkers.has(w.id);
            if (isLong) subEl.classList.toggle("clamped", !open);
            if (verifyLineEl) verifyLineEl.hidden = !open;
            e.currentTarget.textContent = open ? "Show less" : "Show more";
          },
        },
      }, isExpanded ? "Show less" : "Show more");
      textEl = h("div", {}, subEl, toggleBtn);
    } else {
      textEl = subEl;
    }
  } else if (hasExpandable) {
    const toggleBtn = h("button", {
      class: "link-btn",
      type: "button",
      on: {
        click: (e) => {
          e.stopPropagation();
          if (expandedWorkers.has(w.id)) expandedWorkers.delete(w.id);
          else expandedWorkers.add(w.id);
          const open = expandedWorkers.has(w.id);
          if (verifyLineEl) verifyLineEl.hidden = !open;
          e.currentTarget.textContent = open ? "Show less" : "Show more";
        },
      },
    }, isExpanded ? "Show less" : "Show more");
    textEl = h("div", {}, toggleBtn);
  }

  /* Updated in place, so it also works inside the history dialog, which doesn't redraw. */
  const logPre = h("pre", { class: "inline-log", hidden: !isLogOpen }, "Loading…");
  const loadLog = () => api(`/api/worker_log?id=${encodeURIComponent(w.id)}`)
    .then((d) => { logPre.textContent = d.log || "(no output)"; })
    .catch((e) => { logPre.textContent = e.message; });
  if (isLogOpen) loadLog();

  const logToggleBtn = h("button", {
    class: "link-btn",
    type: "button",
    on: {
      click: (e) => {
        e.stopPropagation();
        if (openLogs.has(w.id)) openLogs.delete(w.id);
        else openLogs.add(w.id);
        const open = openLogs.has(w.id);
        logPre.hidden = !open;
        e.currentTarget.textContent = open ? "Hide log" : "Show log";
        if (open) loadLog();
      },
    },
  }, isLogOpen ? "Hide log" : "Show log");

  return h("div", { class: "row" },
    h("div", {}, h("div", { class: "row-title" }, w.role || "Worker"),
      h("div", { class: "row-sub" }, h("span", { class: "mono" }, w.id), w.project_dir ? ` · ${w.project_dir}` : ""),
      textEl,
      w.status_reason ? h("div", { class: "row-sub" }, `Reason: ${w.status_reason}`) : null,
      verifyLineEl,
      h("div", {}, logToggleBtn),
      w.route ? h("div", { class: "row-sub" }, `Why this agent: ${w.route}`) : null,
      w.progress ? h("div", { class: "row-sub num" }, `${w.progress.files_done} of ${w.progress.files_total} files, ${w.progress.chunks_done} chunks done, ${w.progress.failed} failed`) : null,
      w.status === "pending_approval" && w.approval_code
        ? h("div", { class: "row-sub" }, "To approve from a chat, tell the agent this code: ", h("span", { class: "code" }, w.approval_code)) : null),
    h("div", { class: "tags" }, workerTags(w)),
    h("div", { class: "btns" }, actions),
    logPre);
}

function openDelegate(d) {
  const choices = d.quotas.providers.filter((p) => p.client && p.client.dispatchable);
  const provider = h("select", {}, h("option", { value: "auto" }, "Auto (first with budget)"), choices.map((p) => h("option", { value: p.id }, p.name)));
  const modes = d.settings.permission_modes;
  const permission = h("select", {}, h("option", { value: "" }, `Default: ${modes[d.settings.permission_mode]}`),
    Object.entries(modes).map(([k, v]) => h("option", { value: k }, v)));
  const input = (attrs) => h("input", { autocomplete: "off", ...attrs });
  const role = input({ required: true, maxlength: 80 });
  const model = input({ placeholder: "The agent's default" });
  const effort = h("select", {}, ["auto", "low", "medium", "high", "max"].map((e) => h("option", { value: e }, e[0].toUpperCase() + e.slice(1))));
  const folder = input({ placeholder: "Where the HUD was started" });
  const timeout = input({ type: "number", min: 0, value: 60 });
  const cap5 = input({ type: "number", min: 1, max: 100 });
  const capWk = input({ type: "number", min: 1, max: 100 });
  const usd = input({ type: "number", min: 0, step: 0.5 });
  const maxTokens = input({ type: "number", min: 0 });
  const keepPrivate = h("input", { type: "checkbox" });
  const prompt = h("textarea", { required: true });
  const god = h("input", { type: "checkbox" });
  const verdict = h("div", { class: "verdict field full", role: "status" }, "Choose an agent to check its real limits.");
  const num = (el) => (el.value === "" ? null : parseFloat(el.value));

  let timer = 0;
  async function preflight() {
    if (provider.value === "auto") {
      verdict.className = "verdict field full";
      verdict.textContent = "Auto checks each agent's real limits when queued, and picks the first with budget.";
      return;
    }
    try {
      const r = await api("/api/preflight", { provider: provider.value, model: model.value.trim(), budget_session_pct: num(cap5), budget_weekly_pct: num(capWk), max_budget_usd: num(usd) });
      verdict.className = `verdict field full ${r.ok ? "ok" : "bad"}`;
      verdict.textContent = r.result;
    } catch (e) {
      verdict.className = "verdict field full bad";
      verdict.textContent = e.message;
    }
  }
  [provider, model, cap5, capWk, usd].forEach((el) => el.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(preflight, 250); }));

  const form = h("form", { class: "form", on: { submit: async (e) => {
    e.preventDefault();
    const res = await act("/api/spawn", {
      role: role.value.trim(), prompt: prompt.value.trim(), provider: provider.value, model: model.value.trim(), effort: effort.value,
      mode: god.checked ? "god" : "standard", permission_mode: permission.value, project_dir: folder.value.trim(),
      budget_session_pct: num(cap5), budget_weekly_pct: num(capWk), max_budget_usd: num(usd),
      max_tokens: num(maxTokens), private: keepPrivate.checked,
      timeout_seconds: Math.round((num(timeout) || 0) * 60),
    });
    if (res && /APPROVAL REQUIRED/.test(res.result || "")) dlg.close();
  } } },
    field("Role", role), field("Agent", provider), field("Permissions", permission), field("Model (optional)", model), field("Effort", effort),
    field("Folder (optional)", folder), field("Timeout, minutes (0 for none)", timeout), field("Most of the 5-hour limit to use, points", cap5), field("Most of the weekly limit to use, points", capWk),
    field("Spend cap, USD (Claude only)", usd), field("Token cap (Antigravity only)", maxTokens), field("Task", prompt, true),
    h("label", { class: "check field full" }, keepPrivate, h("span", {}, "Keep this task on this machine")),
    h("label", { class: "check field full" }, god, h("span", {}, "God mode: maximum effort with permission prompts off. Only for a task you trust completely.")),
    verdict, h("div", { class: "btns field full" }, h("button", { class: "btn primary", type: "submit" }, "Queue for approval")));
  const dlg = modal("Delegate a worker", form);
  preflight();
  role.focus();
}

function openHistory(d) {
  const container = h("div", { class: "rows" });
  const loadMoreBtn = h("button", { class: "btn", type: "button", style: "margin-top: 8px; width: 100%; display: none;" }, "Load more");
  const archive = h("button", { class: "btn", type: "button", on: { click: async () => { await act("/api/clear_history", {}); dlg.close(); } } }, "Archive finished");
  const body = h("div", {}, container, loadMoreBtn);
  const dlg = modal("Worker history", body, archive);

  let offset = 0;
  const limit = 100;
  let allEntries = [];

  async function fetchMore() {
    loadMoreBtn.disabled = true;
    loadMoreBtn.textContent = "Loading…";
    try {
      const res = await api(`/api/history?offset=${offset}&limit=${limit}`);
      const entries = res.entries || [];
      const total = res.total != null ? res.total : entries.length;
      allEntries = allEntries.concat(entries);
      offset += entries.length;

      while (container.firstChild) container.removeChild(container.firstChild);
      if (!allEntries.length) {
        container.appendChild(h("div", { class: "empty" }, "No finished workers."));
      } else {
        for (const w of allEntries) {
          container.appendChild(workerRow(w, true));
        }
      }

      if (offset < total && entries.length > 0) {
        loadMoreBtn.style.display = "";
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = "Load more";
      } else {
        loadMoreBtn.style.display = "none";
      }
    } catch (e) {
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = "Error loading history";
    }
  }

  loadMoreBtn.addEventListener("click", fetchMore);
  fetchMore();
}

let modelFilter = "All";
let expandedModelKey = null;
let modelEdits = {};

export function hasModelEdits() {
  return Object.keys(modelEdits).length > 0;
}

export function modelsPage(d) {
  const settings = d.settings || {};
  const allModels = settings.models || [];
  const bm = settings.benchmarks || {};
  const opencodeProvider = (d.quotas && d.quotas.providers ? d.quotas.providers : []).find(
    (p) => (p.id || "").toLowerCase() === "opencode" || (p.name || "").toLowerCase() === "opencode"
  );

  let bmDate = "unknown";
  if (bm.fetched_at) {
    bmDate = bm.fetched_at_iso || new Date(bm.fetched_at * 1000).toLocaleDateString();
  }
  const bmSource = bm.source || "unknown";

  const providerNames = {
    claude: "Claude",
    antigravity: "Antigravity",
    opencode: "OpenCode",
    local: "Local",
    copilot: "Copilot",
  };
  const providerDisplayName = (id) => {
    const lower = (id || "").toLowerCase();
    if (providerNames[lower]) return providerNames[lower];
    if (!id) return "";
    return id.charAt(0).toUpperCase() + id.slice(1);
  };

  const seenProviders = new Set();
  const filters = [{ id: "All", label: "All" }];
  for (const m of allModels) {
    const prov = (m.provider || "").toLowerCase();
    if (prov && !seenProviders.has(prov)) {
      seenProviders.add(prov);
      filters.push({ id: prov, label: providerDisplayName(prov) });
    }
  }

  const segButtons = filters.map((f) => {
    return h("button", {
      type: "button",
      "aria-pressed": String((modelFilter || "").toLowerCase() === f.id.toLowerCase()),
      on: {
        click: () => {
          modelFilter = f.id;
          document.dispatchEvent(new Event("subway:rerender"));
        },
      },
    }, f.label);
  });

  const headerCard = h("section", { class: "card", "aria-labelledby": "models-page-title" },
    h("div", { class: "card-head" },
      h("div", {},
        h("h2", { class: "card-title", id: "models-page-title" }, "Model rankings"),
        h("div", { class: "card-sub" }, `Benchmark source: ${bmSource} · Date: ${bmDate} · Refresh-free: models update dynamically`)),
      h("div", { class: "seg", role: "group", "aria-label": "Filter by provider" }, segButtons)),
    h("div", { class: "card-sub" }, "Rankings tell the router how strong each model is for a kind of work. C is coding, R is reasoning, W is writing. Five dots is strongest."));

  const filtered = allModels.filter((m) => {
    if (!modelFilter || modelFilter.toLowerCase() === "all") return true;
    return (m.provider || "").toLowerCase() === modelFilter.toLowerCase();
  });

  const groups = {};
  for (const m of filtered) {
    const prov = m.provider || "other";
    if (!groups[prov]) groups[prov] = [];
    groups[prov].push(m);
  }

  const meter = (label, val, short) => {
    const dots = [];
    for (let i = 1; i <= 5; i++) {
      dots.push(h("span", { class: `dot-meter${i <= val ? " filled" : ""}` }));
    }
    const shortLabel = short || label[0];
    return h("div", { class: "model-meter" },
      h("span", { class: "meter-label", "data-short": shortLabel },
        h("span", { class: "meter-full" }, label),
        h("span", { class: "meter-short" }, shortLabel)),
      h("div", { class: "meter-dots" }, dots));
  };

  const scorePicker = (label, skill, currentVal, onChange) => {
    const btns = [1, 2, 3, 4, 5].map((val) => {
      return h("button", {
        type: "button",
        "aria-pressed": String(currentVal === val),
        on: {
          click: (e) => {
            e.stopPropagation();
            onChange(val);
          },
        },
      }, String(val));
    });
    return h("div", { class: "field" },
      h("label", {}, label),
      h("div", { class: "seg", role: "group", "aria-label": label }, btns));
  };

  const groupCards = Object.entries(groups).map(([providerName, mList]) => {
    const rows = mList.map((m) => {
      const key = `${m.provider}:${m.model || ""}`;
      const isExpanded = expandedModelKey === key;
      const currentEdits = modelEdits[key] || {};
      const coding = currentEdits.coding !== undefined ? currentEdits.coding : (m.coding != null ? m.coding : 2);
      const reasoning = currentEdits.reasoning !== undefined ? currentEdits.reasoning : (m.reasoning != null ? m.reasoning : 2);
      const writing = currentEdits.writing !== undefined ? currentEdits.writing : (m.writing != null ? m.writing : 2);

      let sourceTag = "Default";
      if (m.source === "benchmark") sourceTag = "Benchmark";
      else if (m.source === "owner") sourceTag = "Yours";

      const tags = [h("span", { class: "tag" }, sourceTag)];
      if (m.scarce) tags.push(h("span", { class: "tag gold" }, "Scarce"));

      const ex = (d.exhausted_models || []).find((e) => e.model && m.model && (e.model.toLowerCase() === m.model.toLowerCase() || e.model === m.model));
      if (ex && ex.until) {
        const ts = typeof ex.until === "number" ? (ex.until > 1e11 ? ex.until : ex.until * 1000) : Date.parse(ex.until);
        const untilTime = ts ? new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
        if (untilTime) {
          tags.push(h("span", { class: "tag bad" }, `Out of quota until ${untilTime}`));
        }
      }

      const provId = (m.provider || "").toLowerCase();
      const modelProvider = (d.quotas && d.quotas.providers ? d.quotas.providers : []).find(
        (p) => (p.id || "").toLowerCase() === provId || (p.name || "").toLowerCase() === provId
      );
      if (m.free === true) {
        tags.push(h("span", { class: "tag" }, "Free tier"));
      }
      if (m.price_band) {
        tags.push(h("span", { class: "tag" }, m.price_band));
      }
      if (m.auto || m.default) {
        tags.push(h("span", { class: "tag" }, "Auto"));
      }

      if (modelProvider) {
        const limitedMap = modelProvider.limited || {};
        let lim = limitedMap[m.model];
        if (!lim && m.model) {
          const mLow = m.model.toLowerCase();
          for (const [k, v] of Object.entries(limitedMap)) {
            const kLow = k.toLowerCase();
            if (kLow === mLow || kLow.endsWith("/" + mLow) || mLow.endsWith("/" + kLow)) {
              lim = v;
              break;
            }
          }
        }

        if (lim && lim.recent) {
          const ts = typeof lim.retry_at === "number" ? (lim.retry_at > 1e11 ? lim.retry_at : lim.retry_at * 1000) : Date.parse(lim.retry_at);
          const retryTime = ts ? new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
          tags.push(h("span", { class: "tag bad" },
            h("span", {}, "At limit"),
            retryTime ? h("span", {}, ` · Retries around ${retryTime}`) : null));
        } else {
          tags.push(h("span", { class: "tag" }, "No limit reported"));
        }

        const perModelMap = modelProvider.per_model || {};
        let usedTokens = perModelMap[m.model];
        if (usedTokens === undefined && m.model) {
          const mLow = m.model.toLowerCase();
          for (const [k, v] of Object.entries(perModelMap)) {
            const kLow = k.toLowerCase();
            if (kLow === mLow || kLow.endsWith("/" + mLow) || mLow.endsWith("/" + kLow)) {
              usedTokens = v;
              break;
            }
          }
        }
        usedTokens = usedTokens || 0;
        tags.push(h("span", { class: "tag num" }, `${tokens(usedTokens)} tokens used, 7 days`));
      }

      const modelTracks = (d.models_track || []).filter((t) => {
        if (!t.model || !m.model) return false;
        if (t.model.toLowerCase() !== m.model.toLowerCase()) return false;
        if (t.provider && m.provider) {
          return t.provider.toLowerCase() === m.provider.toLowerCase();
        }
        return true;
      });
      let trackLine = null;
      if (modelTracks.length > 0) {
        const landed = modelTracks.reduce((sum, x) => sum + (x.landed || 0), 0);
        const noChange = modelTracks.reduce((sum, x) => sum + (x.no_change || 0), 0);
        const failed = modelTracks.reduce((sum, x) => sum + (x.failed || 0), 0);
        const budgetStopped = modelTracks.reduce((sum, x) => sum + (x.budget_stopped || 0), 0);
        let ratePct = 0;
        if (modelTracks.length === 1 && modelTracks[0].landed_rate != null) {
          const r = Number(modelTracks[0].landed_rate);
          ratePct = Math.round(r <= 1 && r > 0 ? r * 100 : r);
        } else {
          const total = landed + noChange + failed + budgetStopped;
          ratePct = total > 0 ? Math.round((landed / total) * 100) : 0;
        }
        trackLine = h("div", { class: "row-sub model-track-record" }, `Track record: ${ratePct}% (${landed} landed, ${noChange} no change, ${failed} failed)`);
      }

      const summaryBtn = h("button", {
        class: "model-row-btn",
        type: "button",
        on: {
          click: () => {
            expandedModelKey = isExpanded ? null : key;
            document.dispatchEvent(new Event("subway:rerender"));
          },
        },
      },
        h("div", { class: "model-row-main" },
          h("div", { class: "model-info" },
            h("div", { class: "row-title" }, m.model || "Default"),
            trackLine),
          h("div", { class: "tags" }, tags)),
        h("div", { class: "model-meters" },
          meter("Coding", coding, "C"),
          meter("Reasoning", reasoning, "R"),
          meter("Writing", writing, "W")));

      if (!isExpanded) {
        return h("div", { class: "model-item" }, summaryBtn);
      }

      const updateSkill = (skill, val) => {
        modelEdits[key] = {
          ...m,
          ...(modelEdits[key] || {}),
          [skill]: val,
        };
        document.dispatchEvent(new Event("subway:rerender"));
      };

      const saveModel = async (e) => {
        e.stopPropagation();
        const wholeList = allModels.map((item) => {
          const itemKey = `${item.provider}:${item.model || ""}`;
          if (modelEdits[itemKey]) {
            return { ...item, ...modelEdits[itemKey] };
          }
          return item;
        });
        const res = await act("/api/models", wholeList);
        if (res) {
          modelEdits = {};
          expandedModelKey = null;
          document.dispatchEvent(new Event("subway:refresh"));
        }
      };

      const pickers = h("div", { class: "model-expanded-pickers" },
        scorePicker("Coding", "coding", coding, (v) => updateSkill("coding", v)),
        scorePicker("Reasoning", "reasoning", reasoning, (v) => updateSkill("reasoning", v)),
        scorePicker("Writing", "writing", writing, (v) => updateSkill("writing", v)),
        h("div", { class: "btns" },
          h("button", { class: "btn primary", type: "button", on: { click: saveModel } }, "Save")));

      return h("div", { class: "model-item expanded" }, summaryBtn, pickers);
    });

    return h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", { class: "card-title" }, providerName.toUpperCase())),
      h("div", { class: "rows" }, rows));
  });

  const howCard = h("section", { class: "card", "aria-labelledby": "how-title" },
    h("div", { class: "card-head" },
      h("h2", { class: "card-title", id: "how-title" }, "How this works")),
    h("div", { class: "card-sub" },
      "Routing picks a model using the task's difficulty, the role's skill, then remaining allowance; Benchmark means LMArena, Default means our starting estimate, Yours means you edited it; rankings measure strength, not allowance."));

  const content = groupCards.length ? groupCards : [h("div", { class: "card" }, h("div", { class: "empty" }, "No models match this filter."))];
  return [headerCard, ...content, howCard];
}

export function workersPage(d) {
  const workers = d.workers || [];
  const activeCount = workers.filter((w) => ["running", "launching", "stopping"].includes(w.status)).length;
  const cap = (d.settings && d.settings.capacity) || {};
  const limits = (cap.chat != null && cap.machine != null)
    ? ` Limits: ${cap.chat} per chat, ${cap.machine} on this machine.`
    : (cap.chat != null ? ` Limits: ${cap.chat} per chat.` : (cap.machine != null ? ` Limits: ${cap.machine} on this machine.` : ""));
  const sub = `${activeCount} running.${limits}`;

  const budgetVal = (d.settings && d.settings.chat_budget != null) ? d.settings.chat_budget : "";
  const budgetInput = h("input", { type: "number", min: 0, style: "width: 140px;", value: budgetVal });
  const budgetSaveBtn = h("button", {
    class: "btn primary",
    type: "button",
    on: {
      click: () => {
        const n = parseInt(budgetInput.value, 10);
        return act("/api/chat_budget", { tokens: Number.isFinite(n) ? n : 0 });
      },
    },
  }, "Save");

  const chatRows = (d.settings && d.settings.chat_budgets && d.settings.chat_budgets.chats) || [];
  const chatRowEls = chatRows.map((c) => {
    const tag = c.status === "warning" ? h("span", { class: "tag gold" }, "Almost gone")
      : c.status === "reached" ? h("span", { class: "tag bad" }, "Reached, work stopped") : null;
    const clearBtn = h("button", {
      class: "btn",
      type: "button",
      on: {
        click: async () => {
          await act("/api/chat_budget", { tokens: 0, session_id: c.session_id });
        },
      },
    }, "Clear");
    return h("div", { class: "row budget-row" },
      h("div", { class: "budget-main" },
        h("div", { class: "budget-head" }, h("span", { class: "row-title" }, c.label || c.session_id), tag),
        h("div", { class: "row-sub num" }, `${tokens(c.used)} of ${tokens(c.limit)} tokens`)),
      clearBtn);
  });

  const chatBudgetCard = h("section", { class: "card" },
    h("div", { class: "card-head" },
      h("div", {},
        h("h2", { class: "card-title" }, "Universal chat budget"),
        h("div", { class: "card-sub" }, "Default token limit for chats with no budget of their own")),
      h("div", { style: "display: flex; gap: 8px; align-items: center; flex-wrap: wrap;" },
        budgetInput,
        budgetSaveBtn)),
    chatRowEls.length ? h("div", { class: "rows" },
      h("div", { class: "card-sub" }, "Chats with their own budget:"),
      chatRowEls) : null);

  const budgets = d.settings && d.settings.budgets;
  const hasChatScope = Array.isArray(budgets) && budgets.some((b) => b.scope === "chat");

  return [
    permissionsCard(d.permissions || []),
    h("div", { class: "controls-grid" }, modeControl(d.settings), settingsCard(d.settings)),
    hasChatScope ? null : chatBudgetCard,
    h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("div", {},
          h("h2", { class: "card-title" }, "Active workers"),
          h("div", { class: "card-sub" }, sub)),
        h("div", { class: "btns" },
          h("button", { class: "btn", type: "button", on: { click: () => openHistory(get("status")) } }, `History (${d.total_history != null ? d.total_history : (d.history || []).length})`),
          h("button", { class: "btn primary", type: "button", on: { click: () => openDelegate(get("status")) } }, "Delegate worker"))),
      h("div", { class: "rows" }, workers.length ? workers.map((w) => workerRow(w, false)) : h("div", { class: "empty" }, "No active workers."))),
  ];
}

/* ---- Leases ---- */
function openClaim() {
  const path = h("input", { required: true, autocomplete: "off" });
  const holder = h("input", { autocomplete: "off", value: "hud-user" });
  const secs = h("input", { type: "number", min: 10, max: 3600, value: 300 });
  const role = h("select", {}, ["implementer", "reviewer", "refactorer", "coordinator"].map((r) => h("option", { value: r }, r)));
  const form = h("form", { class: "form", on: { submit: async (e) => {
    e.preventDefault();
    const res = await act("/api/claim", { filepath: path.value.trim(), holder: holder.value.trim(), role: role.value, duration: parseInt(secs.value, 10) || 300 });
    if (res && /LEASE ACQUIRED/.test(res.result || "")) dlg.close();
  } } }, field("Absolute file path", path, true), field("Holder", holder), field("Seconds", secs), field("Role", role),
    h("div", { class: "btns field full" }, h("button", { class: "btn primary", type: "submit" }, "Claim")));
  const dlg = modal("Claim a file", form);
  path.focus();
}

export function leasesPage(d) {
  const rows = [];
  for (const [path, l] of Object.entries(d.locks || {})) {
    const isAutoClaimed = Boolean(l.purpose && l.purpose.includes("auto-claimed"));
    rows.push(h("div", { class: "row" },
      h("div", {}, h("div", { class: "row-title mono" }, path), h("div", { class: "row-sub" }, l.purpose || "")),
      h("div", { class: "tags" },
        isAutoClaimed ? h("span", { class: "tag" }, "auto-claimed") : null,
        h("span", { class: "tag gold" }, l.holder || "unknown"), h("span", { class: "tag" }, l.role || "implementer"),
        h("span", { class: "tag num" }, ticking("span", {}, l.expires_at, "countdown"), " left")),
      h("div", { class: "btns" }, h("button", { class: "btn danger", type: "button", on: { click: () => act("/api/release", { filepath: path }) } }, "Release"))));
  }
  for (const [path, so] of Object.entries(d.signoffs || {})) {
    const ok = so.verdict === "APPROVED";
    rows.push(h("div", { class: "row" },
      h("div", {}, h("div", { class: "row-title mono" }, path), h("div", { class: "row-sub" }, so.notes || "")),
      h("div", { class: "tags" }, h("span", { class: `tag ${ok ? "ok" : ""}` }, ok ? "Approved" : "Needs work"), h("span", { class: "tag" }, so.reviewer || ""),
        so.commit_sha ? h("span", { class: "tag mono" }, so.commit_sha) : null, so.drifted ? h("span", { class: "tag bad" }, "Changed since sign-off") : null)));
  }
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", { class: "card-title" }, "File leases"),
      h("div", { class: "card-sub" }, "Enforced for edits in Claude Code once its hook is installed. Advisory for other agents.")),
      h("button", { class: "btn", type: "button", on: { click: openClaim } }, "Claim file")),
    h("div", { class: "rows" }, rows.length ? rows : h("div", { class: "empty" }, "No leases or sign-offs.")));
}

/* Ledger */
export const ledger = { track: "main", entries: [] };

export function renderLedgerEntries(entriesList) {
  const list = entriesList || ledger.entries;
  if (!list.length) return [h("div", { class: "empty" }, `Nothing on '${ledger.track}' yet.`)];
  return list.map((e) => h("div", {
    class: `entry${e.is_destructive ? " alert" : ""}`,
    tabindex: "0",
    role: "button",
    on: {
      click: () => openLedgerDetail(e),
      keydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
          ev.preventDefault();
          openLedgerDetail(e);
        }
      },
    },
  },
    h("div", { class: "entry-head" }, h("span", { class: "mono" }, e.iso || ""), h("b", {}, e.station_id || "unknown"),
      e.session_id && e.session_id !== e.station_id ? h("span", { class: "tag" }, e.session_id) : null),
    h("div", {}, e.message || ""),
    (e.files || []).length ? h("div", { class: "row-sub" }, `Files: ${e.files.join(", ")}`) : null,
    (e.decisions || []).length ? h("div", { class: "row-sub" }, `Decision: ${e.decisions.join("; ")}`) : null));
}

function openLedgerDetail(e) {
  const content = h("div", { class: "dialog-body" },
    h("div", { class: "tags" },
      e.is_destructive ? h("span", { class: "tag bad" }, "destructive") : null,
      e.id ? h("span", { class: "tag mono" }, `id: ${e.id}`) : null,
      e.station_id ? h("span", { class: "tag" }, `station: ${e.station_id}`) : null,
      e.session_id ? h("span", { class: "tag" }, `session: ${e.session_id}`) : null,
      (e.iso || e.time) ? h("span", { class: "tag mono" }, e.iso || e.time) : null),
    h("div", {},
      h("div", { class: "label" }, "Message"),
      h("pre", {}, e.message || "(empty message)")),
    (e.files || []).length ? h("div", {},
      h("div", { class: "label" }, "Files"),
      h("ul", { class: "rows" }, e.files.map((f) => h("li", { class: "mono row-sub" }, f)))) : null,
    (e.decisions || []).length ? h("div", {},
      h("div", { class: "label" }, "Decisions"),
      h("ul", { class: "rows" }, e.decisions.map((d) => h("li", { class: "row-sub" }, d)))) : null);
  modal(e.message ? (e.message.length > 60 ? `${e.message.slice(0, 60)}…` : e.message) : "Ledger entry", content);
}

export function ledgerPage(d, onSwitch) {
  const names = Object.keys(d.tracks || {});
  if (!names.includes(ledger.track)) names.push(ledger.track);
  const msg = h("input", { required: true, autocomplete: "off" });
  const files = h("input", { autocomplete: "off" });
  const entries = renderLedgerEntries(ledger.entries);
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title" }, "Decision ledger"),
      h("button", { class: "btn", type: "button", on: { click: async () => {
        const name = (await ask("New track", "Track name") || "").toLowerCase().replace(/[^a-z0-9_-]/g, "");
        if (name) onSwitch(name);
      } } }, "New track")),
    h("div", { class: "tabs", role: "tablist" }, names.map((t) => h("button", {
      class: "tab", type: "button", role: "tab", "aria-selected": String(t === ledger.track), on: { click: () => onSwitch(t) },
    }, `${t} (${(d.tracks[t] && d.tracks[t].count) || 0})`))),
    h("div", { class: "feed", tabindex: "0" }, entries),
    h("form", { class: "form", on: { submit: async (e) => {
      e.preventDefault();
      const list = files.value.split(",").map((x) => x.trim()).filter(Boolean);
      if (await act("/api/broadcast", { message: msg.value, track: ledger.track, files: list, station_id: "hud-user" })) onSwitch(ledger.track);
    } } }, field("Post to this track", msg, true), field("Files, comma separated (optional)", files, true),
      h("div", { class: "btns field full" }, h("button", { class: "btn primary", type: "submit" }, "Post"))));
}
