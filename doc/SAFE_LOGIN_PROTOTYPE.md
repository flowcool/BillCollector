# Single-submit login prototype

Candidate interface for [upstream issue #20](https://github.com/s-t-e-f-a-n/BillCollector/issues/20).
This fork-only prototype is incomplete and is not approved for deployment.
Stefan has not agreed the recipe or database interface.

## Candidate recipe

Navigate to the portal before the login block. An already visible success locator
skips the whole block. Preparation supports locator chains ending in `fill` only.
The submit chain ends in one `click` or `press(key="Enter")`. Do not use this
contract for a portal that submits automatically when a field is filled, or for
authentication that needs multiple submit actions.

```yaml
services:
  - serviceName: synthetic
    steps:
      - step: 1
        methods:
          - method: goto
            arguments:
              - url: http://127.0.0.1:8787/
      - step: 2
        methods:
          - method: login
            arguments:
              - success: {selector: '#account'}
              - max_failures: 2
              - timeout_ms: 10000
              - session_timeout_ms: 1000
        steps:
          - step: 21
            methods:
              - method: get_by_label
                arguments:
                  - text: Password
              - method: fill
                arguments:
                  - value: '{{PASSWORD}}'
        submit:
          step: 22
          methods:
            - method: get_by_role
              arguments:
                - role: button
                - name: Sign in
            - method: click
```

The daemon is the only supported executor of the new method. The historical batch
runner is unchanged. Existing daemon recipes keep their existing behavior.

## Persistence and recovery

The four additive Task fields are `login_failures`, `login_failure_limit`,
`login_locked` and `login_pending_run_id`. Reservation increments and commits the
budget before dispatch. A pending attempt is settled once on normal exit, runner
failure, monitor finalization or orphan recovery. A crash without success proof
consumes that budget. A crash before dispatch can conservatively consume it too.

The limit cannot increase during a consecutive-failure cycle. A new lower limit
can tighten it. Explicit clear preserves the profile and scheduler configuration,
and leaves only one attempt. Session proof resets the budget. Ordinary lifecycle
status changes cannot clear a login block. Manual VNC/guided-browser submissions
are outside the automatic counter and the UI states that limitation.

## Verification status

Static review and Ruff passed. Functional acceptance is **not established**.
The first focused run passed four tests before interruption. It accidentally
called the default public ntfy transport from a temporary DB. The interrupted
stack was in `urllib3.util.connection`; delivery is unknown. No real credentials
or provider data were used. Both test processes were stopped. The existing suite
also showed one failure before interruption; its details were not collected.

The new test module now forbids `requests.post` unless explicitly mocked and
sets the temporary DB notification topic to empty. These isolation fixes and
the final code have not been tested after the stop.

Before any publication or upstream contribution:

1. Recheck the notification isolation and review every test transport.
2. Run the focused suite with the installed test environment and local Chromium:
   `python -m pytest tests/test_bc_login.py -q`.
3. Complete UI tests for clear, blocked buttons and schedule preservation; add
   transport-mocked notification assertions and full dispatcher coverage.
4. Check stale success proofs, concurrent direct runs, pending-run ownership,
   interrupted settlement, and threshold changes. Review the conservative crash
   policy against Stefan's response.
5. Run the full suite and investigate the interrupted baseline-suite failure.
   Run the historical mock-portal regression in a disposable tree without vault.
6. Run the target baseline's pinned static checks and compare baseline findings.

This is a source-only prototype. Do not connect it to the live database, vault,
portal, Paperless or a deployed daemon. Discard/revert the prototype commit to
roll back local source. For a future deployment, an older binary ignores the
login block, so preserve the DB and stop blocked tasks before reverting binaries.
