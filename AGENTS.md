# AGENTS.md — Subway Context Protocol

`subway-context` is the universal, on-demand transit system connecting independent AI coding agents across different windows and environments (**Antigravity**, **Claude Code**, **OpenCode terminal**, and other MCP clients).

---

## 1. Prime Directive

1. **On-Demand Only**: Never stream or push an entire chat transcript into your context window automatically. Only board and read another agent's session when explicitly requested by the user or when needed to resolve an architectural question.
2. **Prioritize Token Conservation**:
   - For catching up on what another agent decided or changed, **always prefer `mode="decisions"`** first.
   - When resuming an ongoing loop between two windows, use `since="last"` to read **only the incremental delta**.
   - If looking for a specific topic, use `search_chat(quote=...)` or `anchor_text=...` to start reading exactly from that point.
3. **No Hex Serial Numbers**:
   - Subway resolves stations through a 4-tier naming hierarchy:
     1. **Explicit Pins**: `@<alias>` set via `tag_chat` (e.g. `@claude-main`, `@agy-live`).
     2. **Floating Latest**: `@<agent>-latest` or `@latest` for the most recently active session.
     3. **Workspace Heads**: `@<agent>-<project>` (e.g. `@agy-my-project`, `@claude-portfolio`).
     4. **Topic Slugs**: `@<agent>-<topic>` (e.g. `@agy-subway`, `@agy-gitignore`).
   - Refer to chats by alias, open tab name, or search query. Never require human users to copy hex UUIDs.

---

## 2. Supported Lines (Providers)

| Line | Storage Format | Auto-Detection |
|---|---|---|
| **Claude Code** | `~/.claude/projects/*/*.jsonl` | Active process PID via `~/.claude/sessions/` (maps to session ID & tab name) |
| **Antigravity** | `~/.gemini/antigravity/brain/` & `conversation_summaries.db` | Window-level ID (`agy-<pid>`), unless `ANTIGRAVITY_CONVERSATION_ID` is set |
| **OpenCode** | `~/.local/share/opencode/opencode.db` (SQLite) | Window-level ID (`opencode-<pid>`) |

---

## 3. Tool Reference & Workflows

### A. Locating a Chat Without Remembering UUIDs
```python
# 1. Search by exact phrase or code snippet
search_chat(quote="queue the full pipeline run")

# 2. List recent lines and active open tabs
list_chats(limit=10)
```

### B. Catching Up with Another Window (Delta Sync)
```python
# Read only what happened in Claude since our last sync
read_chat(chat_id="@claude-main", since="last")

# Time travel: read context starting from 2 syncs ago
read_chat(chat_id="@claude-main", since="bridge -2")
```

### C. Extracting Rationale & Decisions (90% Token Reduction)
```python
# Skips intermediate syntax errors, large code dumps, and tool spam
read_chat(chat_id="@claude-main", mode="decisions", turns=10)
```
Returns:
- Direct user instructions.
- Strategic reasoning from agent thoughts (`[!NOTE] Thought Process`).
- Architectural decisions and execution commitments.
- Touch manifest of all files modified or referenced.

### D. Inter-Agent Dispatch Mailbox
```python
# Send a transit link to another window
send_note(to="@claude-main", text="Finished refactoring auth. Check git diff.")

# In the recipient window:
check_notes()
```

### E. Live Workspace Guard (Collision Shield)
```python
# 1. Acquire a temporary lease on a file before editing (advisory except in Claude Code, where PreToolUse hook enforces it)
claim_file(filepath="src/components/Header.tsx", purpose="Layout refactor", duration_seconds=300)

# 2. Review and sign off on completed modifications
signoff_file(filepath="src/components/Header.tsx", verdict="approved", notes="Verified responsive layout")

# 3. Release lease once edits and commits are complete
release_file(filepath="src/components/Header.tsx")

# 4. Check active leases and sign-offs across the fleet
list_locks()
```

### F. Shared Decision Tracks
```python
# 1. Catch up on shared track state
join_track(track="main")

# 2. Broadcast a decision to all collaborating agents
broadcast_track(
    message="Switched router to CSS grid",
    track="main",
    files=["src/components/Header.tsx"],
    decisions=["Keep mobile backward compatibility"]
)

# 3. Read synchronized decisions
read_track(track="main", limit=10)
```

