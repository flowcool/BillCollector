# Bourse Direct local delivery

Owner: `infra-8xzcb`. Infra owner: `infra-1lr1.3`.
The BillCollector workspace owns the approved behavior specification and local
delivery receipt in `docs/`. The [Infra runtime contract](INFRA_RUNTIME_CONTRACT.md) defines shared configuration,
folder delivery, vault transport, schema promotion and handoff requirements.
This guide adds Bourse Direct behavior only.

## Scope and authority

Deliver the local integration for both native actions: Download All and Download
incremental. Upstream acceptance in `infra-zlth.52/.75` is separate.
Florent's 2026-10-30 GO covers automatic collection and Paperless delivery.
Infra owns deployment, provider configuration, schedules, Paperless permissions
and live acceptance in `infra-1lr1.3`. Record its exact versioned commands,
targets, stop conditions and provider-only recovery before live execution.
Keep at least 60 seconds between broker login transactions or original requests.
No Ghostfolio production import, stable publication or removal of originals,
containers or proof is included. Preserve CC Genevois and Sunday 04:17.

## Selected implementation

Use a pinned fork image from `playwright-next` that includes the Bourse Direct
modules and recipe. They were integrated in fork PR #76. State the exact image,
recipe and schema hashes in the Infra handoff. Keep historical candidate
`3110157` and its private two-pass proof intact; they are not the current runtime
or proof of the daemon/Infra interfaces. The engine journal, source keys,
unpacking, vault id lookup and sandbox remain in use.

Native `download_all` defaults to full acquisition. Its `incremental: true`
option skips known source keys. The native contract says it does not revisit
corrections (`doc/RECIPE_METHODS.md`). Bourse Direct's daily source identifier
has no content revision. Therefore both Bourse Direct actions must fetch the
offered original again before revision comparison. They use the same native
incremental boolean, task, date context, tracker and folder publication path. They never
use source-key presence alone to discard a notice.

## Configuration and invocation

Apply the shared [runtime contract](INFRA_RUNTIME_CONTRACT.md) before configuring the task.
One native task selects one offered account. `account_type` is `PEA` by default;
`PEA-PME` is supported in isolated tests only. Set the task's `recipe_file` to
`/apps/recipes_playwright/recipe-pw__boursedirect.yaml`. Use the same task ID for
both actions. Do not create a second task or reset the database for Download All.

The native vault item name is exactly `<task.service> <task.user>`, trimmed.
Set those fields to match the existing item. The recipe's service label does not
change this rule. For example, an item named `Bourse Direct` can use task service
`Bourse Direct`, empty user and the explicit recipe path above. Keep secrets in
the existing sidecar. Native preflight resolves one exact item, then fetches its
password and fresh TOTP by UUID. Keep `BW_API_URL`/`BW_API_HOST` or their existing
Settings equivalents. Do not rename or duplicate a vault item automatically.

Run inside the configured engine environment, with its existing BC paths:

```sh
python3 -m billcollector run "$TASK_ID" --collection-mode all
python3 -m billcollector run "$TASK_ID" --collection-mode incremental
```

The override lasts for one invocation. Scheduled/native runs use the recipe's
`incremental: true`. To select an explicit start date, use the native stdin
option; the end is the captured run-start date in the configured timezone:

```sh
printf '%s\n' "$START_DATE" | python3 -m billcollector run "$TASK_ID" \
  --collection-mode all --date-from-stdin
```

`date_window_days` defaults to 90. The adapter reads complete intersecting months,
including the first partial month. It does not promise a strict calendar-day
filter. `max_months=12`, `max_items=100`, `timeout_ms=7200000` bound this recipe.
Missing month/account evidence stops collection. A generic empty month retains
the selected engine's existing empty-run result.

