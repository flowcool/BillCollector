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

This module remains unconnected to the runner even in the local integration
branch. M1 now propagates runner failures, but its direct-download path has not
been replaced. M2/M3 supply a candidate stable account identity and profile
lock; the publication lock and profile lock still need a single run-lifetime
contract. A deployment change must provide the common mount with private
staging outside the watched output tree.

Offline verification:

```sh
python3 -m unittest discover -s tests -v
```
