import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from helpers.BillCollectorRecipeContract import (  # noqa: E402
    RecipeContractError,
    external_recipe_origins,
    load_playwright_recipe,
    validate_recipe_contract,
)
from BillCollectorServices_pw import process_step, retrieve_from_service_with_playwright  # noqa: E402


ALLOWED_ORIGINS = frozenset({"https://example.test"})


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
        buhl = load_playwright_recipe("buhl")
        press = buhl["services"][0]["steps"][4]["methods"][-1]
        self.assertEqual(press, {"method": "press", "arguments": [{"key": "Tab"}]})

    def test_external_recipe_loads_using_bundled_schema(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            path = Path(recipe_dir) / "recipe-pw__sample.yaml"
            path.write_text(yaml.safe_dump(VALID_RECIPE), encoding="utf-8")
            with patch.dict(os.environ, {
                "BILLCOLLECTOR_RECIPES_DIR": recipe_dir,
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            }):
                self.assertEqual(load_playwright_recipe("sample")["formatVersion"], 1)

    def test_external_directory_is_authoritative(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            with patch.dict(os.environ, {
                "BILLCOLLECTOR_RECIPES_DIR": recipe_dir,
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            }):
                with self.assertRaises(RecipeContractError):
                    load_playwright_recipe("winsim")

    def test_external_recipe_requires_version_and_capabilities(self):
        for field in ("formatVersion", "capabilities"):
            recipe = copy.deepcopy(VALID_RECIPE)
            del recipe[field]
            with self.subTest(field=field), self.assertRaises(RecipeContractError):
                validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_unknown_method_cannot_reach_dynamic_dispatch(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"][0]["method"] = "evaluate"
        with self.assertRaisesRegex(RecipeContractError, "unsupported method"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_unknown_argument_and_bad_url_are_rejected(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"][1]["arguments"] = [{"force": True}]
        with self.assertRaisesRegex(RecipeContractError, "invalid arguments"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][0]["methods"][0]["arguments"] = [{"url": "file:///etc/passwd"}]
        with self.assertRaisesRegex(RecipeContractError, "HTTP"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_download_capability_is_required_for_download_steps(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["capabilities"] = ["browser"]
        with self.assertRaisesRegex(RecipeContractError, "download capability"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_receiver_and_press_arguments_match_playwright(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"] = [
            {"method": "fill", "arguments": [{"value": "x"}]}
        ]
        with self.assertRaisesRegex(RecipeContractError, "fill is invalid on page"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)
        recipe["services"][0]["steps"][1]["methods"] = [
            {"method": "get_by_role", "arguments": [{"role": "textbox"}]},
            {"method": "press", "arguments": [{"value": "Tab"}]},
        ]
        with self.assertRaisesRegex(RecipeContractError, "invalid arguments for press"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_press_uses_playwright_key_keyword_at_runtime(self):
        locator = Mock()
        page = Mock(url="https://example.test/login")
        page.get_by_role.return_value = locator
        bcs = SimpleNamespace(service="sample", page=page, usr="user", pwd="secret", otp=None,
                              external_recipe=False)
        step = {"step": 1, "methods": [
            {"method": "get_by_role", "arguments": [{"role": "textbox"}]},
            {"method": "press", "arguments": [{"key": "Tab"}]},
        ]}
        with patch("BillCollectorServices_pw.PageState"):
            process_step(bcs, step)
        locator.press.assert_called_once_with(key="Tab")

    def test_external_recipe_requires_reviewed_https_origins(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RecipeContractError, "require"):
                external_recipe_origins()
        for origin in ("http://example.test", "https://example.test/path", "https://user@example.test"):
            with self.subTest(origin=origin), self.assertRaisesRegex(RecipeContractError, "exact HTTPS"):
                external_recipe_origins(origin)
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][0]["methods"][0]["arguments"] = [
            {"url": "https://evil.test/login"}
        ]
        with self.assertRaisesRegex(RecipeContractError, "origin is not allowed"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_external_placeholders_only_allowed_in_fill(self):
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"][0]["arguments"] = [
            {"role": "link"}, {"name": "{{PASSWORD}}"}
        ]
        with self.assertRaisesRegex(RecipeContractError, "only allowed in fill"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)
        recipe = copy.deepcopy(VALID_RECIPE)
        recipe["services"][0]["steps"][1]["methods"] = [
            {"method": "locator", "arguments": [{"selector": "iframe"}]},
            {"method": "content_frame"},
            {"method": "get_by_role", "arguments": [{"role": "textbox"}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]
        with self.assertRaisesRegex(RecipeContractError, "inside a frame"):
            validate_recipe_contract(recipe, "sample", external=True, allowed_origins=ALLOWED_ORIGINS)

    def test_external_credential_fill_checks_live_page_origin(self):
        locator = Mock()
        page = Mock(url="https://evil.test/login")
        page.get_by_role.return_value = locator
        bcs = SimpleNamespace(service="sample", page=page, usr="user", pwd="secret", otp=None,
                              external_recipe=True, allowed_recipe_origins=ALLOWED_ORIGINS)
        step = {"step": 1, "methods": [
            {"method": "get_by_role", "arguments": [{"role": "textbox"}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]}
        with patch("BillCollectorServices_pw.PageState"):
            with self.assertRaisesRegex(RecipeContractError, "credential fill blocked"):
                process_step(bcs, step)
            locator.fill.assert_not_called()
            page.url = "https://example.test/login"
            process_step(bcs, step)
            locator.fill.assert_called_once_with(value="secret")

    def test_name_and_symlink_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            with self.assertRaisesRegex(RecipeContractError, "safe recipe filename"):
                load_playwright_recipe("../outside", recipe_dir=recipe_dir)
            outside = Path(recipe_dir) / "outside.yaml"
            outside.write_text(yaml.safe_dump(VALID_RECIPE), encoding="utf-8")
            (Path(recipe_dir) / "recipe-pw__sample.yaml").symlink_to(outside)
            with patch.dict(os.environ, {"BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test"}):
                with self.assertRaisesRegex(RecipeContractError, "symlinks"):
                    load_playwright_recipe("sample", recipe_dir=recipe_dir)

    def test_invalid_external_recipe_stops_before_browser(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            recipe = copy.deepcopy(VALID_RECIPE)
            recipe["services"][0]["steps"][0]["methods"][0]["method"] = "evaluate"
            (Path(recipe_dir) / "recipe-pw__sample.yaml").write_text(
                yaml.safe_dump(recipe), encoding="utf-8"
            )
            with patch.dict(os.environ, {
                "BILLCOLLECTOR_RECIPES_DIR": recipe_dir,
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            }):
                with patch("BillCollectorServices_pw.perform_actions") as browser_work, \
                        patch("BillCollectorServices_pw.logger"):
                    self.assertFalse(retrieve_from_service_with_playwright(
                        "sample", "https://example.test", "example", "unused", None, False,
                        account_id="sample example",
                    ))
                    browser_work.assert_not_called()

    def test_missing_external_origin_policy_stops_before_browser(self):
        with tempfile.TemporaryDirectory() as recipe_dir:
            (Path(recipe_dir) / "recipe-pw__sample.yaml").write_text(
                yaml.safe_dump(VALID_RECIPE), encoding="utf-8"
            )
            with patch.dict(os.environ, {"BILLCOLLECTOR_RECIPES_DIR": recipe_dir}, clear=True):
                with patch("BillCollectorServices_pw.perform_actions") as browser_work, \
                        patch("BillCollectorServices_pw.logger"):
                    self.assertFalse(retrieve_from_service_with_playwright(
                        "sample", "https://example.test", "example", "unused", None, False,
                        account_id="sample example",
                    ))
                    browser_work.assert_not_called()


if __name__ == "__main__":
    unittest.main()
