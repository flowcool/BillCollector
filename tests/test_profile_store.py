import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from profile_store import prepare_profile
import BillCollectorServices_pw as services


class ProfileStoreTests(unittest.TestCase):
    def test_reuses_one_accounts_state_across_runs_without_touching_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            first = Path(prepare_profile(root, "Portal account-A"))
            other = Path(prepare_profile(root, "Portal account-B"))
            self.assertNotEqual(first, other)
            self.assertEqual(first.parent, root)
            self.assertNotIn("account-A", first.name)

            # Simulate Chromium writing session data during the first run.
            (first / "Cookies").write_text("session-survives", encoding="utf-8")
            prefs = first / "Default" / "Preferences"
            prefs.write_text('{"existing": "keep me"}', encoding="utf-8")

            restarted = Path(prepare_profile(root, "Portal account-A"))
            self.assertEqual(restarted, first)
            self.assertEqual((restarted / "Cookies").read_text(encoding="utf-8"),
                             "session-survives")
            self.assertEqual(prefs.read_text(encoding="utf-8"), '{"existing": "keep me"}')
            self.assertFalse((other / "Cookies").exists())

    def test_new_directories_and_pdf_preference_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            profile = Path(prepare_profile(root, "Portal account-A"))
            default = profile / "Default"
            prefs = default / "Preferences"

            for path in (root, profile, default):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(prefs.stat().st_mode), 0o600)
            self.assertEqual(json.loads(prefs.read_text(encoding="utf-8")),
                             {"plugins": {"always_open_pdf_externally": True}})

    def test_rejects_empty_identity_and_symlinked_profile_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            with self.assertRaises(ValueError):
                prepare_profile(root, "  ")
            with self.assertRaises(ValueError):
                prepare_profile("", "Portal account-A")
            real = Path(tmp) / "real"
            real.mkdir()
            root.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                prepare_profile(root, "Portal account-A")

    def test_refuses_public_root_without_changing_its_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            root.mkdir(mode=0o755)
            with self.assertRaises(PermissionError):
                prepare_profile(root, "Portal account-A")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)

    def test_rejects_symlinked_account_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            first = Path(prepare_profile(root, "Portal account-A"))
            first.rename(Path(tmp) / "moved-profile")
            first.symlink_to(Path(tmp) / "moved-profile", target_is_directory=True)
            with self.assertRaises(OSError):
                prepare_profile(root, "Portal account-A")

    def test_mocked_browser_reopens_same_profile_without_clearing_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            browser = Mock()
            playwright = SimpleNamespace(chromium=Mock(launch_persistent_context=Mock(return_value=browser)))
            account = SimpleNamespace(account_id="Portal account-A", dbg=False)

            with patch.object(services, "CHROMIUM_PLAYWRIGHT_PROFILE", str(root)):
                self.assertIs(services.InitBrowser(playwright, account), browser)
                profile = Path(playwright.chromium.launch_persistent_context.call_args.kwargs["user_data_dir"])
                (profile / "Cookies").write_text("session-survives", encoding="utf-8")
                self.assertIs(services.InitBrowser(playwright, account), browser)

            calls = playwright.chromium.launch_persistent_context.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["user_data_dir"], calls[1].kwargs["user_data_dir"])
            self.assertEqual((profile / "Cookies").read_text(encoding="utf-8"), "session-survives")

    def test_empty_recipe_run_does_not_clear_context_cookies(self):
        browser = Mock()
        playwright = SimpleNamespace(chromium=Mock())
        browser.new_page.return_value.context.clear_cookies = Mock()
        playwright_context = Mock()
        playwright_context.__enter__ = Mock(return_value=playwright)
        playwright_context.__exit__ = Mock(return_value=False)
        account = SimpleNamespace(yml={"services": []}, dbg=False, account_id="Portal account-A")

        with patch.object(services, "sync_playwright", return_value=playwright_context), \
             patch.object(services, "InitBrowser", return_value=browser):
            self.assertEqual(services.perform_actions(account), [])

        browser.new_page.return_value.context.clear_cookies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
