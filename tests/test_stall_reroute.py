import json
import os
import shutil
import sqlite3
import sys
import tempfile
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
        server.SUBWAY_DIR = self.temp_dir
        server.WORKERS_DIR = os.path.join(self.temp_dir, "workers")
        server.OPENCODE_DIR = os.path.join(self.temp_dir, ".local", "share", "opencode")
        os.makedirs(server.WORKERS_DIR, exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir, "inbox"), exist_ok=True)
        server._model_catalog.clear()

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.OPENCODE_DIR = self.orig_opencode_dir
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
        self.assertEqual(w2["prompt"], prompt_text)
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
        """A silent worker past the stall deadline is handled the same: stopped, model marked

        exhausted, and task rerouted.
        """
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
            "cmd": ["sleep", "10"],  # Emits no output
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

        # 2. Model marked exhausted
        self.assertTrue(server._model_is_bad("silent-model-x"))

        # 3. New worker queued on different model
        all_workers = server._all_workers()
        new_workers = [w for w in all_workers if w["id"] != worker_id]
        self.assertEqual(len(new_workers), 1)
        w2 = new_workers[0]
        self.assertEqual(w2["status"], "pending_approval")
        self.assertEqual(w2["prompt"], prompt_text)
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
            "cmd": [sys.executable, "-c", "import sys; print('Rate limit exceeded: 429'); sys.exit(1)"],
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
            "cmd": [sys.executable, "-c", "import sys; print('Free limit reached'); sys.exit(1)"],
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


if __name__ == "__main__":
    unittest.main()
