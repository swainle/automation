import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.screenshot.chrome import BrowserPreparationError, close_existing_chrome, prepare_default_profile
from automation.com.browser import launch_chrome


class PreparationTests(unittest.TestCase):
    def test_last_used_profile_backup_preserves_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Chrome"
            profile = root / "Profile 2"
            profile.mkdir(parents=True)
            (root / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 2"}}))
            original = {"profile": {"name": "fixture", "exit_type": "Crashed"}, "session": {"restore_on_startup": 1},
                        "extensions": {"fixture": "preserve"}, "safebrowsing": {"enabled": True}}
            raw = json.dumps(original).encode()
            (profile / "Preferences").write_bytes(raw)
            result = prepare_default_profile(Path(folder) / "backups", user_data=root)
            self.assertEqual(result["profile"], "Profile 2")
            self.assertEqual(result["backup"].read_bytes(), raw)
            after = json.loads((profile / "Preferences").read_text())
            self.assertEqual(after["extensions"], original["extensions"])
            self.assertEqual(after["safebrowsing"], {"enabled": True})
            self.assertEqual(after["profile"]["exit_type"], "Normal")
            self.assertTrue(after["profile"]["exited_cleanly"])
            self.assertFalse(after["profile"]["password_manager_enabled"])
            self.assertFalse(after["credentials_enable_service"])
            self.assertEqual(after["session"]["restore_on_startup"], 5)
            self.assertEqual(after["session"]["startup_urls"], [])

    def test_corrupt_preferences_remain_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Default").mkdir()
            target = root / "Default/Preferences"
            target.write_text("invalid json")
            with self.assertRaises(BrowserPreparationError):
                prepare_default_profile(root / "backups", user_data=root)
            self.assertEqual(target.read_text(), "invalid json")

    def test_profile_path_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Local State").write_text(json.dumps({"profile": {"last_used": "../other"}}))
            with self.assertRaises(BrowserPreparationError):
                prepare_default_profile(root / "backups", user_data=root)
            self.assertFalse((root / "backups").exists())

    def test_first_run_prepares_default_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Chrome"
            result = prepare_default_profile(Path(folder) / "backups", user_data=root)
            self.assertIsNone(result["backup"])
            self.assertTrue((root / "Default/Preferences").is_file())

    def test_only_chrome_processes_terminated_and_survivors_killed(self):
        chrome = Mock(info={"name": "chrome.exe"})
        edge = Mock(info={"name": "msedge.exe"})
        with patch("automation.pdd.screenshot.chrome.psutil.process_iter", side_effect=[[chrome, edge], [edge]]), patch(
            "automation.pdd.screenshot.chrome.psutil.wait_procs", side_effect=[([], [chrome]), ([chrome], [])]
        ):
            self.assertEqual(close_existing_chrome(), 1)
        chrome.terminate.assert_called_once()
        chrome.kill.assert_called_once()
        edge.terminate.assert_not_called()

    def test_unclosed_chrome_blocks_relaunch(self):
        chrome = Mock(info={"name": "chrome.exe"})
        with patch("automation.pdd.screenshot.chrome.psutil.process_iter", return_value=[chrome]), patch(
            "automation.pdd.screenshot.chrome.psutil.wait_procs", return_value=([], [chrome])
        ):
            with self.assertRaises(BrowserPreparationError):
                close_existing_chrome()

    def test_popup_switches_only_added_when_requested_without_user_directory(self):
        with patch("automation.com.browser.chrome_executable", return_value="chrome.exe"), patch(
            "automation.com.browser.subprocess.Popen"
        ) as process:
            launch_chrome(url="about:blank", quiet_startup=True)
            command = process.call_args.args[0]
            self.assertIn("--no-first-run", command)
            self.assertIn("--no-default-browser-check", command)
            self.assertIn("--hide-crash-restore-bubble", command)
            self.assertFalse(any(item.startswith("--user-data-dir") for item in command))
