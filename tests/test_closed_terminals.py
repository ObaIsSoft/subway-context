import os as _sandbox_os, sys as _sandbox_sys
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import server
import terminals

DAY = 86400


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


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def raw_record(base, tid):
    with open(os.path.join(base, f"{terminals._slug(tid)}.json"), encoding="utf-8") as f:
        return json.load(f)


class ClosedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="subway_closed_terminals_")
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
        self.base = server._terminals_dir()

    def tearDown(self):
        server.SUBWAY_DIR = self.orig_subway_dir
        server.WORKERS_DIR = self.orig_workers_dir
        server.TRACKS_DIR = self.orig_tracks_dir
        server._self_id = self.orig_self_id
        server._hud_token_cache.clear()
        server._hud_token_cache.update(self.orig_hud_token_cache)
        self.home_patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def hook(self, tid, **kw):
        return terminals.set_terminal_opt_in(self.base, tid, source="hook", share_enabled=True,
                                             name="shell · proj", folder="/proj", **kw)

    # ---- shell_gone ----

    def test_live_shell_stays_open(self):
        rec = self.hook(f"shell-{os.getpid()}-123")
        self.assertFalse(terminals.shell_gone(rec))
        self.assertEqual(terminals.mark_gone_closed(self.base), [])
        self.assertFalse(terminals.get_terminal(self.base, rec["id"]).get("closed"))

    def test_dead_shell_is_closed_and_switched_off(self):
        tid = f"shell-{dead_pid()}-42"
        self.hook(tid, allow_commands=True, capture_output=True)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            closed = terminals.mark_gone_closed(self.base)
        self.assertEqual(closed, [tid])
        rec = terminals.get_terminal(self.base, tid)
        self.assertTrue(rec["closed"])
        self.assertIsInstance(rec["closed_at"], float)
        raw = raw_record(self.base, tid)
        for key in ("allow_commands", "capture_output", "share_enabled", "shell_integration"):
            self.assertNotIn(key, raw)
        self.assertEqual(raw["id"], tid)
        self.assertEqual(raw["name"], "shell · proj")
        self.assertIn("commands", raw)
        self.assertIn("created_at", raw)
        # Idempotent: already closed, not returned again.
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertEqual(terminals.mark_gone_closed(self.base), [])

    def test_permission_error_keeps_open(self):
        tid = f"shell-{os.getpid() + 1}-7"
        self.hook(tid)
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertEqual(terminals.mark_gone_closed(self.base), [])
        self.assertFalse(terminals.get_terminal(self.base, tid).get("closed"))

    def test_malformed_ids_and_editor_never_closed(self):
        for tid in ("shell-abc-1", "shell--1", "term-5", "shell-1-1", "shell-0-9"):
            terminals.set_terminal_opt_in(self.base, tid, source="hook")
        terminals.set_terminal_opt_in(self.base, "shell-999999-1", source="editor")
        with mock.patch("os.kill", side_effect=ProcessLookupError) as kill:
            self.assertEqual(terminals.mark_gone_closed(self.base), [])
        kill.assert_not_called()

    def test_windows_never_calls_os_kill(self):
        self.hook("shell-999999-1")
        with mock.patch.object(terminals.os, "name", "nt"), mock.patch("os.kill") as kill:
            self.assertFalse(terminals.shell_gone({"id": "shell-999999-1", "source": "hook"}))
            self.assertEqual(terminals.mark_gone_closed(self.base), [])
        kill.assert_not_called()

    # ---- opt-in / closing ----

    def test_editor_close_sets_closed_at_and_drops_controls(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor", allow_commands=True,
                                      capture_output=True, shell_integration=True)
        rec = terminals.set_terminal_opt_in(self.base, "t1", closed=True)
        self.assertTrue(rec["closed"])
        self.assertIsInstance(rec["closed_at"], float)
        raw = raw_record(self.base, "t1")
        self.assertNotIn("allow_commands", raw)
        self.assertNotIn("capture_output", raw)

    def test_closed_terminal_cannot_be_turned_back_on(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor", closed=True)
        for kw in ({"allow_commands": True}, {"capture_output": True}, {"share_enabled": True}):
            with self.assertRaises(ValueError):
                terminals.set_terminal_opt_in(self.base, "t1", **kw)
        # Harmless updates are allowed and do not reopen.
        rec = terminals.set_terminal_opt_in(self.base, "t1", name="renamed")
        self.assertTrue(rec["closed"])

    # ---- delete / archive ----

    def test_remove_open_terminal_refused(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor")
        self.assertFalse(terminals.close_log_action(self.base, "t1", "delete"))
        self.assertFalse(terminals.close_log_action(self.base, "t1", "archive"))
        self.assertIsNotNone(terminals.get_terminal(self.base, "t1"))
        self.assertFalse(terminals.close_log_action(self.base, "ghost", "delete"))
        with self.assertRaises(ValueError):
            terminals.close_log_action(self.base, "t1", "explode")

    def test_delete_and_archive_touch_only_that_log(self):
        for tid in ("a", "b"):
            terminals.set_terminal_opt_in(self.base, tid, closed=True)
        keep = terminals._terminal_path(self.base, "b")
        self.assertTrue(terminals.close_log_action(self.base, "a", "delete"))
        self.assertIsNone(terminals.get_terminal(self.base, "a"))
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(terminals.close_log_action(self.base, "b", "archive"))
        self.assertFalse(os.path.exists(keep))
        archived = os.path.join(self.base, terminals.ARCHIVE_SUBDIR, f"{terminals._slug('b')}.json")
        self.assertTrue(os.path.exists(archived))
        self.assertEqual(terminals.list_terminals(self.base), [])
        # Simulate a thread log living next to nothing else in the dir: still untouched.
        self.assertEqual(sorted(os.listdir(self.base)), [terminals.ARCHIVE_SUBDIR])

    def test_archive_closed_keeps_young_and_no_timestamp(self):
        now = 1000 * DAY
        terminals.set_terminal_opt_in(self.base, "old", source="editor", closed=True)
        terminals.set_terminal_opt_in(self.base, "young", source="editor", closed=True)
        terminals.set_terminal_opt_in(self.base, "unnamed", source="editor", closed=True)
        for tid, ts in (("old", now - 8 * DAY), ("young", now - DAY)):
            rec = raw_record(self.base, tid)
            rec["closed_at"] = ts
            terminals._write_json_atomic(terminals._terminal_path(self.base, tid), rec)
        rec = raw_record(self.base, "unnamed")
        rec.pop("closed_at", None)
        terminals._write_json_atomic(terminals._terminal_path(self.base, "unnamed"), rec)
        archived = terminals.archive_closed(self.base, older_than_s=7 * DAY, now=now)
        self.assertEqual(archived, ["old"])
        self.assertIsNotNone(terminals.get_terminal(self.base, "young"))
        self.assertIsNotNone(terminals.get_terminal(self.base, "unnamed"))
        self.assertEqual(raw_record(self.base, "unnamed").get("closed_at"), now)
        # Archived log stays readable by id and is never listed.
        self.assertIsNotNone(terminals.get_terminal(self.base, "old"))
        ids = [r["id"] for r in terminals.list_terminals(self.base)]
        self.assertNotIn("old", ids)
        self.assertEqual(sorted(ids), ["unnamed", "young"])

    def test_archived_log_readable_by_id_and_events_do_not_recreate(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor")
        terminals.record_command(self.base, "t1", "make test", exit_code=0)
        terminals.set_terminal_opt_in(self.base, "t1", closed=True)
        self.assertTrue(terminals.close_log_action(self.base, "t1", "archive"))
        result = terminals.read_terminal(self.base, "t1", caller_id="chat_test")
        self.assertEqual(result["commands"][0]["command"], "make test")
        self.assertIsNone(terminals.record_command(self.base, "t1", "echo hi"))
        self.assertFalse(os.path.exists(terminals._terminal_path(self.base, "t1")))
        self.assertIn("make test", terminals.search_terminal(self.base, "make", terminal_id="t1")[0]["command"])
        via_server = server.t_read_terminal("t1", since="0")
        self.assertIn("make test", via_server)
        with self.assertRaises(ValueError):
            terminals.set_terminal_opt_in(self.base, "t1", source="hook", share_enabled=True)

    def test_list_puts_open_first_and_reports_closed_at(self):
        terminals.set_terminal_opt_in(self.base, "open1", source="editor")
        terminals.set_terminal_opt_in(self.base, "gone", source="editor", closed=True)
        rows = terminals.list_terminals(self.base)
        self.assertEqual([r["id"] for r in rows], ["open1", "gone"])
        self.assertIsNone(rows[0]["closed_at"])
        self.assertIsInstance(rows[1]["closed_at"], float)

    # ---- server ----

    def test_t_list_terminals_hides_closed(self):
        terminals.set_terminal_opt_in(self.base, "open1", source="editor", name="Open")
        terminals.set_terminal_opt_in(self.base, "gone", source="editor", closed=True)
        listing = server.t_list_terminals()
        self.assertIn("open1", listing)
        self.assertNotIn("`gone`", listing)
        self.assertIn("1 closed terminal(s) hidden", listing)

    def test_get_terminals_tidies_dead_shells(self):
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.hook("shell-999999-1")
            handler = _HudPostHandler("/api/terminals", {}, token=server.hud_token())
            handler.do_GET()
        rows = handler.sent["terminals"]
        self.assertTrue(rows[0]["closed"])
        self.assertIsInstance(rows[0]["closed_at"], float)

    def test_terminal_log_without_token_refused(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor", closed=True)
        handler = _HudPostHandler("/api/terminal_log", {"terminal_id": "t1", "action": "delete"})
        handler.do_POST()
        self.assertEqual(handler.status, 401)
        self.assertIsNotNone(terminals.get_terminal(self.base, "t1"))

    def post(self, payload):
        handler = _HudPostHandler("/api/terminal_log", payload, token=server.hud_token())
        handler.do_POST()
        return handler

    def test_terminal_log_routes(self):
        terminals.set_terminal_opt_in(self.base, "open1", source="editor")
        terminals.set_terminal_opt_in(self.base, "gone", source="editor", closed=True)
        self.assertEqual(self.post({"terminal_id": "open1", "action": "delete"}).status, 404)
        self.assertEqual(self.post({"terminal_id": "ghost", "action": "delete"}).status, 404)
        self.assertEqual(self.post({"terminal_id": "gone", "action": "nuke"}).status, 400)
        handler = self.post({"terminal_id": "gone", "action": "archive"})
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.sent["result"], {"done": ["gone"], "action": "archive"})
        handler = self.post({"all_closed": True, "action": "delete"})
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.sent["result"]["done"], [])
        self.assertIsNotNone(terminals.get_terminal(self.base, "open1"))

    def test_terminal_log_all_closed_deletes(self):
        for tid in ("a", "b"):
            terminals.set_terminal_opt_in(self.base, tid, source="editor", closed=True)
        handler = self.post({"all_closed": True, "action": "delete"})
        self.assertEqual(sorted(handler.sent["result"]["done"]), ["a", "b"])
        self.assertEqual(terminals.list_terminals(self.base), [])

    def test_opt_in_conflict_on_closed_terminal(self):
        terminals.set_terminal_opt_in(self.base, "t1", source="editor", closed=True)
        handler = _HudPostHandler("/api/terminal_opt_in",
                                  {"terminal_id": "t1", "share_enabled": True}, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 409)
        self.assertEqual(handler.sent["error"], "terminal is closed")

    def test_run_terminal_deleted_and_closed(self):
        server.set_terminal_sharing(True)
        terminals.set_terminal_opt_in(self.base, "gone", source="editor", closed=True)
        self.assertIn("not found", server.t_run_terminal("removed", "echo hi"))
        self.assertIn("is closed", server.t_run_terminal("gone", "echo hi"))

    def test_event_for_deleted_dead_shell_does_not_recreate(self):
        server.set_terminal_sharing(True)
        tid = f"shell-{dead_pid()}-9"
        self.hook(tid)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertEqual(terminals.mark_gone_closed(self.base), [tid])
        self.assertTrue(terminals.close_log_action(self.base, tid, "archive"))
        self.assertTrue(terminals.close_log_action(self.base, tid, "archive"))  # already archived: no-op
        self.assertFalse(os.path.exists(terminals._terminal_path(self.base, tid)))
        handler = _HudPostHandler("/api/terminal_event", {
            "terminal_id": tid, "command": "ls", "exit_code": 0, "folder": "/proj", "caller_id": "shell",
        }, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 403)
        self.assertFalse(os.path.exists(terminals._terminal_path(self.base, tid)))
        # A retried opt-in after the deletion is refused: the shared record is closed.
        self.assertIn("closed", server.t_run_terminal(tid, "echo hi"))

    def test_event_for_never_known_dead_shell_does_not_recreate(self):
        server.set_terminal_sharing(True)
        tid = f"shell-{dead_pid()}-9"
        handler = _HudPostHandler("/api/terminal_event", {
            "terminal_id": tid, "command": "ls", "exit_code": 0, "folder": "/proj", "caller_id": "shell",
        }, token=server.hud_token())
        handler.do_POST()
        self.assertEqual(handler.status, 403)
        self.assertFalse(os.path.exists(terminals._terminal_path(self.base, tid)))


if __name__ == "__main__":
    unittest.main()
