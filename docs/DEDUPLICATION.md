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
processed document URLs, and successfully published file contents. It does not
store raw account IDs, URLs, credentials, filenames, or invoice contents.

## Enabling it

Configure both variables:

```yaml
environment:
  BILLCOLLECTOR_STATE_DIR: /apps/state
  BILLCOLLECTOR_OUTPUT_DIR: /apps/Output
volumes:
  - ./state:/apps/state
  - ./downloads:/apps/Output
```

Do not mount Paperless directly on `/apps/Downloads` in this mode.
`/apps/Downloads` is temporary staging inside the one-shot container. If
neither variable is configured, BillCollector retains its historical behavior.
Configuring only one variable is an error.

## Decision order

For every `DownloadAll` link:

1. hash the URL;
2. skip it if that URL was published previously for the same Bitwarden item;
3. otherwise download into temporary staging;
4. hash the completed file;
5. delete it if the same content was already published under another URL;
6. otherwise move it into the output directory;
7. atomically replace the JSON state file.

The state uses the Bitwarden item ID rather than its name, so renaming an item
does not reset its history.

## Failure policy

The design is deliberately at-least-once. State is saved after a file reaches
the output directory. If the process crashes in that narrow interval, the next
run may publish a duplicate. Saving state first could permanently lose a
document, which is the less acceptable outcome.

Invalid or unsupported state fails closed. BillCollector never silently resets
the history.

## Operations

- Back up the entire state directory.
- Keep it writable only by BillCollector's UID/GID.
- Never edit `downloads-v1.json` manually.
- Do not run two BillCollector jobs concurrently against the same state.
- Restore state together with the corresponding Paperless environment.
- Deleting state deliberately causes the next run to treat the full portal
  history as new.

## Current scope

Persistent deduplication currently applies to `DownloadAll`, including the
validated Freebox recipe. The legacy `Download` action keeps its previous
behavior until it has a reliable pre-download document identity.

Scheduling remains an operational decision. Before enabling it, prove:

1. a clean first run publishes the expected history;
2. an immediate second run publishes zero documents;
3. a changed URL serving an existing PDF is suppressed by content hash;
4. state backup and restore work;
5. failed state validation stops the job visibly.
