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
import shutil
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
                        shell_integration: Optional[bool] = None, closed: Optional[bool] = None,
                        source: str = "") -> Dict[str, Any]:
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
    if source:
        rec["source"] = source
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


def terminal_source(rec: Dict[str, Any]) -> str:
    """'hook' for shell-hook terminals, 'editor' for extension-backed ones."""
    source = (rec or {}).get("source")
    if source in ("hook", "editor"):
        return source
    return "hook" if str((rec or {}).get("id") or "").startswith("shell-") else "editor"


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
                "source": terminal_source(rec),
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


def _hook_shell_and_rc(shell: str):
    """Returns (shell_name, rc_path) or (None, error_message)."""
    name = (shell or "").strip().lower()
    if not name:
        name = os.path.basename(os.environ.get("SHELL", ""))
    if name not in ("zsh", "bash"):
        return None, f"Unsupported or undetected shell '{name or '?'}': pass shell='zsh' or shell='bash' explicitly."
    rc_path = os.path.join(os.path.expanduser("~"), ".zshrc" if name == "zsh" else ".bashrc")
    return name, rc_path


_HOOK_SCRIPT = '''#!/usr/bin/env python3
import sys, os, json, time, urllib.request, urllib.error

def _hud():
    base = os.path.join(os.path.expanduser("~"), ".subway-context")
    port = 7890
    try:
        with open(os.path.join(base, "hud.json")) as f:
            port = int(json.load(f).get("port") or 7890)
    except Exception:
        pass
    token = ""
    try:
        with open(os.path.join(base, "hud_token")) as f:
            token = f.read().strip()
    except Exception:
        pass
    return port, token

def _is_sharing(port, token):
    if not token:
        return False
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%s/api/terminals" % port,
            headers={"X-Subway-Token": token},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("sharing"))
    except Exception:
        return False

def _post(port, token, path, body):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (port, path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Subway-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            resp.read()
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def _optin(port, token, tid, folder):
    name = _term_name(folder)
    _post(port, token, "/api/terminal_opt_in", {
        "terminal_id": tid, "name": name, "window_id": "",
        "folder": folder, "share_enabled": True,
        "shell_integration": True, "source": "hook",
    })

def _ts(value):
    try:
        return float(value)
    except Exception:
        return None

def _known(port, token, tid):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%s/api/terminals" % port,
            headers={"X-Subway-Token": token},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for row in data.get("terminals") or []:
            if row.get("id") == tid:
                return True
        return False
    except Exception:
        return None

def _term_name(folder):
    shell = os.path.basename(os.environ.get("SHELL", "shell")) or "shell"
    bname = os.path.basename(os.path.normpath(folder)) if folder else ""
    if not bname and folder:
        bname = folder
    return ("%s · %s" % (shell, bname)) if bname else shell

def main():
    try:
        args = sys.argv[1:]
        kind = args[0] if args else ""
        if kind not in ("optin", "event"):
            return
        port, token = _hud()
        if not _is_sharing(port, token):
            return
        if kind == "optin" and len(args) >= 3:
            _optin(port, token, args[1], args[2])
        elif kind == "event" and len(args) >= 6:
            tid, command, code, start, end, folder = args[1:7]
            name = _term_name(folder)
            end_ts = _ts(end)
            if end_ts is None:
                end_ts = time.time()
            start_ts = _ts(start)
            if start_ts is None:
                start_ts = end_ts
            body = {
                "terminal_id": tid, "command": command,
                "exit_code": (int(code) if code not in ("", "None") else None),
                "start_ts": start_ts, "end_ts": end_ts, "output": "",
                "caller_id": "shell", "name": name, "window_id": "", "folder": folder,
            }
            status = _post(port, token, "/api/terminal_event", body)
            if status == 403 and _known(port, token, tid) is False:
                _optin(port, token, tid, folder)
                _post(port, token, "/api/terminal_event", body)
    except Exception:
        pass

if __name__ == "__main__":
    main()
'''

_ZSH_BLOCK = '''if [ -z "$SUBWAY_TERMINAL_ID" ]; then
  export SUBWAY_TERMINAL_ID="shell-$$-$RANDOM"
  ( python3 "$HOME/.subway-context/terminal_hook.py" optin "$SUBWAY_TERMINAL_ID" "$PWD" >/dev/null 2>&1 & )
  _subway_preexec() {
    SUBWAY_CMD_START=$EPOCHREALTIME
    SUBWAY_CMD="$1"
  }
  _subway_precmd() {
    local code=$?
    if [ -n "$SUBWAY_CMD" ]; then
      ( python3 "$HOME/.subway-context/terminal_hook.py" event "$SUBWAY_TERMINAL_ID" "$SUBWAY_CMD" "$code" "$SUBWAY_CMD_START" "$EPOCHREALTIME" "$PWD" >/dev/null 2>&1 & )
      unset SUBWAY_CMD
    fi
    return $code
  }
  preexec_functions+=(_subway_preexec)
  precmd_functions+=(_subway_precmd)
fi'''

