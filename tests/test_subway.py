#!/usr/bin/env python3
"""Comprehensive test suite for Subway Context."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

# Ensure parent directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class TestSubwayContext(unittest.TestCase):
    def setUp(self):
        # Create a sandboxed temporary directory for Subway Context storage
        self.test_dir = tempfile.mkdtemp(prefix="subway_test_")
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_locks_file = server.LOCKS_FILE
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR

        # Point server constants to temp dir
        server.SUBWAY_DIR = self.test_dir
        server.LOCKS_FILE = os.path.join(self.test_dir, "locks.json")
        server.WORKERS_DIR = os.path.join(self.test_dir, "workers")
        server.TRACKS_DIR = os.path.join(self.test_dir, "tracks")
        server._ensure_subway_dir()

    def tearDown(self):
        # Restore server constants and clean up temp dir
        server.SUBWAY_DIR = self.orig_subway_dir
        server.LOCKS_FILE = self.orig_locks_file
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_claim_file_and_collision_shield(self):
        """Test acquiring a lease and preventing collisions."""
        res1 = server.t_claim_file("src/router.ts", holder="claude-main", duration_seconds=100, purpose="Routing refactor")
        self.assertIn("[LEASE ACQUIRED]", res1)
        self.assertIn("src/router.ts", res1)

        # Conflict check
        res2 = server.t_claim_file("src/router.ts", holder="gemini-main", duration_seconds=100, purpose="Clobbering router")
        self.assertIn("[COLLISION WARNING]", res2)
        self.assertIn("already leased to claude-main", res2)

        # Release check
        res3 = server.t_release_file("src/router.ts", holder="claude-main")
        self.assertIn("[RELEASED]", res3)

        # Confirm freed
        locks = server.load_locks().get("locks", {})
        self.assertNotIn("src/router.ts", locks)

    def test_02_regression_shield_and_signoff(self):
        """Test sign-off workflow and regression shield against ungrounded edits."""
        # Claude claims and implements
        server.t_claim_file("src/auth.ts", holder="claude-main", duration_seconds=100, purpose="JWT Auth", baseline_commit="commit_abc1")
        
        # Reviewer approves
        sign_res = server.t_signoff_file("src/auth.ts", verdict="approved", notes="Passes all auth test specs", reviewer="senior-reviewer")
        self.assertIn("[SIGNOFF: APPROVED]", sign_res)

        # Release lease to make it available
        server.t_release_file("src/auth.ts", holder="claude-main")

        # Gemini attempts ungrounded edit without matching baseline
        gemini_attempt = server.t_claim_file("src/auth.ts", holder="gemini-main", baseline_commit="outdated_xyz", purpose="Rewriting auth")
        self.assertIn("[REGRESSION SHIELD]", gemini_attempt)
        self.assertIn("previously verified and APPROVED", gemini_attempt)

        # Gemini claims with coordinator role override
        coord_override = server.t_claim_file("src/auth.ts", holder="gemini-main", role="coordinator", purpose="Authorized redesign")
        self.assertIn("[LEASE ACQUIRED]", coord_override)

    def test_03_shared_tracks_and_destructive_auditing(self):
        """Test decision ledger publishing, subscriptions, and destructive command auditing."""
        join_res = server.t_join_track("test-track", station_id="agent-alpha")
        self.assertIn("[TRACK JOINED]", join_res)

        # Normal decision
        b1 = server.t_broadcast_track("Refactored database pool to connection manager", track="test-track", files=["src/db.ts"], decisions=["Max 20 connections"], station_id="agent-alpha")
        self.assertIn("[BROADCAST]", b1)

        # Destructive command detection
        b2 = server.t_broadcast_track("Running sudo rm -rf /tmp/cache to clear cache", track="test-track", station_id="agent-alpha")
        self.assertIn("[ALERT: DESTRUCTIVE ACTION]", b2)

        # Verify alerts log exists
        alerts_file = os.path.join(server.TRACKS_DIR, "alerts.jsonl")
        self.assertTrue(os.path.exists(alerts_file))

        # Read track
        track_feed = server.t_read_track("test-track", limit=10)
        self.assertIn("Refactored database pool", track_feed)
        self.assertIn("[ALERT: DESTRUCTIVE ACTION]", track_feed)

    def test_04_worker_spawner_and_god_mode(self):
        """Test worker spawning, approval requirement, effort levels, and God Mode activation."""
        # Standard mode requiring user approval by default
        pending_worker = server.t_spin_worker(role="Linter", prompt="Run eslint on src/", provider="antigravity", effort="low")
        self.assertIn("[APPROVAL REQUIRED]", pending_worker)
        self.assertIn("Linter", pending_worker)

        # Confirm and approve worker
        worker_files = [f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")]
        latest_file = os.path.join(server.WORKERS_DIR, sorted(worker_files)[-1])
        with open(latest_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        approve_res = server.t_approve_worker(meta["id"])
        self.assertIn("[APPROVED]", approve_res)

        # God Mode with explicit user confirmation
        god_worker = server.t_spin_worker(role="Architect", prompt="Complete system redesign", provider="antigravity", mode="god", effort="max", user_confirmed=True)
        self.assertIn("[WARNING: GOD MODE ACTIVE]", god_worker)
        self.assertIn("Architect", god_worker)

        # Check worker status
        status_out = server.t_worker_status()
        self.assertIn("Linter", status_out)
        self.assertIn("Architect", status_out)

    def test_05_hud_data_compilation(self):
        """Test that get_hud_data compiles complete system snapshot."""
        data = server.get_hud_data()
        self.assertIn("version", data)
        self.assertIn("time_iso", data)
        self.assertIn("locks", data)
        self.assertIn("workers", data)
        self.assertIn("tracks", data)

    def test_06_binary_discovery_methods(self):
        """Test binary discovery functions return valid types."""
        claude_bin = server.find_claude_binary()
        opencode_bin = server.find_opencode_binary()
        # Should be None or valid executable path
        if claude_bin:
            self.assertTrue(os.path.exists(claude_bin) or os.path.lexists(claude_bin))
        if opencode_bin:
            self.assertTrue(os.path.exists(opencode_bin) or os.path.lexists(opencode_bin))

    def test_07_complexity_estimation_and_auto_assign(self):
        """Test intelligent complexity estimation across low, medium, high, and god tiers."""
        # 1. Low complexity task
        c_low = server.estimate_complexity("Fix typo and lint docstring in router", role="Doc Linter")
        self.assertEqual(c_low["effort"], "low")
        self.assertEqual(c_low["tier"], "low")

        # 2. Medium complexity task
        c_med = server.estimate_complexity("Add unit test suite for auth endpoint", role="Test Engineer")
        self.assertEqual(c_med["effort"], "medium")
        self.assertEqual(c_med["tier"], "medium")

        # 3. High complexity task
        c_high = server.estimate_complexity("Refactor database pooling to solve concurrency race condition", role="Backend Lead")
        self.assertEqual(c_high["effort"], "high")
        self.assertEqual(c_high["tier"], "high")

        # 4. God mode task
        c_god = server.estimate_complexity("Autonomous overhaul and full system redesign of whole architecture", role="Principal Architect")
        self.assertEqual(c_god["effort"], "max")
        self.assertEqual(c_god["mode"], "god")
        self.assertEqual(c_god["tier"], "frontier")

        # 5. Verify auto-assignment in t_spin_worker with confirmation
        spawn_res = server.t_spin_worker(role="Doc Linter", prompt="Fix typo in README", provider="antigravity", effort="auto", user_confirmed=True)
        self.assertIn("[SPAWNED]", spawn_res)
        # Verify stored worker JSON got effort='low'
        worker_files = [f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")]
        self.assertTrue(len(worker_files) > 0)
        latest_file = os.path.join(server.WORKERS_DIR, sorted(worker_files)[-1])
        with open(latest_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["effort"], "low")

    def test_08_dynamic_complexity_escalation(self):
        """Test dynamic mid-task escalation from low tier to God Mode with context handoff."""
        # Spawn initial worker
        server.t_spin_worker(role="Bug Hunter", prompt="Investigate memory flake", provider="antigravity", effort="low", user_confirmed=True)
        worker_files = [f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")]
        latest_file = os.path.join(server.WORKERS_DIR, sorted(worker_files)[-1])
        with open(latest_file, "r", encoding="utf-8") as f:
            w_meta = json.load(f)
        worker_id = w_meta["id"]

        # Escalate to God Mode
        esc_res = server.t_escalate_worker(
            worker_id=worker_id,
            new_effort="max",
            new_mode="god",
            reason="Detected critical memory corruption across 4 modules",
            successor_provider="antigravity"
        )
        self.assertIn("[ESCALATE_COMPLEXITY]", esc_res)

        # Verify original worker was marked escalated
        with open(latest_file, "r", encoding="utf-8") as f:
            updated_meta = json.load(f)
        self.assertTrue(updated_meta.get("escalated"))
        self.assertEqual(updated_meta.get("effort"), "max")
        self.assertEqual(updated_meta.get("mode"), "god")
        self.assertIn("Detected critical memory corruption", updated_meta["escalated_from"]["reason"])

        # Verify track feed recorded the escalation
        track_feed = server.t_read_track("main", limit=10)
        self.assertIn("[ESCALATE_COMPLEXITY]", track_feed)

    def test_09_thread_tab_process_isolation(self):
        """Test isolated session/thread execution and log separation."""
        # Spin 2 separate workers with user confirmation
        w1_out = server.t_spin_worker(role="Worker A", prompt="Do task A", provider="antigravity", track="track-a", user_confirmed=True)
        w2_out = server.t_spin_worker(role="Worker B", prompt="Do task B", provider="antigravity", track="track-b", user_confirmed=True)

        self.assertIn("[SPAWNED]", w1_out)
        self.assertIn("[SPAWNED]", w2_out)

        # Ensure workers have distinct IDs, separate tracks, and isolated log files
        worker_files = sorted([f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")])
        self.assertGreaterEqual(len(worker_files), 2)

        with open(os.path.join(server.WORKERS_DIR, worker_files[-2]), "r", encoding="utf-8") as f:
            meta_a = json.load(f)
        with open(os.path.join(server.WORKERS_DIR, worker_files[-1]), "r", encoding="utf-8") as f:
            meta_b = json.load(f)

        self.assertNotEqual(meta_a["id"], meta_b["id"])
        self.assertNotEqual(meta_a["log_file"], meta_b["log_file"])
        self.assertNotEqual(meta_a["track"], meta_b["track"])

    def test_10_terminal_approval_and_rejection_workflow(self):
        """Test queueing, terminal rejection, and terminal approval of workers."""
        # 1. Queue a worker needing approval
        q_res = server.t_spin_worker(role="Security Auditor", prompt="Scan auth logic", provider="antigravity", effort="high")
        self.assertIn("[APPROVAL REQUIRED]", q_res)

        # Find worker id
        worker_files = sorted([f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")])
        with open(os.path.join(server.WORKERS_DIR, worker_files[-1]), "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["status"], "pending_approval")

        # 2. Reject it via terminal command
        rej_res = server.t_reject_worker(meta["id"], reason="Not needed right now")
        self.assertIn("[REJECTED]", rej_res)

        with open(os.path.join(server.WORKERS_DIR, worker_files[-1]), "r", encoding="utf-8") as f:
            meta_rej = json.load(f)
        self.assertEqual(meta_rej["status"], "rejected")
        self.assertEqual(meta_rej["rejection_reason"], "Not needed right now")

    def test_11_model_and_effort_flag_mapping(self):
        """Test that explicit model and effort flags are properly configured in command arguments."""
        # Test Claude command generation for God mode
        c_god = server.t_spin_worker(role="Lead Architect", prompt="Design event system", provider="claude", mode="god", effort="max")
        worker_files = sorted([f for f in os.listdir(server.WORKERS_DIR) if f.startswith("worker_") and f.endswith(".json")])
        with open(os.path.join(server.WORKERS_DIR, worker_files[-1]), "r", encoding="utf-8") as f:
            claude_meta = json.load(f)

        self.assertEqual(claude_meta["model"], "opus")
        self.assertEqual(claude_meta["effort"], "max")
        # Ensure command contains --model opus and --effort max
        cmd = claude_meta.get("cmd", [])
        self.assertIn("--model", cmd)
        self.assertIn("opus", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("max", cmd)


if __name__ == "__main__":
    unittest.main()

