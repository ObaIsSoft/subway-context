#!/usr/bin/env python3
"""Comprehensive test suite for Subway Context."""
import os as _os
_os.environ["SUBWAY_NO_NOTIFY"] = "1"  # Never send real desktop alerts from tests.
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
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

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
import install


def _race_claim_worker(filepath, holder, project_dir, subway_dir, result_queue):
    """Top-level worker function for multiprocessing race test."""
    os.environ["SUBWAY_DIR"] = subway_dir
    server.SUBWAY_DIR = subway_dir
    server.LOCKS_FILE = os.path.join(subway_dir, "locks.json")
    server._ensure_subway_dir()
    res = server.t_claim_file(filepath, holder=holder, project_dir=project_dir, duration_seconds=100)
    result_queue.put(res)


def _claude_line(obj):
    """Format a JSONL line compact without spaces, as Claude Code writes it."""
    return json.dumps(obj, separators=(',', ':')) + "\n"


class SubwayTestCase(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="subway_tc_")
        self.subway_dir = os.path.join(self.test_dir, ".subway-context")
        self.claude_dir = os.path.join(self.test_dir, ".claude")
        self.claude_json = os.path.join(self.test_dir, ".claude.json")
        self.agy_dir = os.path.join(self.test_dir, ".gemini", "antigravity")
        self.opencode_dir = os.path.join(self.test_dir, ".local", "share", "opencode")
        self.opencode_agents_dir = os.path.join(self.test_dir, ".config", "opencode", "agents")

        os.makedirs(self.subway_dir, exist_ok=True)
        os.makedirs(os.path.join(self.claude_dir, "projects"), exist_ok=True)
        os.makedirs(self.agy_dir, exist_ok=True)
        os.makedirs(self.opencode_dir, exist_ok=True)
        os.makedirs(self.opencode_agents_dir, exist_ok=True)

        # Preserve original server constants
        self.orig_home = server.HOME
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_claude_dir = server.CLAUDE_DIR
        self.orig_claude_json = server.CLAUDE_JSON
        self.orig_agy_dir = server.AGY_DIR
        self.orig_opencode_dir = server.OPENCODE_DIR
        self.orig_opencode_agents_dir = server.OPENCODE_AGENTS_DIR
        self.orig_locks_file = server.LOCKS_FILE
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        self.orig_quotas_file = server.QUOTAS_FILE
        self.orig_sources = dict(server._SOURCES)
        self.orig_fetch_claude = server._fetch_claude_live
        self.orig_fetch_agy = server._fetch_agy_limits
        self.orig_self_id = server._self_id

        # Point server to test sandbox
        server.HOME = self.test_dir
        server.SUBWAY_DIR = self.subway_dir
        server.CLAUDE_DIR = self.claude_dir
        server.CLAUDE_JSON = self.claude_json
        server.AGY_DIR = self.agy_dir
        server.OPENCODE_DIR = self.opencode_dir
        server.OPENCODE_AGENTS_DIR = self.opencode_agents_dir
        server.LOCKS_FILE = os.path.join(self.subway_dir, "locks.json")
        server.WORKERS_DIR = os.path.join(self.subway_dir, "workers")
        server.TRACKS_DIR = os.path.join(self.subway_dir, "tracks")
        server.QUOTAS_FILE = os.path.join(self.subway_dir, "quotas.json")

        os.environ["HOME"] = self.test_dir
        os.environ["SUBWAY_DIR"] = self.subway_dir
        os.environ["SUBWAY_CLAUDE_DIR"] = self.claude_dir
        os.environ["SUBWAY_CLAUDE_JSON"] = self.claude_json
        os.environ["SUBWAY_AGY_DIR"] = self.agy_dir
        os.environ["SUBWAY_OPENCODE_DIR"] = self.opencode_dir
        os.environ["SUBWAY_OPENCODE_AGENTS_DIR"] = self.opencode_agents_dir

        server._ensure_subway_dir()

        # Mock Claude and Antigravity live limit fetchers
        self.mock_claude_data = {
            "five_hour": {"used_pct": 25.0, "resets_at": time.time() + 3600},
            "seven_day": {"used_pct": 15.0, "resets_at": time.time() + 86400 * 3},
        }
        self.mock_agy_data = {
            "plan": "Pro",
            "pools": [
                {
                    "key": "gemini_models",
                    "name": "Gemini Models",
                    "windows": [
                        {"key": "5h", "label": "5-hour", "left_pct": 80.0, "resets_at": time.time() + 3600, "disabled": False, "note": ""},
                        {"key": "weekly", "label": "Weekly", "left_pct": 60.0, "resets_at": time.time() + 86400 * 4, "disabled": False, "note": ""},
                    ],
                }
            ],
        }
        server._fetch_claude_live = lambda: self.mock_claude_data
        server._fetch_agy_limits = lambda: self.mock_agy_data
        server._SOURCES["claude_live"] = (lambda: self.mock_claude_data, 60.0)
        server._SOURCES["agy_limits"] = (lambda: self.mock_agy_data, 10.0)

        with server._usage_lock:
            server._usage.clear()

    def tearDown(self):
        server.HOME = self.orig_home
        server.SUBWAY_DIR = self.orig_subway_dir
        server.CLAUDE_DIR = self.orig_claude_dir
        server.CLAUDE_JSON = self.orig_claude_json
        server.AGY_DIR = self.orig_agy_dir
        server.OPENCODE_DIR = self.orig_opencode_dir
        server.OPENCODE_AGENTS_DIR = self.orig_opencode_agents_dir
        server.LOCKS_FILE = self.orig_locks_file
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server.QUOTAS_FILE = self.orig_quotas_file
        server._SOURCES = dict(self.orig_sources)
        server._fetch_claude_live = self.orig_fetch_claude
        server._fetch_agy_limits = self.orig_fetch_agy
        server._self_id = self.orig_self_id

        with server._usage_lock:
            server._usage.clear()

        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestLeases(SubwayTestCase):
    def test_relative_and_absolute_path_are_one_key(self):
        repo_dir = os.path.join(self.test_dir, "myrepo")
        os.makedirs(repo_dir, exist_ok=True)
        abs_path = os.path.join(repo_dir, "src", "index.ts")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("// code\n")

        res1 = server.t_claim_file("src/index.ts", holder="agentA", project_dir=repo_dir)
        self.assertIn("[LEASE ACQUIRED]", res1)

        res2 = server.t_claim_file(abs_path, holder="agentB")
        self.assertIn("[COLLISION WARNING]", res2)
        self.assertIn("already leased to agentA", res2)

    def test_different_repos_do_not_collide(self):
        repo1 = os.path.join(self.test_dir, "repo1")
        repo2 = os.path.join(self.test_dir, "repo2")
        os.makedirs(repo1, exist_ok=True)
        os.makedirs(repo2, exist_ok=True)

        res1 = server.t_claim_file("README.md", holder="agent1", project_dir=repo1)
        res2 = server.t_claim_file("README.md", holder="agent2", project_dir=repo2)
        self.assertIn("[LEASE ACQUIRED]", res1)
        self.assertIn("[LEASE ACQUIRED]", res2)

    def test_two_unknown_callers_collide(self):
        target = os.path.join(self.test_dir, "collide_target.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("target\n")

        server._self_id = lambda: "unknown-proc-100"
        r1 = server.t_claim_file(target, holder="")
        self.assertIn("[LEASE ACQUIRED]", r1)

        server._self_id = lambda: "unknown-proc-200"
        r2 = server.t_claim_file(target, holder="")
        self.assertIn("[COLLISION WARNING]", r2)
        self.assertIn("already leased to unknown-proc-100", r2)

    def test_eight_processes_racing_produce_one_winner(self):
        race_file = os.path.join(self.test_dir, "race_target.txt")
        with open(race_file, "w", encoding="utf-8") as f:
            f.write("racing\n")

        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = []
        for i in range(8):
            p = ctx.Process(target=_race_claim_worker, args=(race_file, f"racer_{i}", self.test_dir, self.subway_dir, q))
            procs.append(p)

        for p in procs:
            p.start()

        results = [q.get(timeout=10) for _ in range(8)]
        for p in procs:
            p.join(timeout=5)

        acquired = [r for r in results if "[LEASE ACQUIRED]" in r]
        collisions = [r for r in results if "[COLLISION WARNING]" in r]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(collisions), 7)

    def test_self_approval_refused(self):
        filepath = os.path.join(self.test_dir, "auth.py")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("secret\n")

        server._self_id = lambda: "agent-alpha"
        res_claim = server.t_claim_file(filepath, holder="agent-alpha")
        self.assertIn("[LEASE ACQUIRED]", res_claim)

        # Self-approval must be refused
        res_signoff = server.t_signoff_file(filepath, verdict="approved", reviewer="agent-alpha")
        self.assertIn("[SIGNOFF REFUSED]", res_signoff)

        # Different agent approves successfully
        server._self_id = lambda: "agent-beta"
        res_signoff2 = server.t_signoff_file(filepath, verdict="approved", reviewer="agent-beta")
        self.assertIn("[SIGNOFF: APPROVED]", res_signoff2)

    def test_signoff_survives_a_claim(self):
        filepath = os.path.join(self.test_dir, "feature.py")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("feature\n")

        server._self_id = lambda: "author-1"
        server.t_claim_file(filepath, holder="author-1")

        server._self_id = lambda: "reviewer-1"
        sign_res = server.t_signoff_file(filepath, verdict="approved", reviewer="reviewer-1")
        self.assertIn("[SIGNOFF: APPROVED]", sign_res)

        # Release initial claim
        server.t_release_file(filepath, holder="author-1")

        # Coordinator claims approved file
        server._self_id = lambda: "coord-1"
        claim_res = server.t_claim_file(filepath, holder="coord-1", role="coordinator")
        self.assertIn("[LEASE ACQUIRED]", claim_res)

        canon = server._canon_path(filepath)
        locks = server.load_locks()
        self.assertIn(canon, locks["signoffs"])
        self.assertEqual(locks["signoffs"][canon]["verdict"], "APPROVED")

    def test_needs_work_persists(self):
        filepath = os.path.join(self.test_dir, "needs_work.py")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("buggy\n")

        server._self_id = lambda: "reviewer-2"
        sign_res = server.t_signoff_file(filepath, verdict="needs_work", notes="Needs edge case test", reviewer="reviewer-2")
        self.assertIn("[SIGNOFF: NEEDS_WORK]", sign_res)

        canon = server._canon_path(filepath)
        locks = server.load_locks()
        self.assertIn(canon, locks["signoffs"])
        self.assertEqual(locks["signoffs"][canon]["verdict"], "NEEDS_WORK")

        # Reload directly to verify persistence
        locks_reloaded = server.load_locks()
        self.assertEqual(locks_reloaded["signoffs"][canon]["verdict"], "NEEDS_WORK")


