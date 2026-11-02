#!/usr/bin/env python3
"""Subway Context — Universal Interactive Installer & Connector.

Connects Subway Context across coding agent environments with full user control:
- Auto-detects installed coding agents.
- Shows disclaimer and detected paths before making any changes.
- Supports installing on ALL detected agents or SELECTING specific ones (e.g. just 2 of 3).
- Supports granular removal/disconnect per agent.

Usage:
    python3 install.py                         # Interactive install (select all or specific)
    python3 install.py --targets claude,gemini # Install only to Claude Code and Antigravity
    python3 install.py --skip opencode         # Install to all except OpenCode
    python3 install.py --yes                   # Non-interactive: connect all detected
    python3 install.py --status                # Show connection status per agent
    python3 install.py --remove opencode       # Disconnect Subway from only OpenCode
    python3 install.py --remove all            # Disconnect Subway from all agents
"""

import json
import os
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    dirname = os.path.dirname(os.path.abspath(path))
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    if os.path.exists(path):
        bak_path = path + ".subway-bak"
        shutil.copy2(path, bak_path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
    else:
        mode = 0o600

    fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix=".subway_tmp_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Root of JSON in {path} must be an object, got {type(data).__name__}")
        return data
    except Exception as err:
        if isinstance(err, ValueError):
            raise
        raise ValueError(f"Could not read or parse {path}: {err}") from err


def _safe_write_marked_file(path: str, content: str, marker: str = "<!-- subway-managed -->") -> bool:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            if marker not in existing:
                print(f"  Note: Skipping {path} (missing '{marker}' marker)")
                return False
        except Exception as e:
            print(f"  Note: Skipping {path} (could not read: {e})")
            return False

    content = content.rstrip() + "\n" + marker + "\n"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"  Note: Failed to write {path}: {e}")
        return False


def get_default_paths() -> Tuple[str, str]:
    server_dir = os.path.dirname(os.path.abspath(__file__))
    server_py = os.path.join(server_dir, "server.py")
    py_bin = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return server_py, py_bin


def server_command(server_py: str, py_bin: str) -> List[str]:
    if "/_npx/" in server_py.replace("\\", "/"):
        npx = "npx.cmd" if sys.platform == "win32" else "npx"
        return [npx, "-y", "subway-context"]
    return [py_bin, server_py]


def detect_environments() -> Dict[str, Dict[str, Any]]:
    """Probes system to see which supported coding agent environments exist."""
    c_config = os.path.expanduser("~/.claude.json")
    c_bin = shutil.which("claude")
    c_projects = os.path.expanduser("~/.claude/projects")
    claude_detected = False
    claude_evidence = ""
    if c_bin:
        claude_detected = True
        claude_evidence = f"claude binary on PATH ({c_bin})"
    elif os.path.exists(c_projects):
        claude_detected = True
        claude_evidence = f"projects folder exists ({c_projects})"
    else:
        claude_evidence = "neither claude binary on PATH nor ~/.claude/projects found"

    a_config = os.path.expanduser("~/.gemini/config/mcp_config.json")
    agy_bin = os.path.expanduser("~/.gemini/bin/agy")
    agy_which = shutil.which("agy")
    agy_app = "/Applications/Antigravity IDE.app"
    agy_brain = os.path.expanduser("~/.gemini/antigravity/brain")
    agy_detected = False
    agy_evidence = ""
    if os.path.exists(agy_bin):
        agy_detected = True
        agy_evidence = f"binary exists ({agy_bin})"
    elif agy_which:
        agy_detected = True
        agy_evidence = f"agy on PATH ({agy_which})"
    elif os.path.exists(agy_app):
        agy_detected = True
        agy_evidence = f"application exists ({agy_app})"
    elif os.path.exists(agy_brain):
        agy_detected = True
        agy_evidence = f"brain directory exists ({agy_brain})"
    else:
        agy_evidence = "none of ~/.gemini/bin/agy, agy on PATH, /Applications/Antigravity IDE.app, ~/.gemini/antigravity/brain found"

    o_config = os.path.expanduser("~/.config/opencode/opencode.json")
    o_bin2 = shutil.which("opencode2")
    o_bin = shutil.which("opencode")
    o_db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    opencode_detected = False
    opencode_evidence = ""
    if o_bin2:
        opencode_detected = True
        opencode_evidence = f"opencode2 on PATH ({o_bin2})"
    elif o_bin:
        opencode_detected = True
        opencode_evidence = f"opencode on PATH ({o_bin})"
    elif os.path.exists(o_db):
        opencode_detected = True
        opencode_evidence = f"database exists ({o_db})"
    else:
        opencode_evidence = "neither opencode2/opencode on PATH nor ~/.local/share/opencode/opencode.db found"

    envs = {
        "claude": {
            "name": "Claude Code",
            "detected": claude_detected,
            "evidence": claude_evidence,
            "config": c_config,
        },
        "antigravity": {
            "name": "Antigravity (Gemini)",
            "detected": agy_detected,
            "evidence": agy_evidence,
            "config": a_config,
        },
        "opencode": {
            "name": "OpenCode (Terminal)",
            "detected": opencode_detected,
            "evidence": opencode_evidence,
            "config": o_config,
        },
    }

    for v in envs.values():
        status = "Detected" if v["detected"] else "Not found"
        print(f"  {v['name']}: {status} — evidence: {v['evidence']}")

    return envs


