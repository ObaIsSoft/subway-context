import os
import sys
import tempfile

# 1. Disable notifications immediately
os.environ["SUBWAY_NO_NOTIFY"] = "1"

# 2. Ensure repo root and tests directory are on sys.path
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

# 3. Idempotent pre-import isolation sandbox setup
_SANDBOX_INITIALIZED = os.environ.get("_SUBWAY_SANDBOX_INITIALIZED") == "1"

if not _SANDBOX_INITIALIZED:
    _GLOBAL_SANDBOX = tempfile.mkdtemp(prefix="subway_preimport_sandbox_")
    _STUB_BIN = os.path.join(_GLOBAL_SANDBOX, "stub_agent.sh")
    with open(_STUB_BIN, "w", encoding="utf-8") as _f:
        _f.write("#!/bin/sh\nexit 0\n")
    os.chmod(_STUB_BIN, 0o755)

    os.environ["HOME"] = _GLOBAL_SANDBOX
    os.environ["SUBWAY_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".subway-context")
    os.environ["SUBWAY_CLAUDE_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".claude")
    os.environ["SUBWAY_CLAUDE_JSON"] = os.path.join(_GLOBAL_SANDBOX, ".claude.json")
    os.environ["SUBWAY_AGY_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".gemini", "antigravity")
    os.environ["SUBWAY_OPENCODE_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".local", "share", "opencode")
    os.environ["SUBWAY_OPENCODE_AGENTS_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".config", "opencode", "agents")
    os.environ["SUBWAY_COPILOT_DIR"] = os.path.join(_GLOBAL_SANDBOX, ".copilot")
    os.environ["CLAUDE_BIN"] = _STUB_BIN
    os.environ["OPENCODE_BIN"] = _STUB_BIN
    os.environ["AGY_BIN"] = _STUB_BIN
    os.environ.pop("ANTIGRAVITY_CONVERSATION_ID", None)
    os.environ.pop("SUBWAY_STATION_ID", None)
    os.environ.pop("SUBWAY_WORKER_ID", None)
    os.environ.pop("SUBWAY_WORKER", None)
    os.environ["_SUBWAY_SANDBOX_INITIALIZED"] = "1"
