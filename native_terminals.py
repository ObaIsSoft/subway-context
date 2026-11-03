"""Native macOS terminal integration for Subway Context (Terminal.app and iTerm2).

Pure functions and one subprocess runner for controlling Terminal.app and iTerm2
via AppleScript (osascript).
"""

from typing import List, Optional, Tuple
import subprocess

_TERMINAL_APP_NAME = "Terminal"
_ITERM_APP_NAME = "iTerm2"

_TERMINAL_BUSY_SCRIPT = """on run argv
\tset targetTty to item 1 of argv
\ttell application "Terminal"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\tif tty of t is targetTty then
\t\t\t\t\treturn busy of t
\t\t\t\tend if
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn "not_found"
end run"""

_ITERM_BUSY_SCRIPT = """on run argv
\tset targetTty to item 1 of argv
\ttell application "iTerm2"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\trepeat with s in sessions of t
\t\t\t\t\tif tty of s is targetTty then
\t\t\t\t\t\treturn is processing of s
\t\t\t\t\tend if
\t\t\t\tend repeat
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn "not_found"
end run"""

_TERMINAL_TYPE_SCRIPT = """on run argv
\tset cmd to item 1 of argv
\tset targetTty to item 2 of argv
\ttell application "Terminal"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\tif tty of t is targetTty then
\t\t\t\t\tdo script cmd in t
\t\t\t\t\treturn "ok"
\t\t\t\tend if
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn "not_found"
end run"""

_ITERM_TYPE_SCRIPT = """on run argv
\tset cmd to item 1 of argv
\tset targetTty to item 2 of argv
\ttell application "iTerm2"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\trepeat with s in sessions of t
\t\t\t\t\tif tty of s is targetTty then
\t\t\t\t\t\ttell s to write text cmd
\t\t\t\t\t\treturn "ok"
\t\t\t\t\tend if
\t\t\t\tend repeat
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn "not_found"
end run"""

_TERMINAL_HISTORY_SCRIPT = """on run argv
\tset targetTty to item 1 of argv
\ttell application "Terminal"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\tif tty of t is targetTty then
\t\t\t\t\treturn history of t
\t\t\t\tend if
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn ""
end run"""

_ITERM_HISTORY_SCRIPT = """on run argv
\tset targetTty to item 1 of argv
\ttell application "iTerm2"
\t\trepeat with w in windows
\t\t\trepeat with t in tabs of w
\t\t\t\trepeat with s in sessions of t
\t\t\t\t\tif tty of s is targetTty then
\t\t\t\t\t\treturn contents of s
\t\t\t\t\tend if
\t\t\t\tend repeat
\t\t\tend repeat
\t\tend repeat
\tend tell
\treturn ""
end run"""


def _app_name(app: str) -> str:
    low = (app or "").strip().lower()
    if low in ("terminal", "apple_terminal", "terminal.app"):
        return _TERMINAL_APP_NAME
    if low in ("iterm", "iterm2", "iterm.app"):
        return _ITERM_APP_NAME
    return app


def _is_1743(err: str) -> bool:
    return "-1743" in err or "Not authorized to send Apple events" in err


def _automation_error(app: str) -> str:
    name = _app_name(app)
    return f"macOS blocked Subway from controlling {name}. Allow it in System Settings > Privacy & Security > Automation, then try again."


def _osascript(script: str, args: Optional[List[str]] = None, timeout: int = 5) -> Tuple[bool, str]:
    """Execute an AppleScript via osascript with arguments passed through argv."""
    cmd = ["osascript", "-e", script] + list(args or [])
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res.returncode == 0:
            return True, res.stdout
        err = (res.stderr or res.stdout or "").strip()
        return False, err
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


def is_busy(app: str, tty: str) -> Optional[bool]:
    """Check if the given terminal tab or session is busy running a command."""
    low = (app or "").strip().lower()
    if low in ("terminal", "apple_terminal", "terminal.app"):
        script = _TERMINAL_BUSY_SCRIPT
    elif low in ("iterm", "iterm2", "iterm.app"):
        script = _ITERM_BUSY_SCRIPT
    else:
        return None

    ok, out = _osascript(script, [tty])
    if not ok:
        if _is_1743(out):
            raise PermissionError(_automation_error(app))
        return None
    val = out.strip().lower()
    if val == "true":
        return True
    if val == "false":
        return False
    return None


def type_command(app: str, tty: str, command: str) -> Tuple[bool, str]:
    """Send and execute a command in the target tab/session."""
    low = (app or "").strip().lower()
    if low in ("terminal", "apple_terminal", "terminal.app"):
        script = _TERMINAL_TYPE_SCRIPT
    elif low in ("iterm", "iterm2", "iterm.app"):
        script = _ITERM_TYPE_SCRIPT
    else:
        return False, f"unsupported app '{app}'"

    ok, out = _osascript(script, [command, tty])
    if not ok:
        if _is_1743(out):
            return False, _automation_error(app)
        return False, out.strip() or "failed"
    if out.strip() == "not_found":
        return False, "not found"
    return True, ""


def read_history(app: str, tty: str) -> str:
    """Read the full terminal scrollback/contents of the target tab/session."""
    low = (app or "").strip().lower()
    if low in ("terminal", "apple_terminal", "terminal.app"):
        script = _TERMINAL_HISTORY_SCRIPT
    elif low in ("iterm", "iterm2", "iterm.app"):
        script = _ITERM_HISTORY_SCRIPT
    else:
        return ""

    ok, out = _osascript(script, [tty])
    if not ok:
        if _is_1743(out):
            raise PermissionError(_automation_error(app))
        return ""
    return out


def slice_output(history: str, command: str) -> str:
    """Extract output after the last line ending with command, minus the final prompt line."""
    if not history or not command:
        return ""
    lines = history.splitlines()
    # Terminal.app pads the end with blank lines; the last real line is the new prompt.
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        return ""
    prompt = lines[-1].rstrip()
    body = lines[:-1]
    cmd = command.strip()
    start = -1
    # The command line is the same prompt followed by the command. Matching the prompt keeps an
    # output line that merely ends with the command text ("zsh: command not found: flas") out.
    if prompt:
        for i in range(len(body) - 1, -1, -1):
            if body[i].rstrip() in (prompt + " " + cmd, prompt + cmd):
                start = i
                break
    if start == -1:
        # The prompt changed during the command (cd, a venv): fall back to the last line ending with it.
        for i in range(len(body) - 1, -1, -1):
            if body[i].rstrip().endswith(cmd):
                start = i
                break
    if start == -1:
        return ""
    content = body[start + 1:]
    while content and not content[-1].strip():
        content.pop()
    return "\n".join(content)
