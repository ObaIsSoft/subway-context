import os as _sandbox_os, sys as _sandbox_sys  # Isolate HOME, SUBWAY_DIR and agent folders before server loads.
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401
import io
import json
import io
import os
import sqlite3
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import server


class TestCopilotWorkers(unittest.TestCase):
    def setUp(self):
        server._copilot_raw.clear()
        server._copilot_models.clear()
        server._model_catalog.clear()

    def test_parse_fixture(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "copilot_run.jsonl")
        with open(fixture_path, "r", encoding="utf-8") as f:
            content = f.read()
        run = server.parse_copilot_jsonl(content)
        self.assertEqual(run.get("response"), "OK")
        self.assertEqual(run.get("model_used"), "mai-code-1.1-flash")
        u = run.get("usage", {})
        self.assertAlmostEqual(u.get("credits"), 0.28194, places=5)
        self.assertEqual(u.get("requests"), 1)
        self.assertEqual(u.get("input_tokens"), 14067)
        self.assertEqual(u.get("output_tokens"), 5)
        self.assertEqual(u.get("cache_read_tokens"), 0)
        self.assertTrue(run.get("input_excludes_cache"))

        meta = {}
        server._add_usage(meta, u)
        self.assertEqual(meta["usage"]["input"], 14067)
        self.assertEqual(meta["usage"]["output"], 5)
        self.assertEqual(meta["usage"]["requests"], 1)
        self.assertAlmostEqual(meta["usage"]["credits"], 0.28194, places=5)

    def test_worker_command_respects_permission_mode_with_or_without_isolation(self):
        worker_id = "worker_deadbeef"
        with tempfile.TemporaryDirectory() as workdir:
            for mode in ("ask", "auto_safe", "auto_approve"):
                for isolation in ({}, {"isolate": True}, {"isolated": True}):
                    with self.subTest(mode=mode, isolation=isolation):
                        cmd = server._worker_command(
                            "copilot", "/usr/bin/copilot", "auto", "low", False,
                            ["files:write", "terminal:safe"], mode, "hello", None, "",
                            worker_id=worker_id, workdir=workdir, **isolation,
                        )
                        self.assertIn("--sync-session=off", cmd)
                        self.assertIn("--no-ask-user", cmd)
                        self.assertIn("--add-dir", cmd)
                        self.assertIn(workdir, cmd)
                        if mode == "ask":
                            self.assertIn("--acp", cmd)
                            self.assertNotIn("-p", cmd)
                            self.assertNotIn("--allow-all-tools", cmd)
                        elif mode == "auto_safe":
                            self.assertIn("-p", cmd)
                            self.assertNotIn("--acp", cmd)
                            self.assertNotIn("--allow-all-tools", cmd)
                            write_rules = [arg for arg in cmd if arg.startswith("--allow-tool=write(")]
                            expected_dir = (
                                os.path.join(server.SUBWAY_DIR, "worktrees", worker_id)
                                if isolation else workdir
                            )
                            self.assertEqual(
                                write_rules,
                                [f"--allow-tool=write({os.path.realpath(expected_dir)}/**)"],
                            )
                            shell_rules = {arg for arg in cmd if arg.startswith("--allow-tool=shell(")}
                            self.assertEqual(
                                shell_rules,
                                {f"--allow-tool=shell({command}:*)" for command in server.AGY_SAFE_COMMANDS},
                            )
                        else:
                            self.assertIn("--allow-all-tools", cmd)
                            self.assertNotIn("--acp", cmd)

    def test_preflight_budget_names_chat_pool(self):
        mock_raw = {
            "copilot_plan": "individual",
            "quota_reset_date": "2026-12-01",
            "quota_snapshots": {
                "chat": {"percent_remaining": 75.0, "remaining": 1500, "entitlement": 2000, "unlimited": False}
            }
        }
        with patch.object(server, "_copilot_raw", return_value=mock_raw), \
             patch.object(server, "discover_connected_providers", return_value=[{"id": "copilot", "name": "GitHub Copilot", "state": "live"}]):
            ok, msg = server.check_budget_preflight(provider="copilot")
            self.assertTrue(ok)
            self.assertIn("chat pool", msg.lower())
            self.assertIn("1500 of 2000", msg)
            self.assertIn("2026-12-01", msg)

    def test_dispatchable_with_and_without_cli(self):
        mock_raw = {
            "quota_snapshots": {
                "chat": {"percent_remaining": 50.0, "remaining": 100, "entitlement": 200, "unlimited": False}
            }
        }
        with patch.object(server, "_copilot_raw", return_value=mock_raw):
            server.detect_clients.clear()
            with patch("server.shutil.which", side_effect=lambda bin_name: "/usr/local/bin/copilot" if bin_name == "copilot" else None):
                provs = server.discover_connected_providers(refresh=True)
                copilot_prov = next((p for p in provs if p["id"] == "copilot"), None)
                self.assertIsNotNone(copilot_prov)
                self.assertTrue(copilot_prov["client"]["dispatchable"])

            server.detect_clients.clear()
            with patch("server.shutil.which", return_value=None):
                provs = server.discover_connected_providers(refresh=True)
                copilot_prov = next((p for p in provs if p["id"] == "copilot"), None)
                if copilot_prov:
                    self.assertFalse(copilot_prov["client"]["dispatchable"])


