# Write and verify a recipe

Use the daemon's [method contract](RECIPE_METHODS.md) and
[phased-login contract](SAFE_LOGIN.md). This guide explains how to use them.
The installed fork implementation is the execution authority. A contract requirement
or an approved PR is not proof that a release already enforces it.

## Link a task to its recipe and vault item

For service `ExampleMobile`, the default file is
`apps/recipes_playwright/recipe-pw__examplemobile.yaml`. Spaces in the service
name become underscores in the filename. The vault item keeps the original
service/account names: service `ExampleMobile`, account `Home` selects
`ExampleMobile Home`. A blank account selects just the service name.
The filename path can also be overridden per task.

Use one service per task recipe. Use a relative `$schema` reference to
`recipe-pw-schema.yaml` for files beside that schema. When a recipe lives
elsewhere, pass the schema explicitly to the validator. Do not commit a
workstation-specific `/home/...` schema path.

## Chain locators and actions

Each step starts from the Page. Methods run in order. A returned locator or
frame becomes the next chain object. An action returning `None` keeps the
previous object. Properties such as `first` take no arguments. A later step
starts from Page again.

```yaml
services:
- serviceName: synthetic
  steps:
  - step: 1
    methods:
    - method: get_by_role
      arguments:
      - role: link
      - name: Invoice
      - exact: true
    - method: first
    - method: click
```

Use the public API of the pinned Playwright package, not a list of method names
copied from another version. For example, `get_by_label` and
`get_by_placeholder` take a `text` argument. `press` takes `key`, not `value`.
A Page-level `click` requires `selector`; a locator's `click` does not.
The [C3 reference](RECIPE_METHODS.md#2-dispatch-model-the-playwright-surface)
describes the general surface and the five reserved names.

`arguments` is a list of option objects merged into keyword arguments. Prefer
one key per object for ordinary Playwright methods. A collection action uses
one object containing all its required options. Match argument names and types
to the installed API. The shared save/run gate checks method names, argument
names/types and variable syntax before vault or browser access.

## Login, TOTP and variables

Use the standalone `login` action for automatic credential submission. Its
preparation chains end in `fill`; its submit chain ends in `click` or
`press` with `key: Enter`. Choose a success locator unique to the authenticated
state. For password then TOTP, use two phases and different intermediate/final
success markers. The final phase must prove the top-level login success marker.
The [synthetic recipe](examples/recipe-pw__synthetic.yaml) shows this structure.
Legacy `fill`/`click` login sequences do not receive this transaction budget.

An optional TOTP belongs to the portal flow. The vault client permits an item
without TOTP, but a recipe that always fills an OTP field still needs a usable
code. Use a separate recipe or a deliberate manual checkpoint for a genuinely
different portal flow. Never substitute a real TOTP secret into a committed
example.

This fork expands `{{USERNAME}}`, `{{PASSWORD}}` and `{{OTP}}` recursively in
argument values. It also supports inline `{{TODAY}}`, `{{TODAY-7d}}`,
`{{TODAY+1d}}`, `{{DATE_FROM}}` and `{{DATE_TO}}`. Dates use the task's
`date_window_days` (default 90) and the effective `TZ`; the run captures its
calendar context once. Formats can use `%Y`, `%m`, `%d`, `%%` and separators
`- / . _` or spaces, for example `{{DATE_FROM|%d/%m/%Y}}`.
The inclusive window starts `date_window_days - 1` days before `DATE_TO`.
Replacement values remain opaque: a credential containing braces is not
expanded a second time. Unknown variables, malformed delimiters and unsupported
formats fail validation. See the [variable contract](RECIPE_METHODS.md#4-variables).

A `wait_user` step pauses before execution and is consumed on resume. Use a
standalone step with a clear prompt. Its `until` locator supports `selector`,
`role`, `test_id`, `text`, `label` and `placeholder`; label and placeholder
options are normalized to Playwright's `text` keyword. A manual checkpoint
does not grant automatic retries or count manual submissions in the login budget.

## Pagination, empty lists and downloads

| Need | Action and proof |
|---|---|
| Expand a growing list | `click_until_absent`: exactly one supported page locator then the action; require visible-row count growth through `progress_selector`, `max_clicks` and `timeout_ms`. A visible disabled button is not completion. |
| Download a fixed list | `download_all`: exactly one page locator then the action. Supply a stable non-empty unique `key_attribute`, `max_items`, `timeout_ms` and `event_window_ms`. Prove stable ordering and exactly one download per click. |
| Traverse replacement calendars | Daemon `replacement_calendar`: one calendar-root locator, offered previous-month control, account/month/readiness proof and explicit empty markers. Visits all months in the run-start date window under shared bounds. See the [contract and example](REPLACEMENT_CALENDARS.md). |
| Download one known document | `expect_download` with nested steps that trigger the click. Files pass through download tracking and per-task SHA-256 deduplication. |
| Handle an empty list | Prove the portal's explicit empty state in a synthetic fixture. A collection with no downloads can still finish as `failure` under current run-result semantics. |

Collection actions cannot be graceful, nested under `expect_download`, or placed
in login phases. They use bounded, single-attempt execution. Later failure does
not undo prior saved records. Resume intentionally after checking partial effects.
For ordinary steps, three attempts can execute the full chain before a pause.
`graceful: true` means one attempt and continuation after a recorded failure.
Use it only for an optional control whose absence you have verified.

The fork publishes distinct documents at unique paths in the configured
download directory. A duplicate reuses its existing path. This does not prove
crash-safe consumer delivery or archive receipt. Test with isolated files.

## Test without a real account

Start from a separate checkout. Install the development environment as described
in the README. Run from the repository root:

```bash
apps/.venv/bin/python -m pip install -r apps/requirements-dev.txt
apps/.venv/bin/python -m pytest tests/test_doc_recipe.py -q
```

Expected: the synthetic recipe passes schema/login validation and runs twice
against the loopback mock portal. Each run downloads the invoice and contract.
On the second run the per-task deduplication marks both as duplicates. The check
uses temporary database/profile/download paths, fake credentials and no vault
or archive consumer. The browser adapter runs headless. It does not prove the
Docker display stack, real TOTP fetching, portal account safety or a real
provider's selectors.

For a new recipe, add synthetic cases for session reuse, rejected login,
optional TOTP, changed selectors, empty lists, pagination, partial failure and
multiple PDFs as applicable. The existing `tests/test_bc_login.py` and
`tests/test_bc_collection.py` cover the generic action behavior.

The historical mock regression is a separate batch-engine compatibility check.
Run it in an isolated checkout without `--vault`. On a fresh legacy database,
initialize its schema first:

```bash
PYTHONPATH=apps apps/.venv/bin/python - <<'PY'
from BillCollectorServices_pw import DatabaseManager
from helpers import DB_FILE
manager = DatabaseManager(DB_FILE)
manager.conn.close()
PY
apps/.venv/bin/python tests/run_regression.py --ini tests/bc_regression.ini
```

Expected: `VERDICT: PASS`, seven service/account pairs and eight test downloads
cleaned up. This is not proof of daemon onboarding or a provider's live portal.
