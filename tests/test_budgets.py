#!/usr/bin/env python3
"""Comprehensive test suite for Subway Context budgets."""
import os as _os
_os.environ["SUBWAY_NO_NOTIFY"] = "1"  # Never send real desktop alerts from tests.
import contextlib
import datetime as _dt
import hashlib
import hmac
import http.client
import inspect
import io
import json
import multiprocessing
import os
import re
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
from unittest import mock
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


class TestPerChatBudgets(unittest.TestCase):
    """Chat budgets belong to the chat that set them; a universal default covers chats without one."""

    def _cfg_io(self, initial=None):
        store = {"config.json": dict(initial or {})}

        def load(fn):
            return dict(store.get(fn, {}))

        def save(fn, data):
            store[fn] = data

        return store, load, save

    def test_chat_a_budget_never_blocks_chat_b(self):
        store, load, save = self._cfg_io()
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save):
            server.set_budget("chat", 6_000_000, session_id="chat-a")
        with patch.object(server, "_load_json", side_effect=load):
            limit, scope = server.chat_budget_for("chat-b")
            self.assertIsNone(limit)
            self.assertIsNone(scope)

    def test_universal_default_applies_to_chats_without_their_own(self):
        store, load, save = self._cfg_io()
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save):
            server.set_budget("chat", 1000)  # no session_id -> universal default
        with patch.object(server, "_load_json", side_effect=load):
            limit, scope = server.chat_budget_for("any-chat")
            self.assertEqual(limit, 1000)
            self.assertEqual(scope, "universal")

    def test_own_budget_overrides_universal(self):
        store, load, save = self._cfg_io()
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save):
            server.set_budget("chat", 1000)
            server.set_budget("chat", 5000, session_id="chat-a")
        with patch.object(server, "_load_json", side_effect=load):
            limit, scope = server.chat_budget_for("chat-a")
            self.assertEqual((limit, scope), (5000, "chat"))
            limit2, scope2 = server.chat_budget_for("chat-b")
            self.assertEqual((limit2, scope2), (1000, "universal"))

    def test_chat_line_sets_the_right_chat(self):
        store, load, save = self._cfg_io()
        chat_line = "subway-context " + "budget chat 5000"
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save), \
             patch.dict(os.environ, {"SUBWAY_WORKER": ""}):
            server.hook_prompt({"prompt": chat_line, "session_id": "chat-a"})
            limit, scope = server.chat_budget_for("chat-a")
            self.assertEqual((limit, scope), (5000, "chat"))
            limit_b, _ = server.chat_budget_for("chat-b")
            self.assertIsNone(limit_b)

    def test_cli_budget_chat_sets_universal_and_dash_chat_sets_one(self):
        store, load, save = self._cfg_io()
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save):
            msg = server.set_budget("chat", 2000)
            self.assertIn("universal", msg.lower())
            self.assertEqual(server.chat_budget_for("any-chat"), (2000, "universal"))

            msg2 = server.set_budget("chat", 3000, session_id="chat-z")
            self.assertIn("this chat", msg2.lower())
            self.assertEqual(server.chat_budget_for("chat-z"), (3000, "chat"))

    def test_migration_both_ways(self):
        store, load, save = self._cfg_io({"chat_budget_tokens": 7000})
        with patch.object(server, "_load_json", side_effect=load):
            limit, scope = server.chat_budget_for("any-chat")
            self.assertEqual((limit, scope), (7000, "universal"))
        with patch.object(server, "_load_json", side_effect=load), \
             patch.object(server, "_save_json", side_effect=save):
            server.set_budget("claude", 10)  # any save migrates the legacy key
            self.assertNotIn("chat_budget_tokens", store["config.json"])
            self.assertEqual(store["config.json"].get("chat_budget_default"), 7000)

    def test_messages_name_the_scope(self):
        with patch.object(server, "_load_json", side_effect=lambda fn: {"chat_budget_default": 100} if fn == "config.json" else {}), \
             patch.object(server, "chat_tokens_used", return_value=150):
            res = server.hook_prompt({"session_id": "any-chat"})
            reason = res.get("reason", "")
            self.assertIn("universal chat budget is reached", reason.lower())
            self.assertIn("subway-context budget chat <tokens>", reason)
            self.assertNotIn("--chat", reason)

        with patch.object(server, "_load_json", side_effect=lambda fn: {"chat_budgets": {"sid1": 100}} if fn == "config.json" else {}), \
             patch.object(server, "chat_tokens_used", return_value=150):
            res2 = server.hook_prompt({"session_id": "sid1"})
            reason2 = res2.get("reason", "")
            self.assertIn("this chat's budget is reached", reason2.lower())
            self.assertIn("subway-context budget chat <tokens> --chat sid1", reason2)


class TestHookPrompt(unittest.TestCase):
    """Tests for hook_prompt: returns additionalContext when warning, decision block when reached.

    Warning shows once but block always.
    """

    def test_hook_prompt_warning_and_block(self):
        # Mock budget_ping_notice to show once (return text first time, None second time)
        ping_calls = []

        def mock_ping(scope, used, limit, **kwargs):
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
                self.assertIn("budget is reached", res_block1.get("reason", "").lower())

                # Second time: block shows ALWAYS
                res_block2 = server.hook_prompt(event)
                self.assertIsNotNone(res_block2)
                self.assertEqual(res_block2.get("decision"), "block")
                self.assertIn("budget is reached", res_block2.get("reason", "").lower())


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
            mock_stop.assert_not_called()

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

    def test_isolate_worker_claims_edits_without_changes_fails(self):
        meta = {"role": "Builder", "prompt": "Edit server.py to add tests",
                "mode": "standard", "capabilities": ["files:read", "files:write"]}
        exit_res = {"status": "completed", "exit_code": 0}
        server._settle_worker_result(exit_res, meta, True, "Done.")
        self.assertEqual(exit_res["status"], "failed")
        self.assertIn("no changes made", exit_res["reason"].lower())


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
            texts = [c.get("text", "") for c in content]
            self.assertTrue(any("budget is reached" in t.lower() for t in texts))
            self.assertTrue(any("subway-context budget chat <tokens>" in t and "--chat" not in t for t in texts))

    def test_blocked_tools_show_own_chat_flag(self):
        cfg = {"chat_budgets": {"sidX": 1000}}
        req = {
            "jsonrpc": "2.0",
            "id": "call-2",
            "method": "tools/call",
            "params": {
                "name": "list_chats",
                "arguments": {}
            }
        }
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "_self_id", return_value="sidX"), \
             patch.object(server, "chat_tokens_used", return_value=1000), \
             patch.object(server, "enforce_budgets"):
            resp = server.handle(req)
            content = resp.get("result", {}).get("content", [])
            texts = [c.get("text", "") for c in content]
            self.assertTrue(any("subway-context budget chat <tokens> --chat sidX" in t for t in texts))

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

    def test_budget_rows_no_chat_and_cached_provider(self):
        server._provider_5h_cache.clear()
        with patch.object(server, "budget_state", return_value=[]), \
             patch.object(server, "chat_tokens_used", return_value=None), \
             patch.object(server, "_newest_claude_transcript_tokens", return_value=4200), \
             patch.object(server, "_provider_5h_tokens", side_effect=lambda prov: 150 if prov == "claude" else 0) as mock_p5h:
            rows = server.budget_rows()
            by_scope = {r["scope"]: r for r in rows}
            self.assertIn("chat", by_scope)
            self.assertEqual(by_scope["chat"]["used"], 4200)
            self.assertIn("claude", by_scope)
            self.assertEqual(by_scope["claude"]["used"], 150)
            mock_calls = mock_p5h.call_count

            # Second call within 30s should hit cache
            rows2 = server.budget_rows()
            self.assertEqual(mock_p5h.call_count, mock_calls)
            by_scope2 = {r["scope"]: r for r in rows2}
            self.assertEqual(by_scope2["claude"]["used"], 150)

    def test_hud_data_includes_chat_budget_rows(self):
        cfg = {"chat_budgets": {"chat-a": 1000, "chat-b": 2000}}
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "all_chats", return_value=[
                 {"id": "chat-a", "title": "Chat A"},
             ]), \
             patch.object(server, "chat_tokens_used", side_effect=lambda sid=None: {"chat-a": 900, "chat-b": 200}.get(sid)):
            data = server.get_hud_data()
            chats = data["settings"]["chat_budgets"]["chats"]
            by_id = {c["session_id"]: c for c in chats}
            self.assertEqual(set(by_id), {"chat-a", "chat-b"})
            self.assertEqual(by_id["chat-a"]["label"], "Chat A")
            self.assertEqual(by_id["chat-a"]["status"], "warning")
            self.assertEqual(by_id["chat-b"]["label"], server._short_id("chat-b"))
            self.assertEqual(chats[0]["session_id"], "chat-a")


