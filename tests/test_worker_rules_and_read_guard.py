"""Worker prompt rules and large whole-file read guard tests."""
import os as _sandbox_os, sys as _sandbox_sys  # Isolate HOME, SUBWAY_DIR and agent folders before server loads.
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401
import contextlib
import io
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

    def test_antigravity_read_guard_ranged_read_allowed(self):
        path = self._large_file()
        meta = self._worker_meta(provider="antigravity", mode="auto_approve")
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "read_file",
                "tool_info": {
                    "parameters": {
                        "Path": path,
                        "StartLine": 1,
                        "EndLine": 50,
                    }
                }
            }
        }
        proc = mock.MagicMock()
        proc.stdout = [json.dumps(step_event).encode("utf-8") + b"\n"]
        out = io.BytesIO()
        run = {"meta": meta}
        server._pump_agy(proc, out, run, meta=meta)
        self.assertNotIn("read_guard_denial", run)
        proc.terminate.assert_not_called()
        self.assertNotIn("read_guard_hits", meta)
        logged = out.getvalue().decode("utf-8")
        self.assertIn("lines 1-50", logged)

    def test_antigravity_read_guard_small_file_allowed(self):
        small = os.path.join(self.project, "small.txt")
        with open(small, "w", encoding="utf-8") as f:
            f.write("one\ntwo\n")
        meta = self._worker_meta(provider="antigravity", mode="auto_approve")
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "read_file",
                "tool_info": {"parameters": {"Path": small}}
            }
        }
        proc = mock.MagicMock()
        proc.stdout = [json.dumps(step_event).encode("utf-8") + b"\n"]
        out = io.BytesIO()
        run = {"meta": meta}
        server._pump_agy(proc, out, run, meta=meta)
        self.assertNotIn("read_guard_denial", run)
        proc.terminate.assert_not_called()
        self.assertNotIn("read_guard_hits", meta)

    def test_antigravity_read_guard_file_outside_folder_allowed(self):
        external = os.path.join(self.tmp, "outside.txt")
        with open(external, "w", encoding="utf-8") as f:
            f.write("line\n" * 1501)
        meta = self._worker_meta(provider="antigravity", mode="auto_approve")
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "read_file",
                "tool_info": {"parameters": {"Path": external}}
            }
        }
        proc = mock.MagicMock()
        proc.stdout = [json.dumps(step_event).encode("utf-8") + b"\n"]
        out = io.BytesIO()
        run = {"meta": meta}
        server._pump_agy(proc, out, run, meta=meta)
        self.assertNotIn("read_guard_denial", run)
        proc.terminate.assert_not_called()
        self.assertNotIn("read_guard_hits", meta)

    def test_antigravity_read_guard_config_allow_allows(self):
        path = self._large_file()
        server._save_json("config.json", {"large_file_reads": "allow"})
        meta = self._worker_meta(provider="antigravity", mode="auto_approve")
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "read_file",
                "tool_info": {"parameters": {"Path": path}}
            }
        }
        proc = mock.MagicMock()
        proc.stdout = [json.dumps(step_event).encode("utf-8") + b"\n"]
        out = io.BytesIO()
        run = {"meta": meta}
        server._pump_agy(proc, out, run, meta=meta)
        self.assertNotIn("read_guard_denial", run)
        proc.terminate.assert_not_called()
        self.assertNotIn("read_guard_hits", meta)

    def test_antigravity_read_guard_whole_read_stopped_resumed_and_counter_increments(self):
        path = self._large_file()
        meta = self._worker_meta(provider="antigravity", mode="auto_approve")
        meta["model"] = "gemini-test"
        server._save_worker(meta)
        init_event = {"event": "init", "conversation_id": "conv-test-123"}
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "read_file",
                "tool_info": {"parameters": {"Path": path}}
            }
        }
        proc = mock.MagicMock()
        proc.stdout = [json.dumps(init_event).encode("utf-8") + b"\n",
                       json.dumps(step_event).encode("utf-8") + b"\n"]
        out = io.BytesIO()
        run = {"meta": meta}
        server._pump_agy(proc, out, run, meta=meta)

        self.assertIn("read_guard_denial", run)
        expected_msg = f"{path} has 1501 lines. Grep for what you need, then read a line range."
        self.assertEqual(run["read_guard_denial"], expected_msg)
        proc.terminate.assert_called_once()
        self.assertEqual(meta.get("read_guard_hits"), 1)

        # Worker record on disk
        reloaded = server._load_worker(meta["id"])
        self.assertEqual(reloaded.get("read_guard_hits"), 1)

        # Model stats counter
        stats = server._load_model_stats()
        self.assertTrue(any(s.get("read_guard_hits") == 1 for s in stats))

        # Check conversation resume
        cmd = ["agy", "--print", "initial prompt", "--conversation", "conv-test-123"]
        resumed = server._agy_resume(cmd, run["conversation"], run["read_guard_denial"], [])
        self.assertEqual(resumed[resumed.index("--print") + 1], expected_msg)

    def test_legacy_copilot_worker_record_marked_estimate(self):
        legacy = {
            "id": "worker_legacy",
            "provider": "copilot",
            "usage": {"input": 1500, "output": 50, "cache_read": 1200},
        }
        normalized = server._normalise_copilot_worker_record(legacy)
        self.assertTrue(normalized.get("estimate"))
        self.assertTrue(normalized["usage"].get("estimate"))

        with_cache_write = {
            "id": "worker_write",
            "provider": "copilot",
            "usage": {"input": 1500, "output": 50, "cache_read": 1200, "cache_write": 300},
        }
        normalized_write = server._normalise_copilot_worker_record(with_cache_write)
        self.assertNotIn("estimate", normalized_write)


