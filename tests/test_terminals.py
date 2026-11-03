import os as _sandbox_os, sys as _sandbox_sys
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401

import os
import shutil
import subprocess
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
        self.home_patcher = mock.patch.dict(os.environ, {"HOME": self.test_dir})
        self.home_patcher.start()
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        self.orig_self_id = server._self_id
        server.SUBWAY_DIR = self.subway_dir
        server.WORKERS_DIR = os.path.join(self.subway_dir, "workers")
        server.TRACKS_DIR = os.path.join(self.subway_dir, "tracks")
        server._self_id = lambda: "chat_test"
        server._ensure_subway_dir()
        # The master switch is off by default; these tests exercise the other run_terminal gates.
        server.set_terminal_sharing(True)

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server._self_id = self.orig_self_id
        self.home_patcher.stop()
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

    def test_errors_read_preserves_redacted_ansi_free_capped_output_tail(self):
        output = (
            "old line\n" * 250
            + "API_KEY=secret_value\n"
            + "\x1b[31mlast failure\x1b[0m\nfinal line"
        )
        with server._state_lock("terminals"):
            terminals.record_command(
                server._terminals_dir(), "t1", "npm test", exit_code=1, output=output,
            )

        errors = server.t_read_terminal("t1", since="0", mode="errors")
        self.assertIn("...[truncated]...\n", errors)
        self.assertIn("last failure\nfinal line", errors)
        self.assertNotIn("secret_value", errors)
        self.assertNotIn("\x1b[", errors)
        self.assertLessEqual(len(errors), 2200)

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

    def test_run_terminal_keeps_no_output_when_sharing_turned_off_mid_run(self):
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=True)
        with server._state_lock("config"):
            cfg = server._load_json("config.json")
            cfg["permission_mode"] = "auto_safe"
            server._save_json("config.json", cfg)

        def fake_extension_then_switch_off():
            deadline = time.time() + 5
            while time.time() < deadline:
                with server._state_lock("terminals"):
                    pending = terminals.list_pending_runs(server._terminals_dir(), terminal_id="t1")
                if pending:
                    server.set_terminal_sharing(False)
                    with server._state_lock("terminals"):
                        terminals.complete_run_request(server._terminals_dir(), pending[0]["id"],
                                                        output="TOPSECRET", exit_code=0)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=fake_extension_then_switch_off)
        t.start()
        res = server.t_run_terminal("t1", "ls -la", timeout_seconds=5)
        t.join(timeout=5)
        self.assertIn("turned off", res)
        self.assertNotIn("TOPSECRET", res)
        rec = terminals.get_terminal(server._terminals_dir(), "t1") or {}
        self.assertNotIn("TOPSECRET", str(rec.get("commands", [])))

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


class _HudPostHandler(server.SubwayHUDHandler):
    """Runs do_POST in memory so the HUD routes can be tested without a socket."""

    def __init__(self, path, payload, token=""):
        self.path = path
        self.headers = {"Host": "127.0.0.1"}
        if token:
            self.headers["X-Subway-Token"] = token
        self._payload = payload
        self.status = None
        self.sent = None

    def _read_payload(self):
        return self._payload

    def send_json(self, data, status=200):
        self.status = status
        self.sent = data