class TestStreamPumps(unittest.TestCase):
    """Tests for _pump_claude and _pump_agy token tracking and caps."""

    def test_claude_cache_creation_triggers_cap(self):
        assistant_event = {
            "type": "assistant",
            "message": {
                "id": "msg_test_1",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 50000,
                    "output_tokens": 6,
                },
                "content": [{"type": "text", "text": "test output"}],
            },
        }
        raw_lines = [json.dumps(assistant_event).encode("utf-8") + b"\n"]
        proc = MagicMock()
        proc.stdout = raw_lines
        out = io.BytesIO()
        on_cap = MagicMock()
        run = {"cap": 10000, "on_cap": on_cap}

        server._pump_claude(proc, out, run)

        on_cap.assert_called_once()
        self.assertEqual(run["spent"], 50016)
        log = out.getvalue().decode("utf-8")
        self.assertIn("[cap] spent 50016 of 10000, soft 0", log)

    def test_cap_line_printed_once_per_spent_value(self):
        event = {
            "type": "assistant",
            "message": {
                "id": "msg_repeat",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "content": [{"type": "text", "text": "hi"}],
            },
        }
        raw_lines = [json.dumps(event).encode("utf-8") + b"\n"] * 5
        proc = MagicMock()
        proc.stdout = raw_lines
        out = io.BytesIO()
        run = {"cap": 10000}

        server._pump_claude(proc, out, run)

        log = out.getvalue().decode("utf-8")
        self.assertEqual(log.count("[cap] spent 120 of 10000, soft 0"), 1)

    def test_claude_result_usage_includes_cache_creation(self):
        result_event = {
            "type": "result",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 34855,
                "output_tokens": 6,
                "cache_read_input_tokens": 100,
            },
        }
        raw_lines = [json.dumps(result_event).encode("utf-8") + b"\n"]
        proc = MagicMock()
        proc.stdout = raw_lines
        out = io.BytesIO()
        run = {}

        server._pump_claude(proc, out, run)

        self.assertEqual(run["usage"]["input_tokens"], 34865)
        self.assertEqual(run["usage"]["output_tokens"], 6)
        self.assertEqual(run["usage"]["cache_read_tokens"], 100)

    def test_agy_thinking_tokens_count(self):
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "thinking_tokens": 200,
                },
            },
        }
        raw_lines = [json.dumps(step_event).encode("utf-8") + b"\n"]
        proc = MagicMock()
        proc.stdout = raw_lines
        out = io.BytesIO()
        run = {}

        server._pump_agy(proc, out, run)

        self.assertEqual(run["spent"], 350)