def install_claude(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.claude.json")
    try:
        data = _load_json(config_path)
    except ValueError as err:
        reason = str(err)
        print(f"  Error reading {config_path}: {reason}")
        return False, reason

    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        settings_data = _load_json(settings_path)
    except ValueError as err:
        reason = str(err)
        print(f"  Error reading {settings_path}: {reason}")
        return False, reason

    base_cmd = server_command(server_py, py_bin)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": base_cmd[0],
        "args": base_cmd[1:]
    }
    _atomic_write_json(config_path, data)

    # Hook in ~/.claude/settings.json
    hooks = settings_data.setdefault("hooks", {})
    if isinstance(hooks, dict):
        if "/_npx/" in server_py.replace("\\", "/"):
            npx = "npx.cmd" if sys.platform == "win32" else "npx"
            hook_cmd = f"{shlex.quote(npx)} -y subway-context hook-guard"
            hook_prompt_cmd = f"{shlex.quote(npx)} -y subway-context hook-prompt"
            hook_precompact_cmd = f"{shlex.quote(npx)} -y subway-context hook-precompact"
        else:
            hook_cmd = f"{shlex.quote(py_bin)} {shlex.quote(server_py)} hook-guard"
            hook_prompt_cmd = f"{shlex.quote(py_bin)} {shlex.quote(server_py)} hook-prompt"
            hook_precompact_cmd = f"{shlex.quote(py_bin)} {shlex.quote(server_py)} hook-precompact"

        # Remove existing subway hooks and drop entries left empty
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            cleaned = []
            for entry in entries:
                if isinstance(entry, dict) and "hooks" in entry and isinstance(entry["hooks"], list):
                    remaining_hooks = [
                        h for h in entry["hooks"]
                        if isinstance(h, dict) and not (
                            "hook-guard" in h.get("command", "") or "hook-prompt" in h.get("command", "") or "hook-precompact" in h.get("command", "")
                        )
                    ]
                    if remaining_hooks:
                        entry_copy = dict(entry)
                        entry_copy["hooks"] = remaining_hooks
                        cleaned.append(entry_copy)
                elif isinstance(entry, dict):
                    cmd = entry.get("command", "")
                    if not ("hook-guard" in cmd or "hook-prompt" in cmd or "hook-precompact" in cmd):
                        cleaned.append(entry)
                else:
                    cleaned.append(entry)
            hooks[event] = cleaned

        pre_tool = hooks.setdefault("PreToolUse", [])
        if isinstance(pre_tool, list):
            pre_tool.append({
                "matcher": "*",
                "hooks": [{"type": "command", "command": hook_cmd}],
            })

        user_prompt = hooks.setdefault("UserPromptSubmit", [])
        if isinstance(user_prompt, list):
            user_prompt.append({
                "hooks": [{"type": "command", "command": hook_prompt_cmd}],
            })

        pre_compact = hooks.setdefault("PreCompact", [])
        if isinstance(pre_compact, list):
            pre_compact.append({
                "hooks": [{"type": "command", "command": hook_precompact_cmd}],
            })

        _atomic_write_json(settings_path, settings_data)

    # Keyboard / slash command listeners (~/.claude/commands/)
    cmds_dir = os.path.expanduser("~/.claude/commands")
    _safe_write_marked_file(
        os.path.join(cmds_dir, "opencode.md"),
        "Read the latest decisions and files modified by OpenCode terminal agent via Subway Context:\n"
        "Call MCP tool `read_chat(chat_id=\"@opencode\", mode=\"decisions\", since=\"last\")` and summarize the results.\n"
    )
    _safe_write_marked_file(
        os.path.join(cmds_dir, "gemini.md"),
        "Read the latest decisions and files modified by the Antigravity/Gemini session via Subway Context:\n"
        "Call MCP tool `read_chat(chat_id=\"@agy-latest\", mode=\"decisions\", since=\"last\")` and summarize the results.\n"
    )
    _safe_write_marked_file(
        os.path.join(cmds_dir, "subway.md"),
        "List all active agent stations and highlights across Claude Code, Antigravity, and OpenCode:\n"
        "Call MCP tool `list_chats()` and present the active lines.\n"
    )

    return True, f"Connected in {config_path} & commands synced"


