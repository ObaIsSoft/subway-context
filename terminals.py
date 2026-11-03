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
import shlex
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
                        capture_output: Optional[bool] = None,
                        shell_integration: Optional[bool] = None, closed: Optional[bool] = None,
                        source: str = "", app: str = "", tty: str = "") -> Dict[str, Any]:
    """Register or update a terminal's identity and opt-in flags. Missing fields keep their old value."""
    os.makedirs(base_dir, exist_ok=True)
    path = _terminal_path(base_dir, terminal_id)
    rec = _load_json(path)
    rec.setdefault("id", terminal_id)
    rec.setdefault("commands", [])
    rec.setdefault("since", {})
    rec.setdefault("capture_output", False)
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
    if capture_output is not None:
        rec["capture_output"] = bool(capture_output)
    if shell_integration is not None:
        rec["shell_integration"] = bool(shell_integration)
    if closed is not None:
        rec["closed"] = bool(closed)
    if source:
        rec["source"] = source
    if app:
        rec["app"] = app
    if tty:
        rec["tty"] = tty
    rec["updated_at"] = time.time()
    rec.setdefault("created_at", rec["updated_at"])
    _write_json_atomic(path, rec)
    return rec


def record_command(base_dir: str, terminal_id: str, command: str, exit_code: Optional[int] = None,
                   start_ts: Optional[float] = None, end_ts: Optional[float] = None,
                   output: str = "", caller_id: str = "", name: str = "", window_id: str = "",
                   folder: str = "", app: str = "", tty: str = "") -> Dict[str, Any]:
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
    if app:
        rec["app"] = app
    if tty:
        rec["tty"] = tty

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
                "capture_output": bool(rec.get("capture_output")),
                "shell_integration": rec.get("shell_integration"), "closed": bool(rec.get("closed")),
                "source": terminal_source(rec),
                "app": rec.get("app") or "",
                "tty": rec.get("tty") or "",
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


# --- Command classification: safe / normal / protected -------------------------------
# Every part of a command is checked, because "ls; npm publish" must not slip through as
# an "ls" command. Protected commands always ask the user, whatever the terminal mode.
PROTECTED_DEFAULTS = [
    "npm publish", "npm unpublish", "npm deprecate", "npm dist-tag", "npm stage approve",
    "npm stage reject", "npm owner", "npm access", "npm trust", "npm token", "npm login",
    "npm logout", "npm adduser", "gh release", "gh auth", "gh secret", "gh repo delete",
    "gh repo edit", "git push", "git reset --hard", "git clean", "git branch -D",
    "git tag -d", "git filter-branch", "vsce publish", "ovsx publish", "twine upload",
    "docker push", "cargo publish", "rm -r", "rm -R", "find -delete", "find -exec", "dd",
    "mkfs", "diskutil", "chmod -R", "chown -R", "sudo", "su", "doas", "launchctl",
    "crontab", "security", "defaults write", "osascript", "curl", "wget", "ssh", "scp",
    "rsync", "kill", "killall", "pkill", "shutdown", "reboot",
]
PROTECTED_EXCEPTIONS_DEFAULTS = ["npm stage publish"]

# A single-letter entry flag also matches the long form listed here.
LONG_FLAGS = {"-r": "--recursive", "-f": "--force"}

