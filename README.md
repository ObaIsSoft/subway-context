# Subway Context 🚇

> **On-demand, token-efficient context transit system connecting independent AI coding agents across multiple windows (Antigravity, Claude Code, OpenCode terminal).**

Stop copy-pasting 50,000-line markdown summaries between AI tools or blowing through your context window. **Subway Context** acts as an on-demand transit system between your coding agents: board any chat, search by quote, sync only the delta turns since your last visit, and extract high-density decision logs.

---

## Why Subway Context?

When you work across multiple windows (e.g. Antigravity IDE on screen 1, Claude Code in tab 2, OpenCode terminal running in background):
1. **Context Amnesia**: Each window is completely blind to what the other agent discovered, decided, or changed.
2. **Serial Number Hell**: Nobody remembers 36-character UUIDs (`78879399-3626-4504...`).
3. **Token Bankruptcy**: Copying full conversation dumps eats up 20,000–50,000 tokens before you even type your prompt.

**Subway Context solves this:**
- **Zero Dependencies**: Uses standard Python (`/usr/bin/python3`), runs 100% locally on your machine.
- **Search by Quote**: Search any phrase you or the agent said; jump directly to that turn.
- **Delta Sync (`since="last"`)**: Only transfers the new exchanges since the last time you bridged.
- **Decision Log (`mode="decisions"`)**: Cuts 85-90% of token noise by extracting only instructions, rationale, architecture decisions, and files touched.
- **Universal Multi-Window**: Works seamlessly across **Claude Code**, **Antigravity IDE**, and **OpenCode (terminal)**.

---

## Supported Transit Lines

| Line | Storage Format | Supported Capabilities |
|---|---|---|
| **Claude Code** | `~/.claude/projects/*/*.jsonl` | Full transcript, thoughts, active tab detection |
| **Antigravity** | `~/.gemini/antigravity/brain/` & summaries DB | Full transcript, planner thoughts, historical FTS |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | Full transcript, reasoning blocks, costs & models |

---

## Installation & Setup

### 1. Clone Repository
```bash
git clone https://github.com/ObaIsSoft/subway-context.git ~/Documents/dev/subway-context
```

### 2. Register in Claude Code
```bash
claude mcp add --scope user subway-context -- /usr/bin/python3 ~/Documents/dev/subway-context/server.py
```

### 3. Register in Antigravity
Add to your `~/.gemini/config/mcp_config.json`:
```json
{
  "mcpServers": {
    "subway-context": {
      "command": "/usr/bin/python3",
      "args": ["/Users/obafemi/Documents/dev/subway-context/server.py"]
    }
  }
}
```

### 4. Register in OpenCode (CLI / Terminal)
Run:
```bash
opencode2 mcp add --global subway-context -- /usr/bin/python3 ~/Documents/dev/subway-context/server.py
```
Or add directly to `~/.config/opencode/opencode.json`:
```json
{
  "mcp": {
    "servers": {
      "subway-context": {
        "type": "local",
        "command": [
          "/usr/bin/python3",
          "/Users/obafemi/Documents/dev/subway-context/server.py"
        ]
      }
    }
  }
}
```
Verify with:
```bash
opencode2 mcp list
# ✓ subway-context connected
```

---

## Tool Reference

### `search_chat(quote, source, project)`
Search for a specific phrase or code snippet across all agent windows without needing an ID:
```json
{"quote": "queue the full pipeline run"}
```
Returns:
```markdown
# Subway Context: Search results for 'queue the full pipeline run':
- CLAUDE `78879399` (Codebase comprehensive review)
  Matched at Station [216] of 217 (2026-10-27 20:25)
  -> Transit forward: read_chat(chat_id='78879399', start=216) (1 stops left)
```

### `tag_chat(chat_id, alias)`
Assign a sticky, human-friendly alias to any session so you never have to remember UUIDs:
```json
{"chat_id": "78879399", "alias": "claude-main"}
```
Now you can simply refer to `@claude-main` in any command.

---

## 4-Tier Station Aliasing System 🏷️