def install_antigravity(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    try:
        data = _load_json(config_path)
    except ValueError as err:
        reason = str(err)
        print(f"  Error reading {config_path}: {reason}")
        return False, reason

    base_cmd = server_command(server_py, py_bin)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": base_cmd[0],
        "args": base_cmd[1:]
    }
    _atomic_write_json(config_path, data)

    # Antigravity visual @ skills (~/.gemini/config/skills/)
    skills_base = os.path.expanduser("~/.gemini/config/skills")
    for s_name, s_desc, s_call in (
        ("opencode", "Subway Context connector to OpenCode terminal agent.", 'read_chat(chat_id="@opencode", mode="decisions", since="last")'),
        ("claude", "Subway Context connector to Claude Code.", 'read_chat(chat_id="@claude-latest", mode="decisions", since="last")'),
        ("subway", "Subway Context multi-window agent transit system.", "list_chats()"),
    ):
        s_file = os.path.join(skills_base, s_name, "SKILL.md")
        s_content = (
            f"---\nname: {s_name}\ndescription: {s_desc}\n---\n\n"
            f"# {s_name.capitalize()} Transit Connector\n\n"
            f"When mentioned or invoked with `@{s_name}`, call Subway Context MCP tool `{s_call}`.\n"
        )
        _safe_write_marked_file(s_file, s_content)

    return True, f"Connected in {config_path} & @ skills synced"


def install_opencode(server_py: str, py_bin: str) -> Tuple[bool, str]:
    jsonc_path = os.path.expanduser("~/.config/opencode/opencode.jsonc")
    if os.path.exists(jsonc_path):
        print(f"  Note: {jsonc_path} exists; leaving it unmodified.")

    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    try:
        data = _load_json(config_path)
    except ValueError as err:
        reason = str(err)
        print(f"  Error reading {config_path}: {reason}")
        return False, reason

    base_cmd = server_command(server_py, py_bin)
    mcp_obj = data.setdefault("mcp", {})
    servers = mcp_obj.setdefault("servers", {})
    servers["subway-context"] = {
        "type": "local",
        "command": base_cmd
    }
    _atomic_write_json(config_path, data)

    # Sync initial agent stubs
    try:
        sys.path.insert(0, os.path.dirname(server_py))
        from server import _sync_opencode_agent_stubs
        _sync_opencode_agent_stubs()
    except Exception:
        pass

    return True, f"Connected in {config_path} & stubs synced"


def install_all() -> int:
    server_py, py_bin = get_default_paths()
    envs = detect_environments()
    detected_keys = [k for k, v in envs.items() if v["detected"]]
    installers = {
        "claude": install_claude,
        "antigravity": install_antigravity,
        "opencode": install_opencode,
    }
    all_ok = True
    for k in detected_keys:
        installer = installers.get(k)
        if installer:
            success, _ = installer(server_py, py_bin)
            if not success:
                all_ok = False
    return 0 if all_ok else 1


def remove_all() -> bool:
    return remove_target("all")


