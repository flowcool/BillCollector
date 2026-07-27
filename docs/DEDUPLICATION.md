# Persistent download deduplication

BillCollector can keep a persistent, privacy-preserving history for
`DownloadAll` recipes.

## Data flow

```text
provider portal
      |
      v
/apps/Downloads        temporary container staging
      |
      +-- known URL hash --------------------------> skip
      |
      +-- known PDF content hash -----------------> delete staged duplicate
      |
      v
/apps/Output           Paperless consume bind mount
      |
      v
/apps/state/downloads-v1.json
```

The state contains SHA-256 hashes of the Bitwarden item ID, successfully
processed document URLs, final document filenames, and successfully published
file contents. It does not store raw account IDs, URLs, credentials, filenames,
or invoice contents.

## Enabling it

Configure both variables:

```yaml
environment:
  BILLCOLLECTOR_STATE_DIR: /apps/state
  BILLCOLLECTOR_OUTPUT_DIR: /apps/Output
volumes:
  - ./staging:/apps/Downloads
  - ./state:/apps/state
  - ./downloads:/apps/Output
```

Do not mount Paperless directly on `/apps/Downloads` in this mode. The migration
must replace that mount, never add the new output mount beside it. Otherwise
Paperless sees the raw file before deduplication.

Mount staging and output on the same host filesystem so publication is an
atomic rename instead of a cross-filesystem copy followed by unlink. If neither
variable is configured, BillCollector retains its historical behavior.
Configuring only one variable is an error.

## Decision order

For every `DownloadAll` link:

1. hash the URL;
2. skip it if that URL was published previously for the same Bitwarden item;
3. otherwise download into temporary staging;
4. hash the completed file;
5. hash its final filename as the provider document identity;
6. delete it if the same document identity or content was already published;
7. otherwise move it into the output directory;
8. atomically replace the JSON state file.

The state uses the Bitwarden item ID rather than its name, so renaming an item
does not reset its history.

## Failure policy

The design is deliberately at-least-once. State is saved after a file reaches
the output directory. If the process crashes in that narrow interval, the next
run may publish a duplicate. Saving state first could permanently lose a
document, which is the less acceptable outcome.

Invalid or unsupported state fails closed. BillCollector never silently resets
the history. Each update flushes the temporary file, preserves the previous
valid state as `downloads-v1.json.bak`, atomically replaces the primary file,
and flushes the state directory.

## Operations

- Back up the entire state directory.
- Recommended host permissions are owner `1000`, group `1000`, mode `750`.
  BillCollector writes as UID/GID 1000; a backup agent in group 1000 may read
  the hashes.
- Never edit `downloads-v1.json` manually.
- Concurrent jobs against the same state fail immediately on an engine lock.
- Restore state together with the corresponding Paperless environment.
- Deleting state deliberately causes the next run to treat the full portal
  history as new.
- When Paperless is restored to an older point in time, archive then remove the
  BillCollector state before the next run. Keeping a newer state would silently
  skip invoices that no longer exist in the restored Paperless database.
- If the primary JSON is corrupt, stop the job and restore
  `downloads-v1.json.bak`; never reset it automatically.

## Current scope

Persistent deduplication currently applies to `DownloadAll`, including the
validated Freebox recipe. The legacy `Download` action keeps its previous
behavior until it has a reliable pre-download document identity.

Free regenerates invoice URLs and PDF bytes between sessions while keeping a
stable final invoice filename. Filename identity is therefore authoritative for
that provider. If a provider reuses the same filename for a corrected document,
BillCollector treats it as the same invoice; operators must deliberately reset
that account's state to ingest the replacement.

A filename collision in the output directory aborts the current run. Documents
published before the collision are already recorded, so a later run resumes
without republishing them. Hash lists are intentionally unbounded; their size
is negligible for invoice-scale workloads.

Scheduling remains an operational decision. Before enabling it, prove:

1. a clean first run publishes the expected history;
2. an immediate second run publishes zero documents;
3. a changed URL serving an existing PDF is suppressed by content hash;
4. state backup and restore work;
5. failed state validation stops the job visibly.