_PROTECTED_PATHS = [
    "~/.ssh", "~/.npmrc", "~/.aws", "~/.config/gh", "~/.subway-context", "~/.zshrc",
    "~/.bashrc", "~/.zprofile", "~/.bash_profile", "~/.gitconfig", "~/Library/Keychains",
]
_SEGMENT_SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;", ";&"}
_PIPES = {"|", "|&"}
_INLINE_CODE_PROGRAMS = {"python", "python3", "node", "perl", "ruby", "osascript"}
_INLINE_CODE_FLAGS = {"-c", "-e", "-E", "-p", "--eval", "--print"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_PROGRAMS = {"sh", "bash", "zsh", "fish", "dash", "ksh"}
_INTERPRETER_PROGRAMS = {"sh", "bash", "zsh", "fish", "python", "python3", "node", "perl", "ruby"}

_PROTECTED_REASONS = {
    "npm publish": "publishes to npm",
    "npm unpublish": "removes a published npm package",
    "npm deprecate": "changes an npm package",
    "npm dist-tag": "changes npm release tags",
    "npm stage approve": "publishes to npm",
    "npm stage reject": "changes an npm release",
    "npm owner": "changes npm package ownership",
    "npm access": "changes npm package access",
    "npm trust": "changes npm package trust",
    "npm token": "changes npm tokens",
    "npm login": "signs in to npm",
    "npm logout": "signs out of npm",
    "npm adduser": "creates an npm account",
    "gh release": "publishes a GitHub release",
    "gh auth": "changes GitHub sign-in",
    "gh secret": "changes GitHub secrets",
    "gh repo delete": "deletes a GitHub repository",
    "gh repo edit": "changes GitHub repository settings",
    "git push": "pushes to a remote",
    "git reset --hard": "discards local changes",
    "git clean": "deletes untracked files",
    "git branch -D": "deletes a branch",
    "git tag -d": "deletes a tag",
    "git filter-branch": "rewrites git history",
    "vsce publish": "publishes to the VS Code marketplace",
    "ovsx publish": "publishes to Open VSX",
    "twine upload": "uploads to PyPI",
    "docker push": "pushes a container image",
    "cargo publish": "publishes to crates.io",
    "rm -r": "removes files recursively",
    "rm -R": "removes files recursively",
    "find -delete": "deletes files",
    "find -exec": "runs commands on files",
    "dd": "writes raw data to a device",
    "mkfs": "formats a disk",
    "diskutil": "changes a disk",
    "chmod -R": "changes permissions recursively",
    "chown -R": "changes ownership recursively",
    "sudo": "runs as another user",
    "su": "switches user",
    "doas": "runs as another user",
    "launchctl": "changes macOS services",
    "crontab": "changes scheduled jobs",
    "security": "reads or changes the keychain",
    "defaults write": "changes macOS settings",
    "osascript": "runs AppleScript",
    "curl": "reaches the network",
    "wget": "reaches the network",
    "ssh": "connects to another machine",
    "scp": "copies to another machine",
    "rsync": "copies files, possibly to another machine",
    "kill": "stops a process",
    "killall": "stops processes",
    "pkill": "stops processes",
    "shutdown": "shuts down the machine",
    "reboot": "reboots the machine",
}


def _match_paren(text: str, start: int) -> Optional[int]:
    """Index of the ')' matching the '(' at start, or None when it never closes."""
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _extract_substitutions(text: str):
    """Pull $(...), `<...>`, <(...) and >(...) bodies out of a command.

    They are replaced by plain placeholder words so the outer command still tokenises, and
    every body is classified recursively by the caller."""
    out: List[str] = []
    inners: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "(":
            end = _match_paren(text, i + 1)
            if end is None:
                out.append(text[i:])
                break
            inners.append(text[i + 2:end])
            out.append(f" __SUBST{len(inners) - 1}__ ")
            i = end + 1
            continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end == -1:
                out.append(text[i:])
                break
            inners.append(text[i + 1:end])
            out.append(f" __SUBST{len(inners) - 1}__ ")
            i = end + 1
            continue
        if ch in "<>" and i + 1 < len(text) and text[i + 1] == "(":
            end = _match_paren(text, i + 1)
            if end is None:
                out.append(text[i:])
                break
            inners.append(text[i + 2:end])
            out.append(f" __SUBST{len(inners) - 1}__ ")
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), inners


def _split_lines_outside_quotes(text: str) -> List[str]:
    lines: List[str] = []
    buf: List[str] = []
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            buf.append(ch)
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "\r\n":
            lines.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    lines.append("".join(buf))
    return lines


def _tokenize(text: str) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _expand_home(value: str) -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    expanded = value.replace("${HOME}", home).replace("$HOME", home)
    if expanded == "~":
        return home
    if expanded.startswith("~/"):
        return home + expanded[1:]
    return expanded


