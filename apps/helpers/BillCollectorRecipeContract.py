"""Bounded loader for bundled and externally mounted Playwright recipes.

External recipes are data, not Python plugins.  The runtime only reads from the
configured directory; deployments should mount that directory read-only.
"""

import argparse
import hashlib
import json
import os
import re
import stat
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
EXTERNAL_ORIGINS_ENV = "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS"
APPROVALS_FILE_ENV = "BILLCOLLECTOR_RECIPE_APPROVALS_FILE"
FORMAT_VERSION = 1
CAPABILITIES = {"browser", "download"}
SECRET_PLACEHOLDERS = {"{{USERNAME}}", "{{PASSWORD}}", "{{OTP}}"}
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
    "press": ({"key": str}, {}),
    "first": ({}, {}),
    "content_frame": ({}, {}),
    "expect_download": ({}, {}),
    "download_all": ({}, {"timeout_ms": int}),
    "close": ({}, {}),
}
LOCATOR_METHODS = {
    "locator", "get_by_label", "get_by_placeholder", "get_by_text",
    "get_by_title", "get_by_role", "get_by_test_id",
}
METHOD_RECEIVERS = {
    **{method: {"page", "locator", "frame_locator"} for method in LOCATOR_METHODS},
    "goto": {"page"},
    "expect_download": {"page"},
    "download_all": {"locator"},
    "close": {"page"},
    "content_frame": {"locator"},
    "first": {"locator"},
    "click": {"locator"},
    "fill": {"locator"},
    "press": {"locator"},
}


class RecipeContractError(ValueError):
    """A recipe is unavailable or outside the supported execution contract."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                None, None, "unhashable mapping key", key_node.start_mark
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate mapping key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def https_origin(url):
    """Return the exact HTTPS origin of a URL, or None if it is not usable."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    return f"https://{host}{f':{port}' if port not in (None, 443) else ''}"


def external_recipe_origins(value=None):
    """Require an operator-supplied, exact HTTPS origin allowlist."""
    raw = os.environ.get(EXTERNAL_ORIGINS_ENV) if value is None else value
    if not raw:
        raise RecipeContractError(f"external recipes require {EXTERNAL_ORIGINS_ENV}")
    origins = set()
    for item in raw.split(","):
        origin = item.strip()
        if not origin or https_origin(origin) != origin:
            raise RecipeContractError(f"{EXTERNAL_ORIGINS_ENV} needs exact HTTPS origins")
        origins.add(origin)
    return frozenset(origins)


def _validate_steps(steps, location, capabilities, allowed_origins=None, *, in_download=False):
    if not isinstance(steps, list) or not steps:
        raise RecipeContractError(f"{location}: steps must be a non-empty list")
    for index, step in enumerate(steps):
        where = f"{location}.steps[{index}]"
        if not isinstance(step, dict):
            raise RecipeContractError(f"{where}: expected a mapping")
        methods = step.get("methods")
        if not isinstance(methods, list) or not methods:
            raise RecipeContractError(f"{where}: methods must be a non-empty list")
        receiver = "page"
        in_frame = False
        for method_index, entry in enumerate(methods):
            method_where = f"{where}.methods[{method_index}]"
            if not isinstance(entry, dict):
                raise RecipeContractError(f"{method_where}: expected a mapping")
            method = entry.get("method")
            if not isinstance(method, str) or method not in METHOD_ARGUMENTS:
                raise RecipeContractError(f"{method_where}: unsupported method {method!r}")
            if receiver not in METHOD_RECEIVERS[method]:
                raise RecipeContractError(f"{method_where}: {method} is invalid on {receiver}")
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
                if key == "timeout_ms" and (type(value) is not int or not 100 <= value <= 120_000):
                    raise RecipeContractError(f"{method_where}: timeout_ms must be 100..120000")
                if (allowed_origins is not None and value in SECRET_PLACEHOLDERS
                        and (method != "fill" or key != "value")):
                    raise RecipeContractError(f"{method_where}: credentials are only allowed in fill(value)")
                if (allowed_origins is not None and value in SECRET_PLACEHOLDERS and in_frame):
                    raise RecipeContractError(f"{method_where}: credential fill inside a frame is unsupported")
            if method == "goto":
                try:
                    url = urlsplit(arguments["url"])
                    if url.scheme not in ("http", "https") or not url.hostname:
                        raise ValueError("missing HTTP(S) host")
                except ValueError as exc:
                    raise RecipeContractError(f"{method_where}: goto requires an HTTP(S) URL") from exc
                if allowed_origins is not None and https_origin(arguments["url"]) not in allowed_origins:
                    raise RecipeContractError(f"{method_where}: goto origin is not allowed")
            if method == "download_all" and "download" not in capabilities:
                raise RecipeContractError(f"{method_where}: download capability is required")
            if method == "download_all" and in_download:
                raise RecipeContractError(f"{method_where}: download_all cannot be nested under expect_download")
            if method in LOCATOR_METHODS:
                receiver = "locator"
            elif method == "content_frame":
                receiver = "frame_locator"
                in_frame = True
            elif method == "first":
                receiver = "locator"
            else:
                receiver = "done"
        nested = step.get("steps")
        expects_download = any(entry["method"] == "expect_download" for entry in methods)
        if expects_download:
            _validate_steps(nested, where, capabilities, allowed_origins, in_download=True)
        elif nested is not None:
            raise RecipeContractError(f"{where}: nested steps require expect_download")