class TestReadPath(SubwayTestCase):
    def test_large_transcript_decisions_and_search(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_large")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chatlarge123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            f.write(_claude_line({"type": "ai-title", "aiTitle": "Large Session"}))
            f.write(_claude_line({
                "type": "user",
                "message": {"content": [{"type": "text", "text": "Plan the architecture."}]},
                "timestamp": "2026-11-01T10:00:00Z"
            }))
            # Padding over 15MB of progress lines
            f.write(_claude_line({"type": "progress", "data": "X" * 15_000_100}))
            f.write(_claude_line({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "DECISION: Switched database pool to connection manager."}]},
                "timestamp": "2026-11-01T10:05:00Z"
            }))

        self.assertGreater(os.path.getsize(chat_path), 15_000_000)

        # 1. Decisions mode returns newest message
        decisions_res = server.t_read_chat(chat_id, mode="decisions")
        self.assertIn("connection manager", decisions_res)

        # 2. Search finds it across large transcripts
        search_res = server.t_search_chat(quote="connection manager")
        self.assertIn(chat_id[:8], search_res)
        self.assertIn("connection manager", search_res)

    def test_first_since_last_says_what_was_skipped(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_delta")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chatdelta123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(_claude_line({
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": f"User question {i}"}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:00Z"
                }))
                f.write(_claude_line({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"Assistant reply {i}"}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:30Z"
                }))

        server._self_id = lambda: "reader-window-1"
        res = server.t_read_chat(chat_id, since="last", turns=3)
        self.assertIn("First transit between these two windows: showing the latest 3 of 10 stations.", res)
        self.assertIn("Stations 0-6 were not delivered; read them with start=0.", res)

    def test_plain_read_does_not_move_watermark(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_plain")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chatplain123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            f.write(_claude_line({
                "type": "user",
                "message": {"content": [{"type": "text", "text": "Question 1"}]},
                "timestamp": "2026-11-01T10:00:00Z"
            }))
            f.write(_claude_line({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Answer 1"}]},
                "timestamp": "2026-11-01T10:01:00Z"
            }))

        server._self_id = lambda: "reader-window-2"
        cps_before = server.get_checkpoints("reader-window-2", chat_id)
        self.assertEqual(len(cps_before), 0)

        server.t_read_chat(chat_id, mode="recent")
        cps_after = server.get_checkpoints("reader-window-2", chat_id)
        self.assertEqual(len(cps_after), 0)

    def test_truncated_read_continues_from_undelivered_station(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_trunc")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chattrunc123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(_claude_line({
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": f"Prompt {i} " + ("word " * 60)}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:00Z"
                }))
                f.write(_claude_line({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"Response {i} " + ("reply " * 60)}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:30Z"
                }))

        server._self_id = lambda: "reader-window-3"
        # Reading with a small character budget causes truncation
        res1 = server.t_read_chat(chat_id, since="last", turns=5, max_chars=400)
        cps = server.get_checkpoints("reader-window-3", chat_id)
        self.assertTrue(len(cps) > 0)
        watermark = int(cps[-1].get(chat_id, 0))
        self.assertLess(watermark, 5)

        # Next transit boards exactly at watermark
        res2 = server.t_read_chat(chat_id, since="last")
        self.assertIn(f"boarding at station [{watermark}]", res2)

    def test_start_zero_works(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_zero")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chatzero123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            f.write(_claude_line({
                "type": "user",
                "message": {"content": [{"type": "text", "text": "Station Zero Content"}]},
                "timestamp": "2026-11-01T10:00:00Z"
            }))
            f.write(_claude_line({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Station Zero Reply"}]},
                "timestamp": "2026-11-01T10:01:00Z"
            }))

        res = server.t_read_chat(chat_id, start=0)
        self.assertIn("### [0]", res)
        self.assertIn("Station Zero Content", res)

    def test_bridge_does_not_rewind(self):
        proj_dir = os.path.join(self.claude_dir, "projects", "proj_bridge")
        os.makedirs(proj_dir, exist_ok=True)
        chat_id = "chatbridge123456"
        chat_path = os.path.join(proj_dir, f"{chat_id}.jsonl")

        with open(chat_path, "w", encoding="utf-8") as f:
            for i in range(4):
                f.write(_claude_line({
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": f"Message {i}"}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:00Z"
                }))
                f.write(_claude_line({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"Answer {i}"}]},
                    "timestamp": f"2026-11-01T10:{i:02d}:30Z"
                }))

        server._self_id = lambda: "reader-window-4"
        server.t_read_chat(chat_id, since="last")
        cps1 = server.get_checkpoints("reader-window-4", chat_id)
        self.assertTrue(len(cps1) > 0)
        wm1 = cps1[-1].get(chat_id)

        # Historical bridge read does not rewind watermark
        server.t_read_chat(chat_id, since="bridge -1")
        cps2 = server.get_checkpoints("reader-window-4", chat_id)
        wm2 = cps2[-1].get(chat_id)
        self.assertEqual(wm1, wm2)


