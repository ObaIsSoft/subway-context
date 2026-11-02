"""Terminal capture store: commands run in opted-in editor terminals and standalone shells.

Kept apart from server.py (no import of it, no circular import): every function takes what
it needs as an argument (a base directory, caller id, mode, safe-command list). Callers
(server.py) are responsible for locking: wrap writes in `with _state_lock("terminals"):`.
Each terminal's record lives in its own file under base_dir, written through the atomic
write helper here (tempfile in the same folder + os.replace).
"""
import contextlib
import glob
import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)")
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COMMANDS_PER_TERMINAL = 500
RETENTION_SECONDS = 7 * 86400
TERMINAL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")

HOOK_BEGIN_MARKER = "# >>> subway-context terminal hook >>>"
HOOK_END_MARKER = "# <<< subway-context terminal hook <<<"

_SECRET_NAME_RE = re.compile(r"(token|secret|key|password|passwd|pwd|auth|credential|apikey)", re.IGNORECASE)
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")
_BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*)(bearer\s+)?\S+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:token|secret|key|password|passwd|pwd|apikey)[A-Za-z0-9_-]*)"
    r"\s*([=:])\s*(\"[^\"]*\"|'[^']*'|\S+)"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def redact_text(text: str) -> str:
    """Redact secret-looking values: tokens/keys/passwords, Authorization headers, .env KEY=value lines."""
    if not text:
        return text
    out_lines = []
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line.strip())
        if m and _SECRET_NAME_RE.search(m.group(1)):
            line = f"{m.group(1)}=***REDACTED***"
        line = _BEARER_RE.sub(lambda mo: f"{mo.group(1)}***REDACTED***", line)
        line = _KV_SECRET_RE.sub(lambda mo: f"{mo.group(1)}{mo.group(2)}***REDACTED***", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _write_json_atomic(path: str, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _load_json(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def _slug(terminal_id: str) -> str:
    return hashlib.sha256((terminal_id or "").encode("utf-8")).hexdigest()[:24]


def _terminal_path(base_dir: str, terminal_id: str) -> str:
    if not terminal_id or not isinstance(terminal_id, str):
        raise ValueError("terminal_id is required")
    return os.path.join(base_dir, f"{_slug(terminal_id)}.json")


def get_terminal(base_dir: str, terminal_id: str) -> Optional[Dict[str, Any]]:
    rec = _load_json(_terminal_path(base_dir, terminal_id))
    return rec or None


def set_terminal_opt_in(base_dir: str, terminal_id: str, name: str = "", window_id: str = "",
                        folder: str = "", share_enabled: Optional[bool] = None,
                        allow_commands: Optional[bool] = None,
                        shell_integration: Optional[bool] = None, closed: Optional[bool] = None) -> Dict[str, Any]:
    """Register or update a terminal's identity and opt-in flags. Missing fields keep their old value."""
    os.makedirs(base_dir, exist_ok=True)
    path = _terminal_path(base_dir, terminal_id)
    rec = _load_json(path)
    rec.setdefault("id", terminal_id)
    rec.setdefault("commands", [])
    rec.setdefault("since", {})
    if name:
        rec["name"] = name
    if window_id:
        rec["window_id"] = window_id
    if folder:
        rec["folder"] = folder
    if share_enabled is not None:
        rec["share_enabled"] = bool(share_enabled)
    if allow_commands is not None:
        rec["allow_commands"] = bool(allow_commands)
    if shell_integration is not None:
        rec["shell_integration"] = bool(shell_integration)
    if closed is not None:
        rec["closed"] = bool(closed)
    rec["updated_at"] = time.time()
    rec.setdefault("created_at", rec["updated_at"])
    _write_json_atomic(path, rec)
    return rec


def record_command(base_dir: str, terminal_id: str, command: str, exit_code: Optional[int] = None,
                   start_ts: Optional[float] = None, end_ts: Optional[float] = None,
                   output: str = "", caller_id: str = "", name: str = "", window_id: str = "",
                   folder: str = "") -> Dict[str, Any]:
    """Append one finished command to a terminal's history, redacted, capped and pruned."""
    os.makedirs(base_dir, exist_ok=True)
    path = _terminal_path(base_dir, terminal_id)
    rec = _load_json(path)
    rec.setdefault("id", terminal_id)
    rec.setdefault("since", {})
    if name:
        rec["name"] = name
    if window_id:
        rec["window_id"] = window_id
    if folder:
        rec["folder"] = folder

    clean_output = redact_text(strip_ansi(output or ""))
    encoded = clean_output.encode("utf-8", "ignore")
    if len(encoded) > MAX_OUTPUT_BYTES:
        clean_output = "...[truncated]...\n" + encoded[-MAX_OUTPUT_BYTES:].decode("utf-8", "ignore")

    entry = {
        "ts": start_ts if start_ts is not None else time.time(),
        "end_ts": end_ts,
        "command": redact_text(command or ""),
        "exit_code": exit_code,
        "output": clean_output,
        "caller_id": caller_id,
    }
    commands = rec.get("commands")
    if not isinstance(commands, list):
        commands = []
    commands.append(entry)
    cutoff = time.time() - RETENTION_SECONDS
    commands = [c for c in commands if (c.get("ts") or 0) >= cutoff]
    commands = commands[-MAX_COMMANDS_PER_TERMINAL:]
    rec["commands"] = commands
    rec["updated_at"] = time.time()
    rec.setdefault("created_at", rec["updated_at"])
    _write_json_atomic(path, rec)
    return entry


def list_terminals(base_dir: str) -> List[Dict[str, Any]]:
    """Summaries of every known terminal: name, folder, last command, failure count."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(base_dir):
        return out
    for fp in glob.glob(os.path.join(base_dir, "*.json")):
        with contextlib.suppress(OSError, ValueError):
            rec = _load_json(fp)
            if not rec or not rec.get("id"):
                continue
            commands = rec.get("commands") or []
            failures = sum(1 for c in commands if c.get("exit_code") not in (0, None))
            last = commands[-1] if commands else None
            out.append({
                "id": rec.get("id"), "name": rec.get("name") or rec.get("id"),
                "folder": rec.get("folder") or "", "window_id": rec.get("window_id") or "",
                "share_enabled": bool(rec.get("share_enabled")), "allow_commands": bool(rec.get("allow_commands")),
                "shell_integration": rec.get("shell_integration"), "closed": bool(rec.get("closed")),
                "last_command": last.get("command") if last else "",
                "last_exit_code": last.get("exit_code") if last else None,
                "last_ts": (last.get("ts") if last else None) or rec.get("updated_at") or 0,
                "command_count": len(commands), "failures": failures,
            })
    out.sort(key=lambda r: r.get("last_ts") or 0, reverse=True)
    return out


def read_terminal(base_dir: str, terminal_id: str, caller_id: str = "", since: str = "last",
                  mode: str = "commands") -> Dict[str, Any]:
    """Read a terminal's command history. since='last' returns what this caller hasn't seen yet."""
    rec = _load_json(_terminal_path(base_dir, terminal_id))
    if not rec:
        return {"error": f"terminal '{terminal_id}' not found"}
    commands = rec.get("commands") or []
    since_map = rec.get("since") or {}
    if since == "last":
        cutoff = float(since_map.get(caller_id) or 0) if caller_id else 0.0
    else:
        try:
            cutoff = float(since)
        except (TypeError, ValueError):
            cutoff = 0.0
    selected = [c for c in commands if (c.get("ts") or 0) > cutoff]
    if mode == "errors":
        selected = [c for c in selected if c.get("exit_code") not in (0, None)]
    elif mode != "full":
        mode = "commands"
    if caller_id:
        with contextlib.suppress(OSError):
            path = _terminal_path(base_dir, terminal_id)
            fresh = _load_json(path)
            if fresh:
                fresh_since = fresh.get("since") or {}
                fresh_since[caller_id] = commands[-1]["ts"] if commands else cutoff
                fresh["since"] = fresh_since
                _write_json_atomic(path, fresh)
    return {
        "id": rec.get("id"), "name": rec.get("name"), "folder": rec.get("folder"),
        "mode": mode, "commands": selected,
    }


def search_terminal(base_dir: str, query: str, terminal_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """Find commands or output matching a substring, across one terminal or all of them."""
    q = (query or "").lower()
    out: List[Dict[str, Any]] = []
    if not q:
        return out
    if terminal_id:
        paths = [_terminal_path(base_dir, terminal_id)]
    elif os.path.isdir(base_dir):
        paths = glob.glob(os.path.join(base_dir, "*.json"))
    else:
        paths = []
    for fp in paths:
        with contextlib.suppress(OSError, ValueError):
            rec = _load_json(fp)
            if not rec:
                continue
            for c in rec.get("commands") or []:
                if q in (c.get("command") or "").lower() or q in (c.get("output") or "").lower():
                    hit = dict(c)
                    hit["terminal_id"] = rec.get("id")
                    hit["name"] = rec.get("name")
                    out.append(hit)
    out.sort(key=lambda c: c.get("ts") or 0, reverse=True)
    return out[:max(1, limit)]


def is_safe_command(command: str, safe_list: List[str]) -> bool:
    """Same matching rule as the Antigravity auto_safe launch flags: exact match or a prefix
    followed by a space (so 'git status' matches 'git status --short' but not 'git statuses')."""
    cmd = (command or "").strip()
    if not cmd:
        return False
    for entry in safe_list or []:
        entry = (entry or "").strip()
        if not entry:
            continue
        if cmd == entry or cmd.startswith(entry + " "):
            return True
    return False


RUN_ID_RE = re.compile(r"^run_[0-9a-f]{8}$")


def _run_path(base_dir: str, run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"invalid run id '{run_id}'")
    return os.path.join(base_dir, "_runs", f"{run_id}.json")


def create_run_request(base_dir: str, run_id: str, terminal_id: str, command: str,
                       timeout_seconds: int = 120, caller_id: str = "") -> Dict[str, Any]:
    """Queue a command for the extension to execute in an opted-in terminal. The extension polls
    for pending runs and reports the result back through complete_run_request."""
    d = os.path.join(base_dir, "_runs")
    os.makedirs(d, exist_ok=True)
    rec = {"id": run_id, "terminal_id": terminal_id, "command": command,
           "timeout_seconds": timeout_seconds, "caller_id": caller_id,
           "status": "pending", "created_at": time.time()}
    _write_json_atomic(_run_path(base_dir, run_id), rec)
    return rec


def get_run_request(base_dir: str, run_id: str) -> Optional[Dict[str, Any]]:
    rec = _load_json(_run_path(base_dir, run_id))
    return rec or None


def list_pending_runs(base_dir: str, terminal_id: str = "") -> List[Dict[str, Any]]:
    d = os.path.join(base_dir, "_runs")
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(d):
        return out
    for fp in glob.glob(os.path.join(d, "run_*.json")):
        with contextlib.suppress(OSError, ValueError):
            rec = _load_json(fp)
            if rec.get("status") == "pending" and (not terminal_id or rec.get("terminal_id") == terminal_id):
                out.append(rec)
    return sorted(out, key=lambda r: r.get("created_at", 0))


def complete_run_request(base_dir: str, run_id: str, output: str = "", exit_code: Optional[int] = None,
                         error: str = "") -> Optional[Dict[str, Any]]:
    path = _run_path(base_dir, run_id)
    rec = _load_json(path)
    if not rec:
        return None
    rec.update({"status": "done", "output": redact_text(strip_ansi(output or "")),
               "exit_code": exit_code, "error": error, "completed_at": time.time()})
    _write_json_atomic(path, rec)
    return rec


def prune_terminal(base_dir: str, terminal_id: str) -> None:
    """Drop commands older than the retention window and re-cap. Call periodically, not just on write."""
    path = _terminal_path(base_dir, terminal_id)
    rec = _load_json(path)
    if not rec:
        return
    commands = rec.get("commands") or []
    cutoff = time.time() - RETENTION_SECONDS
    pruned = [c for c in commands if (c.get("ts") or 0) >= cutoff][-MAX_COMMANDS_PER_TERMINAL:]
    if pruned != commands:
        rec["commands"] = pruned
        _write_json_atomic(path, rec)