### G. Autonomous Worker Delegation & Live HUD
```python
# 1. Delegate a task to an autonomous worker subagent
# Note: Workers always require user approval before running (via HUD, CLI, or approval code)
spin_worker(role="Unit Tester", prompt="Run pytest and report regressions", provider="auto", track="main")

# 2. If given the 6-character approval code by the user:
approve_worker(worker_id="worker_496c67", approval_code="ABC123")

# 3. Check worker status or inspect logs
worker_status(worker_id="worker_496c67")

# 4. Handle worker permission requests if needed (relay works for Antigravity and Claude workers)
permission_requests(worker_id="worker_496c67")
answer_permission(request_id="perm_12345678", decision="allow")

# 5. Collect final reports from completed workers
collect_reports(track="main", wait=True)

# 6. View live transit dashboard snapshot (or start HUD with start_hud())
hud_status()
```

---

## 4. Grounding & Code Inspection

`subway-context` is designed to transmit **intent, decisions, and coordinates**. 
When another agent mentions that it modified a file, **do not ask it to print the full file contents**. Use your own native file-reading tools (`read_file`, `git diff`) to inspect the local disk directly.

## Worker Permission Requests

MCP can't push into a chat, so each chat has to listen while it has workers running:

- **Claude Code**: run `subway-context watch --stream` through your background monitor tool. Each request arrives as an event.
- **Antigravity and OpenCode**: call `watch_workers` in a loop. Pending requests also appear on your next Subway tool call.

Check that a request is still pending first, because the user may have answered in the HUD. Then show it to them and pass on their answer with `answer_permission`. Never decide for them.

## Delegating Well
Workers should spend tokens editing, not searching. One round spent 3.83M input tokens on 276 tool calls with about 4 edits. The next did 5 tasks on 332K.
- Put the code in the prompt: extract the exact functions, so workers never read large files.
- One task, one small scope, with an exact spec and a limit on tool calls.
- Use isolate=true so workers can't overwrite each other. Changes return as patches, and clashes are flagged.
- Use the cheapest model that fits, and low effort for a precise spec. Thinking is the biggest output cost.
- Set a token cap about 2-3 times the expected use, because a cap too tight wastes the whole run.
- Don't use workers for experiments that wait on permissions: run those directly.
- The initiator plans and reviews. At [LOW LIMIT], stop implementing and hand off.
- Verify patches yourself: compile, tests, and a real browser check.

### Lessons from real runs

- A thin brief is the costliest option. Saving Claude tokens on a spec wasted 157K of worker tokens. Give workers the exact code and the exact edit, so they never have to hunt.
- The initiator measures and specifies; workers type. A short direct probe has beaten worker guesses again and again, including wrong theories of our own. Prove a cause before fixing it.
- Experiments and live tests that need permission go in a script run by the initiator, or in one command the user can approve. Workers can't run most commands.
- Check what a router will pick before launching. Never launch `auto` blind: it once sent three workers to Opus.
- Check a worker's report against the tree. Workers report success with no files changed, and reviewers raise false alarms: verify each finding in the code.
- Count everything that costs: thinking tokens and cache creation, not just input and output. Each call resends the whole context, so a cap must allow for the next call.
- Wrap up at two thirds of a cap, and never start a call that would pass it. Make edits early, so a stop loses little.
- Never print large outputs into the chat. Trim every command, and never echo a worker's prompt.
- Before blaming a model, prove the runner. Every OpenCode edit failure in our first sessions came from our own ACP runner (auto-deny, relative paths, auto-approve gaps, and an id collision that wiped the session id at the first edit). Once fixed, three free OpenCode models landed a calibration edit in 14-17s at about 34-40K tokens, faster and cheaper than Gemini Flash Low (36s, 71K).
- Don't rely on a test harness's silence: a hook or guard can stop a whole command, so confirm it ran.
- A killed Antigravity turn sends no total, and the resumed turn's total then includes it. Trust the live per-step count over a final total when they disagree.