_BASH_BLOCK = '''if [ -z "$SUBWAY_TERMINAL_ID" ]; then
  export SUBWAY_TERMINAL_ID="shell-$$-$RANDOM"
  ( python3 "$HOME/.subway-context/terminal_hook.py" optin "$SUBWAY_TERMINAL_ID" "$PWD" >/dev/null 2>&1 & )
  _subway_prev_debug_cmd=""
  if trap -p DEBUG | grep -q .; then
    _subway_prev_debug_cmd=$(trap -p DEBUG | sed -E "s/^trap -- '(.*)' DEBUG$/\\\\1/")
  fi
  _subway_preexec() {
    if [ -n "$_subway_prev_debug_cmd" ]; then eval "$_subway_prev_debug_cmd"; fi
    SUBWAY_CMD="$BASH_COMMAND"
    SUBWAY_CMD_START=$(date +%s.%N)
  }
  trap '_subway_preexec' DEBUG
  _subway_precmd() {
    local code=$?
    if [ -n "$SUBWAY_CMD" ]; then
      ( python3 "$HOME/.subway-context/terminal_hook.py" event "$SUBWAY_TERMINAL_ID" "$SUBWAY_CMD" "$code" "$SUBWAY_CMD_START" "$(date +%s.%N)" "$PWD" >/dev/null 2>&1 & )
      unset SUBWAY_CMD
    fi
    return $code
  }
  PROMPT_COMMAND="_subway_precmd${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
fi'''

_HOOK_BLOCK_RE = re.compile(re.escape(HOOK_BEGIN_MARKER) + r".*?" + re.escape(HOOK_END_MARKER) + r"\n?", re.DOTALL)
_HOOK_SCRIPT_NAME = "terminal_hook.py"


def install_hook(shell: str = "", server_url: str = "", token: str = "") -> str:
    name, rc_path = _hook_shell_and_rc(shell)
    if name is None:
        return rc_path  # it's the error message in this branch
    try:
        if not os.path.exists(rc_path):
            open(rc_path, "a", encoding="utf-8").close()
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"Could not read {rc_path}: {e}"
    backup_path = rc_path + ".subway-bak"
    if not os.path.exists(backup_path):
        try:
            shutil.copy2(rc_path, backup_path)
        except OSError as e:
            return f"Could not back up {rc_path}: {e}"
    content = _HOOK_BLOCK_RE.sub("", content)
    block = _ZSH_BLOCK if name == "zsh" else _BASH_BLOCK
    new_content = content.rstrip("\n") + "\n\n" + HOOK_BEGIN_MARKER + "\n" + block + "\n" + HOOK_END_MARKER + "\n"
    try:
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        subway_dir = os.path.join(os.path.expanduser("~"), ".subway-context")
        os.makedirs(subway_dir, exist_ok=True)
        with open(os.path.join(subway_dir, _HOOK_SCRIPT_NAME), "w", encoding="utf-8") as f:
            f.write(_HOOK_SCRIPT)
    except OSError as e:
        return f"Could not write {rc_path}: {e}"
    return (f"Installed the {name} hook in {rc_path} (backed up the original to {backup_path}). "
            f"Open a new {name} session, or run `source {rc_path}`, for it to take effect. "
            f"It will report to whatever HUD is running on {server_url} at the time each command finishes.")


def uninstall_hook(shell: str = "") -> str:
    name, rc_path = _hook_shell_and_rc(shell)
    if name is None:
        return rc_path
    if not os.path.exists(rc_path):
        return f"No hook found: {rc_path} does not exist."
    try:
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"Could not read {rc_path}: {e}"
    if HOOK_BEGIN_MARKER not in content:
        return f"No subway-context hook installed in {rc_path}."
    new_content = _HOOK_BLOCK_RE.sub("", content)
    try:
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return f"Could not write {rc_path}: {e}"
    script_path = os.path.join(os.path.expanduser("~"), ".subway-context", _HOOK_SCRIPT_NAME)
    with contextlib.suppress(OSError):
        if os.path.exists(script_path):
            os.remove(script_path)
    return f"Uninstalled the {name} hook from {rc_path}."


def hook_status(shell: str = "") -> str:
    name, rc_path = _hook_shell_and_rc(shell)
    if name is None:
        return rc_path
    if not os.path.exists(rc_path):
        return f"Not installed ({rc_path} does not exist)."
    try:
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"Could not read {rc_path}: {e}"
    return f"Installed in {rc_path}." if HOOK_BEGIN_MARKER in content else f"Not installed in {rc_path}."

