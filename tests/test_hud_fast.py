import http.server
import os
import time
import unittest
import threading
from unittest import mock

import server
from tests import test_subway


class TestHUDFast(test_subway.SubwayTestCase):
    def setUp(self):
        super().setUp()
        self._orig = dict(server._hud_cache)
        server._hud_cache.clear()
        with server._source_in_flight_lock:
            server._source_in_flight.clear()
        server._ledger_cache.clear()

    def tearDown(self):
        super().tearDown()
        server._hud_cache.clear()
        server._hud_cache.update(self._orig)
        with server._source_in_flight_lock:
            server._source_in_flight.clear()
        server._ledger_cache.clear()

    def test_send_json_swallows_broken_pipe(self):
        class DummyHandler(server.SubwayHUDHandler):
            def __init__(self):
                self.wfile = mock.MagicMock()
                self._headers_buffer = []

            def send_response(self, code, message=None):
                pass

            def send_header(self, keyword, value):
                pass

            def end_headers(self):
                pass

            def _security_headers(self, nonce=""):
                pass

        handler = DummyHandler()
        handler.wfile.write.side_effect = BrokenPipeError(32, "Broken pipe")
        handler.send_json({"hello": "world"})

        handler.wfile.write.side_effect = ConnectionResetError(54, "Connection reset by peer")
        handler.send_json({"hello": "world"})

        srv = server.SubwayHUDServer.__new__(server.SubwayHUDServer)
        with mock.patch("sys.exc_info", return_value=(BrokenPipeError, BrokenPipeError(), None)), \
             mock.patch.object(http.server.ThreadingHTTPServer, "handle_error") as mock_super:
            srv.handle_error(None, None)
            mock_super.assert_not_called()

        with mock.patch("sys.exc_info", return_value=(ValueError, ValueError("boom"), None)), \
             mock.patch.object(http.server.ThreadingHTTPServer, "handle_error") as mock_super:
            srv.handle_error(None, None)
            mock_super.assert_called_once()

    def test_second_get_hud_data_returns_cached_within_10s(self):
        with mock.patch.object(server, "discover_connected_providers", return_value=[]) as mock_prov:
            res1 = server.get_hud_data()
            self.assertIn("fetched_at", res1)
            self.assertGreater(mock_prov.call_count, 0)
            mock_prov.reset_mock()

            res2 = server.get_hud_data()
            self.assertEqual(res1["fetched_at"], res2["fetched_at"])
            mock_prov.assert_not_called()

    def test_old_cache_returns_at_once_and_triggers_single_refresh(self):
        old_data = {"version": "cached", "fetched_at": time.time() - 25.0}
        server._hud_cache[""] = {"data": old_data, "at": time.time() - 25.0, "stale": False, "busy": False}

        refresh_started = threading.Event()
        release_refresh = threading.Event()
        read_returned = threading.Event()
        results = []

        def slow_compile(*args, **kwargs):
            refresh_started.set()
            release_refresh.wait(timeout=2.0)
            return {"version": "new", "fetched_at": time.time()}

        with mock.patch.object(server, "_compile_hud_data", side_effect=slow_compile) as mock_comp:
            def read_cached_data():
                results.extend((server.get_hud_data(), server.get_hud_data()))
                read_returned.set()

            reader = threading.Thread(target=read_cached_data)
            reader.start()
            try:
                self.assertTrue(refresh_started.wait(timeout=1.0))
                self.assertTrue(read_returned.wait(timeout=0.5))
                self.assertEqual(results, [old_data, old_data])
            finally:
                release_refresh.set()
                reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive())

            deadline = time.time() + 1.0
            while time.time() < deadline and server._hud_cache[""]["data"].get("version") != "new":
                time.sleep(0.01)
            self.assertEqual(server._hud_cache[""]["data"].get("version"), "new")
            self.assertEqual(mock_comp.call_count, 1)

    def test_post_marks_cache_stale(self):
        server._hud_cache[""] = {"data": {"version": "cached", "fetched_at": time.time()}, "at": time.time(), "stale": False, "busy": False}

        class PostHandler(server.SubwayHUDHandler):
            def __init__(self, path, payload):
                self.path = path
                self._payload = payload
                self.headers = {"Host": "127.0.0.1"}

            def _gate(self, api=True):
                return True

            def _read_payload(self):
                return self._payload

            def send_json(self, data, status=200):
                self.sent_data = data

        handler = PostHandler("/api/stop_worker", {"id": "worker_abc", "reason": "stop"})
        with mock.patch.object(server, "t_stop_worker", return_value="stopped"):
            handler.do_POST()

        self.assertTrue(server._hud_cache[""]["stale"])

    def test_routing_sees_exhausted_provider_even_with_stale_hud_cache(self):
        old_hud_data = {
            "version": server.VERSION,
            "fetched_at": time.time(),
            "quotas": {
                "providers": [
                    {
                        "id": "claude",
                        "pools": [{"windows": [{"key": "weekly", "left_pct": 80.0}]}]
                    }
                ]
            }
        }
        server._hud_cache[""] = {"data": old_hud_data, "at": time.time(), "stale": False, "busy": False}

        self.mock_claude_data = {
            "five_hour": {"used_pct": 50.0, "resets_at": time.time() + 3600},
            "seven_day": {"used_pct": 100.0, "resets_at": time.time() + 86400 * 3},
        }
        server.t_spin_worker(role="Architect", prompt="Complete overhaul", provider="auto", mode="god")
        workers = server._all_workers()
        self.assertTrue(len(workers) > 0)
        wid = workers[0]["id"]
        meta = server._load_worker(wid)
        self.assertEqual(meta["provider"], "antigravity")

    def test_mocked_5s_source_first_hud_under_2_5s_and_eventual_fresh_data(self):
        server._hud_cache.clear()
        with server._usage_lock:
            server._usage.clear()

        fresh = {
            "five_hour": {"used_pct": 5.0, "resets_at": time.time() + 3600},
            "seven_day": {"used_pct": 3.0, "resets_at": time.time() + 86400 * 3},
        }

        def mock_sleep_5s():
            time.sleep(5.0)
            return fresh

        server._SOURCES["claude_live"] = (mock_sleep_5s, 60.0)

        t0 = time.time()
        res1 = server.get_hud_data()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.5, f"first get_hud_data took {elapsed:.2f}s, expected < 2.5s")

        claude = next((p for p in res1["quotas"]["providers"] if p["id"] == "claude"), None)
        self.assertIsNotNone(claude)
        self.assertTrue(claude.get("loading") or claude.get("stale"))

        time.sleep(5.2)

        res2 = server.get_hud_data()
        claude2 = next((p for p in res2["quotas"]["providers"] if p["id"] == "claude"), None)
        self.assertIsNotNone(claude2)
        self.assertFalse(claude2.get("loading"))
        windows = (claude2.get("pools") or [{}])[0].get("windows") or []
        self.assertTrue(any(w.get("left_pct") == 95.0 for w in windows))

    def test_slow_source_with_saved_disk_returns_stale_with_age(self):
        server._hud_cache.clear()
        with server._usage_lock:
            server._usage.clear()

        old_live_data = {
            "five_hour": {"used_pct": 50.0, "resets_at": time.time() + 1000},
            "seven_day": {"used_pct": 40.0, "resets_at": time.time() + 5000},
        }
        fetched_at = time.time() - 45.0
        with server._state_lock("usage"):
            server._save_json("usage_cache.json", {"claude_live": {"data": old_live_data, "fetched_at": fetched_at}})

        def slow_fetch():
            time.sleep(2.0)
            return {"five_hour": {"used_pct": 1.0}, "seven_day": {"used_pct": 2.0}}

        server._SOURCES["claude_live"] = (slow_fetch, 60.0)

        t0 = time.time()
        res = server.get_hud_data()
        elapsed = time.time() - t0

        self.assertLess(elapsed, 2.5)
        claude_prov = next((p for p in res["quotas"]["providers"] if p["id"] == "claude"), None)
        self.assertIsNotNone(claude_prov)
        self.assertTrue(claude_prov.get("stale"))
        self.assertAlmostEqual(claude_prov.get("age", 0), 45.0, delta=5.0)

    def test_slow_other_sessions_scan_runs_in_background(self):
        provider = "hud-fast-test"
        old_cache = server._other_sessions_cache.pop(provider, None)
        started = threading.Event()
        finished = threading.Event()

        def slow_scan(provider, now):
            started.set()
            time.sleep(0.5)
            finished.set()
            return {"sessions": 1, "tokens_5h": 10}

        try:
            with mock.patch.object(server, "_other_sessions_uncached", side_effect=slow_scan):
                t0 = time.perf_counter()
                sessions = server._other_sessions_for_hud(provider)
                elapsed = time.perf_counter() - t0
                self.assertLess(elapsed, 0.25)
                self.assertIsNone(sessions)
                self.assertTrue(started.wait(timeout=1.0))
                self.assertTrue(finished.wait(timeout=1.0))
                self.assertEqual(server._other_sessions_cache[provider][1]["sessions"], 1)
        finally:
            server._other_sessions_cache.pop(provider, None)
            if old_cache is not None:
                server._other_sessions_cache[provider] = old_cache

    def test_scan_ledger_skips_unchanged_files(self):
        f1 = os.path.join(self.test_dir, "transcript1.jsonl")
        f2 = os.path.join(self.test_dir, "transcript2.jsonl")
        with open(f1, "w") as f:
            f.write("event1\n")
        with open(f2, "w") as f:
            f.write("event2\n")

        now = time.time()
        parse_mock = mock.MagicMock(side_effect=lambda p: [(now, 100, 50, 10)])

        res1 = server._scan_ledger([f1, f2], parse_mock)
        self.assertEqual(parse_mock.call_count, 2)
        self.assertEqual(res1["5h"]["input"], 200)

        res2 = server._scan_ledger([f1, f2], parse_mock)
        self.assertEqual(parse_mock.call_count, 2, "Unchanged files should not be re-parsed")
        self.assertEqual(res2["5h"]["input"], 200)

        time.sleep(0.05)
        with open(f1, "a") as f:
            f.write("event3\n")
        os.utime(f1, (time.time() + 2, time.time() + 2))

        parse_mock.side_effect = lambda p: [(now, 150, 50, 10)]
        res3 = server._scan_ledger([f1, f2], parse_mock)
        self.assertEqual(parse_mock.call_count, 3, "Only changed file f1 should be parsed")
        self.assertEqual(res3["5h"]["input"], 250)


if __name__ == "__main__":
    unittest.main()