class TestWorkers(SubwayTestCase):
    def test_exit_7_is_failed_with_exit_code_7(self):
        worker_id = "worker_000007"
        log_file = server._log_file(worker_id)
        meta = {
            "id": worker_id,
            "role": "Exit Tester",
            "provider": "claude",
            "cmd": ["/bin/sh", "-c", "exit 7"],
            "timeout_seconds": 10,
            "project_dir": self.test_dir,
            "log_file": log_file,
            "status": "running",
            "pid": os.getpid(),
        }
        server._save_worker(meta)

        code = server.supervise_worker(worker_id)
        self.assertEqual(code, 0)

        updated = server._load_worker(worker_id)
        updated = server._update_worker_status(updated)
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["exit_code"], 7)

    def test_timeout_gives_timed_out(self):
        worker_id = "worker_000001"
        log_file = server._log_file(worker_id)
        meta = {
            "id": worker_id,
            "role": "Sleep Tester",
            "provider": "claude",
            "cmd": ["/bin/sh", "-c", "sleep 10"],
            "timeout_seconds": 1,
            "project_dir": self.test_dir,
            "log_file": log_file,
            "status": "running",
            "pid": os.getpid(),
        }
        server._save_worker(meta)

        server.supervise_worker(worker_id)
        updated = server._load_worker(worker_id)
        updated = server._update_worker_status(updated)
        self.assertEqual(updated["status"], "timed_out")

    def test_reject_and_stop_end_the_process(self):
        # 1. Stop a running process
        proc = subprocess.Popen(["sleep", "60"])
        wid_running = "worker_000002"
        server._save_worker({
            "id": wid_running,
            "role": "Runner",
            "status": "running",
            "pid": proc.pid,
            "track": "main",
            "log_file": server._log_file(wid_running),
        })

        stop_res = server.t_stop_worker(wid_running, reason="Test termination")
        self.assertIn("[STOPPING]", stop_res)
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())

        # 2. Reject a pending worker
        wid_pending = "worker_000003"
        server._save_worker({
            "id": wid_pending,
            "role": "Queued",
            "status": "pending_approval",
            "track": "main",
            "log_file": server._log_file(wid_pending),
        })

        rej_res = server.t_reject_worker(wid_pending, reason="Not needed")
        self.assertIn("[REJECTED]", rej_res)
        meta_rej = server._load_worker(wid_pending)
        self.assertEqual(meta_rej["status"], "rejected")

    def test_spin_never_launches_without_approval(self):
        res = server.t_spin_worker(role="Unit Tester", prompt="Run pytest suite", provider="claude")
        self.assertIn("[APPROVAL REQUIRED]", res)

        workers = server._all_workers()
        self.assertTrue(len(workers) > 0)
        latest = workers[0]
        self.assertEqual(latest["status"], "pending_approval")
        self.assertIsNone(latest["pid"])

    def test_wrong_code_refused(self):
        res = server.t_spin_worker(role="Linter", prompt="Run flake8", provider="claude")
        self.assertIn("[APPROVAL REQUIRED]", res)
        wid = server._all_workers()[0]["id"]

        wrong_res = server.t_approve_worker(wid, approval_code="BAD123")
        self.assertIn("[APPROVAL REFUSED]", wrong_res)

        real_code = server.approval_code(wid)
        good_res = server.t_approve_worker(wid, approval_code=real_code)
        self.assertIn("[APPROVED & LAUNCHED]", good_res)

    def test_audit_everything_in_prompt_does_not_turn_on_skip_permissions(self):
        server.t_spin_worker(role="Auditor", prompt="audit everything in the system", provider="claude")
        wid = server._all_workers()[0]["id"]
        meta = server._load_worker(wid)
        self.assertFalse(meta["skip_permissions"])
        self.assertNotIn("--dangerously-skip-permissions", meta["cmd"])

    def test_invalid_worker_id_rejected(self):
        self.assertIn("not found", server.t_approve_worker("../traversal", approval_code="123456"))
        self.assertIn("not found", server.t_reject_worker("nonexistent_worker"))
        self.assertIn("not found", server.t_stop_worker("bad_id_123"))


class TestBudget(SubwayTestCase):
    def test_provider_claude_uses_claude_limits_not_antigravity(self):
        self.mock_claude_data = {
            "five_hour": {"used_pct": 20.0, "resets_at": time.time() + 3600},
            "seven_day": {"used_pct": 10.0, "resets_at": time.time() + 86400 * 3},
        }
        self.mock_agy_data = {
            "plan": "Pro",
            "pools": [
                {
                    "key": "gemini_models",
                    "name": "Gemini Models",
                    "windows": [
                        {"key": "5h", "label": "5-hour", "left_pct": 99.0, "resets_at": time.time() + 3600, "disabled": False, "note": ""},
                        {"key": "weekly", "label": "Weekly", "left_pct": 99.0, "resets_at": time.time() + 86400 * 4, "disabled": False, "note": ""},
                    ],
                }
            ],
        }
        ok, msg = server.check_budget_preflight(model="sonnet", provider="claude")
        self.assertTrue(ok)
        self.assertIn("Claude Code", msg)
        self.assertIn("90.0% weekly", msg)
        self.assertNotIn("Gemini", msg)

    def test_auto_skips_exhausted_agent(self):
        # Set Claude to 0% weekly allowance left
        self.mock_claude_data = {
            "five_hour": {"used_pct": 50.0, "resets_at": time.time() + 3600},
            "seven_day": {"used_pct": 100.0, "resets_at": time.time() + 86400 * 3},
        }
        # In God mode, candidate order is ["claude", "antigravity", "opencode"]
        server.t_spin_worker(role="Architect", prompt="Complete overhaul", provider="auto", mode="god")
        wid = server._all_workers()[0]["id"]
        meta = server._load_worker(wid)
        # Claude should be skipped due to 0% allowance; Antigravity chosen instead
        self.assertEqual(meta["provider"], "antigravity")

    def test_usd_cap_on_opencode_refused(self):
        ok, msg = server.check_budget_preflight(provider="opencode", max_budget_usd=10.0)
        self.assertFalse(ok)
        self.assertIn("[BUDGET GUARD ALERT]", msg)
        self.assertIn("can't enforce a USD cap", msg)

    def test_disabled_window_ignored(self):
        self.mock_agy_data = {
            "plan": "Pro",
            "pools": [
                {
                    "key": "gemini_models",
                    "name": "Gemini Models",
                    "windows": [
                        {"key": "5h", "label": "5-hour", "left_pct": 0.0, "resets_at": time.time() + 3600, "disabled": True, "note": ""},
                        {"key": "weekly", "label": "Weekly", "left_pct": 75.0, "resets_at": time.time() + 86400 * 4, "disabled": False, "note": ""},
                    ],
                }
            ],
        }
        ok, msg = server.check_budget_preflight(model="gemini-pro", provider="antigravity")
        self.assertTrue(ok)
        self.assertIn("Budget approved", msg)


