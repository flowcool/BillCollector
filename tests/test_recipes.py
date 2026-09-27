import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from selenium.common.exceptions import TimeoutException


ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"
sys.path.insert(0, str(APPS))

from BillCollectorRecipes import (  # noqa: E402
    CheckRecipe,
    CheckRecipeMetadata,
    is_yaml_file,
)
from BillCollectorServices import (  # noqa: E402
    ACTION_MAP,
    click_until_absent_webelement,
    download_all_webelements,
    download_webelement,
    perform_actions,
    perform__navigate,
    perform__switch_to_default_frame,
    perform__switch_to_parent_frame,
    wait_for_new_download,
    webElementObj,
)


class PaginationElement:
    def __init__(self, driver):
        self.driver = driver

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.driver.enabled

    def click(self):
        self.driver.clicks += 1
        if not self.driver.stalled:
            self.driver.remaining_pages -= 1
            self.driver.loaded_items += 1


class PaginationResultElement:
    def __init__(self, displayed=True):
        self.displayed = displayed

    def is_displayed(self):
        return self.displayed


class PaginationDriver:
    def __init__(
            self, remaining_pages, stalled=False, enabled=True,
            total_items=None):
        self.remaining_pages = remaining_pages
        self.stalled = stalled
        self.enabled = enabled
        self.loaded_items = 1
        self.total_items = total_items
        self.clicks = 0
        self.scrolled_elements = []
        self.button = PaginationElement(self)

    def execute_script(self, script, element):
        self.assert_scroll_script(script)
        self.scrolled_elements.append(element)

    @staticmethod
    def assert_scroll_script(script):
        expected = ("scrollIntoView", "behavior: 'instant'", "block: 'center'")
        if any(fragment not in script for fragment in expected):
            raise AssertionError(f"Unexpected script: {script}")

    def find_elements(self, _locator, element):
        if element == "button.load-more":
            return [self.button] if self.remaining_pages > 0 else []
        if element == "a.invoice":
            visible = [PaginationResultElement()] * self.loaded_items
            hidden_count = max(
                0, (self.total_items or self.loaded_items) - self.loaded_items)
            return visible + [PaginationResultElement(False)] * hidden_count
        raise AssertionError(f"Unexpected selector: {element}")


