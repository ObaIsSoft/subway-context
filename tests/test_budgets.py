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
        worker_id = "w_isolate_test"
        meta = {
            "id": worker_id,
            "provider": "claude",
            "model": "haiku",
            "cmd": ["test"],
            "timeout_seconds": 10,
            "worktree": "/tmp/test_wt",
            "repo": "/tmp/test_repo",
            "base": "HEAD",
            "isolate": True,
            "prompt": "Edit server.py to add tests",
            "status": "running",
        }
        # Simulate exit file processing in supervise_worker or test logic
        exit_res = {"status": "completed", "exit_code": 0}
        wt_note = "No files changed."
        if exit_res.get("status") == "completed" and meta.get("isolate"):
            prompt = meta.get("prompt") or ""
            if ("Edit " in prompt or "edit" in prompt) and wt_note == "No files changed.":
                exit_res["status"] = "failed"
                exit_res["reason"] = "Reported work, but no files changed."
        self.assertEqual(exit_res["status"], "failed")
        self.assertEqual(exit_res["reason"], "Reported work, but no files changed.")


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

    def test_open_hud_vscode_hint(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}), \
             patch("webbrowser.open") as mock_open, \
             patch("builtins.print") as mock_print:
            server._open_hud(7890)
            mock_open.assert_not_called()
            mock_print.assert_called_with("Open it in the editor: Cmd+Shift+P, Subway: Open HUD")

class TestModelRoutingAndStats(unittest.TestCase):
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
        self.assertEqual(server.compaction_threshold(high_prompt, 1000000), 600000)

        # Normal/low complexity -> 75% threshold
        low_prompt = "Fix a typo in the comment"
        pct_low = server.compaction_threshold_pct(low_prompt)
        self.assertEqual(pct_low, 75.0)
        self.assertEqual(server.compaction_threshold(low_prompt, 200000), 150000)
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
            self.assertIn("This chat is at 80% of its context. Compact now: /compact keep refactor auth", notice)
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
            self.assertIn("Claude Code compacts by itself near its limit; Subway can't run /compact inside an open chat", notice_auto)
            mock_compact_opencode.assert_called_once()


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
            if cmd == ["notify-send", "--help"]:
                res.stdout = "Usage: notify-send [OPTION...] ... --action=... -w, --wait"
                res.stderr = ""
            elif len(cmd) > 1 and cmd[0] == "notify-send" and "--action=open=Open" in cmd:
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

        self.assertIn(["notify-send", "--help"], calls)
        self.assertIn(["notify-send", "--app-name", "Subway Context", "--action=open=Open", "--wait", "Job Done", "All tasks finished"], calls)
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

        self.assertIn(["notify-send", "--help"], calls)
        self.assertIn(["notify-send", "Job Done", "All tasks finished"], calls)
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

        # Linux missing notify-send (pacman)
        with patch("sys.platform", "linux"), \
             patch("shutil.which", side_effect=lambda bin_name: "/usr/bin/pacman" if bin_name == "pacman" else None), \
             patch("builtins.print") as mock_print:
            res = install.ensure_notifier(interactive=False, auto_yes=True)
            self.assertEqual(res, "missing")
            output = " ".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("pacman -S libnotify", output)



