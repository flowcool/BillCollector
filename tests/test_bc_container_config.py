"""Container configuration behavior against private paths and synthetic state."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from billcollector import cli, config, display, recipes
from billcollector.db import Database
from billcollector.ui import app as ui_app


@pytest.fixture
def process_app(tmp_path):
    apps = tmp_path / "apps"
    package = apps / "billcollector"
    package.mkdir(parents=True)
    source = Path(config.__file__).parent
    for name in ("__init__.py", "config.py", "cli.py", "db.py"):
        shutil.copyfile(source / name, package / name)
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("BC_", "BW_", "VAULT_", "PLAYWRIGHT_"))}
    environment.update(PYTHONPATH=str(apps), HOME=str(tmp_path / "home"))

    def run(code, *, variables=None, dotenv=""):
        (apps / ".env").write_text(dotenv)
        result = subprocess.run(
            [sys.executable, "-c", code], env=environment | (variables or {}),
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        return result

    return apps, run


def test_dotenv_paths_and_export_priority(process_app, tmp_path):
    apps, run = process_app
    data = tmp_path / "from-dotenv"
    dbdir = tmp_path / "dbdir"
    exported = tmp_path / "exported"
    result = run(
        "import json; from billcollector import config as c; "
        "print(json.dumps([str(c.DATA_DIR), str(c.DB_FILE), str(c.PROFILE_ROOT)]))",
        variables={"BC_PROFILE_ROOT": str(exported)},
        dotenv=f"BC_DATA_DIR={data}\nBC_DB_DIR={dbdir}\nBC_PROFILE_ROOT=ignored\n",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(data), str(dbdir / "bc.db"), str(exported)]
    assert not (apps / "db").exists()


def test_explicit_db_file_parent_is_created(process_app, tmp_path):
    _apps, run = process_app
    database = tmp_path / "elsewhere" / "nested" / "custom.db"
    result = run("from billcollector.db import Database; Database().close()",
                 variables={"BC_DB_FILE": str(database)})
    assert result.returncode == 0, result.stderr
    assert database.is_file()


def test_legacy_paths_and_ui_port_priority(process_app):
    apps, run = process_app
    result = run(
        "import json; from billcollector import config as c, cli; "
        "p=cli._build_parser(); print(json.dumps([str(c.DB_FILE), str(c.PROFILE_ROOT), "
        "str(c.DOWNLOAD_DIR), p.parse_args(['serve']).port, "
        "p.parse_args(['serve','--port','8124']).port]))",
        variables={"BC_UI_PORT": "8123"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(apps / "db" / "bc.db"),
                                        str(apps / "browser" / "profiles"),
                                        str(apps / "Downloads"), 8123, 8124]


def test_helper_import_does_not_write_legacy_state_or_replace_browser_path(process_app, tmp_path):
    apps, run = process_app
    source = Path(config.__file__).parent.parent
    shutil.copytree(source / "helpers", apps / "helpers", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(source / "BillCollectorServices_pw.py", apps / "BillCollectorServices_pw.py")
    shutil.copyfile(source / "billcollector/redaction.py", apps / "billcollector/redaction.py")
    shutil.copyfile(source / "billcollector/pagestate.py", apps / "billcollector/pagestate.py")
    shutil.copyfile(source / "billcollector/interrupt.py", apps / "billcollector/interrupt.py")
    database = tmp_path / "batch" / "nested" / "test.db"
    result = run(
        "import os; from pathlib import Path; import helpers; "
        "assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == '/synthetic/browser'; "
        "assert not Path('apps/db').exists(); "
        "from BillCollectorServices_pw import DatabaseManager; "
        f"db=DatabaseManager({str(database)!r}); db.close_connection()",
        variables={"PLAYWRIGHT_BROWSERS_PATH": "/synthetic/browser"},
    )
    assert result.returncode == 0, result.stderr
    assert not (apps / "db").exists()
    assert database.is_file()


@pytest.mark.parametrize("value", ["0", "65536", "wrong"])
def test_invalid_port_and_explicit_cli_override(process_app, value):
    _apps, run = process_app
    code = "from billcollector.cli import _build_parser; _build_parser().parse_args(['serve'])"
    result = run(code, variables={"BC_UI_PORT": value})
    assert result.returncode == 2
    result = run(code.replace("['serve']", "['serve','--port','8123']"),
                 variables={"BC_UI_PORT": value})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,value", [
    ("BC_IMPORT_INI", "maybe"), ("BC_MAX_CONCURRENCY", "0"),
    ("BC_DISPLAY_COUNT", "bad"), ("BC_UI_BASE_URL", "https://user:secret@example.test"),
    ("BC_DATA_DIR", ""), ("BC_NOVNC_PATH", "/"),
])
def test_bad_environment_is_rejected_without_echoing_values(process_app, name, value):
    _apps, run = process_app
    result = run("from billcollector import config; config.validate_environment()",
                 variables={name: value})
    assert result.returncode != 0
    assert "secret" not in result.stderr


def test_environment_defaults_do_not_overwrite_stored_settings(process_app, tmp_path):
    _apps, run = process_app
    result = run(
        "import json; from billcollector.db import Database; db=Database(); "
        "before=db.get_setting('max_concurrency'); db.set_setting('max_concurrency','3'); "
        "db.close(); db=Database(); print(json.dumps([before,db.get_setting('max_concurrency')])); db.close()",
        variables={"BC_DB_FILE": str(tmp_path / "db" / "test.db"), "BC_MAX_CONCURRENCY": "2"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["2", "3"]


@pytest.mark.parametrize("enabled,expected", [("false", 0), ("true", 1)])
def test_bootstrap_import_switch_survives_restart(tmp_path, monkeypatch, enabled, expected):
    ini = tmp_path / "source.ini"
    ini.write_text("[Playwright]\nsynthetic=user\n")
    path = tmp_path / "test.db"
    monkeypatch.setenv("BC_IMPORT_INI", enabled)
    monkeypatch.delenv("BC_UI_PASSWORD", raising=False)
    monkeypatch.setattr(config, "INI_FILES", [ini])
    monkeypatch.setattr(cli, "Database", lambda: Database(path))
    with mock.patch.object(ui_app, "start"):
        assert cli.main(["serve"]) == 0
        assert cli.main(["serve"]) == 0
    with_db = Database(path)
    try:
        tasks = with_db.list_tasks()
        assert len(tasks) == expected
        assert all(not task["enabled"] for task in tasks)
    finally:
        with_db.close()


def test_empty_recipe_directory_uses_bundled_schema(tmp_path, monkeypatch):
    from tests.test_bc_recipes import VALID

    monkeypatch.setattr(config, "RECIPE_DIR", tmp_path)
    path = config.recipe_path_for("synthetic")
    assert recipes.save_recipe(path, VALID) == (True, None)
    assert recipes.load_recipe(path)[0]
    assert not (tmp_path / "recipe-pw-schema.yaml").exists()
    assert not recipes.validate_recipe("services: wrong")[0]


def test_fixed_proxy_rejects_stored_conflicts_and_ui_changes(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    db.set_setting("display_count", "2")
    monkeypatch.setenv("BC_NOVNC_PATH", "/browser")
    try:
        with pytest.raises(config.ConfigurationError, match="display_count=1"):
            config.validate_runtime(db.get_setting)
        db.set_setting("display_count", "1")
        config.validate_runtime(db.get_setting)
        with pytest.raises(config.ConfigurationError, match="max_concurrency=1"):
            db.set_setting("max_concurrency", "2")
        assert db.get_setting("max_concurrency") == "1"
        with pytest.raises(config.ConfigurationError):
            db.set_setting("display_count", "2")
    finally:
        db.close()


def test_proxy_listener_is_explicit_and_reachable(monkeypatch):
    monkeypatch.setenv("BC_NOVNC_HOST", "0.0.0.0")
    monkeypatch.delenv("BC_NOVNC_PATH", raising=False)
    with pytest.raises(config.ConfigurationError, match="requires BC_NOVNC_PATH"):
        config.novnc_listen_address(6080, False)
    monkeypatch.setenv("BC_NOVNC_PATH", "/browser")
    child = mock.Mock()
    child.poll.return_value = None
    with mock.patch.object(display, "_spawn", return_value=child) as spawn, \
            mock.patch.object(display, "_wait_for_x_display"):
        children = display.start_display_stack(99)
    assert len(children) == 3
    assert "0.0.0.0:6080" in spawn.call_args.args


def test_whitespace_vnc_password_does_not_authorize_non_loopback(monkeypatch):
    monkeypatch.setenv("BC_NOVNC_HOST", "0.0.0.0")
    monkeypatch.delenv("BC_NOVNC_PATH", raising=False)
    settings = config.DEFAULT_SETTINGS | {"vnc_password": "   "}
    with pytest.raises(config.ConfigurationError, match="requires BC_NOVNC_PATH"):
        config.validate_runtime(settings.get)