class TestBudgetOriginAndHookGuard(unittest.TestCase):
    def test_hook_guard_denies_budget_and_token_in_bash(self):
        patterns = [
            "curl -X POST http://localhost:8765/api/budget",
            "curl http://127.0.0.1:8765/api/chat_budget",
            "curl -d '{}' http://localhost:8765/api/settings",
            "curl -H 'X-Subway-Token: secret' http://localhost:8765/api/status",
        ]
        for cmd in patterns:
            event = {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
            }
            res = server.hook_guard(event)
            self.assertIsNotNone(res, f"Expected hook_guard to deny: {cmd}")
            out = res.get("hookSpecificOutput", {})
            self.assertEqual(out.get("permissionDecision"), "deny")
            self.assertEqual(
                out.get("permissionDecisionReason"),
                "Budgets are the user's to change. Ask them to run it, or use the HUD.",
            )

    def test_hook_guard_allows_worktree_read_but_denies_state_path(self):
        sdir = tempfile.mkdtemp()
        orig = server.SUBWAY_DIR
        server.SUBWAY_DIR = sdir
        try:
            wt_file = os.path.join(sdir, "worktrees", "x", "server.py")
            os.makedirs(os.path.dirname(wt_file), exist_ok=True)
            with open(wt_file, "w") as f:
                f.write("# test")
            res = server.hook_guard({"tool_name": "Read", "tool_input": {"file_path": wt_file}})
            self.assertIsNone(res)

            cfg_file = os.path.join(sdir, "config.json")
            res2 = server.hook_guard({"tool_name": "Read", "tool_input": {"file_path": cfg_file}})
            self.assertIsNotNone(res2)
            self.assertEqual(
                res2["hookSpecificOutput"]["permissionDecisionReason"],
                "Subway's state files are the user's to change.",
            )
        finally:
            server.SUBWAY_DIR = orig
            shutil.rmtree(sdir, ignore_errors=True)

    def test_hud_post_budget_routes_require_matching_origin_or_referer(self):
        routes = ["/api/budget", "/api/chat_budget", "/api/settings"]

        class DummyHUDHandler(server.SubwayHUDHandler):
            def __init__(self, path, headers):
                self.path = path
                self.headers = headers
                self.denied = None
            def _gate(self, api=True):
                return True
            def _read_payload(self):
                return None
            def _deny(self, code, msg):
                self.denied = (code, msg)

        for route in routes:
            # 1. Plain curl: no Origin or Referer -> 403
            h = DummyHUDHandler(route, {"Host": "127.0.0.1:8765"})
            h.do_POST()
            self.assertEqual(h.denied, (403, "Origin not allowed"), f"Expected plain curl to be denied for {route}")

            # 2. Mismatched Origin -> 403
            h = DummyHUDHandler(route, {"Host": "127.0.0.1:8765", "Origin": "http://evil.com"})
            h.do_POST()
            self.assertEqual(h.denied, (403, "Origin not allowed"), f"Expected mismatched origin to be denied for {route}")

            # 3. Matching Origin -> passes origin check (proceeds to _read_payload)
            h = DummyHUDHandler(route, {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"})
            h.do_POST()
            self.assertIsNone(h.denied, f"Expected matching origin to pass for {route}")

            # 4. Matching Referer -> passes origin check
            h = DummyHUDHandler(route, {"Host": "127.0.0.1:8765", "Referer": "http://127.0.0.1:8765/hud"})
            h.do_POST()
            self.assertIsNone(h.denied, f"Expected matching referer to pass for {route}")



class TestReviewFixes(unittest.TestCase):
    def test_bad_budget_value_changes_nothing(self):
        with mock.patch.object(server, "set_budget") as setter:
            out = server.hook_prompt({"prompt": "subway-context budget chat <tokens>", "session_id": "x"})
        setter.assert_not_called()
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "") if out else ""
        if ctx:
            self.assertIn("Not changed", ctx)

    def test_shell_writes_to_subway_state_are_caught(self):
        self.assertTrue(server._writes_subway_state("echo '{}' > ~/.subway-context/config.json"))
        self.assertTrue(server._writes_subway_state("sed -i s/1/2/ ~/.subway-context/config.json"))
        self.assertFalse(server._writes_subway_state("cat ~/.subway-context/workers/a.log 2>&1"))

    def test_blocked_prompt_keeps_pings_pending(self):
        with mock.patch.object(server, "_load_json", return_value={"chat_budget_tokens": 10}), \
             mock.patch.object(server, "chat_tokens_used", return_value=20), \
             mock.patch.object(server, "pending_worker_pings") as pings:
            out = server.hook_prompt({"prompt": "hello", "session_id": "x"})
        self.assertEqual(out["decision"], "block")
        pings.assert_not_called()

    def test_notification_keying_notifies_once(self):
        test_pings = {}
        with patch.object(server, "_load_json", side_effect=lambda fn: test_pings if fn == "limit_pings.json" else {}), \
             patch.object(server, "_save_json", side_effect=lambda fn, data: test_pings.update(data) if fn == "limit_pings.json" else None), \
             patch.object(server, "_send_desktop_notification") as mock_send:
            k = "test_event|123"
            self.assertTrue(server.notify_desktop_event(k, "Title", "Message"))
            self.assertEqual(mock_send.call_count, 1)
            self.assertFalse(server.notify_desktop_event(k, "Title", "Message"))
            self.assertEqual(mock_send.call_count, 1)
            self.assertIn(k, test_pings)

    def test_hook_prompt_blocks_and_notifies(self):
        with patch.object(server, "_load_json", return_value={"chat_budget_tokens": 100}), \
             patch.object(server, "chat_tokens_used", return_value=150), \
             patch.object(server, "notify_desktop_event") as mock_notify:
            out = server.hook_prompt({"session_id": "sess_block_test"})
            self.assertEqual(out.get("decision"), "block")
            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            self.assertIn("sess_block_test", args[0])

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_subway_dir = server.SUBWAY_DIR
        server.SUBWAY_DIR = self.temp_dir

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_estimate_complexity_roles_raise_tier(self):
        # Base task is low (typo fix)
        c_base = server.estimate_complexity("fix typo in readme")
        self.assertEqual(c_base["tier"], "low")

        # Reviewer raises low -> medium
        c_rev = server.estimate_complexity("fix typo in readme", role="reviewer")
        self.assertEqual(c_rev["tier"], "medium")

        # Architect raises medium -> high
        c_arch = server.estimate_complexity("write unit tests", role="Lead Architect")
        self.assertEqual(c_arch["tier"], "high")

        # Security raises high -> frontier
        c_sec = server.estimate_complexity("refactor auth flow", role="security auditor")
        self.assertEqual(c_sec["tier"], "frontier")

    def test_unknown_allowance_counts_as_25(self):
        mock_providers = [
            {"id": "claude", "client": {"dispatchable": True}},
            {"id": "antigravity", "client": {"dispatchable": True}}
        ]
        # antigravity model has pool with 30 left; claude has unknown pool (None)
        with mock.patch.object(server, "_pool_left", side_effect=lambda provs, prov, m: 30.0 if prov == "antigravity" else None):
            res = server.route_task("hard mathematical proof", god=True, providers=mock_providers)
            # 30.0 left > 25.0 default for unknown, so antigravity should outrank claude if scarce/tied
            self.assertIn("picks", res)

    def test_model_stats_recorded_on_worker_finish(self):
        meta = {
            "id": "worker_test1",
            "provider": "claude",
            "model": "sonnet",
            "prompt": "edit function to fix bug",
            "role": "coder",
            "status": "running",
            "pid": 999999,
        }
        # 1. Landed outcome
        server._record_worker_stat(meta, {"status": "completed", "reason": "Patch applied to the working tree."})
        # 2. No change outcome (hallucination)
        server._record_worker_stat(meta, {"status": "failed", "reason": "Reported work, but no files changed."})
        # 3. Budget stopped
        server._record_worker_stat(meta, {"status": "budget_stopped", "reason": "Stopped at its budget"})
        # 4. Failed
        server._record_worker_stat(meta, {"status": "failed", "reason": "Syntax error in script"})

        stats = server._load_model_stats()
        self.assertEqual(len(stats), 4)
        outcomes = [s["outcome"] for s in stats]
        self.assertEqual(outcomes, ["landed", "no_change", "budget_stopped", "failed"])
        self.assertEqual(stats[0]["provider"], "claude")
        self.assertEqual(stats[0]["model"], "sonnet")
        self.assertIn("tier", stats[0])

    def test_routing_weighs_landed_rate_and_downranks_no_change(self):
        mock_providers = [{"id": "claude", "client": {"dispatchable": True}}]
        tier = "medium"
        role = "coder"
        jt = server._job_type(tier, role)

        # < 5 records: benchmark stays as prior
        stats = [
            {"provider": "claude", "model": "sonnet", "job_type": jt, "outcome": "failed"}
            for _ in range(4)
        ]
        with mock.patch.object(server, "_load_model_stats", return_value=stats):
            res = server.route_task("write a test", role="coder", providers=mock_providers)
            # Sonnet coding strength is 4, >= need (3)
            self.assertTrue(any(p["model"] == "sonnet" for p in res["picks"]))

        # >= 5 records with low landed rate weighs strength down
        bad_stats = [
            {"provider": "claude", "model": "sonnet", "job_type": jt, "outcome": "failed"}
            for _ in range(5)
        ]
        with mock.patch.object(server, "_load_model_stats", return_value=bad_stats):
            res_bad = server.route_task("write a test", role="coder", providers=mock_providers)
            # Landed rate is 0%, strength drops to 0.0, so sonnet is not capable of meeting need=3
            for pick in res_bad.get("picks", []):
                if pick.get("model") == "sonnet":
                    # If present, it was only included because nothing else met need
                    break

        # High no_change rate on code edit tasks down-ranks
        no_change_stats = [
            {"provider": "claude", "model": "sonnet", "job_type": jt, "outcome": "no_change"}
            for _ in range(5)
        ]
        with mock.patch.object(server, "_load_model_stats", return_value=no_change_stats):
            res_nc = server.route_task("edit code to fix bug", role="coder", providers=mock_providers)
            self.assertIn("picks", res_nc)

class TestCompaction(unittest.TestCase):
    """Tests for compaction threshold maths, quiet rule, context measurement, and modes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="subway_compaction_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_threshold_maths(self):
        # High complexity -> 60% threshold
        high_prompt = "Perform complete architecture refactor across multiple files and protocol migration"
        pct_high = server.compaction_threshold_pct(high_prompt)
        self.assertEqual(pct_high, 60.0)
        self.assertEqual(server.compaction_threshold(high_prompt, 200000), 120000)
        # On a 1M window the token point (300K by default) comes first.
        with patch.object(server, "compact_at_tokens", return_value=300000):
            self.assertEqual(server.compaction_threshold(high_prompt, 1000000), 300000)

        # Normal/low complexity -> 75% threshold
        low_prompt = "Fix a typo in the comment"
        pct_low = server.compaction_threshold_pct(low_prompt)
        self.assertEqual(pct_low, 75.0)
        self.assertEqual(server.compaction_threshold(low_prompt, 200000), 150000)
        with patch.object(server, "compact_at_tokens", return_value=1000000):
            self.assertEqual(server.compaction_threshold(low_prompt, 1000000), 750000)

    def test_quiet_rule(self):
        # Window = 200,000 -> half window = 100,000
        # If remaining chat budget < 100,000: stay quiet
        self.assertTrue(server.is_compaction_quiet(99999, 200000))
        self.assertTrue(server.is_compaction_quiet(50000, 200000))
        self.assertFalse(server.is_compaction_quiet(100000, 200000))
        self.assertFalse(server.is_compaction_quiet(150000, 200000))
        self.assertFalse(server.is_compaction_quiet(None, 200000))

        # Test quiet rule suppresses compaction_notice
        cfg = {"chat_budget_tokens": 100000, "compaction": "suggest"}
        with patch.object(server, "_load_json", return_value=cfg), \
             patch.object(server, "chat_tokens_used", return_value=60000), \
             patch.object(server, "chat_context_usage", return_value=(180000, 200000)):
            # Remaining budget is 40,000 < half window (100,000) -> None
            notice = server.compaction_notice("test_sid", "simple prompt")
            self.assertIsNone(notice)

    def test_compaction_modes_and_context_usage(self):
        # Test config modes
        with patch.object(server, "_load_json", return_value={}):
            self.assertEqual(server.get_compaction(), "suggest")

        with patch.object(server, "_load_json", return_value={"compaction": "auto"}):
            self.assertEqual(server.get_compaction(), "auto")

        with patch.object(server, "_load_json", return_value={"compaction": "never"}):
            self.assertEqual(server.get_compaction(), "never")
            # 'never' says nothing
            with patch.object(server, "chat_context_usage", return_value=(190000, 200000)):
                self.assertIsNone(server.compaction_notice("test_sid", "test prompt"))

        # Context usage parsing for Claude Code (200k vs 1m)
        t_file = os.path.join(self.tmpdir, "session.jsonl")
        with open(t_file, "w") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-3-5-sonnet",
                    "usage": {
                        "input_tokens": 100000,
                        "cache_read_input_tokens": 20000,
                        "cache_creation_input_tokens": 30000
                    }
                }
            }) + "\n")
        tokens, window = server.chat_context_usage(t_file)
        self.assertEqual(tokens, 150000)
        self.assertEqual(window, 200000)

        # 1m window model
        with open(t_file, "a") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-3-7-sonnet-[1m]",
                    "usage": {
                        "input_tokens": 600000,
                        "cache_read_input_tokens": 50000,
                        "cache_creation_input_tokens": 50000
                    }
                }
            }) + "\n")
        tokens, window = server.chat_context_usage(t_file)
        self.assertEqual(tokens, 700000)
        self.assertEqual(window, 1000000)

    def test_suggest_and_auto_notices(self):
        # Test 'suggest' ping and desktop notification
        cfg = {"compaction": "suggest"}
        with patch.object(server, "_load_json", side_effect=lambda f: cfg if f == "config.json" else {}), \
             patch.object(server, "_save_json"), \
             patch.object(server, "chat_context_usage", return_value=(160000, 200000)), \
             patch.object(server, "read_track_entries", return_value=[{"message": "refactor auth"}]), \
             patch.object(server, "notify_desktop_event") as mock_notify:
            notice = server.compaction_notice("session_suggest", "normal prompt")
            self.assertIsNotNone(notice)
            self.assertIn("This chat holds 160K tokens of context (80% of its window), re-read on every turn. "
                          "Compact now: /compact keep refactor auth", notice)
            mock_notify.assert_called_once()

        # Test 'auto' adds claude note
        cfg_auto = {"compaction": "auto"}
        with patch.object(server, "_load_json", side_effect=lambda f: cfg_auto if f == "config.json" else {}), \
             patch.object(server, "_save_json"), \
             patch.object(server, "chat_context_usage", return_value=(160000, 200000)), \
             patch.object(server, "read_track_entries", return_value=[{"message": "refactor auth"}]), \
             patch.object(server, "notify_desktop_event"), \
             patch.object(server, "compact_opencode_workers") as mock_compact_opencode:
            notice_auto = server.compaction_notice("session_auto", "normal prompt")
            self.assertIsNotNone(notice_auto)
            self.assertIn("In auto mode Claude Code compacts by itself near 300,000 tokens; Subway can't run /compact", notice_auto)
            mock_compact_opencode.assert_called_once()

    def test_window_survives_compaction(self):
        t_file = os.path.join(self.tmpdir, "s.jsonl")
        with open(t_file, "w") as f:
            for n in (700000, 180000):  # Before and after a compaction, model name without [1m].
                f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5",
                                    "usage": {"input_tokens": n}}}) + "\n")
        self.assertEqual(server.chat_context_usage(t_file), (180000, 1000000))

    def test_focus_line_skips_compaction_markers(self):
        entries = [{"message": "Compacted (manual) at 12:57. Full history: read_chat(x, segment=2)"},
                   {"message": "ship the Kimi gauge"}]
        with patch.object(server, "read_track_entries", return_value=entries):
            self.assertEqual(server.compact_focus_line(), "keep ship the Kimi gauge")

    def _sync(self, cfg, mode, at=300000):
        saved = {}
        with patch.object(server, "CLAUDE_DIR", self.tmpdir), \
             patch.object(server, "_load_json", side_effect=lambda f: cfg if f == "config.json" else {}), \
             patch.object(server, "_save_json", side_effect=lambda f, d: saved.update(d)):
            note = server._sync_claude_autocompact(mode, at)
        path = os.path.join(self.tmpdir, "settings.json")
        return note, (json.load(open(path)) if os.path.exists(path) else {}), saved

    def test_auto_sets_and_removes_claude_compaction_point(self):
        with open(os.path.join(self.tmpdir, "settings.json"), "w") as f:
            json.dump({"hooks": {"PreCompact": []}}, f)
        note, data, saved = self._sync({}, "auto")
        self.assertIn("300,000", note)
        self.assertEqual(data, {"hooks": {"PreCompact": []}, "autoCompactWindow": 300000})
        note, data, _ = self._sync({"claude_autocompact_set": 300000}, "suggest")
        self.assertEqual(data, {"hooks": {"PreCompact": []}})

    def test_auto_leaves_the_users_own_compaction_point(self):
        with open(os.path.join(self.tmpdir, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 500000}, f)
        note, data, _ = self._sync({}, "auto")
        self.assertIn("left it alone", note)
        self.assertEqual(data["autoCompactWindow"], 500000)
        _, data, _ = self._sync({}, "never")
        self.assertEqual(data["autoCompactWindow"], 500000)


class TestCodePacket(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="subway_code_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unresolved_name_refuses(self):
        res = server.t_spin_worker(role="Tester", prompt="Check code", project_dir=self.tmpdir,
                                   code=["nonexistent_symbol_xyz"])
        self.assertTrue(res.startswith("Error:"), res)
        self.assertIn("nonexistent_symbol_xyz", res)

    def test_size_limit_refuses(self):
        large_file = os.path.join(self.tmpdir, "big.py")
        with open(large_file, "w") as f:
            f.write("# comment\n" + ("x = 1\n" * 15000))
        # 1-15000 lines is > 60000 chars
        res = server.t_spin_worker(role="Tester", prompt="Check code", project_dir=self.tmpdir,
                                   code=["big.py:1-14000"])
        self.assertTrue(res.startswith("Error:"), res)
        self.assertIn("60000", res)

    def test_changed_file_refuses(self):
        test_py = os.path.join(self.tmpdir, "logic.py")
        with open(test_py, "w") as f:
            f.write("def my_func():\n    return 42\n")

        with mock.patch.object(server, "_worker_command", return_value="echo test"):
            res = server.t_spin_worker(role="Tester", prompt="Check logic", project_dir=self.tmpdir,
                                       provider="claude", code=["logic.py:my_func"])
        self.assertIn("APPROVAL REQUIRED", res)
        # Extract worker_id
        m = re.search(r"worker_[0-9a-fA-F]+", res)
        self.assertIsNotNone(m)
        worker_id = m.group(0)

        # Mutate the file
        with open(test_py, "w") as f:
            f.write("def my_func():\n    return 100\n")

        launch_res = server._launch_worker(worker_id)
        self.assertTrue(launch_res.startswith("Error:"), launch_res)
        self.assertIn("changed", launch_res)

    def test_js_function_extraction(self):
        js_file = os.path.join(self.tmpdir, "component.js")
        with open(js_file, "w") as f:
            f.write("import foo from 'bar';\n\nexport function myWidget(props) {\n  const x = 1;\n  return x;\n}\n\nfunction other() {}\n")

        with mock.patch.object(server, "_worker_command", return_value="echo test") as mock_cmd:
            res = server.t_spin_worker(role="Tester", prompt="Build UI", project_dir=self.tmpdir,
                                       provider="claude", code=["component.js:myWidget"])
            self.assertIn("APPROVAL REQUIRED", res)
            called_prompt = mock_cmd.call_args[0][7]
            self.assertIn("=== CODE (all you need: do not search) ===", called_prompt)
            self.assertIn("export function myWidget(props) {", called_prompt)
            self.assertIn("component.js:3-6", called_prompt)


class TestHudAutostart(unittest.TestCase):
    """Test HUD autostart on MCP server start and that a running HUD isn't started twice."""

    def test_running_hud_not_started_twice(self):
        """When HUD is already running on the port, autostart should not spawn a process."""
        with patch.object(server, "_load_json", return_value={"hud_autostart": True}), \
             patch.object(server, "_hud_probe", return_value={"service": "subway-context", "version": server.VERSION}), \
             patch("subprocess.Popen") as mock_popen:
            server._autostart_hud()
            mock_popen.assert_not_called()

    def test_hud_starts_when_not_running(self):
        """When nothing answers healthz, HUD process should start with SUBWAY_HUD_NO_OPEN=1 and start_new_session=True."""
        with patch.object(server, "_load_json", return_value={"hud_autostart": True}), \
             patch.object(server, "_hud_probe", return_value=None), \
             patch("subprocess.Popen") as mock_popen:
            server._autostart_hud()
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("env", {}).get("SUBWAY_HUD_NO_OPEN"), "1")

    def test_hud_autostart_disabled_config(self):
        """When hud_autostart is false in config, do not start HUD."""
        with patch.object(server, "_load_json", return_value={"hud_autostart": False}), \
             patch.object(server, "_hud_probe", return_value=None), \
             patch("subprocess.Popen") as mock_popen:
            server._autostart_hud()
            mock_popen.assert_not_called()

    def test_hud_autostart_cli(self):
        """subway-context hud-autostart on|off sets config value."""
        with patch.object(server, "_save_json") as mock_save, \
             patch.object(server, "_load_json", return_value={}):
            ret = server.run_cli(["hud-autostart", "off"])
            self.assertEqual(ret, 0)
            mock_save.assert_called_with("config.json", {"hud_autostart": False})

            ret = server.run_cli(["hud-autostart", "on"])
            self.assertEqual(ret, 0)
            mock_save.assert_called_with("config.json", {"hud_autostart": True})


