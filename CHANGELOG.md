# Changelog

## 1.3.2 - 2026-11-02

- **HUD status is fast**: `/api/status` answers from the last snapshot (per project) and refreshes in the background once it is 10 seconds old or after any change made from the HUD. Only the first request after a restart waits for a full read. Routing, budgets and workers still read live data.
- **No more broken-pipe traces**: a browser that reloads or closes mid-response is not an error, so the HUD no longer prints a traceback for it.

## 1.3.1 - 2026-11-02

> [!IMPORTANT]
> Chat budgets were one global value: a budget set in one chat blocked every other chat whose own total passed it. Budgets now belong to the chat that sets them.

- **Per-chat budgets**: `subway-context budget chat <tokens>` typed in a chat sets that chat's own budget. Run in a terminal it sets a universal default (off unless you set it); `--chat <id>` sets one chat. A chat's budget never blocks another chat. Block and warning messages say whose budget it is and give the exact command. An old global chat budget becomes the universal default. The HUD settings card lists the universal default and each chat's own budget.
- **GitHub Copilot as a provider**:
  - Models come from Copilot Chat's own list on this Mac, one entry per model name, with other ids kept as aliases.
  - Workers run through the Copilot CLI (`npm i -g @github/copilot`, then run `copilot` once to sign in; the VS Code sign-in isn't shared).
  - Each worker records tokens (cached input included), AIC credits, requests, and the model `auto` picked.
  - The CLI can refuse specific models on a plan (Copilot Free accepts only `auto`). Subway learns which, retries once with `auto`, and stops routing refused models to workers; it relearns after 7 days, so a plan upgrade is picked up.
  - Routing follows the live chat allowance and the shared low-allowance rule; an empty chat allowance excludes Copilot. Inline completions are shown, never spent.
  - Copilot appears in model rankings, the usage graph (from Copilot CLI sessions) and every HUD endpoint.
- **Reviews**: Reviewers now receive the builder's task, so they judge the patch against it.
- **Guard**: Workers can edit files in their own worktrees under `~/.subway-context/worktrees`; the rest of Subway's state stays off limits.
- **HUD**: Rankings filters and graph colours come from the providers present; any new provider gets its own colour.
- **Not yet**: Copilot Chat sessions can't be read or handed off from (next release). Subway still can't see OpenCode free-model quota, so a worker on an exhausted free model can stall.

## 1.3.0 - 2026-11-02

> [!IMPORTANT]
> Fixes an unauthenticated budget route in 1.2.0. Users on 1.2.0 should upgrade immediately.

- **Budgets**: `subway-context budget` shows them; `subway-context budget <chat|provider> <tokens>` sets them; typing that command as your own chat message in Claude Code works, including 3.5M and even when blocked. The HUD sets them too. Agents can't: the guard is best effort for shell commands.
- **Budget enforcement**: Warnings at 80%, block at 100%. Claude Code blocks the prompt and tools through hooks. Antigravity and OpenCode have no such hook, so there the block lands on the next Subway tool call.
- **Worker token caps**: Told to wrap up at two thirds, and no call is started that would pass the cap. A call already running can still overrun. Counts thinking and Claude cache creation. Set Claude caps above about 100K: one call can cost 35K.
- **Custom providers**: Report no tokens, so they use max_seconds.
- **Worker pings**: Finished, failed or stopped workers report to the launching chat. Every client gets a line on its next tool result; Claude Code also gets it on your next message.
- **Worker priority**: Urgent, normal, low. Waits return as soon as an urgent worker needs attention.
- **God mode**: No worker count, only free memory limits it. `auto` spreads work across providers with Claude only as a last resort. It may still pick a model with no quota left.
- **OpenCode and local workers**: Run through ACP, so they ask permission and can be denied. `auto_safe` lets them read and edit inside their own folder.
- **Model sign-in**: Models that fail sign-in are skipped for 24 hours.
- **HUD**: Budget rows with real usage, reset card cycles through providers, spend chart coloured per provider, worker history pages with Load more.
- **Worktrees**: Removed after each worker. Windows is written but untested.
- Fail-closed patch verifier.
- Routing by each model's real results.
- Models out of quota skipped.
- Exact code briefs for workers.
- Leases claimed on first edit.
- Compaction warnings and readable history by segment.
- Desktop alerts only for what is still waiting, click to open the HUD.
- The HUD starts with Subway and opens in the editor on first use.
- A Subway: Open HUD editor command.
- Auto-approve never asks.
- OpenCode workers continue when they stop early.
- Copilot usage gauges.
- Kimi usage gauges (experimental, from an undocumented endpoint).
- Compaction markers name the right segment.
- Help page.
- Recommended companion tools in the README.
- Known gaps: Windows untested; no OpenCode Go gauges; a call already running can pass a token cap.

## 1.2.0 - 2026-11-02

- Initial release of budget routes and multi-window transit features.
