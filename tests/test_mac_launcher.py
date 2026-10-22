"""Mac entry-point errors must remain visible without changing source/workspace."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_macos


class MacLauncherTests(unittest.TestCase):
    def test_finder_launch_opens_random_port_and_logs_under_user_support(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(launch_macos.Path, "home", return_value=Path(folder)), \
                    patch.object(launch_macos.sys, "argv", ["Hearing Compiler"]), \
                    patch.object(launch_macos, "main") as entry, \
                    patch.object(launch_macos.subprocess, "run") as alert:
                launch_macos.run()
                entry.assert_called_once_with()
                self.assertEqual(launch_macos.sys.argv[1:], ["serve", "--port", "0", "--open"])
                self.assertTrue((Path(folder) / "Library/Application Support/HearingCompilerPrototype/launcher.log").is_file())
                alert.assert_not_called()

    def test_startup_failure_is_visible_and_preserves_written_diagnostics(self):
        def fail():
            print("Error: workspace already open", file=launch_macos.sys.stderr)
            raise SystemExit(1)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(launch_macos.Path, "home", return_value=Path(folder)), \
                    patch.object(launch_macos.sys, "argv", ["Hearing Compiler"]), \
                    patch.object(launch_macos, "main", side_effect=fail), \
                    patch.object(launch_macos.subprocess, "run") as alert:
                with self.assertRaises(SystemExit) as raised:
                    launch_macos.run()
                self.assertEqual(raised.exception.code, 1)
                arguments = alert.call_args.args[0]
                self.assertEqual(arguments[0], "/usr/bin/osascript")
                self.assertIn("launcher.log", arguments[-1])
                self.assertIn("workspace already open", (Path(folder) / "Library/Application Support/HearingCompilerPrototype/launcher.log").read_text())

    def test_explicit_cli_does_not_redirect_or_create_finder_state(self):
        with patch.object(launch_macos.sys, "argv", ["app", "serve", "--state", "scratch"]), \
                patch.object(launch_macos, "main") as entry, \
                patch.object(launch_macos.Path, "home") as home:
            launch_macos.run()
            entry.assert_called_once_with()
            home.assert_not_called()

    def test_unexpected_startup_failure_retains_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(launch_macos.Path, "home", return_value=Path(folder)), \
                    patch.object(launch_macos.sys, "argv", ["Hearing Compiler"]), \
                    patch.object(launch_macos, "main", side_effect=RuntimeError("Unexpected failure")), \
                    patch.object(launch_macos.subprocess, "run") as alert:
                with self.assertRaises(SystemExit):
                    launch_macos.run()
                log = (Path(folder) / "Library/Application Support/HearingCompilerPrototype/launcher.log").read_text()
                self.assertIn("Traceback", log)
                self.assertIn("RuntimeError: Unexpected failure", log)
                alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
