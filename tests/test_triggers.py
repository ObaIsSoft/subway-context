#!/usr/bin/env python3
"""Worker trigger tests: follow-up steps, verdicts, chains, and on_worker_done."""
import contextlib
import json
import os
import re
import shutil
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


if __name__ == "__main__":
    unittest.main()
