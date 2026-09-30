"""Bounded loader for bundled and externally mounted Playwright recipes.

External recipes are data, not Python plugins.  The runtime only reads from the
configured directory; deployments should mount that directory read-only.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import ValidationError, validate

from .BillCollectorHelpers import (
    RECIPES_PLAYWRIGHT_DIR,
    RECIPES_PLAYWRIGHT_PREFIX,
    RECIPES_PLAYWRIGHT_SCHEMA_FILE,
)


RECIPE_DIR_ENV = "BILLCOLLECTOR_RECIPES_DIR"
FORMAT_VERSION = 1
CAPABILITIES = {"browser", "download"}
METHOD_ARGUMENTS = {
    "goto": ({"url": str}, {}),
    "locator": ({"selector": str}, {}),
    "get_by_label": ({"text": str}, {"exact": bool}),
    "get_by_placeholder": ({"text": str}, {"exact": bool}),
    "get_by_text": ({"text": str}, {"exact": bool}),
    "get_by_title": ({"text": str}, {"exact": bool}),
    "get_by_role": ({"role": str}, {"name": str, "exact": bool}),
    "get_by_test_id": ({"test_id": str}, {}),
    "click": ({}, {}),
    "fill": ({"value": str}, {}),
    "press": ({"value": str}, {}),
    "first": ({}, {}),
    "content_frame": ({}, {}),
    "expect_download": ({}, {}),
    "close": ({}, {}),
}


class RecipeContractError(ValueError):
    """A recipe is unavailable or outside the supported execution contract."""


def _validate_steps(steps, location, capabilities):
    if not isinstance(steps, list) or not steps:
        raise RecipeContractError(f"{location}: steps must be a non-empty list")
    for index, step in enumerate(steps):
        where = f"{location}.steps[{index}]"
        if not isinstance(step, dict):
            raise RecipeContractError(f"{where}: expected a mapping")
        methods = step.get("methods")
        if not isinstance(methods, list) or not methods:
            raise RecipeContractError(f"{where}: methods must be a non-empty list")
        for method_index, entry in enumerate(methods):
            method_where = f"{where}.methods[{method_index}]"
            if not isinstance(entry, dict):
                raise RecipeContractError(f"{method_where}: expected a mapping")
            method = entry.get("method")
            if not isinstance(method, str) or method not in METHOD_ARGUMENTS:
                raise RecipeContractError(f"{method_where}: unsupported method {method!r}")
            if method == "expect_download" and "download" not in capabilities:
                raise RecipeContractError(f"{method_where}: download capability is required")
            raw_arguments = entry.get("arguments", [])
            if not isinstance(raw_arguments, list):
                raise RecipeContractError(f"{method_where}: arguments must be a list")
            arguments = {}
            for argument in raw_arguments:
                if not isinstance(argument, dict) or len(argument) != 1:
                    raise RecipeContractError(f"{method_where}: each argument must be a single-key mapping")
                key, value = next(iter(argument.items()))
                if key in arguments:
                    raise RecipeContractError(f"{method_where}: duplicate argument {key!r}")
                arguments[key] = value
            required, optional = METHOD_ARGUMENTS[method]
            if not required.keys() <= arguments.keys() or arguments.keys() - required.keys() - optional.keys():
                raise RecipeContractError(f"{method_where}: invalid arguments for {method}")
            for key, value in arguments.items():
                expected_type = (required | optional)[key]
                if not isinstance(value, expected_type) or (expected_type is str and not value):
                    raise RecipeContractError(f"{method_where}: invalid {key!r} value")
            if method == "goto":
                try:
                    url = urlsplit(arguments["url"])
                    if url.scheme not in ("http", "https") or not url.hostname:
                        raise ValueError("missing HTTP(S) host")
                except ValueError as exc:
                    raise RecipeContractError(f"{method_where}: goto requires an HTTP(S) URL") from exc
        nested = step.get("steps")
        expects_download = any(entry["method"] == "expect_download" for entry in methods)
        if expects_download:
            _validate_steps(nested, where, capabilities)
        elif nested is not None:
            raise RecipeContractError(f"{where}: nested steps require expect_download")


def validate_recipe_contract(recipe, service_name, external=False):
    """Validate one selected recipe before any browser work."""
    if not isinstance(recipe, dict):
        raise RecipeContractError("recipe must be a mapping")
    if external:
        if type(recipe.get("formatVersion")) is not int or recipe["formatVersion"] != FORMAT_VERSION:
            raise RecipeContractError("external recipe requires formatVersion: 1")
        capabilities = recipe.get("capabilities")
        if (not isinstance(capabilities, list) or not capabilities
                or any(not isinstance(item, str) or item not in CAPABILITIES for item in capabilities)
                or len(set(capabilities)) != len(capabilities) or "browser" not in capabilities):
            raise RecipeContractError("external recipe needs unique supported capabilities including browser")
    else:
        # Old bundled recipes predate metadata. Their current behavior includes downloads.
        capabilities = {"browser", "download"}
    services = recipe.get("services")
    if (not isinstance(services, list) or len(services) != 1
            or not isinstance(services[0], dict)
            or services[0].get("serviceName") != service_name):
        raise RecipeContractError("recipe must contain exactly the requested service")
    _validate_steps(services[0].get("steps"), "services[0]", capabilities)
    return recipe


def load_playwright_recipe(service_name, recipe_dir=None):
    """Load from a configured external directory, or the legacy bundled directory.

    Supplying an external directory is authoritative: missing or invalid recipes
    never silently fall back to bundled ones.
    """
    normalized = service_name.lower().replace(" ", "_")
    if not re.fullmatch(r"[a-z0-9_]+", normalized):
        raise RecipeContractError("service name is not a safe recipe filename")
    external_dir = recipe_dir if recipe_dir is not None else os.environ.get(RECIPE_DIR_ENV)
    external = external_dir is not None
    directory = Path(external_dir if external else RECIPES_PLAYWRIGHT_DIR)
    recipe_path = directory / f"{RECIPES_PLAYWRIGHT_PREFIX}{normalized}.yaml"
    if recipe_path.is_symlink():
        raise RecipeContractError("recipe symlinks are not supported")
    try:
        if recipe_path.stat().st_size > 1_000_000:
            raise RecipeContractError("recipe exceeds the 1 MB limit")
        with recipe_path.open(encoding="utf-8") as stream:
            recipe = yaml.safe_load(stream)
        with open(RECIPES_PLAYWRIGHT_SCHEMA_FILE, encoding="utf-8") as stream:
            schema = yaml.safe_load(stream)
        validate(instance=recipe, schema=schema)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise RecipeContractError(f"recipe schema violation at {location} ({exc.validator})") from exc
    except yaml.YAMLError as exc:
        raise RecipeContractError(f"invalid YAML recipe {recipe_path}") from exc
    except OSError as exc:
        raise RecipeContractError(f"cannot read recipe {recipe_path}: {exc.strerror}") from exc
    return validate_recipe_contract(recipe, normalized, external=external)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate a Playwright recipe without opening a browser")
    parser.add_argument("service", help="Service name matching recipe-pw__<service>.yaml")
    parser.add_argument("--dir", dest="recipe_dir", help="External recipe directory")
    args = parser.parse_args()
    try:
        load_playwright_recipe(args.service, recipe_dir=args.recipe_dir)
    except RecipeContractError as exc:
        print(f"Invalid recipe: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Valid recipe: {args.service}")