def _touched_protected_path(tokens: List[str]) -> str:
    for token in tokens:
        if not token:
            continue
        # --file=~/.npmrc style values
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if value.startswith("-"):
            continue
        expanded = _expand_home(value)
        if expanded.startswith(os.sep):
            norm = os.path.normpath(expanded)
            for display in _PROTECTED_PATHS:
                root = os.path.normpath(_expand_home(display))
                if norm == root or norm.startswith(root + os.sep):
                    return display
        # Relative paths and globs (cat .npmrc from ~, cat ~/.ss*/id_rsa): match the name itself.
        # Over-matching is fine here: it only means the user is asked.
        for display in _PROTECTED_PATHS:
            marker = display[2:] if display.startswith("~/") else display
            if marker in value or (any(ch in value for ch in "*?[") and _glob_could_match(value, marker)):
                return display
    return ""


def _glob_could_match(pattern: str, marker: str) -> bool:
    """True when a glob in the token could expand to the protected name (e.g. ~/.ss*/id_rsa).

    Every part of a multi-part name has to be matched in order, so `ls *` from ~ doesn't count as
    touching Library/Keychains while `ls ~/Library/*` does."""
    import fnmatch
    names = [n for n in marker.split("/") if n]
    parts = [p for p in pattern.replace("\\", "/").split("/") if p]
    for i in range(len(parts) - len(names) + 1):
        window = parts[i:i + len(names)]
        if not any(any(ch in part for ch in "*?[") for part in window):
            continue
        ok = True
        for name, part in zip(names, window):
            if name.startswith(".") and not part.startswith("."):
                ok = False  # shells don't expand * to dotfiles unless the dot is written
                break
            if not fnmatch.fnmatch(name, part):
                ok = False
                break
        if ok:
            return True
    return False


def _peel(tokens: List[str]):
    """Drop assignments and wrappers (env, command, nohup, time, nice, timeout, xargs, exec).

    Returns (program, args, used_wrapper, bare_env)."""
    toks = list(tokens)
    used_wrapper = False
    while toks:
        while toks and _ASSIGNMENT_RE.match(toks[0]):
            toks.pop(0)
        if not toks:
            break
        name = os.path.basename(toks[0])
        if name == "env":
            used_wrapper = True
            toks.pop(0)
            while toks:
                if _ASSIGNMENT_RE.match(toks[0]):
                    toks.pop(0)
                    continue
                if toks[0].startswith("-"):
                    flag = toks.pop(0)
                    if flag in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and toks:
                        toks.pop(0)
                    continue
                break
            if not toks:
                return "", [], used_wrapper, True
            continue
        if name in ("command", "nohup", "exec", "time"):
            used_wrapper = True
            toks.pop(0)
            while toks and toks[0].startswith("-"):
                toks.pop(0)
            continue
        if name == "nice":
            used_wrapper = True
            toks.pop(0)
            while toks and toks[0].startswith("-"):
                flag = toks.pop(0)
                if flag in ("-n", "--adjustment") and toks:
                    toks.pop(0)
            continue
        if name == "timeout":
            used_wrapper = True
            toks.pop(0)
            while toks and toks[0].startswith("-"):
                flag = toks.pop(0)
                if flag in ("-s", "--signal", "-k", "--kill-after") and toks:
                    toks.pop(0)
            if toks:
                toks.pop(0)  # the duration
            continue
        if name == "xargs":
            used_wrapper = True
            toks.pop(0)
            while toks and toks[0].startswith("-"):
                flag = toks.pop(0)
                if flag in ("-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a") and toks:
                    toks.pop(0)
            continue
        break
    if not toks:
        return "", [], used_wrapper, False
    return os.path.basename(toks[0]), toks[1:], used_wrapper, False


def _flag_matches(flag: str, token: str) -> bool:
    if not token:
        return False
    if flag.startswith("--") or (flag.startswith("-") and len(flag) > 2):
        return token == flag
    if not flag.startswith("-") or len(flag) != 2:
        return token == flag
    if token == LONG_FLAGS.get(flag):
        return True
    return token.startswith("-") and not token.startswith("--") and flag[1] in token[1:]


def _entry_matches(entry_words: List[str], program: str, args: List[str]) -> bool:
    if not entry_words:
        return False
    if os.path.basename(entry_words[0]) != program:
        return False
    position = 0
    for word in entry_words[1:]:
        if word.startswith("-"):
            if not any(_flag_matches(word, token) for token in args):
                return False
            continue
        found = False
        for i in range(position, len(args)):
            if args[i] == word:
                position = i + 1
                found = True
                break
        if not found:
            return False
    return True


