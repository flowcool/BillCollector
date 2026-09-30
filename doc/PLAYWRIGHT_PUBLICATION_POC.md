# Playwright publication proof of concept (M4)

`apps/download_publication.py` is an offline, standard-library-only seam. It is
not connected to the current runner or a production deployment.

The caller holds `DownloadPublisher(state_dir, output_dir, staging_dir)` for the
whole account run, then calls `publish(download, service=..., account=...)` for
each Playwright `Download`. `service` and `account` must be stable, non-secret
identifiers; recipe display labels, passwords, and transient session IDs are
unsuitable. `True` means a new artifact was published, `False` means identical
PDF bytes were already published for that account. Errors must fail the run.

The three directories must be persistent and disjoint. Staging and output must
be sibling directories on the **same mount**, with the DMS watching *only*
output. Separate Docker bind mounts can still make `rename(2)` fail with
`EXDEV` even when `st_dev` matches. Staging must not be inside a recursively
watched consume directory. The module checks `st_dev` and disjoint paths; it
does not reconfigure Docker or Paperless.

The run lock prevents cooperating processes from publishing concurrently.
Playwright's `failure()` waits for completion; `save_as()` copies into private
staging. The module rejects empty or non-PDF files, flushes their bytes, records
a prepared row in SQLite, atomically renames into output, flushes the directory,
and marks the row published. Output names are opaque account/content hashes;
untrusted suggested filenames never become paths. On restart, a prepared row
with a valid staging file is finished. A row whose staging file vanished is
treated as already renamed; the DMS may have consumed the final file meanwhile.
Corrupt SQLite or staged bytes stop processing for manual recovery; neither is
silently reset.

This provides at-most-once local publication under the stated crash/consumer
assumptions, **not** proof that the DMS successfully ingested the PDF. If an
external actor deletes staging, its absence is indistinguishable from a rename
followed by immediate consumption. Orphan staging files created before the
SQLite prepared transaction are retained for manual inspection. No DMS-specific
API, receipt, or retry policy is included.

Runner integration should follow M1's error contract: `perform_actions()`
currently returns from `finally`, masking exceptions, and
`retrieve_from_service_with_playwright()` returns success even after a run with
no successful downloads. M2 must settle stable account identity in recipe/API
inputs; M3 must ensure the run lock and persistent browser profile have
compatible lifetimes. A deployment change must provide the common mount with
private staging outside the watched output tree.

Offline verification:

```sh
python -m unittest discover -s tests -v
```
