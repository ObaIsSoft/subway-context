import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SUBWAY_NO_NOTIFY", "1")
import server


class DesktopNotificationTests(unittest.TestCase):
    def test_chat_budget_warnings_are_keyed_per_chat(self):
        pings = {}
        with mock.patch.object(server, "_state_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(server, "_load_json", return_value=pings), \
             mock.patch.object(server, "_save_json", side_effect=lambda _name, data: pings.update(data)):
            self.assertTrue(server.budget_ping_notice("chat", 800, 1000, subject_id="chat-a"))
            self.assertTrue(server.budget_ping_notice("chat", 800, 1000, subject_id="chat-b"))
            self.assertFalse(server.budget_ping_notice("chat", 800, 1000, subject_id="chat-a"))
        self.assertIn("budget_warning|chat|chat-a|1000|80", pings)
        self.assertIn("budget_warning|chat|chat-b|1000|80", pings)

    def test_provider_budget_key_includes_window_and_reset(self):
        pings = {}
        with mock.patch.object(server, "_state_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(server, "_load_json", return_value=pings), \
             mock.patch.object(server, "_save_json", side_effect=lambda _name, data: pings.update(data)):
            self.assertTrue(server.budget_ping_notice("claude", 800, 1000, window="5h", reset_at=123456))
        self.assertIn("budget_warning|provider|claude|5h|123456|1000|80", pings)

    def test_budget_warning_opens_budget_settings_route(self):
        with mock.patch.object(server, "notify_desktop_event", return_value=True) as notify:
            message = server.budget_ping_notice("chat", 800, 1000, subject_id="chat-a", desktop=True)
        self.assertIn("[BUDGET]", message)
        self.assertEqual(notify.call_args.kwargs["route"], "overview")

    def test_provider_limit_alert_uses_usage_route_and_reset_key(self):
        reset = int(server.time.time()) + 3600
        provider = {
            "id": "claude", "name": "Claude Code",
            "pools": [{"name": "Subscription", "windows": [{
                "key": "5h", "label": "5-hour session", "left_pct": 15, "resets_at": reset,
            }]}],
        }
        with mock.patch.object(server, "_limit_checked", 0), \
             mock.patch.object(server, "_load_json", return_value={"shed_at_pct": 20}), \
             mock.patch.object(server, "discover_connected_providers", return_value=[provider]), \
             mock.patch.object(server, "notify_desktop_event", return_value=True) as notify:
            message = server.low_limit_notice()
        self.assertIn("[LOW LIMIT]", message)
        self.assertIn(f"claude|Subscription|5-hour session|{reset}|20", notify.call_args.args[0])
        self.assertEqual(notify.call_args.kwargs["route"], "usage")

    def test_failed_worker_notifies_and_normal_completion_stays_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            exit_path = os.path.join(directory, "worker.exit")
            for status, should_alert in (("failed", True), ("completed", False)):
                with open(exit_path, "w", encoding="utf-8") as f:
                    json.dump({"status": status, "exit_code": 1 if status == "failed" else 0}, f)
                meta = {"id": "worker_test", "status": "running", "priority": "low"}
                with mock.patch.object(server, "_exit_file", return_value=exit_path), \
                     mock.patch.object(server, "_state_lock", return_value=contextlib.nullcontext()), \
                     mock.patch.object(server, "_load_worker", return_value=meta), \
                     mock.patch.object(server, "_save_worker"), \
                     mock.patch.object(server, "_record_worker_stat"), \
                     mock.patch.object(server, "notify_desktop_event") as notify:
                    server._update_worker_status(meta)
                if should_alert:
                    notify.assert_called_once()
                    self.assertIn("worker_test", notify.call_args.args[0])
                    self.assertEqual(notify.call_args.kwargs["route"], "workers")
                else:
                    notify.assert_not_called()

    def test_notification_failure_is_logged_and_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "notify.log")
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch("sys.platform", "darwin"), \
                 mock.patch("shutil.which", return_value="/opt/homebrew/bin/terminal-notifier"), \
                 mock.patch("subprocess.run", side_effect=OSError("not executable")), \
                 mock.patch.object(server, "SUBWAY_DIR", directory):
                mechanism, error = server._send_desktop_notification("Test", "Message")
            self.assertEqual(mechanism, "terminal-notifier")
            self.assertIn("not executable", error)
            with open(log_path, "r", encoding="utf-8") as f:
                self.assertIn("[terminal-notifier]", f.read())

    def test_terminal_notifier_uses_homebrew_path_when_missing_from_path(self):
        notifier_path = "/opt/homebrew/bin/terminal-notifier"
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.platform", "darwin"), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(server.os.path, "isfile", side_effect=lambda path: path == notifier_path), \
             mock.patch.object(server.os, "access", return_value=True), \
             mock.patch("subprocess.run") as run:
            mechanism, error = server._send_desktop_notification("Test", "Message")
        self.assertEqual((mechanism, error), ("terminal-notifier", None))
        self.assertEqual(run.call_args.args[0][0], notifier_path)

    def test_terminal_notifier_missing_uses_osascript(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.platform", "darwin"), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(server.os.path, "isfile", return_value=False), \
             mock.patch.object(server.os, "access", return_value=False), \
             mock.patch("subprocess.run") as run:
            mechanism, error = server._send_desktop_notification("Test", "Message")
        self.assertEqual((mechanism, error), ("osascript", None))
        self.assertEqual(run.call_args.args[0][:2], ["osascript", "-e"])

    def test_notify_test_cli_sends_and_reports_mechanism(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.platform", "darwin"), \
             mock.patch("shutil.which", return_value="/opt/homebrew/bin/terminal-notifier"), \
             mock.patch("subprocess.run", return_value=completed) as run, \
             mock.patch("builtins.print") as output:
            result = server.run_cli(["notify", "test"])
        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertIn("#workers", run.call_args.args[0][-1])
        self.assertIn("/opt/homebrew/bin/terminal-notifier", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
