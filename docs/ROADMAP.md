# Subway roadmap

The sixteen items from the 2026-11-02 planning list, refined into goals, approach, a test for done, and dependencies. Grouped into releases so each release ships something whole.

Rules that apply to every item:
- Nothing provider- or plan-specific is hardcoded. Read what is detected on the device.
- Every existing notification stays. New features add, never remove.
- Builders and reviewers are picked by role and remaining allowance, reviewer on another provider (`review="auto"`).
- Tokens and passwords are never read from other apps or printed.

## Release plan

| Release | Theme | Items |
|---|---|---|
| 1.3.x | Finish what is open | Copilot models, usage and workers; provider colours; low-allowance handoff; docs and installer |
| 1.4 | Fast and measurable | 1 load test, 6 speed, 19 model speed, 9 estimates and progress, 10 history, 16 modes, 3 UI design |
| 1.5 | Threads, roles and events | 2 lineage, 17 capability roles, 7 thread management, 8 thread to thread, 21 split threads at compaction, 15 concurrency, 5 post mail |
| 2.0 | Beyond the editor | 20 delegation shapes, 18 specialised models, 4 MCP passthrough, 11 other editors, 14 outside usage, 12 app context, 13 browser LLMs |

1.4 comes first because the load test and speed work tell us where the limits are before 1.5 adds more threads.

## 1.4: Fast and measurable

### 1. Load test
- **Goal:** know how Subway behaves with two Claude Code tabs, the HUD and the maximum workers running at once.
- **Approach:** a harness that starts N fake MCP clients over stdio and calls the common tools in parallel (read_chat, read_track, claim_file, broadcast_track, spin_worker in dry mode), while the HUD polls. Then one real run: two Claude tabs plus a full swarm. Record p50/p95 tool latency, hook latency, lock waits, CPU, RAM, and any corrupted or lost JSON (ledger, locks, workers).
- **Done when:** a repeatable `subway-context loadtest` report exists, with no lost ledger entries or corrupted state at full load, and the numbers feed item 6.
- **Depends on:** nothing.

### 6. Latency and speed
- **Goal:** every tool call and hook feels instant.
- **Approach:** measure first (item 1). Likely wins:
  - Hooks run on every prompt and every tool call, each starting a fresh Python. Make the hook a thin client to the always-running HUD process, with a cold-start fallback.
  - Index transcripts once and update incrementally, instead of re-parsing on each read_chat or search_chat.
  - The 10-second status cache (on branch `review/gemini-relay-hud-cache`) after review.
  - Cache gauge reads per provider with their own refresh intervals.
- **Done when:** hook p95 under 50 ms and read_chat p95 under 300 ms on a large transcript, measured by item 1's harness.
- **Depends on:** 1.

### 19. Model speed
- **Goal:** speed counts in routing, next to strength and allowance.
- **Approach:** measure each model's real speed from every worker run: time to first output, output tokens per second, and total time per kind of task. Keep it per model and per provider, since the same model can be faster on one. Routing weighs speed by the task: interactive and small tasks favour fast models, deep tasks favour strong ones. Show speed in Model rankings next to the C, R and W dots.
- **Done when:** every model that has run shows measured speed, and routing picks a faster model for a small task when strengths are close (tested).
- **Depends on:** nothing; better with 1.

### 9. Time and complexity estimates, progress
- **Goal:** each task shows an estimate before it starts and progress while it runs.
- **Approach:** estimate_complexity already gives a tier. Add time and token estimates from model_stats history (actuals per role, complexity and model), shown as a range. Progress comes from worker steps against the typical count for that kind of task, plus files touched. Estimates recalibrate from each finished worker.
- **Done when:** the HUD shows estimate, elapsed time and ETA per worker, and estimates land within the range for most finished tasks (tracked).
- **Depends on:** nothing; better with 10.

### 10. History and ledger management
- **Goal:** the ledger and worker history stay useful as they grow.
- **Approach:** search and filters (by track, provider, worker, kind of entry), retention settings, archive instead of delete, export, and a compact view that folds repeated permission lines. Remove the 50-entry cap on worker history in the HUD (paged instead). Each entry records who really posted it, so one chat can't post under another's ID.
- **HUD controls:**
  - every worker gets archive, delete (with a confirm step built into the page), label/rename and pin;
  - its row opens a modal with the full log (live tail, with "show more" instead of a cut-off), the report, the patch, the review verdict, and the triggered chain (builder → reviewer → follow-ups);
  - bulk actions on a selection;
  - the same controls exist on ledger entries.
