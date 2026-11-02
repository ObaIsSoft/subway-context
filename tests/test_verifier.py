#!/usr/bin/env python3
"""Tests for fail-closed worker verification (verify_worker and CLI)."""
import os as _os
_os.environ["SUBWAY_NO_NOTIFY"] = "1"  # Never send real desktop alerts from tests.
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import server
from server import verify_worker, run_cli


class TestWorkerVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="subway_verifier_test_")
        self.repo_dir = os.path.join(self.tmp_dir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        self.workers_dir = os.path.join(self.tmp_dir, "workers")
        os.makedirs(self.workers_dir, exist_ok=True)

        # Initialize test git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=self.repo_dir, check=True, capture_output=True)

        # Seed files
        server_py = (
            "def clip(s, n):\n"
            "    return s[:n] if len(s) > n else s\n"
        )
        with open(os.path.join(self.repo_dir, "server.py"), "w", encoding="utf-8") as f:
            f.write(server_py)

        tests_dir = os.path.join(self.repo_dir, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("")

        test_subway = (
            "import unittest\n"
            "import server\n\n"
            "class TestSubway(unittest.TestCase):\n"
            "    def test_clip(self):\n"
            "        self.assertEqual(server.clip('hello', 3), 'hel')\n"
        )
        with open(os.path.join(tests_dir, "test_subway.py"), "w", encoding="utf-8") as f:
            f.write(test_subway)

        test_budgets = (
            "import unittest\n\n"
            "class TestBudgets(unittest.TestCase):\n"
            "    def test_budget(self):\n"
            "        self.assertTrue(True)\n"
        )
        with open(os.path.join(tests_dir, "test_budgets.py"), "w", encoding="utf-8") as f:
            f.write(test_budgets)

        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_hud_import_missing_export_fails(self):
        patch = (
            "diff --git a/hud/views.js b/hud/views.js\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/hud/views.js\n"
            "@@ -0,0 +1,3 @@\n"
            "+export function existingView() {\n"
            "+    return 1;\n"
            "+}\n"
            "diff --git a/hud/app.js b/hud/app.js\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/hud/app.js\n"
            "@@ -0,0 +1,2 @@\n"
            "+import { missingView } from \"./views.js\";\n"
            "+missingView();\n"
        )
        meta = self._create_worker("w_bad_js", patch)
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("views.js doesn't export missingView" in str(r) for r in res.get("reasons", [])))

    def _create_worker(self, wid: str, patch_content: str, meta_extra: dict = None) -> dict:
        patch_file = os.path.join(self.workers_dir, f"{wid}.patch")
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_content)
        meta = {
            "id": wid,
            "repo": self.repo_dir,
            "base": "HEAD",
            "patch_path": patch_file,
            "test_suites": ["tests.test_subway", "tests.test_budgets"],
        }
        if meta_extra:
            meta.update(meta_extra)
        meta_file = os.path.join(self.workers_dir, f"{wid}.json")
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return meta

    def test_empty_patch_must_not_pass(self):
        meta = self._create_worker("w_empty", "")
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("empty patch", res["line"])

    def test_syntax_error_must_not_pass(self):
        patch = (
            "diff --git a/server.py b/server.py\n"
            "--- a/server.py\n"
            "+++ b/server.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def clip(s, n):\n"
            "+    def broken(:\n"
            "     return s[:n] if len(s) > n else s\n"
        )
        meta = self._create_worker("w_syntax", patch)
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("compilation error", res["line"])

    def test_failing_test_must_not_pass(self):
        patch = (
            "diff --git a/server.py b/server.py\n"
            "--- a/server.py\n"
            "+++ b/server.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def clip(s, n):\n"
            "-    return s[:n] if len(s) > n else s\n"
            "+    return 'wrong'\n"
        )
        meta = self._create_worker("w_fail_test", patch)
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("tests failed", res["line"])

    def test_wrong_file_must_not_pass(self):
        # Create other.py first in base commit
        other_path = os.path.join(self.repo_dir, "other.py")
        with open(other_path, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "add", "other.py"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add other.py"], cwd=self.repo_dir, check=True, capture_output=True)

        patch = (
            "diff --git a/other.py b/other.py\n"
            "--- a/other.py\n"
            "+++ b/other.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        meta = self._create_worker("w_wrong_file", patch, meta_extra={"files": ["server.py"]})
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FLAG")
        self.assertIn("outside brief", res["line"])

    def test_deleted_test_must_not_pass(self):
        patch = (
            "diff --git a/tests/test_budgets.py b/tests/test_budgets.py\n"
            "--- a/tests/test_budgets.py\n"
            "+++ b/tests/test_budgets.py\n"
            "@@ -3,3 +3,1 @@\n"
            " class TestBudgets(unittest.TestCase):\n"
            "-    def test_budget(self):\n"
            "-        self.assertTrue(True)\n"
            "+    pass\n"
        )
        meta = self._create_worker("w_del_test", patch)
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertIn(res["status"], ("FLAG", "FAIL"))  # A deleted test also lowers the count, which fails outright.

    def test_weakened_assert_must_not_pass(self):
        patch = (
            "diff --git a/tests/test_budgets.py b/tests/test_budgets.py\n"
            "--- a/tests/test_budgets.py\n"
            "+++ b/tests/test_budgets.py\n"
            "@@ -3,3 +3,3 @@\n"
            " class TestBudgets(unittest.TestCase):\n"
            "     def test_budget(self):\n"
            "-        self.assertTrue(True)\n"
            "+        x = 1\n"
        )
        meta = self._create_worker("w_weak_assert", patch)
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FLAG")
        self.assertIn("patch removes assert line", res["line"])

    def test_untested_change_flags(self):
        patch = (
            "diff --git a/server.py b/server.py\n"
            "--- a/server.py\n"
            "+++ b/server.py\n"
            "@@ -2,1 +2,4 @@\n"
            "     return s[:n] if len(s) > n else s\n"
            "+\n"
            "+def uncalled_helper():\n"
            "+    val = 42\n"
            "+    return val\n"
        )
        meta = self._create_worker("w_untested", patch, meta_extra={"files": ["server.py"]})
        res = verify_worker(meta)
        self.assertNotEqual(res["status"], "PASS")
        self.assertEqual(res["status"], "FLAG")
        self.assertIn("untested change", res["line"])

    def test_good_patch_must_pass(self):
        patch = (
            "diff --git a/server.py b/server.py\n"
            "--- a/server.py\n"
            "+++ b/server.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def clip(s, n):\n"
            "+    s = str(s)\n"
            "     return s[:n] if len(s) > n else s\n"
        )
        meta = self._create_worker("w_good", patch, meta_extra={"files": ["server.py"]})
        res = verify_worker(meta)
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["line"], "PASS")

    def test_cli_verify(self):
        patch = (
            "diff --git a/server.py b/server.py\n"
            "--- a/server.py\n"
            "+++ b/server.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def clip(s, n):\n"
            "+    s = str(s)\n"
            "     return s[:n] if len(s) > n else s\n"
        )
        meta = self._create_worker("w_cli", patch, meta_extra={"files": ["server.py"]})
        from unittest import mock
        with mock.patch.object(server, "WORKERS_DIR", self.workers_dir):
            ret = run_cli(["verify", meta["id"]])
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
