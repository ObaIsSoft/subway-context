#!/usr/bin/env python3
"""subway-context: an on-demand, multi-window context transit system for AI coding agents.

Connects Antigravity, Claude Code, OpenCode (terminal), and any MCP-compatible agent.
Named after a subway transit system: jump between windows, lines, and stations on demand
without serial numbers or full re-reading penalties.

Features:
  1. Multi-Client Support:
     - Claude Code (~/.claude/projects/ & ~/.claude/sessions/)
     - Antigravity (~/.gemini/antigravity/brain/ & conversation_summaries.db)
     - OpenCode (~/.local/share/opencode/opencode.db)
  2. Quote / Anchor Search: search across any agent chat by exact words or code snippet.
  3. Incremental Stations / Watermarks: since="last", since="bridge -N" syncs only the delta.
  4. Decision & Rationale Extraction: mode="decisions" extracts instructions, plans,
     architectural choices, and execution commitments.
  5. Touch Manifest: auto-detects and lists all project files referenced or changed.
  6. Sticky Station Aliases: tag_chat("claude-main") so you don't need IDs.
  7. Cross-Agent Mailbox: send_note / check_notes for inter-agent handoffs.

Data sources (read-only):
  * Claude Code:  ~/.claude/projects/*/*.jsonl, ~/.claude/sessions/*.json
  * Antigravity:  ~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl
                  & conversation_summaries.db
  * OpenCode:     ~/.local/share/opencode/opencode.db
Writes:
  * ~/.subway-context/ (aliases, checkpoints, note inboxes)
"""
from __future__ import annotations

import datetime as _dt
import glob
import http.server
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = "1.1.0"
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("SUBWAY_CLAUDE_DIR", os.path.join(HOME, ".claude"))
AGY_DIR = os.environ.get("SUBWAY_AGY_DIR", os.path.join(HOME, ".gemini", "antigravity"))
OPENCODE_DIR = os.environ.get("SUBWAY_OPENCODE_DIR", os.path.join(HOME, ".local", "share", "opencode"))
SUBWAY_DIR = os.environ.get("SUBWAY_DIR", os.path.join(HOME, ".subway-context"))
LOCKS_FILE = os.path.join(SUBWAY_DIR, "locks.json")
WORKERS_DIR = os.path.join(SUBWAY_DIR, "workers")
TRACKS_DIR = os.path.join(SUBWAY_DIR, "tracks")

DEFAULT_MAX_CHARS = 30000
MAX_THOUGHT_CHARS = 1200


def log(*a: Any) -> None:
    print("[subway-context]", *a, file=sys.stderr, flush=True)


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def one_line(s: str, n: int = 120) -> str:
    return clip(re.sub(r"\s+", " ", s or "").strip(), n)


def fmt_ts(ts: Any) -> str:
    if ts is None or ts == "":
        return ""
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            v = float(ts)
            while v > 1e11:
                v /= 1000.0
            return _dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")
        s = str(ts).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        d = _dt.datetime.fromisoformat(s.split(" +")[0].strip())
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def sort_key_ts(ts: Any) -> float:
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            while v > 1e11:
                v /= 1000.0
            return v
        s = str(ts).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s).split(" +")[0].strip()
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# File extraction & touch tracking
# --------------------------------------------------------------------------

FILE_PATH_RE = re.compile(
    r"""(?:\b|['"`])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|html|css|json|md|sql|yaml|yml|sh|toml|go|rs|env|pbtxt))\b"""
)


def extract_files_from_text(text: str) -> Set[str]:
    hits = set()
    for m in FILE_PATH_RE.finditer(text):
        path = m.group(1).strip()
        if not path.startswith("http") and not path.startswith("0.") and "/" in path:
            hits.add(path)
        elif path.endswith((".py", ".ts", ".tsx", ".js", ".html", ".md", ".sql", ".json", ".sh")):
            hits.add(path)
    return hits


# --------------------------------------------------------------------------
# Stations & Checkpoints (~/.subway-context/)
# --------------------------------------------------------------------------

def _ensure_subway_dir() -> None:
    os.makedirs(SUBWAY_DIR, exist_ok=True)
    os.makedirs(os.path.join(SUBWAY_DIR, "inbox"), exist_ok=True)
    os.makedirs(WORKERS_DIR, exist_ok=True)
    os.makedirs(TRACKS_DIR, exist_ok=True)


