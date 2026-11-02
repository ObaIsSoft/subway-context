import json
import os
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
