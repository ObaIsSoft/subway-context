"""Worker prompt rules and large whole-file read guard tests."""
import os as _sandbox_os, sys as _sandbox_sys  # Isolate HOME, SUBWAY_DIR and agent folders before server loads.
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401
import contextlib
import json
import re
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SUBWAY_NO_NOTIFY", "1")

import server


class TestWorkerRulesAndReadGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subway_worker_rules_")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.subway = os.path.join(self.tmp, "subway")
        self.stack = contextlib.ExitStack()
        env = {
            "SUBWAY_DIR": self.subway,
            "SUBWAY_CLAUDE_DIR": os.path.join(self.tmp, "claude"),
            "SUBWAY_CLAUDE_JSON": os.path.join(self.tmp, "claude.json"),
            "SUBWAY_AGY_DIR": os.path.join(self.tmp, "agy"),
            "SUBWAY_OPENCODE_DIR": os.path.join(self.tmp, "opencode"),
            "SUBWAY_OPENCODE_AGENTS_DIR": os.path.join(self.tmp, "opencode-agents"),
        }
        self.stack.enter_context(mock.patch.dict(os.environ, env))
        paths = {
            "HOME": self.tmp,
            "SUBWAY_DIR": self.subway,
            "WORKERS_DIR": os.path.join(self.subway, "workers"),
            "TRACKS_DIR": os.path.join(self.subway, "tracks"),
            "LOCKS_FILE": os.path.join(self.subway, "locks.json"),
            "QUOTAS_FILE": os.path.join(self.subway, "quotas.json"),
            "CLAUDE_DIR": env["SUBWAY_CLAUDE_DIR"],
            "CLAUDE_JSON": env["SUBWAY_CLAUDE_JSON"],
            "AGY_DIR": env["SUBWAY_AGY_DIR"],
            "OPENCODE_DIR": env["SUBWAY_OPENCODE_DIR"],
            "OPENCODE_AGENTS_DIR": env["SUBWAY_OPENCODE_AGENTS_DIR"],
            "HUD_DIR": os.path.join(self.tmp, "hud"),
            "HUD_FILE": os.path.join(self.tmp, "hud", "index.html"),
        }
        for name, value in paths.items():
            self.stack.enter_context(mock.patch.object(server, name, value))
        for name, value in {
            "_worker_command": ["worker-cli"],
            "find_claude_binary": "/bin/echo",
            "find_opencode_binary": "/bin/echo",
            "find_agy_binary": "/bin/echo",
            "find_copilot_binary": "/bin/echo",
            "check_budget_preflight": (True, "ok"),
            "route_task": {"picks": [{"provider": "claude", "model": "sonnet"}],
                           "reason": "test", "private": False},
            "_self_id": "test-launcher",
            "_broadcast_track_internal": None,
            "notify_if_still_waiting": None,
        }.items():
            self.stack.enter_context(mock.patch.object(server, name, return_value=value))
        server._ensure_subway_dir()

    def tearDown(self):
        self.stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _large_file(self, name="large.txt", count=1501):
        path = os.path.join(self.project, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("line\n" * count)
        return path

    def _worker_meta(self, provider="antigravity", mode="auto_approve"):
        return {
            "id": "worker_abcdef",
            "role": "Reader",
            "provider": provider,
            "permission_mode": mode,
            "project_dir": self.project,
            "launched_by": "test-launcher",
        }

    def _permission_record(self, rid):
        with open(server._perm_file(rid), "r", encoding="utf-8") as f:
            return json.load(f)

    def _parent(self, status="running", permission_mode="auto_safe", mode="standard"):
        parent = {"id": "worker_aaaaaa", "role": "Lead", "provider": "claude", "status": status,
                  "permission_mode": permission_mode, "mode": mode, "launched_by": "test-launcher"}
        server._save_worker(parent)
        return parent

    def _spin_as(self, parent_id, **kw):
        launches = []
        with mock.patch.dict(os.environ, {"SUBWAY_WORKER_ID": parent_id} if parent_id else {}), \
                mock.patch.object(server, "_launch_worker", side_effect=lambda w: launches.append(w) or f"launched {w}"):
            result = server.t_spin_worker(role="Sub", prompt="Read-only check", provider="claude",
                                          project_dir=self.project, **kw)
        wid = re.search(r"worker_[0-9a-f]{6}", result).group(0)
        return server._load_worker(wid), launches, result

    def test_sub_worker_inherits_parent_mode_and_approval(self):
        self._parent(permission_mode="auto_safe")
        child, launches, result = self._spin_as("worker_aaaaaa")
        self.assertEqual(child["permission_mode"], "auto_safe")
        self.assertEqual(child["parent_id"], "worker_aaaaaa")
        self.assertEqual(child["approved_by"], "lineage:worker_aaaaaa")
        self.assertEqual(launches, [child["id"]])
        self.assertIn("approved with its parent", result)

    def test_sub_worker_never_broader_than_parent(self):
        self._parent(permission_mode="auto_safe")
        child, _, _ = self._spin_as("worker_aaaaaa", permission_mode="auto_approve", mode="god")
        self.assertEqual(child["permission_mode"], "auto_safe")
        self.assertEqual(child["mode"], "standard")
        narrower, _, _ = self._spin_as("worker_aaaaaa", permission_mode="ask")
        self.assertEqual(narrower["permission_mode"], "ask")

    def test_finished_parent_or_chat_caller_still_needs_user_approval(self):
        self._parent(status="completed", permission_mode="auto_approve")
        child, launches, _ = self._spin_as("worker_aaaaaa", permission_mode="ask")
        self.assertEqual(launches, [])
        self.assertEqual(child["status"], "pending_approval")
        self.assertNotIn("parent_id", child)
        from_chat, launches, _ = self._spin_as("", permission_mode="ask")
        self.assertEqual(launches, [])
        self.assertEqual(from_chat["status"], "pending_approval")

    def test_edit_prompt_gets_standing_rules_once_and_review_gets_only_reading_rule(self):
        source = os.path.join(self.project, "sample.py")
        with open(source, "w", encoding="utf-8") as f:
            f.write("def target():\n    return 1\n")
        result = server.t_spin_worker(
            role="Editor", prompt="Fix the target", provider="claude", project_dir=self.project, code=["target"])
        worker_id = result.split("`")[1]
        prompt = server._load_worker(worker_id)["prompt"]
        self.assertIn("=== CODE (all you need: do not search) ===", prompt)
        self.assertEqual(prompt.count("SUBWAY STANDING RULES"), 1)
        self.assertEqual(prompt.count(server._WORKER_READING_RULE), 1)

        review = server.t_spin_worker(
            role="Reviewer", prompt=server._REVIEW_PROMPT, provider="claude", project_dir=self.project)
        review_id = review.split("`")[1]
        review_prompt = server._load_worker(review_id)["prompt"]
        self.assertIn(server._WORKER_READING_RULE, review_prompt)
        self.assertNotIn("SUBWAY STANDING RULES", review_prompt)

    def test_reviewers_known_by_role_or_read_only_opening_get_only_reading_rule(self):
        for role, prompt in (("Review Stall Signals", "Check this diff and reply APPROVE or REJECT."),
                             ("Checker", "Read-only code review. Do not edit any file.")):
            result = server.t_spin_worker(role=role, prompt=prompt, provider="claude", project_dir=self.project)
            saved = server._load_worker(result.split("`")[1])["prompt"]
            self.assertEqual(saved.count(server._WORKER_READING_RULE), 1)
            self.assertNotIn("SUBWAY STANDING RULES", saved)

    def test_worker_rules_can_be_disabled(self):
        server._save_json("config.json", {"worker_rules": False})
        result = server.t_spin_worker(
            role="Editor", prompt="Fix it", provider="claude", project_dir=self.project)
        worker_id = result.split("`")[1]
        self.assertNotIn("SUBWAY STANDING RULES", server._load_worker(worker_id)["prompt"])

    def test_guard_denies_large_whole_file_reads_in_every_permission_mode(self):
        path = self._large_file()
        for mode in ("ask", "auto_safe", "auto_approve"):
            with self.subTest(mode=mode):
                rid = server.request_permission(
                    self._worker_meta(mode=mode), "read_file", path,
                    raw_input={"TargetFile": path})
                record = self._permission_record(rid)
                self.assertEqual(record["status"], "answered")
                self.assertEqual(record["decision"], "deny")
                self.assertIn(f"{path} has 1501 lines.", record["denial_reason"])
                self.assertIn("read a line range.", record["denial_reason"])

    def test_range_small_config_off_and_unreadable_inputs(self):
        path = self._large_file()
        meta = self._worker_meta(provider="opencode")
        denied = server._large_file_read_denial(
            meta, "Read", {"filePath": path}, kind="read", locations=[path])
        self.assertIn(f"{path} has 1501 lines.", denied)

        ranged = server.request_permission(
            meta, "Read", json.dumps({"filePath": path, "offset": 1, "limit": 20}),
            raw_input={"filePath": path, "offset": 1, "limit": 20})
        self.assertEqual(self._permission_record(ranged)["decision"], "allow")
        self.assertIsNone(server._large_file_read_denial(meta, "read_file", {
            "path": path, "start_line": 1, "end_line": 20}))

        small = os.path.join(self.project, "small.txt")
        with open(small, "w", encoding="utf-8") as f:
            f.write("one\ntwo\n")
        allowed = server.request_permission(meta, "read_file", small, raw_input={"path": small})
        self.assertEqual(self._permission_record(allowed)["decision"], "allow")

        server._save_json("config.json", {"large_file_reads": "allow"})
        disabled = server.request_permission(meta, "read_file", path, raw_input={"path": path})
        self.assertEqual(self._permission_record(disabled)["decision"], "allow")

        missing = os.path.join(self.project, "missing.txt")
        self.assertIsNone(server._large_file_read_denial(meta, "read_file", missing))
        external = os.path.join(self.tmp, "outside.txt")
        with open(external, "w", encoding="utf-8") as f:
            f.write("line\n" * 1501)
        self.assertIsNone(server._large_file_read_denial(meta, "read_file", external))
        with mock.patch.object(server, "log"), mock.patch("builtins.open", side_effect=PermissionError("unreadable")):
            self.assertIsNone(server._large_file_read_denial(meta, "read_file", path))


if __name__ == "__main__":
    unittest.main()
