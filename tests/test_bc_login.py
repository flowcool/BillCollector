"""Issue #20: isolated durable budget, entry guards and real loopback submits."""

import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from playwright.sync_api import sync_playwright

from billcollector import config, runner
from billcollector.db import Database, SCHEMA
from billcollector.interrupt import RunInterrupted
from billcollector.login import login_options, settle_attempt, validate_login_recipe
from billcollector.orchestrator import Orchestrator
from billcollector.recipes import validate_recipe


@pytest.fixture(autouse=True)
def no_notification_network(monkeypatch):
    """A missing notification mock must fail locally, never contact ntfy."""
    def refuse(*args, **kwargs):
        raise AssertionError("Notification transport must be mocked in login tests")

    monkeypatch.setattr("billcollector.notify.requests.post", refuse)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "login.db")
    database.set_setting("ntfy_topic", "")
    yield database
    database.close()


def task_run(db, limit=2):
    task = db.create_task("mock", "synthetic", enabled=True, cron="0 0 * * *")
    run = db.create_run(task)
    db.update_task(task, login_failure_limit=limit)
    return task, run


def login_step():
    return {
        "step": 2,
        "methods": [{"method": "login", "arguments": [
            {"success": {"selector": "#account"}}, {"timeout_ms": 50},
            {"session_timeout_ms": 50}]}],
        "steps": [{"step": 21, "methods": [
            {"method": "locator", "arguments": [{"selector": "#password"}]},
            {"method": "fill", "arguments": [{"value": "synthetic-password"}]}]}],
        "submit": {"step": 22, "methods": [
            {"method": "locator", "arguments": [{"selector": "#submit"}]},
            {"method": "click"}]},
    }


def test_migrate_old_database_twice_preserves_data(tmp_path):
    path = tmp_path / "old.db"
    old_schema = "\n".join(line for line in SCHEMA.splitlines() if "login_" not in line)
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO Task(service,user,enabled) VALUES ('old','user',1)")
    for _ in range(2):
        with_db = Database(path)
        task = with_db.get_task(1)
        assert task["service"] == "old"
        assert task["enabled"] == 1
        assert task["login_failures"] == task["login_locked"] == 0
        assert task["login_pending_run_id"] is None
        with_db.close()


def test_durable_crash_budget_one_attempt_clear_and_success(db):
    task, run = task_run(db)
    assert db.reserve_login_attempt(task, run, 2)
    assert not db.claim_task_run(task)  # pending survives admission, even if idle
    assert not settle_attempt(db, task, run)
    assert not db.settle_login_attempt(task, run)  # idempotent
    assert db.reserve_login_attempt(task, run + 1, 2)
    path = db.path
    other = Database(path)
    assert settle_attempt(other, task, run + 1)
    assert not other.claim_task_run(task)
    assert not other.reserve_login_attempt(task, run + 2, 2)
    assert other.clear_login_lock(task)
    assert other.get_task(task)["login_failures"] == 1
    assert other.get_task(task)["enabled"] == 1
    assert other.reserve_login_attempt(task, run + 2, 2)
    other.login_succeeded(task, run + 2)
    assert other.get_task(task)["login_failures"] == 0
    assert other.get_task(task)["login_pending_run_id"] is None
    other.close()


def test_clear_requires_idle_locked_task_without_pending_submission(db):
    task, run = task_run(db, 1)
    assert not db.clear_login_lock(task)
    assert db.reserve_login_attempt(task, run, 1)
    db.update_task(task, login_locked=1)
    assert not db.clear_login_lock(task)
    settle_attempt(db, task, run)
    db.set_task_status(task, "waiting")
    assert not db.clear_login_lock(task)
    db.set_task_status(task, "idle")
    assert db.clear_login_lock(task)


def test_concurrent_connections_can_reserve_only_one_attempt(db):
    task, run = task_run(db)

    def reserve(_):
        connection = Database(db.path)
        try:
            return connection.reserve_login_attempt(task, run, 2)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert db.get_task(task)["login_failures"] == 1


def test_orphan_cleanup_consumes_pending_and_keeps_lock(db):
    task, run = task_run(db, 1)
    assert db.reserve_login_attempt(task, run, 1)
    db.set_task_status(task, "running")
    assert Orchestrator(db).cleanup_orphans() == 1
    assert db.get_task(task)["status"] == "idle"
    assert db.get_task(task)["login_locked"] == 1
    assert not db.claim_task_run(task)


def test_monitor_finalization_cannot_remove_login_lock(db):
    task, run = task_run(db, 1)
    db.update_run(run, pid=12345)
    assert db.reserve_login_attempt(task, run, 1)
    Orchestrator(db)._finalize_run(task, 12345, -9, [])
    assert db.get_task(task)["login_locked"] == 1
    assert db.get_run(run)["result"] == "interrupted"


async def test_blocked_manual_and_scheduled_admission_never_spawns(db):
    task, _ = task_run(db)
    db.update_task(task, login_locked=1)
    orchestrator = Orchestrator(db)
    with mock.patch.object(orchestrator, "_spawn") as spawn:
        assert await orchestrator.start_run(task) is None
        assert await orchestrator.start_run(task) is None
    spawn.assert_not_called()
    assert db.list_runs(task)[0]["pid"] is None


