# Changelog

## 1.2.1 - 2026-11-02

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

## 1.2.0 - 2026-11-02

- Initial release of budget routes and multi-window transit features.
