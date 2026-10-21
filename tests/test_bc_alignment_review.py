"""Synthetic regressions for the daemon alignment review; no live state."""

import os
import runpy
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from billcollector import config, display, gitcommit, pagestate, recipes, runner
from billcollector.db import Database
from billcollector.interrupt import RunInterrupted
from billcollector.orchestrator import Orchestrator
from billcollector.ui import auth
from tests.test_bc_downloads import _StubDownload
from billcollector.downloads import track_download


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, folder in (("RUN_DIR", "runs"), ("DISPLAY_DIR", "displays"),
                         ("PROFILE_ROOT", "profiles")):
        path = tmp_path / folder
        path.mkdir()
        monkeypatch.setattr(config, name, path)
    db = Database(tmp_path / "test.db")
    db.set_setting("download_dir", str(tmp_path / "out"))
    try:
        yield db
    finally:
        db.close()


def test_private_paths_override_external_environment(tmp_path, monkeypatch):
    names = ("BC_DATA_DIR", "BC_DB_DIR", "BC_DB_FILE", "BC_LOG_DIR",
             "BC_DOWNLOAD_DIR", "BC_PROFILE_ROOT")
    for name in names:
        monkeypatch.setenv(name, str(tmp_path / "external"))
    state = runpy.run_path(str(Path(__file__).with_name("conftest.py")))
    for name in names:
        assert Path(os.environ[name]).is_relative_to(state["_TEST_ROOT"])


def test_deleted_ini_tasks_do_not_return(isolated, tmp_path):
    ini = tmp_path / "input.ini"
    ini.write_text("[Playwright]\nsvc=user\n")
    assert isolated.import_ini([ini]) == 1
    isolated.conn.execute("DELETE FROM Task")
    isolated.conn.commit()
    assert isolated.import_ini([ini]) == 0
    assert isolated.list_tasks() == []


@pytest.mark.parametrize("password,listen", [(False, "127.0.0.1:6080"), (True, "6080")])
def test_vnc_auth_controls_listener(tmp_path, password, listen):
    authfile = tmp_path / "vnc.rfbauth"
    authfile.touch()
    child = mock.Mock()
    child.poll.return_value = None
    with mock.patch.object(display, "_spawn", return_value=child) as spawn, \
            mock.patch.object(display, "_wait_for_x_display"):
        display.start_display_stack(99, rfb_auth_file=authfile if password else None)
    assert listen in spawn.call_args.args
    assert "127.0.0.1:5999" in spawn.call_args.args


def test_download_collision_and_duplicate_do_not_replace_or_republish(isolated, tmp_path):
    task = isolated.create_task("svc", "user")
    run = isolated.create_run(task)
    source = tmp_path / "source"
    def save(name, data):
        source.write_bytes(data)
        return track_download(_StubDownload(source, name, "https://synthetic.invalid/doc"),
                              run, {"id": task}, isolated)
    first = save("invoice.pdf", b"first")
    second = save("invoice.pdf", b"second")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in (tmp_path / "out").iterdir())
    duplicate = save("renamed.pdf", b"first")
    assert first["saved_path"] != second["saved_path"]
    assert Path(first["saved_path"]).read_bytes() == b"first"
    assert Path(second["saved_path"]).read_bytes() == b"second"
    assert duplicate["saved_path"] == first["saved_path"]
    assert sorted((p.name, p.stat().st_mtime_ns) for p in (tmp_path / "out").iterdir()) == before
    escaped = save("../../escape.pdf", b"third")
    assert Path(escaped["saved_path"]).parent == tmp_path / "out"


def test_recipe_commit_leaves_pre_staged_changes(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "user.name", "Synthetic test")
    (tmp_path / "recipe.yaml").write_text("before")
    git("add", "recipe.yaml")
    git("commit", "-qm", "baseline")
    (tmp_path / "other").write_text("unrelated")
    git("add", "other")
    (tmp_path / "recipe.yaml").write_text("after")
    ok, output = gitcommit.git_commit(["recipe.yaml"], "recipe update", repo=tmp_path)
    assert ok, output
    assert git("show", "--pretty=", "--name-only") == "recipe.yaml"
    assert git("diff", "--cached", "--name-only") == "other"


def test_reused_pid_does_not_hold_task_and_dead_lock_is_released(isolated):
    task = isolated.create_task("svc", "user")
    run = isolated.create_run(task, pid=os.getpid(), display=99)
    isolated.set_task_status(task, "running")
    orch = Orchestrator(isolated)
    assert orch.cleanup_orphans() == 1
    assert isolated.get_task(task)["status"] == "idle"
    newrun = isolated.create_run(task, pid=424242, display=99)
    lock = display.lock_path_for(99)
    lock.write_text("424242\n")
    orch._finalize_run(task, 424242, -9, [])
    assert not lock.exists()
    assert isolated.get_run(newrun)["result"] == "interrupted"
    assert isolated.get_run(run)["result"] == "interrupted"


def test_live_runner_identity_is_preserved(isolated):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    child = subprocess.Popen([*command, "run", "7"])
    try:
        orch = Orchestrator(isolated, runner_cmd=command)
        assert orch._runner_matches(child.pid, 7)
        assert not orch._runner_matches(child.pid, 8)
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_scan_propagates_shutdown():
    page = mock.Mock()
    page.evaluate.side_effect = RunInterrupted()
    with pytest.raises(RunInterrupted):
        pagestate.PageState("synthetic", 1, page)


@pytest.mark.parametrize("action", ["check", "select_option", "hover", "unknown_action"])
def test_repair_keeps_actions(action):
    step = {"methods": [{"method": "get_by_role"}, {"method": action}]}
    assert recipes._locator_prefix_len(step) == 1


def test_expired_until_never_gets_zero_timeout():
    ctx = mock.Mock()
    ctx.last_pause_deadline = datetime.now() - timedelta(seconds=1)
    step = {"step": 1, "methods": [{"method": "wait_user", "arguments":
            [{"prompt": "synthetic"}, {"until": {"selector": "#synthetic"}}]}]}
    def pause(*args):
        if pause.count:
            return "cancel"
        pause.count += 1
        return "resume"
    pause.count = 0
    with mock.patch.object(runner, "pause_loop", side_effect=pause):
        with pytest.raises(runner.PauseOutcomeAbort):
            runner._execute_wait_user_step(ctx, step, step["methods"][0])
    ctx.page.locator.return_value.wait_for.assert_not_called()


@pytest.mark.parametrize("service,user", [("../../db", ""), ("safe", "../db"), ("..", "")])
def test_unsafe_task_paths_are_rejected(service, user):
    with pytest.raises(ValueError):
        config.profile_dir_for(service, user)


def test_stored_profile_outside_root_cannot_be_deleted(isolated, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("preserve")
    task = isolated.create_task("svc", "user", profile_dir=str(outside))
    ok, error = isolated.reset_profile(task)
    assert not ok and error
    assert marker.read_text() == "preserve"


def test_bcrypt_byte_limit_fails_closed_for_environment(isolated):
    assert auth.password_too_long("é" * 37)
    assert not auth.password_too_long("é" * 36)
    with pytest.raises(ValueError, match="72-byte"):
        auth.ensure_password(isolated, "é" * 37)
    assert auth.get_password_hash(isolated) is None
