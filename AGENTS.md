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
   - Use `tag_chat` to assign sticky `@aliases` (e.g. `@claude-main`, `@terminal-coder`).
   - Refer to chats by alias, open tab name, or search query.

---

## 2. Supported Lines (Providers)

| Line | Storage Format | Auto-Detection |
|---|---|---|
| **Claude Code** | `~/.claude/projects/*/*.jsonl` | Active process PID via `~/.claude/sessions/` |
| **Antigravity** | `~/.gemini/antigravity/brain/` & `conversation_summaries.db` | Current conversation ID |
| **OpenCode** | `~/.local/share/opencode/opencode.db` (SQLite) | Live session records |

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

---

## 4. Grounding & Code Inspection

`subway-context` is designed to transmit **intent, decisions, and coordinates**. 
When another agent mentions that it modified a file, **do not ask it to print the full file contents**. Use your own native file-reading tools (`read_file`, `git diff`) to inspect the local disk directly.
