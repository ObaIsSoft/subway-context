#!/usr/bin/env python3
"""Comprehensive test suite for Subway Context budgets."""
import contextlib
import datetime as _dt
import hashlib
import hmac
import http.client
import inspect
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Pre-import isolation: configure temp dir and stub binaries before importing server or install
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
os.environ["CLAUDE_BIN"] = _STUB_BIN
os.environ["OPENCODE_BIN"] = _STUB_BIN
os.environ["AGY_BIN"] = _STUB_BIN
os.environ.pop("ANTIGRAVITY_CONVERSATION_ID", None)
os.environ.pop("SUBWAY_STATION_ID", None)

import server


class TestBudgetState(unittest.TestCase):
    """Tests for budget_state(): ok, warning at 80%, reached at 100%."""

    def test_chat_budget_levels(self):
        # 1. OK (< 80%)
        with patch.object(server, "_load_json", return_value={"chat_budget_tokens": 1000}), \
             patch.object(server, "chat_tokens_used", return_value=500):
            res = server.budget_state()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["scope"], "chat")
            self.assertEqual(res[0]["status"], "ok")
            self.assertEqual(res[0]["pct"], 50.0)

        # 2. Warning (>= 80% and < 100%)
        with patch.object(server, "_load_json", return_value={"chat_budget_tokens": 1000}), \
             patch.object(server, "chat_tokens_used", return_value=800):
            res = server.budget_state()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["status"], "warning")
            self.assertEqual(res[0]["pct"], 80.0)

        # 3. Reached (>= 100%)
        with patch.object(server, "_load_json", return_value={"chat_budget_tokens": 1000}), \
             patch.object(server, "chat_tokens_used", return_value=1000):
            res = server.budget_state()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["status"], "reached")
            self.assertEqual(res[0]["pct"], 100.0)

    def test_provider_budget_levels(self):
        # Provider ok, warning at 80%, reached at 100%
        for used, expected_status in [(799, "ok"), (800, "warning"), (1000, "reached")]:
            with patch.object(server, "_load_json", return_value={"budgets": {"claude": 1000}}), \
                 patch.object(server, "_provider_5h_tokens", return_value=used):
                res = server.budget_state()
                p_entry = next((e for e in res if e["scope"] == "claude"), None)
                self.assertIsNotNone(p_entry)
                self.assertEqual(p_entry["status"], expected_status)


class TestHookPrompt(unittest.TestCase):
    """Tests for hook_prompt: returns additionalContext when warning, decision block when reached.

    Warning shows once but block always.
    """

    def test_hook_prompt_warning_and_block(self):
        # Mock budget_ping_notice to show once (return text first time, None second time)
        ping_calls = []

        def mock_ping(scope, used, limit):
            ping_calls.append((scope, used, limit))
            if len(ping_calls) == 1:
                return "Warning: chat budget at 80%"
            return None

        cfg = {"chat_budget_tokens": 1000}
        event = {"session_id": "test_session"}

        with patch.object(server, "_load_json", return_value=cfg):
            # Warning condition (850 tokens < 1000 limit)
            with patch.object(server, "chat_tokens_used", return_value=850), \
                 patch.object(server, "budget_ping_notice", side_effect=mock_ping):
                # First time: returns additionalContext
                res1 = server.hook_prompt(event)
                self.assertIsNotNone(res1)
                self.assertIn("hookSpecificOutput", res1)
                self.assertEqual(
                    res1["hookSpecificOutput"]["additionalContext"],
                    "Warning: chat budget at 80%"
                )

                # Second time: warning already shown, returns None
                res2 = server.hook_prompt(event)
                self.assertIsNone(res2)

            # Reached condition (1000 tokens >= 1000 limit)
            with patch.object(server, "chat_tokens_used", return_value=1000):
                # First time: returns decision block
                res_block1 = server.hook_prompt(event)
                self.assertIsNotNone(res_block1)
                self.assertEqual(res_block1.get("decision"), "block")
                self.assertIn("Budget reached", res_block1.get("reason", ""))

                # Second time: block shows ALWAYS
                res_block2 = server.hook_prompt(event)
                self.assertIsNotNone(res_block2)
                self.assertEqual(res_block2.get("decision"), "block")
                self.assertIn("Budget reached", res_block2.get("reason", ""))


