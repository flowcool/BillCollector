# Bourse Direct local delivery

> **Historical boundary.** This file is a retained local-delivery receipt, not
> the current recipe or deployment source. The engine owns generic collection
> behavior and its runtime contract. The recipe and its current 13-month
> `Download All` bound are owned by
> [`flowcool/billcollector-recipes`](https://github.com/flowcool/billcollector-recipes).
> Infra owns deployment and live acceptance. Use the catalog's
> `docs/bourse-direct.md`, the exported
> `recipe-pw__bourse_direct.yaml`, and the engine
> `doc/INFRA_RUNTIME_CONTRACT.md` for current work. Paths, pins and limits below
> remain historical evidence for the candidate named in this document.

Owner: `infra-8xzcb`. Operational consumer: `infra-1lr1.3`.
The [behavior spec](specs/bourse-direct-acquisition.md) owns requirements.
The [source inventory](bourse-direct-preparation.yaml) preserves prior evidence.
The [local delivery receipt](bourse-direct-local-delivery.yaml) records the exact
selected artifact, checks, image archive and proof container.

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

Use engine `45e530b7d6240abb308810183dbe4f65aae5b6dc` plus the focused local
port in `tmp/bd-local-delivery`. The generic engine alone has no Bourse Direct
modules. Reuse provider and notice-revision code from candidate `3110157` in
`tmp/bd-recipe-52`. Keep that candidate and `tmp/bd-recipe-live-52` intact.
Runtime commit `de2c57f` is packaged in the image. It scopes strict delivery to
Bourse Direct and preserves generic consumer recovery. Use
`tmp/bd-local-runtime-de2c57f` to reproduce the exact image checks. The image
qualification tool lives with the engine tests. The selected engine's journal, source keys, unpacking, vault UUID lookup,
sandbox and other provider mechanisms remain in place. The final candidate also
includes the concurrent vault transport correction from current default branch
`a5c8948`. Repeat CI and image checks qualify this combined candidate.

Native `download_all` defaults to full acquisition. Its `incremental: true`
option skips known source keys. The native contract says it does not revisit
corrections (`doc/RECIPE_METHODS.md`). Bourse Direct's daily source identifier
has no content revision. Therefore both Bourse Direct actions must fetch the
offered original again before revision comparison. They use the same native
incremental boolean, task, date context, tracker and consumer hook. They never
use source-key presence alone to discard a notice.

## Configuration and invocation

Use the selected engine's `doc/BOURSE_DIRECT.md` for the complete contract.
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

Use private persistent retention as `download_dir`. Never point this directory
at Paperless consume. Infra connects the consumer hook to its versioned delivery
path. No consumer means local retention only; `consumed` is not a Paperless receipt.

## Retention and delivery

Both actions enumerate all offered notices in the selected account/window.
They keep the first untouched original and each distinct content revision.
An unchanged or bounded metadata-regenerated repeat creates an audit row but no
new retained file or consumer call. Changed content under an existing source ID
creates a new retained original and one new delivery obligation. Equal text or
filename alone never establishes identity. See `doc/NOTICE_IDENTITY.md`.

An already retained notice is not automatically sent again. An already delivered
Paperless notice is not sent again when its revision is unchanged. Neither action
is a forced re-delivery command. Paperless receipts must be verified independently.
Do not seed this database from an old collector or Paperless listing without a
separate reviewed mapping. The retained real qualification database is proof,
not a production-state migration input.

A new notice commits journal and DB intent before publication. The Bourse Direct action selects
`delivery_policy=bourse-direct`. Before calling any consumer, it commits
`delivery_uncertain`. Generic notice identity keeps its normal recovery rule. Success completes local delivery.
A timeout, crash or exception leaves that state and preserves the original.
Startup recovery skips that uncertain row. Both Bourse Direct actions stop.
Other providers can continue. No automatic consumer or original-request replay
resolves uncertainty. A staged file can finish local publication once, using the
selected engine journal; the acquisition key still requires reconciliation.

Infra must inspect the original and external result. For a confirmed Paperless
arrival, retain a private JSON receipt with `original_sha256`, positive integer
`paperless_document_id`, `source: Bourse Direct`, and boolean readback fields
`original_readback`, `owner_readback`, `reader_readback`, all true. Infra must
obtain those readbacks; the acknowledgement command does not query Paperless:

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

The selected engine is tested through its actual YAML and daemon on a loopback
portal. Both modes cover first acquisition, repeat suppression, regeneration,
content correction, original bytes, failure before publication, crash after an
external effect, timeout without automatic replay, verified-receipt recovery,
and isolation from another provider. Account selection and year rollover are
isolated tests. These are not live-source or Paperless proofs.

Real-source proof remains candidate `3110157` (runtime `9bda8bc`, same apps/doc
content): five PEA originals, then zero new and five duplicates; no consumer.
Coverage is the current month at that qualification, October 2026. It does not
qualify PEA-PME, historical months, a real correction, interrupted Paperless
delivery or a fresh live run of the new selected artifact.

Rollback: revert the focused source commits. Keep additive DB columns, profiles,
originals, receipts and pending journal. Do not downgrade a live DB. Infra's
provider-only rollback must preserve other providers and their schedules.
