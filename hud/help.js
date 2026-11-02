/* Help and newcomer reference for Subway Context. */
import { h } from "./lib.js";

let query = "";

export function helpPage() {
  const intro = h("div", { class: "card help-intro" },
    h("h2", { class: "card-title" }, "What is Subway?"),
    h("p", { class: "help-desc" },
      "Subway is a local MCP server that lets Claude Code, Antigravity and OpenCode chats share context and hand work to supervised workers; the HUD is its local dashboard on 127.0.0.1."
    )
  );

  const searchInput = h("input", {
    type: "search",
    class: "help-search",
    placeholder: "Filter sections, terms, tools, or commands...",
    "aria-label": "Search help",
    value: query,
    on: { input: onInput }
  });

  const emptyMsg = h("div", { class: "empty", hidden: true }, "No help topics match your search.");

  const sectionsData = [
    {
      title: "Pages",
      subtitle: "What each page in the HUD shows",
      items: [
        { term: "Overview", desc: "What needs you (pending approvals, alerts, low limits), and what's actively running." },
        { term: "Usage", desc: "Limits and tokens, as reported by each tool and provider, with budget tracking." },
        { term: "Workers", desc: "Delegated agents, their real-time state, live logs, and pending permissions." },
        { term: "Models", desc: "How each model is ranked, benchmark scores, and where that ranking came from." },
        { term: "Leases", desc: "Who holds which file, preventing agents from making conflicting edits simultaneously." },
        { term: "Ledger", desc: "Shared decisions, notes, and milestones synchronized across all connected chats." },
      ]
    },
    {
      title: "Key Terms",
      subtitle: "Core concepts and definitions used throughout Subway",
      items: [
        { term: "station", desc: "A connected chat window (such as Claude Code, Antigravity, or OpenCode)." },
        { term: "track and ledger", desc: "Shared notes and chronological decision records synchronized between collaborating agents." },
        { term: "lease", desc: "A claim on a file so two agents don't edit it at once; claimed automatically on first edit." },
        { term: "worker", desc: "An autonomous agent Subway starts and supervises to perform specific tasks." },
        { term: "swarm and god mode", desc: "Running many workers at once with no count limit, constrained only by free memory." },
        { term: "priority", desc: "Worker queue urgency levels: urgent, normal, and low." },
        { term: "permission modes", desc: "How actions are authorized: Approve everything, Auto-approve safe commands, or Auto-approve everything." },
        { term: "budgets", desc: "Token limits set per chat and per provider, with a warning at 80% and a hard block at 100%." },
        { term: "token cap and wrap-up", desc: "Workers are told to wrap up at two thirds of their cap, and no tool call is started that would pass the cap." },
        { term: "verifier", desc: "Automated check returning PASS, FLAG, or FAIL; proves changes are mechanically sound, not proof of correctness." },
        { term: "compaction and segments", desc: "Context trimming modes (Suggest, Auto, Never); keeps active context compact while full history stays readable." },
        { term: "compaction point", desc: "`subway-context compaction auto 300k` sets Auto at 300,000 tokens. In Auto, Claude Code compacts itself near that point (Subway sets Claude Code's autoCompactWindow, leaving a value the user set alone), OpenCode workers are compacted, and Antigravity is warned only. Default 300,000; range 100,000 to 1,000,000." },
        { term: "track record routing", desc: "Assigning worker tasks to models based on task type and historical success." },
        { term: "out of quota", desc: "Occurs when a provider or model runs out of tokens or hits rate limits, pausing work or falling back." },
        { term: "desktop alerts", desc: "System notifications sent when workers need approval, finish tasks, or approach budget limits." },
      ]
    },
    {
      title: "Main MCP Tools",
      subtitle: "Primary tools exposed by the Subway MCP server (one line each)",
      items: [
        { term: "read_chat", desc: "Reads messages, decisions, or diffs from another agent's session transcript." },
        { term: "search_chat", desc: "Searches across all chat sessions for specific phrases or code snippets." },
        { term: "send_note", desc: "Dispatches a note or coordinate link to another agent's mailbox." },
        { term: "check_notes", desc: "Checks your session mailbox for incoming notes from other agents." },
        { term: "claim_file", desc: "Acquires an exclusive file lease before editing to prevent collision." },
        { term: "release_file", desc: "Releases a previously held file lease after edits are complete." },
        { term: "spin_worker", desc: "Launches a single supervised worker agent with a prompt, role, and track." },
        { term: "spin_swarm", desc: "Launches multiple parallel worker agents to complete a batch of tasks." },
        { term: "worker_status", desc: "Checks execution state, token consumption, and logs for a specific worker." },
        { term: "answer_permission", desc: "Approves or denies a pending tool or command execution request from a worker." },
        { term: "quota_status", desc: "Returns remaining tokens, pool limits, and reset times across providers." },
      ]
    },
    {
      title: "Terminal Commands",
      subtitle: "CLI controls via the subway-context binary",
      items: [
        { term: "subway-context", desc: "Run as an MCP server over stdio." },
        { term: "subway-context hud [port] [--open]", desc: "Start the Live HUD on 127.0.0.1." },
        { term: "subway-context workers", desc: "List workers, with approval codes for pending ones." },
        { term: "subway-context approve <worker_id>", desc: "Launch a pending worker." },
        { term: "subway-context reject <worker_id> [reason]", desc: "Reject a worker with an optional reason." },
        { term: "subway-context stop <worker_id>", desc: "Stop a running worker." },
        { term: "subway-context permissions", desc: "List worker permission requests." },
        { term: "subway-context watch [--stream]", desc: "Wait for, or stream, permission requests and finished workers." },
        { term: "subway-context mode [ask|auto_safe|auto_approve]", desc: "Show or set how workers get permissions." },
        { term: "subway-context allow|always|deny <request_id>", desc: "Allow, always allow, or deny a worker permission request." },
        { term: "subway-context tail <worker_id>", desc: "Show a worker's status and log." },
        { term: "subway-context locks | track [name] | quota [source]", desc: "Show active file leases, view a shared context track, or inspect token quota." },
        { term: "subway-context install | status | uninstall", desc: "Install, check connection status, or uninstall agent registrations." },
        { term: "subway-context <tool> '<json args>'", desc: "Call any MCP tool directly." },
      ]
    },
    {
      title: "Recommended companion tools",
      subtitle: "Optional: Subway doesn't install, bundle or configure any of them. Users add them to their own MCP config if they want them.",
      items: [
        {
          term: "fetch (mcp-server-fetch)",
          desc: "For simple pages."
        },
        {
          term: "Lightpanda (lightpanda.io)",
          desc: "A fast open-source headless browser for JavaScript-heavy pages, with its own MCP server. Install on macOS with brew install lightpanda-io/browser/lightpanda. No native Windows build (use WSL). It cannot take screenshots or render PDFs."
        },
        {
          term: "Apify (@apify/actors-mcp-server)",
          desc: "Cloud scrapers called Actors, useful for sites that block simple requests and for public social media research (YouTube, Reddit, X, TikTok, Instagram, LinkedIn). Needs Node 22+ and an APIFY_TOKEN. Free plan gives $5 of usage a month and blocks use when it runs out, which is the only hard ceiling. There is no setting that caps spending; on a paid plan, cap each run with maxTotalChargeUsd or maxItems when calling an Actor. Scraping public data can still break a platform's terms, and in the EU public posts are still personal data under GDPR; prefer aggregate, public signals (counts, trends, sentiment) and do not store who said what."
        },
        {
          term: "Suggested order",
          desc: "fetch for simple pages, Lightpanda for JavaScript-heavy pages, Apify for blocked sites and social media."
        },
        {
          term: "Configuration",
          desc: h("pre", null, JSON.stringify({
            mcpServers: {
              fetch: {
                command: "uvx",
                args: ["mcp-server-fetch"]
              },
              lightpanda: {
                command: "lightpanda",
                args: ["mcp"]
              },
              apify: {
                command: "npx",
                args: ["-y", "@apify/actors-mcp-server"],
                env: {
                  APIFY_TOKEN: "<your token>"
                }
              }
            }
          }, null, 2))
        }
      ]
    }
  ];

  const sectionRecords = sectionsData.map((sec) => {
    const titleEl = h("h2", { class: "card-title" }, sec.title);
    const subEl = sec.subtitle ? h("div", { class: "card-sub" }, sec.subtitle) : null;
    const itemEls = sec.items.map((it) =>
      h("div", { class: "help-item" },
        h("div", { class: "help-term mono" }, it.term),
        h("div", { class: "help-def" }, it.desc)
      )
    );
    const listEl = h("div", { class: "help-list" }, itemEls);
    const cardEl = h("section", { class: "card help-section", "aria-label": sec.title },
      h("div", { class: "card-head" }, titleEl, subEl),
      listEl
    );
    return { el: cardEl, titleEl, items: itemEls };
  });

  function filter(q) {
    query = q;
    const term = q.trim().toLowerCase();
    let anyVisible = false;
    for (const sec of sectionRecords) {
      let secMatch = false;
      const titleMatch = sec.titleEl.textContent.toLowerCase().includes(term);
      for (const item of sec.items) {
        const itemMatch = !term || titleMatch || item.textContent.toLowerCase().includes(term);
        item.hidden = !itemMatch;
        if (itemMatch) secMatch = true;
      }
      sec.el.hidden = !secMatch;
      if (secMatch) anyVisible = true;
    }
    emptyMsg.hidden = anyVisible;
  }

  function onInput(e) {
    filter(e.target.value);
  }

  if (query) filter(query);

  return h("div", { class: "help-container" },
    intro,
    h("div", { class: "help-search-wrap" }, searchInput),
    sectionRecords.map((s) => s.el),
    emptyMsg
  );
}