class TestThreadPermissionOverride(unittest.TestCase):
    """A1: per-thread permission-mode overrides on top of the universal default."""

    setUp = TestWorkerRulesAndReadGuard.setUp
    tearDown = TestWorkerRulesAndReadGuard.tearDown
    _spin_as = TestWorkerRulesAndReadGuard._spin_as

    def test_get_permission_mode_thread_override_wins_over_universal(self):
        server.set_permission_mode("auto_safe")
        self.assertEqual(server.get_permission_mode("chat-1"), "auto_safe")
        server.set_permission_mode("auto_approve", thread_id="chat-1")
        self.assertEqual(server.get_permission_mode("chat-1"), "auto_approve")
        # Other threads and the universal lookup are unaffected.
        self.assertEqual(server.get_permission_mode("chat-2"), "auto_safe")
        self.assertEqual(server.get_permission_mode(), "auto_safe")

    def test_set_permission_mode_thread_clear_resets_to_universal(self):
        server.set_permission_mode("auto_approve")
        server.set_permission_mode("ask", thread_id="chat-1")
        self.assertEqual(server.get_permission_mode("chat-1"), "ask")
        msg = server.set_permission_mode("", thread_id="chat-1")
        self.assertIn("reset to the universal mode", msg)
        self.assertEqual(server.get_permission_mode("chat-1"), "auto_approve")

    def test_set_permission_mode_invalid_mode_for_thread_errors_without_writing(self):
        before = server._load_json("thread_settings.json")
        result = server.set_permission_mode("not-a-mode", thread_id="chat-1")
        self.assertTrue(result.startswith("Error:"))
        self.assertEqual(server._load_json("thread_settings.json"), before)

    def test_get_permission_mode_corrupt_thread_settings_falls_back_to_universal(self):
        server.set_permission_mode("auto_safe")
        path = os.path.join(server.SUBWAY_DIR, "thread_settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertEqual(server.get_permission_mode("chat-1"), "auto_safe")

    def test_get_permission_mode_unknown_or_finished_thread_falls_back(self):
        # A thread with no override, or one whose worker has already finished, just gets the universal mode.
        server.set_permission_mode("ask")
        self.assertEqual(server.get_permission_mode("worker_zzzzzz"), "ask")
        self.assertEqual(server.get_permission_mode(""), "ask")

    def test_spin_worker_direct_launch_uses_launching_chats_override(self):
        server.set_permission_mode("ask")
        server.set_permission_mode("auto_safe", thread_id="test-launcher")
        child, _, _ = self._spin_as("")
        self.assertEqual(child["permission_mode"], "auto_safe")

    def test_spin_worker_explicit_permission_mode_beats_thread_default(self):
        server.set_permission_mode("auto_safe", thread_id="test-launcher")
        child, _, _ = self._spin_as("", permission_mode="ask")
        self.assertEqual(child["permission_mode"], "ask")

    def test_spin_worker_with_no_override_falls_back_to_universal(self):
        server.set_permission_mode("auto_approve")
        child, _, _ = self._spin_as("")
        self.assertEqual(child["permission_mode"], "auto_approve")

    def test_running_worker_keeps_its_mode_after_override_changes(self):
        server.set_permission_mode("auto_safe", thread_id="test-launcher")
        child, _, _ = self._spin_as("")
        self.assertEqual(child["permission_mode"], "auto_safe")
        server.set_permission_mode("ask", thread_id="test-launcher")
        reloaded = server._load_worker(child["id"])
        self.assertEqual(reloaded["permission_mode"], "auto_safe")

    def test_thread_permission_rows_lists_overrides_with_labels(self):
        server._save_worker({"id": "worker_bbbbbb", "role": "Sub Lead", "provider": "claude",
                              "status": "running", "permission_mode": "ask", "launched_by": "test-launcher"})
        server.set_permission_mode("auto_safe")
        server.set_permission_mode("auto_approve", thread_id="worker_bbbbbb")
        rows = server.thread_permission_rows()["threads"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["thread_id"], "worker_bbbbbb")
        self.assertEqual(rows[0]["label"], "Sub Lead")
        self.assertEqual(rows[0]["permission_mode"], "auto_approve")
        self.assertEqual(rows[0]["universal"], "auto_safe")
        self.assertTrue(rows[0]["is_worker"])

    def test_thread_permission_rows_empty_when_no_overrides(self):
        self.assertEqual(server.thread_permission_rows(), {"threads": []})


if __name__ == "__main__":
    unittest.main()