class TestHTTPServer(SubwayTestCase):
    def setUp(self):
        super().setUp()
        self.httpd = server.SubwayHUDServer(("127.0.0.1", 0), server.SubwayHUDHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = server.hud_token()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _request(self, path, method="GET", headers=None, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers=headers or {}, data=body, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.getcode(), resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            finally:
                e.close()
            return e.code, body, e.headers

    def test_no_token_gives_401(self):
        code, body, _ = self._request("/api/status")
        self.assertEqual(code, 401)
        self.assertIn(b"Missing or invalid token", body)

    def test_bad_host_and_bad_origin_give_403(self):
        # Bad Host
        code1, body1, _ = self._request("/", headers={"Host": "evil.example.com"})
        self.assertEqual(code1, 403)
        self.assertIn(b"Host not allowed", body1)

        # Bad Origin
        code2, body2, _ = self._request("/api/status", headers={
            "X-Subway-Token": self.token,
            "Origin": "http://attacker.example.com",
        })
        self.assertEqual(code2, 403)
        self.assertIn(b"Origin not allowed", body2)

    def test_text_plain_post_gives_415(self):
        code, body, _ = self._request("/api/claim", method="POST", headers={
            "X-Subway-Token": self.token,
            "Content-Type": "text/plain",
        }, body=b"raw text payload")
        self.assertEqual(code, 415)
        self.assertIn(b"application/json", body)

    def test_path_traversal_in_worker_log_gives_400(self):
        code, body, _ = self._request("/api/worker_log?id=../../etc/passwd", headers={
            "X-Subway-Token": self.token,
        })
        self.assertEqual(code, 400)
        self.assertIn(b"invalid worker id", body)

    def test_spawn_never_launches(self):
        payload = json.dumps({"role": "Linter", "prompt": "lint files"}).encode("utf-8")
        code, body, _ = self._request("/api/spawn", method="POST", headers={
            "X-Subway-Token": self.token,
            "Content-Type": "application/json",
        }, body=payload)
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("[APPROVAL REQUIRED]", data.get("result", ""))

        workers = server._all_workers()
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["status"], "pending_approval")
        self.assertIsNone(workers[0]["pid"])

    def test_no_cors_headers_anywhere(self):
        endpoints = [
            ("/", "GET", None, None),
            ("/healthz", "GET", None, None),
            ("/api/status", "GET", {"X-Subway-Token": self.token}, None),
            ("/hud/app.js", "GET", None, None),
        ]
        for path, method, headers, body in endpoints:
            code, _, hdrs = self._request(path, method=method, headers=headers, body=body)
            self.assertIsNone(hdrs.get("Access-Control-Allow-Origin"), f"CORS header found on {path}")

    def test_hud_page_and_assets_and_traversal_refused(self):
        # 1. Main HUD page served with placeholders substituted
        code, body, hdrs = self._request("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", hdrs.get("Content-Type", ""))
        body_str = body.decode("utf-8")
        self.assertNotIn("__SUBWAY_TOKEN__", body_str)
        self.assertNotIn("__CSP_NONCE__", body_str)

        # 2. Assets under /hud/
        code_asset, body_asset, hdrs_asset = self._request("/hud/app.js")
        self.assertEqual(code_asset, 200)
        self.assertIn("text/javascript", hdrs_asset.get("Content-Type", ""))

        # 3. Path traversal refused
        code_trav, _, _ = self._request("/hud/../server.py")
        self.assertEqual(code_trav, 404)


class TestComplexityAndRegex(unittest.TestCase):
    def test_complexity_scale_and_whole_word(self):
        # Batch scale calculation
        c1 = server.estimate_complexity("an extraction job over 29,028 documents")
        self.assertEqual(c1["tier"], "high")
        self.assertEqual(c1["effort"], "high")

        # Whole-word match: 'catalog' does not match 'log'
        self.assertFalse(server._has_any("create a catalog of products", ["log"]))
        self.assertTrue(server._has_any("tail the server log", ["log"]))
        c2 = server.estimate_complexity("create a catalog of products")
        self.assertNotEqual(c2["effort"], "low")

    def test_destructive_regex(self):
        # False positive check
        self.assertFalse(bool(server.DESTRUCTIVE_RE.search("Confirm the transform")))

        # True positives
        self.assertTrue(bool(server.DESTRUCTIVE_RE.search("git push --force")))
        self.assertTrue(bool(server.DESTRUCTIVE_RE.search("git clean -fdx")))
        self.assertTrue(bool(server.DESTRUCTIVE_RE.search("DROP DATABASE prod")))

    def test_alias_traversal_rejected(self):
        with self.assertRaises(ValueError):
            server.save_alias("../../traversal", "chat123")

        with self.assertRaises(ValueError):
            server.t_tag_chat("../sneaky", "chat123")


class TestMCPAndPermissions(SubwayTestCase):
    def test_mcp_initialize_and_tools_list(self):
        # initialize
        init_res = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "subway-context")

        # tools/list
        list_res = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = list_res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("read_chat", tool_names)
        self.assertIn("claim_file", tool_names)

    def test_every_tool_schema_matches_its_function_signature(self):
        for tool_name, (fn, spec) in server.TOOLS.items():
            fn_params = inspect.signature(fn).parameters
            schema_props = spec.get("inputSchema", {}).get("properties", {})
            required = spec.get("inputSchema", {}).get("required", [])

            # Schema properties must match function parameters
            self.assertEqual(set(schema_props.keys()), set(fn_params.keys()),
                             f"Tool '{tool_name}' schema properties do not match signature parameters")

            # Schema required fields must be in signature
            for req in required:
                self.assertIn(req, fn_params, f"Required field '{req}' not in signature of '{tool_name}'")

    def test_permission_modes(self):
        # Default is 'ask'
        self.assertEqual(server.get_permission_mode(), "ask")

        # 'ask' mode: no --settings or accept-edits for agy, no Edit/Write/Bash in Claude's --allowedTools
        cmd_claude = server._worker_command("claude", "claude", "sonnet", "medium", False, ["files:read", "files:write", "terminal:safe"], "ask", "task", None, "")
        allowed_claude = [arg for arg in cmd_claude if arg.startswith("Read") or "Edit" in arg]
        for a in allowed_claude:
            self.assertNotIn("Edit", a)
            self.assertNotIn("Write", a)
            self.assertNotIn("Bash", a)

        cmd_agy = server._worker_command("antigravity", "agy", "inherit", "medium", False, ["files:read", "files:write", "terminal:safe"], "ask", "task", None, "")
        self.assertNotIn("--settings", cmd_agy)
        self.assertNotIn("accept-edits", cmd_agy)

        # 'auto_safe' mode adds the safe list
        cmd_claude_safe = server._worker_command("claude", "claude", "sonnet", "medium", False, ["files:read", "files:write", "terminal:safe"], "auto_safe", "task", None, "")
        tools_str = next(cmd_claude_safe[i+1] for i, a in enumerate(cmd_claude_safe) if a == "--allowedTools")
        self.assertIn("Edit", tools_str)
        self.assertIn("Write", tools_str)
        self.assertIn("Bash(git status*)", tools_str)

        cmd_agy_safe = server._worker_command("antigravity", "agy", "inherit", "medium", False, ["files:read", "files:write", "terminal:safe"], "auto_safe", "task", None, "")
        self.assertIn("accept-edits", cmd_agy_safe)
        self.assertIn("--settings", cmd_agy_safe)

        # 'auto_approve' mode sets skip_permissions
        cmd_claude_appr = server._worker_command("claude", "claude", "sonnet", "medium", False, ["files:read"], "auto_approve", "task", None, "")
        self.assertIn("--dangerously-skip-permissions", cmd_claude_appr)

        cmd_open_appr = server._worker_command("opencode", "opencode", "default", "medium", False, ["files:read"], "auto_approve", "task", None, "")
        self.assertEqual(cmd_open_appr[-1], "acp")  # Asking is set through the environment, not a flag.

        # set_permission_mode is NOT exposed as an MCP tool
        self.assertNotIn("set_permission_mode", server.TOOLS)

    def test_relay_and_permission_answering(self):
        # _agy_rules decomposes compound commands
        rules = server._agy_rules("git status && ls -la ; pytest")
        self.assertEqual(rules, ["command(git status)", "command(ls)", "command(pytest)"])

        # _agy_resume updates arguments and permissions
        base_cmd = ["agy", "--print", "initial", "--output-format", "stream-json", "--print-timeout", "0"]
        resumed = server._agy_resume(base_cmd, "conv_123", "continue task", ["command(pytest)"])
        self.assertIn("continue task", resumed)
        self.assertIn("--conversation", resumed)
        self.assertIn("conv_123", resumed)
        settings_idx = resumed.index("--settings") + 1
        settings = json.loads(resumed[settings_idx])
        self.assertIn("command(pytest)", settings["permissions"]["allow"])

        # request_permission queues a request
        meta = {
            "id": "worker_c00001",
            "role": "Relay Tester",
            "provider": "antigravity",
            "launched_by": "launcher-station-1",
            "track": "main",
        }
        rid = server.request_permission(meta, "run_command", "npm run build")
        self.assertTrue(rid.startswith("perm_"))
        self.assertTrue(os.path.exists(server._perm_file(rid)))

        # t_answer_permission refuses a non-launcher window
        server._self_id = lambda: "intruder-station-2"
        res_denied = server.t_answer_permission(rid, "allow")
        self.assertIn("[REFUSED]", res_denied)

        # Launcher window can answer
        server._self_id = lambda: "launcher-station-1"
        res_allowed = server.t_answer_permission(rid, "allow")
        self.assertIn("[ALLOW]", res_allowed)

        # The handle() notice reaches the launcher window on tool calls
        rid2 = server.request_permission(meta, "run_command", "rm -rf build")
        handle_res = server.handle({
            "jsonrpc": "2.0",
            "id": 55,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}}
        })
        content_text = handle_res["result"]["content"][0]["text"]
        self.assertIn("[PERMISSION NEEDED]", content_text)
        self.assertIn(rid2, content_text)

    def test_worker_ping_appears_once_only(self):
        launcher = "launcher-station-ping-test"
        orig_self_id = server._self_id
        server._self_id = lambda: launcher
        try:
            worker_id = "worker_fa1234"
            meta = {
                "id": worker_id,
                "role": "Ping Tester",
                "provider": "claude",
                "status": "completed",
                "reason": "finished cleanly",
                "launched_by": launcher,
            }
            server._save_worker(meta)

            res1 = server.handle({
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}}
            })
            text1 = res1["result"]["content"][0]["text"]
            expected = f"[WORKER] {worker_id} (Ping Tester) completed: finished cleanly"
            self.assertIn(expected, text1)

            res2 = server.handle({
                "jsonrpc": "2.0",
                "id": 102,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}}
            })
            text2 = res2["result"]["content"][0]["text"]
            self.assertNotIn(expected, text2)
        finally:
            server._self_id = orig_self_id

    def test_elicitation_auto_closing_and_chat_reply(self):
        launcher = "launcher-station-elicit"
        server._self_id = lambda: launcher
        server._asked.clear()
        server._closed.clear()

        sent_messages = []
        orig_send = server.send
        server.send = lambda obj: sent_messages.append(obj)
        try:
            meta = {
                "id": "worker_elicit1",
                "role": "Elicit Test Worker",
                "provider": "antigravity",
                "launched_by": launcher,
                "track": "main",
            }
            rid = server.request_permission(meta, "run_command", "npm run build")
            self.assertTrue(rid.startswith("perm_"))

            # First step: creates the question in the launcher's chat
            server._elicitation_step()
            self.assertIn(rid, server._asked)
            self.assertEqual(len(sent_messages), 1)
            create_msg = sent_messages[-1]
            self.assertEqual(create_msg["method"], "elicitation/create")
            self.assertEqual(create_msg["id"], f"elicit-{rid}")
            self.assertIn("npm run build", create_msg["params"]["message"])

            # Calling step again while still pending does not duplicate the question
            server._elicitation_step()
            self.assertEqual(len(sent_messages), 1)

            # Permission answered elsewhere (e.g. from the Live HUD)
            server._answer_permission(rid, "allow", "hud")

            # Second step: detects answered elsewhere and sends cancellation
            server._elicitation_step()
            self.assertIn(rid, server._closed)
            self.assertEqual(len(sent_messages), 2)
            cancel_msg = sent_messages[-1]
            self.assertEqual(cancel_msg["method"], "notifications/cancelled")
            self.assertEqual(cancel_msg["params"]["requestId"], f"elicit-{rid}")
            self.assertEqual(cancel_msg["params"]["reason"], "Answered elsewhere")

            # Test direct chat response answering via _handle_elicitation_reply
            rid2 = server.request_permission(meta, "run_command", "git push origin main")
            server._elicitation_step()
            self.assertEqual(len(sent_messages), 3)

            # User answers accept with "allow" in chat
            server._handle_elicitation_reply({
                "jsonrpc": "2.0",
                "id": f"elicit-{rid2}",
                "result": {"action": "accept", "content": {"decision": "allow"}}
            })
            self.assertIn(rid2, server._closed)
            perm_data = server._load_json(server._perm_file(rid2))
            self.assertEqual(perm_data["status"], "answered")
            self.assertEqual(perm_data["decision"], "allow")
            self.assertEqual(perm_data["answered_by"], "chat")

            # Subsequent elicitation step does not emit cancel because it was answered in chat
            server._elicitation_step()
            self.assertEqual(len(sent_messages), 3)
        finally:
            server.send = orig_send
            server._asked.clear()
            server._closed.clear()