class TestCopilotPermissionFallback(unittest.TestCase):
    class Process:
        def __init__(self, stdout=b"", returncode=0):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO()
            self.returncode = returncode

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = 0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        paths = {
            "HOME": root,
            "SUBWAY_DIR": os.path.join(root, ".subway-context"),
            "WORKERS_DIR": os.path.join(root, ".subway-context", "workers"),
            "TRACKS_DIR": os.path.join(root, ".subway-context", "tracks"),
            "LOCKS_FILE": os.path.join(root, ".subway-context", "locks.json"),
            "QUOTAS_FILE": os.path.join(root, ".subway-context", "quotas.json"),
            "CLAUDE_DIR": os.path.join(root, ".claude"),
            "CLAUDE_JSON": os.path.join(root, ".claude.json"),
            "AGY_DIR": os.path.join(root, ".gemini", "antigravity"),
            "OPENCODE_DIR": os.path.join(root, ".local", "share", "opencode"),
            "OPENCODE_AGENTS_DIR": os.path.join(root, ".config", "opencode", "agents"),
        }
        for name, value in paths.items():
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        env_patch = patch.dict(os.environ, {
            "HOME": root,
            "SUBWAY_DIR": paths["SUBWAY_DIR"],
            "SUBWAY_CLAUDE_DIR": paths["CLAUDE_DIR"],
            "SUBWAY_CLAUDE_JSON": paths["CLAUDE_JSON"],
            "SUBWAY_AGY_DIR": paths["AGY_DIR"],
            "SUBWAY_OPENCODE_DIR": paths["OPENCODE_DIR"],
            "SUBWAY_OPENCODE_AGENTS_DIR": paths["OPENCODE_AGENTS_DIR"],
            "SUBWAY_NO_NOTIFY": "1",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        server._ensure_subway_dir()
        server._copilot_raw.clear()
        server._copilot_models.clear()
        server._model_catalog.clear()
        server._hud_cache.clear()
        self.addCleanup(server._copilot_raw.clear)
        self.addCleanup(server._copilot_models.clear)
        self.addCleanup(server._model_catalog.clear)
        self.addCleanup(server._hud_cache.clear)

    def _worker(self, isolate=False, max_tokens=0):
        wid = "worker_a1b2c3"
        project = os.path.join(self.temp.name, "project")
        os.makedirs(project, exist_ok=True)
        worktree = os.path.join(server.SUBWAY_DIR, "worktrees", wid) if isolate else ""
        if worktree:
            os.makedirs(worktree, exist_ok=True)
        cmd = server._worker_command(
            "copilot", "/mock/copilot", "auto", "low", False,
            ["files:read", "files:write", "terminal:safe"], "ask", "do task", None, "",
            worker_id=wid, workdir=worktree or project, isolate=isolate,
        )
        meta = {
            "id": wid, "role": "Tester", "provider": "copilot", "model": "auto",
            "effort": "low", "mode": "standard", "permission_mode": "ask",
            "capabilities": ["files:read", "files:write", "terminal:safe"],
            "skip_permissions": False, "budget": {"max_tokens": max_tokens},
            "track": "main", "project_dir": project, "worktree": worktree or None,
            "isolate": isolate, "launched_by": "launcher-test", "cmd": cmd,
            "status": "running", "timeout_seconds": 0, "usage": {"input": 0, "output": 0},
            "prompt": "do task", "log_file": server._log_file(wid), "pid": 4321,
        }
        server._save_worker(meta)
        return meta

    def _run_supervisor(self, meta, processes):
        def safe_result(_proc, _out, run, _quota_handler):
            run["response"] = "safe-mode result"

        with patch.object(server.subprocess, "Popen", side_effect=processes), \
             patch.object(server, "_worker_activity_signals", return_value={}), \
             patch.object(server, "_pump_copilot", side_effect=safe_result), \
             patch.object(server, "_record_worker_stat"), \
             patch.object(server, "_settle_worker_result"), \
             patch.object(server, "_collect_worktree", return_value="No files changed."), \
             patch.object(server, "_run_triggers"), \
             patch.object(server, "start_approved_workers"), \
             patch.object(server, "notify_desktop_event"), \
             patch.object(server.signal, "signal"):
            server.supervise_worker(meta["id"])

    def test_acp_start_failure_falls_back_to_safe_commands_and_notifies(self):
        for isolate in (False, True):
            with self.subTest(isolate=isolate):
                meta = self._worker(isolate=isolate, max_tokens=4000)
                fallback_proc = self.Process()
                self._run_supervisor(meta, [OSError("ACP unavailable"), fallback_proc])

                saved = server._load_worker(meta["id"])
                note = "Copilot ACP unavailable; running with safe-command auto-approval"
                self.assertEqual(saved["start_note"], note)
                self.assertIn(note, server.t_worker_status(meta["id"]))
                self.assertIn(note, server.worker_listing())
                inbox = os.path.join(server.SUBWAY_DIR, "inbox", "launcher-test.jsonl")
                with open(inbox, encoding="utf-8") as f:
                    self.assertIn(note, f.read())
                self.assertNotIn("--acp", saved["cmd"])
                self.assertIn("-p", saved["cmd"])
                self.assertIn("--allow-tool=shell(ls:*)", saved["cmd"])
                expected_write = os.path.join(server.SUBWAY_DIR, "worktrees", meta["id"]) if isolate else meta["project_dir"]
                self.assertIn(f"--allow-tool=write({os.path.realpath(expected_write)}/**)", saved["cmd"])
                self.assertNotIn("--max-ai-credits", saved["cmd"])

    def test_acp_handshake_failure_falls_back_and_success_does_not_add_note(self):
        init_reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        failed_session = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "ACP unsupported"}}).encode()
        broken_acp = self.Process(stdout=b"\n".join((init_reply, failed_session)) + b"\n", returncode=None)
        safe_proc = self.Process()
        meta = self._worker()
        self._run_supervisor(meta, [broken_acp, safe_proc])
        self.assertIn("start_note", server._load_worker(meta["id"]))

        session_reply = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "copilot-session"}}).encode()
        prompt_reply = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"response": "ACP result"}}).encode()
        working_acp = self.Process(stdout=b"\n".join((init_reply, session_reply, prompt_reply)) + b"\n", returncode=None)
        meta_ok = self._worker()
        self._run_supervisor(meta_ok, [working_acp])
        self.assertNotIn("start_note", server._load_worker(meta_ok["id"]))

    def test_copilot_usd_cap_maps_to_credits_but_token_cap_does_not(self):
        route = {"picks": [{"provider": "copilot", "model": "auto"}], "private": False, "reason": "test route"}
        mocks = [
            patch.object(server, "find_claude_binary", return_value="/mock/claude"),
            patch.object(server, "find_opencode_binary", return_value="/mock/opencode"),
            patch.object(server, "find_agy_binary", return_value="/mock/agy"),
            patch.object(server, "find_copilot_binary", return_value="/mock/copilot"),
            patch.object(server, "route_task", return_value=route),
            patch.object(server, "check_budget_preflight", return_value=(True, "Budget approved.")),
            patch.object(server, "t_broadcast_track"),
            patch.object(server, "notify_if_still_waiting"),
            patch.object(server, "start_approved_workers"),
            patch.object(server, "_self_id", return_value="launcher-test"),
        ]
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], mocks[7], mocks[8], mocks[9]:
            server.t_spin_worker("Capped", "do capped task", provider="copilot", model="auto", max_budget_usd=1.25)
            server.t_spin_worker("Token capped", "do token task", provider="copilot", model="auto", max_tokens=5000)
        workers = {w["role"]: w for w in server._all_workers()}
        self.assertIn("--max-ai-credits", workers["Capped"]["cmd"])
        credit_index = workers["Capped"]["cmd"].index("--max-ai-credits")
        self.assertEqual(workers["Capped"]["cmd"][credit_index + 1], "125")
        self.assertNotIn("--max-ai-credits", workers["Token capped"]["cmd"])


class TestCopilotIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "copilot_run.jsonl")

    def tearDown(self):
        server._copilot_cli_usage.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build_session_state(self):
        """A ~/.copilot/session-state look-alike holding the fixture's usage checkpoints, timestamped now."""
        sess_dir = os.path.join(self.temp_dir, "session-now")
        os.makedirs(sess_dir, exist_ok=True)
        dest_events = os.path.join(sess_dir, "events.jsonl")
        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        with open(self.fixture_path, "r", encoding="utf-8") as f_in, open(dest_events, "w", encoding="utf-8") as f_out:
            for line in f_in:
                if "session.usage_checkpoint" in line:
                    ev = json.loads(line)
                    ev["timestamp"] = now_iso
                    f_out.write(json.dumps(ev) + "\n")
                else:
                    f_out.write(line)
        os.utime(dest_events, (now, now))
        return self.temp_dir

    def test_copilot_cli_usage_missing_folder(self):
        missing_dir = os.path.join(self.temp_dir, "nonexistent")
        res = server._copilot_cli_usage(missing_dir)
        self.assertIsNone(res)

    def test_copilot_cli_usage_from_fixture(self):
        sess_dir = os.path.join(self.temp_dir, "session-1")
        os.makedirs(sess_dir, exist_ok=True)
        dest_events = os.path.join(sess_dir, "events.jsonl")

        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        with open(self.fixture_path, "r", encoding="utf-8") as f_in, open(dest_events, "w", encoding="utf-8") as f_out:
            for line in f_in:
                if "session.usage_checkpoint" in line:
                    ev = json.loads(line)
                    ev["timestamp"] = now_iso
                    f_out.write(json.dumps(ev) + "\n")
                else:
                    f_out.write(line)

        os.utime(dest_events, (now, now))

        res = server._copilot_cli_usage(self.temp_dir)
        self.assertIsNotNone(res)
        self.assertIn("hourly", res)
        self.assertEqual(len(res["hourly"]), 24)
        self.assertIn("5h", res)
        self.assertIn("7d", res)
        self.assertIn("credits", res)
        self.assertIn("5h", res["credits"])
        self.assertIn("7d", res["credits"])
        self.assertGreater(res["credits"]["5h"], 0)
        self.assertGreater(res["5h"]["input"], 0)
        self.assertGreater(sum(res["hourly"]), 0)

    def test_copilot_cli_usage_excludes_cached_input(self):
        sess_dir = os.path.join(self.temp_dir, "session-cache")
        os.makedirs(sess_dir)
        events_path = os.path.join(sess_dir, "events.jsonl")
        event = {
            "type": "session.usage_checkpoint",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
            "data": {
                "accountingSnapshot": {
                    "tokenDetails": {
                        "input": {"tokenCount": 150},
                        "output": {"tokenCount": 10},
                        "cache_read": {"tokenCount": 800},
                        "cache_write": {"tokenCount": 50},
                    },
                    "modelMetrics": {
                        "model-a": {"usage": {
                            "inputTokens": 1000,
                            "outputTokens": 10,
                            "cacheReadTokens": 800,
                            "cacheWriteTokens": 50,
                        }}
                    },
                }
            },
        }
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        res = server._copilot_cli_usage(self.temp_dir)
        self.assertEqual(res["5h"]["input"], 200)
        self.assertEqual(res["5h"]["cache_read"], 800)
        self.assertEqual(res["hourly"][-1], 210)
        self.assertEqual(res["per_model"]["model-a"], 210)

    def test_copilot_view_carries_tokens(self):
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        fake_tokens = {
            "5h": {"input": 100, "output": 20, "cache_read": 0},
            "7d": {"input": 100, "output": 20, "cache_read": 0},
            "hourly": [0] * 23 + [120],
            "credits": {"5h": 0.05, "7d": 0.05},
            "per_model": {"gpt-6-luna": 120},
        }
        with patch.object(server, "_copilot_cli_usage", return_value=fake_tokens):
            view = server._copilot_view(client, refresh=True)
            self.assertIsNotNone(view.get("tokens"))
            self.assertEqual(view["tokens"], fake_tokens)
            self.assertIn("Tokens from Copilot CLI sessions on this Mac", view.get("source", ""))

    def test_preflight_and_spawn_accept_copilot(self):
        ok, msg = server.check_budget_preflight(model="auto", provider="copilot")
        self.assertTrue(ok)

        with patch.object(server, "find_copilot_binary", return_value="/mock/copilot"), \
             patch.object(server, "_launch_worker") as launch, \
             patch.object(server, "start_approved_workers"), \
             patch.object(server, "notify_if_still_waiting"), \
             patch.object(server, "t_broadcast_track"), \
             patch.object(server.subprocess, "Popen", side_effect=OSError("no subprocess may start")) as popen:
            res = server.t_spin_worker(
                role="Unit Tester",
                prompt="echo hello",
                provider="copilot",
                model="auto",
            )
            self.assertIn("worker_", res)
            launch.assert_not_called()  # No worker is launched: the record waits for the user's approval.
            self.assertFalse([c for c in popen.call_args_list if "__supervise" in (c.args[0] if c.args else [])])

    def test_result_event_records_credits(self):
        line = json.dumps({"type": "result", "sessionId": "s1", "result": "done",
                           "usage": {"premiumRequests": 2, "credits": 0.5}})
        run = server.parse_copilot_jsonl(line)
        self.assertEqual(run["usage"]["requests"], 2)
        self.assertAlmostEqual(run["usage"]["credits"], 0.5)

    def test_copilot_view_is_json_serialisable(self):
        res = server._copilot_cli_usage(self._build_session_state())
        self.assertIsNotNone(res)
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        raw = {"copilot_plan": "individual", "quota_reset_date": "2026-12-01",
               "quota_snapshots": {"chat": {"percent_remaining": 50.0, "remaining": 100,
                                            "entitlement": 200, "unlimited": False}}}
        with patch.object(server, "_copilot_cli_usage", return_value=res), \
             patch.object(server, "_copilot_raw", return_value=raw), \
             patch.object(server, "_copilot_models", return_value=["mai-code-1.1-flash"]), \
             patch.object(server, "_other_sessions", return_value=[]):
            view = server._copilot_view(client, refresh=True)
        json.dumps(view)  # A self-referential usage dict used to raise "Circular reference detected".
        self.assertEqual(view["credits"], res["credits"])

    def test_chat_pool_unknown_keeps_copilot_dispatchable(self):
        with patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "_copilot_models", return_value=["mai-code-1.1-flash"]), \
             patch.object(server, "_other_sessions", return_value=[]):
            client = {"id": "copilot", "installed": True, "dispatchable": True}
            with patch.object(server, "_copilot_raw", return_value={"quota_snapshots": {}}):
                server._copilot_view(client, refresh=True)
            self.assertTrue(client["dispatchable"])

            client = {"id": "copilot", "installed": True, "dispatchable": True}
            empty = {"quota_snapshots": {"chat": {"remaining": 0, "entitlement": 200, "unlimited": False}}}
            with patch.object(server, "_copilot_raw", return_value=empty):
                server._copilot_view(client, refresh=True)
            self.assertFalse(client["dispatchable"])

    def test_copilot_view_no_raw_dispatchable_follows_cli(self):
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        with patch.object(server, "_copilot_raw", return_value=None), \
             patch.object(server, "_copilot_models", return_value=[]), \
             patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "find_copilot_binary", return_value="/usr/local/bin/copilot"):
            server._copilot_view(client, refresh=True)
        self.assertTrue(client["dispatchable"])

        client = {"id": "copilot", "installed": True, "dispatchable": True}
        with patch.object(server, "_copilot_raw", return_value=None), \
             patch.object(server, "_copilot_models", return_value=[]), \
             patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "find_copilot_binary", return_value=None):
            server._copilot_view(client, refresh=True)
        self.assertFalse(client["dispatchable"])

    def test_copilot_catalog_carries_aliases(self):
        server._model_catalog.clear()
        with patch.object(server, "_copilot_models",
                           return_value=[{"model": "gpt-5", "name": "GPT-5", "aliases": ["gpt-5-chat"]}]), \
             patch.object(server, "find_copilot_binary", return_value=None):
            catalog = server.model_catalog()
        entry = next(m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "gpt-5")
        self.assertEqual(entry["aliases"], ["gpt-5-chat"])

    def test_review_step_prompt_carries_builder_task(self):
        meta = {"id": "worker_abc123", "status": "completed", "provider": "claude", "role": "Builder",
                "track": "main", "report": "changed two files",
                "prompt": "Work only in this repo. No git.\n\nAdd a --dry-run flag to the exporter.",
                "then": [server._review_trigger_step("auto")]}
        captured = {}

        def fake_spin(**kwargs):
            captured.update(kwargs)
            return "Worker worker_def456 queued."

        with patch.object(server, "_load_worker", side_effect=lambda wid: dict(meta) if wid == meta["id"] else None), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "route_task", return_value={"picks": [{"provider": "copilot", "model": "auto"}], "reason": ""}), \
             patch.object(server, "t_spin_worker", side_effect=fake_spin), \
             patch.object(server, "_launch_worker"), \
             patch.object(server, "t_broadcast_track"):
            server._run_triggers(meta["id"], {"status": "completed", "response": "done"})

        child_prompt = captured.get("prompt", "")
        self.assertIn("TASK:", child_prompt)
        self.assertIn("Add a --dry-run flag to the exporter.", child_prompt)
        self.assertNotIn("Work only in this repo", child_prompt)
        self.assertLess(child_prompt.index("TASK:"), child_prompt.index("PREVIOUS WORKER"))

    def test_copilot_auto_cli_catalog_entry(self):
        server._model_catalog.clear()
        with patch.object(server, "find_copilot_binary", return_value=None):
            catalog_no_cli = server.model_catalog()
            copilot_autos = [m for m in catalog_no_cli if m.get("provider") == "copilot" and m.get("model") == "auto"]
            self.assertEqual(len(copilot_autos), 0)

        server._model_catalog.clear()
        with patch.object(server, "find_copilot_binary", return_value="/usr/local/bin/copilot"):
            catalog_cli = server.model_catalog()
            copilot_autos = [m for m in catalog_cli if m.get("provider") == "copilot" and m.get("model") == "auto"]
            self.assertEqual(len(copilot_autos), 1)

    def test_route_task_copilot_chat_allowance(self):
        healthy_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 100, "entitlement": 200, "percent_remaining": 50.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        low_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 10, "entitlement": 200, "percent_remaining": 5.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        zero_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 0, "entitlement": 200, "percent_remaining": 0.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        provs = [
            {"id": "copilot", "client": {"dispatchable": True}},
            {"id": "antigravity", "client": {"dispatchable": True}}
        ]
        with patch.object(server, "_copilot_models", return_value=[{"model": "copilot-small", "coding": 2, "reasoning": 2, "writing": 2}]):
            server._model_catalog.clear()
            # Healthy chat pool picks Copilot for a small task when it is cheapest
            with patch.object(server, "_copilot_raw", return_value=healthy_raw):
                res_healthy = server.route_task("fix typo in readme", providers=provs)
                self.assertEqual(res_healthy["picks"][0]["provider"], "copilot")

            # Chat under shed_at_pct demotes it
            with patch.object(server, "_copilot_raw", return_value=low_raw):
                res_low = server.route_task("fix typo in readme", providers=provs)
                self.assertNotEqual(res_low["picks"][0]["provider"], "copilot")

            # Chat 0 excludes it
            with patch.object(server, "_copilot_raw", return_value=zero_raw):
                res_zero = server.route_task("fix typo in readme", providers=provs)
                self.assertFalse(any(p["provider"] == "copilot" for p in res_zero["picks"]))

    def test_token_counting_model_metrics(self):
        ev = {
            "type": "session.usage_checkpoint",
            "data": {
                "tokenDetails": {
                    "input": {"tokenCount": 3},
                    "output": {"tokenCount": 100},
                    "cache_read": {"tokenCount": 50}
                },
                "modelMetrics": {
                    "gpt-4o": {
                        "usage": {
                            "inputTokens": 25000,
                            "outputTokens": 100,
                            "cacheReadTokens": 50,
                            "cacheWriteTokens": 0
                        }
                    }
                }
            }
        }
        run = {}
        server._process_copilot_event(ev, run, lambda text: None)
        self.assertEqual(run["usage"]["input_tokens"], 24950)
        self.assertEqual(run["usage"]["output_tokens"], 100)
        self.assertEqual(run["usage"]["cache_read_tokens"], 50)
        self.assertEqual(run["spent"], 25050)
        self.assertTrue(run["input_excludes_cache"])

    def test_copilot_input_clamps_cache_and_handles_missing_cache(self):
        # tokenDetails.input already excludes cache reads (live shape), so it is never reduced by cache_read.
        for input_tokens, cache_read, expected in ((40, 80, 40), (40, None, 40)):
            event = {
                "type": "session.usage_checkpoint",
                "data": {"tokenDetails": {
                    "input": {"tokenCount": input_tokens},
                    "output": {"tokenCount": 5},
                    **({"cache_read": {"tokenCount": cache_read}} if cache_read is not None else {}),
                }},
            }
            run = {}
            server._process_copilot_event(event, run, lambda text: None)
            self.assertEqual(run["usage"]["input_tokens"], expected)
            self.assertEqual(run["usage"]["cache_read_tokens"], cache_read or 0)

        parsed = server.parse_copilot_jsonl(json.dumps({
            "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 12}
        }))
        self.assertEqual(parsed["usage"]["input_tokens"], 0)
        self.assertEqual(parsed["usage"]["cache_read_tokens"], 12)

        no_cache = server.parse_copilot_jsonl(json.dumps({
            "usage": {"input_tokens": 10, "output_tokens": 2}
        }))
        self.assertEqual(no_cache["usage"]["input_tokens"], 10)

    def test_pump_copilot_records_fresh_input(self):
        proc = MagicMock()
        proc.stdout = [json.dumps({
            "type": "session.usage_checkpoint",
            "data": {"tokenDetails": {
                "input": {"tokenCount": 100},
                "output": {"tokenCount": 5},
                "cache_read": {"tokenCount": 60},
            }},
        }).encode("utf-8")]
        run = {}
        server._pump_copilot(proc, io.BytesIO(), run)
        self.assertEqual(run["usage"]["input_tokens"], 100)
        self.assertEqual(run["usage"]["cache_read_tokens"], 60)

    def test_copilot_live_checkpoint_shape_counts_cache_once(self):
        # Numbers from a real 1.3.3 worker checkpoint: modelMetrics input = uncached + cache reads + cache writes.
        td = {"input": {"tokenCount": 159}, "output": {"tokenCount": 66333},
              "cache_read": {"tokenCount": 4476250}, "cache_write": {"tokenCount": 126823}}
        mm = {"gpt-6-luna": {"usage": {"inputTokens": 4603232, "outputTokens": 66333,
                                        "cacheReadTokens": 4476250, "cacheWriteTokens": 126823}}}
        for data in ({"accountingSnapshot": {"tokenDetails": td, "modelMetrics": mm}},
                     {"accountingSnapshot": {"tokenDetails": td}}):
            run = {}
            server._process_copilot_event({"type": "session.usage_checkpoint", "data": data}, run, lambda text: None)
            self.assertEqual(run["usage"]["input_tokens"], 159 + 126823)
            self.assertEqual(run["usage"]["cache_read_tokens"], 4476250)

    def test_legacy_copilot_worker_usage_normalizes_without_rewriting(self):
        root = os.path.join(self.temp_dir, "subway")
        workers_dir = os.path.join(root, "workers")
        tracks_dir = os.path.join(root, "tracks")
        os.makedirs(workers_dir)
        created_at = server._dt.datetime.now().astimezone().isoformat()
        old_record = {
            "id": "worker_abcdef", "provider": "copilot", "status": "completed",
            "created_at": created_at,
            "usage": {"input": 1500, "output": 50, "cache_read": 1200},
        }
        new_record = {
            "id": "worker_123456", "provider": "copilot", "status": "completed",
            "created_at": created_at,
            "usage": {"input": 400, "output": 20, "cache_read": 300},
            "input_excludes_cache": True,
        }
        old_path = os.path.join(workers_dir, "worker_abcdef.json")
        new_path = os.path.join(workers_dir, "worker_123456.json")
        with open(old_path, "w", encoding="utf-8") as f:
            json.dump(old_record, f)
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(new_record, f)
        with open(old_path, "rb") as f:
            old_bytes = f.read()

        with patch.object(server, "SUBWAY_DIR", root), \
             patch.object(server, "WORKERS_DIR", workers_dir), \
             patch.object(server, "TRACKS_DIR", tracks_dir), \
             patch.object(server, "_copilot_cli_usage", return_value=None):
            records = {w["id"]: w for w in server._all_workers()}
            self.assertEqual(records["worker_abcdef"]["usage"]["input"], 300)
            self.assertEqual(records["worker_123456"]["usage"]["input"], 400)
            self.assertTrue(records["worker_abcdef"]["input_excludes_cache"])
            again = {w["id"]: w for w in server._all_workers()}
            self.assertEqual(again["worker_abcdef"]["usage"]["input"], 300)
            self.assertEqual(server._provider_5h_tokens("copilot"), 770)

        with open(old_path, "rb") as f:
            self.assertEqual(f.read(), old_bytes)

    def test_new_copilot_usage_record_is_marked(self):
        meta = {"provider": "copilot", "usage": {"input": 0, "output": 0, "cache_read": 0}}
        server._add_usage(meta, {
            "input_tokens": 40, "output_tokens": 5, "cache_read_tokens": 60,
            "credits": 0.25, "requests": 1,
        })
        self.assertTrue(meta["input_excludes_cache"])
        self.assertEqual(meta["usage"]["input"], 40)
        self.assertEqual(meta["usage"]["cache_read"], 60)
        self.assertEqual(meta["usage"]["output"], 5)
        self.assertEqual(meta["usage"]["credits"], 0.25)
        self.assertEqual(meta["usage"]["requests"], 1)

    def test_copilot_models_deduplicate(self):
        raw_models = [
            {"id": "mai-code-1-flash-4th", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1.1-flash", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1-flash-secondary", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1-flash-tertiary", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "kimi-k3-copilot", "name": "Kimi K3 (GitHub)", "policy": {"state": "enabled"}},
            {"id": "kimi-k3-base", "name": "Kimi K3 (GitHub)", "policy": {"state": "enabled"}},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "run1")
            os.makedirs(model_dir, exist_ok=True)
            with open(os.path.join(model_dir, "models.json"), "w", encoding="utf-8") as f:
                json.dump(raw_models, f)

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                models = server._copilot_models()
                self.assertEqual(len(models), 2)
                mai = next(m for m in models if m["name"] == "MAI-Code-1.1-Flash")
                kimi = next(m for m in models if m["name"] == "Kimi K3 (GitHub)")
                self.assertEqual(mai["model"], "mai-code-1.1-flash")
                self.assertEqual(set(mai["aliases"]), {"mai-code-1-flash-4th", "mai-code-1-flash-secondary", "mai-code-1-flash-tertiary"})
                self.assertEqual(kimi["model"], "kimi-k3-base")
                self.assertEqual(kimi["aliases"], ["kimi-k3-copilot"])


    def test_copilot_cli_refused_recorded_and_retried_with_auto(self):
        worker_id = "worker_cptest1"
        log_file = os.path.join(self.temp_dir, f"{worker_id}.log")
        meta = {
            "id": worker_id,
            "provider": "copilot",
            "model": "gpt-4.1",
            "cmd": ["copilot", "-p", "hi", "--model", "gpt-4.1", "--output-format", "json"],
            "log_file": log_file,
            "status": "running",
            "timeout_seconds": 60,
        }
        with open(log_file, "w") as f:
            f.write("")

        first_proc = MagicMock()
        first_proc.poll.return_value = 1
        first_proc.wait.return_value = 1
        first_proc.stdout = [b'Error: Model "gpt-4.1" from --model flag is not available.\n']

        second_proc = MagicMock()
        second_proc.poll.return_value = 0
        second_proc.wait.return_value = 0
        second_proc.stdout = [b'{"type":"assistant.message","data":{"content":"done"}}\n']

        spawned_cmds = []

        def fake_popen(cmd, **kwargs):
            spawned_cmds.append(list(cmd))
            if len(spawned_cmds) == 1:
                return first_proc
            return second_proc

        with patch.object(server, "_load_worker", return_value=meta), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "_write_json_atomic"), \
             patch.object(server, "_load_json", return_value={"refused": {}, "accepted": {}}), \
             patch.object(server, "_save_json") as mock_save, \
             patch("subprocess.Popen", side_effect=fake_popen):
            server.supervise_worker(worker_id)

        self.assertEqual(len(spawned_cmds), 2)
        self.assertIn("gpt-4.1", spawned_cmds[0])
        self.assertIn("auto", spawned_cmds[1])
        # Saved refused store
        saved_stores = [args[1] for args, _ in mock_save.call_args_list if args[0] == server.COPILOT_CLI_MODELS_FILE]
        self.assertTrue(any("gpt-4.1" in s.get("refused", {}) for s in saved_stores))

    def test_copilot_cli_accepted_recorded(self):
        worker_id = "worker_cptest2"
        log_file = os.path.join(self.temp_dir, f"{worker_id}.log")
        meta = {
            "id": worker_id,
            "provider": "copilot",
            "model": "gpt-6-luna",
            "cmd": ["copilot", "-p", "hi", "--model", "gpt-6-luna", "--output-format", "json"],
            "log_file": log_file,
            "status": "running",
            "timeout_seconds": 60,
        }
        with open(log_file, "w") as f:
            f.write("")

        proc = MagicMock()
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.stdout = [b'{"type":"assistant.message","data":{"content":"hello"}}\n']

        with patch.object(server, "_load_worker", return_value=meta), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "_write_json_atomic"), \
             patch.object(server, "_load_json", return_value={"refused": {}, "accepted": {}}), \
             patch.object(server, "_save_json") as mock_save, \
             patch("subprocess.Popen", return_value=proc):
            server.supervise_worker(worker_id)

        saved_stores = [args[1] for args, _ in mock_save.call_args_list if args[0] == server.COPILOT_CLI_MODELS_FILE]
        self.assertTrue(any("gpt-6-luna" in s.get("accepted", {}) for s in saved_stores))

    def test_copilot_cli_expiry_after_7_days(self):
        now = time.time()
        store = {
            "refused": {
                "old-refused": now - (8 * 86400),
                "recent-refused": now - (2 * 86400),
            },
            "accepted": {}
        }
        with patch.object(server, "_load_copilot_cli_models", return_value=store):
            self.assertFalse(server.is_copilot_cli_refused("old-refused"))
            self.assertTrue(server.is_copilot_cli_refused("recent-refused"))

    def test_copilot_routing_excludes_refused_ids(self):
        provs = [{"id": "copilot", "client": {"dispatchable": True}}]
        cp_models = [{"model": "refused-mod", "coding": 4, "reasoning": 4, "writing": 4},
                     {"model": "good-mod", "coding": 3, "reasoning": 3, "writing": 3}]
        with patch.object(server, "_copilot_models", return_value=cp_models), \
             patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"), \
             patch.object(server, "_copilot_raw", return_value={"quota_snapshots": {"chat": {"remaining": 100, "entitlement": 100, "unlimited": True}}}):
            server._model_catalog.clear()
            route = server.route_task("write a python script", providers=provs)
            picks = [p["model"] for p in route.get("picks", []) if p["provider"] == "copilot"]
            self.assertNotIn("refused-mod", picks)

    def test_copilot_catalog_marks_cli_false(self):
        cp_models = [{"model": "refused-mod"}, {"model": "accepted-mod"}]
        with patch.object(server, "_copilot_models", return_value=cp_models), \
             patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"):
            server._model_catalog.clear()
            catalog = server.model_catalog()
            refused_entries = [m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "refused-mod"]
            accepted_entries = [m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "accepted-mod"]
            self.assertTrue(len(refused_entries) > 0)
            self.assertIs(refused_entries[0].get("cli"), False)
            self.assertTrue(len(accepted_entries) > 0)
            self.assertNotEqual(accepted_entries[0].get("cli"), False)

    def test_copilot_review_step_falls_back_to_auto(self):
        with patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"):
            step_refused = server._review_trigger_step("copilot/refused-mod")
            self.assertEqual(step_refused["provider"], "copilot")
            self.assertEqual(step_refused["model"], "auto")

            step_accepted = server._review_trigger_step("copilot/good-mod")
            self.assertEqual(step_accepted["provider"], "copilot")
            self.assertEqual(step_accepted["model"], "good-mod")

            then_steps = server._parse_trigger_steps([
                {"role": "Tester", "prompt": "test", "provider": "copilot", "model": "refused-mod"}
            ])
            self.assertEqual(then_steps[0]["provider"], "copilot")
            self.assertEqual(then_steps[0]["model"], "auto")


