import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml


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
    download_all_webelements,
    perform_actions,
    perform__switch_to_default_frame,
    perform__switch_to_parent_frame,
    webElementObj,
)


class RecipeValidationTests(unittest.TestCase):
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

    @patch("BillCollectorServices.time.sleep", return_value=None)
    def test_parameterless_frame_switch_actions(self, _sleep):
        browser = SimpleNamespace(dbg=False, drv=MagicMock())

        perform__switch_to_parent_frame(browser, None)
        perform__switch_to_default_frame(browser, None)

        browser.drv.switch_to.parent_frame.assert_called_once_with()
        browser.drv.switch_to.default_content.assert_called_once_with()

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


if __name__ == "__main__":
    unittest.main()
