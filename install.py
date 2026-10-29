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
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_json(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def get_default_paths() -> Tuple[str, str]:
    server_dir = os.path.dirname(os.path.abspath(__file__))
    server_py = os.path.join(server_dir, "server.py")
    py_bin = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return server_py, py_bin


def detect_environments() -> Dict[str, Dict[str, Any]]:
    """Probes system to see which supported coding agent environments exist."""
    c_config = os.path.expanduser("~/.claude.json")
    c_dir = os.path.expanduser("~/.claude")
    c_bin = shutil.which("claude")
    claude_found = os.path.exists(c_config) or os.path.exists(c_dir) or c_bin is not None

    a_config = os.path.expanduser("~/.gemini/config/mcp_config.json")
    a_dir = os.path.expanduser("~/.gemini")
    agy_found = os.path.exists(a_config) or os.path.exists(a_dir)

    o_config = os.path.expanduser("~/.config/opencode/opencode.json")
    o_dir = os.path.expanduser("~/.config/opencode")
    o_share = os.path.expanduser("~/.local/share/opencode")
    o_bin = shutil.which("opencode2") or shutil.which("opencode")
    opencode_found = os.path.exists(o_config) or os.path.exists(o_dir) or os.path.exists(o_share) or o_bin is not None

    return {
        "claude": {
            "name": "Claude Code",
            "detected": claude_found,
            "config": c_config,
        },
        "antigravity": {
            "name": "Antigravity (Gemini)",
            "detected": agy_found,
            "config": a_config,
        },
        "opencode": {
            "name": "OpenCode (Terminal)",
            "detected": opencode_found,
            "config": o_config,
        },
    }


def install_claude(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.claude.json")
    data = _load_json(config_path)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": py_bin,
        "args": [server_py]
    }
    _atomic_write_json(config_path, data)

    # Keyboard / slash command listeners (~/.claude/commands/)
    try:
        cmds_dir = os.path.expanduser("~/.claude/commands")
        os.makedirs(cmds_dir, exist_ok=True)
        with open(os.path.join(cmds_dir, "opencode.md"), "w", encoding="utf-8") as f:
            f.write("Read the latest decisions and files modified by OpenCode terminal agent via Subway Context:\n"
                    "Call MCP tool `read_chat(chat_id=\"@opencode\", mode=\"decisions\", since=\"last\")` and summarize the results.\n")
        with open(os.path.join(cmds_dir, "gemini.md"), "w", encoding="utf-8") as f:
            f.write("Read the latest decisions and files modified by the Antigravity/Gemini session via Subway Context:\n"
                    "Call MCP tool `read_chat(chat_id=\"@agy-latest\", mode=\"decisions\", since=\"last\")` and summarize the results.\n")
        with open(os.path.join(cmds_dir, "subway.md"), "w", encoding="utf-8") as f:
            f.write("List all active agent stations and highlights across Claude Code, Antigravity, and OpenCode:\n"
                    "Call MCP tool `list_chats()` and present the active lines.\n")
    except Exception:
        pass

    return True, f"Connected in {config_path} & commands synced"


def install_antigravity(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    data = _load_json(config_path)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": py_bin,
        "args": [server_py]
    }
    _atomic_write_json(config_path, data)

    # Antigravity visual @ skills (~/.gemini/config/skills/)
    try:
        skills_base = os.path.expanduser("~/.gemini/config/skills")
        for s_name, s_desc, s_call in (
            ("opencode", "Subway Context connector to OpenCode terminal agent.", 'read_chat(chat_id="@opencode", mode="decisions", since="last")'),
            ("claude", "Subway Context connector to Claude Code.", 'read_chat(chat_id="@claude-latest", mode="decisions", since="last")'),
            ("subway", "Subway Context multi-window agent transit system.", "list_chats()"),
        ):
            s_dir = os.path.join(skills_base, s_name)
            os.makedirs(s_dir, exist_ok=True)
            with open(os.path.join(s_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {s_name}\ndescription: {s_desc}\n---\n\n"
                        f"# {s_name.capitalize()} Transit Connector\n\n"
                        f"When mentioned or invoked with `@{s_name}`, call Subway Context MCP tool `{s_call}`.\n")
    except Exception:
        pass

    return True, f"Connected in {config_path} & @ skills synced"


def install_opencode(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    data = _load_json(config_path)
    mcp_obj = data.setdefault("mcp", {})
    servers = mcp_obj.setdefault("servers", {})
    servers["subway-context"] = {
        "type": "local",
        "command": [py_bin, server_py]
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


def remove_target(target: str) -> bool:
    target = target.lower()
    removed = False
    if target in ("claude", "all"):
        c_path = os.path.expanduser("~/.claude.json")
        if os.path.exists(c_path):
            d = _load_json(c_path)
            if "mcpServers" in d and "subway-context" in d["mcpServers"]:
                del d["mcpServers"]["subway-context"]
                _atomic_write_json(c_path, d)
                print("  ✓ Claude Code: Disconnected from ~/.claude.json")
                removed = True

    if target in ("antigravity", "gemini", "agy", "all"):
        a_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
        if os.path.exists(a_path):
            d = _load_json(a_path)
            if "mcpServers" in d and "subway-context" in d["mcpServers"]:
                del d["mcpServers"]["subway-context"]
                _atomic_write_json(a_path, d)
                print("  ✓ Antigravity: Disconnected from ~/.gemini/config/mcp_config.json")
                removed = True

    if target in ("opencode", "opencode2", "all"):
        o_path = os.path.expanduser("~/.config/opencode/opencode.json")
        if os.path.exists(o_path):
            d = _load_json(o_path)
            if "mcp" in d and "servers" in d["mcp"] and "subway-context" in d["mcp"]["servers"]:
                del d["mcp"]["servers"]["subway-context"]
                _atomic_write_json(o_path, d)
                print("  ✓ OpenCode: Disconnected from ~/.config/opencode/opencode.json")
                removed = True

    return removed


def check_status() -> None:
    print("🚇 Subway Context Connection Status:\n")
    c_path = os.path.expanduser("~/.claude.json")
    c_conn = "subway-context" in _load_json(c_path).get("mcpServers", {})
    print(f"  {'✓' if c_conn else '✗'} Claude Code:     {'Connected' if c_conn else 'Not connected'} ({c_path})")

    a_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    a_conn = "subway-context" in _load_json(a_path).get("mcpServers", {})
    print(f"  {'✓' if a_conn else '✗'} Antigravity:     {'Connected' if a_conn else 'Not connected'} ({a_path})")

    o_path = os.path.expanduser("~/.config/opencode/opencode.json")
    o_conn = "subway-context" in _load_json(o_path).get("mcp", {}).get("servers", {})
    print(f"  {'✓' if o_conn else '✗'} OpenCode:        {'Connected' if o_conn else 'Not connected'} ({o_path})")


def parse_cli_targets(args: List[str], detected_keys: List[str]) -> Optional[List[str]]:
    """Parse command line flags like --targets claude,gemini or --skip opencode."""
    # Check --targets or --only
    for i, a in enumerate(args):
        if a in ("--targets", "--only") and i + 1 < len(args):
            raw = args[i + 1]
            return _normalize_selection(raw, detected_keys)
        if a.startswith("--targets=") or a.startswith("--only="):
            raw = a.split("=", 1)[1]
            return _normalize_selection(raw, detected_keys)

    # Check --skip
    for i, a in enumerate(args):
        if a == "--skip" and i + 1 < len(args):
            skip_targets = _normalize_selection(args[i + 1], detected_keys)
            return [k for k in detected_keys if k not in skip_targets]
        if a.startswith("--skip="):
            skip_targets = _normalize_selection(a.split("=", 1)[1], detected_keys)
            return [k for k in detected_keys if k not in skip_targets]

    # Individual flags: --claude, --gemini, --antigravity, --opencode
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
    raw = raw.strip().lower()
    if raw in ("all", "both"):
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
        remove_target(target)
        print("\n✓ Completed.")
        return

    server_py, py_bin = get_default_paths()
    envs = detect_environments()
    detected_keys = [k for k, v in envs.items() if v["detected"]]

    print("🚇 Subway Context Universal Connector\n")
    print("Environment Detection:")
    for k, v in envs.items():
        status = "Detected" if v["detected"] else "Not found"
        icon = "✓" if v["detected"] else "—"
        print(f"  {icon} {v['name']:<22}: {status} ({v['config']})")

    if not detected_keys:
        print("\n⚠️  No supported agent environments (Claude, Antigravity, OpenCode) were detected.")
        print("Please install at least one supported tool before connecting Subway Context.")
        return

    print("\nDisclaimer & Privacy:")
    print("  Subway Context runs 100% locally on standard I/O (stdio).")
    print("  It will only register in the environments you select.")
    print("  Unselected or uninstalled tools are NEVER touched or modified.\n")

    # Determine targets
    cli_targets = parse_cli_targets(args, detected_keys)
    auto_yes = "--yes" in args or "-y" in args or "-f" in args or "--force" in args

    if cli_targets is not None:
        selected_keys = cli_targets
    elif auto_yes or not sys.stdin.isatty():
        # Non-interactive default: connect all detected
        selected_keys = detected_keys
    else:
        # Interactive selection
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

    print(f"\n✓ Completed. {connected_count} agent line(s) connected.")
    print("You can now bridge chats across your selected windows.")


if __name__ == "__main__":
    main()
