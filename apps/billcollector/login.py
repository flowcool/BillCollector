"""Candidate single-submit recipe contract (upstream issue #20).

Validation is separate from execution so malformed opt-in recipes fail before
vault or browser access. Ordinary recipes retain their existing behavior.
"""

LOCATORS = frozenset({
    "locator", "get_by_role", "get_by_text", "get_by_label", "get_by_test_id",
    "get_by_placeholder", "get_by_alt_text", "get_by_title", "first", "last",
    "nth", "filter", "content_frame",
})
SUCCESS_KEYS = frozenset({"role", "test_id", "placeholder", "label", "text", "selector"})


def settle_attempt(db, task_id, run_id):
    """Settle at most once and notify only on the transition to blocked."""
    from .notify import notify_login_locked

    settled = db.settle_login_attempt(task_id, run_id)
    task = db.get_task(task_id)
    if settled and task["login_locked"]:
        notify_login_locked(db, task)
    return bool(task["login_locked"])


def is_login(step):
    return any(m.get("method") == "login" for m in step.get("methods", []))


def _action(step, terminals):
    methods = step.get("methods", []) if isinstance(step, dict) else []
    if (not methods or step.get("steps") or step.get("submit")
            or step.get("graceful") or methods[-1].get("method") not in terminals
            or any(m.get("method") not in LOCATORS for m in methods[:-1])):
        raise ValueError("login requires locator chains with one explicit terminal action")
    if methods[-1]["method"] == "press":
        args = methods[-1].get("arguments", [])
        if args not in ([{"key": "Enter"}], ["Enter"]):
            raise ValueError("login submit press must use Enter")


def login_options(step):
    methods = step.get("methods", [])
    if len(methods) != 1 or methods[0].get("method") != "login" or step.get("graceful"):
        raise ValueError("login must be a standalone non-graceful method")
    options = {}
    for arg in methods[0].get("arguments", []):
        if not isinstance(arg, dict) or options.keys() & arg.keys():
            raise ValueError("login arguments must have unique names")
        options.update(arg)
    if options.keys() - {"success", "max_failures", "timeout_ms", "session_timeout_ms"}:
        raise ValueError("unknown login argument")
    success = options.get("success")
    if not isinstance(success, dict) or len(SUCCESS_KEYS & success.keys()) != 1:
        raise ValueError("login requires one explicit success locator")
    for key, default in (("max_failures", 2), ("timeout_ms", 10000),
                         ("session_timeout_ms", 1000)):
        value = options.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"login {key} must be a positive integer")
    for preparation in step.get("steps", []):
        _action(preparation, {"fill"})
    _action(step.get("submit"), {"click", "press"})
    return options


def validate_login_recipe(recipe):
    """One top-level login block per task recipe; no implicit nested execution."""
    count = 0

    def visit(step, nested=False):
        nonlocal count
        if is_login(step):
            if nested:
                raise ValueError("login blocks cannot be nested")
            login_options(step)
            count += 1
        else:
            if "submit" in step:
                raise ValueError("submit belongs to a login block")
            for child in step.get("steps", []):
                visit(child, True)

    for service in recipe.get("services", []):
        for step in service.get("steps", []):
            visit(step)
    if count > 1:
        raise ValueError("only one login block is supported per task recipe")