class TestEnforceBudgets(unittest.TestCase):
    """Tests for enforce_budgets: stops running worker over max_tokens, custom past max_seconds."""

    def test_stops_running_worker_over_max_tokens(self):
        worker = {
            "id": "w_tokens_123",
            "status": "running",
            "provider": "claude",
            "budget": {"max_tokens": 500},
            "usage": {"input": 300, "output": 250},  # total 550 >= 500
        }
        with patch.object(server, "_load_json", return_value={}), \
             patch.object(server, "_all_workers", return_value=[worker]), \
             patch.object(server, "t_stop_worker") as mock_stop:
            server.enforce_budgets()
            mock_stop.assert_called_once_with("w_tokens_123", reason="budget")

    def test_stops_running_custom_provider_past_max_seconds(self):
        worker = {
            "id": "w_custom_456",
            "status": "running",
            "provider": "my-custom-provider",
            "budget": {"max_seconds": 60},
            "started_at": time.time() - 120,  # 120s ago > 60s
        }
        with patch.object(server, "_load_json", return_value={}), \
             patch.object(server, "_all_workers", return_value=[worker]), \
             patch.object(server, "t_stop_worker") as mock_stop:
            server.enforce_budgets()
            mock_stop.assert_called_once_with("w_custom_456", reason="budget")

    def test_does_not_stop_if_under_budget(self):
        worker = {
            "id": "w_ok_789",
            "status": "running",
            "provider": "my-custom-provider",
            "budget": {"max_tokens": 500, "max_seconds": 60},
            "usage": {"input": 100, "output": 100},
            "started_at": time.time() - 10,
        }
        with patch.object(server, "_load_json", return_value={}), \
             patch.object(server, "_all_workers", return_value=[worker]), \
             patch.object(server, "t_stop_worker") as mock_stop:
            server.enforce_budgets()
            mock_stop.assert_not_called()


class TestCheckBudgetPreflight(unittest.TestCase):
    """Tests for check_budget_preflight: refuses a reached provider including custom."""

    def test_refuses_reached_standard_provider(self):
        cfg = {"budgets": {"claude": 1000}}
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "_provider_5h_tokens", return_value=1000):
            ok, msg = server.check_budget_preflight(provider="claude")
            self.assertFalse(ok)
            self.assertIn("[BUDGET GUARD VIOLATION]", msg)
            self.assertIn("claude", msg)
            self.assertIn("reached", msg)

    def test_refuses_reached_custom_provider(self):
        cfg = {"budgets": {"custom-llm": 500}}
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "_provider_5h_tokens", return_value=500):
            ok, msg = server.check_budget_preflight(provider="custom-llm")
            self.assertFalse(ok)
            self.assertIn("[BUDGET GUARD VIOLATION]", msg)
            self.assertIn("custom-llm", msg)
            self.assertIn("reached", msg)


class TestHandleToolsCallBudget(unittest.TestCase):
    """Tests for tools/call through handle: returns isError when reached,

    except hud_status, quota_status, check_notes, answer_permission.
    """

    def test_blocked_tools_return_is_error(self):
        cfg = {"chat_budget_tokens": 1000}
        req = {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "list_chats",
                "arguments": {}
            }
        }
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "chat_tokens_used", return_value=1000), \
             patch.object(server, "enforce_budgets"):
            resp = server.handle(req)
            self.assertIsNotNone(resp)
            self.assertTrue(resp.get("result", {}).get("isError"))
            content = resp.get("result", {}).get("content", [])
            self.assertTrue(any("Budget reached" in c.get("text", "") for c in content))

    def test_exempt_tools_allowed_when_budget_reached(self):
        cfg = {"chat_budget_tokens": 1000}
        exempt_tools = ["hud_status", "quota_status", "check_notes", "answer_permission"]

        for tool_name in exempt_tools:
            req = {
                "jsonrpc": "2.0",
                "id": f"call-{tool_name}",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {}
                }
            }
            mock_tool_fn = MagicMock(return_value=f"ok from {tool_name}")
            orig_spec = server.TOOLS[tool_name]
            # Replace tool callable with mock
            with patch.dict(server.TOOLS, {tool_name: (mock_tool_fn, orig_spec[1])}), \
                 patch.object(server, "_load_json", return_value=cfg), \
                 patch.object(server, "chat_tokens_used", return_value=1000), \
                 patch.object(server, "enforce_budgets"), \
                 patch.object(server, "_pending_permissions", return_value=[]), \
                 patch.object(server, "_provider_5h_tokens", return_value=0), \
                 patch.object(server, "low_limit_notice", return_value=""):
                resp = server.handle(req)
                self.assertIsNotNone(resp)
                # Should not be budget isError
                self.assertFalse(resp.get("result", {}).get("isError", False))
                mock_tool_fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