class TestInstaller(SubwayTestCase):
    def test_installer_preserves_malformed_claude_json_and_mode(self):
        c_path = os.path.expanduser("~/.claude.json")
        os.makedirs(os.path.dirname(c_path), exist_ok=True)

        # 1. Malformed JSON left byte-identical
        malformed = b'{\n  "mcpServers": { broken json unclosed'
        with open(c_path, "wb") as f:
            f.write(malformed)

        ok, reason = install.install_claude("server.py", sys.executable)
        self.assertFalse(ok)
        with open(c_path, "rb") as f:
            self.assertEqual(f.read(), malformed)

        # 2. Mode 0600 preserved and backup made
        valid = {"mcpServers": {}}
        with open(c_path, "w", encoding="utf-8") as f:
            json.dump(valid, f)
        os.chmod(c_path, 0o600)

        ok2, _ = install.install_claude("server.py", sys.executable)
        self.assertTrue(ok2)
        mode = stat.S_IMODE(os.stat(c_path).st_mode)
        self.assertEqual(mode, 0o600)

        bak_path = c_path + ".subway-bak"
        self.assertTrue(os.path.exists(bak_path))

        # 3. Hook not duplicated
        install.install_claude("server.py", sys.executable)
        s_path = os.path.expanduser("~/.claude/settings.json")
        with open(s_path, "r", encoding="utf-8") as f:
            s_data = json.load(f)
        pre_tool = s_data.get("hooks", {}).get("PreToolUse", [])
        hook_count = sum(1 for e in pre_tool for h in e.get("hooks", []) if "hook-guard" in h.get("command", ""))
        self.assertEqual(hook_count, 1)  # One guard on every tool: it covers leases and budgets.

        # 4. Remove cleans up
        rem_ok = install.remove_target("claude")
        self.assertTrue(rem_ok)

        with open(c_path, "r", encoding="utf-8") as f:
            d_after = json.load(f)
        self.assertNotIn("subway-context", d_after.get("mcpServers", {}))

        with open(s_path, "r", encoding="utf-8") as f:
            s_after = json.load(f)
        hooks_after = sum(1 for e in s_after.get("hooks", {}).get("PreToolUse", []) for h in e.get("hooks", []) if "hook-guard" in h.get("command", ""))
        self.assertEqual(hooks_after, 0)