class TerminalSharingTests(unittest.TestCase):
    """Covers the terminal sharing master switch and the HUD routes that read it."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="subway_terminals_share_")
        self.subway_dir = os.path.join(self.test_dir, ".subway-context")
        os.makedirs(self.subway_dir, exist_ok=True)
        self.home_patcher = mock.patch.dict(os.environ, {"HOME": self.test_dir})
        self.home_patcher.start()
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        self.orig_self_id = server._self_id
        self.orig_hud_token_cache = dict(server._hud_token_cache)
        server.SUBWAY_DIR = self.subway_dir
        server.WORKERS_DIR = os.path.join(self.subway_dir, "workers")
        server.TRACKS_DIR = os.path.join(self.subway_dir, "tracks")
        server._self_id = lambda: "chat_test"
        server._hud_token_cache.clear()
        server._ensure_subway_dir()
        self.event = {
            "terminal_id": "t1", "command": "ls -la", "exit_code": 0,
            "start_ts": time.time(), "end_ts": time.time() + 1, "output": "", "caller_id": "shell",
            "name": "shell", "window_id": "", "folder": "/proj",
        }

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server._self_id = self.orig_self_id
        server._hud_token_cache.clear()
        server._hud_token_cache.update(self.orig_hud_token_cache)
        self.home_patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_terminal_event_refused_when_sharing_off_and_nothing_stored(self):
        server.set_terminal_sharing(False)
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", name="Term", share_enabled=True)
        handler = _HudPostHandler("/api/terminal_event", self.event, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 403)
        self.assertIn("terminal sharing is off", handler.sent.get("error", ""))
        rec = terminals.get_terminal(server._terminals_dir(), "t1")
        self.assertEqual(rec.get("commands"), [])

    def test_terminal_event_stored_when_sharing_on(self):
        server.set_terminal_sharing(True)
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", name="Term", share_enabled=True)
        handler = _HudPostHandler("/api/terminal_event", self.event, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        rec = terminals.get_terminal(server._terminals_dir(), "t1")
        self.assertEqual(len(rec.get("commands") or []), 1)
        self.assertEqual(rec["commands"][0]["command"], "ls -la")

    def test_terminal_sharing_route_requires_hud_token_and_changes_setting(self):
        server.set_terminal_sharing(False)
        handler = _HudPostHandler("/api/terminal_sharing", {"enabled": True})
        handler.do_POST()
        self.assertEqual(handler.status, 401)
        self.assertIn("token", handler.sent.get("error", ""))
        self.assertFalse(server.terminal_sharing_enabled())

        handler = _HudPostHandler("/api/terminal_sharing", {"enabled": True}, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.sent["result"]["terminal_sharing"])
        self.assertTrue(server.terminal_sharing_enabled())

        handler = _HudPostHandler("/api/terminal_sharing", {"enabled": False}, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        self.assertFalse(server.terminal_sharing_enabled())

    def test_run_terminal_refused_when_sharing_off(self):
        server.set_terminal_sharing(False)
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), "t1", allow_commands=True, shell_integration=True)
        res = server.t_run_terminal("t1", "echo hi")
        self.assertIn("terminal sharing is off", res)


class TerminalHookTests(unittest.TestCase):
    """Exercises hook install, uninstall, status and script in terminals.py."""

    def setUp(self):
        self.temp_home = tempfile.mkdtemp(prefix="subway_hook_home_")
        self.env_patcher = mock.patch.dict(os.environ, {"HOME": self.temp_home, "SHELL": "/bin/bash"})
        self.env_patcher.start()
        self.orig_subway_dir = server.SUBWAY_DIR
        self.orig_workers_dir = server.WORKERS_DIR
        self.orig_tracks_dir = server.TRACKS_DIR
        server.SUBWAY_DIR = os.path.join(self.temp_home, ".subway-context")
        server.WORKERS_DIR = os.path.join(server.SUBWAY_DIR, "workers")
        server.TRACKS_DIR = os.path.join(server.SUBWAY_DIR, "tracks")

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        self.env_patcher.stop()
        shutil.rmtree(self.temp_home, ignore_errors=True)

    def test_install_fresh_rc(self):
        # Bash on fresh home
        msg = terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890", token="secret_hud_token")
        self.assertIn("Installed the bash hook", msg)
        self.assertIn("http://127.0.0.1:7890", msg)
        self.assertNotIn("secret_hud_token", msg)
        rc_path = os.path.join(self.temp_home, ".bashrc")
        backup_path = rc_path + ".subway-bak"
        self.assertTrue(os.path.exists(rc_path))
        self.assertTrue(os.path.exists(backup_path))
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "")
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn(terminals.HOOK_BEGIN_MARKER, content)
            self.assertIn(terminals.HOOK_END_MARKER, content)
            self.assertIn("SUBWAY_TERMINAL_ID", content)

        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        self.assertTrue(os.path.exists(script_path))

        # Zsh on fresh home
        msg_zsh = terminals.install_hook(shell="zsh", server_url="http://127.0.0.1:7890")
        self.assertIn("Installed the zsh hook", msg_zsh)
        zsh_rc = os.path.join(self.temp_home, ".zshrc")
        self.assertTrue(os.path.exists(zsh_rc))
        with open(zsh_rc, "r", encoding="utf-8") as f:
            zsh_content = f.read()
            self.assertIn("preexec_functions+=(_subway_preexec)", zsh_content)

    def test_hook_optin_payload_has_no_allow_commands(self):
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        with open(script_path, "r", encoding="utf-8") as f:
            script = f.read()
        self.assertIn('"optin"', script)
        self.assertNotIn("allow_commands", script)

    def test_installed_hook_text_checks_sharing(self):
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        with open(script_path, "r", encoding="utf-8") as f:
            script = f.read()
        self.assertIn("sharing", script)
        self.assertIn('data.get("sharing")', script)

    def test_install_existing_content_preserved_and_backed_up(self):
        rc_path = os.path.join(self.temp_home, ".bashrc")
        initial = "export MY_VAR=123\nalias g=git\n"
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write(initial)
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        backup_path = rc_path + ".subway-bak"
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), initial)
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertTrue(content.startswith("export MY_VAR=123\nalias g=git\n\n" + terminals.HOOK_BEGIN_MARKER))

    def test_install_twice_no_duplicates_and_backup_not_overwritten(self):
        rc_path = os.path.join(self.temp_home, ".bashrc")
        initial = "# my initial config\n"
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write(initial)
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        backup_path = rc_path + ".subway-bak"
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), initial)

        # Second install
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        with open(backup_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), initial)  # Still original, not modified by first install
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertEqual(content.count(terminals.HOOK_BEGIN_MARKER), 1)
            self.assertEqual(content.count(terminals.HOOK_END_MARKER), 1)

    def test_uninstall_hook(self):
        rc_path = os.path.join(self.temp_home, ".zshrc")
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write("export ZSH_VAR=1\n")
        terminals.install_hook(shell="zsh", server_url="http://127.0.0.1:7890")
        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        self.assertTrue(os.path.exists(script_path))

        res = terminals.uninstall_hook(shell="zsh")
        self.assertIn("Uninstalled the zsh hook", res)
        with open(rc_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertNotIn(terminals.HOOK_BEGIN_MARKER, content)
            self.assertNotIn(terminals.HOOK_END_MARKER, content)
            self.assertIn("export ZSH_VAR=1", content)
        self.assertFalse(os.path.exists(script_path))

    def test_uninstall_not_installed(self):
        # Case 1: rc does not exist
        res = terminals.uninstall_hook(shell="bash")
        self.assertIn("does not exist", res)

        # Case 2: rc exists but no hook
        rc_path = os.path.join(self.temp_home, ".bashrc")
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write("# no hooks here\n")
        res = terminals.uninstall_hook(shell="bash")
        self.assertIn("No subway-context hook installed", res)
        with open(rc_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# no hooks here\n")

    def test_hook_status(self):
        rc_path = os.path.join(self.temp_home, ".bashrc")
        # Fresh home, no rc file
        res = terminals.hook_status(shell="bash")
        self.assertIn("Not installed", res)
        self.assertIn("does not exist", res)

        # rc exists without hook
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write("echo hi\n")
        res = terminals.hook_status(shell="bash")
        self.assertIn("Not installed in", res)

        # After install
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        res = terminals.hook_status(shell="bash")
        self.assertEqual(res, f"Installed in {rc_path}.")

    def test_unsupported_or_undetected_shell(self):
        # Explicit unsupported shell
        for fn in (terminals.install_hook, terminals.uninstall_hook, terminals.hook_status):
            msg = fn(shell="fish")
            self.assertIn("Unsupported or undetected shell 'fish'", msg)

        # Undetected shell from environment
        with mock.patch.dict(os.environ, {"SHELL": "/bin/csh"}):
            for fn in (terminals.install_hook, terminals.uninstall_hook, terminals.hook_status):
                msg = fn(shell="")
                self.assertIn("Unsupported or undetected shell 'csh'", msg)

        # Unset shell
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHELL", None)
            for fn in (terminals.install_hook, terminals.uninstall_hook, terminals.hook_status):
                msg = fn(shell="")
                self.assertIn("Unsupported or undetected shell '?'", msg)

    def test_default_shell_from_env(self):
        with mock.patch.dict(os.environ, {"SHELL": "/usr/local/bin/zsh"}):
            msg = terminals.hook_status(shell="")
            self.assertIn(".zshrc", msg)

        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            msg = terminals.hook_status(shell="")
            self.assertIn(".bashrc", msg)

    def test_unwritable_rc_file_does_not_crash(self):
        rc_path = os.path.join(self.temp_home, ".bashrc")
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write("# read-only test\n")
        try:
            os.chmod(rc_path, 0o400)
            res_install = terminals.install_hook(shell="bash")
            self.assertIsInstance(res_install, str)
            res_uninstall = terminals.uninstall_hook(shell="bash")
            self.assertIsInstance(res_uninstall, str)
            res_status = terminals.hook_status(shell="bash")
            self.assertIsInstance(res_status, str)
        finally:
            os.chmod(rc_path, 0o600)

    def test_no_token_leakage(self):
        secret = "secret_auth_token_9999"
        res = terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890", token=secret)
        self.assertNotIn(secret, res)
        rc_path = os.path.join(self.temp_home, ".bashrc")
        with open(rc_path, "r", encoding="utf-8") as f:
            self.assertNotIn(secret, f.read())
        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        with open(script_path, "r", encoding="utf-8") as f:
            self.assertNotIn(secret, f.read())

    def test_bash_sourcing_roundtrip(self):
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        rc_path = os.path.join(self.temp_home, ".bashrc")
        env = dict(os.environ, HOME=self.temp_home)
        res = subprocess.run(
            ["bash", "-c", f"source {rc_path}; true; echo done"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res.returncode, 0, f"stderr: {res.stderr}")
        self.assertIn("done", res.stdout)

    def test_bash_sourcing_with_preexisting_trap(self):
        rc_path = os.path.join(self.temp_home, ".bashrc")
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write("trap 'echo existing_trap' DEBUG\n")
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        env = dict(os.environ, HOME=self.temp_home)
        res = subprocess.run(
            ["bash", "-c", f"source {rc_path}; true; echo done"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res.returncode, 0, f"stderr: {res.stderr}")
        self.assertIn("done", res.stdout)

    def test_zsh_sourcing_roundtrip(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh not installed on test machine")
        terminals.install_hook(shell="zsh", server_url="http://127.0.0.1:7890")
        rc_path = os.path.join(self.temp_home, ".zshrc")
        env = dict(os.environ, HOME=self.temp_home)
        res = subprocess.run(
            ["zsh", "-c", f"source {rc_path}; true; echo done"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res.returncode, 0, f"stderr: {res.stderr}")
        self.assertIn("done", res.stdout)

    def test_terminal_hook_script_roundtrip(self):
        terminals.install_hook(shell="bash", server_url="http://127.0.0.1:7890")
        script_path = os.path.join(self.temp_home, ".subway-context", "terminal_hook.py")
        env = dict(os.environ, HOME=self.temp_home)
        # Test optin
        res1 = subprocess.run(
            ["python3", script_path, "optin", "tid-1", self.temp_home],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res1.returncode, 0)

        # Test event
        res2 = subprocess.run(
            ["python3", script_path, "event", "tid-1", "ls -la", "0", "100.0", "101.5", self.temp_home],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res2.returncode, 0)


if __name__ == "__main__":
    unittest.main()