class DesktopNotificationTests(unittest.TestCase):
    """Test desktop notification integration on Linux, Windows, and macOS."""

    def test_linux_notify_with_action(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            res = mock.MagicMock()
            if cmd[0] == "/usr/bin/notify-send" and cmd[1:] == ["--help"]:
                res.stdout = "Usage: notify-send [OPTION...] ... --action=... -w, --wait"
                res.stderr = ""
            elif len(cmd) > 1 and cmd[0] == "/usr/bin/notify-send" and "--action=open=Open" in cmd:
                res.stdout = "open\n"
                res.stderr = ""
            else:
                res.stdout = ""
                res.stderr = ""
            return res

        def fake_thread(target, daemon=True):
            t = mock.MagicMock()
            t.start.side_effect = target
            return t

        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("threading.Thread", side_effect=fake_thread):
            server._send_desktop_notification("Job Done", "All tasks finished")

        self.assertIn(["/usr/bin/notify-send", "--help"], calls)
        self.assertIn(["/usr/bin/notify-send", "--app-name", "Subway Context", "--action=open=Open", "--wait", "Job Done", "All tasks finished"], calls)
        self.assertIn(["xdg-open", "http://127.0.0.1:7890/#workers"], calls)

    def test_linux_notify_without_action(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            res = mock.MagicMock()
            res.stdout = "notify-send without action support"
            res.stderr = ""
            return res

        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", side_effect=fake_run):
            server._send_desktop_notification("Job Done", "All tasks finished")

        self.assertIn(["/usr/bin/notify-send", "--help"], calls)
        self.assertIn(["/usr/bin/notify-send", "Job Done", "All tasks finished"], calls)
        self.assertNotIn(["xdg-open", "http://127.0.0.1:7890/#workers"], calls)

    def test_linux_notify_missing(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", return_value=None), \
             patch("subprocess.run") as mock_run:
            server._send_desktop_notification("Job Done", "All tasks finished")
            mock_run.assert_not_called()

    def test_windows_native_toast_escaping(self):
        title = 'Worker "Alpha" & \'Beta\' <urgent>'
        message = 'Needs "help" & \'details\' > now'

        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "win32"), \
             patch("subprocess.run") as mock_run:
            server._send_desktop_notification(title, message)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            kwargs = mock_run.call_args[1]

            self.assertEqual(cmd[:5], ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command"])
            ps_script = cmd[5]

            # Verify XML escaping and PowerShell escaping
            self.assertIn("&quot;Alpha&quot; &amp; &apos;Beta&apos; &lt;urgent&gt;", ps_script)
            self.assertIn("Needs &quot;help&quot; &amp; &apos;details&apos; &gt; now", ps_script)
            self.assertIn('activationType="protocol" launch="http://127.0.0.1:7890/#workers"', ps_script)
            self.assertIn(r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe", ps_script)
            self.assertEqual(kwargs.get("creationflags"), getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_macos_notifications(self):
        # With terminal-notifier
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "darwin"), \
             patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), \
             patch("subprocess.run") as mock_run:
            server._send_desktop_notification("Hi", "There")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "/usr/local/bin/terminal-notifier")
            self.assertIn("-open", args)

        # Without terminal-notifier (osascript fallback with quote escaping)
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.platform", "darwin"), \
             patch("shutil.which", return_value=None), \
             patch.object(server.os.path, "isfile", return_value=False), \
             patch("subprocess.run") as mock_run:
            server._send_desktop_notification('Alert "1"', 'Msg with "quotes"')
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[:2], ["osascript", "-e"])
            self.assertIn(r'Alert \"1\"', args[2])
            self.assertIn(r'Msg with \"quotes\"', args[2])

    def test_install_ensure_notifier(self):
        import install

        # Windows
        with patch("sys.platform", "win32"), patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "present")
            output = " ".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("Windows native toasts need nothing extra", output)

        # Linux with --action
        help_mock = mock.MagicMock(stdout="--action=open", stderr="")
        with patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", return_value=help_mock), \
             patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "present")

        # Linux without --action
        help_mock = mock.MagicMock(stdout="no action flag", stderr="")
        with patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", return_value=help_mock), \
             patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "no-action")
            output = " ".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("can't open the HUD on click", output)

        # Linux missing notify-send (apt)
        with patch("sys.platform", "linux"), \
             patch("shutil.which", side_effect=lambda bin_name: "/usr/bin/apt" if bin_name == "apt" else None), \
             patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "missing")
            output = " ".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("apt install libnotify-bin", output)

        # Linux missing notify-send (dnf)
        with patch("sys.platform", "linux"), \
             patch("shutil.which", side_effect=lambda bin_name: "/usr/bin/dnf" if bin_name == "dnf" else None), \
             patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "missing")
            output = " ".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("dnf install libnotify", output)

        