def validate_recipe_contract(recipe, service_name, external=False, allowed_origins=None):
    """Validate one selected recipe before any browser work."""
    if not isinstance(recipe, dict):
        raise RecipeContractError("recipe must be a mapping")
    if external:
        if allowed_origins is None:
            allowed_origins = external_recipe_origins()
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
    _validate_steps(services[0].get("steps"), "services[0]", capabilities, allowed_origins)
    return recipe


def load_playwright_recipe(service_name, recipe_dir=None, *, allowed_origins=None, expected_sha256=None):
    """Load from a configured external directory, or the legacy bundled directory.

    Supplying an external directory is authoritative: missing or invalid recipes
    never silently fall back to bundled ones.
    """
    normalized = service_name.lower().replace(" ", "_")
    if not re.fullmatch(r"[a-z0-9_]+", normalized):
        raise RecipeContractError("service name is not a safe recipe filename")
    external_dir = recipe_dir if recipe_dir is not None else os.environ.get(RECIPE_DIR_ENV)
    external = external_dir is not None
    allowed_origins = (allowed_origins if allowed_origins is not None else
                       external_recipe_origins()) if external else None
    directory = Path(external_dir if external else RECIPES_PLAYWRIGHT_DIR)
    recipe_path = directory / f"{RECIPES_PLAYWRIGHT_PREFIX}{normalized}.yaml"
    if recipe_path.is_symlink():
        raise RecipeContractError("recipe symlinks are not supported")
    try:
        if recipe_path.stat().st_size > 1_000_000:
            raise RecipeContractError("recipe exceeds the 1 MB limit")
        raw_recipe = recipe_path.read_bytes()
        if expected_sha256 is not None and hashlib.sha256(raw_recipe).hexdigest() != expected_sha256:
            raise RecipeContractError("recipe bytes differ from operator-approved SHA-256")
        loader = _UniqueKeyLoader(raw_recipe)
        try:
            recipe = loader.get_single_data()
        finally:
            loader.dispose()
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
    return validate_recipe_contract(recipe, normalized, external=external, allowed_origins=allowed_origins)


def preflight_external_recipe(service_name, account_id):
    """Freeze an approved recipe and account-scoped origins before vault lookup."""
    recipe_dir = os.environ.get(RECIPE_DIR_ENV)
    approval_path = os.environ.get(APPROVALS_FILE_ENV)
    if not recipe_dir or not approval_path or not os.path.isabs(approval_path):
        raise RecipeContractError(f"external recipes require {APPROVALS_FILE_ENV}")
    path = Path(approval_path)
    if path.resolve().is_relative_to(Path(recipe_dir).resolve()):
        raise RecipeContractError("approval file must be outside the recipe directory")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                raise RecipeContractError("approval file must be a regular file under 1 MB")
            approvals = json.load(stream)
    except (OSError, ValueError) as exc:
        raise RecipeContractError("cannot read operator approval file") from exc
    if not isinstance(approvals, dict) or approvals.get("formatVersion") != 1:
        raise RecipeContractError("approval file has an unsupported format")
    accounts = approvals.get("accounts")
    entry = accounts.get(account_id) if isinstance(accounts, dict) else None
    normalized = service_name.lower().replace(" ", "_")
    if not isinstance(entry, dict) or entry.get("service") != normalized:
        raise RecipeContractError("service/account has no matching recipe approval")
    digest = entry.get("sha256")
    origins = entry.get("origins")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RecipeContractError("approval needs a SHA-256 recipe pin")
    if not isinstance(origins, list) or not origins or any(
        not isinstance(item, str) or https_origin(item) != item for item in origins
    ) or len(set(origins)) != len(origins):
        raise RecipeContractError("approval needs unique exact HTTPS origins")
    allowed = frozenset(origins)
    if not allowed <= external_recipe_origins():
        raise RecipeContractError("account origins exceed the deployment allowlist")
    recipe = load_playwright_recipe(normalized, allowed_origins=allowed,
                                    expected_sha256=digest)
    return recipe, allowed


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