Set the global `download_dir` to `/apps/paperless-consume` as specified in the
[runtime contract](INFRA_RUNTIME_CONTRACT.md#delivery-to-paperless). Infra mounts
the Paperless consume directory there. The shipped consumer registry stays empty;
there is no configurable consumer hook. Infra's Paperless consumption workflow
filters `BourseDirect_*.pdf` and sets owner, exact Source `Bourse Direct`, and reader
permissions. Infra verifies the archived original and those fields independently.
`consumed` records local publication, not a Paperless receipt.

## Retention and delivery

Both actions enumerate all offered notices in the selected account/window.
They publish the first untouched original and each distinct content revision.
An unchanged or bounded metadata-regenerated repeat creates an audit row but no
new published file. Changed content under an existing source ID
publishes a new original for the consume-folder workflow. Equal text or
filename alone never establishes identity. See `doc/NOTICE_IDENTITY.md`.

An already retained notice is not automatically sent again. An already delivered
Paperless notice is not sent again when its revision is unchanged. Neither action
is a forced re-delivery command. Paperless receipts must be verified independently.
Do not seed this database from an old collector or Paperless listing without a
separate reviewed mapping. The retained real qualification database is proof,
not a production-state migration input.

A new notice commits journal and DB intent before publication. The strict
Bourse Direct delivery policy commits `delivery_uncertain` before completing the
delivery phase. With the shipped empty consumer registry, that phase completes
local publication immediately. It does not wait for Paperless ingestion.
A timeout, crash or exception leaves that state and preserves the original.
Startup recovery skips that uncertain row. Both Bourse Direct actions stop.
Other providers can continue. No automatic delivery or original-request replay
resolves uncertainty. A staged file can finish local publication once, using the
selected engine journal; the acquisition key still requires reconciliation.

Infra must inspect the original and external result. For a confirmed Paperless
arrival, retain a private JSON receipt with `original_sha256`, positive integer
`paperless_document_id`, `source: Bourse Direct`, and boolean readback fields
`original_readback`, `owner_readback`, `reader_readback`, all true. Infra must
obtain those readbacks; the acknowledgement command does not query Paperless.
The command also requires matching original bytes at the Download `saved_path`.
If the consume watcher already removed that file, this command cannot acknowledge
the row as-is. Stop and use an Infra-reviewed recovery that preserves the original
and receipt; do not resend, recreate the file in consume, or clear pending state
merely to satisfy this precondition.

This recovery command is not a normal consume-folder ingestion step.

```sh
python3 -m billcollector.bourse_direct_delivery --task "$TASK_ID" \
  --download "$DOWNLOAD_ID" --receipt "$PRIVATE_RECEIPT"
```

The command checks the task lock, exact row/hash and retained bytes. It records
the receipt and clears only a matching acquisition key. It sends nothing.
Missing or uncertain external proof means stop and keep pending state. Confirmed
absence needs a reviewed Infra recovery action; this command cannot resend.
An unmatched acquisition key also requires inspection. Never clear it blindly.

## Evidence and limits

The synthetic tests exercise the selected YAML and runner code against a
loopback portal, with injected credentials/TOTP and test consumers. They bypass
native vault preflight/transport and the real Infra/Paperless delivery path. Both modes cover first acquisition, repeat suppression, regeneration,
content correction, original bytes, failure before publication, crash after an
external effect, timeout without automatic replay, verified-receipt recovery,
and isolation from another provider. Account selection and year rollover are
isolated tests. These are not live-source, deployed-vault or Paperless proofs. Use the runtime
contract's daemon-path rule for a real qualification and declare every bypass.

Real-source proof remains candidate `3110157` (runtime `9bda8bc`, same apps/doc
content): five PEA originals, then zero new and five duplicates; no consumer.
That historical workspace harness injected credentials from a sibling reader;
it did not qualify `vault_transport`, native preflight or daemon orchestration.
Coverage is the current month at that qualification, October 2026. It does not
qualify PEA-PME, historical months, a real correction, interrupted Paperless
delivery or a fresh live run of the new selected artifact.

Rollback: revert the focused source commits. Keep additive DB columns, profiles,
originals, receipts and pending journal. Do not downgrade a live DB. Infra's
provider-only rollback must preserve other providers and their schedules.
