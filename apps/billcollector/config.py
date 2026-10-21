"""Runtime configuration for the BillCollector daemon.

Central place for filesystem paths, ``.env`` loading, and default settings.
The DB ``Settings`` table (see :mod:`billcollector.db`) overrides the defaults
at runtime. Paths can be redirected with ``BC_*`` environment variables, which
keeps the test suite from touching the production ``apps/`` data.
"""

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

# The package lives at apps/billcollector/, so the app root is the parent dir.
APP_DIR = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    """Invalid operator configuration; messages never contain secret values."""


def load_env() -> None:
    """Load the application .env before resolving paths; exports take priority."""
    from dotenv import load_dotenv

    env_file = APP_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


load_env()


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default)))


# Keep legacy paths. Containers group state by explicitly setting these inputs.
DATA_DIR = _path("BC_DATA_DIR", APP_DIR / "data")
DB_DIR = _path("BC_DB_DIR", APP_DIR / "db")
DB_FILE = _path("BC_DB_FILE", DB_DIR / "bc.db")
LOG_DIR = _path("BC_LOG_DIR", DATA_DIR / "logs")
DISPLAY_DIR = DATA_DIR / "displays"
RUN_DIR = DATA_DIR / "runs"

# Browser + downloads. Per-task profiles are never auto-deleted (decision 9).
BROWSER_DIR = _path("BC_BROWSER_DIR", APP_DIR / "browser")
PROFILE_ROOT = _path("BC_PROFILE_ROOT", BROWSER_DIR / "profiles")
DOWNLOAD_DIR = _path("BC_DOWNLOAD_DIR", APP_DIR / "Downloads")

# Recipes stay as git-versioned YAML on disk (decision 7).
RECIPE_DIR = _path("BC_RECIPE_DIR", APP_DIR / "recipes_playwright")
# The image keeps its trusted schema outside operator-writable recipe mounts.
# Host installs retain the versioned source schema, also when BC_RECIPE_DIR moves.
_IMAGE_SCHEMA = Path("/opt/billcollector/recipe-pw-schema.yaml")
RECIPE_SCHEMA = (_IMAGE_SCHEMA if _IMAGE_SCHEMA.is_file()
                 else APP_DIR / "recipes_playwright" / "recipe-pw-schema.yaml")
RECIPE_PREFIX = "recipe-pw__"

# INI files imported into Tasks on first daemon start (decision 7 / M1 item 2).
INI_FILES = (APP_DIR / "bc_default.ini", APP_DIR / "bc_test.ini")

# Point Playwright at the repo-local browser build before it is imported.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSER_DIR))

# Defaults. Runtime-overridable via the Settings table (db.get_setting).
# Values are strings to match the TEXT Settings table; parse at the call site.
DEFAULT_SETTINGS = {
    "pause_deadline_seconds": "1800",    # 30 min hold before a pause aborts
    "max_concurrency": os.environ.get("BC_MAX_CONCURRENCY", "1"),
    "display_start": "99",               # first Xvfb display (:99)
    "display_count": os.environ.get("BC_DISPLAY_COUNT", "16"),
    "git_commit_enabled": "false",       # auto-commit recipe edits made in the UI
    "ui_base_url": os.environ.get("BC_UI_BASE_URL", "http://127.0.0.1:8000"),
    "ntfy_topic": "billcollector",       # ntfy push topic for pause deep links
    "download_dir": str(DOWNLOAD_DIR),
    "vnc_password": "",                  # empty: noVNC is localhost-only, no auth
}


