# Playwright publication proof of concept (M4)

`apps/download_publication.py` is a standard-library-only publication seam.
The local Playwright runner now holds it for the entire account run and routes
each completed download through it. This is not a production deployment.

`BILLCOLLECTOR_PUBLICATION_ROOT` is required and must be an absolute path to a
persistent, private root. The runner creates `state/`, `staging/`, and `output/`
under this one root. Mount the **root once**; do not bind-mount the children
separately. Watch only `output/` with the downstream consumer. The wrapper
`BillCollector.sh` uses `BILLCOLLECTOR_HOST_PUBLICATION_DIR` (default
`${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/publication`) as that
single host mount. Its old `apps/Downloads` bind is deliberately removed on
this Playwright branch; an operator must explicitly point the DMS to the new
`output/` before any deployment. Never silently repoint a live consumer.

The caller holds `DownloadPublisher(state_dir, output_dir, staging_dir)` for the
whole account run, then calls `publish(download, service=..., account=...)` for
each Playwright `Download`. `service` and `account` must be stable, non-secret
identifiers; recipe display labels, passwords, and transient session IDs are
unsuitable. `True` means a new artifact was published, `False` means identical
PDF bytes were already published for that account. Errors must fail the run.

The three directories must be persistent and disjoint. Staging and output must
be sibling directories reached through the **same mounted tree**, with the DMS
watching *only* output. Separate Docker bind mounts can still make `rename(2)`
fail with `EXDEV` even when `st_dev` matches: the `st_dev` precheck cannot prove
that two paths are in the same rename domain. Staging must not be inside a
recursively watched consume directory. The module does not reconfigure Docker
or Paperless. The state directory and SQLite database must also be on durable
storage whose file and directory `fsync` calls have meaningful semantics.

The run lock prevents cooperating processes from publishing concurrently.
Playwright's `failure()` waits for completion; `save_as()` copies into private
staging. The module rejects empty or non-PDF files and flushes their bytes and
the staging directory before committing the `prepared` record in SQLite. It
commits a `renaming` intent before `os.replace()`, then flushes the output and
staging directories before marking `published`. Output names are opaque
account/content hashes; untrusted suggested filenames never become paths.
On restart, a `prepared` row with a valid staging file can be finished; a
`renaming` row can be marked published only when the expected final PDF is
present, valid, and the stage is absent. A missing final file is *ambiguous*:
the consumer may have taken it or the rename may have been lost in a crash.
The publisher stops for manual recovery instead of silently marking it
published or retrying a possible duplicate. Corrupt SQLite or staged bytes
likewise stop processing; neither is silently reset. An older prototype SQLite
database whose status constraint does not allow `renaming` is not migrated by
this seam; retain it for manual inspection before using the corrected format.

This provides fail-closed local publication under the stated crash/consumer
assumptions, **not** proof that the DMS successfully ingested the PDF. If an
external actor deletes staging, its absence is indistinguishable from a rename
followed by immediate consumption. Orphan staging files created before the
SQLite prepared transaction may remain and need manual inspection. No
DMS-specific API, receipt, or retry policy is included. A manual recovery
decision needs external evidence of whether the PDF was consumed; deleting the
database or a row blindly may cause duplicate output.

M1 propagates runner and publication failures. M2/M3 supply the stable
Bitwarden-item account identity and profile lock; the profile and publication
locks now span the same account run. The old direct-download path was removed.
This integration remains offline-tested only: an actual Docker build, same-mount
rename inside that container, browser smoke, and DMS consume test are still
release gates. No existing Selenium dedup state is migrated or reused.

Offline verification:

```sh
python3 -m unittest discover -s tests -v
```
