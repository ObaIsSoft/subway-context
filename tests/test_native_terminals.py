import os as _sandbox_os, sys as _sandbox_sys
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest import mock

import native_terminals
import server
import terminals


class _FakeHttpResponse:
    def __init__(self, data):
        self._data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class NativeTerminalsTests(unittest.TestCase):
    def setUp(self):
        self.temp_home = tempfile.mkdtemp(prefix="subway_native_test_")
        self.subway_dir = os.path.join(self.temp_home, ".subway-context")
        self.terminals_dir = os.path.join(self.subway_dir, "terminals")
        os.makedirs(self.terminals_dir, exist_ok=True)

        self.orig_terminals_dir = server._terminals_dir
        server._terminals_dir = lambda: self.terminals_dir

        self.env_patcher = mock.patch.dict(os.environ, {
            "HOME": self.temp_home,
            "SUBWAY_DIR": self.subway_dir,
            "SHELL": "/bin/zsh",
            "TERM_PROGRAM": "Apple_Terminal",
        })
        self.env_patcher.start()
        server.set_terminal_sharing(True)

    def tearDown(self):
        server._terminals_dir = self.orig_terminals_dir
        self.env_patcher.stop()
        server.set_terminal_sharing(False)
        shutil.rmtree(self.temp_home, ignore_errors=True)

    def test_hook_payload_app_and_tty_detection(self):
        # Execute terminal_hook.py script with subprocess mocked
        base = self.subway_dir
        with open(os.path.join(base, "hud.json"), "w", encoding="utf-8") as f:
            json.dump({"port": 7890}, f)
        with open(os.path.join(base, "hud_token"), "w", encoding="utf-8") as f:
            f.write("test-token")

        calls = []

        def fake_urlopen(req, timeout=1):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.full_url, body))
            if req.full_url.endswith("/api/terminals"):
                return _FakeHttpResponse({"sharing": True, "terminals": []})
            return _FakeHttpResponse({"ok": True})

        mock_ps = mock.Mock(stdout="  ttys023  \n", returncode=0)

        # 1. Apple_Terminal optin
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}), \
             mock.patch("subprocess.run", return_value=mock_ps), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ns = {"__name__": "subway_hook_test"}
            exec(compile(terminals._HOOK_SCRIPT, "terminal_hook.py", "exec"), ns)
            with mock.patch.object(sys, "argv", ["terminal_hook.py", "optin", "shell-12345-random", self.temp_home]):
                ns["main"]()

        optin_payload = calls[-1][1]
        self.assertEqual(optin_payload["app"], "terminal")
        self.assertEqual(optin_payload["tty"], "/dev/ttys023")
        # Ensure hook never sends allow_commands or capture_output
        self.assertNotIn("allow_commands", optin_payload)
        self.assertNotIn("capture_output", optin_payload)

        # 2. iTerm.app event
        mock_ps.stdout = "ttys009\n"
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}), \
             mock.patch("subprocess.run", return_value=mock_ps), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(sys, "argv", ["terminal_hook.py", "event", "shell-99999-rand", "ls", "0", "1.0", "2.0", self.temp_home]):
                ns["main"]()

        event_payload = calls[-1][1]
        self.assertEqual(event_payload["app"], "iterm")
        self.assertEqual(event_payload["tty"], "/dev/ttys009")
        self.assertNotIn("allow_commands", event_payload)
        self.assertNotIn("capture_output", event_payload)

        # 3. Ghostty / other: app is empty
        mock_ps.stdout = "??"
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}), \
             mock.patch("subprocess.run", return_value=mock_ps), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(sys, "argv", ["terminal_hook.py", "optin", "shell-555-rand", self.temp_home]):
                ns["main"]()

        ghostty_payload = calls[-1][1]
        self.assertEqual(ghostty_payload["app"], "")
        self.assertEqual(ghostty_payload["tty"], "")

    def test_slice_output(self):
        # Found
        h1 = "user@mac:~$ echo hello\nhello\nuser@mac:~$ "
        self.assertEqual(native_terminals.slice_output(h1, "echo hello"), "hello")

        # Not found
        self.assertEqual(native_terminals.slice_output(h1, "git status"), "")
        self.assertEqual(native_terminals.slice_output("", "echo hello"), "")
        self.assertEqual(native_terminals.slice_output(h1, ""), "")

        # Last occurrence wins
        h2 = "user$ echo hi\nfirst output\nuser$ echo hi\nsecond output line 1\nsecond output line 2\n\nuser$ "
        self.assertEqual(native_terminals.slice_output(h2, "echo hi"), "second output line 1\nsecond output line 2")

        # Prompt line dropped
        h3 = "user$ true\nuser$ "
        self.assertEqual(native_terminals.slice_output(h3, "true"), "")

        # Strip trailing blank lines
        h4 = "user$ cat test\nline 1\nline 2\n\n   \nuser$ "
        self.assertEqual(native_terminals.slice_output(h4, "cat test"), "line 1\nline 2")

    def test_osascript_arguments_argv_only(self):
        with mock.patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            ok, err = native_terminals.type_command("terminal", "/dev/ttys001", "echo safe; rm -rf /")
            self.assertTrue(ok)
            call_cmd = mock_subproc.call_args[0][0]
            # cmd is ["osascript", "-e", script, "echo safe; rm -rf /", "/dev/ttys001"]
            self.assertEqual(call_cmd[0], "osascript")
            self.assertEqual(call_cmd[1], "-e")
            script = call_cmd[2]
            argv = call_cmd[3:]
            self.assertNotIn("echo safe; rm -rf /", script)
            self.assertNotIn("/dev/ttys001", script)
            self.assertEqual(argv, ["echo safe; rm -rf /", "/dev/ttys001"])

    def test_run_native_terminal_success(self):
        tid = "shell-100-test"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True, capture_output=True,
                app="terminal", tty="/dev/ttys001",
            )
        server.set_terminal_sharing(True)

        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", return_value=False), \
             mock.patch.object(native_terminals, "type_command") as mock_type, \
             mock.patch.object(native_terminals, "read_history") as mock_hist:

            def record_event(*args, **kwargs):
                # Simulate terminal_hook recording the finished command
                terminals.record_command(
                    self.terminals_dir, tid, "echo hi", exit_code=0,
                    start_ts=time.time(), end_ts=time.time() + 0.1,
                )
                return True, ""

            mock_type.side_effect = record_event
            mock_hist.return_value = "$ echo hi\nhi there\n$ "

            res = server.t_run_terminal(tid, "echo hi", timeout_seconds=2)
            self.assertEqual(res, "[exit 0] hi there")

            # Check that command entry recorded output
            rec = terminals.get_terminal(self.terminals_dir, tid)
            self.assertEqual(rec["commands"][-1]["output"], "hi there")

    def test_run_native_terminal_busy_refused(self):
        tid = "shell-101-busy"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True,
                app="terminal", tty="/dev/ttys001",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", return_value=True), \
             mock.patch.object(native_terminals, "type_command") as mock_type:

            res = server.t_run_terminal(tid, "echo hi", timeout_seconds=1)
            self.assertIn("[REFUSED]", res)
            self.assertIn("is busy running something", res)
            mock_type.assert_not_called()

    def test_run_native_terminal_not_found_window(self):
        tid = "shell-102-missing"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True,
                app="terminal", tty="/dev/ttys001",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", return_value=None):

            res = server.t_run_terminal(tid, "echo hi", timeout_seconds=1)
            self.assertIn("[REFUSED]", res)
            self.assertIn("terminal window not found; is it still open?", res)

    def test_run_native_terminal_newline_refused(self):
        tid = "shell-103-newline"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True,
                app="terminal", tty="/dev/ttys001",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"):
            res = server.t_run_terminal(tid, "echo 1\necho 2", timeout_seconds=1)
            self.assertIn("[REFUSED]", res)
            self.assertIn("one line only", res)

            res2 = server.t_run_terminal(tid, "echo 1\recho 2", timeout_seconds=1)
            self.assertIn("[REFUSED]", res2)
            self.assertIn("one line only", res2)

    def test_run_native_terminal_capture_off_no_history_read(self):
        tid = "shell-104-nocap"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True, capture_output=False,
                app="iterm", tty="/dev/ttys002",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", return_value=False), \
             mock.patch.object(native_terminals, "type_command") as mock_type, \
             mock.patch.object(native_terminals, "read_history") as mock_hist:

            def record_event(*args, **kwargs):
                terminals.record_command(
                    self.terminals_dir, tid, "ls", exit_code=0,
                    start_ts=time.time(), end_ts=time.time() + 0.1,
                )
                return True, ""

            mock_type.side_effect = record_event
            res = server.t_run_terminal(tid, "ls", timeout_seconds=2)
            mock_hist.assert_not_called()
            self.assertEqual(res, "[exit 0] (output capture is off for this terminal)")

    def test_run_native_terminal_sharing_turned_off_mid_run(self):
        tid = "shell-105-midrun"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True, capture_output=True,
                app="terminal", tty="/dev/ttys001",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", return_value=False), \
             mock.patch.object(native_terminals, "type_command") as mock_type:

            def turn_off_sharing(*args, **kwargs):
                server.set_terminal_sharing(False)
                terminals.record_command(
                    self.terminals_dir, tid, "echo off", exit_code=0,
                    start_ts=time.time(), end_ts=time.time() + 0.1,
                )
                return True, ""

            mock_type.side_effect = turn_off_sharing
            res = server.t_run_terminal(tid, "echo off", timeout_seconds=2)
            self.assertIn("[REFUSED]", res)
            self.assertIn("terminal sharing was turned off while the command ran; its output was not kept.", res)

            rec = terminals.get_terminal(self.terminals_dir, tid)
            self.assertEqual(rec["commands"][-1]["output"], "")

    def test_ghostty_hook_terminal_refused(self):
        tid = "shell-106-ghostty"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True,
                app="", tty="",
            )
        res = server.t_run_terminal(tid, "echo hi", timeout_seconds=1)
        self.assertIn("[REFUSED]", res)
        self.assertIn("only Terminal.app and iTerm2 can run commands from Subway", res)

    def test_1743_automation_error_message(self):
        err_out = "execution error: Not authorized to send Apple events to Terminal. (-1743)"
        with mock.patch.object(native_terminals, "_osascript", return_value=(False, err_out)):
            ok, msg = native_terminals.type_command("terminal", "/dev/ttys001", "ls")
            self.assertFalse(ok)
            expected = "macOS blocked Subway from controlling Terminal. Allow it in System Settings > Privacy & Security > Automation, then try again."
            self.assertEqual(msg, expected)

            with self.assertRaises(PermissionError) as ctx:
                native_terminals.is_busy("terminal", "/dev/ttys001")
            self.assertIn(expected, str(ctx.exception))

        tid = "shell-107-perm"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, allow_commands=True,
                app="terminal", tty="/dev/ttys001",
            )
        with mock.patch.object(server, "get_permission_mode", return_value="auto_approve"), \
             mock.patch.object(native_terminals, "is_busy", side_effect=PermissionError(expected)):
            res = server.t_run_terminal(tid, "ls")
            self.assertIn(expected, res)

    def test_event_thread_capture_stores_redacted_output_only_when_all_switches_on(self):
        tid = "shell-108-capture"
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(
                self.terminals_dir, tid,
                share_enabled=True, capture_output=True,
                app="terminal", tty="/dev/ttys001",
            )
        server.set_terminal_sharing(True)

        now = time.time()
        with mock.patch.object(native_terminals, "read_history", return_value="$ echo secret\nAPI_KEY=sk-supersecret\nhello world\n$ "):
            # Call _capture_hook_output directly or via terminal_event
            terminals.record_command(
                self.terminals_dir, tid, "echo secret", exit_code=0,
                start_ts=now, end_ts=now + 1.0,
            )
            server._capture_hook_output(self.terminals_dir, tid, "echo secret", now, now + 1.0, "terminal", "/dev/ttys001")

            rec = terminals.get_terminal(self.terminals_dir, tid)
            cmd_entry = rec["commands"][-1]
            self.assertNotIn("sk-supersecret", cmd_entry["output"])
            self.assertIn("***REDACTED***", cmd_entry["output"])
            self.assertIn("hello world", cmd_entry["output"])

        # When master switch is OFF, capture does not store output
        server.set_terminal_sharing(False)
        with mock.patch.object(native_terminals, "read_history", return_value="$ echo 2\noutput2\n$ "):
            terminals.record_command(
                self.terminals_dir, tid, "echo 2", exit_code=0,
                start_ts=now + 2.0, end_ts=now + 3.0,
            )
            server._capture_hook_output(self.terminals_dir, tid, "echo 2", now + 2.0, now + 3.0, "terminal", "/dev/ttys001")
            rec2 = terminals.get_terminal(self.terminals_dir, tid)
            self.assertEqual(rec2["commands"][-1]["output"], "")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("osacompile"), "macOS only")
class AppleScriptCompileTests(unittest.TestCase):
    """Every AppleScript must compile; tests that patch _osascript can't catch syntax errors."""

    def test_all_scripts_compile(self):
        out_dir = tempfile.mkdtemp(prefix="subway_osacompile_")
        try:
            for name in [k for k in dir(native_terminals) if k.endswith("_SCRIPT")]:
                res = subprocess.run(["osacompile", "-e", getattr(native_terminals, name),
                                      "-o", os.path.join(out_dir, name + ".scpt")],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(res.returncode, 0, f"{name}: {res.stderr}")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