def _load_json(filename: str) -> Dict[str, Any]:
    path = os.path.join(SUBWAY_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_json(filename: str, data: Dict[str, Any]) -> None:
    _ensure_subway_dir()
    path = os.path.join(SUBWAY_DIR, filename)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def slugify_topic(title: str, max_words: int = 3) -> str:
    """Extract a clean, readable topic slug from a conversation title."""
    if not title:
        return "chat"
    text = re.sub(r"[^\w\s-]", "", title).strip().lower()
    words = [w for w in re.split(r"[\s_-]+", text)
             if w and w not in ("to", "the", "a", "an", "and", "in", "on", "for", "of", "with", "at", "by", "from")]
    if not words:
        return "chat"
    return "-".join(words[:max_words])


def load_aliases() -> Dict[str, str]:
    return _load_json("aliases.json")


def _sync_opencode_agent_stubs() -> None:
    """Keep OpenCode's ~/.config/opencode/agents/ in sync with all 4 Subway alias tiers:
    1. Pinned aliases from aliases.json
    2. Floating latest stubs (@agy-latest, @claude-latest, @opencode-latest)
    3. Dynamic workspace heads for active projects (@agy-<proj>, @claude-<proj>)
    4. Top recent topic slugs (@agy-<topic>)
    """
    ag_dir = os.path.expanduser("~/.config/opencode/agents")
    try:
        os.makedirs(ag_dir, exist_ok=True)
    except Exception:
        return

    aliases = load_aliases()
    chats: List[Dict[str, Any]] = []
    try:
        chats = all_chats(include_subagents=False)
    except Exception:
        pass

    desired_stubs: Dict[str, Dict[str, str]] = {}

    # 1. Pinned aliases
    for alias, cid in aliases.items():
        clean = alias.strip().lower().lstrip("@")
        desired_stubs[clean] = {
            "desc": f"Subway Context pinned transit connector to @{clean}.",
            "type": "pinned"
        }

    # 2. Floating latest
    for a in ("claude-latest", "agy-latest", "opencode-latest"):
        src_name = "Claude Code" if "claude" in a else ("Antigravity" if "agy" in a else "OpenCode")
        desired_stubs[a] = {
            "desc": f"Subway Context transit connector to latest {src_name} session.",
            "type": "latest"
        }

    # 3. Dynamic workspace heads
    projects = set()
    for c in chats:
        p = (c.get("project") or "").strip().lower()
        if p and p != "unknown":
            projects.add(p)
    for p in projects:
        desired_stubs[f"claude-{p}"] = {
            "desc": f"Subway Context transit connector to latest Claude Code in {p}.",
            "type": "ws"
        }
        desired_stubs[f"agy-{p}"] = {
            "desc": f"Subway Context transit connector to latest Antigravity chat in {p}.",
            "type": "ws"
        }

    # 4. Top recent Antigravity topic slugs
    agy_chats = [c for c in chats if c.get("source") == "antigravity"]
    agy_chats.sort(key=lambda c: sort_key_ts(c.get("updated")), reverse=True)
    for c in agy_chats[:6]:
        title = c.get("title") or ""
        slug = slugify_topic(title)
        if slug and slug != "chat":
            desired_stubs[f"agy-{slug}"] = {
                "desc": f"Subway Context transit to @agy-{slug} ({one_line(title, 60)}).",
                "type": "topic"
            }

    # Write desired stubs
    for stub_name, info in desired_stubs.items():
        fpath = os.path.join(ag_dir, f"{stub_name}.md")
        content = (
            f"---\n"
            f"description: {info['desc']}\n"
            f"mode: subagent\n"
            f"---\n"
            f"<!-- subway-auto-stub -->\n"
            f"You are the Subway Context transit connector for @{stub_name}.\n"
            f"When invoked, call MCP tool `read_chat(chat_id=\"@{stub_name}\", mode=\"decisions\", since=\"last\")` "
            f"to pull the latest context, decisions, and files touched into this session.\n"
        )
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    # Prune outdated subway-auto-stubs (preserve manual stubs and current desired stubs)
    try:
        for fname in os.listdir(ag_dir):
            if fname.endswith(".md"):
                sname = fname[:-3]
                if sname not in desired_stubs:
                    fpath = os.path.join(ag_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            hdr = f.read(500)
                        if "<!-- subway-auto-stub -->" in hdr:
                            os.remove(fpath)
                    except Exception:
                        pass
    except Exception:
        pass


def save_alias(alias: str, target_id: str) -> None:
    clean_alias = alias.strip().lower().lstrip("@")
    aliases = load_aliases()
    aliases[clean_alias] = target_id.strip()
    _save_json("aliases.json", aliases)
    _sync_opencode_agent_stubs()


def _checkpoint_key(c1: str, c2: str) -> str:
    a, b = sorted([c1.strip().lower(), c2.strip().lower()])
    return f"{a}<->{b}"


def get_checkpoints(c1: str, c2: str) -> List[Dict[str, Any]]:
    cps = _load_json("checkpoints.json")
    k = _checkpoint_key(c1, c2)
    return cps.get(k, [])


def record_checkpoint(c1: str, c2: str, c1_exchange: int, c2_exchange: int) -> None:
    cps = _load_json("checkpoints.json")
    k = _checkpoint_key(c1, c2)
    hist = cps.get(k, [])
    rec = {
        "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        c1: c1_exchange,
        c2: c2_exchange,
    }
    hist.append(rec)
    cps[k] = hist[-50:]
    _save_json("checkpoints.json", cps)


# --------------------------------------------------------------------------
# Turn Model & Exchanges
# --------------------------------------------------------------------------

def to_exchanges(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ex: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for t in turns:
        role = t["role"]
        if role in ("user", "summary"):
            cur = {
                "user": t["text"],
                "assistant": [],
                "thoughts": [],
                "tools": [],
                "files": set(),
                "ts": t.get("ts"),
                "kind": role,
            }
            cur["files"].update(extract_files_from_text(t["text"]))
            ex.append(cur)
        else:
            if cur is None:
                cur = {
                    "user": "",
                    "assistant": [],
                    "thoughts": [],
                    "tools": [],
                    "files": set(),
                    "ts": t.get("ts"),
                    "kind": "user",
                }
                ex.append(cur)
            if t.get("thought"):
                th = t["thought"].strip()
                if th and (not cur["thoughts"] or cur["thoughts"][-1] != th):
                    cur["thoughts"].append(th)
            if t.get("tool"):
                cur["tools"].append(t["tool"])
                cur["files"].update(extract_files_from_text(t["tool"]))
            if t.get("text", "").strip():
                cur["assistant"].append(t["text"].strip())
                cur["files"].update(extract_files_from_text(t["text"]))
    return ex


def render_exchange(i: int, e: Dict[str, Any], user_label: str, max_assistant: int,
                    include_thoughts: bool, include_tools: bool) -> str:
    out = []
    ts = fmt_ts(e.get("ts"))
    ts_str = f" ({ts})" if ts else ""
    if e["kind"] == "summary":
        out.append(f"### [{i}] Compacted summary of earlier conversation{ts_str}\n{e['user'].strip()}")
    else:
        out.append(f"### [{i}] {user_label}{ts_str}\n{e['user'].strip() or '(no text)'}")

    if include_thoughts and e.get("thoughts"):
        for th in e["thoughts"]:
            capped_th = clip(th, MAX_THOUGHT_CHARS)
            out.append(f"> [!NOTE] Thought Process\n> " + capped_th.replace("\n", "\n> "))

    if include_tools and e.get("tools"):
        t_lines = "\n".join(f"- `{t}`" for t in e["tools"][:10])
        out.append(f"**Tools used:**\n{t_lines}")

    a = "\n\n".join(e["assistant"]).strip()
    if a:
        out.append("**Assistant:**\n" + clip(a, max_assistant))
    return "\n\n".join(out)


# Comprehensive decision & intent heuristics
DECISION_PATTERNS = [
    r"\b(?:decided|decision|deciding|concluded|conclusion|verdict)\b",
    r"\b(?:agreed|agreed upon|accepted|rule:|rules:)\b",
    r"\b(?:plan|action plan|strategy|roadmap|step \d+:)\b",
    r"\b(?:we will|i will|let's|let us|going forward|instead of|rather than|choose|chosen|switching to)\b",
    r"\b(?:refactor|implement|build|remove|deprecate|architect|architecture)\b",
]
DECISION_RE = re.compile("|".join(DECISION_PATTERNS), re.IGNORECASE)


def render_decisions(exchanges: List[Tuple[int, Dict[str, Any]]], max_chars: int) -> str:
    """Extract a comprehensive Decision, Intent & Architecture Log."""
    out = ["## Subway Context: Decision & Architecture Log"]
    used = 0
    all_files: Set[str] = set()

    for i, e in exchanges:
        all_files.update(e.get("files", set()))
        points = []
        u = one_line(e["user"], 300)
        if u and e["kind"] != "summary":
            points.append(f"**User Instruction / Goal:**\n> {u}")

        # Capture key rationale from thoughts
        thought_points = []
        for th in e.get("thoughts", []):
            for sentence in re.split(r"(?<=[.!?\n])\s+", th):
                s = sentence.strip()
                if 20 < len(s) < 350 and DECISION_RE.search(s):
                    thought_points.append(f"- *Rationale:* {s}")
        if thought_points:
            points.append("**Strategic Reasoning:**\n" + "\n".join(thought_points[:5]))

        # Capture decisions / conclusions from assistant text
        assistant_points = []
        for a in e.get("assistant", []):
            for block in a.split("\n\n"):
                b = block.strip()
                if 20 < len(b) < 400 and DECISION_RE.search(b):
                    assistant_points.append(f"- *Action / Decision:* {one_line(b, 250)}")
        if assistant_points:
            points.append("**Decisions & Execution Commitments:**\n" + "\n".join(assistant_points[:5]))

        if points:
            entry = f"### Station [{i}] ({fmt_ts(e.get('ts'))})\n" + "\n\n".join(points)
            if used + len(entry) > max_chars:
                out.append("…(truncated further decisions; increase max_chars or narrow turns)")
                break
            out.append(entry)
            used += len(entry)

    if all_files:
        out.append("\n### Code & Artifacts Modified / Referenced Across Turns:")
        out.extend(f"- `{f}`" for f in sorted(all_files)[:30])

    return "\n\n".join(out)


def render_chat(meta: Dict[str, Any], turns: List[Dict[str, Any]], mode: str, n: int,
                start: int, max_chars: int, include_thoughts: bool, include_tools: bool,
                anchor_info: Optional[str] = None) -> str:
    ex = to_exchanges(turns)
    header = [
        f"# subway-context: {meta['source'].upper()} `{meta['id']}`",
        f"Title: {meta.get('title') or '(untitled)'}",
        f"Project: {meta.get('project') or '?'}   Last active: {fmt_ts(meta.get('updated'))}   Exchanges: {len(ex)}",
        f"Mode: {mode}",
    ]
    if anchor_info:
        header.append(f"Transit: {anchor_info}")
    if meta.get("subagent"):
        header.append(f"Subagent Line: Spun by Parent `{meta.get('parent')}`")

    # Discover any child subagents spun by this session
    cid = meta.get("id")
    children_lines: List[str] = []
    if meta.get("source") == "antigravity":
        try:
            con = _agy_db()
            if con:
                rows = con.execute("select conversation_id, title, preview from conversation_summaries where parent_conversation_id=?", (cid,)).fetchall()
                for ch_id, ch_title, ch_prev in rows:
                    ch_desc = ch_title or one_line(ch_prev or "", 60)
                    if not ch_desc:
                        try:
                            tp = _agy_transcript_path(ch_id)
                            if os.path.exists(tp):
                                with open(tp, "r", encoding="utf-8") as f:
                                    for _ in range(6):
                                        l = f.readline()
                                        if not l: break
                                        m = re.search(r"content=(.*?)(?:\n|\Z)", l)
                                        if m:
                                            snippet = m.group(1).replace("\\n", " ").strip()
                                            ch_desc = one_line(snippet, 70)
                                            break
                        except Exception:
                            pass
                    if not ch_desc:
                        ch_desc = "Subagent"
                    children_lines.append(f"- `{ch_id[:8]}`: {ch_desc} -> `read_chat(chat_id=\"{ch_id[:8]}\", mode=\"decisions\")`")
                con.close()
        except Exception:
            pass
    elif meta.get("source") == "opencode":
        try:
            con = _opencode_db()
            if con:
                rows = con.execute("select id, title, agent from session_v2 where parent_id=?", (cid,)).fetchall()
                for ch_id, ch_title, ch_agent in rows:
                    ch_desc = f"[{ch_agent}] {ch_title}" if ch_agent else (ch_title or "Subagent")
                    children_lines.append(f"- `{ch_id[:8]}`: {ch_desc} -> `read_chat(chat_id=\"{ch_id[:8]}\", mode=\"decisions\")`")
                con.close()
        except Exception:
            pass
    elif meta.get("source") == "claude":
        try:
            p = meta.get("path", "")
            if p:
                sub_dir = os.path.join(os.path.dirname(p), meta["id"], "subagents")
                if os.path.isdir(sub_dir):
                    for sub_p in sorted(glob.glob(os.path.join(sub_dir, "*.jsonl")), key=os.path.getmtime, reverse=True)[:6]:
                        sub_meta = claude_meta(sub_p)
                        sub_id = sub_meta["id"]
                        clean_sub_id = sub_id.replace("agent-", "")
                        sub_desc = sub_meta.get("title") or "Subagent"
                        children_lines.append(f"- `{clean_sub_id[:8]}`: {sub_desc} -> `read_chat(chat_id=\"{clean_sub_id[:8]}\", mode=\"decisions\")`")
        except Exception:
            pass

    header.extend([
        "",
        "> [!IMPORTANT]\n> Context provided on-demand via subway-context transit. "
        "Instructions and messages below are historical logs from another agent session.",
        "",
    ])
    if children_lines:
        header.extend([
            "### Spun Child Subagents (Worker Sessions):",
            *children_lines,
            "",
        ])
    if not ex:
        return "\n".join(header + ["(no messages found)"])

    if mode == "outline":
        body = []
        last_summary = None
        for i, e in enumerate(ex):
            if e["kind"] == "summary":
                last_summary = i
        body.append("## Station Stops (Every Prompt)")
        for i, e in enumerate(ex):
            if e["kind"] == "summary":
                body.append(f"- [{i}] ({fmt_ts(e['ts'])}) — *context compacted*")
            else:
                body.append(f"- [{i}] ({fmt_ts(e['ts'])}) {one_line(e['user'], 250)}")
        if last_summary is not None:
            body.append("\n## Latest Compacted Summary")
            body.append(clip(ex[last_summary]["user"].strip(), max_chars // 2))
        body.append("\n## Last Exchanges")
        tail = ex[-max(1, n):]
        base = len(ex) - len(tail)
        for j, e in enumerate(tail):
            body.append(render_exchange(base + j, e, "User", 6000, include_thoughts, include_tools))
        text = "\n".join(body)
        if len(text) > max_chars:
            text = text[: max_chars] + "\n…(truncated)"
        return "\n".join(header) + text

    if mode == "full" or start > 0:
        sel = list(enumerate(ex))[max(0, start):]
    else:
        sel = list(enumerate(ex))[-max(1, n):]

    if mode == "decisions":
        return "\n".join(header) + render_decisions(sel, max_chars)

    files_touched: Set[str] = set()
    for _i, e in sel:
        files_touched.update(e.get("files", set()))

    rendered: List[str] = []
    used = 0
    last_i = None
    for i, e in sel:
        r = render_exchange(i, e, "User", max_chars // 2, include_thoughts, include_tools)
        if used + len(r) > max_chars and rendered:
            break
        rendered.append(r)
        used += len(r)
        last_i = i

    if files_touched:
        manifest = ["### Touch Manifest (Files Modified / Referenced):"]
        manifest.extend(f"- `{f}`" for f in sorted(files_touched)[:25])
        rendered.insert(0, "\n".join(manifest))

    if last_i is not None and last_i < len(ex) - 1:
        rendered.append(f"\n…more exchanges ahead: call read_chat(start={last_i + 1})")

    return "\n".join(header) + "\n\n".join(rendered)


# --------------------------------------------------------------------------
# Provider 1: Claude Code
# --------------------------------------------------------------------------

_claude_meta_cache: Dict[str, Tuple[Tuple[float, int], Dict[str, Any]]] = {}


def claude_open_sessions() -> Dict[str, Dict[str, Any]]:
    res: Dict[str, Dict[str, Any]] = {}
    for p in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            d = json.load(open(p))
            pid = int(d.get("pid", 0))
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except PermissionError:
                    alive = True
                except OSError:
                    alive = False
            if alive and d.get("sessionId"):
                d["_pid"] = pid
                res[d["sessionId"]] = d
        except Exception:
            continue
    return res


def _read_tail(path: str, nbytes: int) -> List[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > nbytes:
            f.seek(size - nbytes)
            data = f.read()
            lines = data.split(b"\n")[1:]
        else:
            lines = f.read().split(b"\n")
    return [l.decode("utf-8", "replace") for l in lines if l.strip()]


def _read_head(path: str, nbytes: int) -> List[str]:
    with open(path, "rb") as f:
        data = f.read(nbytes)
    lines = data.split(b"\n")
    if len(data) == nbytes:
        lines = lines[:-1]
    return [l.decode("utf-8", "replace") for l in lines if l.strip()]


def _claude_is_human(o: Dict[str, Any], is_subagent: bool = False) -> bool:
    if o.get("isMeta"):
        return False
    if o.get("isSidechain") and not is_subagent:
        return False
    origin = o.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    if o.get("turnOrigin"):
        return o.get("turnOrigin") == "human"
    c = (o.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        return True
    return isinstance(c, str) and not c.lstrip().startswith("<")


def claude_parse_lines(lines: List[str], is_subagent: bool = False) -> List[Dict[str, Any]]:
    turns: List[Dict[str, Any]] = []
    for line in lines:
        if '"type":"user"' not in line and '"type":"assistant"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        typ = o.get("type")
        if o.get("isSidechain") and not is_subagent:
            continue
        msg = o.get("message") or {}
        content = msg.get("content") or []
        ts = o.get("timestamp")

        if typ == "user":
            if o.get("isCompactSummary"):
                txt = ""
                if isinstance(content, list):
                    txt = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                elif isinstance(content, str):
                    txt = content
                turns.append({"role": "summary", "text": txt, "ts": ts})
                continue
            if not _claude_is_human(o, is_subagent=is_subagent):
                continue
            txt_parts = []
            if isinstance(content, str):
                txt_parts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        t = b.get("text", "")
                        if not t.lstrip().startswith(("<ide_", "<system-reminder>", "[Image: source:")):
                            txt_parts.append(t)
            user_text = "\n".join(txt_parts).strip()
            if user_text:
                turns.append({"role": "user", "text": user_text, "ts": ts})

        elif typ == "assistant":
            ast_text = []
            ast_thoughts = []
            ast_tools = []
            if isinstance(content, str):
                ast_text.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        t = b.get("text", "")
                        if not t.lstrip().startswith(("<ide_", "<system-reminder>")):
                            ast_text.append(t)
                    elif bt == "thinking":
                        th = (b.get("thinking") or "").strip()
                        if th:
                            ast_thoughts.append(th)
                    elif bt == "tool_use":
                        inp = b.get("input") or {}
                        hint = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
                        tool_name = b.get("name") or "tool"
                        if tool_name == "Agent":
                            hint = f"Agent [{inp.get('subagent_type', 'subagent')}]: {inp.get('description', '')}"
                        ast_tools.append(f"{tool_name}: {one_line(str(hint), 120)}")
            for th in ast_thoughts:
                turns.append({"role": "assistant", "text": "", "thought": th, "ts": ts})
            for tl in ast_tools:
                turns.append({"role": "assistant", "text": "", "tool": tl, "ts": ts})
            if ast_text:
                turns.append({"role": "assistant", "text": "\n".join(ast_text), "ts": ts})
    return turns


def claude_files(include_subagents: bool = False) -> List[str]:
    files = [p for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")) if os.path.isfile(p)]
    if include_subagents:
        files.extend([p for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*", "subagents", "*.jsonl")) if os.path.isfile(p)])
    return files


def claude_meta(path: str, open_sessions: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    is_sub = "/subagents/" in path
    sid = os.path.basename(path)[:-6]
    parent_id = os.path.basename(os.path.dirname(os.path.dirname(path))) if is_sub else ""
    cached = _claude_meta_cache.get(path)
    if cached and cached[0] == key:
        meta = dict(cached[1])
    else:
        title = custom = last_prompt = ""
        for line in _read_tail(path, 1_500_000):
            if '"ai-title"' in line or '"custom-title"' in line or '"last-prompt"' in line:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "ai-title":
                    title = o.get("aiTitle") or title
                elif o.get("type") == "custom-title":
                    custom = o.get("customTitle") or o.get("title") or custom
                elif o.get("type") == "last-prompt":
                    last_prompt = o.get("lastPrompt") or last_prompt
        cwd = ""
        first_prompt = ""
        for line in _read_head(path, 400_000):
            if not cwd and '"cwd"' in line:
                m = re.search(r'"cwd":"([^"]+)"', line)
                if m:
                    cwd = m.group(1)
            if not first_prompt and '"type":"user"' in line:
                try:
                    o = json.loads(line)
                    if _claude_is_human(o, is_subagent=is_sub) and not o.get("isCompactSummary"):
                        c = (o.get("message") or {}).get("content")
                        if isinstance(c, list):
                            first_prompt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                        elif isinstance(c, str):
                            first_prompt = c
                except Exception:
                    pass
            if cwd and first_prompt:
                break
        display_title = custom or title
        if not display_title and first_prompt:
            display_title = f"[Subagent] {one_line(first_prompt, 70)}" if is_sub else one_line(first_prompt, 80)
        if not display_title:
            display_title = f"[Subagent of {parent_id[:8]}]" if is_sub else "(untitled)"

        meta = {
            "source": "claude", "id": sid, "path": path,
            "title": display_title,
            "first_prompt": first_prompt, "last_prompt": last_prompt,
            "project": cwd or os.path.basename(os.path.dirname(os.path.dirname(path) if is_sub else path)),
            "updated": st.st_mtime, "size": st.st_size,
            "subagent": is_sub, "parent": parent_id,
        }
        _claude_meta_cache[path] = (key, dict(meta))
    os_ = open_sessions if open_sessions is not None else claude_open_sessions()
    if sid in os_:
        meta["open"] = True
        meta["tab_name"] = os_[sid].get("name") or ""
        meta["status"] = os_[sid].get("status") or ""
    return meta


def claude_read(path: str, mode: str, n: int) -> List[Dict[str, Any]]:
    is_sub = "/subagents/" in path
    if mode == "recent":
        size = os.path.getsize(path)
        nbytes = 4_000_000
        while True:
            lines = _read_tail(path, nbytes)
            turns = claude_parse_lines(lines, is_subagent=is_sub)
            users = sum(1 for t in turns if t["role"] in ("user", "summary"))
            if users > n or nbytes >= size:
                return turns
            nbytes *= 4
    elif mode == "outline":
        lines = _read_head(path, 3_000_000)
    else:
        lines = _read_head(path, 15_000_000)
    return claude_parse_lines(lines, is_subagent=is_sub)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return claude_parse_lines(f)


# --------------------------------------------------------------------------
# Provider 2: Antigravity
# --------------------------------------------------------------------------

def _agy_db() -> Optional[sqlite3.Connection]:
    p = os.path.join(AGY_DIR, "conversation_summaries.db")
    if not os.path.exists(p):
        return None
    for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            con.execute("select 1 from conversation_summaries limit 1")
            return con
        except Exception:
            continue
    return None


def _agy_transcript_path(cid: str) -> str:
    return os.path.join(AGY_DIR, "brain", cid, ".system_generated", "logs", "transcript.jsonl")


def _workspace_name(uris: str) -> str:
    try:
        lst = json.loads(uris) if uris else []
    except Exception:
        lst = [uris]
    names = [u.rstrip("/").split("/")[-1] for u in lst if u]
    return ", ".join(names)


def agy_list(include_subagents: bool = False) -> List[Dict[str, Any]]:
    con = _agy_db()
    out: List[Dict[str, Any]] = []
    if con is None:
        return out
    try:
        fts_titles: Dict[str, str] = {}
        try:
            for cid, title in con.execute(
                    "select conversation_id, title from conversations_fts where chunk_index=-1"):
                if title:
                    fts_titles[cid] = title
        except Exception:
            pass
        rows = con.execute(
            "select conversation_id, title, preview, step_count, last_modified_time, workspace_uris, "
            "parent_conversation_id, killed, last_user_input_time from conversation_summaries").fetchall()
        for cid, title, preview, steps, lmt, ws, parent, killed, lui in rows:
            if parent and not include_subagents:
                continue
            display_title = title or fts_titles.get(cid)
            if not display_title and preview:
                display_title = one_line(preview, 80)
            if not display_title and parent:
                # Subagent without title: check task brief from transcript
                try:
                    tp = _agy_transcript_path(cid)
                    if os.path.exists(tp):
                        with open(tp, "r", encoding="utf-8") as f:
                            for _ in range(6):
                                l = f.readline()
                                if not l: break
                                m = re.search(r"content=(.*?)(?:\n|\Z)", l)
                                if m:
                                    snippet = m.group(1).replace("\\n", " ").strip()
                                    display_title = f"[Subagent] {one_line(snippet, 70)}"
                                    break
                except Exception:
                    pass
            if not display_title:
                display_title = f"[Subagent of {parent[:8]}]" if parent else "(untitled)"
            out.append({
                "source": "antigravity", "id": cid,
                "title": display_title,
                "project": _workspace_name(ws), "updated": lmt, "steps": steps,
                "subagent": bool(parent), "parent": parent or "",
            })
    finally:
        con.close()
    return out


def _strip_user_request(c: str) -> str:
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", c or "", re.S)
    return (m.group(1) if m else (c or "")).strip()


def agy_read(cid: str) -> List[Dict[str, Any]]:
    tp = _agy_transcript_path(cid)
    turns: List[Dict[str, Any]] = []
    if os.path.exists(tp):
        with open(tp, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                typ = o.get("type")
                ts = o.get("created_at")
                if typ == "USER_INPUT" and o.get("source", "").startswith("USER"):
                    turns.append({"role": "user", "text": _strip_user_request(o.get("content", "")), "ts": ts})
                elif typ == "SYSTEM_MESSAGE" and not turns:
                    # Capture subagent task instructions
                    raw_c = o.get("content", "")
                    m = re.search(r"content=(.*?)(?:\Z|</SYSTEM_MESSAGE>)", raw_c, re.S)
                    prompt_txt = m.group(1).strip() if m else raw_c.strip()
                    if prompt_txt:
                        turns.append({"role": "user", "text": f"[Subagent Task Brief]\n{prompt_txt}", "ts": ts})
                elif typ == "PLANNER_RESPONSE":
                    if o.get("thinking"):
                        th = o["thinking"].strip()
                        if th:
                            turns.append({"role": "assistant", "text": "", "thought": th, "ts": ts})
                    for tc in o.get("tool_calls") or []:
                        name = tc.get("name") if isinstance(tc, dict) else str(tc)
                        args = tc.get("args") if isinstance(tc, dict) else {}
                        if name == "invoke_subagent":
                            role = args.get("Role") or args.get("TypeName") or "subagent"
                            prompt = args.get("Prompt") or ""
                            turns.append({"role": "assistant", "text": "", "tool": f"invoke_subagent [{role}]: {one_line(str(prompt), 100)}", "ts": ts})
                        else:
                            cmd = args.get("CommandLine") or args.get("Path") or ""
                            turns.append({"role": "assistant", "text": "", "tool": f"{name} {one_line(str(cmd), 100)}", "ts": ts})
                    if o.get("content"):
                        turns.append({"role": "assistant", "text": o["content"], "ts": ts})
        if turns:
            return turns

    con = _agy_db()
    if con is None:
        return turns
    try:
        rows = con.execute(
            "select chunk_index, user_body, agent_body, user_step_byte_offsets, agent_step_byte_offsets, "
            "last_step_created_at from conversations_fts where conversation_id=? and chunk_index>=0 "
            "order by chunk_index", (cid,)).fetchall()
    finally:
        con.close()
    segs: List[Tuple[int, str, str]] = []
    for _ci, ub, ab, uo, ao, _ts in rows:
        for role, body, offs in (("user", ub, uo), ("assistant", ab, ao)):
            if not body:
                continue
            b = body.encode("utf-8")
            try:
                pairs = json.loads(offs.decode() if isinstance(offs, bytes) else (offs or "[]"))
            except Exception:
                pairs = []
            if not pairs:
                segs.append((0, role, body))
                continue
            for k, (step, off) in enumerate(pairs):
                end = pairs[k + 1][1] if k + 1 < len(pairs) else len(b)
                txt = b[off:end].decode("utf-8", "replace").strip()
                if txt:
                    segs.append((int(step), role, txt))
    segs.sort(key=lambda s: (s[0], 0 if s[1] == "user" else 1))
    for _step, role, txt in segs:
        turns.append({"role": role, "text": txt, "ts": None})
    return turns


# --------------------------------------------------------------------------
# Provider 3: OpenCode (Terminal Agent)
# --------------------------------------------------------------------------

def _opencode_db() -> Optional[sqlite3.Connection]:
    p = os.path.join(OPENCODE_DIR, "opencode.db")
    if not os.path.exists(p):
        return None
    for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            con.execute("select 1 from session_v2 limit 1")
            return con
        except Exception:
            continue
    return None


def opencode_list(include_subagents: bool = False) -> List[Dict[str, Any]]:
    con = _opencode_db()
    out: List[Dict[str, Any]] = []
    if con is None:
        return out
    try:
        rows = con.execute(
            "select id, title, directory, time_updated, cost, agent, model, parent_id from session_v2 order by time_updated desc"
        ).fetchall()
        for sid, title, dirpath, updated, cost, agent, model, parent_id in rows:
            if parent_id and not include_subagents:
                continue
            display_title = (f"[{agent}] " if agent else "") + (title or "(opencode session)")
            out.append({
                "source": "opencode",
                "id": sid,
                "title": display_title,
                "project": os.path.basename(dirpath or ""),
                "updated": updated,
                "cost": cost,
                "subagent": bool(parent_id),
                "parent": parent_id or "",
            })
    finally:
        con.close()
    return out


def opencode_read(sid: str) -> List[Dict[str, Any]]:
    con = _opencode_db()
    turns: List[Dict[str, Any]] = []
    if con is None:
        return turns
    try:
        rows = con.execute(
            "select type, data, time_created from session_message where session_id=? order by seq", (sid,)
        ).fetchall()
        for m_type, raw_data, ts in rows:
            try:
                d = json.loads(raw_data)
            except Exception:
                continue
            if m_type == "user":
                txt = d.get("text", "")
                if txt.strip():
                    turns.append({"role": "user", "text": txt, "ts": ts})
            elif m_type == "assistant":
                content = d.get("content", [])
                ast_text = []
                for b in content:
                    bt = b.get("type")
                    if bt == "reasoning":
                        th = (b.get("text") or "").strip()
                        if th:
                            turns.append({"role": "assistant", "text": "", "thought": th, "ts": ts})
                    elif bt == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            ast_text.append(txt)
                    elif bt == "tool":
                        call_name = b.get("name") or "tool"
                        ast_text.append(f"[tool {call_name}]")
                if ast_text:
                    turns.append({"role": "assistant", "text": "\n".join(ast_text), "ts": ts})
    finally:
        con.close()
    return turns


# --------------------------------------------------------------------------
# Multi-Window Registry & Resolution
# --------------------------------------------------------------------------

def all_chats(source: str = "all", include_subagents: bool = False) -> List[Dict[str, Any]]:
    chats: List[Dict[str, Any]] = []
    if source in ("all", "claude"):
        opens = claude_open_sessions()
        for p in claude_files(include_subagents):
            try:
                chats.append(claude_meta(p, opens))
            except Exception as e:
                log("claude meta failed", p, e)
    if source in ("all", "antigravity"):
        chats.extend(agy_list(include_subagents))
    if source in ("all", "opencode"):
        chats.extend(opencode_list(include_subagents))
    return chats


def resolve(ident: str, source: str = "all", project: str = "") -> Dict[str, Any]:
    ident = (ident or "").strip()
    aliases = load_aliases()
    clean_alias = ident.lower().lstrip("@")

    # 1. Explicit pinned aliases (highest precedence, never drifts)
    if clean_alias in aliases:
        target = aliases[clean_alias]
        chats = all_chats(source, include_subagents=True)
        for c in chats:
            if c["id"].lower() == target.lower():
                return c
        ident = target
        clean_alias = ident.lower().lstrip("@")

    chats = all_chats(source, include_subagents=True)
    if project:
        chats = [c for c in chats if project.lower() in (c.get("project") or "").lower()]
    if not chats:
        raise ValueError(f"no chats found across active agents (source={source}, project={project})")

    # 2. Floating Latest (@latest, @agy-latest, @claude-latest, @opencode-latest)
    if clean_alias in ("", "latest", "last", "newest"):
        return max(chats, key=lambda c: sort_key_ts(c.get("updated")))
    if clean_alias in ("agy-latest", "gemini-latest", "agy", "gemini"):
        agy_matches = [c for c in chats if c.get("source") == "antigravity"]
        if agy_matches:
            return max(agy_matches, key=lambda c: sort_key_ts(c.get("updated")))
    if clean_alias in ("claude-latest", "claude"):
        claude_matches = [c for c in chats if c.get("source") == "claude"]
        if claude_matches:
            return max(claude_matches, key=lambda c: sort_key_ts(c.get("updated")))
    if clean_alias in ("opencode-latest", "opencode"):
        opencode_matches = [c for c in chats if c.get("source") == "opencode"]
        if opencode_matches:
            return max(opencode_matches, key=lambda c: sort_key_ts(c.get("updated")))

    # 3. Dynamic Workspace Heads & Topic Slugs (<agent>-<target>)
    m = re.match(r"^(agy|gemini|claude|opencode)-([a-z0-9_-]+)$", clean_alias)
    if m:
        agent_prefix, rest = m.group(1), m.group(2)
        src = "antigravity" if agent_prefix in ("agy", "gemini") else agent_prefix
        agent_chats = [c for c in chats if c.get("source") == src]
        if agent_chats:
            # 3a. Workspace head match (e.g. @agy-dziner, @claude-portfolio)
            ws_matches = [c for c in agent_chats if rest in (c.get("project") or "").lower()]
            if ws_matches:
                return max(ws_matches, key=lambda c: sort_key_ts(c.get("updated")))

            # 3b. Topic slug search within this agent (e.g. @agy-subway-stations, @agy-gitignore)
            tokens = [t for t in re.split(r"[\s_-]+", rest) if len(t) >= 2]
            scored = []
            for c in agent_chats:
                title_low = (c.get("title") or "").lower()
                score = sum(1 for t in tokens if t in title_low)
                if score > 0:
                    scored.append((score, sort_key_ts(c.get("updated")), c))
            if scored:
                scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
                return scored[0][2]

    # 4. Fallback: ID prefix, tab_name, or title match
    low = clean_alias
    clean_low = low.replace("agent-", "")
    hits = [c for c in chats if c["id"].lower().startswith(low) or c["id"].lower().replace("agent-", "").startswith(clean_low)]
    if not hits:
        hits = [c for c in chats if (c.get("tab_name") or "").lower() == low]
    if not hits:
        hits = [c for c in chats if low in (c.get("title") or "").lower()]
    if not hits:
        # Cross-agent topic slug match
        tokens = [t for t in re.split(r"[\s_-]+", low) if len(t) >= 2]
        scored = []
        for c in chats:
            title_low = (c.get("title") or "").lower()
            score = sum(1 for t in tokens if t in title_low)
            if score > 0:
                scored.append((score, sort_key_ts(c.get("updated")), c))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            hits = [scored[0][2]]
    if not hits:
        raise ValueError(f"no chat matches '{ident}'. Use list_chats or search_chat.")
    if len(hits) > 1:
        hits.sort(key=lambda c: sort_key_ts(c.get("updated")), reverse=True)
        exact = [c for c in hits if c["id"].lower() == low]
        if exact:
            return exact[0]
        listing = "\n".join(f"- {c['source']} {c['id'][:8]} {c.get('title')}" for c in hits[:10])
        raise ValueError(f"'{ident}' is ambiguous; specify more characters:\n{listing}")
    return hits[0]


def detect_self() -> Optional[Dict[str, Any]]:
    """Auto-detect if caller is a running Claude Code terminal session."""
    opens = {}
    for p in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            d = json.load(open(p))
            opens[int(d.get("pid", 0))] = d
        except Exception:
            pass
    pid = os.getppid()
    for _ in range(8):
        if pid in opens:
            return {"source": "claude", "id": opens[pid].get("sessionId"), "tab_name": opens[pid].get("name")}
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
            pid = int(out.stdout.strip() or 0)
        except Exception:
            return None
        if pid <= 1:
            return None
    return None


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _get_last_entry_preview(chat: Dict[str, Any]) -> str:
    """Return a 1-2 sentence snippet of the last user prompt and assistant response."""
    src = chat.get("source")
    cid = chat.get("id")
    try:
        if src == "claude":
            lp = chat.get("last_prompt") or ""
            if lp:
                return f"Last: {one_line(lp, 110)}"
            data = claude_read(chat["path"], "recent", 1)
        elif src == "opencode":
            data = opencode_read(cid)
        else:
            data = agy_read(cid)

        ex = to_exchanges(data)
        if not ex:
            return chat.get("title") or "(no messages)"
        last = ex[-1]
        u = one_line(last.get("user") or "", 65)
        a = one_line(last.get("assistant")[0] if last.get("assistant") else "", 75)
        if u and a:
            return f"User: \"{u}\" -> Agent: \"{a}\""
        elif u:
            return f"User: \"{u}\""
        elif a:
            return f"Agent: \"{a}\""
        return chat.get("title") or ""
    except Exception:
        return chat.get("title") or ""


def t_list_chats(source: str = "all", project: str = "", query: str = "", limit: int = 15,
                 include_subagents: bool = False) -> str:
    chats = all_chats(source, include_subagents)
    aliases = load_aliases()
    id_to_aliases: Dict[str, List[str]] = {}
    for a, cid in aliases.items():
        id_to_aliases.setdefault(cid, []).append(a)

    if project:
        chats = [c for c in chats if project.lower() in (c.get("project") or "").lower()]
    if query:
        q = query.lower()
        chats = [c for c in chats if q in " ".join(str(c.get(k) or "") for k in
                                                   ("title", "first_prompt", "last_prompt", "tab_name")).lower()]
    chats.sort(key=lambda c: (bool(c.get("open")), sort_key_ts(c.get("updated"))), reverse=True)
    chats = chats[: max(1, int(limit))]
    if not chats:
        return "No chats found across Antigravity, Claude Code, or OpenCode."
    me = detect_self()
    lines = ["| line (agent) | station id | alias / open tab | project | last active | last exchange / highlight |",
             "|---|---|---|---|---|---|"]
    for c in chats:
        labels = []
        if c.get("open"):
            labels.append(c.get("tab_name") or "open")
        if c.get("subagent"):
            parent_short = c.get('parent')[:8] if c.get('parent') else '?'
            labels.append(f"↳ child of `{parent_short}`")
        if c["id"] in id_to_aliases:
            labels.extend(f"@{a}" for a in id_to_aliases[c["id"]])
        else:
            # Show readable auto-alias hint if no explicit pin
            slug = slugify_topic(c.get("title") or "")
            prefix = "agy" if c["source"] == "antigravity" else c["source"]
            if slug and slug != "chat":
                labels.append(f"(@{prefix}-{slug})")
        if me and me.get("id") == c["id"]:
            labels.append("(current line)")
        label_str = ", ".join(labels)
        highlight = _get_last_entry_preview(c)
        lines.append(f"| {c['source']} | `{c['id'][:8]}` | {label_str} | {os.path.basename(c.get('project') or '')} | "
                     f"{fmt_ts(c.get('updated'))} | {one_line(highlight, 160).replace('|', '/')} |")
    lines.append("\nTip: Board any chat by ID, prefix, @pinned-alias, dynamic workspace head (e.g. @agy-dziner), or topic slug (e.g. @agy-subway).")
    try:
        _sync_opencode_agent_stubs()
    except Exception:
        pass
    return "\n".join(lines)


def t_search_chat(quote: str, source: str = "all", project: str = "", limit: int = 5,
                  include_subagents: bool = True) -> str:
    quote = (quote or "").strip()
    if not quote:
        return "Please provide a quote or snippet to search for."
    q_low = quote.lower()
    chats = all_chats(source, include_subagents=include_subagents)
    if project:
        chats = [c for c in chats if project.lower() in (c.get("project") or "").lower()]

    matches = []
    for c in chats:
        if c["source"] == "claude":
            turns = claude_read(c["path"], "full", 0)
        elif c["source"] == "opencode":
            turns = opencode_read(c["id"])
        else:
            turns = agy_read(c["id"])

        ex = to_exchanges(turns)
        for i, e in enumerate(ex):
            corpus = " ".join([e["user"]] + e["assistant"] + e["thoughts"])
            if q_low in corpus.lower():
                matches.append({
                    "chat": c,
                    "exchange_index": i,
                    "total_exchanges": len(ex),
                    "ts": e.get("ts"),
                    "sample": one_line(e["user"] or (e["assistant"][0] if e["assistant"] else ""), 120),
                })
                break
        if len(matches) >= limit:
            break

    if not matches:
        return f"No matches found across active agents for: '{quote}'."

    lines = [f"# Subway Context: Search results for '{quote}':"]
    for m in matches:
        c = m["chat"]
        idx = m["exchange_index"]
        rem = m["total_exchanges"] - idx
        sub_tag = f" [Subagent of `{c['parent'][:8]}`]" if c.get("subagent") else ""
        lines.append(
            f"- **{c['source'].upper()}** `{c['id'][:8]}`{sub_tag} ({c.get('title')})\n"
            f"  Matched at Station `[{idx}]` of {m['total_exchanges']} ({fmt_ts(m['ts'])})\n"
            f"  Snippet: \"{m['sample']}\"\n"
            f"  -> Transit forward: `read_chat(chat_id='{c['id'][:8]}', start={idx})` ({rem} stops left)"
        )
    return "\n".join(lines)


def t_tag_chat(alias: str, chat_id: str = "") -> str:
    """Assign a memorable sticky alias to a chat. If chat_id is omitted, tags the current active session."""
    target_id = chat_id
    if not target_id:
        me = detect_self()
        if me:
            target_id = me["id"]
        else:
            # Fallback to the newest active chat in the caller's environment
            target_id = "latest"
    c = resolve(target_id)
    save_alias(alias, c["id"])
    return f"Station alias '@{alias.lower().lstrip('@')}' now points to {c['source'].upper()} `{c['id'][:8]}` ({c.get('title')})."


def t_read_chat(chat_id: str, mode: str = "recent", turns: int = 6, start: int = 0,
                since: str = "", anchor_text: str = "",
                max_chars: int = DEFAULT_MAX_CHARS, include_tools: bool = False,
                include_thoughts: bool = True, source: str = "all", project: str = "") -> str:
    if mode not in ("recent", "outline", "full", "decisions"):
        raise ValueError("mode must be 'recent', 'outline', 'full', or 'decisions'")
    c = resolve(chat_id, source, project)

    if c["source"] == "claude":
        data = claude_read(c["path"], "full" if (start > 0 or anchor_text or since) else mode, int(turns))
    elif c["source"] == "opencode":
        data = opencode_read(c["id"])
    else:
        data = agy_read(c["id"])

    ex = to_exchanges(data)
    start_idx = max(0, int(start))
    anchor_info = None

    if anchor_text:
        a_low = anchor_text.strip().lower()
        found_idx = None
        for i, e in enumerate(ex):
            corpus = " ".join([e["user"]] + e["assistant"] + e["thoughts"])
            if a_low in corpus.lower():
                found_idx = i
                break
        if found_idx is not None:
            start_idx = found_idx
            anchor_info = f"Boarding at quote '{anchor_text}' (station [{found_idx}])"
        else:
            anchor_info = f"Quote '{anchor_text}' not found; using default turn range."

    me = detect_self()
    my_id = me["id"] if me else "caller"
    target_id = c["id"]

    if since:
        cps = get_checkpoints(my_id, target_id)
        if not cps:
            anchor_info = "First transit between these two windows; boarding at station [0]."
            start_idx = 0
        else:
            offset = 1
            m = re.match(r"(?i)bridge\s*-\s*(\d+)", since.strip())
            if m:
                offset = int(m.group(1))
            idx_in_history = max(0, len(cps) - offset)
            last_cp = cps[idx_in_history]
            prev_turn = last_cp.get(target_id, 0)
            start_idx = prev_turn
            anchor_info = f"Transit delta since watermark ({fmt_ts(last_cp.get('ts'))}); boarding at station [{start_idx}]"

    cur_ex_len = len(ex)
    record_checkpoint(my_id, target_id, 0, cur_ex_len)

    return render_chat(c, data, mode, int(turns), start_idx, int(max_chars),
                       include_thoughts, include_tools, anchor_info)


def t_send_note(to: str, text: str, from_chat: str = "", share_chat: str = "") -> str:
    target = resolve(to)
    shared = None
    if share_chat:
        shared = resolve(share_chat)
    sender = from_chat
    if not sender:
        me = detect_self()
        sender = me["id"] if me else "unknown"
    rec = {
        "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "from": sender, "text": text,
        "shared_chat": ({"id": shared["id"], "source": shared["source"], "title": shared.get("title")}
                        if shared else None),
    }
    _ensure_subway_dir()
    inbox_path = os.path.join(SUBWAY_DIR, "inbox", f"{target['id']}.jsonl")
    with open(inbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return (f"Transit dispatch queued for {target['source'].upper()} `{target['id'][:8]}` ({target.get('title')}). "
            f"Delivered when that session calls check_notes.")


def t_check_notes(my_chat_id: str = "", keep: bool = False) -> str:
    me = None
    if my_chat_id:
        me = resolve(my_chat_id)
    else:
        d = detect_self()
        if d:
            me = d
    if not me:
        return ("Specify my_chat_id (Antigravity: Conversation ID; Claude Code / OpenCode: Session ID).")
    inbox_path = os.path.join(SUBWAY_DIR, "inbox", f"{me['id']}.jsonl")
    if not os.path.exists(inbox_path) or os.path.getsize(inbox_path) == 0:
        return f"No new dispatches for station {me['id'][:8]}."
    recs = [json.loads(l) for l in open(inbox_path, encoding="utf-8") if l.strip()]
    out = [f"# {len(recs)} Subway Dispatch(es) for station {me['id'][:8]}",
           "> Messages relayed from other agent windows."]
    for r in recs:
        out.append(f"\n## From {r.get('from', '?')[:8]} at {r.get('ts')}\n{r.get('text', '')}")
        sc = r.get("shared_chat")
        if sc:
            out.append(f"Transit link: {sc['source'].upper()} `{sc['id']}` ({sc.get('title')}). "
                       f"Board it with read_chat(chat_id='{sc['id']}').")
    if not keep:
        with open(os.path.join(SUBWAY_DIR, "inbox", f"{me['id']}.read.jsonl"), "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        open(inbox_path, "w").close()
    return "\n".join(out)


def t_whoami() -> str:
    d = detect_self()
    if d:
        return f"Running under Claude Code line: {d['id']} (tab: {d.get('tab_name') or '?'})."
    return ("Active on host. Antigravity agents: check your Conversation ID in context.")


# --------------------------------------------------------------------------
# Dynamic Binary Discovery
# --------------------------------------------------------------------------

def find_claude_binary() -> Optional[str]:
    """Dynamically locate Claude Code binary across system PATH, user bins, or IDE extension bundles."""
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin and (os.path.exists(env_bin) or os.path.lexists(env_bin)):
        return env_bin
    p = shutil.which("claude")
    if p:
        return p
    candidates = [
        os.path.join(HOME, ".local", "bin", "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        *glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin", "claude")),
        *glob.glob(os.path.join(HOME, ".npm-global", "bin", "claude")),
        *glob.glob(os.path.join(HOME, ".vscode", "extensions", "anthropic.claude-code-*", "resources", "native-binary", "claude")),
        *glob.glob(os.path.join(HOME, ".antigravity-ide", "extensions", "anthropic.claude-code-*", "resources", "native-binary", "claude")),
        os.path.join(HOME, "Library", "Application Support", "Claude", "claude"),
    ]
    for c in candidates:
        if os.path.exists(c) or os.path.lexists(c):
            return c
    return None


def find_opencode_binary() -> Optional[str]:
    """Dynamically locate OpenCode / OpenCode2 binary across PATH, nvm, or local bins."""
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin and (os.path.exists(env_bin) or os.path.lexists(env_bin)):
        return env_bin
    for name in ("opencode", "opencode2"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        *glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin", "opencode*")),
        *glob.glob(os.path.join(HOME, ".local", "bin", "opencode*")),
        "/usr/local/bin/opencode",
        "/usr/local/bin/opencode2",
        "/opt/homebrew/bin/opencode",
        "/opt/homebrew/bin/opencode2",
    ]
    for c in candidates:
        if os.path.exists(c) or os.path.lexists(c):
            return c
    return None


# --------------------------------------------------------------------------
# Feature B: Live Workspace Guard (File Leases & Collision Shield)
# --------------------------------------------------------------------------

def _clean_locks(locks_data: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    active = {}
    for path, info in locks_data.get("locks", {}).items():
        if info.get("expires_at", 0) > now:
            active[path] = info
    locks_data["locks"] = active
    return locks_data


def load_locks() -> Dict[str, Any]:
    _ensure_subway_dir()
    data = _load_json("locks.json")
    if not isinstance(data, dict) or "locks" not in data:
        data = {"locks": {}}
    return _clean_locks(data)


def save_locks(data: Dict[str, Any]) -> None:
    _ensure_subway_dir()
    _save_json("locks.json", data)


def t_claim_file(filepath: str, holder: str = "", duration_seconds: int = 300, purpose: str = "") -> str:
    """Acquire a temporary workspace lease on a file to prevent multi-agent collision."""
    if not filepath:
        return "Error: filepath is required."
    clean_path = os.path.normpath(filepath.strip())
    if not holder:
        me = detect_self()
        holder = me["id"][:8] if me else "unknown-station"

    try:
        duration = max(10, min(int(duration_seconds), 3600))
    except Exception:
        duration = 300

    now = time.time()
    expires_at = now + duration

    data = load_locks()
    locks = data.get("locks", {})
    existing = locks.get(clean_path)

    if existing and existing.get("expires_at", 0) > now:
        prev_holder = existing.get("holder", "another-agent")
        if prev_holder != holder:
            rem = int(existing["expires_at"] - now)
            purp_str = f" for '{existing.get('purpose')}'" if existing.get("purpose") else ""
            return (f"🚨 COLLISION WARNING: '{clean_path}' is already leased to {prev_holder}{purp_str}. "
                    f"Lease expires in {rem}s. Do NOT overwrite or clobber until released or expired.")

    locks[clean_path] = {
        "holder": holder,
        "acquired_at": now,
        "expires_at": expires_at,
        "duration": duration,
        "purpose": purpose or "Active modification",
    }
    data["locks"] = locks
    save_locks(data)
    exp_iso = _dt.datetime.fromtimestamp(expires_at).astimezone().strftime("%H:%M:%S")
    return (f"🔒 LEASE ACQUIRED: '{clean_path}' claimed by {holder} until {exp_iso} ({duration}s). "
            f"Purpose: {purpose or 'Active modification'}.")


def t_release_file(filepath: str, holder: str = "") -> str:
    """Release a workspace file lease."""
    if not filepath:
        return "Error: filepath is required."
    clean_path = os.path.normpath(filepath.strip())
    data = load_locks()
    locks = data.get("locks", {})
    if clean_path not in locks:
        return f"File '{clean_path}' was not currently leased."

    current = locks[clean_path]
    if holder and current.get("holder") and current.get("holder") != holder:
        return f"Warning: '{clean_path}' is held by {current.get('holder')}, not {holder}. Lease not released."

    del locks[clean_path]
    data["locks"] = locks
    save_locks(data)
    return f"🔓 RELEASED: '{clean_path}' is now free for modification by any agent."


def t_list_locks() -> str:
    """List all currently active file leases across all agents."""
    data = load_locks()
    save_locks(data)
    locks = data.get("locks", {})
    if not locks:
        return "No active file leases. All files are currently free for modification."
    now = time.time()
    lines = ["# Active Workspace File Leases (Workspace Guard)", ""]
    for path, info in sorted(locks.items(), key=lambda x: x[1].get("expires_at", 0)):
        rem = max(0, int(info.get("expires_at", 0) - now))
        h = info.get("holder", "unknown")
        purp = info.get("purpose", "")
        lines.append(f"- `🔒 {path}` — held by **{h}** ({rem}s remaining) :: {purp}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Shared Context Tracks (~/.subway-context/tracks/)
# --------------------------------------------------------------------------

def _track_file(track: str) -> str:
    _ensure_subway_dir()
    clean = re.sub(r"[^\w-]", "", track.strip().lower()) or "main"
    return os.path.join(TRACKS_DIR, f"{clean}.jsonl")


def t_join_track(track: str = "main", station_id: str = "") -> str:
    """Subscribe a station to a shared context track and retrieve current track state."""
    clean_track = re.sub(r"[^\w-]", "", track.strip().lower()) or "main"
    if not station_id:
        me = detect_self()
        station_id = me["id"][:8] if me else "unknown"

    sub_file = os.path.join(TRACKS_DIR, f"{clean_track}.subscribers.json")
    subs: List[str] = []
    if os.path.exists(sub_file):
        try:
            with open(sub_file, "r", encoding="utf-8") as f:
                subs = json.load(f)
        except Exception:
            subs = []
    if station_id not in subs:
        subs.append(station_id)
        with open(sub_file, "w", encoding="utf-8") as f:
            json.dump(subs, f, indent=2)

    history = t_read_track(track=clean_track, limit=5)
    return (f"🚉 Joined shared context track '{clean_track}' as station `{station_id}`. "
            f"Active subscribers: {len(subs)}.\n\n{history}")


def t_broadcast_track(message: str, track: str = "main", files: Optional[List[str]] = None,
                      decisions: Optional[List[str]] = None, station_id: str = "") -> str:
    """Broadcast a decision, commitment, or status update to a shared multi-agent track."""
    if not message:
        return "Error: message is required."
    clean_track = re.sub(r"[^\w-]", "", track.strip().lower()) or "main"
    if not station_id:
        me = detect_self()
        station_id = me["id"][:8] if me else "host"

    rec = {
        "id": uuid.uuid4().hex[:8],
        "timestamp": time.time(),
        "iso": _dt.datetime.now().astimezone().strftime("%H:%M:%S"),
        "station_id": station_id,
        "message": message.strip(),
        "files": files or [],
        "decisions": decisions or [],
    }
    tf = _track_file(clean_track)
    with open(tf, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    file_note = f" (files: {', '.join(files)})" if files else ""
    return f"📡 Broadcast posted to track '{clean_track}' by `{station_id}`: {message}{file_note}"


def t_read_track(track: str = "main", limit: int = 15, since_timestamp: float = 0.0) -> str:
    """Read the latest synchronized decisions and status updates from a shared track."""
    clean_track = re.sub(r"[^\w-]", "", track.strip().lower()) or "main"
    tf = _track_file(clean_track)
    if not os.path.exists(tf) or os.path.getsize(tf) == 0:
        return f"Track '{clean_track}' is currently empty. Broadcast decisions with broadcast_track()."

    entries = []
    try:
        with open(tf, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        if e.get("timestamp", 0) >= since_timestamp:
                            entries.append(e)
                    except Exception:
                        continue
    except Exception as exc:
        return f"Error reading track '{clean_track}': {exc}"

    if not entries:
        return f"No entries in track '{clean_track}' since timestamp {since_timestamp}."

    recent = entries[-int(limit):]
    lines = [f"# Shared Context Track: '{clean_track}' ({len(recent)} recent dispatches)", ""]
    for e in recent:
        iso = e.get("iso") or _dt.datetime.fromtimestamp(e.get("timestamp", time.time())).strftime("%H:%M:%S")
        st = e.get("station_id", "unknown")
        msg = e.get("message", "")
        lines.append(f"- **[{iso}] `{st}`**: {msg}")
        fl = e.get("files")
        if fl:
            lines.append(f"  - *Files touched/claimed*: {', '.join(f'`{f}`' for f in fl)}")
        decs = e.get("decisions")
        if decs:
            for d in decs:
                lines.append(f"  - *Decision*: {d}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Automated Worker Spawner & Delegation
# --------------------------------------------------------------------------

def _worker_file(worker_id: str) -> str:
    _ensure_subway_dir()
    return os.path.join(WORKERS_DIR, f"{worker_id}.json")


def _update_worker_status(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Check if the worker process is still alive and update metadata."""
    if meta.get("status") == "running":
        pid = meta.get("pid")
        if pid:
            alive = False
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False

            if not alive:
                meta["status"] = "completed"
                meta["completed_at"] = _dt.datetime.now().astimezone().isoformat()
                log_file = meta.get("log_file")
                if log_file and os.path.exists(log_file):
                    try:
                        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                            tail = "".join(lines[-10:])
                            meta["report"] = tail.strip()
                    except Exception:
                        pass
                try:
                    with open(_worker_file(meta["id"]), "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2)
                except Exception:
                    pass
    return meta


def t_spin_worker(role: str, prompt: str, provider: str = "auto", track: str = "main",
                  project_dir: str = "", timeout_seconds: int = 600) -> str:
    """Spawn an autonomous worker subagent (Claude Code, OpenCode, or Antigravity) and link it to a shared track."""
    if not role or not prompt:
        return "Error: role and prompt are required."
    _ensure_subway_dir()

    cwd = project_dir or os.getcwd()
    if not os.path.isdir(cwd):
        cwd = HOME

    selected_provider = provider.lower()
    bin_path = None

    if selected_provider == "auto":
        opencode_bin = find_opencode_binary()
        claude_bin = find_claude_binary()
        if opencode_bin:
            selected_provider = "opencode"
            bin_path = opencode_bin
        elif claude_bin:
            selected_provider = "claude"
            bin_path = claude_bin
        else:
            selected_provider = "antigravity"
    elif selected_provider in ("claude", "claude-code"):
        selected_provider = "claude"
        bin_path = find_claude_binary()
    elif selected_provider in ("opencode", "opencode2"):
        selected_provider = "opencode"
        bin_path = find_opencode_binary()
    elif selected_provider == "antigravity":
        pass
    else:
        return f"Error: unknown provider '{provider}'. Must be 'auto', 'claude', 'opencode', or 'antigravity'."

    worker_id = f"worker_{uuid.uuid4().hex[:6]}"
    log_file = os.path.join(WORKERS_DIR, f"{worker_id}.log")

    if selected_provider == "antigravity":
        meta = {
            "id": worker_id,
            "role": role,
            "provider": "antigravity",
            "track": track,
            "project_dir": cwd,
            "status": "pending_dispatch",
            "pid": None,
            "created_at": _dt.datetime.now().astimezone().isoformat(),
            "completed_at": None,
            "prompt": prompt,
            "log_file": log_file,
            "report": "Delegation prepared. Call invoke_subagent() to execute inside Antigravity.",
        }
        with open(_worker_file(worker_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        t_broadcast_track(f"Delegated task '{role}' to Antigravity worker `{worker_id}`", track=track, station_id=worker_id)
        return (f"🚀 Antigravity Worker `{worker_id}` initialized for role '{role}'.\n"
                f"Invoke via: invoke_subagent(Role='{role}', Prompt='{prompt}')")

    if not bin_path:
        return f"Error: Could not locate binary for '{selected_provider}'. Ensure it is installed and on PATH."

    if selected_provider == "claude":
        cmd = [bin_path, "--dangerously-skip-permissions", "-p", prompt]
    else:
        cmd = [bin_path, "run", "--auto", "--title", f"Subway: {role}", prompt]

    try:
        out_f = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=out_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid = proc.pid
    except Exception as exc:
        return f"Error spawning worker process: {exc}"

    meta = {
        "id": worker_id,
        "role": role,
        "provider": selected_provider,
        "track": track,
        "project_dir": cwd,
        "status": "running",
        "pid": pid,
        "created_at": _dt.datetime.now().astimezone().isoformat(),
        "completed_at": None,
        "prompt": prompt,
        "log_file": log_file,
        "report": "",
    }
    with open(_worker_file(worker_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    t_broadcast_track(
        f"Spun {selected_provider.upper()} worker `{worker_id}` (PID {pid}) on track '{track}' for role: '{role}'",
        track=track,
        station_id=worker_id,
    )

    return (f"🚀 SPUN WORKER: `{worker_id}` [{selected_provider.upper()}] (PID {pid})\n"
            f"- Role: **{role}**\n"
            f"- Track: **{track}**\n"
            f"- Working Dir: `{cwd}`\n"
            f"- Live Log: `{log_file}`\n"
            f"Check progress anytime with worker_status(worker_id='{worker_id}') or collect_reports().")


def t_worker_status(worker_id: str = "", track: str = "") -> str:
    """Check the execution status and output tail of worker subagents."""
    _ensure_subway_dir()
    files = glob.glob(os.path.join(WORKERS_DIR, "worker_*.json"))
    if not files:
        return "No workers have been spawned yet."

    records = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                meta = json.load(f)
                meta = _update_worker_status(meta)
                records.append(meta)
        except Exception:
            continue

    records.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    if worker_id:
        match = [r for r in records if r.get("id") == worker_id or worker_id in r.get("id", "")]
        if not match:
            return f"Worker `{worker_id}` not found."
        m = match[0]
        status_icon = "🟢 RUNNING" if m.get("status") == "running" else "✅ COMPLETED" if m.get("status") == "completed" else "⚪ " + m.get("status", "unknown")
        out = [
            f"# Worker Detail: `{m['id']}`",
            f"- **Role**: {m.get('role')}",
            f"- **Provider**: {m.get('provider', '').upper()}",
            f"- **Status**: {status_icon}",
            f"- **PID**: {m.get('pid')}",
            f"- **Track**: {m.get('track')}",
            f"- **Directory**: `{m.get('project_dir')}`",
            f"- **Created At**: {m.get('created_at')}",
            f"- **Completed At**: {m.get('completed_at') or 'in progress'}",
            "",
            "## Task Prompt",
            f"> {m.get('prompt')}",
            "",
            "## Output Log Tail",
        ]
        log_f = m.get("log_file")
        if log_f and os.path.exists(log_f):
            try:
                with open(log_f, "r", encoding="utf-8", errors="replace") as lf:
                    lines = lf.readlines()
                    tail = "".join(lines[-25:])
                    out.append(f"```text\n{tail.strip() or '(no output yet)'}\n```")
            except Exception as e:
                out.append(f"*(error reading log: {e})*")
        else:
            out.append("*(log file not found)*")
        return "\n".join(out)

    if track:
        records = [r for r in records if r.get("track") == track]

    lines = ["# Autonomous Subway Workers", ""]
    for r in records[:15]:
        st = "🟢 RUNNING" if r.get("status") == "running" else "✅ DONE" if r.get("status") == "completed" else "⚪ " + r.get("status", "")
        lines.append(f"- `{r.get('id')}` [{r.get('provider', '').upper()}] **{r.get('role')}** :: {st} (PID {r.get('pid')}, track: {r.get('track')})")
    return "\n".join(lines)


def t_collect_reports(track: str = "main", wait: bool = False) -> str:
    """Collect final reports, files modified, and decisions from workers on a track."""
    _ensure_subway_dir()
    if wait:
        start_wait = time.time()
        while time.time() - start_wait < 15:
            files = glob.glob(os.path.join(WORKERS_DIR, "worker_*.json"))
            any_running = False
            for fp in files:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        m = json.load(f)
                        m = _update_worker_status(m)
                        if (track == "all" or m.get("track") == track) and m.get("status") == "running":
                            any_running = True
                except Exception:
                    continue
            if not any_running:
                break
            time.sleep(1.0)

    files = glob.glob(os.path.join(WORKERS_DIR, "worker_*.json"))
    records = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                m = json.load(f)
                m = _update_worker_status(m)
                if track == "all" or m.get("track") == track:
                    records.append(m)
        except Exception:
            continue

    if not records:
        return f"No workers found for track '{track}'."

    records.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    out = [f"# Worker Reports for Track: '{track}'", ""]
    for m in records:
        icon = "🟢" if m.get("status") == "running" else "✅" if m.get("status") == "completed" else "⚠️"
        out.append(f"## {icon} `{m['id']}` — {m.get('role')} ({m.get('provider', '').upper()})")
        out.append(f"- **Status**: {m.get('status')} (PID {m.get('pid')})")
        out.append(f"- **Completed**: {m.get('completed_at') or 'in progress'}")
        log_f = m.get("log_file")
        if log_f and os.path.exists(log_f):
            try:
                with open(log_f, "r", encoding="utf-8", errors="replace") as lf:
                    lines = lf.readlines()
                    tail = "".join(lines[-15:])
                    out.append(f"\n```text\n{tail.strip() or '(no output yet)'}\n```\n")
            except Exception:
                pass
        out.append("---")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Feature C: Live Station Board HUD (Dashboard & HTTP Server)
# --------------------------------------------------------------------------

def get_hud_data(project: str = "") -> Dict[str, Any]:
    """Compile a full snapshot of the Subway transit network for the HUD."""
    _ensure_subway_dir()
    chats_data = t_list_chats(source="all", project=project, limit=12)
    locks = load_locks().get("locks", {})
    worker_files = glob.glob(os.path.join(WORKERS_DIR, "worker_*.json"))
    workers = []
    for fp in worker_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                w = json.load(f)
                w = _update_worker_status(w)
                workers.append(w)
        except Exception:
            continue
    workers.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    tracks_summary = {}
    for tf in glob.glob(os.path.join(TRACKS_DIR, "*.jsonl")):
        tname = os.path.basename(tf).replace(".jsonl", "")
        cnt = 0
        last_entry = None
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        cnt += 1
                        try:
                            last_entry = json.loads(line.strip())
                        except Exception:
                            pass
        except Exception:
            pass
        tracks_summary[tname] = {"count": cnt, "last_entry": last_entry}

    return {
        "version": VERSION,
        "timestamp": time.time(),
        "time_iso": _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "locks": locks,
        "workers": workers[:15],
        "tracks": tracks_summary,
        "raw_stations_text": chats_data,
    }


def t_hud_status(project: str = "") -> str:
    """Display an ASCII departures-board style dashboard of the Subway transit network."""
    data = get_hud_data(project=project)
    now = time.time()
    locks = data.get("locks", {})
    workers = data.get("workers", [])
    tracks = data.get("tracks", {})

    sep = "=" * 78
    subsep = "-" * 78
    lines = [
        sep,
        f"🚇 SUBWAY CONTEXT LIVE HUD — STATION DEPARTURE BOARD   [{data['time_iso']}]",
        sep,
        "[WORKSPACE GUARD — ACTIVE FILE LEASES]",
    ]
    if not locks:
        lines.append("  (No files locked. Workspace collision-free.)")
    else:
        for p, info in locks.items():
            rem = max(0, int(info.get("expires_at", 0) - now))
            h = info.get("holder", "unknown")
            lines.append(f"  🔒 {p:<36} <- Held by {h} ({rem}s left) :: {info.get('purpose', '')}")

    lines.extend([subsep, "[AUTONOMOUS WORKER SWARM]"])
    if not workers:
        lines.append("  (No workers spawned. Run spin_worker() to delegate.)")
    else:
        for w in workers[:6]:
            st = "🟢 RUNNING" if w.get("status") == "running" else "✅ DONE"
            prov = w.get("provider", "").upper()
            lines.append(f"  ⚙️  {w['id']:<14} [{prov:<8}] {st:<10} Track: {w.get('track', 'main'):<6} Role: {w.get('role')}")

    lines.extend([subsep, "[SHARED CONTEXT TRACKS]"])
    if not tracks:
        lines.append("  (No active tracks. Join or broadcast with join_track() / broadcast_track().)")
    else:
        for tname, tinfo in tracks.items():
            last = tinfo.get("last_entry") or {}
            msg = last.get("message", "No dispatches yet")
            st_id = last.get("station_id", "none")
            lines.append(f"  📡 Track '{tname}': {tinfo.get('count', 0)} events. Latest from `{st_id}`: {msg}")

    lines.extend([
        sep,
        "🌐 Live Web HUD running at: http://localhost:7890",
        sep,
    ])
    return "\n".join(lines)


HUD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Subway Context — Live Station Board HUD</title>
<style>
:root {
  --bg: #090d16;
  --surface: #111827;
  --surface-hover: #1e293b;
  --border: #1f293d;
  --text: #f9fafb;
  --muted: #9ca3af;
  --cyan: #06b6d4;
  --emerald: #10b981;
  --amber: #f59e0b;
  --rose: #f43f5e;
  --purple: #8b5cf6;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "JetBrains Mono", monospace;
  background-color: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 24px;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 12px; }
.logo-icon { font-size: 28px; }
.brand h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
.brand-sub { font-size: 13px; color: var(--muted); }
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(16, 185, 129, 0.15);
  border: 1px solid rgba(16, 185, 129, 0.4);
  color: var(--emerald);
  padding: 4px 12px;
  border-radius: 9999px;
  font-size: 12px;
  font-weight: 600;
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--emerald);
  box-shadow: 0 0 8px var(--emerald);
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.metric-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.metric-card .title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); }
.metric-card .val { font-size: 28px; font-weight: 700; margin-top: 4px; color: var(--cyan); }
.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 24px;
}
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  display: flex;
  flex-direction: column;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.panel-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--surface-hover);
  border: 1px solid var(--border);
}
.badge-amber { background: rgba(245, 158, 11, 0.15); color: var(--amber); border-color: rgba(245, 158, 11, 0.3); }
.badge-emerald { background: rgba(16, 185, 129, 0.15); color: var(--emerald); border-color: rgba(16, 185, 129, 0.3); }
.badge-cyan { background: rgba(6, 182, 212, 0.15); color: var(--cyan); border-color: rgba(6, 182, 212, 0.3); }
.table-wrap { overflow-x: auto; width: 100%; }
table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
th { color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--border); font-weight: 500; font-size: 12px; }
td { padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.04); }
tr:hover td { background: var(--surface-hover); }
.btn {
  background: var(--surface-hover);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn:hover { background: var(--cyan); color: #000; border-color: var(--cyan); }
.btn-danger { color: var(--rose); }
.btn-danger:hover { background: var(--rose); color: #fff; border-color: var(--rose); }
.form-row { display: flex; gap: 8px; margin-top: 14px; }
input, select, textarea {
  background: #0d121d;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  font-family: inherit;
}
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--cyan); }
.track-feed { max-height: 280px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.track-item {
  background: #0d121d;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
}
.track-time { color: var(--muted); font-size: 11px; margin-bottom: 4px; display: flex; justify-content: space-between; }
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: none; align-items: center; justify-content: center; z-index: 100;
}
.modal {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; max-width: 700px; width: 90%; max-height: 80vh; overflow-y: auto;
}
pre { background: #080c14; padding: 12px; border-radius: 8px; font-size: 12px; overflow-x: auto; color: #a5f3fc; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo-icon">🚇</div>
    <div>
      <h1>SUBWAY CONTEXT LIVE HUD</h1>
      <div class="brand-sub">Autonomous Multi-Agent Workspace Shield & Station Board</div>
    </div>
  </div>
  <div style="display: flex; gap: 12px; align-items: center;">
    <div class="status-pill"><div class="pulse-dot"></div> STATION ACTIVE</div>
    <button class="btn" onclick="fetchStatus()">Refresh</button>
  </div>
</header>

<div class="metrics-grid">
  <div class="metric-card">
    <div class="title">Active File Leases</div>
    <div class="val" id="metric-locks" style="color: var(--amber);">0</div>
  </div>
  <div class="metric-card">
    <div class="title">Worker Swarms</div>
    <div class="val" id="metric-workers" style="color: var(--purple);">0</div>
  </div>
  <div class="metric-card">
    <div class="title">Shared Tracks</div>
    <div class="val" id="metric-tracks" style="color: var(--cyan);">0</div>
  </div>
  <div class="metric-card">
    <div class="title">Last Heartbeat</div>
    <div class="val" id="metric-time" style="font-size: 16px; color: var(--muted); margin-top: 12px;">--:--:--</div>
  </div>
</div>

<div class="dashboard-grid">
  <!-- Panel 1: Workspace Guard -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">🛡️ Workspace Guard (File Leases)</div>
      <span class="badge badge-amber" id="lock-count-badge">0 active</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>File Path</th>
            <th>Holder</th>
            <th>Expires In</th>
            <th>Purpose</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="locks-body">
          <tr><td colspan="5" style="color: var(--muted); text-align: center;">No active file leases. All files free for modification.</td></tr>
        </tbody>
      </table>
    </div>
    <form id="claim-form" class="form-row" onsubmit="handleClaim(event)">
      <input type="text" id="claim-path" placeholder="File path (e.g. src/index.ts)" style="flex: 2;" required />
      <input type="text" id="claim-holder" placeholder="Holder (@claude-main)" style="flex: 1;" />
      <input type="number" id="claim-duration" placeholder="300s" value="300" style="width: 70px;" />
      <button type="submit" class="btn">Claim</button>
    </form>
  </div>

  <!-- Panel 2: Worker Swarm -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">⚙️ Autonomous Worker Swarm</div>
      <span class="badge badge-cyan" id="worker-count-badge">0 workers</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Worker ID</th>
            <th>Provider</th>
            <th>Status</th>
            <th>Role</th>
            <th>PID</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="workers-body">
          <tr><td colspan="6" style="color: var(--muted); text-align: center;">No worker subagents running.</td></tr>
        </tbody>
      </table>
    </div>
    <form id="spawn-form" class="form-row" onsubmit="handleSpawn(event)">
      <input type="text" id="spawn-role" placeholder="Role (e.g. Test Runner)" style="flex: 1;" required />
      <input type="text" id="spawn-prompt" placeholder="Instruction prompt..." style="flex: 2;" required />
      <select id="spawn-provider">
        <option value="auto">Auto</option>
        <option value="opencode">OpenCode</option>
        <option value="claude">Claude</option>
        <option value="antigravity">Antigravity</option>
      </select>
      <button type="submit" class="btn">Spawn</button>
    </form>
  </div>

  <!-- Panel 3: Shared Decision Tracks -->
  <div class="panel" style="grid-column: 1 / -1;">
    <div class="panel-head">
      <div class="panel-title">📡 Shared Decision Ledger (Track: <span id="current-track-name" style="color: var(--cyan);">main</span>)</div>
      <div style="display: flex; gap: 8px;">
        <button class="btn" onclick="switchTrack('main')">main</button>
        <button class="btn" onclick="switchTrack('pr')">pr</button>
      </div>
    </div>
    <div class="track-feed" id="track-feed">
      <div style="color: var(--muted); text-align: center; padding: 20px;">No broadcast entries on this track.</div>
    </div>
    <form id="broadcast-form" class="form-row" onsubmit="handleBroadcast(event)" style="margin-top: 14px;">
      <input type="text" id="bc-msg" placeholder="Broadcast decision or milestone to all agents..." style="flex: 3;" required />
      <input type="text" id="bc-files" placeholder="Files touched (comma separated)" style="flex: 2;" />
      <button type="submit" class="btn">Broadcast</button>
    </form>
  </div>
</div>

<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
      <h3 id="modal-title" style="font-size: 16px;">Log Output</h3>
      <button class="btn" onclick="closeModal()">Close</button>
    </div>
    <pre id="modal-content">Loading...</pre>
  </div>
</div>

<script>
let currentTrack = "main";

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    renderStatus(data);
  } catch (e) {
    console.error("Status fetch failed", e);
  }
}

function renderStatus(data) {
  document.getElementById("metric-time").innerText = data.time_iso.split(" ")[1] || "--:--:--";
  
  // Render Locks
  const locks = data.locks || {};
  const lockKeys = Object.keys(locks);
  document.getElementById("metric-locks").innerText = lockKeys.length;
  document.getElementById("lock-count-badge").innerText = lockKeys.length + " active";
  
  const locksBody = document.getElementById("locks-body");
  if (lockKeys.length === 0) {
    locksBody.innerHTML = '<tr><td colspan="5" style="color: var(--muted); text-align: center;">No active file leases. All files free.</td></tr>';
  } else {
    const now = Date.now() / 1000;
    locksBody.innerHTML = lockKeys.map(k => {
      const item = locks[k];
      const rem = Math.max(0, Math.floor(item.expires_at - now));
      return `<tr>
        <td><code>🔒 ${k}</code></td>
        <td><span class="badge badge-cyan">${item.holder}</span></td>
        <td><span style="color: ${rem < 30 ? 'var(--rose)' : 'var(--amber)'}; font-weight: 600;">${rem}s</span></td>
        <td style="color: var(--muted);">${item.purpose || ''}</td>
        <td><button class="btn btn-danger" onclick="releaseLock('${k}', '${item.holder}')">Release</button></td>
      </tr>`;
    }).join("");
  }

  // Render Workers
  const workers = data.workers || [];
  document.getElementById("metric-workers").innerText = workers.length;
  document.getElementById("worker-count-badge").innerText = workers.length + " workers";
  
  const workersBody = document.getElementById("workers-body");
  if (workers.length === 0) {
    workersBody.innerHTML = '<tr><td colspan="6" style="color: var(--muted); text-align: center;">No worker subagents spawned.</td></tr>';
  } else {
    workersBody.innerHTML = workers.map(w => {
      const isRunning = w.status === 'running';
      const statusBadge = isRunning
        ? '<span class="badge badge-emerald">RUNNING</span>'
        : '<span class="badge">DONE</span>';
      return `<tr>
        <td><code>${w.id}</code></td>
        <td><span class="badge badge-purple">${(w.provider || '').toUpperCase()}</span></td>
        <td>${statusBadge}</td>
        <td><strong>${w.role}</strong></td>
        <td>${w.pid || '-'}</td>
        <td><button class="btn" onclick="viewWorkerLog('${w.id}')">Log</button></td>
      </tr>`;
    }).join("");
  }

  // Render Tracks count
  const tracks = data.tracks || {};
  document.getElementById("metric-tracks").innerText = Object.keys(tracks).length;
}

async function loadTrackFeed() {
  try {
    const res = await fetch(`/api/track?name=${currentTrack}`);
    const data = await res.json();
    const feed = document.getElementById("track-feed");
    const lines = (data.content || '').split('\\n');
    const items = lines.filter(l => l.startsWith('- '));
    if (items.length === 0) {
      feed.innerHTML = '<div style="color: var(--muted); text-align: center; padding: 20px;">No broadcast entries on this track.</div>';
    } else {
      feed.innerHTML = items.map(it => {
        return `<div class="track-item">${it.replace(/^- /, '')}</div>`;
      }).join("");
    }
  } catch (e) {
    console.error("Track feed load error", e);
  }
}

async function handleClaim(e) {
  e.preventDefault();
  const path = document.getElementById("claim-path").value;
  const holder = document.getElementById("claim-holder").value;
  const duration = parseInt(document.getElementById("claim-duration").value || "300");
  await fetch("/api/claim", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({filepath: path, holder: holder, duration: duration})
  });
  document.getElementById("claim-path").value = "";
  fetchStatus();
}

async function releaseLock(path, holder) {
  await fetch("/api/release", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({filepath: path, holder: holder})
  });
  fetchStatus();
}

async function handleSpawn(e) {
  e.preventDefault();
  const role = document.getElementById("spawn-role").value;
  const prompt = document.getElementById("spawn-prompt").value;
  const provider = document.getElementById("spawn-provider").value;
  await fetch("/api/spawn", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({role: role, prompt: prompt, provider: provider, track: currentTrack})
  });
  document.getElementById("spawn-role").value = "";
  document.getElementById("spawn-prompt").value = "";
  fetchStatus();
}

async function handleBroadcast(e) {
  e.preventDefault();
  const msg = document.getElementById("bc-msg").value;
  const filesRaw = document.getElementById("bc-files").value;
  const files = filesRaw ? filesRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
  await fetch("/api/broadcast", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({message: msg, track: currentTrack, files: files, station_id: "hud-admin"})
  });
  document.getElementById("bc-msg").value = "";
  document.getElementById("bc-files").value = "";
  loadTrackFeed();
}

function switchTrack(trackName) {
  currentTrack = trackName;
  document.getElementById("current-track-name").innerText = trackName;
  loadTrackFeed();
}

async function viewWorkerLog(id) {
  document.getElementById("modal-title").innerText = `Log Tail: ${id}`;
  document.getElementById("modal-content").innerText = "Loading...";
  document.getElementById("modal-overlay").style.display = "flex";
  try {
    const res = await fetch(`/api/worker_log?id=${id}`);
    const data = await res.json();
    document.getElementById("modal-content").innerText = data.log || "(empty log)";
  } catch (e) {
    document.getElementById("modal-content").innerText = `Error: ${e}`;
  }
}

function closeModal() {
  document.getElementById("modal-overlay").style.display = "none";
}

fetchStatus();
loadTrackFeed();
setInterval(fetchStatus, 2000);
setInterval(loadTrackFeed, 3000);
</script>
</body>
</html>
"""


class SubwayHUDHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        pr = urllib.parse.urlparse(self.path)
        path = pr.path
        query = urllib.parse.parse_qs(pr.query)

        if path in ("/", "/index.html"):
            payload = HUD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/status":
            proj = query.get("project", [""])[0]
            data = get_hud_data(project=proj)
            self.send_json(data)
            return

        if path == "/api/worker_log":
            wid = query.get("id", [""])[0]
            log_file = os.path.join(WORKERS_DIR, f"{wid}.log")
            if os.path.exists(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                        tail = "".join(lines[-100:])
                        self.send_json({"id": wid, "log": tail})
                        return
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                    return
            self.send_json({"error": "log not found"}, 404)
            return

        if path == "/api/track":
            tname = query.get("name", ["main"])[0]
            txt = t_read_track(track=tname, limit=30)
            self.send_json({"track": tname, "content": txt})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        pr = urllib.parse.urlparse(self.path)
        path = pr.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/claim":
            res = t_claim_file(
                filepath=payload.get("filepath", ""),
                holder=payload.get("holder", ""),
                duration_seconds=payload.get("duration", 300),
                purpose=payload.get("purpose", ""),
            )
            self.send_json({"result": res})
            return

        if path == "/api/release":
            res = t_release_file(
                filepath=payload.get("filepath", ""),
                holder=payload.get("holder", ""),
            )
            self.send_json({"result": res})
            return

        if path == "/api/broadcast":
            res = t_broadcast_track(
                message=payload.get("message", ""),
                track=payload.get("track", "main"),
                files=payload.get("files", []),
                decisions=payload.get("decisions", []),
                station_id=payload.get("station_id", "web-hud"),
            )
            self.send_json({"result": res})
            return

        if path == "/api/spawn":
            res = t_spin_worker(
                role=payload.get("role", ""),
                prompt=payload.get("prompt", ""),
                provider=payload.get("provider", "auto"),
                track=payload.get("track", "main"),
                project_dir=payload.get("project_dir", ""),
            )
            self.send_json({"result": res})
            return

        self.send_response(404)
        self.end_headers()


_HUD_SERVER_INSTANCE = None

def start_hud_server(port: int = 7890, daemon: bool = True) -> Optional[http.server.HTTPServer]:
    global _HUD_SERVER_INSTANCE
    if _HUD_SERVER_INSTANCE is not None:
        return _HUD_SERVER_INSTANCE
    try:
        httpd = http.server.HTTPServer(("127.0.0.1", port), SubwayHUDHandler)
        _HUD_SERVER_INSTANCE = httpd
        t = threading.Thread(target=httpd.serve_forever, daemon=daemon)
        t.start()
        log(f"Subway Live HUD running at http://127.0.0.1:{port}")
        return httpd
    except Exception as e:
        log(f"HUD server could not bind to port {port}: {e}")
        return None



TOOLS = {
    "list_chats": (t_list_chats, {
        "description": "List all active lines and stations (chats) across Antigravity, Claude Code, and OpenCode. "
                       "Highlights open tabs and sticky @aliases.",
        "inputSchema": {"type": "object", "properties": {
            "source": {"type": "string", "enum": ["all", "antigravity", "claude", "opencode"], "default": "all"},
            "project": {"type": "string", "description": "Filter by project directory, e.g. 'dziner'."},
            "query": {"type": "string", "description": "Filter by text substring in title or prompts."},
            "limit": {"type": "integer", "default": 15},
            "include_subagents": {"type": "boolean", "default": False},
        }},
    }),
    "search_chat": (t_search_chat, {
        "description": "Find which agent window contains an exact phrase, code snippet, or instruction. "
                       "Returns the station ID, exact stop index, and token delta so you can board from there.",
        "inputSchema": {"type": "object", "required": ["quote"], "properties": {
            "quote": {"type": "string", "description": "The phrase, quote, or code snippet you remember."},
            "source": {"type": "string", "enum": ["all", "antigravity", "claude", "opencode"], "default": "all"},
            "project": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
            "include_subagents": {"type": "boolean", "default": True, "description": "Search across spun worker subagents as well."},
        }},
    }),
    "tag_chat": (t_tag_chat, {
        "description": "Assign a sticky station alias to a chat (e.g. tag_chat(alias='claude-main')). "
                       "If chat_id is omitted, it automatically tags the current active session. "
                       "You can then use '@alias' directly without remembering IDs.",
        "inputSchema": {"type": "object", "required": ["alias"], "properties": {
            "alias": {"type": "string", "description": "Memorable nickname, e.g. 'claude-main' or 'terminal-coder'."},
            "chat_id": {"type": "string", "description": "Optional Chat ID or prefix. Omit to tag current chat."},
        }},
    }),
    "read_chat": (t_read_chat, {
        "description": "Board and read a chat context from another agent. "
                       "chat_id can be UUID, prefix, open tab name, or @alias. "
                       "Modes: 'recent' (last N stops), 'decisions' (comprehensive rulings/files only), 'outline', 'full'. "
                       "Use since='last' for incremental delta, or anchor_text='phrase' to board at an exact quote.",
        "inputSchema": {"type": "object", "required": ["chat_id"], "properties": {
            "chat_id": {"type": "string", "description": "Chat ID, prefix, open tab name, or @alias."},
            "mode": {"type": "string", "enum": ["recent", "outline", "full", "decisions"], "default": "recent"},
            "turns": {"type": "integer", "default": 6, "description": "Number of exchanges for recent/outline."},
            "start": {"type": "integer", "default": 0, "description": "First exchange index to board from."},
            "since": {"type": "string", "description": "Delta since previous transit: 'last' or 'bridge -N'."},
            "anchor_text": {"type": "string", "description": "Board from the exchange containing this exact quote."},
            "max_chars": {"type": "integer", "default": DEFAULT_MAX_CHARS},
            "include_tools": {"type": "boolean", "default": False},
            "include_thoughts": {"type": "boolean", "default": True},
            "source": {"type": "string", "enum": ["all", "antigravity", "claude", "opencode"], "default": "all"},
            "project": {"type": "string"},
        }},
    }),
    "send_note": (t_send_note, {
        "description": "Send a transit dispatch to another agent window (seen via check_notes).",
        "inputSchema": {"type": "object", "required": ["to", "text"], "properties": {
            "to": {"type": "string", "description": "Recipient chat ID, prefix, tab name, or @alias."},
            "text": {"type": "string"},
            "from_chat": {"type": "string"},
            "share_chat": {"type": "string"},
        }},
    }),
    "check_notes": (t_check_notes, {
        "description": "Check and clear dispatches left for this agent window.",
        "inputSchema": {"type": "object", "properties": {
            "my_chat_id": {"type": "string"},
            "keep": {"type": "boolean", "default": False},
        }},
    }),
    "whoami": (t_whoami, {
        "description": "Report active station ID and line for this server.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "claim_file": (t_claim_file, {
        "description": "Acquire a temporary workspace lease on a file to prevent collision with other agents.",
        "inputSchema": {"type": "object", "required": ["filepath"], "properties": {
            "filepath": {"type": "string", "description": "Target file path to claim."},
            "holder": {"type": "string", "description": "Station ID or agent alias claiming the file (default: self)."},
            "duration_seconds": {"type": "integer", "default": 300, "description": "Lease lifetime in seconds (10-3600)."},
            "purpose": {"type": "string", "description": "Short explanation of the planned changes."},
        }},
    }),
    "release_file": (t_release_file, {
        "description": "Release a workspace file lease so other agents can edit it.",
        "inputSchema": {"type": "object", "required": ["filepath"], "properties": {
            "filepath": {"type": "string", "description": "File path to release."},
            "holder": {"type": "string", "description": "Station ID or agent alias releasing the file."},
        }},
    }),
    "list_locks": (t_list_locks, {
        "description": "List all active workspace file leases across all agents.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "spin_worker": (t_spin_worker, {
        "description": "Spawn an autonomous worker subagent (Claude Code, OpenCode, or Antigravity) and link it to a shared track.",
        "inputSchema": {"type": "object", "required": ["role", "prompt"], "properties": {
            "role": {"type": "string", "description": "Brief 2-5 word job title for the worker (e.g. 'Unit Tester')."},
            "prompt": {"type": "string", "description": "Clear actionable instruction for the subagent."},
            "provider": {"type": "string", "enum": ["auto", "claude", "opencode", "antigravity"], "default": "auto"},
            "track": {"type": "string", "default": "main", "description": "Shared context track name."},
            "project_dir": {"type": "string", "description": "Working directory (default: current workspace)."},
            "timeout_seconds": {"type": "integer", "default": 600},
        }},
    }),
    "worker_status": (t_worker_status, {
        "description": "Check execution status and live log tails of spawned worker subagents.",
        "inputSchema": {"type": "object", "properties": {
            "worker_id": {"type": "string", "description": "Specific worker ID to inspect, or omit to list all."},
            "track": {"type": "string", "description": "Filter by shared context track name."},
        }},
    }),
    "collect_reports": (t_collect_reports, {
        "description": "Collect completion reports and log tails from workers on a track.",
        "inputSchema": {"type": "object", "properties": {
            "track": {"type": "string", "default": "main", "description": "Track name or 'all'."},
            "wait": {"type": "boolean", "default": False, "description": "Wait up to 15s for running workers to finish."},
        }},
    }),
    "join_track": (t_join_track, {
        "description": "Subscribe this station to a shared multi-agent decision track and receive current track state.",
        "inputSchema": {"type": "object", "properties": {
            "track": {"type": "string", "default": "main", "description": "Track name (e.g. 'main', 'pr')."},
            "station_id": {"type": "string", "description": "Station ID or alias (default: self)."},
        }},
    }),
    "broadcast_track": (t_broadcast_track, {
        "description": "Publish a decision, milestone, or file modification to a shared multi-agent track.",
        "inputSchema": {"type": "object", "required": ["message"], "properties": {
            "message": {"type": "string", "description": "Summary of decision or progress update."},
            "track": {"type": "string", "default": "main"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "List of files touched or claimed."},
            "decisions": {"type": "array", "items": {"type": "string"}, "description": "Architectural rulings or commitments."},
            "station_id": {"type": "string", "description": "Author station ID or alias."},
        }},
    }),
    "read_track": (t_read_track, {
        "description": "Read latest synchronized decisions and status dispatches from a shared context track.",
        "inputSchema": {"type": "object", "properties": {
            "track": {"type": "string", "default": "main"},
            "limit": {"type": "integer", "default": 15},
            "since_timestamp": {"type": "number", "default": 0.0},
        }},
    }),
    "hud_status": (t_hud_status, {
        "description": "Snapshot departure-board view of active lines, file leases, worker swarms, and shared tracks.",
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "Optional project filter."},
        }},
    }),
}


# --------------------------------------------------------------------------
# MCP stdio JSON-RPC loop
# --------------------------------------------------------------------------

def send(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = req.get("method")
    rid = req.get("id")
    if rid is None:
        return None
    if method == "initialize":
        pv = (req.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "subway-context", "version": VERSION},
            "instructions": "Subway Context: Multi-window transit for coding agents. "
                            "Board chats with search_chat, tag_chat, and read_chat(since='last', mode='decisions').",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [dict(name=n, **spec) for n, (_f, spec) in TOOLS.items()]}}
    if method == "tools/call":
        p = req.get("params") or {}
        name = p.get("name")
        args = p.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            text = TOOLS[name][0](**args)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            log(traceback.format_exc())
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}}
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return {"jsonrpc": "2.0", "id": rid, "result": {key: []}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    try:
        _sync_opencode_agent_stubs()
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] != "--stdio":
        arg = sys.argv[1]
        if arg in ("--install", "-i", "install"):
            from install import install_all
            install_all()
            return
        if arg in ("--status", "status"):
            from install import check_status
            check_status()
            return
        if arg in ("--hud", "hud"):
            port = 7890
            if len(sys.argv) > 2:
                try:
                    port = int(sys.argv[2])
                except Exception:
                    pass
            print(f"🚇 Starting Subway Context Live HUD on http://localhost:{port} ...")
            start_hud_server(port=port, daemon=False)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nHUD stopped.")
            return
        if arg in ("--uninstall", "--remove", "uninstall"):
            from install import remove_all
            remove_all()
            return
        name = arg
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(TOOLS[name][0](**args))
        return
    start_hud_server(port=7890, daemon=True)
    log(f"v{VERSION} ready (HUD active on http://127.0.0.1:7890)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            try:
                resp = handle(r)
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": r.get("id"), "error": {"code": -32603, "message": str(e)}}
            if resp is not None:
                send(resp)


if __name__ == "__main__":
    main()