- **Done when:** a month of entries loads fast, is searchable, and nothing is lost by retention without archiving. Every worker can be labelled, archived, deleted and opened to its log and report from the HUD, on phone and desktop.
- **Depends on:** nothing.

### 16. Normal and god modes
- **Goal:** two clear modes the user understands and can switch per task.
- **Approach:** one table, shown in the HUD and help page:
  - **Normal:** budget-aware, approval as set by the user, worker counts by RAM, models picked for value, lineage within limits.
  - **God:** frontier models first by gauges and quota, worker count limited only by memory, lineage may exceed the limits, the same budgets and caps still apply.
  - Both keep the stop button, caps and the user's permission choices.
  - Mode is set per chat, per task, or in a single command.
  - **Delegation nudge (both modes):** the principal chat should check, brief and verify, while workers make the changes. The tool hooks count the file edits a principal chat makes itself. After a few (a setting, default 3, per task), the prompt hook adds a line saying "You have made N edits yourself; brief a worker for the rest", naming the best provider by allowance. Worker chats are never nudged. It is a nudge only, never a block, and it can be turned off in the HUD settings.
- **Done when:** the mode is visible on every worker, and switching mode changes only the documented behaviours (tested). A principal chat that edits past the threshold sees the nudge once per task, and a worker never does.
- **Depends on:** nothing.

### 3. UI design and polish
- **Goal:** a calmer, clearer HUD.
- **Approach:**
  - A design pass first, as a short spec with mockups the user approves, then the build.
  - Covers a type scale (font family, sizes, weights), a palette beyond black and gold that includes the provider colours, spacing, and where modals open, including the permission prompt that was hard to click.
  - The weekly and session gauge colours stay as they are.
  - Mobile-first, then tablet, laptop and desktop.
- **Done when:** the user approves the spec and the built HUD matches it on all four screen sizes.
- **Depends on:** the provider colours build.

## 1.5: Threads and events

### 2. Lineage test (capacity and capability)
- **Goal:** prove sub-workers before they ship (code is on branch `next`).
- **Approach:**
  - **Capacity:** the RAM-scaled limits, refusals past them, god mode going beyond them, and stopping a parent stopping its children.
  - **Capability:** a real task that benefits from splitting, measured against one worker doing it alone for time, tokens and quality; sub-worker patches land in the parent's patch; budgets roll up.
  - Sub-worker use is decided per task by role, task and complexity, not always on.
- **Done when:** both tests pass on a real task, and the comparison shows when splitting pays off.
- **Depends on:** 1, 9.

### 17. Capability roles
- **Goal:** each model does the work it is best at, and stronger models lead weaker ones.
- **Approach:**
  - **Higher roles** go to the models strongest at reasoning: planner, architect, reviewer, overseer. Ratings come from rankings and track record, not a fixed list.
  - **Overseer pattern.** An overseer writes a blueprint: the plan, the interfaces, files per worker and acceptance tests, and where useful worked scenarios. It splits the work across cheaper or free models for the grunt work, checks each result against the blueprint, and sends back what fails. For example, a model strong at front-end design oversees UI work that free models implement.
  - **Specialist roles** by measured strength:
    - **Kinds of work:** writing, design, images, tests, research.
    - **Coordination:** a lineage overseer or a tree overseer.
    - **Assignments are examples, not rules:** say nemotron free on writing, mimo on images, beam as tree overseer. Each assignment comes from rankings and results on this machine, and changes as models change.
  - The track record (model_stats) is kept per role, so a model good at review but weak at implementation is used that way.
- **Done when:**
  - A task given to an overseer produces a blueprint, gets done by cheaper workers, and passes the overseer's checks.
  - The cost and quality are recorded against one strong model doing it alone.
- **Depends on:** 2, 9, 19.

### 7. Thread management
- **Goal:** see and manage every thread in one place.
- **Approach:** a thread is any chat or worker, from any provider. The HUD lists them with provider, state, last activity, track and cost. You can rename, pin, tag, archive, link to a track, stop or resume. Stale threads are flagged.
- **Tags and tracking in the HUD:** change a thread's tag, track and label from the HUD (the same data as tag_chat), and follow a thread's activity there: last decisions, files touched, workers it started.
- **Settings per thread and universal:** every setting (budget, compaction, handoff, mode, auto-approve view, nudges) has a universal value and an optional per-thread override, shown side by side with "reset to universal". Worker settings inherit from the thread that started them.
- **Input from the HUD, later:** first release is view-only. Sending a message or command into a thread from the HUD comes after, per client, where the client allows it.
- **Done when:** every live chat and worker on the machine appears and can be acted on from the HUD; tags and per-thread settings can be changed there and take effect in that thread.
- **Depends on:** 10.

