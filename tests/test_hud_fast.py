import http.server
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

    def tearDown(self):
        super().tearDown()
        server._hud_cache.clear()
        server._hud_cache.update(self._orig)

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

        refresh_called = threading.Event()

        def slow_compile(*args, **kwargs):
            refresh_called.set()
            time.sleep(0.05)
            return {"version": "new", "fetched_at": time.time()}

        with mock.patch.object(server, "_compile_hud_data", side_effect=slow_compile) as mock_comp:
            t0 = time.time()
            res1 = server.get_hud_data()
            res2 = server.get_hud_data()
            dur = time.time() - t0
            self.assertLess(dur, 0.05)
            self.assertIs(res1, old_data)
            self.assertIs(res2, old_data)
            self.assertTrue(refresh_called.wait(timeout=2.0))
            time.sleep(0.1)
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


if __name__ == "__main__":
    unittest.main()