class RecipeValidationTests(unittest.TestCase):
    def pagination_element(self, driver, max_clicks=20):
        element = webElementObj(timeout=1, max_clicks=max_clicks)
        element.selectors = [
            webElementObj.selectorObj("css selector", "button.load-more"),
            webElementObj.selectorObj("css selector", "a.invoice"),
        ]
        return SimpleNamespace(drv=driver, dbg=False), element

    @staticmethod
    def immediate_wait(wait):
        def until(predicate):
            result = predicate(wait.call_args.args[0])
            if result:
                return result
            raise TimeoutException()

        wait.return_value.until.side_effect = until

    def test_all_bundled_recipes_match_the_schema(self):
        recipes = sorted((APPS / "bc-recipes").glob("bc-recipe__*.yaml"))
        self.assertTrue(recipes, "No bundled recipes were found")

        for recipe in recipes:
            with self.subTest(recipe=recipe.name):
                self.assertIsNotNone(CheckRecipe(recipe))

    def test_every_schema_action_has_a_runtime_handler(self):
        schema_path = APPS / "bc-recipes" / "bc-recipe-schema.yaml"
        with schema_path.open(encoding="utf-8") as stream:
            schema = yaml.safe_load(stream)

        action_types = set(
            schema["properties"]["services"]["items"]["properties"]["actions"]
            ["items"]["properties"]["actionType"]["enum"]
        )
        self.assertEqual(action_types, set(ACTION_MAP))

    def test_free_recipe_metadata_is_compatible(self):
        metadata = CheckRecipeMetadata(
            APPS / "bc-recipes" / "bc-metadata__free.yaml",
            "free",
            ACTION_MAP,
        )

        self.assertEqual(metadata["recipeFormatVersion"], 1)

    def test_newer_recipe_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "bc-metadata__free.yaml"
            metadata.write_text(
                "service: free\n"
                "recipeVersion: 1.0.0\n"
                "recipeFormatVersion: 999\n"
                "requiredActions: [Click]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                    RuntimeError, "requires recipe format 999"):
                CheckRecipeMetadata(
                    metadata,
                    "free",
                    ACTION_MAP,
                    APPS / "bc-recipes" / "bc-metadata-schema.yaml",
                )

    def test_unsupported_recipe_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "bc-metadata__free.yaml"
            metadata.write_text(
                "service: free\n"
                "recipeVersion: 1.0.0\n"
                "recipeFormatVersion: 1\n"
                "requiredActions: [FutureAction]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                    RuntimeError, "unsupported actions: FutureAction"):
                CheckRecipeMetadata(
                    metadata,
                    "free",
                    ACTION_MAP,
                    APPS / "bc-recipes" / "bc-metadata-schema.yaml",
                )

    def test_invalid_yaml_returns_a_validation_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            malformed = Path(temp_dir) / "malformed.yaml"
            malformed.write_text("services: [\n", encoding="utf-8")
            with malformed.open(encoding="utf-8") as stream:
                valid, parsed = is_yaml_file(stream)

        self.assertFalse(valid)
        self.assertIsNone(parsed)

    def test_click_until_absent_schema_requires_two_locators(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recipe = Path(temp_dir) / "invalid-pagination.yaml"
            recipe.write_text(
                "services:\n"
                "  - serviceName: test\n"
                "    actions:\n"
                "      - step: 1\n"
                "        actionType: ClickUntilAbsent\n"
                "        parameters:\n"
                "          locators:\n"
                "            - locatorType: ID\n"
                "              element: load-more\n",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertIsNone(CheckRecipe(recipe))

    @patch("BillCollectorServices.time.sleep", return_value=None)
    def test_parameterless_frame_switch_actions(self, _sleep):
        browser = SimpleNamespace(dbg=False, drv=MagicMock())

        perform__switch_to_parent_frame(browser, None)
        perform__switch_to_default_frame(browser, None)

        browser.drv.switch_to.parent_frame.assert_called_once_with()
        browser.drv.switch_to.default_content.assert_called_once_with()

    def test_navigate_opens_an_absolute_https_url(self):
        browser = SimpleNamespace(drv=MagicMock())

        perform__navigate(
            browser,
            {"url": "https://example.test/invoices"},
        )

        browser.drv.get.assert_called_once_with(
            "https://example.test/invoices")

    def test_navigate_rejects_non_https_urls(self):
        browser = SimpleNamespace(drv=MagicMock())

        with self.assertRaisesRegex(ValueError, "absolute HTTPS URL"):
            perform__navigate(browser, {"url": "http://example.test"})

    def test_click_until_absent_succeeds_when_control_is_initially_absent(self):
        driver = PaginationDriver(remaining_pages=0)
        browser, element = self.pagination_element(driver)

        self.assertTrue(click_until_absent_webelement(browser, element))
        self.assertEqual(driver.clicks, 0)

    @patch("BillCollectorServices.WebDriverWait")
    def test_click_until_absent_loads_pages_until_control_disappears(self, wait):
        driver = PaginationDriver(remaining_pages=2, total_items=3)
        browser, element = self.pagination_element(driver)
        self.immediate_wait(wait)

        self.assertTrue(click_until_absent_webelement(browser, element))
        self.assertEqual(driver.clicks, 2)
        self.assertEqual(driver.loaded_items, 3)
        self.assertEqual(driver.scrolled_elements, [driver.button] * 2)

    @patch("BillCollectorServices.WebDriverWait")
    def test_click_until_absent_rejects_stalled_progress(self, wait):
        driver = PaginationDriver(remaining_pages=1, stalled=True)
        browser, element = self.pagination_element(driver)
        self.immediate_wait(wait)

        with self.assertRaisesRegex(
                RuntimeError, "did not load additional elements"):
            click_until_absent_webelement(browser, element)

    @patch("BillCollectorServices.WebDriverWait")
    def test_click_until_absent_does_not_treat_disabled_as_absent(self, wait):
        driver = PaginationDriver(remaining_pages=1, enabled=False)
        browser, element = self.pagination_element(driver)
        self.immediate_wait(wait)

        with self.assertRaisesRegex(RuntimeError, "did not become actionable"):
            click_until_absent_webelement(browser, element)
        self.assertEqual(driver.clicks, 0)

    @patch("BillCollectorServices.WebDriverWait")
    def test_click_until_absent_rejects_exhausted_click_limit(self, wait):
        driver = PaginationDriver(remaining_pages=2)
        browser, element = self.pagination_element(driver, max_clicks=1)
        self.immediate_wait(wait)

        with self.assertRaisesRegex(RuntimeError, "after 1 clicks"):
            click_until_absent_webelement(browser, element)

    @patch(
        "BillCollectorServices.wait_for_new_download",
        side_effect=[["invoice-1.pdf"], ["invoice-2.pdf"]],
    )
    @patch("BillCollectorServices.os.listdir", return_value=[])
    def test_download_all_clicks_every_matching_element(
            self, _listdir, wait_for_download):
        first = MagicMock()
        second = MagicMock()
        first.get_attribute.return_value = "https://example.test/1.pdf"
        second.get_attribute.return_value = "https://example.test/2.pdf"
        driver = MagicMock()
        driver.current_window_handle = "main"
        driver.current_url = "https://example.test/invoices"
        driver.window_handles = ["main"]
        driver.find_elements.return_value = [first, second]
        browser = SimpleNamespace(drv=driver, dld="/downloads")
        element = webElementObj(timeout=10)
        element.selectors = [
            webElementObj.selectorObj("css selector", "a.invoice")
        ]

        with patch("BillCollectorServices.WebDriverWait") as wait:
            wait.return_value.until.return_value = [first, second]
            downloaded = download_all_webelements(browser, element)

        self.assertEqual(downloaded, ["invoice-1.pdf", "invoice-2.pdf"])
        driver.execute_script.assert_any_call(
            "window.open(arguments[0], '_blank');",
            "https://example.test/1.pdf")
        driver.execute_script.assert_any_call(
            "window.open(arguments[0], '_blank');",
            "https://example.test/2.pdf")
        self.assertEqual(driver.execute_script.call_count, 2)
        self.assertEqual(wait_for_download.call_count, 2)

    @patch(
        "BillCollectorServices.wait_for_new_download",
        return_value=["invoice.pdf"],
    )
    @patch("BillCollectorServices.os.listdir", return_value=[])
    @patch("BillCollectorServices.click_webelement")
    def test_single_download_publishes_through_persistent_state(
            self, click, _listdir, wait_for_download):
        state = MagicMock()
        state.publish.return_value = "invoice.pdf"
        link = MagicMock()
        link.get_attribute.return_value = "https://example.test/invoice.pdf"
        driver = MagicMock()
        driver.find_element.return_value = link
        browser = SimpleNamespace(
            drv=driver,
            dld="/downloads",
            state=state,
            output_dir="/output",
        )
        element = webElementObj(timeout=10)
        element.selectors = [
            webElementObj.selectorObj("css selector", "a.invoice")
        ]

        downloaded = download_webelement(browser, element)

        self.assertEqual(downloaded, "invoice.pdf")
        click.assert_called_once_with(browser, element)
        wait_for_download.assert_called_once_with("/downloads", set(), 10)
        state.publish.assert_called_once_with(
            "https://example.test/invoice.pdf",
            "/downloads/invoice.pdf",
            "/output",
        )

    @patch(
        "BillCollectorServices.wait_for_new_download",
        return_value=["invoice.pdf"],
    )
    @patch("BillCollectorServices.os.listdir", return_value=[])
    @patch("BillCollectorServices.click_webelement")
    def test_single_download_skips_known_document_after_download(
            self, _click, _listdir, _wait_for_download):
        state = MagicMock()
        state.publish.return_value = None
        link = MagicMock()
        link.get_attribute.return_value = None
        driver = MagicMock()
        driver.current_url = "https://example.test/invoices"
        driver.find_element.return_value = link
        browser = SimpleNamespace(
            drv=driver,
            dld="/downloads",
            state=state,
            output_dir="/output",
        )
        element = webElementObj(timeout=10)
        element.selectors = [
            webElementObj.selectorObj("css selector", "button.download")
        ]

        self.assertIsNone(download_webelement(browser, element))
        state.publish.assert_called_once_with(
            "https://example.test/invoices",
            "/downloads/invoice.pdf",
            "/output",
        )

    def test_action_failure_propagates(self):
        browser = SimpleNamespace(
            usr="subscriber",
            yml={
                "services": [{
                    "serviceName": "free",
                    "actions": [{
                        "step": 1,
                        "actionType": "Click",
                        "parameters": {},
                    }],
                }]
            },
        )

        with patch(
                "BillCollectorServices.perform__click",
                side_effect=RuntimeError("login failed")):
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                perform_actions(browser)

    def test_actions_do_not_log_subscriber_identifier(self):
        subscriber = "sensitive-subscriber-id"
        browser = SimpleNamespace(
            usr=subscriber,
            yml={"services": [{"serviceName": "free", "actions": []}]},
        )
        output = StringIO()

        with redirect_stdout(output):
            perform_actions(browser)

        self.assertNotIn(subscriber, output.getvalue())

    @patch(
        "BillCollectorServices.os.listdir",
        side_effect=[
            [],
            [".org.chromium.temporary"],
            [".org.chromium.temporary", "invoice.pdf.crdownload"],
            ["invoice.pdf"],
        ],
    )
    @patch("BillCollectorServices.time.sleep", return_value=None)
    @patch(
        "BillCollectorServices.time.monotonic",
        side_effect=[0, 0.1, 0.2, 0.3, 0.4],
    )
    def test_wait_ignores_chrome_temporary_files(
            self, _monotonic, _sleep, _listdir):
        self.assertEqual(
            wait_for_new_download("/downloads", set(), 10),
            ["invoice.pdf"])


if __name__ == "__main__":
    unittest.main()
