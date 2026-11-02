import os
import unittest
from unittest.mock import patch, MagicMock
import install
import server


class TestEditorExtension(unittest.TestCase):
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_extension_already_installed(self, mock_run, mock_which):
        def fake_which(cmd):
            return "/usr/local/bin/code" if cmd == "code" else None
        mock_which.side_effect = fake_which

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "other.extension\nsubway.subway-context\n"
        mock_run.return_value = mock_proc

        res = install.ensure_editor_extension(interactive=False, auto_yes=False)
        self.assertEqual(res, ["installed"])
        mock_run.assert_called_once_with(
            ["/usr/local/bin/code", "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("builtins.input", return_value="y")
    @patch("os.path.exists")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_extension_ask_interactive(self, mock_run, mock_which, mock_exists, mock_input):
        mock_which.side_effect = lambda cmd: "/usr/local/bin/code" if cmd == "code" else None
        mock_exists.side_effect = lambda p: p.endswith("subway-context.vsix")

        list_proc = MagicMock()
        list_proc.returncode = 0
        list_proc.stdout = ""

        install_proc = MagicMock()
        install_proc.returncode = 0

        mock_run.side_effect = [list_proc, install_proc]

        res = install.ensure_editor_extension(interactive=True, auto_yes=False)
        self.assertEqual(res, ["installed-now"])
        mock_input.assert_called_once()
        self.assertEqual(mock_run.call_count, 2)
        install_args = mock_run.call_args_list[1][0][0]
        self.assertEqual(install_args[0], "/usr/local/bin/code")
        self.assertEqual(install_args[1], "--install-extension")
        self.assertTrue(install_args[2].endswith("subway-context.vsix"))

    @patch("os.path.exists")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_extension_auto_yes(self, mock_run, mock_which, mock_exists):
        mock_which.side_effect = lambda cmd: "/usr/local/bin/code" if cmd == "code" else None
        mock_exists.side_effect = lambda p: p.endswith("subway-context.vsix")

        list_proc = MagicMock()
        list_proc.returncode = 0
        list_proc.stdout = ""

        install_proc = MagicMock()
        install_proc.returncode = 0

        mock_run.side_effect = [list_proc, install_proc]

        res = install.ensure_editor_extension(interactive=False, auto_yes=True)
        self.assertEqual(res, ["installed-now"])
        self.assertEqual(mock_run.call_count, 2)
        install_args = mock_run.call_args_list[1][0][0]
        self.assertEqual(install_args[0], "/usr/local/bin/code")
        self.assertEqual(install_args[1], "--install-extension")

    @patch("os.path.exists")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_extension_missing_vsix(self, mock_run, mock_which, mock_exists):
        mock_which.side_effect = lambda cmd: "/usr/local/bin/code" if cmd == "code" else None
        mock_exists.return_value = False

        list_proc = MagicMock()
        list_proc.returncode = 0
        list_proc.stdout = ""
        mock_run.return_value = list_proc

        res = install.ensure_editor_extension(interactive=True, auto_yes=True)
        self.assertEqual(res, ["missing-vsix"])
        self.assertEqual(mock_run.call_count, 1)

if __name__ == "__main__":
    unittest.main()