def _exception_matches(exception_words: List[str], program: str, args: List[str]) -> bool:
    if not exception_words:
        return False
    if os.path.basename(exception_words[0]) != program:
        return False
    words = exception_words[1:]
    non_flags = [token for token in args if not token.startswith("-")]
    return non_flags[:len(words)] == words


def _match_protected(program: str, args: List[str], protected_list, exceptions) -> str:
    entries = [e for e in (protected_list or []) if isinstance(e, str) and e.strip()]
    exception_words = [e.split() for e in (exceptions or []) if isinstance(e, str) and e.strip()]
    for entry in entries:
        words = entry.split()
        if not _entry_matches(words, program, args):
            continue
        waived = False
        for exc_words in exception_words:
            # An exception never cancels an entry that spells the same command: if the user
            # also listed it as protected, the exception cannot unprotect itself.
            if exc_words == words:
                continue
            if _exception_matches(exc_words, program, args):
                waived = True
                break
        if waived:
            continue
        return entry
    return ""


def _protected_reason(entry: str) -> str:
    normalized = " ".join((entry or "").split())
    if normalized in _PROTECTED_REASONS:
        return _PROTECTED_REASONS[normalized]
    words = normalized.split()
    program = os.path.basename(words[0]) if words else normalized
    if program == "npm":
        return "changes npm state"
    if program in ("gh", "git"):
        return "changes a repository"
    return f"matches a protected command ({normalized})"


def _is_c_flag(token: str) -> bool:
    return token == "-c" or (token.startswith("-") and not token.startswith("--") and "c" in token[1:])


def classify_command(command, safe_list, protected_list, exceptions):
    """Classify every part of a command as "safe", "normal" or "protected".

    Returns (kind, reason). A reason is a short phrase safe to show the user. Protected
    always wins; one unreadable or risky part makes the whole command protected."""
    return _classify_command(command, safe_list, protected_list, exceptions, 0)


def _classify_command(command, safe_list, protected_list, exceptions, depth: int):
    if depth > 6:
        return "protected", "could not read the command clearly"
    if command is None:
        text = ""
    elif isinstance(command, str):
        text = command
    else:
        text = str(command)
    if not text.strip():
        return "normal", "empty command"

    try:
        masked, substitutions = _extract_substitutions(text)
    except Exception:
        return "protected", "could not read the command clearly"

    for inner in substitutions:
        kind, reason = _classify_command(inner, safe_list, protected_list, exceptions, depth + 1)
        if kind == "protected":
            return "protected", reason

    segments: List[Any] = []
    separator = ""
    try:
        for line in _split_lines_outside_quotes(masked):
            current: List[str] = []
            for token in _tokenize(line):
                if token in _SEGMENT_SEPARATORS:
                    if current:
                        segments.append((separator, current))
                        current = []
                    separator = token
                else:
                    current.append(token)
            if current:
                segments.append((separator, current))
            separator = ";"
    except ValueError:
        return "protected", "could not read the command clearly"

    has_redirect = False
    normal_reason = ""
    wrapper_seen = False

    for separator_before, tokens in segments:
        display_path = _touched_protected_path(tokens)
        if display_path:
            return "protected", f"touches {display_path}"
        # Any all-punctuation token with ">" is an output redirect (>, >>, &>, >|, >&).
        if any(token and set(token) <= set("<>&|") and ">" in token for token in tokens):
            has_redirect = True

        program, args, used_wrapper, bare_env = _peel(tokens)
        if bare_env:
            return "protected", "prints environment variables, which can hold secrets"
        if used_wrapper:
            wrapper_seen = True
        if program == "printenv":
            return "protected", "prints environment variables, which can hold secrets"
        if separator_before in _PIPES and program in _INTERPRETER_PROGRAMS:
            return "protected", "pipes into an interpreter"
        # sh < script, bash <<< "npm publish", python3 << EOF: input Subway can't read.
        if program in _INTERPRETER_PROGRAMS and any(
                token and set(token) <= set("<>&|") and "<" in token for token in args):
            return "protected", "feeds commands to an interpreter"
        # python3 -c "...", node -e "...": inline code Subway can't check.
        if program in _INLINE_CODE_PROGRAMS and any(
                token in _INLINE_CODE_FLAGS
                or (token.startswith("-") and not token.startswith("--") and any(ch in token[1:] for ch in "ceE"))
                for token in args):
            return "protected", "runs inline code Subway can't check"

        if program in _SHELL_PROGRAMS:
            index = next((i for i, arg in enumerate(args) if _is_c_flag(arg)), None)
            if index is not None:
                if index + 1 >= len(args):
                    return "protected", "could not read the command clearly"
                kind, reason = _classify_command(args[index + 1], safe_list, protected_list,
                                                 exceptions, depth + 1)
                if kind == "protected":
                    return "protected", reason
                normal_reason = normal_reason or "runs a nested command"
        elif program == "eval":
            if not args:
                return "protected", "could not read the command clearly"
            kind, reason = _classify_command(" ".join(args), safe_list, protected_list,
                                             exceptions, depth + 1)
            if kind == "protected":
                return "protected", reason
            normal_reason = normal_reason or "runs a nested command"

        entry = _match_protected(program, args, protected_list, exceptions)
        if entry:
            return "protected", _protected_reason(entry)
        if separator_before in _PIPES and not normal_reason:
            normal_reason = "joins several commands"

    normal = bool(normal_reason) or has_redirect or bool(substitutions) or wrapper_seen
    if len(segments) > 1:
        normal = True
        normal_reason = normal_reason or "joins several commands"
    if has_redirect and not normal_reason:
        normal_reason = "writes to a file"
    if substitutions and not normal_reason:
        normal_reason = "runs a nested command"
    if wrapper_seen and not normal_reason:
        normal_reason = "wraps another command"
    if normal:
        return "normal", normal_reason or "runs more than one step"
    if len(segments) == 1 and is_safe_command(" ".join(segments[0][1]), safe_list):
        return "safe", ""
    return "normal", "not on the safe list"


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
import sys, os, json, time, urllib.request, urllib.error, subprocess

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

