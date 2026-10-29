#!/usr/bin/env python3
"""Subway Context — Universal One-Click Installer & Connector.

Connects Subway Context across all available coding agents on this machine:
1. Claude Code (~/.claude.json)
2. Antigravity IDE (~/.gemini/config/mcp_config.json)
3. OpenCode Terminal (~/.config/opencode/opencode.json)

Usage:
    python3 install.py          # Auto-detect and connect all available agents
    python3 install.py --status # Check connection status across all agents
    python3 install.py --remove # Disconnect Subway Context from all agents
"""

import json
import os
import sys
from typing import Any, Dict, List, Tuple


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
    py_bin = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return server_py, py_bin


def install_claude(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.claude.json")
    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.exists(config_path) and not os.path.exists(claude_dir):
        return False, "Not detected (no ~/.claude found)"

    data = _load_json(config_path)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": py_bin,
        "args": [server_py]
    }
    _atomic_write_json(config_path, data)
    return True, f"Connected in {config_path}"


def install_antigravity(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    gemini_dir = os.path.expanduser("~/.gemini")
    if not os.path.exists(config_path) and not os.path.exists(gemini_dir):
        return False, "Not detected (no ~/.gemini found)"

    data = _load_json(config_path)
    servers = data.setdefault("mcpServers", {})
    servers["subway-context"] = {
        "command": py_bin,
        "args": [server_py]
    }
    _atomic_write_json(config_path, data)
    return True, f"Connected in {config_path}"


def install_opencode(server_py: str, py_bin: str) -> Tuple[bool, str]:
    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    opencode_dir = os.path.expanduser("~/.config/opencode")
    if not os.path.exists(config_path) and not os.path.exists(opencode_dir):
        return False, "Not detected (no ~/.config/opencode found)"

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


def remove_all() -> None:
    print("🚇 Disconnecting Subway Context across all agent environments...\n")
    # Claude
    c_path = os.path.expanduser("~/.claude.json")
    if os.path.exists(c_path):
        d = _load_json(c_path)
        if "mcpServers" in d and "subway-context" in d["mcpServers"]:
            del d["mcpServers"]["subway-context"]
            _atomic_write_json(c_path, d)
            print("  ✓ Claude Code: Removed subway-context")

    # Antigravity
    a_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    if os.path.exists(a_path):
        d = _load_json(a_path)
        if "mcpServers" in d and "subway-context" in d["mcpServers"]:
            del d["mcpServers"]["subway-context"]
            _atomic_write_json(a_path, d)
            print("  ✓ Antigravity: Removed subway-context")

    # OpenCode
    o_path = os.path.expanduser("~/.config/opencode/opencode.json")
    if os.path.exists(o_path):
        d = _load_json(o_path)
        if "mcp" in d and "servers" in d["mcp"] and "subway-context" in d["mcp"]["servers"]:
            del d["mcp"]["servers"]["subway-context"]
            _atomic_write_json(o_path, d)
            print("  ✓ OpenCode: Removed subway-context")

    print("\n✓ Disconnected.")


def check_status() -> None:
    print("🚇 Subway Context Connection Status:\n")
    # Claude
    c_path = os.path.expanduser("~/.claude.json")
    c_conn = "subway-context" in _load_json(c_path).get("mcpServers", {})
    print(f"  {'✓' if c_conn else '✗'} Claude Code:     {'Connected' if c_conn else 'Not connected'} ({c_path})")

    # Antigravity
    a_path = os.path.expanduser("~/.gemini/config/mcp_config.json")
    a_conn = "subway-context" in _load_json(a_path).get("mcpServers", {})
    print(f"  {'✓' if a_conn else '✗'} Antigravity:     {'Connected' if a_conn else 'Not connected'} ({a_path})")

    # OpenCode
    o_path = os.path.expanduser("~/.config/opencode/opencode.json")
    o_conn = "subway-context" in _load_json(o_path).get("mcp", {}).get("servers", {})
    print(f"  {'✓' if o_conn else '✗'} OpenCode:        {'Connected' if o_conn else 'Not connected'} ({o_path})")


def install_all() -> None:
    server_py, py_bin = get_default_paths()
    print("🚇 Subway Context Universal Connector\n")
    print(f"Server binary: {server_py}")
    print(f"Python runner: {py_bin}\n")

    targets = [
        ("Claude Code", install_claude),
        ("Antigravity (Gemini)", install_antigravity),
        ("OpenCode (Terminal)", install_opencode),
    ]

    connected_count = 0
    for name, installer in targets:
        success, msg = installer(server_py, py_bin)
        status_icon = "✓" if success else "—"
        print(f"  {status_icon} {name:<22}: {msg}")
        if success:
            connected_count += 1

    print(f"\n✓ Completed. {connected_count} of {len(targets)} agent lines connected.")
    print("You can now query Subway Context seamlessly across all active windows.")


if __name__ == "__main__":
    if "--status" in sys.argv:
        check_status()
    elif "--remove" in sys.argv or "--uninstall" in sys.argv:
        remove_all()
    else:
        install_all()