### 8. Thread to thread context
- **Goal:** one thread can follow another, on any provider, and get its decisions or chosen events.
- **Approach:**
  - **Follow:** `follow_thread(source, events, deliver_to)` subscribes, for example to decisions, files changed, errors or a worker finishing. Matches are delivered as notes or pings to the follower, instead of the follower polling read_chat.
  - **Copilot:** following Copilot threads needs a reader for Copilot Chat's saved sessions, which is a new source.
- **Done when:** a Copilot thread and a Claude thread can each follow the other and receive only the events they asked for.
- **Depends on:** 7, and a Copilot chat reader.

### 21. Split threads at compaction
- **Goal:** a project too big for one thread is split rather than squeezed. When compaction would still leave a big context, the highlights become threads of their own.
- **Approach:**
  - **Find the highlights:** when a chat nears its compaction point, Subway reads it with read_chat in decisions mode. It groups the open work into highlights, each with its goal, decisions, files, open questions and done-when. Finished work becomes one summary line.
  - **Split, don't just compact:** the chat compacts down to a short index of the highlights. Each open highlight becomes a thread: a worker, a continuation chat on the best provider by allowance and rankings, or a parked entry to start later. Each thread gets only its own slice of the context, so none of them starts big.
  - **Stay linked:** each child thread follows its parent through item 8 (decisions and finish events). Its result comes back to the parent's index, so the parent remains the overseer (item 17) with a small context.
  - **When:** in "suggest" mode, the compaction notice lists the proposed highlights and you pick which to spin. In "auto" mode it spins them, within the lineage limits (item 2) and the budgets. A chat whose work forms a single highlight just compacts as today.
  - **Any provider:** it works for every chat Subway can read and compact (Claude via its hook, Antigravity via `agy -p "/compact"`), so nothing is tied to one client.
- **Done when:** a long project chat compacts into an index of a few lines plus several linked threads. Each thread finishes its piece and reports back, and none starts near its compaction point.
- **Depends on:** 7, 8, 17, 2.

### 15. Concurrency from one chat
- **Goal:** typing several commands in the same chat runs them at the same time.
- **Approach:**
  - An editor chat handles one prompt at a time, so Subway does it. The user should not have to remember anything.
  - **The chat model decides.** A `run_concurrent(tasks)` tool, with Subway's MCP instructions telling every chat model when to use it:
    - when a request holds independent parts (a numbered list, "also…", separate files or areas);
    - when a part is long-running;
    - when a part is a different kind of work from the rest.

    It does not split when the parts depend on each other, or when one is too small to be worth a worker.
  - **Subway nudges.** The prompt hook spots several independent asks in one message and adds a one-line hint to the chat to fan them out. This is a suggestion, never a forced split.
  - **The user can override.** The prefix (proposed: `&`) forces a line into the background, and `!` keeps it in the chat.
  - Clients without a prompt hook (Antigravity, OpenCode) rely on the MCP instructions and the tool.
  - Each concurrent task gets its own worker, isolated copy and lease checks, so they can't edit the same file at once.
- **Done when:** a message with three independent asks runs as three parallel workers without any prefix, a dependent multi-step ask stays in one thread, and both cases are tested.
- **Depends on:** 7.

### 5. Post mail (scheduled and event tasks)
- **Goal:** queue a task to start when something happens.
- **Approach:** a persistent mailbox run by the always-on HUD process. Each item has a task, a target provider or `auto`, and a condition:
  - a time;
  - a worker finishing (already built as triggers);
  - a ledger message;
  - a file changing;
  - a token warning;
  - a provider's allowance coming back.

  A task queued on a warning waits for that provider's reset, unless it has been handed off to another provider by then, in which case it is dropped with a ledger line. Items can be listed, edited and cancelled in the HUD.
- **Done when:** a task queued on a low-allowance warning starts by itself after the reset, and is skipped when the work was handed off.
- **Depends on:** the low-allowance handoff build, 7.

## 2.0: Beyond the editor

### 20. Delegation shapes: tree, mesh and adaptive
- **Goal:** workers can be connected in whatever shape fits the task, not only parent and children.
- **Approach:** Subway already has the pieces: a chain (triggers), a tree (lineage) and shared tracks. Add:
  - **Mesh:** peer workers on one track that send each other notes and claim work from a shared board ("blackboard"), with leases so two never take the same item.
  - **Pipeline:** each stage hands its output to the next, already possible with triggers, plus fan-out and fan-in (many workers, then one that merges).
  - **Adaptive network:** each connection (which role hands to which model) carries a weight. Weights are learned from outcomes: success, review verdicts, cost and time. Good paths get used more and bad ones fade, like neurons strengthening the links that work. Every routing choice stays explainable, saying why that worker was picked.
  - Shape is chosen per task by the planner (item 17) or the user, within the mode's limits (item 16).