class TestChatSegments(unittest.TestCase):
    def test_claude_segments_two_boundaries(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "turn one in seg 1"}]}}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "turn two in seg 2"}]}}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "turn three in seg 3"}]}}) + "\n")
            path = f.name

        try:
            chat = {"source": "claude", "id": "test_claude_sid", "path": path}
            # Segment 1
            t1, lbl1, tot1 = server.chat_segments_info(chat, segment=1)
            self.assertEqual(lbl1, "1")
            self.assertEqual(tot1, 3)
            self.assertIn("turn one in seg 1", str(t1))
            self.assertNotIn("turn two in seg 2", str(t1))
            self.assertNotIn("turn three in seg 3", str(t1))

            # Segment 2
            t2, lbl2, tot2 = server.chat_segments_info(chat, segment="2")
            self.assertEqual(lbl2, "2")
            self.assertEqual(tot2, 3)
            self.assertIn("turn two in seg 2", str(t2))
            self.assertNotIn("turn one in seg 1", str(t2))
            self.assertNotIn("turn three in seg 3", str(t2))

            # Segment 3
            t3, lbl3, tot3 = server.chat_segments_info(chat, segment=3)
            self.assertEqual(lbl3, "3")
            self.assertEqual(tot3, 3)
            self.assertIn("turn three in seg 3", str(t3))
            self.assertNotIn("turn one in seg 1", str(t3))
            self.assertNotIn("turn two in seg 2", str(t3))

            # segment='latest' is n (3)
            t_lat, lbl_lat, tot_lat = server.chat_segments_info(chat, segment="latest")
            self.assertEqual(lbl_lat, "3")
            self.assertEqual(tot_lat, 3)
            self.assertEqual(t_lat, t3)

            # segment='all' returns everything with a marker line at each boundary
            t_all, lbl_all, tot_all = server.chat_segments_info(chat, segment="all")
            self.assertEqual(lbl_all, "all")
            self.assertEqual(tot_all, 3)
            all_str = str(t_all)
            self.assertIn("turn one in seg 1", all_str)
            self.assertIn("turn two in seg 2", all_str)
            self.assertIn("turn three in seg 3", all_str)
            self.assertIn("--- compaction boundary ---", all_str)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_opencode_segments_with_compaction(self):
        db_path = os.path.join(server.OPENCODE_DIR, "opencode.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS session_message (session_id TEXT, seq INTEGER, type TEXT, data TEXT, time_created REAL)")
        sid = "opencode_compaction_sid"
        con.execute("DELETE FROM session_message WHERE session_id=?", (sid,))
        con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
                    (sid, 1, "user", json.dumps({"text": "prompt before compact"}), 100.0))
        con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
                    (sid, 2, "compaction", json.dumps({}), 101.0))
        con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
                    (sid, 3, "user", json.dumps({"text": "prompt after compact"}), 102.0))
        con.commit()
        con.close()

        chat = {"source": "opencode", "id": sid}
        t1, lbl1, tot1 = server.chat_segments_info(chat, segment=1)
        self.assertEqual(lbl1, "1")
        self.assertEqual(tot1, 2)
        self.assertIn("prompt before compact", str(t1))
        self.assertNotIn("prompt after compact", str(t1))

        t2, lbl2, tot2 = server.chat_segments_info(chat, segment="latest")
        self.assertEqual(lbl2, "2")
        self.assertEqual(tot2, 2)
        self.assertIn("prompt after compact", str(t2))
        self.assertNotIn("prompt before compact", str(t2))

        t_all, lbl_all, tot_all = server.chat_segments_info(chat, segment="all")
        self.assertEqual(tot_all, 2)
        self.assertIn("--- compaction boundary ---", str(t_all))

    def test_unknown_provider(self):
        chat = {"source": "some_future_ai", "id": "unknown_123"}
        info = server.chat_segments_info(chat, segment=1)
        data, lbl, tot = info[0], info[1], info[2]
        self.assertEqual(tot, 1)
        self.assertEqual(lbl, "1")
        self.assertEqual(getattr(info, "note", None), "compaction history not supported for this provider")


