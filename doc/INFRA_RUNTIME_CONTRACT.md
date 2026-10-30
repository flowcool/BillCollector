# Infra runtime contract

This file is the single Infra-facing interface reference for the fork's
`playwright-next` daemon. Provider guides add provider behavior and link here.
Infra owns deployment, mounts, schedules, Paperless workflows and live acceptance.
A source merge or an image publication is not deployment or provider acceptance.

## Delivery to Paperless

Set the global Settings key `download_dir` to `/apps/paperless-consume`.
Infra mounts its Paperless consume directory there, writable by the daemon UID/GID.
This setting applies to every task in the database. It is not a per-task field.
An empty setting falls back to `BC_DOWNLOAD_DIR`, then `/apps/Downloads`.
An environment change does not override a non-empty stored `download_dir`.

Use folder delivery with the shipped empty consumer registry
(`apps/billcollector/consumers/__init__.py`, `CONSUMERS = []`). There is no
Settings or environment interface that installs a consumer hook. Do not require
an upload callback or a new consumer service for this integration.

The tracker stages originals below `<download_dir>/.billcollector/`, commits
SQLite intent, then atomically publishes each file in `download_dir`. Infra must
preserve this hidden journal with the DB and keep it outside document ingestion.
Keep the database's absolute path stable: it selects the journal namespace.
A consume-folder watcher may remove a published file before the tracker finishes.
The original `saved_path` can therefore be absent after successful consumption.

With no consumers, the tracker can record `consumed` after local publication.
That state, a successful Run, an empty consume directory, and a workflow trigger
are not Paperless receipts. Infra verifies the archived original and compares its
SHA-256 with the Download row. Paperless deduplication here is exact-byte only;
it does not identify an unchanged notice in a PDF regenerated with new bytes.
Use SHA-256 as the original-byte identity in qualification and handoff evidence.
Bourse Direct revision comparison remains the collector's separate bounded rule.

Infra owns a Paperless consumption workflow filtered on `BourseDirect_*.pdf`.
It sets the document owner, the exact Source value `Bourse Direct`, and reader
permissions. These are Paperless workflow settings, not BillCollector Task fields.
Verify original bytes, owner, Source and reader permissions on the resulting
Paperless document. Do not infer them from the filename or successful publication.
Other providers use their Infra-owned consumption rules.