class TestLocalAndRouting(SubwayTestCase):
    GB = server.GB

    def data(self, total=8, available=3, loaded=None, models=None):
        total *= self.GB
        return {"running": True, "memory": {"total": total, "available": available * self.GB, "swap_used": 0},
                "profile": server.local_profile(total), "loaded": loaded or [],
                "models": models if models is not None else [
                    {"name": "big:latest", "needs": 9 * self.GB, "measured": True, "capabilities": ["completion", "tools"]},
                    {"name": "small:latest", "needs": int(0.4 * self.GB), "measured": False, "capabilities": ["completion", "tools"]},
                    {"name": "embed:latest", "needs": int(0.1 * self.GB), "measured": False, "capabilities": ["embedding"]},
                    {"name": "taste-critic:latest", "needs": int(0.2 * self.GB), "measured": True, "capabilities": ["completion", "tools"]},
                ]}

    def test_profile_scales_with_machine(self):
        small = server.local_profile(8 * self.GB)
        self.assertEqual((small["num_ctx"], small["parallel"], small["keep_alive"]), (4096, 1, "0"))
        self.assertGreater(server.local_profile(32 * self.GB)["num_ctx"], small["num_ctx"])

    def test_memory_gate_refuses_what_wont_fit(self):
        d = self.data()
        self.assertFalse(server.local_fit("big", d)[0])
        self.assertTrue(server.local_fit("small", d)[0])
        self.assertFalse(server.local_fit("missing", d)[0])

    def test_loaded_model_memory_counts_as_available(self):
        d = self.data(available=1, loaded=[{"name": "small:latest", "bytes": 3 * self.GB}])
        self.assertTrue(server.local_fit("small", d)[0])
        self.assertGreater(server.local_room(d), 0)

    def test_chooser_skips_embeddings_and_custom_models(self):
        self.assertEqual(server.choose_local_model(True, self.data()), "small:latest")

    def test_stopped_ollama_never_fits(self):
        self.assertFalse(server.local_fit("small", {"running": False})[0])

    def test_routing_follows_the_task_not_the_role(self):
        code_job = "Refactor the database layer and authentication flow"
        write_job = "Write the documentation and release notes"
        arch_res = server.route_task(code_job, role="Principal Architect")
        writer_res = server.route_task(write_job, role="Technical Writer")
        self.assertEqual(arch_res["skill"], "coding")
        self.assertEqual(writer_res["skill"], "writing")
        self.assertNotEqual(arch_res["skill"], writer_res["skill"])
        self.assertNotEqual(arch_res["order"], writer_res["order"])

    def test_spin_swarm_spreads_providers(self):
        res = server.t_spin_swarm(role="Reviewer", prompt="Review the codebase", provider="auto",
                                  subtasks=["subtask 1", "subtask 2"])
        self.assertIn("[SWARM]", res)
        workers = [w for w in server._all_workers() if "Reviewer" in w.get("role", "")]
        self.assertGreaterEqual(len(workers), 2)
        # Check reasons
        for w in workers[-2:]:
            self.assertTrue(bool(w.get("route")))
            self.assertIn("Assigned to", w.get("route"))

    def test_senior_engineer_refactor_routing(self):
        providers = [
            {"id": "claude", "client": {"dispatchable": True}, "pools": [{"name": "default", "windows": [{"left_pct": 50.0}]}]},
            {"id": "antigravity", "client": {"dispatchable": True}, "pools": [
                {"name": "gemini", "windows": [{"left_pct": 50.0}]},
                {"name": "argon", "windows": [{"left_pct": 50.0}]},
            ]},
        ]
        res = server.route_task("Refactor the database layer and authentication flow", role="Senior Engineer", providers=providers)
        self.assertEqual(res["skill"], "coding")
        self.assertGreaterEqual(len(res["picks"]), 1)
        first = res["picks"][0]
        first_model = next(m for m in server.DEFAULT_MODELS if m["provider"] == first["provider"] and m["model"] == first["model"])
        self.assertGreaterEqual(first_model["coding"], 4)
        self.assertEqual(first["model"], "opus")
        pick_models = [p["model"] for p in res["picks"]]
        self.assertIn("opus", pick_models)
        self.assertNotIn("gemini-3.8-flash-high", pick_models)

    def test_low_task_gets_cheapest_adequate_model(self):
        providers = [
            {"id": "claude", "client": {"dispatchable": True}, "pools": [{"name": "default", "windows": [{"left_pct": 50.0}]}]},
            {"id": "antigravity", "client": {"dispatchable": True}, "pools": [
                {"name": "gemini", "windows": [{"left_pct": 50.0}]},
                {"name": "argon", "windows": [{"left_pct": 50.0}]},
            ]},
        ]
        res = server.route_task("Fix a typo in the readme comment", role="Developer", providers=providers)
        self.assertEqual(res["tier"], "low")
        self.assertGreaterEqual(len(res["picks"]), 1)
        first = res["picks"][0]
        self.assertNotEqual(first["model"], "opus")
        model_info = next(m for m in server.DEFAULT_MODELS if m["provider"] == first["provider"] and m["model"] == first["model"])
        self.assertEqual(model_info["coding"], 2)

    def test_exhausted_pool_is_skipped(self):
        providers = [
            {"id": "claude", "client": {"dispatchable": True}, "pools": [{"name": "default", "windows": [{"left_pct": 0.0}]}]},
            {"id": "antigravity", "client": {"dispatchable": True}, "pools": [
                {"name": "gemini", "windows": [{"left_pct": 50.0}]},
                {"name": "argon", "windows": [{"left_pct": 50.0}]},
            ]},
        ]
        res = server.route_task("Refactor the parser", role="Senior Engineer", providers=providers)
        self.assertNotIn("claude", res["order"])
        self.assertFalse(any(p["provider"] == "claude" for p in res["picks"]))

    def test_scarce_argon_ranking(self):
        providers = [
            {"id": "claude", "client": {"dispatchable": True}, "pools": [{"name": "default", "windows": [{"left_pct": 50.0}]}]},
            {"id": "antigravity", "client": {"dispatchable": True}, "pools": [
                {"name": "gemini", "windows": [{"left_pct": 50.0}]},
                {"name": "argon", "windows": [{"left_pct": 50.0}]},
            ]},
        ]
        res = server.route_task("Refactor the auth architecture", role="Senior Engineer", providers=providers)
        picks = res["picks"]
        opus_idx = next(i for i, p in enumerate(picks) if p["model"] == "opus")
        argon_idx = next(i for i, p in enumerate(picks) if p["model"] == "gemini-4-argon-high")
        self.assertLess(opus_idx, argon_idx)

        providers_no_opus = [
            {"id": "antigravity", "client": {"dispatchable": True}, "pools": [
                {"name": "gemini", "windows": [{"left_pct": 50.0}]},
                {"name": "argon", "windows": [{"left_pct": 50.0}]},
            ]},
        ]
        res_only = server.route_task("Refactor the auth architecture", role="Senior Engineer", providers=providers_no_opus)
        self.assertEqual(res_only["picks"][0]["model"], "gemini-4-argon-high")

    def test_custom_models_json_overrides_defaults(self):
        models_file = os.path.join(server.SUBWAY_DIR, "models.json")
        custom_data = {
            "models": [
                {"provider": "custom_agent", "model": "model-xyz", "coding": 5, "reasoning": 5, "writing": 5}
            ]
        }
        with open(models_file, "w", encoding="utf-8") as f:
            json.dump(custom_data, f)
        try:
            catalog = server.model_catalog()
            mine = [m for m in catalog if m["provider"] == "custom_agent"]
            self.assertEqual(mine[0]["source"], "owner")
            self.assertTrue(any(m["provider"] == "opencode" for m in catalog) and any(m["provider"] == "local" for m in catalog))
        finally:
            with contextlib.suppress(OSError):
                os.remove(models_file)

    def test_opencode_models_discovery_in_catalog(self):
        orig_fn = server._opencode_models
        try:
            server._opencode_models = lambda: ["opencode/flint-40b-free", "opencode/nemotron-3.5-super-vl-free"]
            catalog = server.model_catalog()
            oc_models = [m for m in catalog if m.get("provider") == "opencode"]
            self.assertGreaterEqual(len(oc_models), 3)
            self.assertTrue(any(m.get("model") == "opencode/flint-40b-free" and m.get("free") for m in oc_models))
        finally:
            server._opencode_models = orig_fn

    def test_capacity_scales_with_memory_and_is_shared_by_chats(self):
        orig_mem, orig_cpu, orig_all = server.memory_snapshot, os.cpu_count, server._all_workers
        try:
            server.memory_snapshot = lambda: {"total": 512 * server.GB, "available": 400 * server.GB}
            os.cpu_count = lambda: 32
            self.assertEqual(server.capacity()["machine"], 64)
            server.memory_snapshot = lambda: {"total": 8 * server.GB, "available": 1 * server.GB}
            self.assertEqual(server.capacity()["machine"], 2)
            server.memory_snapshot = lambda: {"total": 16 * server.GB, "available": 8 * server.GB}
            os.cpu_count = lambda: 4
            server._all_workers = lambda include_archive=False: [{"status": "running", "launched_by": f"c{i}"} for i in range(8)]
            self.assertIn("machine's limit is 8", server._capacity_block({"launched_by": "other"}))
        finally:
            server.memory_snapshot, os.cpu_count, server._all_workers = orig_mem, orig_cpu, orig_all

    def test_fake_agy_stream_max_tokens_budget_stopped(self):
        worker_id = "worker_beef01"
        log_file = server._log_file(worker_id)
        script = (
            "import sys, json, time\n"
            "for _ in range(2):\n"
            "    sys.stdout.write(json.dumps({'event': 'step_update', 'step_update': {'usage': {'input_tokens': 30, 'output_tokens': 30}}}) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        meta = {
            "id": worker_id,
            "role": "Coder",
            "provider": "antigravity",
            "cmd": [sys.executable, "-c", script],
            "budget": {"max_tokens": 50},
            "project_dir": self.test_dir,
            "log_file": log_file,
            "status": "running",
            "pid": 99999,
            "timeout_seconds": 10,
        }
        server._save_worker(meta)
        code = server.supervise_worker(worker_id)
        self.assertEqual(code, 0)
        updated = server._load_worker(worker_id)
        updated = server._update_worker_status(updated)
        self.assertEqual(updated["status"], "budget_stopped")
        self.assertIn("token cap", updated.get("report", ""))

    def test_opencode_429_sets_limit_hit(self):
        db_path = os.path.join(self.opencode_dir, "opencode.db")
        con = sqlite3.connect(db_path)
        try:
            con.execute("CREATE TABLE session_v2 (id TEXT, model TEXT, time_updated INTEGER)")
            con.execute("CREATE TABLE session_message (type TEXT, time_created INTEGER, data TEXT)")
            now_ms = int(time.time() * 1000)
            err_data = json.dumps({
                "finish": "error",
                "error": json.dumps({"status": 429, "message": "Rate limit exceeded"}),
                "tokens": {"input": 10, "output": 20, "reasoning": 0, "cache": {"read": 5}},
                "cost": 0.01,
            })
            con.execute("INSERT INTO session_message (type, time_created, data) VALUES (?, ?, ?)",
                        ("assistant", now_ms, err_data))
            con.commit()
        finally:
            con.close()

        usage = server._fetch_opencode_usage()
        self.assertIsNotNone(usage.get("limit_hit"))
        self.assertTrue(usage["limit_hit"]["recent"])
        self.assertIn("Rate limit exceeded", usage["limit_hit"]["message"])

    def test_private_work_never_reaches_the_cloud(self):
        self.assertEqual(server.route_task("Summarize this confidential contract")["order"], ["local"])
        self.assertEqual(server.route_task("Fix this", private=True)["order"], ["local"])
        res = server.t_spin_worker(role="R", prompt="Fix this", provider="claude", private=True)
        self.assertIn("private", res)

    def test_bulk_work_goes_local_first_and_hard_work_does_not(self):
        self.assertEqual(server.route_task("Extraction job over 29,028 documents")["order"][0], "local")
        self.assertNotIn("local", server.route_task("Refactor the auth flow across services")["order"])

    def test_chunks_cover_all_text(self):
        self.assertEqual("".join(server._chunks("abcdefghij", 3)), "abcdefghij")

    def _create_fake_opencode2(self):
        script_path = os.path.join(self.test_dir, "fake_opencode2.py")
        code = (
            "import sys, json, time\n"
            "def send(msg):\n"
            "    sys.stdout.write(json.dumps(msg) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "prompt_id = None\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n"
            "        continue\n"
            "    try:\n"
            "        req = json.loads(line)\n"
            "    except Exception:\n"
            "        continue\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        send({'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': 1}})\n"
            "    elif method == 'session/new':\n"
            "        send({'jsonrpc': '2.0', 'id': req_id, 'result': {'sessionId': 'fake_ses_01', 'configOptions': []}})\n"
            "    elif method == 'session/set_config_option':\n"
            "        send({'jsonrpc': '2.0', 'id': req_id, 'result': {}})\n"
            "    elif method == 'session/prompt':\n"
            "        prompt_id = req_id\n"
            "        prompt_text = ''\n"
            "        for p in (req.get('params') or {}).get('prompt') or []:\n"
            "            if isinstance(p, dict) and p.get('type') == 'text':\n"
            "                prompt_text += p.get('text', '')\n"
            "        if 'test_allow' in prompt_text:\n"
            "            send({'jsonrpc': '2.0', 'id': 201, 'method': 'session/request_permission',\n"
            "                  'params': {'sessionId': 'fake_ses_01', 'toolCall': {'title': 'Bash', 'rawInput': 'echo 123'},\n"
            "                             'options': [{'kind': 'allow_once', 'optionId': 'opt_allow'}, {'kind': 'reject_once', 'optionId': 'opt_deny'}]}})\n"
            "        elif 'test_deny' in prompt_text:\n"
            "            send({'jsonrpc': '2.0', 'id': 202, 'method': 'session/request_permission',\n"
            "                  'params': {'sessionId': 'fake_ses_01', 'toolCall': {'title': 'rm', 'rawInput': 'rm file'},\n"
            "                             'options': [{'kind': 'allow_once', 'optionId': 'opt_allow'}, {'kind': 'reject_once', 'optionId': 'opt_deny'}]}})\n"
            "        elif 'test_cap' in prompt_text:\n"
            "            while True:\n"
            "                time.sleep(0.5)\n"
            "    elif req.get('result') is not None:\n"
            "        res = req.get('result') or {}\n"
            "        outcome = res.get('outcome') or {}\n"
            "        chosen = outcome.get('optionId')\n"
            "        if req_id == 201:\n"
            "            send({'jsonrpc': '2.0', 'method': 'session/update',\n"
            "                  'params': {'sessionId': 'fake_ses_01', 'update': {'sessionUpdate': 'tool_call', 'toolCall': {'title': 'Bash', 'rawInput': 'echo 123'}}}})\n"
            "            send({'jsonrpc': '2.0', 'id': prompt_id, 'result': {'response': 'Report: allowed and finished'}})\n"
            "        elif req_id == 202:\n"
            "            send({'jsonrpc': '2.0', 'id': prompt_id, 'result': {'response': f'Report: answer was {chosen}'}})\n"
            "    elif method == 'session/cancel':\n"
            "        sys.exit(0)\n"
        )
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
        return script_path

    def test_opencode_acp_allow_runs_and_reports(self):
        script_path = self._create_fake_opencode2()
        worker_id = "worker_a0c001"
        meta = {
            "id": worker_id,
            "role": "ACP Worker",
            "provider": "opencode",
            "cmd": [sys.executable, script_path, "acp"],
            "project_dir": self.test_dir,
            "log_file": server._log_file(worker_id),
            "status": "running",
            "pid": os.getpid(),
            "prompt": "test_allow",
            "timeout_seconds": 10,
        }
        server._save_worker(meta)
        def answer_thread():
            deadline = time.time() + 5
            while time.time() < deadline:
                perms = server._pending_permissions(worker_id)
                if perms:
                    server._answer_permission(perms[0]["id"], "allow", "tester")
                    return
                time.sleep(0.05)
        threading.Thread(target=answer_thread, daemon=True).start()
        code = server.supervise_worker(worker_id)
        self.assertEqual(code, 0)
        updated = server._load_worker(worker_id)
        self.assertEqual(updated["status"], "completed")
        self.assertIn("Report: allowed and finished", updated.get("report", ""))

    def test_copilot_acp_permission_uses_subway_relay(self):
        script_path = self._create_fake_opencode2()
        worker_id = "worker_c0a001"
        meta = {
            "id": worker_id,
            "role": "ACP Worker",
            "provider": "copilot",
            "cmd": [sys.executable, script_path, "--acp"],
            "project_dir": self.test_dir,
            "log_file": server._log_file(worker_id),
            "status": "running",
            "pid": os.getpid(),
            "prompt": "test_allow",
            "model": "auto",
            "effort": "auto",
            "permission_mode": "ask",
            "timeout_seconds": 10,
        }
        server._save_worker(meta)
        original_request = server.request_permission
        captured = {}

        def answer_via_subway(*args, **kwargs):
            rid = original_request(*args, **kwargs)
            captured["rid"] = rid
            server._answer_permission(rid, "allow", "tester")
            return rid

        with mock.patch.object(server, "request_permission", side_effect=answer_via_subway) as request:
            code = server.supervise_worker(worker_id)

        self.assertEqual(code, 0)
        request.assert_called_once()
        permission = server._load_json(server._perm_file(captured["rid"]))
        self.assertEqual(permission["provider"], "copilot")
        self.assertEqual(permission["decision"], "allow")
        updated = server._load_worker(worker_id)
        self.assertEqual(updated["status"], "completed")
        self.assertIn("Report: allowed and finished", updated.get("report", ""))

    def test_opencode_acp_deny_is_sent_as_reject_once(self):
        script_path = self._create_fake_opencode2()
        worker_id = "worker_a0c002"
        meta = {
            "id": worker_id,
            "role": "ACP Worker",
            "provider": "opencode",
            "cmd": [sys.executable, script_path, "acp"],
            "project_dir": self.test_dir,
            "log_file": server._log_file(worker_id),
            "status": "running",
            "pid": os.getpid(),
            "prompt": "test_deny",
            "timeout_seconds": 10,
        }
        server._save_worker(meta)
        def answer_thread():
            deadline = time.time() + 5
            while time.time() < deadline:
                perms = server._pending_permissions(worker_id)
                if perms:
                    server._answer_permission(perms[0]["id"], "deny", "tester")
                    return
                time.sleep(0.05)
        threading.Thread(target=answer_thread, daemon=True).start()
        code = server.supervise_worker(worker_id)
        self.assertEqual(code, 0)
        updated = server._load_worker(worker_id)
        self.assertEqual(updated["status"], "completed")
        self.assertIn("opt_deny", updated.get("report", ""))

    def test_opencode_acp_cap_stops_it(self):
        script_path = self._create_fake_opencode2()
        db_path = os.path.join(self.opencode_dir, "opencode.db")
        con = sqlite3.connect(db_path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS session_v2 (id TEXT, model TEXT, time_updated INTEGER, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER)")
            con.execute("INSERT INTO session_v2 (id, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read) VALUES (?, ?, ?, ?, ?)",
                        ("fake_ses_01", 300, 300, 50, 0))
            con.commit()
        finally:
            con.close()

        worker_id = "worker_a0c003"
        meta = {
            "id": worker_id,
            "role": "ACP Worker",
            "provider": "opencode",
            "cmd": [sys.executable, script_path, "acp"],
            "project_dir": self.test_dir,
            "log_file": server._log_file(worker_id),
            "budget": {"max_tokens": 500},
            "status": "running",
            "pid": os.getpid(),
            "prompt": "test_cap",
            "timeout_seconds": 10,
        }
        server._save_worker(meta)
        code = server.supervise_worker(worker_id)
        self.assertEqual(code, 0)
        updated = server._load_worker(worker_id)
        self.assertEqual(updated["status"], "budget_stopped")
        self.assertIn("token cap", updated.get("report", ""))


class TestIsolatedWorkerOutcome(unittest.TestCase):
    def test_read_only_worker_with_no_changes_completes_with_reply(self):
        result = {"status": "completed", "exit_code": 0}
        meta = {"role": "Researcher", "prompt": "Read-only research. Do not edit any file.",
                "capabilities": ["files:read"]}
        server._settle_worker_result(result, meta, True, "Found three call sites.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reason"], "Found three call sites.")

    def test_edit_worker_with_empty_patch_does_not_complete(self):
        result = {"status": "completed", "exit_code": 0}
        meta = {"role": "Builder", "prompt": "Edit the parser and add a test.",
                "mode": "standard", "capabilities": ["files:read", "files:write"]}
        server._settle_worker_result(result, meta, True, "I made no changes because the parser already supports this.")
        self.assertEqual(result["status"], "failed")
        self.assertIn("no changes made", result["reason"].lower())
        self.assertIn("parser already supports this", result["reason"])
        self.assertEqual(server._determine_outcome(meta, result), "no_change")

    def test_reviewer_with_no_changes_completes(self):
        result = {"status": "completed", "exit_code": 0}
        meta = {"role": "Code Reviewer", "prompt": "Review the parser change.",
                "mode": "standard", "capabilities": ["files:read", "files:write"]}
        server._settle_worker_result(result, meta, True, "No issues found.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reason"], "No issues found.")

    def test_edit_worker_with_patch_completes(self):
        result = {"status": "completed", "exit_code": 0}
        meta = {"role": "Builder", "prompt": "Add parser support.",
                "mode": "standard", "capabilities": ["files:read", "files:write"]}
        server._settle_worker_result(result, meta, False, "Added parser support.")
        self.assertEqual(result["status"], "completed")

    def test_in_place_fingerprint_tracks_untracked_file_edits(self):
        with tempfile.TemporaryDirectory() as repo:
            path = os.path.join(repo, "new.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("before\n")

            def fake_git(args, **kwargs):
                output = b"?? new.py\0" if args[1] == "status" else b""
                return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

            with mock.patch.object(server.subprocess, "run", side_effect=fake_git):
                start = server._git_change_fingerprint(repo)
                self.assertEqual(start, server._git_change_fingerprint(repo))
                with open(path, "w", encoding="utf-8") as f:
                    f.write("after\n")
                self.assertNotEqual(start, server._git_change_fingerprint(repo))

    def test_no_change_failure_is_reported_in_worker_ping(self):
        meta = {"id": "worker_abcdef", "role": "Builder", "status": "failed",
                "report": "No changes made. Parser already supports this.", "launched_by": "launcher"}
        with mock.patch.object(server, "_all_workers", return_value=[meta]), \
             mock.patch.object(server, "_load_json", return_value={}), \
             mock.patch.object(server, "_save_json"), \
             mock.patch.object(server, "_state_lock", return_value=contextlib.nullcontext()):
            lines = server.pending_worker_pings("launcher")
        self.assertIn("[WORKER] worker_abcdef (Builder) failed: No changes made. Parser already supports this.", lines)


if __name__ == "__main__":
    unittest.main()


class TestHistoryApi(SubwayTestCase):
    def test_history_paging(self):
        os.makedirs(server.WORKERS_DIR, exist_ok=True)
        for i in range(15):
            wid = f"worker_{i:06d}"
            wfile = server._worker_file(wid)
            with open(wfile, "w", encoding="utf-8") as f:
                json.dump({"id": wid, "status": "completed", "completed_at": f"2026-01-01T00:{i:02d}:00"}, f)

        token = server.hud_token()
        httpd = server.start_hud_server(port=0, daemon=True)
        self.assertIsNotNone(httpd)
        port = httpd.server_address[1]

        def get_hist(offset, limit):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/history?offset={offset}&limit={limit}")
            req.add_header("X-Subway-Token", token)
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))

        res = get_hist(0, 10)
        self.assertEqual(res["total"], 15)
        self.assertEqual(len(res["entries"]), 10)

        res2 = get_hist(10, 10)
        self.assertEqual(res2["total"], 15)
        self.assertEqual(len(res2["entries"]), 5)



