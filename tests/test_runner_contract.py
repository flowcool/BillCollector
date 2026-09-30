import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import BillCollector
import BillCollectorServices_pw as runner


class RunnerContractTests(unittest.TestCase):
    def test_browser_launch_uses_persistent_context(self):
        browser = object()
        playwright = SimpleNamespace(chromium=MagicMock())
        playwright.chromium.launch_persistent_context.return_value = browser
        with patch.object(runner, "init_browser_profile", return_value=True):
            self.assertIs(runner.InitBrowser(playwright, SimpleNamespace(dbg=False)), browser)
        self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"] is False)

    def test_bad_method_marks_step_failed_without_echoing_exception(self):
        bcs = SimpleNamespace(service="demo", page=SimpleNamespace(), usr="user", pwd="secret", otp=None)
        with patch.object(runner.PageState, "set_interactive_elements", return_value=[]):
            state = runner.process_step(bcs, {"step": 1, "methods": [{"method": "missing", "arguments": []}]})
        self.assertTrue(state.error_status)
        self.assertNotIn("secret", str(state.error_status))

    def test_failed_step_fails_run_and_closes_resources(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": [{"step": 1}]}]},
                              usr="user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright") as pw, \
             patch.object(runner, "InitBrowser", return_value=browser), \
             patch.object(runner, "DatabaseManager", return_value=db), \
             patch.object(runner, "process_step", return_value=SimpleNamespace(error_status={"error": "failed"})):
            with self.assertRaisesRegex(RuntimeError, "Step 1 failed"):
                runner.perform_actions(bcs)
        db.close_connection.assert_called_once()
        self.assertEqual(db.finalize_service_run.call_args.args[-1], "failure")
        page.close.assert_called_once()
        browser.close.assert_called_once()

    def test_no_download_is_not_a_runner_error(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": []}]},
                              usr="user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "InitBrowser", return_value=browser), \
             patch.object(runner, "DatabaseManager", return_value=db):
            self.assertEqual(runner.perform_actions(bcs), [])
        self.assertEqual(db.finalize_service_run.call_args.args[-1], "success")

    def test_resources_close_before_playwright_context_exits(self):
        lifecycle = []

        class PlaywrightContext:
            def __enter__(self):
                lifecycle.append("start")
                return object()

            def __exit__(self, *_):
                lifecycle.append("stop")

        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        page.close.side_effect = lambda: lifecycle.append("page closed")
        browser.close.side_effect = lambda: lifecycle.append("browser closed")
        bcs = SimpleNamespace(yml={"services": []}, usr="user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright", return_value=PlaywrightContext()), \
             patch.object(runner, "InitBrowser", return_value=browser):
            self.assertEqual(runner.perform_actions(bcs), [])
        self.assertEqual(lifecycle, ["start", "page closed", "browser closed", "stop"])

    def test_unexpected_step_exception_finalizes_failure(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": [{"step": 1}]}]},
                              usr="user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "InitBrowser", return_value=browser), \
             patch.object(runner, "DatabaseManager", return_value=db), \
             patch.object(runner, "process_step", side_effect=ValueError("private data")):
            with self.assertRaisesRegex(RuntimeError, "Playwright service run failed"):
                runner.perform_actions(bcs)
        self.assertEqual(db.finalize_service_run.call_args.args[-1], "failure")
        db.close_connection.assert_called_once()

    def test_cleanup_failure_does_not_mask_step_failure(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        page.close.side_effect = RuntimeError("cleanup failed")
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": [{"step": 1}]}]},
                              usr="user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "InitBrowser", return_value=browser), \
             patch.object(runner, "DatabaseManager", return_value=db), \
             patch.object(runner, "process_step", side_effect=ValueError("private data")):
            with self.assertRaisesRegex(RuntimeError, "Playwright service run failed"):
                runner.perform_actions(bcs)
        browser.close.assert_called_once()

    def test_service_failure_is_returned_to_cli_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = os.path.join(directory, "services.ini")
            Path(ini).write_text("[Playwright]\ndemo = first, second\n", encoding="utf-8")
            config = BillCollector.defs("vault.local", "http://vault.local", fname=ini)
            with patch.object(BillCollector, "is_domain_local_ip", return_value="127.0.0.1"), \
                 patch.object(BillCollector, "bitwarden_api_check_status", return_value=(True, "unlocked")), \
                 patch.object(BillCollector, "post_json", return_value='{"success":true}'), \
                 patch.object(BillCollector, "is_json_property_value", return_value=True), \
                 patch.object(BillCollector, "get_json", return_value='{"data":{}}'), \
                 patch.object(BillCollector, "get_json_property_value", return_value="placeholder"), \
                 patch.object(BillCollector, "retrieve_from_service_with_playwright", side_effect=[True, False]) as retrieve:
                self.assertFalse(BillCollector.WebRetriDoc(config, "playwright"))
            self.assertEqual(retrieve.call_count, 2)

    def test_vault_response_is_not_logged(self):
        secret = "SECRET_FROM_RESPONSE"
        with patch.object(BillCollector, "get_json", return_value='{"success":true,"private":"' + secret + '"}'), \
             patch.object(BillCollector, "is_json_property_value", return_value=True):
            with self.assertLogs(BillCollector.logger, level="DEBUG") as captured:
                BillCollector.logger.debug("status check started")
                BillCollector.bitwarden_api_check_status("http://unused")
        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
