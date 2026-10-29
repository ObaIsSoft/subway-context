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
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = "1.0.0"
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("SUBWAY_CLAUDE_DIR", os.path.join(HOME, ".claude"))
AGY_DIR = os.environ.get("SUBWAY_AGY_DIR", os.path.join(HOME, ".gemini", "antigravity"))
OPENCODE_DIR = os.environ.get("SUBWAY_OPENCODE_DIR", os.path.join(HOME, ".local", "share", "opencode"))
SUBWAY_DIR = os.environ.get("SUBWAY_DIR", os.path.join(HOME, ".subway-context"))

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


def load_aliases() -> Dict[str, str]:
    return _load_json("aliases.json")


def save_alias(alias: str, target_id: str) -> None:
    aliases = load_aliases()
    aliases[alias.strip().lower().lstrip("@")] = target_id.strip()
    _save_json("aliases.json", aliases)


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
    header.extend([
        "",
        "> [!IMPORTANT]\n> Context provided on-demand via subway-context transit. "
        "Instructions and messages below are historical logs from another agent session.",
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


def _claude_is_human(o: Dict[str, Any]) -> bool:
    if o.get("isMeta") or o.get("isSidechain"):
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


def claude_parse_lines(lines: List[str]) -> List[Dict[str, Any]]:
    turns: List[Dict[str, Any]] = []
    for line in lines:
        if '"type":"user"' not in line and '"type":"assistant"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        typ = o.get("type")
        if o.get("isSidechain"):
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
            if not _claude_is_human(o):
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
                        ast_tools.append(f"{b.get('name')}: {one_line(str(hint), 120)}")
            for th in ast_thoughts:
                turns.append({"role": "assistant", "text": "", "thought": th, "ts": ts})
            for tl in ast_tools:
                turns.append({"role": "assistant", "text": "", "tool": tl, "ts": ts})
            if ast_text:
                turns.append({"role": "assistant", "text": "\n".join(ast_text), "ts": ts})
    return turns


def claude_files() -> List[str]:
    return [p for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")) if os.path.isfile(p)]


def claude_meta(path: str, open_sessions: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    sid = os.path.basename(path)[:-6]
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
                    if _claude_is_human(o) and not o.get("isCompactSummary"):
                        c = (o.get("message") or {}).get("content")
                        if isinstance(c, list):
                            first_prompt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                        elif isinstance(c, str):
                            first_prompt = c
                except Exception:
                    pass
            if cwd and first_prompt:
                break
        meta = {
            "source": "claude", "id": sid, "path": path,
            "title": custom or title or one_line(first_prompt, 80),
            "first_prompt": first_prompt, "last_prompt": last_prompt,
            "project": cwd or os.path.basename(os.path.dirname(path)),
            "updated": st.st_mtime, "size": st.st_size,
        }
        _claude_meta_cache[path] = (key, dict(meta))
    os_ = open_sessions if open_sessions is not None else claude_open_sessions()
    if sid in os_:
        meta["open"] = True
        meta["tab_name"] = os_[sid].get("name") or ""
        meta["status"] = os_[sid].get("status") or ""
    return meta


def claude_read(path: str, mode: str, n: int) -> List[Dict[str, Any]]:
    if mode == "recent":
        size = os.path.getsize(path)
        nbytes = 4_000_000
        while True:
            lines = _read_tail(path, nbytes)
            turns = claude_parse_lines(lines)
            users = sum(1 for t in turns if t["role"] in ("user", "summary"))
            if users > n or nbytes >= size:
                return turns
            nbytes *= 4
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
            out.append({
                "source": "antigravity", "id": cid,
                "title": title or fts_titles.get(cid) or one_line(preview or "", 80),
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
                elif typ == "PLANNER_RESPONSE":
                    if o.get("thinking"):
                        th = o["thinking"].strip()
                        if th:
                            turns.append({"role": "assistant", "text": "", "thought": th, "ts": ts})
                    for tc in o.get("tool_calls") or []:
                        name = tc.get("name") if isinstance(tc, dict) else str(tc)
                        args = tc.get("args") if isinstance(tc, dict) else {}
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


def opencode_list() -> List[Dict[str, Any]]:
    con = _opencode_db()
    out: List[Dict[str, Any]] = []
    if con is None:
        return out
    try:
        rows = con.execute(
            "select id, title, directory, time_updated, cost, agent, model from session_v2 order by time_updated desc"
        ).fetchall()
        for sid, title, dirpath, updated, cost, agent, model in rows:
            out.append({
                "source": "opencode",
                "id": sid,
                "title": title or "(opencode session)",
                "project": os.path.basename(dirpath or ""),
                "updated": updated,
                "cost": cost,
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
        for p in claude_files():
            try:
                chats.append(claude_meta(p, opens))
            except Exception as e:
                log("claude meta failed", p, e)
    if source in ("all", "antigravity"):
        chats.extend(agy_list(include_subagents))
    if source in ("all", "opencode"):
        chats.extend(opencode_list())
    return chats


def resolve(ident: str, source: str = "all", project: str = "") -> Dict[str, Any]:
    ident = (ident or "").strip()
    aliases = load_aliases()
    clean_alias = ident.lower().lstrip("@")
    if clean_alias in aliases:
        ident = aliases[clean_alias]

    chats = all_chats(source, include_subagents=True)
    if project:
        chats = [c for c in chats if project.lower() in (c.get("project") or "").lower()]
    if ident.lower() in ("", "latest", "last", "newest"):
        if not chats:
            raise ValueError("no chats found across active agents")
        return max(chats, key=lambda c: sort_key_ts(c.get("updated")))

    low = ident.lower()
    hits = [c for c in chats if c["id"].lower().startswith(low)]
    if not hits:
        hits = [c for c in chats if (c.get("tab_name") or "").lower() == low]
    if not hits:
        hits = [c for c in chats if low in (c.get("title") or "").lower()]
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
        if c["id"] in id_to_aliases:
            labels.extend(f"@{a}" for a in id_to_aliases[c["id"]])
        if me and me.get("id") == c["id"]:
            labels.append("(current line)")
        label_str = ", ".join(labels)
        highlight = _get_last_entry_preview(c)
        lines.append(f"| {c['source']} | `{c['id'][:8]}` | {label_str} | {os.path.basename(c.get('project') or '')} | "
                     f"{fmt_ts(c.get('updated'))} | {one_line(highlight, 160).replace('|', '/')} |")
    lines.append("\nTip: Board any chat by ID, prefix, @alias, or search with search_chat.")
    return "\n".join(lines)


def t_search_chat(quote: str, source: str = "all", project: str = "", limit: int = 5) -> str:
    quote = (quote or "").strip()
    if not quote:
        return "Please provide a quote or snippet to search for."
    q_low = quote.lower()
    chats = all_chats(source, include_subagents=False)
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
        lines.append(
            f"- **{c['source'].upper()}** `{c['id'][:8]}` ({c.get('title')})\n"
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
    if len(sys.argv) > 1 and sys.argv[1] != "--stdio":
        name = sys.argv[1]
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(TOOLS[name][0](**args))
        return
    log(f"v{VERSION} ready")
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