class TestAutoApproveNeverAsks(unittest.TestCase):
    def test_auto_approve_request_is_answered_without_asking(self):
        with mock.patch.object(server, "_write_json_atomic"), \
             mock.patch.object(server, "_answer_permission") as answer, \
             mock.patch.object(server, "notify_if_still_waiting") as alert, \
             mock.patch.object(server, "t_broadcast_track") as broadcast:
            server.request_permission({"id": "worker_abc123", "permission_mode": "auto_approve"}, "command", "ls")
        answer.assert_called_once()
        self.assertEqual(answer.call_args[0][1], "allow")
        alert.assert_not_called()
        broadcast.assert_not_called()


class TestOpenCodeCalibration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="subway_oc_calib_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_fake_opencode2(self):
        script_path = os.path.join(self.test_dir, "fake_opencode2.py")
        code = (
            "import sys, json\n"
            "def send(msg):\n"
            "    sys.stdout.write(json.dumps(msg) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "prompt_id = None\n"
            "turn = 0\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line: continue\n"
            "    try: req = json.loads(line)\n"
            "    except Exception: continue\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        send({'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': 1}})\n"
            "    elif method == 'session/new':\n"
            "        send({'jsonrpc': '2.0', 'id': req_id, 'result': {'sessionId': 'fake_ses_01'}})\n"
            "    elif method == 'session/prompt':\n"
            "        prompt_id = req_id\n"
            "        turn += 1\n"
            "        if turn == 1:\n"
            "            send({'jsonrpc': '2.0', 'id': prompt_id, 'result': {'response': 'I will edit server.py to add the feature.'}})\n"
            "        else:\n"
            "            send({'jsonrpc': '2.0', 'method': 'session/update',\n"
            "                  'params': {'sessionId': 'fake_ses_01', 'update': {'sessionUpdate': 'tool_call', 'toolCall': {'title': 'edit'}}}})\n"
            "            send({'jsonrpc': '2.0', 'id': prompt_id, 'result': {'response': 'DONE: edited server.py'}})\n"
            "    elif method == 'session/cancel':\n"
            "        sys.exit(0)\n"
        )
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
        return script_path

    def test_opencode_plan_reply_triggers_continue(self):
        fake_bin = self._create_fake_opencode2()
        proc = subprocess.Popen([sys.executable, fake_bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = io.BytesIO()
        run = {"continues": 0, "cap": 10000, "spent": 100}
        meta = {"prompt": "Edit server.py to calibrate OpenCode", "project_dir": self.test_dir}
        stopped = {"flag": False}

        server._pump_opencode(proc, out, run, meta, stopped)

        self.assertEqual(run["continues"], 1)
        self.assertEqual(run["response"], "DONE: edited server.py")
        logs = out.getvalue().decode("utf-8")
        self.assertIn("[continue] plan", logs)

    def test_opencode_token_cap_blocks_continue(self):
        fake_bin = self._create_fake_opencode2()
        proc = subprocess.Popen([sys.executable, fake_bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = io.BytesIO()
        # Cap is 1000, spent is 700 (which is >= 1000 * 2 // 3 = 666)
        run = {"continues": 0, "cap": 1000, "spent": 700}
        meta = {"prompt": "Edit server.py to calibrate OpenCode", "project_dir": self.test_dir}
        stopped = {"flag": False}

        server._pump_opencode(proc, out, run, meta, stopped)

        self.assertEqual(run["continues"], 0)
        self.assertEqual(run["response"], "I will edit server.py to add the feature.")
        logs = out.getvalue().decode("utf-8")
        self.assertNotIn("[continue] plan", logs)


class TestHudOpenOnUse(unittest.TestCase):
    def setUp(self):
        server._hud_opened_on_use = False
        server._hud_open_thread = None

    def tearDown(self):
        server._hud_opened_on_use = False
        server._hud_open_thread = None

    def _call_tool(self):
        req = {
            "jsonrpc": "2.0",
            "id": "test-call",
            "method": "tools/call",
            "params": {"name": "hud_status", "arguments": {}}
        }
        with patch.object(server, "enforce_budgets"), \
             patch.object(server, "_load_json", return_value={"hud_open_on_use": True}):
            res = server.handle(req)
        if server._hud_open_thread:
            server._hud_open_thread.join(timeout=2)
        return res

    def test_first_call_opens_once_second_does_not(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(server, "_hud_probe", return_value={"version": server.VERSION}), \
             patch.object(server, "_open_hud") as mock_open:
            os.environ.pop("SUBWAY_NO_NOTIFY", None)
            os.environ.pop("SUBWAY_WORKER", None)

            self._call_tool()
            mock_open.assert_called_once()

            self._call_tool()
            mock_open.assert_called_once()

    def test_skipped_when_setting_off(self):
        req = {
            "jsonrpc": "2.0",
            "id": "test-call",
            "method": "tools/call",
            "params": {"name": "hud_status", "arguments": {}}
        }
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(server, "_hud_probe", return_value={"version": server.VERSION}), \
             patch.object(server, "_open_hud") as mock_open, \
             patch.object(server, "enforce_budgets"), \
             patch.object(server, "_load_json", return_value={"hud_open_on_use": False}):
            os.environ.pop("SUBWAY_NO_NOTIFY", None)
            os.environ.pop("SUBWAY_WORKER", None)

            server.handle(req)
            if server._hud_open_thread:
                server._hud_open_thread.join(timeout=2)
            mock_open.assert_not_called()

    def test_skipped_when_subway_worker_set(self):
        with patch.dict(os.environ, {"SUBWAY_WORKER": "1"}, clear=False), \
             patch.object(server, "_hud_probe", return_value={"version": server.VERSION}), \
             patch.object(server, "_open_hud") as mock_open:
            os.environ.pop("SUBWAY_NO_NOTIFY", None)

            self._call_tool()
            mock_open.assert_not_called()

    def test_cli_hud_open_on_use(self):
        with patch.object(server, "_save_json") as mock_save, \
             patch.object(server, "_load_json", return_value={}):
            ret = server.run_cli(["hud-open-on-use", "off"])
            self.assertEqual(ret, 0)
            mock_save.assert_called_with("config.json", {"hud_open_on_use": False})

            ret = server.run_cli(["hud-open-on-use", "on"])
            self.assertEqual(ret, 0)
            mock_save.assert_called_with("config.json", {"hud_open_on_use": True})


class TestLedgerEndpoint(unittest.TestCase):
    def test_api_ledger_answers(self):
        handler = server.SubwayHUDHandler.__new__(server.SubwayHUDHandler)
        handler.path = "/api/ledger?name=main"
        handler.headers = {}
        sent = {}
        handler.send_json = lambda data, status=200: sent.update({"data": data, "status": status})
        with patch.object(handler, "_gate", return_value=True), \
             patch.object(server, "read_track_entries", return_value=[]):
            handler.do_GET()
        self.assertIn("entries", sent.get("data", {}))
        self.assertEqual(sent["data"]["track"], "main")






class TestCopilotBudgets(unittest.TestCase):
    def setUp(self):
        import server
        server._copilot_raw.clear()  # Each test needs its own gh reply, not a cached one.
        server._copilot_models.clear()
        server._model_catalog.clear()

    def test_copilot_view_mocked_gh(self):
        from unittest.mock import patch, MagicMock
        import json
        import server

        mock_payload = {
            "copilot_plan": "individual",
            "quota_reset_date": "2026-12-01",
            "quota_snapshots": {
                "chat": {
                    "percent_remaining": 75.0,
                    "remaining": 1500,
                    "entitlement": 2000,
                    "unlimited": False,
                },
                "completions": {
                    "percent_remaining": 100.0,
                    "remaining": 0,
                    "entitlement": 0,
                    "unlimited": True,
                },
                "premium_interactions": {
                    "percent_remaining": 0.0,
                    "remaining": 0,
                    "entitlement": 0,
                    "unlimited": False,
                },
            },
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(mock_payload)

        with patch("subprocess.run", return_value=mock_proc):
            view = server._copilot_view({}, refresh=True)
            self.assertEqual(view["state"], "live")
            self.assertEqual(view["plan"], "individual")
            pool_map = {p["name"]: p for p in view["pools"]}
            self.assertIn("Chat", pool_map)
            self.assertIn("Completions (inline editor suggestions Subway shows but never spends)", pool_map)
            self.assertNotIn("Premium requests", pool_map)

            chat_win = pool_map["Chat"]["windows"][0]
            self.assertEqual(chat_win["label"], "Monthly")
            self.assertEqual(chat_win["left_pct"], 75.0)
            self.assertEqual(chat_win["resets_at"], server._parse_ts("2026-12-01"))

            comp_win = pool_map["Completions (inline editor suggestions Subway shows but never spends)"]["windows"][0]
            self.assertEqual(comp_win["label"], "Monthly")
            self.assertIn("not applied", comp_win.get("note", "").lower())

    def test_copilot_view_failure_fallback(self):
        from unittest.mock import patch
        import server

        with patch("subprocess.run", side_effect=Exception("gh not found")):
            view = server._copilot_view({}, refresh=True)
            self.assertEqual(view["state"], "detected")
            self.assertEqual(view["pools"], [])
            self.assertIn("could not be read", view["note"])

    def test_copilot_models_usable_filter(self):
        from unittest.mock import patch
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "run1")
            os.makedirs(model_dir, exist_ok=True)
            models_file = os.path.join(model_dir, "models.json")
            data = [
                {
                    "id": "gpt-4o",
                    "name": "GPT-4o",
                    "vendor": "Azure OpenAI",
                    "is_chat_default": False,
                    "model_picker_price_category": "medium",
                    "policy": {"state": "enabled"}
                },
                {
                    "id": "claude-3.5-sonnet",
                    "name": "Claude 3.5 Sonnet",
                    "vendor": "Anthropic",
                    "is_chat_default": True,
                    "model_picker_price_category": "high",
                    "policy": None
                },
                {
                    "id": "disabled-model",
                    "name": "Disabled",
                    "vendor": "VendorX",
                    "is_chat_default": False,
                    "model_picker_price_category": "low",
                    "policy": {"state": "disabled"}
                },
                {
                    "id": "null-policy-not-default",
                    "name": "Ignored",
                    "vendor": "VendorY",
                    "is_chat_default": False,
                    "policy": None
                }
            ]
            with open(models_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                models = server._copilot_models()
                self.assertEqual(len(models), 2)
                m_ids = [m["model"] for m in models]
                self.assertEqual(m_ids, ["gpt-4o", "claude-3.5-sonnet"])
                self.assertEqual(models[0], {
                    "model": "gpt-4o",
                    "name": "GPT-4o",
                    "vendor": "Azure OpenAI",
                    "price_band": "medium",
                    "default": False
                })
                self.assertEqual(models[1], {
                    "model": "claude-3.5-sonnet",
                    "name": "Claude 3.5 Sonnet",
                    "vendor": "Anthropic",
                    "price_band": "high",
                    "default": True
                })

    def test_copilot_models_newest_file_wins(self):
        from unittest.mock import patch
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            dir_code = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "older")
            dir_insiders = os.path.join(tmp_dir, "Library", "Application Support", "Code - Insiders", "User", "workspaceStorage", "ws2", "GitHub.copilot-chat", "debug-logs", "newer")
            os.makedirs(dir_code, exist_ok=True)
            os.makedirs(dir_insiders, exist_ok=True)

            file_older = os.path.join(dir_code, "models.json")
            file_newer = os.path.join(dir_insiders, "models.json")

            with open(file_older, "w", encoding="utf-8") as f:
                json.dump([{"id": "older-model", "name": "Old", "vendor": "V1", "is_chat_default": True}], f)
            with open(file_newer, "w", encoding="utf-8") as f:
                json.dump([{"id": "newer-model", "name": "New", "vendor": "V2", "is_chat_default": True}], f)

            os.utime(file_older, (1000.0, 1000.0))
            os.utime(file_newer, (2000.0, 2000.0))

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                models = server._copilot_models()
                self.assertEqual(len(models), 1)
                self.assertEqual(models[0]["model"], "newer-model")

    def test_copilot_models_missing_file_returns_empty_and_view_has_note(self):
        from unittest.mock import patch
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                models = server._copilot_models()
                self.assertEqual(models, [])

                view = server._copilot_view({}, refresh=True)
                self.assertEqual(view["models"], [])
                self.assertIn("Open Copilot Chat in VS Code once to list its models.", view["note"])

    def test_catalog_includes_copilot_entries(self):
        from unittest.mock import patch
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "run1")
            os.makedirs(model_dir, exist_ok=True)
            models_file = os.path.join(model_dir, "models.json")
            with open(models_file, "w", encoding="utf-8") as f:
                json.dump([{
                    "id": "copilot-gpt-4o",
                    "name": "GPT-4o",
                    "vendor": "OpenAI",
                    "model_picker_price_category": "medium",
                    "is_chat_default": True,
                    "policy": {"state": "enabled"}
                }], f)

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                server._model_catalog.clear()
                catalog = server.model_catalog()
                copilot_entries = [m for m in catalog if m.get("provider") == "copilot"]
                self.assertTrue(len(copilot_entries) >= 1)
                entry = next((m for m in copilot_entries if m.get("model") == "copilot-gpt-4o"), None)
                self.assertIsNotNone(entry)
                self.assertEqual(entry["coding"], 2)
                self.assertEqual(entry["reasoning"], 2)
                self.assertEqual(entry["writing"], 2)
                self.assertEqual(entry["price_band"], "medium")
                self.assertTrue(entry["default"])

    def test_copilot_view_carries_models(self):
        from unittest.mock import patch, MagicMock
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "run1")
            os.makedirs(model_dir, exist_ok=True)
            models_file = os.path.join(model_dir, "models.json")
            with open(models_file, "w", encoding="utf-8") as f:
                json.dump([{
                    "id": "copilot-claude-sonnet",
                    "name": "Claude 3.5 Sonnet",
                    "vendor": "Anthropic",
                    "model_picker_price_category": "high",
                    "is_chat_default": True,
                    "policy": {"state": "enabled"}
                }], f)

            mock_payload = {
                "copilot_plan": "individual",
                "quota_reset_date": "2026-12-01",
                "quota_snapshots": {
                    "chat": {"percent_remaining": 80.0, "entitlement": 100, "remaining": 80, "unlimited": False}
                }
            }
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = json.dumps(mock_payload)

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand), \
                 patch("subprocess.run", return_value=mock_proc):
                server._copilot_models.clear()
                server._copilot_raw.clear()
                view = server._copilot_view({}, refresh=True)
                self.assertEqual(view["state"], "live")
                self.assertEqual(len(view["models"]), 1)
                self.assertEqual(view["models"][0]["model"], "copilot-claude-sonnet")
                self.assertEqual(view["note"], "")


class TestKimiBudgets(unittest.TestCase):
    def setUp(self):
        import server
        server._kimi_raw.clear()  # Each test needs its own reply, not a cached one.

    def _cred(self, expires_in):  # expires_in seconds from now; returns temp token-file path.
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": "sk-kimi-test", "refresh_token": "r", "expires_at": time.time() + expires_in,
                       "scope": "", "token_type": "bearer", "expires_in": expires_in}, f)
        return path

    def _request(self, reply, status=200):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(reply).encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda s, a, b, c: False
        return mock.MagicMock(return_value=resp)

    def test_kimi_view_mocked_limits(self):
        reply = {"usages": {
            "limit_5h": {"used_ratio": 0.5, "reset_time": "2026-11-02T18:00:00+00:00"},
            "limit_7d": {"used_ratio": 0.25, "reset_time": "2026-11-08T00:00:00+00:00"},
            "limit_month_total": {"used_ratio": 0.9, "reset_time": "2026-12-01T00:00:00+00:00"},
            "limit_month_code": {"used_ratio": 0.4, "reset_time": "2026-12-01T00:00:00+00:00"},
        }, "boosterWallet": {"id": "w1"}}
        path = self._cred(expires_in=3600)
        with mock.patch.object(server.glob, "glob", return_value=[path]), \
             mock.patch("urllib.request.urlopen", self._request(reply)):
            view = server._kimi_view({}, refresh=True)
        os.remove(path)
        self.assertEqual(view["state"], "live")
        self.assertEqual(view["plan"], "Kimi Code")
        pool_map = {p["name"]: p for p in view["pools"]}
        self.assertEqual(set(pool_map), {"5-hour", "Weekly", "Monthly total", "Monthly code"})
        self.assertEqual(pool_map["5-hour"]["windows"][0]["left_pct"], 50.0)
        self.assertEqual(pool_map["Weekly"]["windows"][0]["left_pct"], 75.0)
        self.assertEqual(view["pools"][0]["key"], "limit_5h")
        reset = server._dt.datetime.fromisoformat("2026-11-02T18:00:00+00:00").timestamp()
        self.assertEqual(pool_map["5-hour"]["windows"][0]["resets_at"], reset)
        self.assertNotIn("sk-kimi-test", json.dumps(view))  # Never leak the token.

    def test_kimi_view_empty_reply_signed_in(self):
        path = self._cred(expires_in=3600)
        opener = self._request({})
        with mock.patch.object(server.glob, "glob", return_value=[path]), \
             mock.patch("urllib.request.urlopen", opener):
            view = server._kimi_view({}, refresh=True)
        os.remove(path)
        self.assertEqual(view["state"], "live")
        self.assertEqual(view["pools"], [])
        self.assertEqual(view["note"], "Signed in. Kimi reported no plan limits for this account.")

    def test_kimi_view_expired_token(self):
        path = self._cred(expires_in=-3600)
        opener = self._request({"usages": {}})
        with mock.patch.object(server.glob, "glob", return_value=[path]), \
             mock.patch("urllib.request.urlopen", opener):
            view = server._kimi_view({}, refresh=True)
        os.remove(path)
        self.assertEqual(view["state"], "detected")
        self.assertEqual(view["pools"], [])
        self.assertEqual(view["note"], "Open Kimi Code to refresh your sign-in.")
        opener.assert_not_called()  # Expired token: never hit the endpoint.

    def test_kimi_view_other_failure_keeps_card(self):
        path = self._cred(expires_in=3600)
        with mock.patch.object(server.glob, "glob", return_value=[path]), \
             mock.patch("urllib.request.urlopen", side_effect=server.urllib.error.URLError("dns down")), \
             mock.patch.object(server.time, "sleep"):
            view = server._kimi_view({}, refresh=True)
        os.remove(path)
        self.assertEqual(view["note"], "Doesn't expose usage locally, and Subway can't dispatch workers to it yet.")
        self.assertEqual(view["pools"], [])