Subway Context eliminates raw UUIDs with a layered 4-tier naming hierarchy:

| Tier | Format | Examples | Behavior |
|:---|:---|:---|:---|
| **1. Explicit Pins** | `@<alias>` | `@claude-main`, `@agy-live`, `@opencode-terminal` | **Immutable & sticky** via `tag_chat`. Never drifts when switching tabs or opening side tasks. |
| **2. Floating Latest** | `@<agent>-latest`, `@latest` | `@agy-latest`, `@claude-latest`, `@latest` | Dynamically resolves to the most recently touched session globally or per-platform. |
| **3. Workspace Heads** | `@<agent>-<project>` | `@agy-dziner`, `@claude-portfolio` | Dynamically resolves to the **latest active chat in that workspace folder**. |
| **4. Topic Slugs** | `@<agent>-<topic>` | `@agy-subway`, `@agy-gitignore`, `@agy-previous-chats` | Automatically extracted from conversation titles. Reference chats by **topic** rather than ID. |

---

### `read_chat(chat_id, mode, since, anchor_text, ...)`
Read context from another agent window with token-saving controls:
- **`since="last"`**: Only reads the delta since the previous bridge call between these two windows.
- **`since="bridge -2"`**: Time-travel back 2 syncs ago.
- **`mode="decisions"`**: Extracts user goals, strategic reasoning, action decisions, and touched files only.
- **`anchor_text="phrase"`**: Jump directly to an exact comment and read forward.

### `send_note(to, text, share_chat)` & `check_notes()`
Drop a lightweight dispatch into another agent window's inbox so it picks up where you left off.

---

## Daily Workflow & Prompting Guide

Natural prompts work best across all agents:

1. **List all active agent chats & highlights:**
   > *"Subway, list my chats"*
   > *(Calls `list_chats()` to show active tabs, lines, and latest exchanges)*

2. **Sync architectural decisions & modified files:**
   > *"Read the latest decisions from Claude Code"*
   > *(Calls `read_chat(chat_id="@claude-main", mode="decisions", since="last")`)*

3. **Search cross-agent history by phrase or snippet:**
   > *"Search our history for 'queue the full pipeline run'"*
   > *(Calls `search_chat(quote="queue the full pipeline run")`)*

4. **Catch up on a specific tagged station:**
   > *"Read decisions from @claude-main since our last sync"*
   > `read_chat(chat_id="@claude-main", mode="decisions", since="last")`

5. **Start reading from an exact quote:**
   > *"Read @claude-main starting from when I asked about 2022drift"*
   > `read_chat(chat_id="@claude-main", anchor_text="what about 2022drift")`

6. **Inter-agent mailbox handoff:**
   > *"Send a note to @opencode-terminal: auth refactor complete, please run tests"*
   > `send_note(to="@opencode-terminal", text="Auth refactor complete, please run tests")`

---

## Native OpenCode `@` Autocomplete Integration

Subway Context automatically syncs all 4 tiers of aliases directly into OpenCode's agent registry (`~/.config/opencode/agents/*.md`). 

Whenever you open OpenCode, all Subway lines appear in its native `@` autocomplete popup without manual file copying:
- Type `@claude-` to instantly select `@claude-main`, `@claude-latest`, or `@claude-<project>` without colliding with workspace files (e.g. `claude_report.json`).
- Type `@agy-` to view active Antigravity stations (`@agy-live`, `@agy-latest`, `@agy-<project>`, `@agy-<topic>`).
- Dynamic topic stubs carry a safety tag (`<!-- subway-auto-stub -->`) and are cleanly pruned when conversations cycle out of the top recent list, preserving your custom agent files.

---

## Architecture & Security

- **100% Local & Offline**: Operates over standard input/output (`stdio`). No remote API keys or third-party servers.
- **Read-Only on Chat Stores**: Never modifies conversation histories of host tools.
- **Sandboxed Metadata**: State, aliases, checkpoints, and mailboxes live strictly in `~/.subway-context/`.

## License
MIT
