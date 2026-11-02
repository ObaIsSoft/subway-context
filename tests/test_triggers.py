#!/usr/bin/env python3
"""Worker trigger tests: follow-up steps, verdicts, chains, and on_worker_done."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SUBWAY_NO_NOTIFY", "1")

import server


class TestWorkerTriggers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subway_triggers_")
        self.subway = os.path.join(self.tmp, ".subway-context")
        self.stack = contextlib.ExitStack()
        patch = self.stack.enter_context
        self.launch_worker_impl = server._launch_worker
        patch(mock.patch.object(server, "HOME", self.tmp))
        patch(mock.patch.object(server, "SUBWAY_DIR", self.subway))
        patch(mock.patch.object(server, "WORKERS_DIR", os.path.join(self.subway, "workers")))
        patch(mock.patch.object(server, "TRACKS_DIR", os.path.join(self.subway, "tracks")))
        patch(mock.patch.object(server, "_worker_command", return_value="echo test"))
        patch(mock.patch.object(server, "find_claude_binary", return_value="/bin/echo"))
        patch(mock.patch.object(server, "find_opencode_binary", return_value="/bin/echo"))
        patch(mock.patch.object(server, "find_agy_binary", return_value="/bin/echo"))
        patch(mock.patch.object(server, "check_budget_preflight", return_value=(True, "ok")))
        patch(mock.patch.object(server, "t_broadcast_track"))
        patch(mock.patch.object(server, "notify_if_still_waiting"))
        patch(mock.patch.object(server, "_self_id", return_value="tester"))
        patch(mock.patch.object(server, "_launch_worker", return_value="[APPROVED & LAUNCHED] test"))
        patch(mock.patch.object(server, "route_task", return_value={
            "picks": [{"provider": "claude", "model": "sonnet"}], "reason": "test", "private": False}))
        server._ensure_subway_dir()
        os.makedirs(server.WORKERS_DIR, exist_ok=True)

    def tearDown(self):
        self.stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spin(self, **kw):
        kw.setdefault("provider", "claude")
        kw.setdefault("project_dir", self.tmp)
        res = server.t_spin_worker(**kw)
        self.assertIn("APPROVAL REQUIRED", res)
        return re.search(r"worker_[0-9a-f]{6}", res).group(0), res

    def children(self, parent):
        return [w for w in server._all_workers() if w.get("triggered_by") == parent]

    def git_repo(self, name="review_repo"):
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
        with open(os.path.join(repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("original\n")
        subprocess.run(["git", "-C", repo, "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
        return repo

    def capture_review(self, worker_id):
        meta = server._load_worker(worker_id)
        server._capture_in_place_review_diff(meta)
        server._save_worker(meta)
        server._run_triggers(worker_id, {"status": "completed"})
        return self.children(worker_id)[0]["prompt"]

    def test_completed_fires_and_failed_does_not(self):
        a, _ = self.spin(role="A", prompt="build", then=json.dumps([
            {"role": "B", "prompt": "next", "provider": "claude"}]))
        server._run_triggers(a, {"status": "completed", "reason": "done"})
        self.assertEqual(len(self.children(a)), 1)
        self.assertEqual(self.children(a)[0]["role"], "B")

        b, _ = self.spin(role="C", prompt="build", then=json.dumps([
            {"role": "D", "prompt": "next", "provider": "claude"}]))
        server._run_triggers(b, {"status": "failed", "reason": "bad"})
        self.assertEqual(len(self.children(b)), 0)

    def test_approved_and_rejected_follow_verdict(self):
        a, _ = self.spin(role="A", prompt="build", then=json.dumps([
            {"role": "OK", "prompt": "go", "provider": "claude", "when": "approved"},
            {"role": "NO", "prompt": "go", "provider": "claude", "when": "rejected"}]))
        meta = server._load_worker(a)
        meta["verdict"] = True
        server._save_worker(meta)
        server._run_triggers(a, {"status": "completed", "response": "APPROVE: looks good"})
        self.assertEqual([w["role"] for w in self.children(a)], ["OK"])
        self.assertEqual(server._load_worker(a)["verdict"], "approve")

        b, _ = self.spin(role="A2", prompt="build", then=json.dumps([
            {"role": "OK", "prompt": "go", "provider": "claude", "when": "approved"},
            {"role": "NO", "prompt": "go", "provider": "claude", "when": "rejected"}]))
        meta = server._load_worker(b)
        meta["verdict"] = True
        server._save_worker(meta)
        server._run_triggers(b, {"status": "completed", "response": "REJECT: breaks tests"})
        self.assertEqual([w["role"] for w in self.children(b)], ["NO"])
        self.assertEqual(server._load_worker(b)["verdict"], "reject")

    def test_review_auto_adds_reviewer_on_another_provider(self):
        a, _ = self.spin(role="Builder", prompt="build", review="auto")
        step = server._load_worker(a)["then"][0]
        self.assertEqual(step["role"], "Reviewer")
        self.assertTrue(step["verdict"])
        self.assertTrue(step["other_provider"])
        self.assertEqual(step["provider"], "auto")
        with mock.patch.object(server, "route_task", return_value={"picks": [
                {"provider": "claude", "model": "sonnet"},
                {"provider": "opencode", "model": "free"}], "reason": "test", "private": False}):
            server._run_triggers(a, {"status": "completed"})
        self.assertEqual(self.children(a)[0]["provider"], "opencode")

    def test_chain_passes_then_down(self):
        a, _ = self.spin(role="A", prompt="build", then=json.dumps([{
            "role": "B", "prompt": "middle", "provider": "claude",
            "then": [{"role": "C", "prompt": "last", "provider": "claude"}]}]))
        server._run_triggers(a, {"status": "completed"})
        b = self.children(a)[0]
        self.assertEqual(b["then"][0]["role"], "C")
        server._run_triggers(b["id"], {"status": "completed"})
        self.assertEqual(self.children(b["id"])[0]["role"], "C")

    def test_on_worker_done_on_finished_worker_fires_at_once(self):
        a, _ = self.spin(role="A", prompt="build")
        meta = server._load_worker(a)
        meta["status"] = "completed"
        meta["report"] = "done"
        server._save_worker(meta)
        res = server.t_on_worker_done(a, json.dumps([
            {"role": "B", "prompt": "next", "provider": "claude"}]))
        self.assertIn("FIRED", res)
        self.assertEqual(len(self.children(a)), 1)

    def test_bad_json_and_unknown_fields_refused(self):
        res = server.t_spin_worker(role="A", prompt="p", provider="claude", project_dir=self.tmp,
                                   then="{bad")
        self.assertTrue(res.startswith("Error:"), res)
        res = server.t_spin_worker(role="A", prompt="p", provider="claude", project_dir=self.tmp,
                                   then=json.dumps([{"role": "B", "prompt": "b", "bogus": 1}]))
        self.assertTrue(res.startswith("Error:"), res)
        self.assertIn("unknown", res.lower())

    def test_patch_is_in_child_prompt(self):
        a, _ = self.spin(role="A", prompt="build", then=json.dumps([
            {"role": "B", "prompt": "use the patch", "provider": "claude", "include": "patch"}]))
        with open(os.path.join(server.WORKERS_DIR, f"{a}.patch"), "w", encoding="utf-8") as f:
            f.write("diff --git a/x b/x\n+hello\n")
        server._run_triggers(a, {"status": "completed"})
        child = self.children(a)[0]
        self.assertIn("diff --git", child["prompt"])
        self.assertIn("PREVIOUS WORKER", child["prompt"])

        b, _ = self.spin(role="A2", prompt="build", then=json.dumps([
            {"role": "B", "prompt": "use the report", "provider": "claude", "include": "patch"}]))
        server._run_triggers(b, {"status": "completed", "reason": "report only"})
        self.assertIn("report only", self.children(b)[0]["prompt"])

    def test_in_place_launch_records_dirty_paths(self):
        repo = self.git_repo()
        with open(os.path.join(repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("already modified\n")
        worker_id, _ = self.spin(role="Builder", prompt="Change a file",
                                 project_dir=repo, review="auto")

        real_popen = subprocess.Popen

        def popen_worker_only(args, *popen_args, **popen_kwargs):
            if args[0] == "git":
                return real_popen(args, *popen_args, **popen_kwargs)
            return mock.Mock(pid=123, wait=mock.Mock())

        with mock.patch.object(server, "_launch_worker", self.launch_worker_impl), \
                mock.patch.object(server, "_capacity_block", return_value=None), \
                mock.patch("subprocess.Popen", side_effect=popen_worker_only):
            server._launch_worker(worker_id)

        meta = server._load_worker(worker_id)
        self.assertIn("tracked.txt", meta["git_dirty_before"])
        self.assertLessEqual(len(meta["git_dirty_before"]), 500)

    def test_in_place_launch_dirty_path_list_is_capped(self):
        root_result = subprocess.CompletedProcess([], 0, stdout=self.tmp, stderr="")
        status = b"\0".join(f"?? path-{index}".encode() for index in range(501)) + b"\0"
        status_result = subprocess.CompletedProcess([], 0, stdout=status, stderr=b"")
        with mock.patch.object(server.subprocess, "run", side_effect=[root_result, status_result]):
            paths = server._git_dirty_paths(self.tmp)
        self.assertEqual(len(paths), 500)

    def test_in_place_review_separates_preexisting_and_worker_changes(self):
        repo = self.git_repo()
        with open(os.path.join(repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("already modified content\n")
        worker_id, _ = self.spin(role="Builder", prompt="Add a file",
                                 project_dir=repo, review="auto")
        meta = server._load_worker(worker_id)
        meta["git_dirty_before"] = ["tracked.txt"]
        server._save_worker(meta)
        with open(os.path.join(repo, "new.txt"), "w", encoding="utf-8") as f:
            f.write("worker-added content\n")

        prompt = self.capture_review(worker_id)

        self.assertIn("Changed by this worker", prompt)
        self.assertIn("Untracked file new.txt", prompt)
        self.assertIn("worker-added content", prompt)
        self.assertIn("Already modified before this worker started (may include this worker's edits)", prompt)
        self.assertIn("The second group may contain other people's work.", prompt)
        self.assertIn("already modified content", prompt)
        self.assertIn("?? new.txt", prompt)
        stored = server._load_worker(worker_id)
        self.assertNotIn("patch_path", stored)
        self.assertTrue(stored["review_diff_path"].endswith(f"{worker_id}.review_diff"))
        self.assertFalse(os.path.exists(os.path.join(server.WORKERS_DIR, f"{worker_id}.patch")))

    def test_in_place_review_explains_non_git_project(self):
        worker_id, _ = self.spin(role="Builder", prompt="Make a change", review="auto")

        prompt = self.capture_review(worker_id)

        self.assertIn("No diff is available", prompt)
        self.assertIn("not a git repository", prompt)

    def test_in_place_review_trims_oversized_diff_with_note(self):
        repo = self.git_repo("large_review_repo")
        with open(os.path.join(repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("large change " * 7000)
        worker_id, _ = self.spin(role="Builder", prompt="Make a large change",
                                 project_dir=repo, review="auto")

        prompt = self.capture_review(worker_id)
        review_diff_path = server._load_worker(worker_id)["review_diff_path"]
        with open(review_diff_path, "r", encoding="utf-8") as f:
            diff_text = f.read()

        self.assertLessEqual(len(diff_text), 60000)
        self.assertIn("[Review diff trimmed to 60000 characters.]", diff_text)
        self.assertIn("[Review diff trimmed to 60000 characters.]", prompt)


if __name__ == "__main__":
    unittest.main()