class TestHostEditor(unittest.TestCase):
    """The HUD opens in the editor hosting this chat, never in a different installed editor."""
    ENV_VSCODE = {"__CFBundleIdentifier": "com.microsoft.VSCode", "VSCODE_IPC_HOOK": "", "VSCODE_IPC_HOOK_CLI": ""}
    ENV_AGY = {"__CFBundleIdentifier": "com.google.antigravity-ide", "VSCODE_IPC_HOOK": "", "VSCODE_IPC_HOOK_CLI": ""}

    def test_host_with_extension_opens_its_own_link(self):
        ok = mock.MagicMock(returncode=0)
        with mock.patch.dict(os.environ, self.ENV_AGY), \
             mock.patch.object(server, "_cli_path", return_value="/x/agy-ide"), \
             mock.patch.object(server, "_has_extension", return_value=True), \
             mock.patch.object(server.subprocess, "run", return_value=ok) as run, \
             mock.patch("webbrowser.open") as browser:
            server._open_hud(7890)
        browser.assert_not_called()
        self.assertIn("antigravity-ide://subway.subway-context/open?port=7890", run.call_args[0][0])

    def test_host_without_extension_uses_browser_and_says_how(self):
        server.EDITOR_HINT.clear()
        with mock.patch.dict(os.environ, self.ENV_VSCODE), \
             mock.patch.object(server, "_cli_path", return_value="/x/code"), \
             mock.patch.object(server, "_has_extension", return_value=False), \
             mock.patch.object(server.subprocess, "run") as run, \
             mock.patch.object(server, "notify_desktop_event"), \
             mock.patch("webbrowser.open") as browser:
            server._open_hud(7890)
        browser.assert_called_once()
        run.assert_not_called()  # Never opens a different editor.
        self.assertIn("VS Code", server.EDITOR_HINT.get("text", ""))


