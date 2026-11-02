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

    def _build_session_state(self):
        """A ~/.copilot/session-state look-alike holding the fixture's usage checkpoints, timestamped now."""
        sess_dir = os.path.join(self.temp_dir, "session-now")
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
        return self.temp_dir

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

        with patch.object(server, "find_copilot_binary", return_value="/mock/copilot"), \
             patch.object(server, "_launch_worker") as launch, \
             patch.object(server, "start_approved_workers"), \
             patch.object(server, "notify_if_still_waiting"), \
             patch.object(server, "t_broadcast_track"), \
             patch.object(server.subprocess, "Popen", side_effect=OSError("no subprocess may start")) as popen:
            res = server.t_spin_worker(
                role="Unit Tester",
                prompt="echo hello",
                provider="copilot",
                model="auto",
            )
            self.assertIn("worker_", res)
            launch.assert_not_called()  # No worker is launched: the record waits for the user's approval.
            self.assertFalse([c for c in popen.call_args_list if "__supervise" in (c.args[0] if c.args else [])])

    def test_result_event_records_credits(self):
        line = json.dumps({"type": "result", "sessionId": "s1", "result": "done",
                           "usage": {"premiumRequests": 2, "credits": 0.5}})
        run = server.parse_copilot_jsonl(line)
        self.assertEqual(run["usage"]["requests"], 2)
        self.assertAlmostEqual(run["usage"]["credits"], 0.5)

    def test_copilot_view_is_json_serialisable(self):
        res = server._copilot_cli_usage(self._build_session_state())
        self.assertIsNotNone(res)
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        raw = {"copilot_plan": "individual", "quota_reset_date": "2026-12-01",
               "quota_snapshots": {"chat": {"percent_remaining": 50.0, "remaining": 100,
                                            "entitlement": 200, "unlimited": False}}}
        with patch.object(server, "_copilot_cli_usage", return_value=res), \
             patch.object(server, "_copilot_raw", return_value=raw), \
             patch.object(server, "_copilot_models", return_value=["mai-code-1.1-flash"]), \
             patch.object(server, "_other_sessions", return_value=[]):
            view = server._copilot_view(client, refresh=True)
        json.dumps(view)  # A self-referential usage dict used to raise "Circular reference detected".
        self.assertEqual(view["credits"], res["credits"])

    def test_chat_pool_unknown_keeps_copilot_dispatchable(self):
        with patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "_copilot_models", return_value=["mai-code-1.1-flash"]), \
             patch.object(server, "_other_sessions", return_value=[]):
            client = {"id": "copilot", "installed": True, "dispatchable": True}
            with patch.object(server, "_copilot_raw", return_value={"quota_snapshots": {}}):
                server._copilot_view(client, refresh=True)
            self.assertTrue(client["dispatchable"])

            client = {"id": "copilot", "installed": True, "dispatchable": True}
            empty = {"quota_snapshots": {"chat": {"remaining": 0, "entitlement": 200, "unlimited": False}}}
            with patch.object(server, "_copilot_raw", return_value=empty):
                server._copilot_view(client, refresh=True)
            self.assertFalse(client["dispatchable"])

    def test_review_step_prompt_carries_builder_task(self):
        meta = {"id": "worker_abc123", "status": "completed", "provider": "claude", "role": "Builder",
                "track": "main", "report": "changed two files",
                "prompt": "Work only in this repo. No git.\n\nAdd a --dry-run flag to the exporter.",
                "then": [server._review_trigger_step("auto")]}
        captured = {}

        def fake_spin(**kwargs):
            captured.update(kwargs)
            return "Worker worker_def456 queued."

        with patch.object(server, "_load_worker", side_effect=lambda wid: dict(meta) if wid == meta["id"] else None), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "route_task", return_value={"picks": [{"provider": "copilot", "model": "auto"}], "reason": ""}), \
             patch.object(server, "t_spin_worker", side_effect=fake_spin), \
             patch.object(server, "_launch_worker"), \
             patch.object(server, "t_broadcast_track"):
            server._run_triggers(meta["id"], {"status": "completed", "response": "done"})

        child_prompt = captured.get("prompt", "")
        self.assertIn("TASK:", child_prompt)
        self.assertIn("Add a --dry-run flag to the exporter.", child_prompt)
        self.assertNotIn("Work only in this repo", child_prompt)
        self.assertLess(child_prompt.index("TASK:"), child_prompt.index("PREVIOUS WORKER"))

    def test_copilot_auto_cli_catalog_entry(self):
        server._model_catalog.clear()
        with patch.object(server, "find_copilot_binary", return_value=None):
            catalog_no_cli = server.model_catalog()
            copilot_autos = [m for m in catalog_no_cli if m.get("provider") == "copilot" and m.get("model") == "auto"]
            self.assertEqual(len(copilot_autos), 0)

        server._model_catalog.clear()
        with patch.object(server, "find_copilot_binary", return_value="/usr/local/bin/copilot"):
            catalog_cli = server.model_catalog()
            copilot_autos = [m for m in catalog_cli if m.get("provider") == "copilot" and m.get("model") == "auto"]
            self.assertEqual(len(copilot_autos), 1)

    def test_route_task_copilot_chat_allowance(self):
        healthy_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 100, "entitlement": 200, "percent_remaining": 50.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        low_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 10, "entitlement": 200, "percent_remaining": 5.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        zero_raw = {
            "quota_snapshots": {
                "chat": {"remaining": 0, "entitlement": 200, "percent_remaining": 0.0, "unlimited": False},
                "premium_interactions": {"remaining": 0, "entitlement": 0, "unlimited": False}
            }
        }
        provs = [
            {"id": "copilot", "client": {"dispatchable": True}},
            {"id": "antigravity", "client": {"dispatchable": True}}
        ]
        with patch.object(server, "_copilot_models", return_value=[{"model": "copilot-small", "coding": 2, "reasoning": 2, "writing": 2}]):
            server._model_catalog.clear()
            # Healthy chat pool picks Copilot for a small task when it is cheapest
            with patch.object(server, "_copilot_raw", return_value=healthy_raw):
                res_healthy = server.route_task("fix typo in readme", providers=provs)
                self.assertEqual(res_healthy["picks"][0]["provider"], "copilot")

            # Chat under shed_at_pct demotes it
            with patch.object(server, "_copilot_raw", return_value=low_raw):
                res_low = server.route_task("fix typo in readme", providers=provs)
                self.assertNotEqual(res_low["picks"][0]["provider"], "copilot")

            # Chat 0 excludes it
            with patch.object(server, "_copilot_raw", return_value=zero_raw):
                res_zero = server.route_task("fix typo in readme", providers=provs)
                self.assertFalse(any(p["provider"] == "copilot" for p in res_zero["picks"]))

    def test_token_counting_model_metrics(self):
        ev = {
            "type": "session.usage_checkpoint",
            "data": {
                "tokenDetails": {
                    "input": {"tokenCount": 3},
                    "output": {"tokenCount": 100},
                    "cache_read": {"tokenCount": 50}
                },
                "modelMetrics": {
                    "gpt-4o": {
                        "usage": {
                            "inputTokens": 25000,
                            "outputTokens": 100,
                            "cacheReadTokens": 50,
                            "cacheWriteTokens": 0
                        }
                    }
                }
            }
        }
        run = {}
        server._process_copilot_event(ev, run, lambda text: None)
        self.assertEqual(run["usage"]["input_tokens"], 25000)
        self.assertEqual(run["usage"]["output_tokens"], 100)
        self.assertEqual(run["spent"], 25100)

    def test_copilot_models_deduplicate(self):
        raw_models = [
            {"id": "mai-code-1-flash-4th", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1.1-flash", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1-flash-secondary", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "mai-code-1-flash-tertiary", "name": "MAI-Code-1.1-Flash", "policy": {"state": "enabled"}},
            {"id": "kimi-k3-copilot", "name": "Kimi K3 (GitHub)", "policy": {"state": "enabled"}},
            {"id": "kimi-k3-base", "name": "Kimi K3 (GitHub)", "policy": {"state": "enabled"}},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = os.path.join(tmp_dir, "Library", "Application Support", "Code", "User", "workspaceStorage", "ws1", "GitHub.copilot-chat", "debug-logs", "run1")
            os.makedirs(model_dir, exist_ok=True)
            with open(os.path.join(model_dir, "models.json"), "w", encoding="utf-8") as f:
                json.dump(raw_models, f)

            def fake_expand(p):
                if p.startswith("~/"):
                    return os.path.join(tmp_dir, p[2:])
                return tmp_dir if p == "~" else p

            with patch("os.path.expanduser", side_effect=fake_expand):
                server._copilot_models.clear()
                models = server._copilot_models()
                self.assertEqual(len(models), 2)
                mai = next(m for m in models if m["name"] == "MAI-Code-1.1-Flash")
                kimi = next(m for m in models if m["name"] == "Kimi K3 (GitHub)")
                self.assertEqual(mai["model"], "mai-code-1.1-flash")
                self.assertEqual(set(mai["aliases"]), {"mai-code-1-flash-4th", "mai-code-1-flash-secondary", "mai-code-1-flash-tertiary"})
                self.assertEqual(kimi["model"], "kimi-k3-base")
                self.assertEqual(kimi["aliases"], ["kimi-k3-copilot"])


if __name__ == "__main__":
    unittest.main()
