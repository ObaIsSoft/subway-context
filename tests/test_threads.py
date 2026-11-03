import os as _sandbox_os, sys as _sandbox_sys  # Isolate HOME, SUBWAY_DIR and agent folders before server loads.
_sandbox_sys.path.insert(0, _sandbox_os.path.dirname(_sandbox_os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401
"""Thread recording (roadmap item 7): threads.py primitives and the server.py hook points."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("SUBWAY_NO_NOTIFY", "1")

import server
import terminals
import threads

CHAT = "chat-aaaaaaaa-1111-2222-3333-444444444444"
W_ROOT = "worker_aaaaaa"
W_PARENT = "worker_bbbbbb"
W_CHILD = "worker_cccccc"
W_LOOSE = "worker_dddddd"
W_SELF = "worker_eeeeee"


def _lines(base_dir, thread_id):
    with open(threads.thread_path(base_dir, thread_id), "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestThreadsModule(unittest.TestCase):
    """threads.py alone: no server state is touched."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="subway_threads_")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_record_and_read_round_trip(self):
        threads.record(self.base, "chat-1", "spawned", "chat-1", peer="worker_aaaaaa",
                       data={"settings": {"provider": "claude"}}, at=100.0)
        threads.record(self.base, "chat-1", "finished", "worker_aaaaaa", data={"status": "completed"}, at=200.0)
        threads.record(self.base, "chat-1", "permission_asked", "worker_aaaaaa", at=300.0)
        result = threads.read_thread(self.base, "chat-1")

        self.assertEqual(result["thread"], "chat-1")
        self.assertEqual(len(result["records"]), 3)
        first, second, third = result["records"]
        self.assertEqual(first["v"], 1)
        self.assertEqual(first["kind"], "spawned")
        self.assertEqual(first["node"], "chat-1")
        self.assertEqual(first["peer"], "worker_aaaaaa")
        self.assertEqual(first["at"], 100.0)
        self.assertEqual(first["data"], {"settings": {"provider": "claude"}})
        self.assertEqual(second["data"], {"status": "completed"})
        self.assertEqual(third["peer"], "")
        self.assertEqual(third["data"], {})

        # Node summary: node = the worker that acted; the peer is the edge source.
        self.assertEqual(result["nodes"]["worker_aaaaaa"]["kinds"], ["finished", "permission_asked"])
        self.assertEqual(result["nodes"]["chat-1"], {"first_at": 100.0, "last_at": 100.0, "kinds": ["spawned"]})
        self.assertEqual(result["nodes"]["worker_aaaaaa"]["first_at"], 100.0)
        self.assertEqual(result["nodes"]["worker_aaaaaa"]["last_at"], 300.0)
        # Only the spawned record has a peer; finished and permission_asked are not edge kinds.
        self.assertEqual(result["edges"], [{"kind": "spawned", "from": "chat-1", "to": "worker_aaaaaa",
                                            "at": 100.0, "data": {"settings": {"provider": "claude"}}}])

    def test_missing_thread_is_empty(self):
        result = threads.read_thread(self.base, "nobody")
        self.assertEqual(result, {"thread": "nobody", "records": [], "nodes": {}, "edges": []})

    def test_unknown_kind_and_empty_id_raise(self):
        with self.assertRaises(ValueError):
            threads.record(self.base, "chat-1", "teleported", "chat-1")
        with self.assertRaises(ValueError):
            threads.read_thread(self.base, "")
        os.makedirs(os.path.join(self.base, threads.THREADS_SUBDIR), exist_ok=True)
        with open(os.path.join(self.base, threads.THREADS_SUBDIR, "scratch.txt"), "w") as f:
            f.write("x")
        before = sorted(os.listdir(os.path.join(self.base, threads.THREADS_SUBDIR)))
        with self.assertRaises(ValueError):
            threads.record(self.base, "", "spawned", "chat-1")
        self.assertEqual(sorted(os.listdir(os.path.join(self.base, threads.THREADS_SUBDIR))), before)

    def test_caps_strings_and_lists(self):
        big = "x" * 2000
        threads.record(self.base, "chat-caps", "note", "chat-caps", data={
            "nested_dict": {"k": big},
            "nested_list": [big, {"deep": [big]}],
            "many": list(range(200)),
            "short": "fine",
        })
        (rec,) = _lines(self.base, "chat-caps")
        data = rec["data"]
        self.assertLessEqual(len(data["nested_dict"]["k"]), 501)
        self.assertTrue(data["nested_dict"]["k"].endswith("…"))
        self.assertLessEqual(len(data["nested_list"][0]), 501)
        self.assertLessEqual(len(data["nested_list"][1]["deep"][0]), 501)
        self.assertLessEqual(len(data["many"]), 50)
        self.assertEqual(data["many"][:3], [0, 1, 2])
        self.assertEqual(data["short"], "fine")

    def test_corrupt_and_half_written_lines_are_skipped(self):
        threads.record(self.base, "chat-corrupt", "spawned", "chat-corrupt", peer="worker_bbbbbb", at=1.0)
        path = threads.thread_path(self.base, "chat-corrupt")
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
            f.write('{"v": 1, "thr')
        result = threads.read_thread(self.base, "chat-corrupt")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["kind"], "spawned")

        # A valid record after the junk is still read.
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        threads.record(self.base, "chat-corrupt", "note", "chat-corrupt", at=2.0)
        result = threads.read_thread(self.base, "chat-corrupt")
        self.assertEqual([r["kind"] for r in result["records"]], ["spawned", "note"])

    def test_safe_name_stays_inside_threads_folder(self):
        folder = os.path.realpath(os.path.join(self.base, threads.THREADS_SUBDIR))
        for tid in ("a/b", "..", "../../etc/passwd", "has spaces", "x" * 300, "worker_abcdef",
                    "a" * 80 + "-" + "0" * 8):
            with self.subTest(tid=tid[:20]):
                path = os.path.realpath(threads.thread_path(self.base, tid))
                self.assertEqual(os.path.dirname(path), folder)
                self.assertLessEqual(len(os.path.basename(path)), 95)  # <=80 + "-" + 8 hash + ".jsonl"
        self.assertEqual(os.path.basename(threads.thread_path(self.base, "x" * 300)), threads.safe_name("x" * 300) + ".jsonl")

        # Two ids that sanitize to the same prefix still get different files.
        self.assertNotEqual(threads.thread_path(self.base, "a/b"), threads.thread_path(self.base, "a_b"))
        threads.record(self.base, "a/b", "note", "n1", at=1.0)
        threads.record(self.base, "a_b", "note", "n2", at=2.0)
        self.assertEqual([r["node"] for r in threads.read_thread(self.base, "a/b")["records"]], ["n1"])
        self.assertEqual([r["node"] for r in threads.read_thread(self.base, "a_b")["records"]], ["n2"])

    def test_concurrent_records_are_all_valid(self):
        errors = []
        start = threading.Event()

        def work(n):
            start.wait(5)
            try:
                for i in range(50):
                    threads.record(self.base, "chat-concurrent", "note", f"n{n}", data={"i": i})
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        workers = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in workers:
            t.start()
        start.set()
        for t in workers:
            t.join(timeout=20)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        recs = threads.read_thread(self.base, "chat-concurrent")["records"]
        self.assertEqual(len(recs), 400)

    def test_list_threads_newest_first(self):
        threads.record(self.base, "chat-old", "spawned", "chat-old", at=10.0)
        threads.record(self.base, "chat-new", "spawned", "chat-new", at=20.0)
        threads.record(self.base, "chat-new", "note", "chat-new", at=30.0)
        listed = threads.list_threads(self.base)
        self.assertEqual([t["thread"] for t in listed], ["chat-new", "chat-old"])
        self.assertEqual(listed[0]["records"], 2)
        self.assertEqual(listed[0]["updated_at"], 30.0)
        self.assertEqual(listed[1]["records"], 1)

        missing = tempfile.mkdtemp(prefix="subway_threads_missing_")
        try:
            self.assertEqual(threads.list_threads(missing), [])
        finally:
            shutil.rmtree(missing, ignore_errors=True)

    def test_record_without_fcntl(self):
        with mock.patch.object(threads, "fcntl", None):
            threads.record(self.base, "chat-nofcntl", "note", "chat-nofcntl", at=5.0)
        self.assertEqual([r["kind"] for r in threads.read_thread(self.base, "chat-nofcntl")["records"]], ["note"])


class ThreadServerCase(unittest.TestCase):
    """Server hook points, against a temporary SUBWAY_DIR and WORKERS_DIR."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subway_thread_srv_")
        self.subway = os.path.join(self.tmp, ".subway-context")
        self.stack = mock.patch.multiple(
            server,
            HOME=self.tmp, SUBWAY_DIR=self.subway,
            WORKERS_DIR=os.path.join(self.subway, "workers"),
            TRACKS_DIR=os.path.join(self.subway, "tracks"),
            LOCKS_FILE=os.path.join(self.subway, "locks.json"),
            QUOTAS_FILE=os.path.join(self.subway, "quotas.json"),
        )
        self.stack.start()
        server._ensure_subway_dir()

    def tearDown(self):
        self.stack.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reads(self, thread_id):
        return threads.read_thread(server.SUBWAY_DIR, thread_id)["records"]

    def save(self, **meta):
        meta.setdefault("role", "Worker")
        meta.setdefault("status", "pending_approval")
        server._save_worker(meta)
        return meta

    # ----- _thread_record / redaction ------------------------------------------------

    def test_thread_record_never_raises(self):
        with mock.patch.object(threads, "record", side_effect=OSError("disk full")):
            self.assertIsNone(server._thread_record("chat-1", "spawned", "chat-1"))
            self.assertIsNone(server._thread_record("chat-1", "spawned", "chat-1", data={"a": "b"}))
        # An unknown kind also fails inside and is swallowed.
        self.assertIsNone(server._thread_record("chat-2", "teleported", "chat-2"))
        # Empty thread id does nothing at all.
        with mock.patch.object(threads, "record") as rec:
            server._thread_record("", "spawned", "x")
        rec.assert_not_called()

    def test_note_still_delivered_when_recording_fails(self):
        recipient = "11111111-1111-1111-1111-111111111111"
        target = {"source": "claude", "id": recipient, "title": "Other"}
        with mock.patch.object(server, "_detected_chat", return_value={"source": "claude", "id": "chat-sender"}), \
             mock.patch.object(server, "resolve", return_value=target), \
             mock.patch.object(threads, "record", side_effect=OSError("disk full")):
            result = server.t_send_note("@claude-main", "hello there")
        self.assertIn("Transit dispatch queued", result)
        inbox = os.path.join(server.SUBWAY_DIR, "inbox", f"{recipient}.jsonl")
        with open(inbox, "r", encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["text"], "hello there")

    def test_note_records_edge(self):
        recipient = "22222222-2222-2222-2222-222222222222"
        target = {"source": "claude", "id": recipient, "title": "Other"}
        with mock.patch.object(server, "_detected_chat", return_value={"source": "claude", "id": "chat-sender"}), \
             mock.patch.object(server, "resolve", return_value=target):
            server.t_send_note("@claude-main", "y" * 500)
        (rec,) = self.reads("chat-sender")
        self.assertEqual((rec["kind"], rec["node"], rec["peer"]), ("note", "chat-sender", recipient))
        self.assertLessEqual(len(rec["data"]["text_head"]), 200)

    def test_secret_in_detail_is_redacted(self):
        token = "npm_abcdefghijklmnopqrstuvwxyz0123456789"
        server._thread_record("chat-secret", "permission_asked", "w", data={
            "detail": f"export NPM_TOKEN={token}"})
        path = threads.thread_path(server.SUBWAY_DIR, "chat-secret")
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(token, raw)
        (rec,) = [json.loads(line) for line in raw.splitlines() if line.strip()]
        self.assertIn("***REDACTED***", rec["data"]["detail"])

    # ----- roots --------------------------------------------------------------------

    def test_thread_root_resolution(self):
        # A worker that was saved with its own root returns it (meta and saved record agree).
        self.save(id=W_ROOT, thread_root=CHAT, status="running")
        self.save(id=W_SELF, thread_root=W_SELF, launched_by="hud")
        self.assertEqual(server._thread_root(server._load_worker(W_ROOT)), CHAT)

        self.save(id=W_PARENT, launched_by="chat-parent")
        for key in ("parent_id", "triggered_by", "rerouted_from"):
            with self.subTest(key=key):
                child = {"id": W_CHILD, key: W_PARENT}
                self.assertEqual(server._thread_root(child), "chat-parent")

        # A worker started from the HUD (or with no launcher) roots its own thread.
        self.assertEqual(server._thread_root({"id": W_LOOSE, "launched_by": "hud"}), W_LOOSE)
        self.assertEqual(server._thread_root({"id": W_LOOSE, "launched_by": ""}), W_LOOSE)
        # Little-endianness check: an unsaved HUD worker meta without thread_root still yields a string.
        self.assertIsInstance(server._thread_root({"id": W_LOOSE}), str)

    def test_thread_root_cycle_terminates(self):
        self.save(id=W_ROOT, parent_id=W_PARENT)
        self.save(id=W_PARENT, parent_id=W_ROOT)
        # C -> a -> b -> a ... : every step is stored, so the recursion stops at the depth limit and
        # returns a real node rather than hanging or raising.
        root = server._thread_root({"id": W_CHILD, "parent_id": W_ROOT})
        self.assertIn(root, (W_ROOT, W_PARENT))
        started = time.monotonic()
        self.assertIsInstance(server._thread_root({"id": W_CHILD, "parent_id": W_CHILD}), str)
        self.assertLess(time.monotonic() - started, 5)

    def test_thread_root_for_caller(self):
        self.assertEqual(server._thread_root_for_caller("chat-1"), "chat-1")
        self.assertEqual(server._thread_root_for_caller(""), "")
        self.save(id=W_ROOT, thread_root=CHAT, status="running")
        self.assertEqual(server._thread_root_for_caller(W_ROOT), CHAT)
        # A worker id that was never saved is returned as-is.
        self.assertEqual(server._thread_root_for_caller(W_CHILD), W_CHILD)

    # ----- context manager and settings ---------------------------------------------

    def test_thread_parent_sets_and_resets(self):
        self.assertIsNone(server._THREAD_PARENT.get())
        with server._thread_parent("worker_aaaaaa", "followed", {"role": "r"}):
            self.assertEqual(server._THREAD_PARENT.get(), ("worker_aaaaaa", "followed", {"role": "r"}))
        self.assertIsNone(server._THREAD_PARENT.get())
        with self.assertRaises(RuntimeError):
            with server._thread_parent("worker_aaaaaa", "rerouted"):
                self.assertIsNotNone(server._THREAD_PARENT.get())
                raise RuntimeError("boom")
        self.assertIsNone(server._THREAD_PARENT.get())

    def test_settings_snapshot_excludes_secrets(self):
        meta = {
            "role": "Architect", "provider": "claude", "model": "sonnet", "effort": "high",
            "mode": "standard", "permission_mode": "auto_safe", "capabilities": ["files:read"],
            "budget": {"max_usd": 2.0, "preflight": "budget ok"}, "timeout_seconds": 60,
            "isolate": True, "private": False, "priority": "normal", "track": "main", "review": "auto",
            "route": "z" * 900, "project_dir": "/proj", "swarm_id": "sw1", "then": [{}, {}],
            "prompt": "TOP SECRET PROMPT", "cmd": ["claude", "--token", "SECRET"],
        }
        snap = server._settings_snapshot(meta)
        for key in ("role", "provider", "model", "effort", "mode", "permission_mode", "capabilities",
                    "timeout_seconds", "isolate", "private", "priority", "track", "review",
                    "project_dir", "swarm_id"):
            self.assertIn(key, snap)
        self.assertEqual(snap["budget"], {"max_usd": 2.0})
        self.assertLessEqual(len(snap["route"]), 300)
        self.assertEqual(snap["steps"], 2)
        self.assertNotIn("prompt", snap)
        self.assertNotIn("cmd", snap)
        self.assertNotIn("TOP SECRET", json.dumps(snap))
        self.assertNotIn("SECRET", json.dumps(snap))

    # ----- request_permission / _answer_permission ----------------------------------

    def test_request_permission_records_redacted_detail(self):
        token = "npm_abcdefghijklmnopqrstuvwxyz0123456789"
        self.save(id=W_ROOT, status="running", launched_by="chat-launcher", thread_root="chat-launcher")
        meta = {"id": W_ROOT, "role": "Worker", "provider": "claude", "launched_by": "chat-launcher",
                "thread_root": "chat-launcher", "permission_mode": "ask", "track": "main"}
        with mock.patch.object(server, "notify_if_still_waiting"), \
             mock.patch.object(server, "_broadcast_track_internal"):
            rid = server.request_permission(meta, "run_command", f"export NPM_TOKEN={token} " + "q" * 900)

        rec = [r for r in self.reads("chat-launcher") if r["kind"] == "permission_asked"][0]
        self.assertEqual(rec["node"], W_ROOT)
        self.assertEqual(rec["data"]["request_id"], rid)
        self.assertEqual(rec["data"]["tool"], "run_command")
        self.assertLessEqual(len(rec["data"]["detail"]), 301)
        self.assertNotIn(token, json.dumps(rec))

        with mock.patch.object(server, "_broadcast_track_internal"):
            server._answer_permission(rid, "deny", "tester")
        answered = [r for r in self.reads("chat-launcher") if r["kind"] == "permission_answered"][0]
        self.assertEqual(answered["node"], W_ROOT)
        self.assertEqual(answered["data"], {"request_id": rid, "decision": "deny", "answered_by": "tester"})

    def test_request_permission_auto_approve_still_records(self):
        meta = {"id": W_PARENT, "role": "Worker", "provider": "claude", "launched_by": "hud",
                "permission_mode": "auto_approve"}
        with mock.patch.object(server, "_broadcast_track_internal"):
            server.request_permission(meta, "read_file", "big.txt")
        kinds = [r["kind"] for r in self.reads(W_PARENT)]
        self.assertEqual(kinds, ["permission_asked", "permission_answered"])

    # ----- lifecycle hooks ----------------------------------------------------------

    def test_stop_reject_escalate_record(self):
        self.save(id=W_ROOT, thread_root=CHAT, status="running", launched_by=CHAT, pid=999999)
        self.save(id=W_PARENT, thread_root=CHAT, status="pending_approval")
        with mock.patch.object(server, "_broadcast_track_internal"), \
             mock.patch.object(server, "_update_worker_status", side_effect=lambda m: m), \
             mock.patch.object(server.os, "kill"):
            self.assertIn("[STOPPING]", server.t_stop_worker(W_ROOT, reason="done"))
        self.assertIn("[REJECTED]", server.t_reject_worker(W_PARENT, reason="not needed"))

        with mock.patch.object(server, "_broadcast_track_internal"), \
             mock.patch.object(server, "_log_tail", return_value=""), \
             mock.patch.object(server, "_update_worker_status", side_effect=lambda m: m), \
             mock.patch.object(server, "check_budget_preflight", return_value=(True, "ok")), \
             mock.patch.object(server, "_self_id", return_value=CHAT), \
             mock.patch.object(server, "_worker_command", return_value="echo test"), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_agy_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "route_task", return_value={
                 "picks": [{"provider": "claude", "model": "sonnet"}], "reason": "test", "private": False}), \
             mock.patch.object(server, "notify_if_still_waiting"):
            server.t_escalate_worker(W_PARENT, new_effort="max", new_mode="god", reason="stuck")

        recs = self.reads(CHAT)
        by_kind = {}
        for r in recs:
            by_kind.setdefault(r["kind"], []).append(r)
        self.assertEqual(by_kind["stopped"][0]["node"], W_ROOT)
        self.assertEqual(by_kind["stopped"][0]["data"]["reason"], "done")
        self.assertEqual(by_kind["rejected"][0]["node"], W_PARENT)
        self.assertEqual(by_kind["rejected"][0]["data"]["reason"], "not needed")
        esc = by_kind["escalated"][0]
        self.assertEqual(esc["node"], W_PARENT)
        self.assertEqual(esc["data"]["to"], {"effort": "max", "mode": "god"})
        self.assertEqual(esc["data"]["reason"], "stuck")

    def test_supervise_finish_records(self):
        worker_id = "worker_000007"
        log_file = os.path.join(self.tmp, "w.log")
        open(log_file, "w", encoding="utf-8").close()
        self.save(id=worker_id, thread_root="chat-root", status="running", pid=os.getpid(),
                  provider="claude", cmd=["/bin/sh", "-c", "exit 7"], timeout_seconds=10,
                  project_dir=self.tmp, log_file=log_file,
                  created_at="2026-01-01T00:00:00+00:00", started_at="2026-01-01T00:00:00+00:00",
                  usage={"input": 10, "output": 2})
        self.assertEqual(server.supervise_worker(worker_id), 0)
        finished = [r for r in self.reads("chat-root") if r["kind"] == "finished"][0]
        self.assertEqual(finished["node"], worker_id)
        data = finished["data"]
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["exit_code"], 7)
        self.assertEqual(data["usage"], {"input": 10, "output": 2})
        self.assertGreaterEqual(data["duration_s"], 0)

    def test_launch_worker_records_started(self):
        worker_id = "worker_abcdef"
        self.save(id=worker_id, thread_root="chat-root", status="pending_approval", provider="claude",
                  cmd=["/bin/sh", "-c", "true"], log_file=os.path.join(self.tmp, "w.log"),
                  project_dir=self.tmp, role="Worker", track="main")
        proc = mock.Mock(pid=4242)
        proc.wait.return_value = 0
        with mock.patch.object(server.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(server, "_capacity_block", return_value=""), \
             mock.patch.object(server, "_git_change_fingerprint", return_value=None), \
             mock.patch.object(server, "_broadcast_track_internal"):
            res = server._launch_worker(worker_id)
        self.assertIn("APPROVED & LAUNCHED", res)
        popen.assert_called_once()
        started = [r for r in self.reads("chat-root") if r["kind"] == "started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["node"], worker_id)
        self.assertEqual(started[0]["data"]["pid"], 4242)

    def test_spin_worker_root_worker_meta_matches_thread_root(self):
        route = {"picks": [{"provider": "claude", "model": "sonnet"}], "reason": "test", "private": False}
        with mock.patch.object(server, "_self_id", return_value="hud"), \
             mock.patch.object(server, "_worker_command", return_value="echo test"), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_agy_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "check_budget_preflight", return_value=(True, "ok")), \
             mock.patch.object(server, "_broadcast_track_internal"), \
             mock.patch.object(server, "notify_if_still_waiting"), \
             mock.patch.object(server, "route_task", return_value=route):
            res = server.t_spin_worker(role="R", prompt="p" * 900, provider="claude", project_dir=self.tmp)
        self.assertIn("APPROVAL REQUIRED", res)
        wid = [w["id"] for w in server._all_workers()][0]
        meta = server._load_worker(wid)
        self.assertEqual(meta["thread_root"], server._thread_root(meta))
        (rec,) = self.reads(meta["thread_root"])
        self.assertEqual((rec["kind"], rec["node"], rec["peer"]), ("spawned", "hud", wid))
        self.assertEqual(len(rec["data"]["prompt_head"]), 200)

    def test_spin_worker_under_thread_parent_shares_thread(self):
        self.save(id=W_PARENT, thread_root=CHAT, status="running")
        route = {"picks": [{"provider": "claude", "model": "sonnet"}], "reason": "test", "private": False}
        with mock.patch.object(server, "_self_id", return_value="chat-other"), \
             mock.patch.object(server, "_worker_command", return_value="echo test"), \
             mock.patch.object(server, "find_claude_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_opencode_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "find_agy_binary", return_value="/bin/echo"), \
             mock.patch.object(server, "check_budget_preflight", return_value=(True, "ok")), \
             mock.patch.object(server, "_broadcast_track_internal"), \
             mock.patch.object(server, "notify_if_still_waiting"), \
             mock.patch.object(server, "route_task", return_value=route), \
             server._thread_parent(W_PARENT, "followed", {"role": "Reviewer", "verdict": True}):
            server.t_spin_worker(role="Reviewer", prompt="check", provider="claude", project_dir=self.tmp)
        child = [w["id"] for w in server._all_workers() if w["id"] != W_PARENT][0]
        self.assertEqual(server._load_worker(child)["thread_root"], CHAT)
        (rec,) = self.reads(CHAT)
        self.assertEqual((rec["kind"], rec["node"], rec["peer"]), ("followed", W_PARENT, child))
        self.assertEqual(rec["data"]["verdict"], True)
        self.assertEqual(rec["data"]["role"], "Reviewer")
        self.assertTrue(rec["data"]["verdict"])
        self.assertEqual(rec["data"]["role"], "Reviewer")

    # ----- t_run_terminal -----------------------------------------------------------

    def _terminal(self, tid="t1"):
        server.set_terminal_sharing(True)
        with server._state_lock("terminals"):
            terminals.set_terminal_opt_in(server._terminals_dir(), tid, name="T", folder="/proj",
                                          share_enabled=True, allow_commands=True, shell_integration=True,
                                          app="code", source="editor")

    def test_terminal_run_records_once(self):
        self._terminal("t1")
        with mock.patch.object(server, "_self_id", return_value="chat-term"), \
             mock.patch.object(server, "get_terminal_mode", return_value="auto_approve"), \
             mock.patch.object(terminals, "create_run_request") as create, \
             mock.patch.object(terminals, "get_run_request",
                               return_value={"status": "done", "exit_code": 0, "output": "",
                                             "created_at": 1.0, "completed_at": 2.0}) as get_req, \
             mock.patch.object(terminals, "record_command") as record_cmd:
            res = server.t_run_terminal("t1", "ls")
        self.assertTrue(res.startswith("[exit 0]"), res)
        create.assert_called_once()
        get_req.assert_called()
        record_cmd.assert_called_once()
        recs = [r for r in self.reads("chat-term") if r["kind"] == "terminal_run"]
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["node"], recs[0]["peer"]), ("chat-term", "t1"))
        self.assertEqual(recs[0]["data"]["kind"], "safe")
        self.assertEqual(recs[0]["data"]["answer"], "")
        self.assertFalse(recs[0]["data"]["asked"])

    def test_terminal_run_denied_records_deny(self):
        self._terminal("t1")
        with mock.patch.object(server, "_self_id", return_value="chat-term"), \
             mock.patch.object(server, "get_terminal_mode", return_value="auto_approve"), \
             mock.patch.object(server, "request_permission", return_value="perm_x"), \
             mock.patch.object(server, "await_permission", return_value="deny"), \
             mock.patch.object(terminals, "create_run_request") as create:
            res = server.t_run_terminal("t1", "npm publish")
        self.assertIn("[DENIED]", res)
        create.assert_not_called()
        recs = [r for r in self.reads("chat-term") if r["kind"] == "terminal_run"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["data"]["answer"], "deny")
        self.assertEqual(recs[0]["data"]["kind"], "protected")


if __name__ == "__main__":
    unittest.main()