class TestOpenCodeIdCollision(unittest.TestCase):
    def test_permission_request_with_colliding_id_keeps_session(self):
        """OpenCode's permission request can reuse our session/new id; it must never wipe the session id."""
        src = open(os.path.join(os.path.dirname(server.__file__), "server.py"), encoding="utf-8").read()
        body = src[src.index("def _pump_opencode("):]
        body = body[:body.index("\ndef ", 10)]
        for check in ("msg_id == init_id", "msg_id == session_new_id", "msg_id == curr_config_id", "msg_id == prompt_id"):
            line = next(l for l in body.splitlines() if check in l and l.strip().startswith("if "))
            self.assertIn("is_reply", line, check)



class TestResetTimes(unittest.TestCase):
    def test_copilot_reset_date_becomes_seconds(self):
        raw = {"copilot_plan": "individual", "quota_reset_date": "2026-12-01", "quota_snapshots": {
            "chat": {"entitlement": 100, "remaining": 40, "percent_remaining": 40.0, "unlimited": False}}}
        with patch.object(server, "_copilot_raw", return_value=raw):
            v = server._copilot_view({}, False)
        resets = [w["resets_at"] for p in v["pools"] for w in p["windows"]]
        self.assertTrue(resets and all(isinstance(r, float) for r in resets), resets)

    def test_fmt_reset_accepts_text(self):
        self.assertEqual(server._fmt_reset("2001-01-01"), "now")
        self.assertEqual(server._fmt_reset("not a date"), "unknown")
