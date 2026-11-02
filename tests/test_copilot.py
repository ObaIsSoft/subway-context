import io
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
        self.assertEqual(u.get("cache_read_tokens"), 0)
        self.assertTrue(run.get("input_excludes_cache"))

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
        server._copilot_cli_usage.clear()
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

    def test_copilot_cli_usage_excludes_cached_input(self):
        sess_dir = os.path.join(self.temp_dir, "session-cache")
        os.makedirs(sess_dir)
        events_path = os.path.join(sess_dir, "events.jsonl")
        event = {
            "type": "session.usage_checkpoint",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
            "data": {
                "accountingSnapshot": {
                    "tokenDetails": {
                        "input": {"tokenCount": 150},
                        "output": {"tokenCount": 10},
                        "cache_read": {"tokenCount": 800},
                        "cache_write": {"tokenCount": 50},
                    },
                    "modelMetrics": {
                        "model-a": {"usage": {
                            "inputTokens": 1000,
                            "outputTokens": 10,
                            "cacheReadTokens": 800,
                            "cacheWriteTokens": 50,
                        }}
                    },
                }
            },
        }
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        res = server._copilot_cli_usage(self.temp_dir)
        self.assertEqual(res["5h"]["input"], 200)
        self.assertEqual(res["5h"]["cache_read"], 800)
        self.assertEqual(res["hourly"][-1], 210)
        self.assertEqual(res["per_model"]["model-a"], 210)

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

    def test_copilot_view_no_raw_dispatchable_follows_cli(self):
        client = {"id": "copilot", "installed": True, "dispatchable": True}
        with patch.object(server, "_copilot_raw", return_value=None), \
             patch.object(server, "_copilot_models", return_value=[]), \
             patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "find_copilot_binary", return_value="/usr/local/bin/copilot"):
            server._copilot_view(client, refresh=True)
        self.assertTrue(client["dispatchable"])

        client = {"id": "copilot", "installed": True, "dispatchable": True}
        with patch.object(server, "_copilot_raw", return_value=None), \
             patch.object(server, "_copilot_models", return_value=[]), \
             patch.object(server, "_copilot_cli_usage", return_value=None), \
             patch.object(server, "find_copilot_binary", return_value=None):
            server._copilot_view(client, refresh=True)
        self.assertFalse(client["dispatchable"])

    def test_copilot_catalog_carries_aliases(self):
        server._model_catalog.clear()
        with patch.object(server, "_copilot_models",
                           return_value=[{"model": "gpt-5", "name": "GPT-5", "aliases": ["gpt-5-chat"]}]), \
             patch.object(server, "find_copilot_binary", return_value=None):
            catalog = server.model_catalog()
        entry = next(m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "gpt-5")
        self.assertEqual(entry["aliases"], ["gpt-5-chat"])

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
        self.assertEqual(run["usage"]["input_tokens"], 24950)
        self.assertEqual(run["usage"]["output_tokens"], 100)
        self.assertEqual(run["usage"]["cache_read_tokens"], 50)
        self.assertEqual(run["spent"], 25050)
        self.assertTrue(run["input_excludes_cache"])

    def test_copilot_input_clamps_cache_and_handles_missing_cache(self):
        # tokenDetails.input already excludes cache reads (live shape), so it is never reduced by cache_read.
        for input_tokens, cache_read, expected in ((40, 80, 40), (40, None, 40)):
            event = {
                "type": "session.usage_checkpoint",
                "data": {"tokenDetails": {
                    "input": {"tokenCount": input_tokens},
                    "output": {"tokenCount": 5},
                    **({"cache_read": {"tokenCount": cache_read}} if cache_read is not None else {}),
                }},
            }
            run = {}
            server._process_copilot_event(event, run, lambda text: None)
            self.assertEqual(run["usage"]["input_tokens"], expected)
            self.assertEqual(run["usage"]["cache_read_tokens"], cache_read or 0)

        parsed = server.parse_copilot_jsonl(json.dumps({
            "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 12}
        }))
        self.assertEqual(parsed["usage"]["input_tokens"], 0)
        self.assertEqual(parsed["usage"]["cache_read_tokens"], 12)

        no_cache = server.parse_copilot_jsonl(json.dumps({
            "usage": {"input_tokens": 10, "output_tokens": 2}
        }))
        self.assertEqual(no_cache["usage"]["input_tokens"], 10)

    def test_pump_copilot_records_fresh_input(self):
        proc = MagicMock()
        proc.stdout = [json.dumps({
            "type": "session.usage_checkpoint",
            "data": {"tokenDetails": {
                "input": {"tokenCount": 100},
                "output": {"tokenCount": 5},
                "cache_read": {"tokenCount": 60},
            }},
        }).encode("utf-8")]
        run = {}
        server._pump_copilot(proc, io.BytesIO(), run)
        self.assertEqual(run["usage"]["input_tokens"], 100)
        self.assertEqual(run["usage"]["cache_read_tokens"], 60)

    def test_copilot_live_checkpoint_shape_counts_cache_once(self):
        # Numbers from a real 1.3.3 worker checkpoint: modelMetrics input = uncached + cache reads + cache writes.
        td = {"input": {"tokenCount": 159}, "output": {"tokenCount": 66333},
              "cache_read": {"tokenCount": 4476250}, "cache_write": {"tokenCount": 126823}}
        mm = {"gpt-6-luna": {"usage": {"inputTokens": 4603232, "outputTokens": 66333,
                                        "cacheReadTokens": 4476250, "cacheWriteTokens": 126823}}}
        for data in ({"accountingSnapshot": {"tokenDetails": td, "modelMetrics": mm}},
                     {"accountingSnapshot": {"tokenDetails": td}}):
            run = {}
            server._process_copilot_event({"type": "session.usage_checkpoint", "data": data}, run, lambda text: None)
            self.assertEqual(run["usage"]["input_tokens"], 159 + 126823)
            self.assertEqual(run["usage"]["cache_read_tokens"], 4476250)

    def test_legacy_copilot_worker_usage_normalizes_without_rewriting(self):
        root = os.path.join(self.temp_dir, "subway")
        workers_dir = os.path.join(root, "workers")
        tracks_dir = os.path.join(root, "tracks")
        os.makedirs(workers_dir)
        created_at = server._dt.datetime.now().astimezone().isoformat()
        old_record = {
            "id": "worker_abcdef", "provider": "copilot", "status": "completed",
            "created_at": created_at,
            "usage": {"input": 1500, "output": 50, "cache_read": 1200},
        }
        new_record = {
            "id": "worker_123456", "provider": "copilot", "status": "completed",
            "created_at": created_at,
            "usage": {"input": 400, "output": 20, "cache_read": 300},
            "input_excludes_cache": True,
        }
        old_path = os.path.join(workers_dir, "worker_abcdef.json")
        new_path = os.path.join(workers_dir, "worker_123456.json")
        with open(old_path, "w", encoding="utf-8") as f:
            json.dump(old_record, f)
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(new_record, f)
        with open(old_path, "rb") as f:
            old_bytes = f.read()

        with patch.object(server, "SUBWAY_DIR", root), \
             patch.object(server, "WORKERS_DIR", workers_dir), \
             patch.object(server, "TRACKS_DIR", tracks_dir), \
             patch.object(server, "_copilot_cli_usage", return_value=None):
            records = {w["id"]: w for w in server._all_workers()}
            self.assertEqual(records["worker_abcdef"]["usage"]["input"], 300)
            self.assertEqual(records["worker_123456"]["usage"]["input"], 400)
            self.assertTrue(records["worker_abcdef"]["input_excludes_cache"])
            again = {w["id"]: w for w in server._all_workers()}
            self.assertEqual(again["worker_abcdef"]["usage"]["input"], 300)
            self.assertEqual(server._provider_5h_tokens("copilot"), 770)

        with open(old_path, "rb") as f:
            self.assertEqual(f.read(), old_bytes)

    def test_new_copilot_usage_record_is_marked(self):
        meta = {"provider": "copilot", "usage": {"input": 0, "output": 0, "cache_read": 0}}
        server._add_usage(meta, {
            "input_tokens": 40, "output_tokens": 5, "cache_read_tokens": 60,
            "credits": 0.25, "requests": 1,
        })
        self.assertTrue(meta["input_excludes_cache"])
        self.assertEqual(meta["usage"]["input"], 40)
        self.assertEqual(meta["usage"]["cache_read"], 60)
        self.assertEqual(meta["usage"]["output"], 5)
        self.assertEqual(meta["usage"]["credits"], 0.25)
        self.assertEqual(meta["usage"]["requests"], 1)

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


    def test_copilot_cli_refused_recorded_and_retried_with_auto(self):
        worker_id = "worker_cptest1"
        log_file = os.path.join(self.temp_dir, f"{worker_id}.log")
        meta = {
            "id": worker_id,
            "provider": "copilot",
            "model": "gpt-4.1",
            "cmd": ["copilot", "-p", "hi", "--model", "gpt-4.1", "--output-format", "json"],
            "log_file": log_file,
            "status": "running",
            "timeout_seconds": 60,
        }
        with open(log_file, "w") as f:
            f.write("")

        first_proc = MagicMock()
        first_proc.poll.return_value = 1
        first_proc.wait.return_value = 1
        first_proc.stdout = [b'Error: Model "gpt-4.1" from --model flag is not available.\n']

        second_proc = MagicMock()
        second_proc.poll.return_value = 0
        second_proc.wait.return_value = 0
        second_proc.stdout = [b'{"type":"assistant.message","data":{"content":"done"}}\n']

        spawned_cmds = []

        def fake_popen(cmd, **kwargs):
            spawned_cmds.append(list(cmd))
            if len(spawned_cmds) == 1:
                return first_proc
            return second_proc

        with patch.object(server, "_load_worker", return_value=meta), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "_write_json_atomic"), \
             patch.object(server, "_load_json", return_value={"refused": {}, "accepted": {}}), \
             patch.object(server, "_save_json") as mock_save, \
             patch("subprocess.Popen", side_effect=fake_popen):
            server.supervise_worker(worker_id)

        self.assertEqual(len(spawned_cmds), 2)
        self.assertIn("gpt-4.1", spawned_cmds[0])
        self.assertIn("auto", spawned_cmds[1])
        # Saved refused store
        saved_stores = [args[1] for args, _ in mock_save.call_args_list if args[0] == server.COPILOT_CLI_MODELS_FILE]
        self.assertTrue(any("gpt-4.1" in s.get("refused", {}) for s in saved_stores))

    def test_copilot_cli_accepted_recorded(self):
        worker_id = "worker_cptest2"
        log_file = os.path.join(self.temp_dir, f"{worker_id}.log")
        meta = {
            "id": worker_id,
            "provider": "copilot",
            "model": "gpt-6-luna",
            "cmd": ["copilot", "-p", "hi", "--model", "gpt-6-luna", "--output-format", "json"],
            "log_file": log_file,
            "status": "running",
            "timeout_seconds": 60,
        }
        with open(log_file, "w") as f:
            f.write("")

        proc = MagicMock()
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.stdout = [b'{"type":"assistant.message","data":{"content":"hello"}}\n']

        with patch.object(server, "_load_worker", return_value=meta), \
             patch.object(server, "_save_worker"), \
             patch.object(server, "_write_json_atomic"), \
             patch.object(server, "_load_json", return_value={"refused": {}, "accepted": {}}), \
             patch.object(server, "_save_json") as mock_save, \
             patch("subprocess.Popen", return_value=proc):
            server.supervise_worker(worker_id)

        saved_stores = [args[1] for args, _ in mock_save.call_args_list if args[0] == server.COPILOT_CLI_MODELS_FILE]
        self.assertTrue(any("gpt-6-luna" in s.get("accepted", {}) for s in saved_stores))

    def test_copilot_cli_expiry_after_7_days(self):
        now = time.time()
        store = {
            "refused": {
                "old-refused": now - (8 * 86400),
                "recent-refused": now - (2 * 86400),
            },
            "accepted": {}
        }
        with patch.object(server, "_load_copilot_cli_models", return_value=store):
            self.assertFalse(server.is_copilot_cli_refused("old-refused"))
            self.assertTrue(server.is_copilot_cli_refused("recent-refused"))

    def test_copilot_routing_excludes_refused_ids(self):
        provs = [{"id": "copilot", "client": {"dispatchable": True}}]
        cp_models = [{"model": "refused-mod", "coding": 4, "reasoning": 4, "writing": 4},
                     {"model": "good-mod", "coding": 3, "reasoning": 3, "writing": 3}]
        with patch.object(server, "_copilot_models", return_value=cp_models), \
             patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"), \
             patch.object(server, "_copilot_raw", return_value={"quota_snapshots": {"chat": {"remaining": 100, "entitlement": 100, "unlimited": True}}}):
            server._model_catalog.clear()
            route = server.route_task("write a python script", providers=provs)
            picks = [p["model"] for p in route.get("picks", []) if p["provider"] == "copilot"]
            self.assertNotIn("refused-mod", picks)

    def test_copilot_catalog_marks_cli_false(self):
        cp_models = [{"model": "refused-mod"}, {"model": "accepted-mod"}]
        with patch.object(server, "_copilot_models", return_value=cp_models), \
             patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"):
            server._model_catalog.clear()
            catalog = server.model_catalog()
            refused_entries = [m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "refused-mod"]
            accepted_entries = [m for m in catalog if m.get("provider") == "copilot" and m.get("model") == "accepted-mod"]
            self.assertTrue(len(refused_entries) > 0)
            self.assertIs(refused_entries[0].get("cli"), False)
            self.assertTrue(len(accepted_entries) > 0)
            self.assertNotEqual(accepted_entries[0].get("cli"), False)

    def test_copilot_review_step_falls_back_to_auto(self):
        with patch.object(server, "is_copilot_cli_refused", side_effect=lambda m: m == "refused-mod"):
            step_refused = server._review_trigger_step("copilot/refused-mod")
            self.assertEqual(step_refused["provider"], "copilot")
            self.assertEqual(step_refused["model"], "auto")

            step_accepted = server._review_trigger_step("copilot/good-mod")
            self.assertEqual(step_accepted["provider"], "copilot")
            self.assertEqual(step_accepted["model"], "good-mod")

            then_steps = server._parse_trigger_steps([
                {"role": "Tester", "prompt": "test", "provider": "copilot", "model": "refused-mod"}
            ])
            self.assertEqual(then_steps[0]["provider"], "copilot")
            self.assertEqual(then_steps[0]["model"], "auto")


if __name__ == "__main__":
    unittest.main()