- **Done when:** the same task runs as a tree, a mesh and a pipeline with results compared, and adaptive weights measurably improve choices over repeated tasks.
- **Depends on:** 2, 17, 5.

### 18. Specialised models and data
- **Goal:** use fine-tuned weights and domain data where they beat general models.
- **Approach:**
  - **Specialised local models:** fine-tunes and adapters (LoRA) run through Ollama. They are registered with what they are for, for example a code-review fine-tune or a model trained on the user's own codebase or writing style. Routing treats them like any model, ranked by their own track record.
  - **Datasets:** a project's documents and datasets are indexed locally, with retrieval added to a worker's context when the task needs it.
  - Nothing leaves the machine unless the user sends it.
- **Done when:** a registered specialised model wins its role on this machine's track record, and a worker can answer from an indexed project dataset.
- **Depends on:** 17.

### 4. Other MCP servers through workers
- **Goal:** workers can use the user's other MCP servers, such as Notion.
- **Approach:**
  - Subway lists the MCP servers configured in each client it detects, and `spin_worker(mcps=["notion"])` gives a worker those servers.
  - For each client, Subway uses that client's own way of loading them: an MCP config file for Claude, config content for OpenCode, and Antigravity's MCP folder.
  - Subway references the user's existing config and never copies secrets between clients.
  - The user opts in per server.
  - **Marketplaces and extensions, on request:** when a task needs a tool no client has, Subway can search the MCP registry and each client's extension or plugin marketplace, show what it found (publisher, permissions, install count), and install it only after the user approves it. Nothing is installed by itself, and installs are recorded in the ledger so they can be removed.
- **Done when:** a worker on each provider can call a Notion tool the user has enabled, and none can call one the user hasn't. A requested server or extension is found, shown and installed only after approval.
- **Depends on:** nothing.

### 11. Other editors
- **Goal:** Subway works in Cursor, Windsurf, Zed, JetBrains and VS Code with Copilot Chat.
- **Approach:**
  - **Step one:** the installer offers to add Subway to every MCP-capable client it finds, asking first. That gives handoff and lookups everywhere.
  - **Step two:** chat-history readers per editor, so their chats can be read and handed off from, in order of use.
  - **Step three:** the editor extension for each editor family.
  - **Mapping from real installs:** the user installs each editor once (Cursor and Windsurf first, as VS Code forks most of their mapping is shared; Zed and JetBrains next), runs one short chat with Subway connected, and a worker maps the real files: where chats, sessions and usage are stored, and how MCP is configured. Nothing is mapped from guesses.
- **Done when:** step one covers every detected editor; readers ship one by one, each tested against a real install.
- **Depends on:** nothing.

### 14. Usage from outside Subway
- **Goal:** the usage graph shows all use of a provider, not only Subway's.
- **Approach:** provider gauges already count everything on the account, including web and app use. Subway knows what its own threads and workers used. The graph stacks: this thread, other Subway threads, workers, and "outside Subway" (the difference). Where a provider gives no account total, the graph says so instead of guessing.
- **Done when:** for Claude, Antigravity and Copilot the four bands add up to the gauge's total.
- **Depends on:** 10.

### 12. Context from Mac apps
- **Goal:** pull context from Reminders, Notes, Calendar and documents on the laptop.
- **Approach:**
  - Prefer existing MCP servers for these apps through item 4 over building our own.
  - Read-only first, opted in per app, with macOS asking its own permission.
  - Nothing is sent to a provider unless the task asks for it.
- **Done when:** a worker can read today's calendar or a named note after the user allows that app.
- **Depends on:** 4.

### 13. Browser and desktop LLM apps
- **Goal:** include chats from browser or desktop LLMs, such as Gemini on the web.
- **Approach:** these have no supported API for reading consumer chats, and automating their pages risks their terms. The safe route is a small browser extension the user turns on per site. It reads the open conversation and sends it to Subway for lookups and handoff. It is read-only, with no automation of the site.
- **Done when:** a Gemini web chat can be read into a Claude or Antigravity chat with one click.
- **Depends on:** 11.

## Open decisions for the user
1. Release grouping above: keep, or move items between releases?
2. Concurrency prefix: `&`, or another marker?
3. Mac app context: read-only to start, and which apps first?
4. Browser LLMs: is a per-site browser extension acceptable?
5. God mode: may it ever auto-approve permissions, or always follow the user's setting? (Proposed: always the user's setting.)