class TestPrecompactSegment(unittest.TestCase):
    def test_marker_names_the_segment_being_compacted(self):
        import tempfile, json as _j
        from unittest import mock
        d = tempfile.mkdtemp()
        tp = os.path.join(d, "abc.jsonl")
        with open(tp, "w") as f:
            f.write(_j.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            f.write(_j.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
        sent = []
        with mock.patch.object(server, "t_broadcast_track", side_effect=lambda **k: sent.append(k["message"])):
            server.hook_precompact({"trigger": "manual", "transcript_path": tp, "session_id": "abc"})
        self.assertIn("segment=2", sent[0])
        os.remove(tp)
        with mock.patch.object(server, "t_broadcast_track", side_effect=lambda **k: sent.append(k["message"])):
            server.hook_precompact({"trigger": "auto", "transcript_path": tp, "session_id": "abc"})
        self.assertIn("segment=1", sent[1])


class TestHudCache(unittest.TestCase):
    def setUp(self):
        server._hud_cache.clear()

    def tearDown(self):
        server._hud_cache.clear()

    def test_cached_per_project_and_refreshed_in_background(self):
        from unittest.mock import patch
        calls = []

        def fake(project=""):
            calls.append(project)
            return {"project": project, "fetched_at": time.time()}
        with patch.object(server, "_compile_hud_data", side_effect=fake):
            self.assertEqual(server.get_hud_data("a")["project"], "a")
            self.assertEqual(server.get_hud_data("b")["project"], "b")
            self.assertEqual(server.get_hud_data("a")["project"], "a")
            self.assertEqual(calls, ["a", "b"])
            server.mark_hud_cache_stale()
            self.assertEqual(server.get_hud_data("a")["project"], "a")  # Served at once, refresh started.
            for _ in range(50):
                if calls.count("a") == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(calls.count("a"), 2)

    def test_send_json_ignores_closed_connection(self):
        handler = server.SubwayHUDHandler.__new__(server.SubwayHUDHandler)
        handler.send_response = lambda *a, **k: None
        handler.send_header = lambda *a, **k: None
        handler._security_headers = lambda *a, **k: None
        handler.end_headers = lambda: None

        class Broken:
            def write(self, _):
                raise BrokenPipeError()
        handler.wfile = Broken()
        handler.send_json({"ok": True})  # Must not raise.