An uncertain publication or archive result stops dependent retries. Preserve DB,
journal, profiles, originals and receipts. Reconcile the existing effect before
sending or collecting again. If a Bourse Direct uncertain row has lost its
`saved_path` after ingestion, the shipped acknowledgement command cannot clear
it. Recovery for that case is a known code gap; keep the task stopped pending
a reviewed engine change and an Infra operation plan. See [download recovery](DOWNLOAD_RECOVERY.md) and
[Bourse Direct recovery](BOURSE_DIRECT.md#retention-and-delivery).

## Vault interface

The daemon path is `runner._vault_preflight` -> `vault.fetch_credentials` ->
`vault_transport.vault_http_request`. Use `BW_API_URL` for the private local
`bw serve` HTTP base URL and `BW_API_HOST` for its HTTP Host override.
The Settings keys `vault_api_url` and `vault_api_host` override the corresponding
environment value when non-empty. Empty or absent keys use the environment.
These Settings and environment values must describe the same intended sidecar.

The base URL has no query, fragment or user information. The transport validates
local DNS addresses, pins the connection, ignores ambient proxies and refuses
redirects. It preserves endpoint queries such as the exact-name search.
`BW_API_HOST=127.0.0.1:<BW_PORT>` can satisfy the sidecar's Host check while the
base URL uses its private container hostname. See [vault transport](vault_transport.md).
Never attach credentials, endpoint responses or private item identifiers to proof.

The item name is exactly `f"{task.service} {task.user}".strip()`.
Search results must contain exactly one equal name. The daemon then fetches the
item and optional TOTP by that item's id, not by a fuzzy name or first match.
A task with service `Bourse Direct` and empty user resolves `Bourse Direct`.
The task's explicit recipe path can differ from the service-derived filename.
The recipe selects the portal URL; a vault item's URI does not select it.
Preserve optional-TOTP behavior and fresh TOTP retrieval through this same client.

## Recipe schema promotion

The image contains the release schema at
`/opt/billcollector/recipe-pw-schema.yaml`. The daemon validates with
`/apps/recipes_playwright/recipe-pw-schema.yaml`, which a recipe-directory mount
can hide. Infra owns the initialization step: `cp -n` can seed a missing file
from the packaged schema, but preserves an existing file even when incompatible
with new recipe actions. The image does not perform this copy automatically.
Without a schema in the mounted recipe directory, validation fails.

Every release and Infra handoff states the SHA-256 of
`apps/recipes_playwright/recipe-pw-schema.yaml`. This candidate's schema is:

```text
1d3b111472c6fdfaab9860fe1ee28d6c255105c69de8281902eefcef631e70f2
```

Calculate it from the exact candidate with
`sha256sum apps/recipes_playwright/recipe-pw-schema.yaml`.
Infra's versioned rollout must compare the packaged and mounted hashes, retain
the prior schema, explicitly promote the reviewed file when needed, and read back
the mounted hash. An image update or `cp -n` alone does not prove promotion.
Schema promotion changes the accepted recipe contract; it does not migrate SQLite.

## Database and task configuration

`Database._init_schema` creates missing tables and invokes `_migrate` on open.
The current migrations add missing columns after `PRAGMA table_info` checks:

| Table | Existing additive migrations |
| --- | --- |
| `Task` | `profile_reset_at`, `date_window_days`, `login_failures`, `login_failure_limit`, `login_locked`, `login_cycle_active`, `login_pending_run_id` |
| `Download` | `state`, `staged_path`, `source_key`, `source_identity`, `revision_sha256`, `delivery_policy`; index `download_notice_revision` |

Adding `login_cycle_active` also initializes it conservatively for prior login
attempt/lock state or a limit of one. Historical download state defaults to
`consumed`; adding columns does not prove archive delivery or populate source keys.
This upstream synchronization adds no DB migration. The schema descriptions
now state that notice identity is daemon-only; the option set is unchanged.
Compare `db.py` with the previously deployed revision for each later release; list additions, backfills, defaults and
compatibility limits in its handoff. Reuse the same DB and Task ids for dedup.
Do not seed history from filenames, Paperless or another collector implicitly.

Keep a verified DB/journal/profile backup before a real upgrade. An older binary
may ignore newer delivery/login state. Code rollback is a focused revert; a DB
rollback requires an Infra recovery plan. Do not erase new columns or historical
rows to make an old binary start.

| Task field | Infra meaning |
| --- | --- |
| `service`, `user` | Unique task pair and exact vault-name components. Preserve identity across upgrades. |
| `recipe_file` | Explicit absolute recipe path; otherwise derived from `service`. |
| `enabled`, `cron` | Automatic scheduling; manual Run now does not enable the task. Cron and dates use `TZ`. |
| `profile_dir` | Persistent browser session path within the configured profile root. |
| `date_window_days` | Positive integer, default `90`; per task, not a Settings key or environment variable. |
| Login fields | Persistent attempt/lock state owned by the daemon; use its reviewed clear/recovery path. |

`date_window_days` includes the run-start day. `--date-from-stdin` overrides the
start for one run; the end is the captured current date in `TZ` (default `UTC`).
Provider monthly traversal can include a complete first partial month. Use the
provider guide to state the actual coverage rather than claiming a day filter.

## Settings and environment

| Interface | Required use and precedence |
| --- | --- |
| Settings `download_dir` | `/apps/paperless-consume` for this integration; global non-empty value wins over `BC_DOWNLOAD_DIR`. |
| Settings `vault_api_url`, `vault_api_host` | Non-empty values override `BW_API_URL`, `BW_API_HOST` respectively. |
| Settings `ntfy_topic` | Explicit opt-in destination; absent or blank means no notification. |
| Environment `NTFY_HOST` | Set `http://<HOST_IP>:7979` for the self-hosted receiver. Otherwise the existing default is public `https://ntfy.sh`. |
| Environment `TZ` | Explicit deployment timezone for cron and run dates. |
| Environment `BC_DB_FILE`, `BC_DATA_DIR`, `BC_PROFILE_ROOT` | Persistent DB, runtime/journal coordination and browser profile locations; keep stable. |
| Settings `download_recovery_timeout_seconds` | Positive startup recovery timeout, default `300` seconds. |

The Compose environment can override the mounted `apps/.env`; `load_env` does
not overwrite exported variables. State the effective values and mounts in the
handoff, with placeholders for private hosts, topics and account references.
Terminal ntfy sends are best-effort and contain fixed account-free messages.
They cover failure, interruption (including Stop/shutdown) and attention timeout.
Success and pause cancellation stay silent. Existing pause/login-lock notifications
have separate payloads. No notification result proves archive delivery.

## Qualification and declared bypasses

A real-provider qualification must execute the daemon runner path, with its
actual vault preflight/transport, configured folder delivery, DB and publication
journal. Use the candidate image, recipe and mounted schema. Capture the run id,
terminal result, original checksums, effective non-secret configuration, journal
outcome and independent archive readback. Run through the daemon/UI or the native
`python3 -m billcollector run <TASK_ID>` entry point in the approved environment.
Record whether scheduling/orchestration was exercised separately.

Real-provider or archive access requires its scoped operation authority. This
contract grants none. Offline tests use isolated DBs, profiles, output and mocked
services. Mark each bypass explicitly: the result qualifies only the exercised
interface, never the bypassed one. Keep historical proof bound to its exact
artifact, inputs and environment. Do not rerun a live account to fill a doc gap.

| Harness | Scope and bypasses that must appear in its receipt |
| --- | --- |
| Workspace `tools/run_provider_recipe.py` | Calls recipe internals with injected credentials; reads the sidecar via SSH/curl outside `vault_transport`; replaces browser/pause behavior and disables consumers. Does not qualify native vault preflight, daemon orchestration, Infra consume mounts or Paperless. |
| Workspace `tools/run_bourse_direct_recipe.py` | Injects credentials/TOTP from the sibling reader and calls recipe internals; changes browser/pause behavior and disables consumers. Does not qualify `vault_transport`, native preflight, daemon orchestration or archive delivery. |
| Workspace `tools/qualify_bourse_direct.py` | Direct provider browser study with the sibling vault reader. Does not qualify the daemon runner, its tracker/journal, vault transport or Paperless. |
| `tests/qualify_bourse_direct_image.py` | Packaged code and synthetic BD/image checks with no network/live mounts. Fixtures inject credentials and consumers. Does not qualify a real sidecar, provider or Paperless integration. |
| `tests/run_regression.py` | Legacy batch/mock-portal compatibility. The no-vault mode injects synthetic credentials/timing. Does not qualify daemon onboarding, its task vault lookup or an Infra archive. |
| `tests/run_sandbox_container.py` | Isolated packaged sandbox/runtime checks. Synthetic fixtures do not qualify real credentials, selectors, consume mounts or Paperless permissions. |

The workspace tools are historical proof sources, not commands to run as part of
this release. Their output cannot substitute for the daemon-path acceptance above.
See [recipe authoring](RECIPE_AUTHORING.md#test-without-a-real-account) for the
synthetic recipe check and its own declared bypasses.

## Required Infra handoff

Copy this template into the owning Infra operation bead. Complete every field.
Use `none` with a source comparison when a release adds no change. Keep private
readbacks in private proof; the handoff contains references and hashes only.

```text
Candidate revision and PR:
Image: ghcr.io/flowcool/billcollector-playwright:sha-<SHORT>@sha256:<DIGEST>
Schema SHA-256 (packaged and mounted):
DB migrations/backfills and rollback compatibility:
Task fields/recipe paths/date coverage:
Required Settings (download_dir, vault overrides, ntfy_topic):
Required environment (BW_API_URL, BW_API_HOST, NTFY_HOST, TZ, BC_*):
Mounts and UID/GID access:
Contract deltas (or none, with comparison revision):
Qualification: exact image/recipe/schema, daemon entry point, run ids/results:
Bypasses and interfaces NOT qualified:
Original-byte SHA-256, journal outcome and private Paperless readback references:
Infra-owned consumption workflow: BourseDirect_*.pdf -> owner, Source, reader:
Operation authority, stop conditions and verified recovery:
```

A handoff is complete when the candidate is pinned, every required configuration
and contract delta is stated, and proof boundaries are explicit. Infra owns
execution/readback under its operation plan. Reuse acceptance only for unchanged
artifacts, inputs and environment; source tests alone do not release a provider.

## Implementation references

Check the exact candidate before a release: [runtime paths and precedence](../apps/billcollector/config.py),
[task fields and additive migrations](../apps/billcollector/db.py),
[native runner/preflight](../apps/billcollector/runner.py),
[exact item and TOTP lookup](../apps/billcollector/vault.py),
[transport enforcement](../apps/vault_transport.py),
[publication journal](../apps/billcollector/downloads.py), and the
[empty consumer registry](../apps/billcollector/consumers/__init__.py).
