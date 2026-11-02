import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import server


class TestCopilotWorkers(unittest.TestCase):
    def setUp(self):
        server._copilot_raw.clear()
        server._copilot_models.clear()
        server._model_catalog.clear()

    def test_parse_fixture(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "copilot_run.jsonl")
        with open(fixture_path, "r", encoding="utf-8") as f:
            content = f.read()
        run = server.parse_copilot_jsonl(content)
        self.assertEqual(run.get("response"), "OK")
        self.assertEqual(run.get("model_used"), "mai-code-1.1-flash")
        u = run.get("usage", {})
        self.assertAlmostEqual(u.get("credits"), 0.28194, places=5)
        self.assertEqual(u.get("requests"), 1)
        self.assertEqual(u.get("input_tokens"), 14067)
        self.assertEqual(u.get("output_tokens"), 5)

        meta = {}
        server._add_usage(meta, u)
        self.assertEqual(meta["usage"]["input"], 14067)
        self.assertEqual(meta["usage"]["output"], 5)
        self.assertEqual(meta["usage"]["requests"], 1)
        self.assertAlmostEqual(meta["usage"]["credits"], 0.28194, places=5)

    def test_worker_command_isolate(self):
        cmd_plain = server._worker_command("copilot", "/usr/bin/copilot", "auto", "low", False,
                                           [], "normal", "hello", None, "", isolate=False)
        self.assertNotIn("--allow-all-tools", cmd_plain)

        cmd_iso = server._worker_command("copilot", "/usr/bin/copilot", "auto", "low", False,
                                         [], "normal", "hello", None, "", isolate=True)
        self.assertIn("--allow-all-tools", cmd_iso)

        cmd_skip = server._worker_command("copilot", "/usr/bin/copilot", "auto", "low", False,
                                          [], "auto_approve", "hello", None, "", isolate=False)
        self.assertIn("--allow-all-tools", cmd_skip)

    def test_preflight_budget_names_chat_pool(self):
        mock_raw = {
            "copilot_plan": "individual",
            "quota_reset_date": "2026-12-01",
            "quota_snapshots": {
                "chat": {"percent_remaining": 75.0, "remaining": 1500, "entitlement": 2000, "unlimited": False}
            }
        }
        with patch.object(server, "_copilot_raw", return_value=mock_raw), \
             patch.object(server, "discover_connected_providers", return_value=[{"id": "copilot", "name": "GitHub Copilot", "state": "live"}]):
            ok, msg = server.check_budget_preflight(provider="copilot")
            self.assertTrue(ok)
            self.assertIn("chat pool", msg.lower())
            self.assertIn("1500 of 2000", msg)
            self.assertIn("2026-12-01", msg)

    def test_dispatchable_with_and_without_cli(self):
        mock_raw = {
            "quota_snapshots": {
                "chat": {"percent_remaining": 50.0, "remaining": 100, "entitlement": 200, "unlimited": False}
            }
        }
        with patch.object(server, "_copilot_raw", return_value=mock_raw):
            server.detect_clients.clear()
            with patch("server.shutil.which", side_effect=lambda bin_name: "/usr/local/bin/copilot" if bin_name == "copilot" else None):
                provs = server.discover_connected_providers(refresh=True)
                copilot_prov = next((p for p in provs if p["id"] == "copilot"), None)
                self.assertIsNotNone(copilot_prov)
                self.assertTrue(copilot_prov["client"]["dispatchable"])

            server.detect_clients.clear()
            with patch("server.shutil.which", return_value=None):
                provs = server.discover_connected_providers(refresh=True)
                copilot_prov = next((p for p in provs if p["id"] == "copilot"), None)
                if copilot_prov:
                    self.assertFalse(copilot_prov["client"]["dispatchable"])


class TestCopilotIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "copilot_run.jsonl")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_copilot_cli_usage_missing_folder(self):
        missing_dir = os.path.join(self.temp_dir, "nonexistent")
        res = server._copilot_cli_usage(missing_dir)
        self.assertIsNone(res)

    def test_copilot_cli_usage_from_fixture(self):
        sess_dir = os.path.join(self.temp_dir, "session-1")
        os.makedirs(sess_dir, exist_ok=True)
        dest_events = os.path.join(sess_dir, "events.jsonl")

        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        with open(self.fixture_path, "r", encoding="utf-8") as f_in, open(dest_events, "w", encoding="utf-8") as f_out:
            for line in f_in:
                if "session.usage_checkpoint" in line:
                    ev = json.loads(line)
                    ev["timestamp"] = now_iso
                    f_out.write(json.dumps(ev) + "\n")
                else:
                    f_out.write(line)

        os.utime(dest_events, (now, now))

        res = server._copilot_cli_usage(self.temp_dir)
        self.assertIsNotNone(res)
        self.assertIn("hourly", res)
        self.assertEqual(len(res["hourly"]), 24)
        self.assertIn("5h", res)
        self.assertIn("7d", res)
        self.assertIn("credits", res)
        self.assertIn("5h", res["credits"])
        self.assertIn("7d", res["credits"])
        self.assertGreater(res["credits"]["5h"], 0)
        self.assertGreater(res["5h"]["input"], 0)
        self.assertGreater(sum(res["hourly"]), 0)

    def test_copilot_view_carries_tokens(self):
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        fake_tokens = {
            "5h": {"input": 100, "output": 20, "cache_read": 0},
            "7d": {"input": 100, "output": 20, "cache_read": 0},
            "hourly": [0] * 23 + [120],
            "credits": {"5h": 0.05, "7d": 0.05},
            "per_model": {"gpt-6-luna": 120},
        }
        with patch.object(server, "_copilot_cli_usage", return_value=fake_tokens):
            view = server._copilot_view(client, refresh=True)
            self.assertIsNotNone(view.get("tokens"))
            self.assertEqual(view["tokens"], fake_tokens)
            self.assertIn("Tokens from Copilot CLI sessions on this Mac", view.get("source", ""))

    def test_preflight_and_spawn_accept_copilot(self):
        ok, msg = server.check_budget_preflight(model="auto", provider="copilot")
        self.assertTrue(ok)

        with patch.object(server, "find_copilot_binary", return_value="/mock/copilot"):
            res = server.t_spin_worker(
                role="Unit Tester",
                prompt="echo hello",
                provider="copilot",
                model="auto",
            )
            self.assertIn("worker_", res)


if __name__ == "__main__":
    unittest.main()
