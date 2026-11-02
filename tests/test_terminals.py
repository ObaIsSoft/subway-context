import os as _sandbox_os, sys as _sandbox_sys
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import server
import terminals


class TerminalsStoreTests(unittest.TestCase):
    """Exercises terminals.py directly, with no server involved."""

    def setUp(self):
        self.base_dir = tempfile.mkdtemp(prefix="subway_terminals_")

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_strip_ansi(self):
        self.assertEqual(terminals.strip_ansi("\x1b[31mred\x1b[0m text"), "red text")
        self.assertEqual(terminals.strip_ansi(""), "")
        self.assertIsNone(terminals.strip_ansi(None) or None)

    def test_redact_env_line_and_bearer_and_kv(self):
        text = "API_KEY=sk-abc123\nAuthorization: Bearer xyz.secret.token\npassword: hunter2\nFOO=bar"
        redacted = terminals.redact_text(text)
        self.assertNotIn("sk-abc123", redacted)
        self.assertNotIn("xyz.secret.token", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("FOO=bar", redacted)
        self.assertIn("***REDACTED***", redacted)

    def test_record_and_list_and_read(self):
        terminals.record_command(self.base_dir, "t1", "echo hi", exit_code=0, output="hi\n", name="Term 1",
                                  folder="/proj", caller_id="editor")
        terminals.record_command(self.base_dir, "t1", "false", exit_code=1, output="boom", caller_id="editor")
        rows = terminals.list_terminals(self.base_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["command_count"], 2)
        self.assertEqual(rows[0]["failures"], 1)

        result = terminals.read_terminal(self.base_dir, "t1", caller_id="chat1", since="last", mode="full")
        self.assertEqual(len(result["commands"]), 2)
        # Second read with since='last' for the same caller returns nothing new.
        result2 = terminals.read_terminal(self.base_dir, "t1", caller_id="chat1", since="last")
        self.assertEqual(result2["commands"], [])

        errors = terminals.read_terminal(self.base_dir, "t1", caller_id="chat2", mode="errors")
        self.assertEqual(len(errors["commands"]), 1)
        self.assertEqual(errors["commands"][0]["exit_code"], 1)

    def test_read_missing_terminal(self):
        result = terminals.read_terminal(self.base_dir, "nope")
        self.assertIn("error", result)

    def test_search_terminal(self):
        terminals.record_command(self.base_dir, "t1", "npm install foo", exit_code=0, output="added 1 package")
        terminals.record_command(self.base_dir, "t2", "ls", exit_code=0, output="nothing here")
        hits = terminals.search_terminal(self.base_dir, "foo")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["terminal_id"], "t1")
        self.assertEqual(terminals.search_terminal(self.base_dir, ""), [])

    def test_retention_and_cap(self):
        old_entry_ts = time.time() - terminals.RETENTION_SECONDS - 10
        path = terminals._terminal_path(self.base_dir, "t1")
        os.makedirs(self.base_dir, exist_ok=True)
        terminals._write_json_atomic(path, {
            "id": "t1", "commands": [{"ts": old_entry_ts, "command": "old", "exit_code": 0, "output": ""}],
            "since": {},
        })
        terminals.record_command(self.base_dir, "t1", "new", exit_code=0, output="")
        rec = terminals.get_terminal(self.base_dir, "t1")
        self.assertEqual(len(rec["commands"]), 1)
        self.assertEqual(rec["commands"][0]["command"], "new")

    def test_cap_at_max_commands(self):
        for i in range(terminals.MAX_COMMANDS_PER_TERMINAL + 10):
            terminals.record_command(self.base_dir, "t1", f"cmd{i}", exit_code=0, output="")
        rec = terminals.get_terminal(self.base_dir, "t1")
        self.assertEqual(len(rec["commands"]), terminals.MAX_COMMANDS_PER_TERMINAL)
        self.assertEqual(rec["commands"][-1]["command"], f"cmd{terminals.MAX_COMMANDS_PER_TERMINAL + 9}")

    def test_output_cap(self):
        big = "x" * (terminals.MAX_OUTPUT_BYTES * 2)
        terminals.record_command(self.base_dir, "t1", "big", exit_code=0, output=big)
        rec = terminals.get_terminal(self.base_dir, "t1")
        self.assertLessEqual(len(rec["commands"][0]["output"].encode("utf-8")), terminals.MAX_OUTPUT_BYTES + 64)

    def test_opt_in_flags(self):
        rec = terminals.set_terminal_opt_in(self.base_dir, "t1", name="Term", share_enabled=True)
        self.assertTrue(rec["share_enabled"])
        self.assertNotIn("allow_commands", rec)  # never set yet
        rec2 = terminals.set_terminal_opt_in(self.base_dir, "t1", allow_commands=True)
        self.assertTrue(rec2["share_enabled"])  # preserved
        self.assertTrue(rec2["allow_commands"])

    def test_is_safe_command_matching(self):
        safe = ["ls", "git status"]
        self.assertTrue(terminals.is_safe_command("ls", safe))
        self.assertTrue(terminals.is_safe_command("ls -la", safe))
        self.assertTrue(terminals.is_safe_command("git status --short", safe))
        self.assertFalse(terminals.is_safe_command("lsof", safe))
        self.assertFalse(terminals.is_safe_command("rm -rf /", safe))
        self.assertFalse(terminals.is_safe_command("", safe))

    def test_run_request_lifecycle(self):
        rec = terminals.create_run_request(self.base_dir, "run_aaaaaaaa", "t1", "echo hi", caller_id="chat1")
        self.assertEqual(rec["status"], "pending")
        pending = terminals.list_pending_runs(self.base_dir, terminal_id="t1")
        self.assertEqual(len(pending), 1)
        done = terminals.complete_run_request(self.base_dir, "run_aaaaaaaa", output="hi\n", exit_code=0)
        self.assertEqual(done["status"], "done")
        self.assertEqual(terminals.list_pending_runs(self.base_dir, terminal_id="t1"), [])

    def test_run_request_invalid_id_rejected(self):
        with self.assertRaises(ValueError):
            terminals.create_run_request(self.base_dir, "not-a-run-id", "t1", "echo hi")

    def test_missing_corrupt_and_nonexistent_base_dir(self):
        self.assertEqual(terminals.list_terminals(os.path.join(self.base_dir, "does_not_exist")), [])
        path = terminals._terminal_path(self.base_dir, "corrupt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(terminals.get_terminal(self.base_dir, "corrupt"))
        # record_command on a corrupt file should start fresh rather than raise.
        terminals.record_command(self.base_dir, "corrupt", "echo ok", exit_code=0, output="")
        rec = terminals.get_terminal(self.base_dir, "corrupt")
        self.assertEqual(len(rec["commands"]), 1)

    def test_terminal_id_requires_identity(self):
        with self.assertRaises(ValueError):
            terminals._terminal_path(self.base_dir, "")

    def test_concurrent_writes_stay_consistent(self):
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    terminals.record_command(self.base_dir, "concurrent", f"cmd-{n}-{i}", exit_code=0, output="")
            except Exception as e:  # pragma: no cover - surfaced via errors list
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        rec = terminals.get_terminal(self.base_dir, "concurrent")
        self.assertIsNotNone(rec)
        self.assertLessEqual(len(rec["commands"]), terminals.MAX_COMMANDS_PER_TERMINAL)


class ServerTerminalWrapperTests(unittest.TestCase):
    """A few tests through the server.py MCP-tool wrappers, with SUBWAY_DIR pointed at a temp dir."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="subway_terminals_srv_")
        self.subway_dir = os.path.join(self.test_dir, ".subway-context")
        os.makedirs(self.subway_dir, exist_ok=True)
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        self.orig_self_id = server._self_id
        server.SUBWAY_DIR = self.subway_dir
        server.WORKERS_DIR = os.path.join(self.subway_dir, "workers")
        server.TRACKS_DIR = os.path.join(self.subway_dir, "tracks")
        server._self_id = lambda: "chat_test"
        server._ensure_subway_dir()

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server._self_id = self.orig_self_id
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_list_read_search_empty(self):
        self.assertIn("No terminals", server.t_list_terminals())
        self.assertEqual(server.t_read_terminal("ghost"), "Error: terminal 'ghost' not found")
        self.assertIn("No matches", server.t_search_terminal("x"))

    def test_list_read_search_with_data(self):
        with server._state_lock("terminals"):
            terminals.record_command(server._terminals_dir(), "t1", "npm test", exit_code=1,
                                      output="FAIL something", name="Term 1", folder="/proj")
        listing = server.t_list_terminals()
        self.assertIn("t1", listing)
        self.assertIn("1 failing", listing)
        read = server.t_read_terminal("t1", since="last", mode="full")
        self.assertIn("npm test", read)
        errors = server.t_read_terminal("t1", since="0", mode="errors")
        self.assertIn("npm test", errors)
        hits = server.t_search_terminal("something")
        self.assertIn("t1", hits)

    def test_run_terminal_refuses_without_opt_in(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", shell_integration=True)
        res = server.t_run_terminal("t1", "echo hi")
        self.assertIn("REFUSED", res)
        self.assertIn("opted in", res)

    def test_run_terminal_refuses_without_shell_integration(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=False)
        res = server.t_run_terminal("t1", "echo hi")
        self.assertIn("REFUSED", res)
        self.assertIn("shell integration", res)

    def test_run_terminal_refuses_when_closed(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True,
                                           shell_integration=True, closed=True)
        res = server.t_run_terminal("t1", "echo hi")
        self.assertIn("closed", res)

    def test_run_terminal_missing_terminal(self):
        self.assertIn("not found", server.t_run_terminal("nope", "echo hi"))

    def test_run_terminal_auto_approve_executes_via_run_request(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=True)
        with server._state_lock("config"):
            cfg = server._load_json("config.json")
            cfg["permission_mode"] = "auto_approve"
            server._save_json("config.json", cfg)

        def fake_extension():
            deadline = time.time() + 5
            while time.time() < deadline:
                with server._state_lock("terminals"):
                    pending = terminals.list_pending_runs(server._terminals_dir(), terminal_id="t1")
                if pending:
                    with server._state_lock("terminals"):
                        terminals.complete_run_request(server._terminals_dir(), pending[0]["id"],
                                                        output="hi\n", exit_code=0)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=fake_extension)
        t.start()
        res = server.t_run_terminal("t1", "echo hi", timeout_seconds=5)
        t.join(timeout=5)
        self.assertIn("exit 0", res)
        self.assertIn("hi", res)

    def test_run_terminal_auto_safe_runs_safe_command_without_asking(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=True)
        with server._state_lock("config"):
            cfg = server._load_json("config.json")
            cfg["permission_mode"] = "auto_safe"
            server._save_json("config.json", cfg)

        def fake_extension():
            deadline = time.time() + 5
            while time.time() < deadline:
                with server._state_lock("terminals"):
                    pending = terminals.list_pending_runs(server._terminals_dir(), terminal_id="t1")
                if pending:
                    with server._state_lock("terminals"):
                        terminals.complete_run_request(server._terminals_dir(), pending[0]["id"],
                                                        output="", exit_code=0)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=fake_extension)
        t.start()
        res = server.t_run_terminal("t1", "ls -la", timeout_seconds=5)
        t.join(timeout=5)
        self.assertIn("exit 0", res)

    def test_run_terminal_ask_mode_denied(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=True)
        with server._state_lock("config"):
            cfg = server._load_json("config.json")
            cfg["permission_mode"] = "ask"
            server._save_json("config.json", cfg)

        def deny_it():
            deadline = time.time() + 5
            while time.time() < deadline:
                pending = server._pending_permissions()
                if pending:
                    server._answer_permission(pending[0]["id"], "deny", "test")
                    return
                time.sleep(0.05)

        t = threading.Thread(target=deny_it)
        t.start()
        res = server.t_run_terminal("t1", "rm -rf something", timeout_seconds=5)
        t.join(timeout=5)
        self.assertIn("DENIED", res)


if __name__ == "__main__":
    unittest.main()