def remove_target(target: str) -> bool:
    target = target.lower()
    removed = False

    if target in ("claude", "all"):
        c_path = os.path.expanduser("~/.claude.json")
        if os.path.exists(c_path):
            try:
                d = _load_json(c_path)
                if "mcpServers" in d and "subway-context" in d["mcpServers"]:
                    del d["mcpServers"]["subway-context"]
                    _atomic_write_json(c_path, d)
                    print("  ✓ Claude Code: Disconnected from ~/.claude.json")
                    removed = True
            except Exception as e:
                print(f"  Error updating {c_path}: {e}")

        s_path = os.path.expanduser("~/.claude/settings.json")
        if os.path.exists(s_path):
            try:
                s_data = _load_json(s_path)
                hooks = s_data.get("hooks", {})
                pre_tool = hooks.get("PreToolUse", [])
                if isinstance(pre_tool, list):
                    new_pre = []
                    hook_found = False
                    for entry in pre_tool:
                        if isinstance(entry, dict) and entry.get("matcher") in ("Edit|Write|MultiEdit|NotebookEdit", "*"):
                            sub_hooks = entry.get("hooks", [])
                            filtered = [h for h in sub_hooks if isinstance(h, dict) and "hook-guard" not in h.get("command", "")]
                            if len(filtered) != len(sub_hooks):
                                hook_found = True
                            if filtered:
                                entry["hooks"] = filtered
                                new_pre.append(entry)
                            else:
                                hook_found = True
                        else:
                            new_pre.append(entry)
                    if hook_found:
                        hooks["PreToolUse"] = new_pre
                        _atomic_write_json(s_path, s_data)
                        print("  ✓ Claude Code: Removed Subway hook from ~/.claude/settings.json")
                        removed = True

                user_prompt = hooks.get("UserPromptSubmit", [])
                if isinstance(user_prompt, list):
                    new_up = []
                    up_found = False
                    for entry in user_prompt:
                        if isinstance(entry, dict):
                            sub_hooks = entry.get("hooks", [])
                            filtered = [h for h in sub_hooks if isinstance(h, dict) and "hook-prompt" not in h.get("command", "")]
                            if len(filtered) != len(sub_hooks):
                                up_found = True
                            if filtered:
                                entry["hooks"] = filtered
                                new_up.append(entry)
                            else:
                                up_found = True
                        else:
                            new_up.append(entry)
                    if up_found:
                        hooks["UserPromptSubmit"] = new_up
                        _atomic_write_json(s_path, s_data)
                        removed = True

                pre_compact = hooks.get("PreCompact", [])
                if isinstance(pre_compact, list):
                    new_pc = []
                    pc_found = False
                    for entry in pre_compact:
                        if isinstance(entry, dict):
                            sub_hooks = entry.get("hooks", [])
                            filtered = [h for h in sub_hooks if isinstance(h, dict) and "hook-precompact" not in h.get("command", "")]
                            if len(filtered) != len(sub_hooks):
                                pc_found = True
                            if filtered:
                                entry["hooks"] = filtered
                                new_pc.append(entry)
                            else:
                                pc_found = True
                        else:
                            new_pc.append(entry)
                    if pc_found:
                        hooks["PreCompact"] = new_pc
                        _atomic_write_json(s_path, s_data)
                        removed = True
            except Exception as e:
                print(f"  Error updating {s_path}: {e}")

        c_cmds = os.path.expanduser("~/.claude/commands")
        if os.path.isdir(c_cmds):
            for fname in os.listdir(c_cmds):
                fpath = os.path.join(c_cmds, fname)
                if os.path.isfile(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        if "<!-- subway-managed -->" in content:
                            os.remove(fpath)
                            print(f"  ✓ Claude Code: Removed managed file {fpath}")
                            removed = True
                    except Exception:
                        pass

    if target in ("antigravity", "gemini", "agy", "all"):
        a_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
        if os.path.exists(a_path):
            try:
                d = _load_json(a_path)
                if "mcpServers" in d and "subway-context" in d["mcpServers"]:
                    del d["mcpServers"]["subway-context"]
                    _atomic_write_json(a_path, d)
                    print("  ✓ Antigravity: Disconnected from ~/.gemini/config/mcp_config.json")
                    removed = True
            except Exception as e:
                print(f"  Error updating {a_path}: {e}")

        skills_base = os.path.expanduser("~/.gemini/config/skills")
        if os.path.isdir(skills_base):
            for root, _, files in os.walk(skills_base, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        if "<!-- subway-managed -->" in content:
                            os.remove(fpath)
                            print(f"  ✓ Antigravity: Removed managed file {fpath}")
                            removed = True
                    except Exception:
                        pass
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                except OSError:
                    pass

    if target in ("opencode", "opencode2", "all"):
        o_path = os.path.expanduser("~/.config/opencode/opencode.json")
        if os.path.exists(o_path):
            try:
                d = _load_json(o_path)
                if "mcp" in d and "servers" in d["mcp"] and "subway-context" in d["mcp"]["servers"]:
                    del d["mcp"]["servers"]["subway-context"]
                    _atomic_write_json(o_path, d)
                    print("  ✓ OpenCode: Disconnected from ~/.config/opencode/opencode.json")
                    removed = True
            except Exception as e:
                print(f"  Error updating {o_path}: {e}")

        agents_dir = os.path.expanduser("~/.config/opencode/agents")
        if os.path.isdir(agents_dir):
            for fname in os.listdir(agents_dir):
                fpath = os.path.join(agents_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        if "<!-- subway-auto-stub -->" in content:
                            os.remove(fpath)
                            print(f"  ✓ OpenCode: Removed stub {fpath}")
                            removed = True
                    except Exception:
                        pass

    return removed


def _check_command_exists(full_cmd: List[str]) -> bool:
    if not full_cmd:
        return False
    exe = full_cmd[0]
    if not (os.path.exists(exe) or shutil.which(exe)):
        return False
    if len(full_cmd) > 1 and full_cmd[1].endswith(".py"):
        if not os.path.exists(full_cmd[1]):
            return False
    return True


def _probe_mcp_server(full_cmd: List[str], timeout: float = 10.0) -> Tuple[bool, str]:
    if not full_cmd:
        return False, "No command configured"

    exe = full_cmd[0]
    if not (os.path.exists(exe) or shutil.which(exe)):
        return False, f"Command executable not found: {exe}"

    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "subway-status", "version": "1.0.0"}
        }
    }
    tools_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except Exception as err:
        return False, f"Failed to spawn process: {err}"

    deadline = time.time() + timeout

    def send_line(msg_dict: Dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg_dict) + "\n")
        proc.stdin.flush()

    def read_response(expected_id: int) -> Optional[Dict[str, Any]]:
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            if proc.stdout is None:
                return None
            rlist, _, _ = select.select([proc.stdout], [], [], remaining)
            if not rlist:
                if proc.poll() is not None:
                    return None
                continue
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    return None
                continue
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("id") == expected_id:
                    return parsed
            except Exception:
                continue
        return None

    try:
        send_line(init_req)
        init_resp = read_response(expected_id=1)
        if not init_resp:
            err_msg = ""
            if proc.stderr:
                try:
                    rlist, _, _ = select.select([proc.stderr], [], [], 0.1)
                    if rlist:
                        err_msg = proc.stderr.read()
                except Exception:
                    pass
            proc.kill()
            return False, f"Timeout or no initialize response{f': {err_msg.strip()[:200]}' if err_msg.strip() else ''}"

        send_line({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send_line(tools_req)

        tools_resp = read_response(expected_id=2)
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()

        if not tools_resp:
            return False, "Timeout or no tools/list response"

        if "error" in tools_resp:
            return False, f"tools/list error: {tools_resp['error']}"

        result = tools_resp.get("result", {})
        tools = result.get("tools", [])
        return True, f"{len(tools)}"

    except Exception as err:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"Communication error: {err}"


def check_status() -> None:
    print("🚇 Subway Context Connection Status:\n")
    targets = [
        ("Claude Code", os.path.expanduser("~/.claude.json"), "claude"),
        ("Antigravity", os.path.expanduser("~/.gemini/config/mcp_config.json"), "antigravity"),
        ("OpenCode", os.path.expanduser("~/.config/opencode/opencode.json"), "opencode"),
    ]

    for name, config_path, key in targets:
        print(f"[{name}] ({config_path})")
        try:
            data = _load_json(config_path)
        except ValueError as err:
            print("  Registered: no")
            print("  Command exists: no")
            print(f"  Failure: Unreadable config ({err})\n")
            continue

        if key in ("claude", "antigravity"):
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            entry = servers.get("subway-context")
        else:
            mcp = data.get("mcp", {}) if isinstance(data, dict) else {}
            servers = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
            entry = servers.get("subway-context")

        if not entry:
            print("  Registered: no")
            print("  Command exists: no")
            print("  Failure: Not registered\n")
            continue

        print("  Registered: yes")
        cmd = entry.get("command")
        args = entry.get("args", [])
        if isinstance(cmd, list):
            full_cmd = list(cmd) + (args if isinstance(args, list) else [])
        elif isinstance(cmd, str):
            full_cmd = [cmd] + (args if isinstance(args, list) else [])
        else:
            full_cmd = []

        cmd_exists = _check_command_exists(full_cmd)
        print(f"  Command exists: {'yes' if cmd_exists else 'no'}")

        if not cmd_exists:
            cmd_str = " ".join(full_cmd) if full_cmd else "<none>"
            print(f"  Failure: Registered command does not exist ({cmd_str})\n")
            continue

        ok, result = _probe_mcp_server(full_cmd, timeout=10.0)
        if ok:
            print(f"  Tool count: {result}\n")
        else:
            print(f"  Failure: {result}\n")


def parse_cli_targets(args: List[str], detected_keys: List[str]) -> Optional[List[str]]:
    """Parse command line flags like --targets claude,gemini or --skip opencode."""
    for i, a in enumerate(args):
        if a in ("--targets", "--only") and i + 1 < len(args):
            raw = args[i + 1]
            return _normalize_selection(raw, detected_keys)
        if a.startswith("--targets=") or a.startswith("--only="):
            raw = a.split("=", 1)[1]
            return _normalize_selection(raw, detected_keys)

    for i, a in enumerate(args):
        if a == "--skip" and i + 1 < len(args):
            skip_targets = _normalize_selection(args[i + 1], detected_keys)
            return [k for k in detected_keys if k not in skip_targets]
        if a.startswith("--skip="):
            skip_targets = _normalize_selection(a.split("=", 1)[1], detected_keys)
            return [k for k in detected_keys if k not in skip_targets]

    individual = []
    if "--claude" in args:
        individual.append("claude")
    if "--gemini" in args or "--antigravity" in args or "--agy" in args:
        individual.append("antigravity")
    if "--opencode" in args or "--open" in args:
        individual.append("opencode")

    if individual:
        return list(dict.fromkeys(individual))
    return None


def _normalize_selection(raw: str, detected_keys: List[str]) -> List[str]:
    raw = raw.strip().strip("'\"[]() ").lower()
    if raw in ("a", "all", "both"):
        return detected_keys
    chosen = []
    num_map = {str(i + 1): k for i, k in enumerate(detected_keys)}
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part in num_map:
            chosen.append(num_map[part])
        elif part in detected_keys:
            chosen.append(part)
        elif part in ("gemini", "agy") and "antigravity" in detected_keys:
            chosen.append("antigravity")
        elif part in ("open", "opencode2") and "opencode" in detected_keys:
            chosen.append("opencode")
    return list(dict.fromkeys(chosen))


def ensure_notifier(interactive: bool, auto_yes: bool) -> str:
    """Offer or verify desktop alert integration so clicking an alert opens the HUD. Always says what it did."""
    if sys.platform.startswith("win"):
        print("  ✓ Desktop alerts: Windows native toasts need nothing extra; clicking an alert opens the HUD.")
        return "present"
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            has_action = False
            try:
                proc = subprocess.run(["notify-send", "--help"], capture_output=True, text=True, timeout=2)
                has_action = "--action" in ((proc.stdout or "") + (proc.stderr or ""))
            except Exception:
                has_action = False
            if has_action:
                print("  ✓ Desktop alerts: notify-send found with action support; clicking an alert opens the HUD.")
                return "present"
            print("  ! Desktop alerts: notify-send found. Alerts work, but can't open the HUD on click.")
            return "no-action"

        why = ("Subway sends desktop alerts when a worker needs you. "
               "With action support, clicking one opens the HUD.")
        pkg_cmd = ""
        if shutil.which("apt") or shutil.which("apt-get"):
            pkg_cmd = "sudo apt install libnotify-bin"
        elif shutil.which("dnf"):
            pkg_cmd = "sudo dnf install libnotify"
        elif shutil.which("pacman"):
            pkg_cmd = "sudo pacman -S libnotify"

        if pkg_cmd:
            print(f"\n  Optional: {why}\n  To add desktop alerts, run: {pkg_cmd}")
        else:
            print(f"\n  Optional: {why}\n  To add desktop alerts, install libnotify (notify-send).")
        return "missing"
    if sys.platform != "darwin":
        return "skipped"
    if shutil.which("terminal-notifier"):
        print("  ✓ Desktop alerts: terminal-notifier found, so clicking an alert opens the HUD.")
        return "present"
    why = ("Subway sends desktop alerts when a worker needs you. With terminal-notifier, clicking one opens the HUD; "
           "without it alerts still show, but macOS opens Script Editor when you click them.")
    brew = shutil.which("brew")
    if not brew:
        print(f"\n  Optional: {why}\n  Install Homebrew, then run: brew install terminal-notifier")
        return "no-brew"
    if not interactive and not auto_yes:
        print(f"\n  Optional: {why}\n  To add it: brew install terminal-notifier")
        return "hint"
    if interactive and not auto_yes:
        print(f"\n  {why}")
        answer = input("  Install terminal-notifier with Homebrew now? [Y/n]: ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("  Skipped. You can add it any time: brew install terminal-notifier")
            return "declined"
    print("  Installing terminal-notifier with Homebrew (brew install terminal-notifier)...")
    try:
        done = subprocess.run([brew, "install", "terminal-notifier"], timeout=600).returncode == 0
    except (OSError, subprocess.SubprocessError):
        done = False
    if done:
        print("  ✓ terminal-notifier installed: clicking a Subway alert now opens the HUD.")
        return "installed"
    print("  ✗ Couldn't install it. Alerts still work. To try again: brew install terminal-notifier")
    return "failed"


def main() -> None:
    args = sys.argv[1:]

    # Status check
    if "--status" in args:
        check_status()
        return

    # Disconnect / Removal
    if "--remove" in args or "--uninstall" in args:
        target = "all"
        for i, a in enumerate(args):
            if a in ("--remove", "--uninstall") and i + 1 < len(args) and not args[i + 1].startswith("-"):
                target = args[i + 1]
                break
        print(f"🚇 Disconnecting Subway Context ({target})...\n")
        if target == "all":
            remove_all()
        else:
            remove_target(target)
        print("\n✓ Completed.")
        return

    server_py, py_bin = get_default_paths()
    print("🚇 Subway Context Universal Connector\n")
    print("Environment Detection:")
    envs = detect_environments()
    detected_keys = [k for k, v in envs.items() if v["detected"]]

    if not detected_keys:
        print("\n⚠️  No supported agent environments (Claude, Antigravity, OpenCode) were detected.")
        print("Please install at least one supported tool before connecting Subway Context.")
        return

    print("\nDisclaimer & Privacy:")
    print("  Subway runs locally and serves MCP over stdio.")
    print("  The optional dashboard listens on 127.0.0.1 only.")
    print("  It writes generated stubs to ~/.config/opencode/agents.\n")

    # Determine targets
    cli_targets = parse_cli_targets(args, detected_keys)
    auto_yes = "--yes" in args or "-y" in args or "-f" in args or "--force" in args

    if cli_targets is not None:
        selected_keys = cli_targets
    elif auto_yes or not sys.stdin.isatty():
        selected_keys = detected_keys
    else:
        print("Select environments to connect:")
        for i, k in enumerate(detected_keys):
            print(f"  [{i + 1}] {envs[k]['name']}")
        print("  [A] All detected environments (Default)")
        print("  [Q] Cancel / Quit")

        try:
            choice = input("\nEnter choice (e.g. 1,2 or 'A') [A]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nInstallation aborted.")
            return

        if choice.lower() in ("q", "quit", "exit", "n", "no"):
            print("Installation cancelled. No changes made.")
            return

        selected_keys = _normalize_selection(choice if choice else "all", detected_keys)

    if not selected_keys:
        print("No environments selected. Exiting without changes.")
        return

    print(f"\nProceeding to connect Subway Context to {len(selected_keys)} environment(s):")
    installers = {
        "claude": install_claude,
        "antigravity": install_antigravity,
        "opencode": install_opencode,
    }

    connected_count = 0
    for k in selected_keys:
        installer = installers.get(k)
        if installer:
            success, msg = installer(server_py, py_bin)
            icon = "✓" if success else "✗"
            print(f"  {icon} {envs[k]['name']:<22}: {msg}")
            if success:
                connected_count += 1

    failed = len(selected_keys) - connected_count
    if failed:
        print(f"\n✗ {connected_count} connected, {failed} failed. Nothing was changed for the failed ones.")
        sys.exit(1)
    print(f"\n✓ Completed. {connected_count} agent line(s) connected.")
    print("You can now bridge chats across your selected windows.")
    ensure_notifier(interactive=sys.stdin.isatty(), auto_yes=auto_yes)


if __name__ == "__main__":
    main()