def test_blocked_direct_runner_stops_before_recipe_vault_or_display(db, monkeypatch):
    task, _ = task_run(db)
    db.update_task(task, login_locked=1)
    monkeypatch.setattr(config, "DB_FILE", db.path)
    with mock.patch.object(runner, "_install_signal_handlers"), \
            mock.patch.object(runner, "CheckRecipe") as recipe, \
            mock.patch.object(runner, "_vault_preflight") as vault, \
            mock.patch.object(runner.display, "allocate_display") as display:
        assert runner.run_task(task) == 1
    recipe.assert_not_called()
    vault.assert_not_called()
    display.assert_not_called()


@pytest.mark.parametrize("mutation", ["graceful", "nested", "prepare_click", "double_submit",
                                      "missing_proof", "negative_limit", "two_logins"])
def test_invalid_contract_rejected_by_runtime_and_ui(mutation):
    step = login_step()
    if mutation == "graceful":
        step["graceful"] = True
    elif mutation == "nested":
        step = {"step": 1, "methods": [{"method": "expect_download"}], "steps": [step]}
    elif mutation == "prepare_click":
        step["steps"][0]["methods"][-1] = {"method": "click"}
    elif mutation == "double_submit":
        step["submit"]["methods"].append({"method": "click"})
    elif mutation == "missing_proof":
        step["methods"][0]["arguments"] = []
    elif mutation == "negative_limit":
        step["methods"][0]["arguments"].append({"max_failures": -1})
    recipe = {"services": [{"serviceName": "mock", "steps": [step]}]}
    if mutation == "two_logins":
        recipe["services"][0]["steps"].append(copy.deepcopy(step))
    with pytest.raises(ValueError):
        validate_login_recipe(recipe)
    assert not validate_recipe(yaml.safe_dump(recipe))[0]


def test_valid_contract_accepted():
    recipe = {"services": [{"serviceName": "mock", "steps": [login_step()]}]}
    validate_login_recipe(recipe)
    assert validate_recipe(yaml.safe_dump(recipe))[0]
    assert login_options(login_step())["max_failures"] == 2


@pytest.fixture
def portal():
    state = SimpleNamespace(submissions=0, success=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = ("<div id='account'>Account</div>" if state.success else
                    "<form method='post'><input id='password' name='password' type='password'>"
                    "<button id='submit'>Sign in</button></form>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            state.submissions += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def browser_ctx(db, portal):
    task, run = task_run(db)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(portal.url)
        context = SimpleNamespace(db=db, task=db.get_task(task), run_id=run, page=page,
                                  debug=False, bcs=SimpleNamespace(page=page, service="mock"))
        yield context
        browser.close()


def test_real_browser_failure_pause_resume_never_resubmits(browser_ctx, portal):
    ctx = browser_ctx
    pauses = []

    def pause(*_):
        pauses.append(portal.submissions)
        if len(pauses) == 2:
            portal.success = True  # simulate operator completing authentication
            ctx.page.goto(portal.url)
        return "resume"

    with mock.patch.object(runner, "pause_loop", side_effect=pause):
        runner._execute_login(ctx, login_step())
    assert pauses == [1, 1]
    assert portal.submissions == 1
    assert ctx.db.get_task(ctx.task["id"])["login_failures"] == 0


def test_real_browser_threshold_blocks_and_clear_grants_one_attempt(browser_ctx, portal):
    ctx = browser_ctx
    with mock.patch.object(runner, "pause_loop", return_value="timeout"):
        with pytest.raises(runner.PauseOutcomeAbort):
            runner._execute_login(ctx, login_step())
    assert portal.submissions == 1
    with mock.patch("billcollector.notify.notify_login_locked") as notify:
        with pytest.raises(runner.StepAbortError):
            runner._execute_login(ctx, login_step())
        notify.assert_called_once()
    assert portal.submissions == 2
    assert ctx.db.get_task(ctx.task["id"])["login_locked"] == 1
    assert ctx.db.clear_login_lock(ctx.task["id"])
    with pytest.raises(runner.StepAbortError):
        runner._execute_login(ctx, login_step())
    assert portal.submissions == 3
    assert ctx.db.get_task(ctx.task["id"])["login_locked"] == 1


def test_real_browser_existing_session_skips_and_resets(browser_ctx, portal):
    portal.success = True
    browser_ctx.page.goto(portal.url)
    browser_ctx.db.update_task(browser_ctx.task["id"], login_failures=1)
    runner._execute_login(browser_ctx, login_step())
    assert portal.submissions == 0
    assert browser_ctx.db.get_task(browser_ctx.task["id"])["login_failures"] == 0


def test_interruption_after_reservation_remains_durable(browser_ctx):
    step = login_step()
    step["steps"] = []
    with mock.patch.object(runner, "_execute_step", side_effect=RunInterrupted("test")):
        with pytest.raises(RunInterrupted):
            runner._execute_login(browser_ctx, step)
    task = browser_ctx.db.get_task(browser_ctx.task["id"])
    assert task["login_failures"] == 1
    assert task["login_pending_run_id"] == browser_ctx.run_id


def test_prepare_failure_never_consumes_attempt(browser_ctx, portal):
    with mock.patch.object(runner, "_execute_step", side_effect=runner.StepAbortError("test")):
        with pytest.raises(runner.StepAbortError):
            runner._execute_login(browser_ctx, login_step())
    assert portal.submissions == 0
    assert browser_ctx.db.get_task(browser_ctx.task["id"])["login_failures"] == 0
