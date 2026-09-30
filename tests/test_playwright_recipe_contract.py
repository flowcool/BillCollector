import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from helpers.BillCollectorRecipeContract import (  # noqa: E402
    RecipeContractError,
    load_playwright_recipe,
    validate_recipe_contract,
)
from BillCollectorServices_pw import retrieve_from_service_with_playwright  # noqa: E402


VALID_RECIPE = {
    "$schema": "/nonexistent/author/machine/schema.yaml",
    "formatVersion": 1,
    "capabilities": ["browser", "download"],
    "services": [{
        "serviceName": "sample",
        "steps": [
            {"step": 1, "methods": [{"method": "goto", "arguments": [{"url": "https://example.test/"}]}]},
            {"step": 2, "methods": [
                {"method": "get_by_role", "arguments": [{"role": "link"}, {"name": "Invoice"}]},
                {"method": "click"},
            ]},
            {"step": 3, "methods": [{"method": "expect_download"}], "steps": [
                {"step": 4, "methods": [
                    {"method": "get_by_text", "arguments": [{"text": "PDF"}]},
                    {"method": "click"},
                ]},
            ]},
        ],
    }],
}


class RecipeContractTests(unittest.TestCase):
    def test_bundled_recipes_still_validate(self):
        directory = Path(__file__).resolve().parents[1] / "apps" / "recipes_playwright"
        names = [path.stem.removeprefix("recipe-pw__") for path in directory.glob("recipe-pw__*.yaml")]
        self.assertGreaterEqual(len(names), 7)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(load_playwright_recipe(name)["services"][0]["serviceName"], name)

    def test_external_recipe_loads_using_bundled_schema(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            path = Path(recipe_dir) / "recipe-pw__sample.yaml"
            path.write_text(yaml.safe_dump(VALID_RECIPE), encoding="utf-8")
            with patch.dict(os.environ, {"BILLCOLLECTOR_RECIPES_DIR": recipe_dir}):
                self.assertEqual(load_playwright_recipe("sample")["formatVersion"], 1)

    def test_external_directory_is_authoritative(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            with patch.dict(os.environ, {"BILLCOLLECTOR_RECIPES_DIR": recipe_dir}):
                with self.assertRaises(RecipeContractError):
                    load_playwright_recipe("winsim")

    def test_external_recipe_requires_version_and_capabilities(self):
        for field in ("formatVersion", "capabilities"):
            recipe = copy.deepcopy(VALID_RECIPE)
            del recipe[field]
            with self.subTest(field=field), self.assertRaises(RecipeContractError):
                validate_recipe_contract(recipe, "sample", external=True)

    def test_unknown_method_cannot_reach_dynamic_dispatch(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"][0]["method"] = "evaluate"
        with self.assertRaisesRegex(RecipeContractError, "unsupported method"):
            validate_recipe_contract(recipe, "sample", external=True)

    def test_unknown_argument_and_bad_url_are_rejected(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"][1]["arguments"] = [{"force": True}]
        with self.assertRaisesRegex(RecipeContractError, "invalid arguments"):
            validate_recipe_contract(recipe, "sample", external=True)
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][0]["methods"][0]["arguments"] = [{"url": "file:///etc/passwd"}]
        with self.assertRaisesRegex(RecipeContractError, "HTTP"):
            validate_recipe_contract(recipe, "sample", external=True)

    def test_download_capability_is_required_for_download_steps(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["capabilities"] = ["browser"]
        with self.assertRaisesRegex(RecipeContractError, "download capability"):
            validate_recipe_contract(recipe, "sample", external=True)

    def test_name_and_symlink_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            with self.assertRaisesRegex(RecipeContractError, "safe recipe filename"):
                load_playwright_recipe("../outside", recipe_dir=recipe_dir)
            outside = Path(recipe_dir) / "outside.yaml"
            outside.write_text(yaml.safe_dump(VALID_RECIPE), encoding="utf-8")
            (Path(recipe_dir) / "recipe-pw__sample.yaml").symlink_to(outside)
            with self.assertRaisesRegex(RecipeContractError, "symlinks"):
                load_playwright_recipe("sample", recipe_dir=recipe_dir)

    def test_invalid_external_recipe_stops_before_browser(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            recipe = copy.deepcopy(VALID_RECIPE)
            recipe["services"][0]["steps"][0]["methods"][0]["method"] = "evaluate"
            (Path(recipe_dir) / "recipe-pw__sample.yaml").write_text(
                yaml.safe_dump(recipe), encoding="utf-8"
            )
            with patch.dict(os.environ, {"BILLCOLLECTOR_RECIPES_DIR": recipe_dir}):
                with patch("BillCollectorServices_pw.perform_actions") as browser_work, \
                        patch("BillCollectorServices_pw.logger"):
                    self.assertFalse(retrieve_from_service_with_playwright(
                        "sample", "https://example.test", "example", "unused", None, False
                    ))
                    browser_work.assert_not_called()


if __name__ == "__main__":
    unittest.main()
