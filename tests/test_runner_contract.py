import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import BillCollector
import BillCollectorServices_pw as runner


class RunnerContractTests(unittest.TestCase):
    def test_run_tables_reject_untrusted_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            db = runner.DatabaseManager(Path(directory) / "runs.sqlite3")
            try:
                with self.assertRaisesRegex(ValueError, "safe database identifier"):
                    db.create_service_run_table("demo;DROP TABLE Service")
                with self.assertRaisesRegex(ValueError, "safe database identifier"):
                    db.insert_page_status("PageStatus_demo_Run1;DROP", SimpleNamespace())
            finally:
                db.close_connection()

    def test_real_publisher_is_used_for_two_account_runs(self):
        pdf = b"%PDF-1.4\n%%EOF\n"

        class FakeDownload:
            body = pdf

            def failure(self):
                return None

            def path(self):
                temporary = Path(directory) / "browser-download"
                temporary.write_bytes(self.body)
                return str(temporary)

            def save_as(self, path):
                Path(path).write_bytes(self.body)

            @property
            def suggested_filename(self):
                raise AssertionError("Untrusted filename must not be read")

        browser = MagicMock()
        page = browser.new_page.return_value
        page.expect_download.return_value.__enter__.return_value.value = FakeDownload()
        outer = {"step": 1, "steps": [{"step": 2}]}
        recipe = {"services": [{"serviceName": "demo", "steps": [outer]}]}

        def step_state(_bcs, step):
            action = '{"action":"expect_download"}' if step is outer else "{}"
            return SimpleNamespace(service_name="demo", step_number=step["step"],
                                   locator_action=action, interactive_elements=[], error_status=None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"BILLCOLLECTOR_PUBLICATION_ROOT": str(root / "publication")}), \
                 patch.object(runner, "DB_FILE", str(root / "runs.sqlite3")), \
                 patch.object(runner, "sync_playwright"), \
                 patch.object(runner, "locked_profile", return_value=nullcontext(str(root / "profile"))), \
                 patch.object(runner, "InitBrowser", return_value=browser), \
                 patch.object(runner, "process_step", side_effect=step_state):
                first = runner.perform_actions(SimpleNamespace(yml=recipe, service="demo",
                    account_id="demo alice", db=None, page=None, drv=None))
                second = runner.perform_actions(SimpleNamespace(yml=recipe, service="demo",
                    account_id="demo alice", db=None, page=None, drv=None))
                broken = FakeDownload()
                broken.body = b"<html>not an invoice</html>"
                page.expect_download.return_value.__enter__.return_value.value = broken
                with self.assertRaisesRegex(RuntimeError, "Download step failed"):
                    runner.perform_actions(SimpleNamespace(yml=recipe, service="demo",
                        account_id="demo alice", db=None, page=None, drv=None))
            self.assertEqual(first, [{"result": "published"}])
            self.assertEqual(second, [{"result": "duplicate"}])
            self.assertEqual(len(list((root / "publication" / "output").glob("*.pdf"))), 1)
            self.assertEqual(list((root / "publication" / "staging").iterdir()), [])
            connection = sqlite3.connect(root / "runs.sqlite3")
            try:
                self.assertEqual(connection.execute(
                    "SELECT result FROM Service ORDER BY id DESC LIMIT 1").fetchone()[0], "failure")
            finally:
                connection.close()

    def test_page_state_does_not_persist_dom_or_recipe_secrets(self):
        secret = "SENTINEL_PRIVATE_VALUE"
        page = MagicMock()
        page.evaluate.side_effect = AssertionError("DOM inspection must be disabled")
        bcs = SimpleNamespace(service="demo", page=page, usr="user", pwd=secret, otp=None)
        step = {"step": 1, "description": secret, "methods": [
            {"method": "get_by_role", "arguments": [{"role": "textbox", "name": secret}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]}
        page.get_by_role.return_value = MagicMock()
        state = runner.process_step(bcs, step)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "runs.sqlite3"
            db = runner.DatabaseManager(db_path)
            table = db.create_service_run_table("demo")
            db.insert_page_status(table, state)
            db.finalize_service_run("demo", table, [{"url": secret}], "success")
            db.close_connection()
            connection = sqlite3.connect(db_path)
            try:
                stored = "\n".join(connection.iterdump())
            finally:
                connection.close()
        self.assertNotIn(secret, stored)
        self.assertNotIn("{{PASSWORD}}", stored)
        page.evaluate.assert_not_called()

    def test_browser_launch_uses_persistent_context(self):
        browser = object()
        playwright = SimpleNamespace(chromium=MagicMock())
        playwright.chromium.launch_persistent_context.return_value = browser
        self.assertIs(runner.InitBrowser(playwright, SimpleNamespace(dbg=False), "/tmp/mock-profile"), browser)
        self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"] is False)

    def test_bad_method_marks_step_failed_without_echoing_exception(self):
        bcs = SimpleNamespace(service="demo", page=SimpleNamespace(), usr="user", pwd="secret", otp=None)
        state = runner.process_step(bcs, {"step": 1, "methods": [{"method": "missing", "arguments": []}]})
        self.assertTrue(state.error_status)
        self.assertNotIn("secret", str(state.error_status))

    def test_failed_step_fails_run_and_closes_resources(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": [{"step": 1}]}]},
                              usr="user", account_id="demo user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright") as pw, \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
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
                              usr="user", account_id="demo user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
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
        bcs = SimpleNamespace(yml={"services": []}, usr="user", account_id="demo user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright", return_value=PlaywrightContext()), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
             patch.object(runner, "InitBrowser", return_value=browser):
            self.assertEqual(runner.perform_actions(bcs), [])
        self.assertEqual(lifecycle, ["start", "page closed", "browser closed", "stop"])

    def test_unexpected_step_exception_finalizes_failure(self):
        db = MagicMock()
        page = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        bcs = SimpleNamespace(yml={"services": [{"serviceName": "demo", "steps": [{"step": 1}]}]},
                              usr="user", account_id="demo user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
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
                              usr="user", account_id="demo user", db=None, page=None, drv=None)
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
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

    def test_no_matching_service_returns_nonzero_cli_status(self):
        cases = (
            ("[Playwright]\ndemo = alice\n", "missing"),
            ("[Playwright]\n", None),
        )
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "services.ini"
            config = BillCollector.defs("vault.local", "http://vault.local", fname=str(ini))
            for contents, service in cases:
                with self.subTest(service=service):
                    ini.write_text(contents, encoding="utf-8")
                    with patch.object(BillCollector, "is_domain_local_ip", return_value="127.0.0.1"), \
                         patch.object(BillCollector, "bitwarden_api_check_status", return_value=(True, "unlocked")), \
                         patch.object(BillCollector, "post_json", return_value='{"success":true}'), \
                         patch.object(BillCollector, "is_json_property_value", return_value=True), \
                         patch.object(BillCollector, "get_json") as vault_lookup, \
                         patch.object(BillCollector, "retrieve_from_service_with_playwright") as browser_run:
                        self.assertEqual(BillCollector.playwright_exit_code(config, service), 1)
                        vault_lookup.assert_not_called()
                        browser_run.assert_not_called()

    def test_profile_startup_failure_reaches_cli_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = os.path.join(directory, "services.ini")
            Path(ini).write_text("[Playwright]\ndemo = first\n", encoding="utf-8")
            config = BillCollector.defs("vault.local", "http://vault.local", fname=ini)
            with patch.object(BillCollector, "is_domain_local_ip", return_value="127.0.0.1"), \
                 patch.object(BillCollector, "bitwarden_api_check_status", return_value=(True, "unlocked")), \
                 patch.object(BillCollector, "post_json", return_value='{"success":true}'), \
                 patch.object(BillCollector, "is_json_property_value", return_value=True), \
                 patch.object(BillCollector, "get_json", return_value='{"data":{}}'), \
                 patch.object(BillCollector, "get_json_property_value", return_value="placeholder"), \
                 patch.object(BillCollector, "retrieve_from_service_with_playwright",
                              side_effect=runner.retrieve_from_service_with_playwright), \
                 patch.object(runner, "load_playwright_recipe", return_value={"services": []}), \
                 patch.object(runner, "sync_playwright"), \
                 patch.object(runner, "locked_profile", side_effect=PermissionError("private profile")):
                self.assertFalse(BillCollector.WebRetriDoc(config, "playwright"))

    def test_runtime_credential_origin_rejection_fails_service(self):
        page = MagicMock()
        page.url = "https://evil.test/login"
        page.get_by_role.return_value = MagicMock()
        browser = MagicMock()
        browser.new_page.return_value = page
        step = {"step": 1, "methods": [
            {"method": "get_by_role", "arguments": [{"role": "textbox"}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]}
        bcs = SimpleNamespace(
            yml={"services": [{"serviceName": "demo", "steps": [step]}]},
            service="demo", usr="user", pwd="secret", otp=None, dbg=False,
            account_id="demo user", db=None, page=None, drv=None,
            external_recipe=True, allowed_recipe_origins=frozenset({"https://good.test"}),
        )
        db = MagicMock()
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(MagicMock())), \
             patch.object(runner, "InitBrowser", return_value=browser), \
             patch.object(runner, "DatabaseManager", return_value=db):
            with self.assertRaisesRegex(RuntimeError, "Playwright service run failed"):
                runner.perform_actions(bcs)
        page.get_by_role.return_value.fill.assert_not_called()
        self.assertEqual(db.finalize_service_run.call_args.args[-1], "failure")

    def test_vault_response_is_not_logged(self):
        secret = "SECRET_FROM_RESPONSE"
        with patch.object(BillCollector, "get_json", return_value='{"success":true,"private":"' + secret + '"}'), \
             patch.object(BillCollector, "is_json_property_value", return_value=True):
            with self.assertLogs(BillCollector.logger, level="DEBUG") as captured:
                BillCollector.logger.debug("status check started")
                BillCollector.bitwarden_api_check_status("http://unused")
        self.assertNotIn(secret, "\n".join(captured.output))


    def test_external_run_limits_publication_to_approved_origins(self):
        publisher = MagicMock()
        browser = MagicMock()
        bcs = SimpleNamespace(yml={"services": []}, usr="user", account_id="demo user", db=None,
                              page=None, drv=None, external_recipe=True,
                              allowed_recipe_origins=frozenset({"https://good.test"}))
        with patch.object(runner, "sync_playwright"), \
             patch.object(runner, "locked_profile", return_value=nullcontext("/tmp/mock-profile")), \
             patch.object(runner, "publication_context", return_value=nullcontext(publisher)), \
             patch.object(runner, "InitBrowser", return_value=browser):
            self.assertEqual(runner.perform_actions(bcs), [])
        self.assertTrue(publisher.accept_url("https://good.test/a.pdf"))
        self.assertTrue(publisher.accept_url("blob:https://good.test/0b1c"))
        self.assertFalse(publisher.accept_url("https://evil.test/a.pdf"))
        self.assertFalse(publisher.accept_url("data:application/pdf;base64,JVBERi0="))


if __name__ == "__main__":
    unittest.main()