class TestCopilotChatTransit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.copilot_dir = os.path.join(self.temp_dir, "copilot")
        self.workers_dir = os.path.join(self.temp_dir, "workers")
        self.vscode_user = os.path.join(self.temp_dir, "Code", "User")
        cli_dir = os.path.join(self.copilot_dir, "session-state", "copilot-cli-sample")
        workspace = os.path.join(self.vscode_user, "workspaceStorage", "workspace-hash")
        chat_dir = os.path.join(workspace, "chatSessions")
        os.makedirs(cli_dir)
        os.makedirs(chat_dir)
        with open(os.path.join(workspace, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "file:///workspace/copilot-vscode-project"}, f)
        fixture_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        self.events_fixture = os.path.join(fixture_dir, "copilot_chat_events.jsonl")
        self.vscode_fixture = os.path.join(fixture_dir, "copilot_vscode_chat.jsonl")
        shutil.copyfile(self.events_fixture, os.path.join(cli_dir, "events.jsonl"))
        shutil.copyfile(self.vscode_fixture, os.path.join(chat_dir, "copilot-vscode-sample.jsonl"))
        empty_dir = os.path.join(self.vscode_user, "globalStorage", "emptyWindowChatSessions")
        os.makedirs(empty_dir)
        with open(self.vscode_fixture, "r", encoding="utf-8") as f:
            empty_fixture = f.read().replace("copilot-vscode-sample", "copilot-empty-sample")
        with open(os.path.join(empty_dir, "copilot-empty-sample.jsonl"), "w", encoding="utf-8") as f:
            f.write(empty_fixture)
        self.patches = [
            patch.object(server, "COPILOT_DIR", self.copilot_dir),
            patch.object(server, "WORKERS_DIR", self.workers_dir),
            patch.object(server, "SUBWAY_DIR", os.path.join(self.temp_dir, "subway")),
            patch.object(server, "_vscode_user_dirs", return_value=[self.vscode_user]),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _chat(self, chat_id="copilot-cli-sample"):
        return next(c for c in server.copilot_list(include_subagents=True) if c["id"] == chat_id)

    def test_list_read_search_segments_and_token_usage(self):
        chats = server.copilot_list()
        by_id = {chat["id"]: chat for chat in chats}
        self.assertIn("copilot-cli-sample", by_id)
        self.assertIn("copilot-vscode-sample", by_id)
        self.assertIn("copilot-empty-sample", by_id)
        self.assertEqual(by_id["copilot-cli-sample"]["project"], "/workspace/copilot-demo")
        self.assertEqual(by_id["copilot-vscode-sample"]["project"], "/workspace/copilot-vscode-project")
        self.assertEqual(by_id["copilot-empty-sample"]["surface"], "VS Code Chat (empty window)")
        self.assertEqual(by_id["copilot-empty-sample"]["project"], "")
        self.assertEqual(by_id["copilot-cli-sample"]["tokens"], 54)
        self.assertEqual(by_id["copilot-vscode-sample"]["tokens"], 37)

        decisions = server.t_read_chat("copilot-cli-sample", source="copilot", mode="decisions")
        self.assertIn("Keep the transcript adapter read-only", decisions)
        recent = server.t_read_chat("copilot-vscode-sample", source="copilot", mode="recent")
        self.assertIn("This is a synthetic Copilot response", recent)
        self.assertIn("compaction not marked", recent)
        outline = server.t_read_chat("copilot-cli-sample", source="copilot", mode="outline")
        full = server.t_read_chat("copilot-cli-sample", source="copilot", mode="full", segment=1)
        latest_read = server.t_read_chat("copilot-cli-sample", source="copilot", mode="recent", segment="latest")
        self.assertIn("context compacted", outline.lower())
        self.assertIn("Review the synthetic parser fixture", full)
        self.assertIn("second synthetic segment", latest_read.lower())
        self.assertIn("CLI result event remains readable", latest_read)
        self.assertEqual(server.chat_tokens_used("copilot-cli-sample"), 54)
        self.assertEqual(server.chat_tokens_used("copilot-vscode-sample"), 37)

        all_segments = server.chat_segments_info(self._chat())
        latest = server.chat_segments_info(self._chat(), "latest")
        self.assertEqual(all_segments[2], 2)
        self.assertEqual(latest[1:3], ("2", 2))
        self.assertTrue(any("compaction boundary" in turn["text"] for turn in all_segments[0]
                            if turn["role"] == "summary"))
        self.assertIn("Review the synthetic parser fixture.", server.t_search_chat(quote="read-only", source="copilot"))

    def test_subway_worker_sessions_are_hidden_unless_requested(self):
        worker_id = "copilot-worker-sample"
        worker_dir = os.path.join(self.copilot_dir, "session-state", worker_id)
        os.makedirs(worker_dir)
        with open(self.events_fixture, "r", encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for event in events:
            if event.get("type") == "session.start":
                event["data"]["sessionId"] = worker_id
            if event.get("type") == "result":
                event["sessionId"] = worker_id
        with open(os.path.join(worker_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        os.makedirs(self.workers_dir, exist_ok=True)
        with open(os.path.join(self.workers_dir, "worker_abcdef.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "worker_abcdef", "provider": "copilot", "conversation": worker_id,
                       "launched_by": "parent-station", "role": "Tester"}, f)

        visible = {chat["id"]: chat for chat in server.copilot_list()}
        self.assertNotIn(worker_id, visible)
        workers = {chat["id"]: chat for chat in server.copilot_list(include_subagents=True)}
        self.assertTrue(workers[worker_id]["worker"])
        self.assertEqual(workers[worker_id]["parent"], "parent-station")

    def test_agent_host_metadata_marks_cli_session_surface(self):
        global_storage = os.path.join(self.vscode_user, "globalStorage")
        os.makedirs(global_storage, exist_ok=True)
        con = sqlite3.connect(os.path.join(global_storage, "agent-host.db"))
        con.execute("create table sessions (session_uri text, provider text)")
        con.execute("insert into sessions values (?, ?)", ("copilotcli://copilot-cli-sample", "copilotcli"))
        con.commit()
        con.close()

        chat = next(c for c in server.copilot_list() if c["id"] == "copilot-cli-sample")
        self.assertEqual(chat["surface"], "VS Code Agent Host")

    def test_session_store_index_enriches_cli_project(self):
        con = sqlite3.connect(os.path.join(self.copilot_dir, "session-store.db"))
        con.execute("create table sessions (id text, cwd text, repository text, branch text, updated_at text, host_type text)")
        con.execute("insert into sessions values (?, ?, ?, ?, ?, ?)",
                    ("copilot-cli-sample", "/indexed/copilot-project", "", "", "2026-10-03T10:00:00Z", "cli"))
        con.commit()
        con.close()

        chat = next(c for c in server.copilot_list() if c["id"] == "copilot-cli-sample")
        self.assertEqual(chat["project"], "/indexed/copilot-project")
        self.assertEqual(chat["updated"], "2026-10-01T10:06:00Z")

    def test_cli_last_active_uses_newest_event_or_file_mtime(self):
        events_path = os.path.join(self.temp_dir, "session-state-test.jsonl")
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session.start", "timestamp": "2026-10-01T10:00:00Z",
                                "data": {"sessionId": "session-time-test"}}) + "\n")
            f.write(json.dumps({"type": "result", "timestamp": "2026-10-01T10:08:00Z",
                                "sessionId": "session-time-test"}) + "\n")
        meta = server._copilot_cli_meta(
            events_path, {"session-time-test": {"updated_at": "2026-11-01T00:00:00Z"}}, {}, set()
        )
        self.assertEqual(meta["updated"], "2026-10-01T10:08:00Z")

        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session.start", "data": {"sessionId": "session-time-test"}}) + "\n")
        with patch("server.os.path.getmtime", return_value=1800000000):
            meta = server._copilot_cli_meta(events_path, {}, {}, set())
        self.assertEqual(meta["updated"], 1800000000)

    def test_alias_resolution_and_threads_api(self):
        with patch.object(server, "all_chats", return_value=server.copilot_list(include_subagents=True)):
            tagged = server.t_tag_chat(alias="copilot-main", chat_id="copilot-cli-sample")
            self.assertIn("@copilot-main", tagged)
            self.assertEqual(server.resolve("@copilot-main", source="copilot")["id"], "copilot-cli-sample")
            note = server.t_send_note(to="@copilot-main", text="Synthetic handoff target.")
            self.assertIn("COPILOT", note)
        inbox = os.path.join(self.temp_dir, "subway", "inbox", "copilot-cli-sample.jsonl")
        with open(inbox, "r", encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["text"], "Synthetic handoff target.")

        class Handler(server.SubwayHUDHandler):
            def __init__(self):
                self.path = "/api/threads?source=copilot&limit=1"

            def _gate(self, api=True):
                return True

            def send_json(self, data, status=200):
                self.sent_data = data

        handler = Handler()
        handler.do_GET()
        self.assertEqual(handler.sent_data["total"], 3)
        self.assertEqual(len(handler.sent_data["chats"]), 1)
        self.assertEqual(handler.sent_data["chats"][0]["source"], "copilot")

    def test_missing_copilot_and_vscode_folders_are_ignored(self):
        missing = os.path.join(self.temp_dir, "does-not-exist")
        with patch.object(server, "COPILOT_DIR", missing), patch.object(server, "_vscode_user_dirs", return_value=[missing]):
            self.assertEqual(server.copilot_list(), [])
            self.assertIsNone(server.chat_tokens_used("missing-session"))

    def test_copilot_reader_tool_calls_permissions_subagents_and_summary_text(self):
        sample_dir = os.path.join(self.copilot_dir, "session-state", "copilot-gaps-sample")
        os.makedirs(sample_dir)
        events = [
            {"type": "session.start", "timestamp": "2026-10-02T10:00:00Z",
             "data": {"sessionId": "copilot-gaps-sample", "selectedModel": "gpt-synthetic"}},
            {"type": "user.message", "timestamp": "2026-10-02T10:01:00Z",
             "data": {"content": "Run the task and fetch api data."}},
            {"type": "tool.execution_start", "timestamp": "2026-10-02T10:02:00Z",
             "data": {"toolName": "fetch_api", "toolCallId": "call_123",
                      "arguments": {"endpoint": "/v1/data", "token": "ghp_1234567890abcdef123456"}}},
            {"type": "tool.execution_complete", "timestamp": "2026-10-02T10:02:10Z",
             "data": {"toolCallId": "call_123", "success": True,
                      "result": "Fetched data safely with password=supersecretpassword"}},
            {"type": "permission.requested", "timestamp": "2026-10-02T10:03:00Z",
             "data": {"command": "curl -H 'Authorization: Bearer secrettoken123' https://api.com"}},
            {"type": "permission.completed", "timestamp": "2026-10-02T10:03:05Z",
             "data": {"decision": "allow"}},
            {"type": "subagent.started", "timestamp": "2026-10-02T10:04:00Z",
             "data": {"subagentId": "sub_1", "role": "Analyzer", "description": "Analyzing payload"}},
            {"type": "subagent.finish", "timestamp": "2026-10-02T10:04:30Z",
             "data": {"subagentId": "sub_1", "role": "Analyzer", "status": "completed"}},
            {"type": "summary_text", "timestamp": "2026-10-02T10:05:00Z",
             "data": {"text": "summary_text line that is not compaction"}},
            {"type": "assistant.message", "timestamp": "2026-10-02T10:06:00Z",
             "data": {"content": "DECISION: We will proceed with the verified payload."}},
        ]
        events_path = os.path.join(sample_dir, "events.jsonl")
        with open(events_path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        chat = {"id": "copilot-gaps-sample", "source": "copilot", "path": events_path}

        # 1. Test copilot_read_segments directly
        segs_full, has_markers_full = server.copilot_read_segments(dict(chat, mode="full"))
        # summary_text must not create compaction boundary
        self.assertFalse(has_markers_full)
        self.assertEqual(len(segs_full), 1)

        # 2. Test read_chat mode "full"
        full_output = server.t_read_chat("copilot-gaps-sample", source="copilot", mode="full")
        # Check tool input and complete result
        self.assertIn("[tool fetch_api]", full_output)
        self.assertIn("/v1/data", full_output)
        self.assertIn("[success]", full_output)
        # Secrets redacted
        self.assertNotIn("ghp_1234567890abcdef123456", full_output)
        self.assertNotIn("supersecretpassword", full_output)
        self.assertNotIn("secrettoken123", full_output)
        self.assertIn("[REDACTED]", full_output)

        # Check permissions
        self.assertIn("[permission requested]", full_output)
        self.assertIn("[permission completed] allow", full_output)

        # Check subagents
        self.assertIn("[subagent start]", full_output)
        self.assertIn("[subagent finish]", full_output)
        self.assertIn("Analyzer", full_output)

        # 3. Test read_chat mode "decisions"
        decisions_output = server.t_read_chat("copilot-gaps-sample", source="copilot", mode="decisions")
        # Mode decisions keeps names only, does not show raw input/result dumps
        self.assertNotIn("/v1/data", decisions_output)
        self.assertNotIn("Fetched data safely", decisions_output)
        self.assertIn("proceed with the verified payload", decisions_output)

        recent_output = server.t_read_chat("copilot-gaps-sample", source="copilot", mode="recent")
        self.assertIn("Last tool: fetch_api:", recent_output)
        self.assertIn("[REDACTED]", recent_output)
        self.assertNotIn("ghp_1234567890abcdef123456", recent_output)


if __name__ == "__main__":
    unittest.main()