def _app_and_tty(tid):
    tp = os.environ.get("TERM_PROGRAM", "")
    app = "terminal" if tp == "Apple_Terminal" else ("iterm" if tp == "iTerm.app" else "")
    tty = ""
    try:
        parts = str(tid).split("-")
        pid = parts[1] if len(parts) >= 2 and parts[0] == "shell" else ""
        if pid:
            res = subprocess.run(["ps", "-o", "tty=", "-p", pid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1)
            raw = (res.stdout or "").strip()
            if raw and raw != "??" and not raw.startswith("?"):
                tty = raw if raw.startswith("/dev/") else ("/dev/" + raw)
    except Exception:
        tty = ""
    return app, tty

def _optin(port, token, tid, folder):
    name = _term_name(folder)
    app, tty = _app_and_tty(tid)
    _post(port, token, "/api/terminal_opt_in", {
        "terminal_id": tid, "name": name, "window_id": "",
        "folder": folder, "share_enabled": True,
        "shell_integration": True, "source": "hook",
        "app": app, "tty": tty,
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
            app, tty = _app_and_tty(tid)
            body = {
                "terminal_id": tid, "command": command,
                "exit_code": (int(code) if code not in ("", "None") else None),
                "start_ts": start_ts, "end_ts": end_ts, "output": "",
                "caller_id": "shell", "name": name, "window_id": "", "folder": folder,
                "app": app, "tty": tty,
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
  # $EPOCHREALTIME is empty until zsh/datetime is loaded; without it every command took no time.
  zmodload zsh/datetime 2>/dev/null
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
    SUBWAY_CMD_START=${EPOCHREALTIME:-$(date +%s)}
  }
  trap '_subway_preexec' DEBUG
  _subway_precmd() {
    local code=$?
    if [ -n "$SUBWAY_CMD" ]; then
      ( python3 "$HOME/.subway-context/terminal_hook.py" event "$SUBWAY_TERMINAL_ID" "$SUBWAY_CMD" "$code" "$SUBWAY_CMD_START" "${EPOCHREALTIME:-$(date +%s)}" "$PWD" >/dev/null 2>&1 & )
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