def ensure_dirs() -> None:
    """Create the runtime directories the daemon writes to."""
    for d in (DATA_DIR, DB_DIR, DB_FILE.parent, LOG_DIR, DISPLAY_DIR, RUN_DIR,
              DOWNLOAD_DIR, PROFILE_ROOT, RECIPE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
            if not os.access(d, os.W_OK | os.X_OK):
                raise PermissionError
        except OSError:
            raise ConfigurationError(f"Runtime directory is not writable: {d}") from None


def import_ini_enabled() -> bool:
    value = os.environ.get("BC_IMPORT_INI", "true").strip().lower()
    if value not in ("true", "false"):
        raise ConfigurationError("BC_IMPORT_INI must be true or false")
    return value == "true"


def novnc_path() -> str:
    raw = os.environ.get("BC_NOVNC_PATH", "")
    prefix = raw.rstrip("/")
    if raw and not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
        raise ConfigurationError("BC_NOVNC_PATH must be an absolute URL path prefix")
    return prefix


def novnc_listen_address(port: int, has_password: bool) -> str:
    host = os.environ.get("BC_NOVNC_HOST")
    if host is None:
        return str(port) if has_password else f"127.0.0.1:{port}"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigurationError("BC_NOVNC_HOST must be an IP address") from None
    if not address.is_loopback and not has_password and not novnc_path():
        raise ConfigurationError("Non-loopback BC_NOVNC_HOST requires BC_NOVNC_PATH or a VNC password")
    return f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"


def validate_setting(key: str, value) -> None:
    if key in ("max_concurrency", "display_count", "display_start"):
        try:
            if int(value) > 0:
                return
        except (TypeError, ValueError):
            pass
        raise ConfigurationError(f"{key} must be a positive integer")
    if key == "ui_base_url":
        if value == "":
            return  # existing notification contract: relative deep links
        try:
            url = urlsplit(str(value))
            if (url.scheme in ("http", "https") and url.hostname
                    and not url.username and not url.password and not url.fragment):
                _ = url.port
                return
        except ValueError:
            pass
        raise ConfigurationError("ui_base_url must be an absolute HTTP(S) URL without credentials or fragment")


def validate_runtime(get_setting) -> None:
    """Validate effective settings before tasks start, including stored values."""
    for key in ("max_concurrency", "display_count", "display_start", "ui_base_url"):
        validate_setting(key, get_setting(key))
    if novnc_path():
        for key in ("max_concurrency", "display_count"):
            if int(get_setting(key)) != 1:
                raise ConfigurationError(f"BC_NOVNC_PATH requires effective {key}=1")
    novnc_listen_address(6080, bool(str(get_setting("vnc_password") or "").strip()))


def validate_environment() -> None:
    for name in ("BC_DATA_DIR", "BC_DB_DIR", "BC_DB_FILE", "BC_LOG_DIR",
                 "BC_BROWSER_DIR", "BC_PROFILE_ROOT", "BC_DOWNLOAD_DIR", "BC_RECIPE_DIR"):
        if name in os.environ and not os.environ[name].strip():
            raise ConfigurationError(f"{name} must be a non-empty path")
    import_ini_enabled()
    novnc_path()
    for key in ("max_concurrency", "display_count", "ui_base_url"):
        validate_setting(key, DEFAULT_SETTINGS[key])


def env(name: str, default=None):
    """Read an environment variable (call :func:`load_env` first at startup)."""
    return os.environ.get(name, default)

def chromium_args() -> list[str]:
    """Additional Chromium launch arguments, space-separated, from BC_CHROMIUM_ARGS.

    Host-specific launch workaround only — leave unset for stock behavior;
    example: ``--disable-software-rasterizer`` on GPU-less WSL2 hosts where the
    renderer produces no frames during the process cold phase (Playwright
    actionability timeouts).
    """
    return os.environ.get("BC_CHROMIUM_ARGS", "").split()


def _validate_component(value: str) -> None:
    if value in (".", "..") or any(c in value for c in ("/", "\\", "\x00")):
        raise ValueError("Service and user must be single path components")


def checked_profile_path(path) -> Path:
    root = PROFILE_ROOT.resolve()
    resolved = Path(path).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("Profile path must stay strictly below PROFILE_ROOT")
    return Path(path)


def recipe_path_for(service: str) -> Path:
    """Path of the per-service recipe file, e.g. recipe-pw__winsim.yaml."""
    _validate_component(service)
    return RECIPE_DIR / f"{RECIPE_PREFIX}{service.lower().replace(' ', '_')}.yaml"


def profile_dir_for(service: str, user: str) -> Path:
    """Per-task persistent browser profile directory (decision 9)."""
    _validate_component(service)
    _validate_component(user)
    safe = f"{service}_{user}".strip("_").replace(" ", "_").lower()
    return checked_profile_path(PROFILE_ROOT / (safe or "default"))
