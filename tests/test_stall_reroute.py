import os as _sandbox_os, sys as _sandbox_sys  # Isolate HOME, SUBWAY_DIR and agent folders before server loads.
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import server


class TestStallAndReroute(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_opencode_dir = server.OPENCODE_DIR
        self.orig_home = server.HOME
        self.orig_claude_dir = server.CLAUDE_DIR
        self.orig_claude_json = server.CLAUDE_JSON
        self.orig_agy_dir = server.AGY_DIR
        self.orig_opencode_agents_dir = server.OPENCODE_AGENTS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        self.orig_hud_dir = server.HUD_DIR
        server.SUBWAY_DIR = self.temp_dir
        server.WORKERS_DIR = os.path.join(self.temp_dir, "workers")
        server.HOME = self.temp_dir
        server.CLAUDE_DIR = os.path.join(self.temp_dir, ".claude")
        server.CLAUDE_JSON = os.path.join(self.temp_dir, ".claude.json")
        server.AGY_DIR = os.path.join(self.temp_dir, ".gemini", "antigravity")
        server.OPENCODE_DIR = os.path.join(self.temp_dir, ".local", "share", "opencode")
        server.OPENCODE_AGENTS_DIR = os.path.join(self.temp_dir, ".config", "opencode", "agents")
        server.TRACKS_DIR = os.path.join(self.temp_dir, "tracks")
        server.HUD_DIR = os.path.join(self.temp_dir, "hud")
        os.makedirs(server.WORKERS_DIR, exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir, "inbox"), exist_ok=True)
        server._model_catalog.clear()

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.HOME = self.orig_home
        server.CLAUDE_DIR = self.orig_claude_dir
        server.CLAUDE_JSON = self.orig_claude_json
        server.AGY_DIR = self.orig_agy_dir
        server.OPENCODE_DIR = self.orig_opencode_dir
        server.OPENCODE_AGENTS_DIR = self.orig_opencode_agents_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server.HUD_DIR = self.orig_hud_dir
        server._model_catalog.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_opencode_retry_db(self, directory, data, created_at):
        os.makedirs(server.OPENCODE_DIR, exist_ok=True)
        db_path = os.path.join(server.OPENCODE_DIR, "opencode.db")
        con = sqlite3.connect(db_path)
        try:
            con.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, time_created INTEGER)")
            con.execute(
                "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            con.execute("INSERT INTO session_v2 VALUES (?, ?, ?)", ("opencode-session", directory, created_at))
            con.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("message-1", "opencode-session", "assistant", 1, created_at, created_at, json.dumps(data)),
            )
            con.commit()
        finally:
            con.close()

    def test_opencode_retry_error_is_logged_and_rerouted_immediately(self):
        worker_id = "worker_444444"
        worker_dir = os.path.join(self.temp_dir, "worktree")
        os.makedirs(worker_dir)
        started_at = time.time() - 2
        retry_at = int((time.time() + 900) * 1000)
        data = {
            "retry": {
                "attempt": 2,
                "at": retry_at,
                "error": {
                    "type": "provider.rate-limit",
                    "message": "Free usage limit reached. Retry after 15 minutes.",
                },
            },
        }
        meta = {
            "id": worker_id,
            "provider": "opencode",
            "model": "opencode/flint-40b-free",
            "project_dir": worker_dir,
            "log_file": server._log_file(worker_id),
            "status": "running",
        }
        self._create_opencode_retry_db(worker_dir, data, int((started_at + 1) * 1000))
        server._save_worker(meta)

        with mock.patch.object(server, "_handle_worker_exhaustion_or_stall", return_value={"status": "failed"}) as handler:
            poll = server._poll_opencode_worker_retry(meta, started_at, set())

        self.assertTrue(poll["quota"])
        self.assertEqual(poll["reason"], "provider.rate-limit: Free usage limit reached. Retry after 15 minutes.")
        handler.assert_called_once()
        args, kwargs = handler.call_args
        self.assertEqual(args[:2], (worker_id, meta))
        self.assertEqual(args[2], f"Quota/usage limit error: {poll['reason']}")
        self.assertEqual(kwargs["reset_time"], retry_at / 1000)
        with open(meta["log_file"], encoding="utf-8") as log:
            line = log.read().strip()
        expected_clock = time.strftime("%H:%M", time.localtime(retry_at / 1000))
        self.assertEqual(
            line,
            f"[opencode] provider.rate-limit: Free usage limit reached. Retry after 15 minutes. "
            f"(retry 2 at {expected_clock})",
        )
        self.assertIn(poll["reason"], server.t_worker_status(worker_id))
        self.assertIn(poll["reason"], server.worker_listing())

    def test_opencode_retry_poller_ignores_messages_without_retry(self):
        worker_dir = os.path.join(self.temp_dir, "worktree")
        os.makedirs(worker_dir)
        started_at = time.time() - 2
        meta = {
            "id": "worker_555555",
            "provider": "opencode",
            "model": "opencode/flint-40b-free",
            "project_dir": worker_dir,
            "log_file": server._log_file("worker_555555"),
            "status": "running",
        }
        self._create_opencode_retry_db(worker_dir, {"tokens": {"input": 1}}, int((started_at + 1) * 1000))
        server._save_worker(meta)

        with mock.patch.object(server, "_handle_worker_exhaustion_or_stall") as handler:
            self.assertIsNone(server._poll_opencode_worker_retry(meta, started_at, set()))

        handler.assert_not_called()
        self.assertFalse(os.path.exists(meta["log_file"]))
        self.assertNotIn("status_reason", server._load_worker(meta["id"]))

    def test_opencode_retry_poller_ignores_missing_database(self):
        meta = {
            "id": "worker_666666",
            "provider": "opencode",
            "model": "opencode/flint-40b-free",
            "project_dir": self.temp_dir,
            "log_file": server._log_file("worker_666666"),
        }
        with mock.patch.object(server, "_handle_worker_exhaustion_or_stall") as handler:
            self.assertIsNone(server._poll_opencode_worker_retry(meta, time.time(), set()))
        handler.assert_not_called()
        self.assertFalse(os.path.exists(meta["log_file"]))

    def test_assistant_reply_mentioning_quota_does_not_reroute(self):
        worker_id = "worker_777777"
        server._mark_model_stall_cooldown("sonnet")
        assistant_event = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Your quota discussion is complete."}]},
        })
        result_event = json.dumps({"type": "result", "result": "Finished."})
        meta = {
            "id": worker_id,
            "role": "Writer",
            "provider": "claude",
            "model": "sonnet",
            "prompt": "Write a short note",
            "project_dir": self.temp_dir,
            "timeout_seconds": 10,
            "status": "running",
            "cmd": [sys.executable, "-c", f"print({assistant_event!r}); print({result_event!r})"],
            "log_file": server._log_file(worker_id),
        }
        server._save_worker(meta)

        with mock.patch.object(server, "find_claude_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "_handle_worker_exhaustion_or_stall") as handler:
            self.assertEqual(server.supervise_worker(worker_id), 0)

        handler.assert_not_called()
        self.assertEqual(len(server._all_workers()), 1)
        self.assertFalse(server._model_is_bad("sonnet"))
        self.assertFalse(server._load_json("stall_cooldowns.json"))

    def test_quota_matcher_requires_an_error_shape_or_provider_phrase(self):
        self.assertIsNone(server.check_quota_error("claude", "The quota and insufficient balance are discussed."))
        self.assertIsNone(server.check_quota_error("claude", "Usage limit reached", source="assistant"))
        self.assertIsNone(server.check_quota_error(
            "claude", "The docs say usage limit reached errors happen when...",
        ))
        self.assertEqual(
            server.check_quota_error("claude", "Claude AI usage limit reached|1793650000"),
            "usage limit reached",
        )
        self.assertEqual(
            server.check_quota_error("claude", "Error: Free usage limit reached."),
            "usage limit reached",
        )
        self.assertEqual(
            server.check_quota_error("claude", "Error: Usage limit reached"),
            "Usage limit reached",
        )
        self.assertEqual(
            server.check_quota_error("antigravity", '{"type":"error","message":"RESOURCE_EXHAUSTED"}', source="error_event"),
            "RESOURCE_EXHAUSTED",
        )
        self.assertEqual(
            server.check_quota_error("opencode", '{"error":{"status":429}}', source="error_event"),
            "429",
        )

    def test_provider_stall_timeout_defaults(self):
        with mock.patch.dict(os.environ, {"SUBWAY_STALL_TIMEOUT": ""}):
            for provider, expected in (
                ("copilot", 300),
                ("antigravity", 300),
                ("opencode", 120),
                ("claude", 120),
                ("local", 120),
                ("custom", 120),
            ):
                with self.subTest(provider=provider):
                    self.assertEqual(server.get_stall_timeout({"provider": provider}), expected)

    def test_copilot_session_id_streaming_command_and_metadata(self):
        captured = {}
        spin_args = {
            "role": "Worker", "prompt": "Do work", "provider": "copilot",
            "model": "gpt-6-luna", "project_dir": self.temp_dir, "permission_mode": "auto_approve",
        }

        def capture_meta(meta):
            captured.update(meta)

        with mock.patch.object(server, "find_copilot_binary", return_value="/mock/copilot"), \
             mock.patch.object(server, "route_task", return_value={
                 "private": False, "reason": "explicit", "picks": [{"provider": "copilot", "model": "gpt-6-luna"}],
             }), mock.patch.object(server, "check_budget_preflight", return_value=(True, "budget ok")), \
             mock.patch.object(server, "_save_worker", side_effect=capture_meta), \
             mock.patch.object(server, "t_broadcast_track"), \
             mock.patch.object(server, "notify_if_still_waiting"):
            result = server.t_spin_worker(**spin_args)

        self.assertIn("[APPROVAL REQUIRED]", result)
        self.assertRegex(captured["copilot_session_id"], r"^[0-9a-f-]{36}$")
        command = captured["cmd"]
        self.assertEqual(command[command.index("--session-id") + 1], captured["copilot_session_id"])
        self.assertEqual(command[command.index("--stream") + 1], "on")

    def test_copilot_events_growth_counts_as_activity_with_silent_output(self):
        session_id = "f12b7af4-f647-4ec1-a404-87eac8a7ae77"
        events = os.path.join(self.temp_dir, ".copilot", "session-state", session_id, "events.jsonl")
        os.makedirs(os.path.dirname(events))
        with open(events, "w", encoding="utf-8") as file:
            file.write('{"type":"session.start"}\n')
        log_file = os.path.join(self.temp_dir, "worker.log")
        open(log_file, "w", encoding="utf-8").close()
        meta = {
            "provider": "copilot", "copilot_session_id": session_id,
            "project_dir": self.temp_dir, "log_file": log_file,
        }
        proc = mock.Mock(pid=123)
        with mock.patch.object(server, "_worker_folder_activity", return_value=None), \
             mock.patch.object(server, "COPILOT_DIR", os.path.join(self.temp_dir, ".copilot")), \
             mock.patch.object(server, "_process_cpu_time", return_value=None):
            previous = server._worker_activity_signals(meta, proc, 0)
            last_active = {key: 10.0 for key in previous}
            with open(events, "a", encoding="utf-8") as file:
                file.write('{"type":"assistant.message"}\n')
            current = server._worker_activity_signals(meta, proc, 0)
            server._update_worker_activity(previous, last_active, current, 20.0)

        self.assertEqual(last_active["copilot_events"], 20.0)
        self.assertEqual(last_active["output"], 10.0)
        self.assertFalse(server._worker_is_stalled(last_active, 100.0, 300.0))

    def test_missing_copilot_events_falls_back_to_output_activity(self):
        log_file = os.path.join(self.temp_dir, "worker.log")
        with open(log_file, "w", encoding="utf-8") as file:
            file.write("")
        meta = {
            "provider": "copilot", "copilot_session_id": "missing-session",
            "project_dir": self.temp_dir, "log_file": log_file,
        }
        with mock.patch.object(server, "_worker_folder_activity", return_value=None), \
             mock.patch.object(server, "_process_cpu_time", return_value=None):
            previous = server._worker_activity_signals(meta, mock.Mock(pid=123), 0)
            last_active = {key: 1.0 for key in previous}
            with open(log_file, "a", encoding="utf-8") as file:
                file.write("still working\n")
            current = server._worker_activity_signals(meta, mock.Mock(pid=123), 0)
            server._update_worker_activity(previous, last_active, current, 2.0)

        self.assertFalse(os.path.exists(os.path.join(
            self.temp_dir, ".copilot", "session-state", "missing-session", "events.jsonl",
        )))
        self.assertEqual(last_active["output"], 2.0)
        self.assertFalse(server._worker_is_stalled(last_active, 3.0, 120.0))

    def test_silent_copilot_with_no_activity_is_stalled_rerouted_not_exhausted(self):
        worker_id = "worker_242424"
        new_worker_id = "worker_252525"
        session_id = "2eb1e932-c4c7-4a56-a708-72507680dd6a"
        meta = {
            "id": worker_id, "role": "Worker", "provider": "copilot", "model": "gpt-6-luna",
            "copilot_session_id": session_id, "prompt": "Make the requested change",
            "project_dir": self.temp_dir, "timeout_seconds": 30, "stall_timeout": 0.3,
            "status": "running", "cmd": [
                sys.executable, "-c", "import threading; threading.Event().wait()",
            ], "log_file": server._log_file(worker_id),
        }
        server._save_worker(meta)
        with mock.patch.object(server, "route_task", return_value={
            "picks": [{"provider": "claude", "model": "sonnet"}], "reason": "alternate",
        }), mock.patch.object(server, "find_claude_binary", return_value="/mock/claude"), \
             mock.patch.object(server, "t_spin_worker", return_value=f"Worker `{new_worker_id}` queued"), \
             mock.patch.object(server, "t_broadcast_track"), \
             mock.patch.object(server, "notify_desktop_event"), \
             mock.patch.object(server, "_ping_launcher"):
            self.assertEqual(server.supervise_worker(worker_id), 0)

        with open(server._exit_file(worker_id), encoding="utf-8") as exit_file:
            exit_result = json.load(exit_file)
        self.assertIn(new_worker_id, exit_result["reason"])
        self.assertIn("stalled", exit_result["reason"])
        self.assertNotIn("gpt-6-luna", server._load_json("bad_models.json"))
        self.assertIn("gpt-6-luna", server._load_json("stall_cooldowns.json"))

    def test_opencode_session_message_update_counts_as_activity(self):
        worker_dir = os.path.join(self.temp_dir, "worktree")
        os.makedirs(worker_dir)
        started_at = time.time() - 2
        meta = {"provider": "opencode", "project_dir": worker_dir}
        self._create_opencode_retry_db(
            worker_dir, {"tokens": {"input": 1}}, int((started_at + 1) * 1000),
        )
        self.assertEqual(
            server._opencode_worker_session_activity(meta, started_at),
            ("opencode-session", int((started_at + 1) * 1000)),
        )
        db_path = os.path.join(server.OPENCODE_DIR, "opencode.db")
        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE session_message SET time_updated=time_updated+1000")
            con.commit()
        finally:
            con.close()
        self.assertEqual(
            server._opencode_worker_session_activity(meta, started_at),
            ("opencode-session", int((started_at + 1) * 1000) + 1000),
        )

    def test_opencode_activity_tolerates_corrupt_or_locked_database(self):
        worker_dir = os.path.join(self.temp_dir, "worktree")
        os.makedirs(worker_dir)
        meta = {"provider": "opencode", "project_dir": worker_dir}
        db_path = os.path.join(server.OPENCODE_DIR, "opencode.db")
        os.makedirs(server.OPENCODE_DIR, exist_ok=True)
        with open(db_path, "wb") as database:
            database.write(b"not a sqlite database")
        self.assertIsNone(server._opencode_worker_session_activity(meta, time.time()))
        with mock.patch.object(server.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertIsNone(server._opencode_worker_session_activity(meta, time.time()))

    def test_cpu_time_increase_counts_as_activity(self):
        meta = {"provider": "custom", "log_file": os.path.join(self.temp_dir, "worker.log")}
        with open(meta["log_file"], "w", encoding="utf-8") as file:
            file.write("")
        proc = mock.Mock(pid=456)
        with mock.patch.object(server, "_worker_folder_activity", return_value=None), \
             mock.patch.object(server, "_process_cpu_time", side_effect=[2.0, 3.5]):
            previous = server._worker_activity_signals(meta, proc, 0)
            last_active = {key: 1.0 for key in previous}
            current = server._worker_activity_signals(meta, proc, 0)
            server._update_worker_activity(previous, last_active, current, 5.0)

        self.assertEqual(last_active["cpu"], 5.0)

    def test_worker_folder_changes_count_as_activity_outside_git(self):
        project_dir = os.path.join(self.temp_dir, "not-a-repository")
        os.makedirs(project_dir)
        work_file = os.path.join(project_dir, "work.txt")
        with open(work_file, "w", encoding="utf-8") as file:
            file.write("started")
        meta = {
            "provider": "custom", "project_dir": project_dir,
            "log_file": os.path.join(self.temp_dir, "worker.log"),
        }
        open(meta["log_file"], "w", encoding="utf-8").close()
        with mock.patch.object(server, "_process_cpu_time", return_value=None):
            previous = server._worker_activity_signals(meta, mock.Mock(pid=789), 0)
            last_active = {key: 1.0 for key in previous}
            with open(work_file, "w", encoding="utf-8") as file:
                file.write("made progress")
            current = server._worker_activity_signals(meta, mock.Mock(pid=789), 0)
            server._update_worker_activity(previous, last_active, current, 5.0)

        self.assertEqual(last_active["worker_folder"], 5.0)

    def test_quota_error_in_stderr_is_handled_while_process_is_running(self):
        worker_id = "worker_888888"
        meta = {
            "id": worker_id,
            "role": "Worker",
            "provider": "opencode",
            "model": "opencode/example-free",
            "prompt": "Run a task",
            "project_dir": self.temp_dir,
            "timeout_seconds": 30,
            "stall_timeout": 20,
            "status": "running",
            "cmd": [
                sys.executable, "-c",
                "import sys,threading; print('Free usage limit reached', file=sys.stderr, flush=True); threading.Event().wait()",
            ],
            "log_file": server._log_file(worker_id),
        }
        server._save_worker(meta)
        t_start = time.time()
        with mock.patch.object(server, "_handle_worker_exhaustion_or_stall", return_value={"status": "failed"}) as handler:
            self.assertEqual(server.supervise_worker(worker_id), 0)
        self.assertLess(time.time() - t_start, 4)
        handler.assert_called_once()

    def test_worker_watchdogs_exit_after_normal_finish(self):
        worker_id = "worker_999999"
        meta = {
            "id": worker_id,
            "role": "Worker",
            "provider": "custom",
            "model": "custom-model",
            "prompt": "Finish",
            "project_dir": self.temp_dir,
            "timeout_seconds": 10,
            "status": "running",
            "cmd": [sys.executable, "-c", "print('done')"],
            "log_file": server._log_file(worker_id),
        }
        server._save_worker(meta)
        original_thread = threading.Thread
        worker_threads = []

        def capture_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            if str(kwargs.get("name", "")).startswith(("worker-budget-", "worker-stall-")):
                worker_threads.append(thread)
            return thread

        with mock.patch.object(server.threading, "Thread", side_effect=capture_thread):
            self.assertEqual(server.supervise_worker(worker_id), 0)

        self.assertEqual(len(worker_threads), 2)
        self.assertTrue(all(not thread.is_alive() for thread in worker_threads))

    def test_private_reroute_preserves_original_spin_arguments(self):
        worker_id = "worker_101010"
        new_worker_id = "worker_202020"
        args = {
            "role": "Private Worker",
            "prompt": "Handle private data",
            "track": "private-track",
            "project_dir": self.temp_dir,
            "timeout_seconds": 123,
            "effort": "high",
            "mode": "standard",
            "permission_mode": "auto_safe",
            "capabilities": ["files:read", "files:write"],
            "budget_session_pct": 12,
            "budget_weekly_pct": 34,
            "max_budget_usd": 5.5,
            "thread": "private-thread",
            "private": True,
            "max_tokens": 4500,
            "max_seconds": 90,
            "isolate": True,
            "priority": "urgent",
            "code": ["server.py:1-2"],
            "then": '[{"when":"completed","role":"follow-up","prompt":"check"}]',
            "review": "auto",
        }
        meta = {
            "id": worker_id,
            "provider": "local",
            "model": "ollama/old-free",
            "prompt": args["prompt"],
            "private": True,
            "launched_by": "launcher",
            "spin_args": {**args, "provider": "local", "model": "ollama/old-free"},
        }
        route = {
            "private": True,
            "reason": "Private data, so it stays on this machine.",
            "picks": [
                {"provider": "claude", "model": "sonnet"},
                {"provider": "local", "model": "ollama/new-model"},
            ],
        }
        new_meta = {"id": new_worker_id}
        with mock.patch.object(server, "route_task", return_value=route), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "t_spin_worker", return_value=f"Worker `{new_worker_id}` queued") as spin, \
             mock.patch.object(server, "_load_worker", return_value=new_meta), \
             mock.patch.object(server, "_save_worker") as save_worker, \
             mock.patch.object(server, "_ping_launcher"), \
             mock.patch.object(server, "notify_desktop_event") as notify:
            result = server._handle_worker_exhaustion_or_stall(worker_id, meta, "stalled", is_stall=True)

        kwargs = spin.call_args.kwargs
        self.assertEqual(kwargs["provider"], "local")
        self.assertEqual(kwargs["model"], "ollama/new-model")
        for key, value in args.items():
            self.assertEqual(kwargs[key], value, key)
        self.assertEqual(new_meta["launched_by"], "launcher")
        save_worker.assert_called_once_with(new_meta)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["rerouted_to"], new_worker_id)
        notify.assert_not_called()

    def test_stalled_copilot_reroutes_without_marking_model_exhausted(self):
        worker_id = "worker_212121"
        new_worker_id = "worker_232323"
        meta = {
            "id": worker_id, "provider": "copilot", "model": "gpt-6-luna",
            "prompt": "Make the requested change", "project_dir": self.temp_dir,
        }
        new_meta = {"id": new_worker_id}
        with mock.patch.object(server, "route_task", return_value={
            "picks": [{"provider": "claude", "model": "sonnet"}], "reason": "alternate",
        }), mock.patch.object(server, "find_claude_binary", return_value="/mock/claude"), \
             mock.patch.object(server, "t_spin_worker", return_value=f"Worker `{new_worker_id}` queued"), \
             mock.patch.object(server, "_load_worker", return_value=new_meta), \
             mock.patch.object(server, "_save_worker"), mock.patch.object(server, "_ping_launcher"), \
             mock.patch.object(server, "t_broadcast_track"):
            result = server._handle_worker_exhaustion_or_stall(worker_id, meta, "stalled", is_stall=True)

        self.assertEqual(result["status"], "stopped")
        self.assertIn("stalled", result["reason"])
        self.assertNotIn("gpt-6-luna", server._load_json("bad_models.json"))
        cooldown = server._load_json("stall_cooldowns.json")["gpt-6-luna"]
        self.assertGreater(cooldown["until"], time.time())
        self.assertLessEqual(cooldown["until"], time.time() + 15 * 60)

    def test_corrupt_quota_and_stall_json_are_recovered(self):
        for filename in ("bad_models.json", "stall_cooldowns.json"):
            with open(os.path.join(self.temp_dir, filename), "w", encoding="utf-8") as state_file:
                state_file.write("{")
        self.assertFalse(server._model_is_bad("recoverable-model"))
        reset_time = time.time() + 60
        server._mark_model_bad("recoverable-model", "usage limit reached", reset_time=reset_time)
        self.assertTrue(server._model_is_bad("recoverable-model"))
        server._mark_model_stall_cooldown("recoverable-model")
        with open(os.path.join(self.temp_dir, "bad_models.json"), encoding="utf-8") as state_file:
            self.assertAlmostEqual(json.load(state_file)["recoverable-model"]["reset_time"], reset_time)
        with open(os.path.join(self.temp_dir, "stall_cooldowns.json"), encoding="utf-8") as state_file:
            self.assertGreater(json.load(state_file)["recoverable-model"]["until"], time.time())

    def test_spin_worker_persists_privacy_and_all_original_spin_arguments(self):
        captured = {}
        spin_args = {
            "role": "Private Worker",
            "prompt": "Keep data local",
            "provider": "local",
            "track": "private-track",
            "project_dir": self.temp_dir,
            "timeout_seconds": 123,
            "effort": "high",
            "mode": "standard",
            "model": "private-model",
            "permission_mode": "auto_safe",
            "capabilities": ["files:read", "files:write"],
            "budget_session_pct": 12,
            "budget_weekly_pct": 34,
            "max_budget_usd": 5.5,
            "thread": "private-thread",
            "private": True,
            "max_tokens": 4500,
            "max_seconds": 90,
            "isolate": True,
            "priority": "urgent",
            "code": None,
            "then": "[]",
            "review": "auto",
        }

        def capture_meta(meta):
            captured.update(meta)

        with mock.patch.object(server, "route_task", return_value={
            "private": True, "reason": "Private data, so it stays on this machine.",
            "picks": [{"provider": "local", "model": "private-model"}],
        }), mock.patch.object(server, "find_opencode_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "_worker_command", return_value=["true"]), \
             mock.patch.object(server, "resolve", return_value={"source": "local", "id": "thread-id"}), \
             mock.patch.object(server, "check_budget_preflight", return_value=(True, "budget ok")), \
             mock.patch.object(server, "_save_worker", side_effect=capture_meta), \
             mock.patch.object(server, "t_broadcast_track"), \
             mock.patch.object(server, "notify_if_still_waiting"):
            result = server.t_spin_worker(**spin_args)

        self.assertIn("[APPROVAL REQUIRED]", result)
        self.assertTrue(captured["private"])
        for key in ("max_tokens", "max_seconds", "budget_session_pct", "budget_weekly_pct", "max_budget_usd",
                    "permission_mode", "capabilities", "isolate", "mode", "effort", "priority", "thread",
                    "track", "project_dir", "code", "then", "review"):
            expected = os.path.realpath(spin_args[key]) if key == "project_dir" else spin_args[key]
            self.assertEqual(captured["spin_args"][key], expected, key)

    def test_private_worker_without_local_alternative_fails_and_alerts(self):
        worker_id = "worker_303030"
        meta = {
            "id": worker_id,
            "provider": "local",
            "model": "ollama/old-model",
            "prompt": "private",
            "private": True,
            "launched_by": "launcher",
        }
        with mock.patch.object(server, "route_task", return_value={
            "private": True, "reason": "Private data, so it stays on this machine.",
            "picks": [{"provider": "claude", "model": "sonnet"}],
        }), mock.patch.object(server, "_ping_launcher"), \
             mock.patch.object(server, "notify_desktop_event") as notify:
            result = server._handle_worker_exhaustion_or_stall(worker_id, meta, "stalled", is_stall=True)

        self.assertEqual(result["status"], "failed")
        self.assertIn("cannot leave the local provider", result["reason"])
        notify.assert_called_once()
        self.assertIn(worker_id, notify.call_args.args[0])
        self.assertEqual(notify.call_args.kwargs["route"], "workers")

    def test_opencode_free_usage_limit_marks_all_free_models_only(self):
        worker_id = "worker_404040"
        reset_time = time.time() + 1800
        meta = {
            "id": worker_id,
            "provider": "opencode",
            "model": "opencode/first-free",
            "prompt": "task",
        }
        with mock.patch.object(server, "_opencode_models", return_value=[
            "opencode/first-free", "opencode/second-free", "opencode/paid-model",
        ]), mock.patch.object(server, "route_task", return_value={"picks": [], "reason": "No candidates"}), \
             mock.patch.object(server, "_ping_launcher"), mock.patch.object(server, "notify_desktop_event"):
            result = server._handle_worker_exhaustion_or_stall(
                worker_id, meta, "Free usage limit reached", reset_time=reset_time,
            )

        self.assertEqual(result["status"], "failed")
        bad = server._load_json("bad_models.json")
        self.assertAlmostEqual(bad["opencode/first-free"]["reset_time"], reset_time)
        self.assertAlmostEqual(bad["opencode/second-free"]["reset_time"], reset_time)
        self.assertNotIn("opencode/paid-model", bad)
        self.assertTrue(server._model_is_bad("opencode/second-free"))
        self.assertFalse(server._model_is_bad("opencode/paid-model"))
        with mock.patch.object(server.time, "time", return_value=reset_time + 1):
            self.assertFalse(server._model_is_bad("opencode/second-free"))

    def test_quota_error_worker_stops_marks_exhausted_and_reroutes(self):
        """A fake worker whose output contains a quota error is stopped, the model marked exhausted,

        and a new worker with the same prompt queued on a different model.
        """
        worker_id = "worker_111111"
        log_file = server._log_file(worker_id)
        prompt_text = "Implement rate-limiter unit tests"
        meta = {
            "id": worker_id,
            "role": "Unit Tester",
            "provider": "opencode",
            "model": "free-oc-model-1",
            "prompt": prompt_text,
            "project_dir": self.temp_dir,
            "timeout_seconds": 60,
            "priority": "urgent",
            "review": "auto",
            "launched_by": "ses_test_launcher",
            "track": "main",
            "status": "running",
            "pid": os.getpid(),
            "cmd": [sys.executable, "-c", "import sys; print('Error: free limit reached HTTP 429'); sys.exit(1)"],
            "log_file": log_file,
        }
        server._save_worker(meta)

        # Mock route_task so that when called, it returns alternative models
        mock_route = {
            "order": ["claude", "opencode"],
            "picks": [
                {"provider": "opencode", "model": "free-oc-model-1"},
                {"provider": "claude", "model": "sonnet"},
            ],
            "tier": "medium",
            "effort": "medium",
            "private": False,
            "skill": "coding",
            "reason": "Claude Sonnet selected as capable alternative.",
        }

        with mock.patch.object(server, "route_task", return_value=mock_route), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/sh"):
            code = server.supervise_worker(worker_id)
            self.assertEqual(code, 0)

        # 1. Original worker must be stopped
        updated_w1 = server._load_worker(worker_id)
        updated_w1 = server._update_worker_status(updated_w1)
        self.assertEqual(updated_w1.get("status"), "stopped")

        # 2. Model marked exhausted in store
        self.assertTrue(server._model_is_bad("free-oc-model-1"))
        bad_store = server._load_json("bad_models.json")
        self.assertIn("free-oc-model-1", bad_store)

        # 3. New worker queued on a different model with the same prompt
        all_workers = server._all_workers()
        new_workers = [w for w in all_workers if w["id"] != worker_id]
        self.assertEqual(len(new_workers), 1)
        w2 = new_workers[0]
        self.assertEqual(w2["status"], "pending_approval")
        self.assertTrue(w2["prompt"].startswith(prompt_text))
        self.assertEqual(w2["prompt"].count("SUBWAY STANDING RULES"), 1)
        self.assertEqual(w2["model"], "sonnet")
        self.assertEqual(w2["provider"], "claude")
        self.assertEqual(w2["rerouted_from"], worker_id)
        self.assertEqual(w2["launched_by"], "ses_test_launcher")
        self.assertEqual(w2["priority"], "urgent")

        # 4. Launcher pinged in inbox
        inbox_file = os.path.join(self.temp_dir, "inbox", "ses_test_launcher.jsonl")
        self.assertTrue(os.path.exists(inbox_file))
        with open(inbox_file, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(any("free-oc-model-1" in l["text"] and w2["id"] in l["text"] for l in lines))

    def test_silent_worker_past_stall_deadline_handled_same(self):
        """A silent worker is stopped, given a short cooldown, and rerouted."""
        worker_id = "worker_222222"
        log_file = server._log_file(worker_id)
        prompt_text = "Perform database migration"
        meta = {
            "id": worker_id,
            "role": "Database Architect",
            "provider": "opencode",
            "model": "silent-model-x",
            "prompt": prompt_text,
            "project_dir": self.temp_dir,
            "timeout_seconds": 60,
            "stall_timeout": 0.3,  # Quick stall timeout for test
            "priority": "normal",
            "launched_by": "ses_silent_launcher",
            "track": "main",
            "status": "running",
            "pid": os.getpid(),
            "cmd": [sys.executable, "-c", "import threading; threading.Event().wait()"],
            "log_file": log_file,
        }
        server._save_worker(meta)

        mock_route = {
            "order": ["claude"],
            "picks": [
                {"provider": "opencode", "model": "silent-model-x"},
                {"provider": "claude", "model": "opus"},
            ],
            "tier": "high",
            "effort": "high",
            "private": False,
            "skill": "coding",
            "reason": "Claude Opus selected.",
        }

        t_start = time.time()
        with mock.patch.object(server, "route_task", return_value=mock_route), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/sh"):
            code = server.supervise_worker(worker_id)
            self.assertEqual(code, 0)
        elapsed = time.time() - t_start

        # Should finish quickly because stall timeout is 0.3s
        self.assertLess(elapsed, 4.0)

        # 1. Original worker stopped
        updated_w1 = server._load_worker(worker_id)
        updated_w1 = server._update_worker_status(updated_w1)
        self.assertEqual(updated_w1.get("status"), "stopped")

        # A stall gets a short routing cooldown, never quota exhaustion.
        self.assertFalse(server._load_json("bad_models.json").get("silent-model-x"))
        self.assertTrue(server._model_is_bad("silent-model-x"))
        self.assertIn("silent-model-x", server._load_json("stall_cooldowns.json"))

        # 3. New worker queued on different model
        all_workers = server._all_workers()
        new_workers = [w for w in all_workers if w["id"] != worker_id]
        self.assertEqual(len(new_workers), 1)
        w2 = new_workers[0]
        self.assertEqual(w2["status"], "pending_approval")
        self.assertTrue(w2["prompt"].startswith(prompt_text))
        self.assertEqual(w2["prompt"].count("SUBWAY STANDING RULES"), 1)
        self.assertEqual(w2["model"], "opus")
        self.assertEqual(w2["rerouted_from"], worker_id)

    def test_second_exhaustion_does_not_reroute_again(self):
        """A second exhaustion of the rerouted worker does not reroute again."""
        worker_id = "worker_333333"
        log_file = server._log_file(worker_id)
        meta = {
            "id": worker_id,
            "role": "Worker",
            "provider": "opencode",
            "model": "model-exhausted-twice",
            "prompt": "Fix cache concurrency bug",
            "project_dir": self.temp_dir,
            "timeout_seconds": 60,
            "priority": "normal",
            "launched_by": "ses_chain_launcher",
            "track": "main",
            "status": "running",
            "pid": os.getpid(),
            "rerouted_from": "worker_orig_1",  # Already rerouted once
            "cmd": [sys.executable, "-c", "import sys; print('Error: Rate limit exceeded: HTTP 429'); sys.exit(1)"],
            "log_file": log_file,
        }
        server._save_worker(meta)

        all_before = len(server._all_workers())

        with mock.patch.object(server, "find_claude_binary", return_value="/bin/sh"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/sh"):
            code = server.supervise_worker(worker_id)
            self.assertEqual(code, 0)

        # 1. Rerouted worker must be marked failed
        updated_w2 = server._load_worker(worker_id)
        updated_w2 = server._update_worker_status(updated_w2)
        self.assertEqual(updated_w2.get("status"), "failed")
        self.assertIn("already rerouted", updated_w2.get("report", ""))

        # 2. NO new worker created (no loop)
        all_after = len(server._all_workers())
        self.assertEqual(all_before, all_after)

        # 3. Launcher received ping explaining second failure
        inbox_file = os.path.join(self.temp_dir, "inbox", "ses_chain_launcher.jsonl")
        self.assertTrue(os.path.exists(inbox_file))
        with open(inbox_file, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(any("already rerouted from `worker_orig_1`" in l["text"] for l in lines))

    def test_opencode_free_quota_skipped_by_route_task_and_shown_in_catalog(self):
        """OpenCode models that hit free limit within 24h are skipped by route_task and show as exhausted in model_catalog."""
        now = time.time()
        mock_prov_hit = {
            "id": "opencode",
            "name": "OpenCode",
            "state": "live",
            "client": {"dispatchable": True},
            "limit_hit": {
                "at": now - 3600,  # 1 hour ago (within 24 hours)
                "model": "gemini-2.0-flash",
                "message": "Free model limit reached",
            },
            "limited": {},
        }

        # 1. Within 24h: catalog shows exhausted
        with mock.patch.object(server, "_source", return_value={"data": mock_prov_hit, "fetched_at": now}), \
             mock.patch.object(server, "_opencode_models", return_value=["gemini-2.0-flash"]):
            server._model_catalog.clear()
            catalog = server.model_catalog()
            m_entry = next((m for m in catalog if m.get("model") == "gemini-2.0-flash"), None)
            self.assertIsNotNone(m_entry)
            self.assertTrue(m_entry.get("exhausted"))

        # 2. Within 24h: route_task skips this model
        mock_providers = [mock_prov_hit, {"id": "claude", "name": "Claude", "client": {"dispatchable": True}}]
        with mock.patch.object(server, "_opencode_models", return_value=["gemini-2.0-flash", "llama-3-70b"]):
            server._model_catalog.clear()
            route = server.route_task("write a script", providers=mock_providers)
            picked_models = [p.get("model") for p in route.get("picks", []) if p.get("provider") == "opencode"]
            self.assertNotIn("gemini-2.0-flash", picked_models)

        # 3. After 24h (a day has passed): no longer exhausted or skipped
        mock_prov_old = {
            "id": "opencode",
            "name": "OpenCode",
            "state": "live",
            "client": {"dispatchable": True},
            "limit_hit": {
                "at": now - 90000,  # 25 hours ago (> 24 hours)
                "model": "gemini-2.0-flash",
                "message": "Old limit",
            },
            "limited": {},
        }
        with mock.patch.object(server, "_source", return_value={"data": mock_prov_old, "fetched_at": now}), \
             mock.patch.object(server, "_opencode_models", return_value=["gemini-2.0-flash"]):
            server._model_catalog.clear()
            catalog_old = server.model_catalog()
            m_entry_old = next((m for m in catalog_old if m.get("model") == "gemini-2.0-flash"), None)
            self.assertIsNotNone(m_entry_old)
            self.assertFalse(m_entry_old.get("exhausted"))

        mock_providers_old = [mock_prov_old]
        with mock.patch.object(server, "_opencode_models", return_value=["gemini-2.0-flash"]):
            server._model_catalog.clear()
            route_old = server.route_task("write a python script", providers=mock_providers_old)
            picked_models_old = [p.get("model") for p in route_old.get("picks", []) if p.get("provider") == "opencode"]
            self.assertIn("gemini-2.0-flash", picked_models_old)

    def test_no_alternative_model_available_marks_failed(self):
        """If nothing else can run the task when model exhausts, worker is marked failed."""
        worker_id = "worker_444444"
        log_file = server._log_file(worker_id)
        meta = {
            "id": worker_id,
            "role": "Solo Worker",
            "provider": "opencode",
            "model": "only-model",
            "prompt": "Solo task",
            "project_dir": self.temp_dir,
            "timeout_seconds": 60,
            "priority": "normal",
            "launched_by": "ses_no_alt_launcher",
            "track": "main",
            "status": "running",
            "pid": os.getpid(),
            "cmd": [sys.executable, "-c", "import sys; print('Error: Free usage limit reached'); sys.exit(1)"],
            "log_file": log_file,
        }
        server._save_worker(meta)

        # route_task returns only the same model that is now excluded
        mock_route = {
            "order": ["opencode"],
            "picks": [{"provider": "opencode", "model": "only-model"}],
            "reason": "No other models available.",
        }

        with mock.patch.object(server, "route_task", return_value=mock_route):
            code = server.supervise_worker(worker_id)
            self.assertEqual(code, 0)

        updated = server._load_worker(worker_id)
        updated = server._update_worker_status(updated)
        self.assertEqual(updated.get("status"), "failed")
        self.assertIn("no alternative model", updated.get("report", ""))




class TestActivitySignalHelpers(unittest.TestCase):
    def test_cpu_time_parses_macos_fractional_seconds(self):
        proc = mock.MagicMock(pid=4242)
        for text, expected in (("  0:00.01\n", 0.01), ("225:57.98\n", 225 * 60 + 57.98), ("1-02:03:04\n", 93784.0)):
            done = mock.MagicMock(returncode=0, stdout=text)
            with mock.patch("builtins.open", side_effect=OSError("no /proc")), \
                 mock.patch.object(server.subprocess, "run", return_value=done):
                self.assertAlmostEqual(server._process_cpu_time(proc), expected, places=2)

    def test_folder_activity_never_watches_home_or_root(self):
        with mock.patch.object(server.subprocess, "run") as run, mock.patch.object(server.os, "walk") as walk:
            self.assertIsNone(server._worker_folder_activity({"project_dir": server.HOME}))
            self.assertIsNone(server._worker_folder_activity({"project_dir": os.sep}))
            run.assert_not_called()
            walk.assert_not_called()

    def test_folder_activity_changes_when_a_changed_file_grows(self):
        with tempfile.TemporaryDirectory() as folder:
            server.subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
            path = os.path.join(folder, "a.txt")
            with open(path, "w") as f:
                f.write("one")
            first = server._worker_folder_activity({"project_dir": folder})
            with open(path, "a") as f:
                f.write(" two")
            os.utime(path, (time.time() + 5, time.time() + 5))
            second = server._worker_folder_activity({"project_dir": folder})
            self.assertEqual(first[0], "git")
            self.assertNotEqual(first, second)


class SubworkerActivityTests(unittest.TestCase):
    def test_lead_waiting_on_subworker_counts_as_active(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "WORKERS_DIR", tmp):
            lead = {"id": "worker_lead1", "provider": "claude"}
            self.assertEqual(server._active_subworkers("worker_lead1"), [])
            for wid, status, parent in (("worker_sub1", "running", "worker_lead1"),
                                        ("worker_sub2", "completed", "worker_lead1"),
                                        ("worker_other", "running", "worker_x")):
                with open(os.path.join(tmp, f"{wid}.json"), "w") as f:
                    _json.dump({"id": wid, "status": status, "launched_by": parent}, f)
            with open(os.path.join(tmp, "worker_bad.json"), "w") as f:
                f.write("{corrupt")
            self.assertEqual(server._active_subworkers("worker_lead1"), ["worker_sub1"])
            self.assertEqual(server._active_subworkers(""), [])
            with mock.patch.object(server, "_worker_folder_activity", return_value=None), \
                    mock.patch.object(server, "_process_cpu_time", return_value=None):
                signals = server._collect_worker_activity_signals(lead, None, 0)
            self.assertEqual(signals["subworkers"][0], ("worker_sub1",))

if __name__ == "__main__":
    unittest.main()
