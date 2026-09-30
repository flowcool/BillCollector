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

The image runs as the dedicated non-root UID/GID `5678:5678`. The wrapper
overrides this with the invoking host UID/GID so its private bind-mounted
profile, database and publication paths stay writable without `chown`. The
existing `apps/db` bind must be writable by that UID; the wrapper creates it
privately only if it is absent. The wrapper refuses a root invoker. A direct
container deployment must provision its
persistent bind mounts for the selected non-root UID before launching it;
the image's ownership does not change ownership of a mounted host directory.

By default the publication root, `state/`, `staging/`, `output/`, and PDFs are
private to the collector (`0700` directories, `0600` PDFs). For a separate DMS
identity, opt in to a trusted shared group: set
`BILLCOLLECTOR_HOST_SHARED_GID` to that numeric GID for `BillCollector.sh`, or
run a direct container with that primary GID and set
`BILLCOLLECTOR_PUBLICATION_SHARED_GID` to the same value. The publisher then
sets the publication root to `0710`, `output/` to setgid `2770`, and published
PDFs to `0640`; `state/` and `staging/` stay `0700`. This grants group members
read/delete access to PDFs in `output/` without opening private state. The DMS
must use a different UID, be a member of this group, and have execute/traverse
access through **all host ancestors** of the publication root. Prefer a
dedicated shared path over a home directory whose parent modes may block it.
Group members can modify or delete output files; only a trusted DMS should
join the group. Verify actual Paperless read/consume permissions in an isolated
deployment before production. No host ownership or Paperless configuration is
changed by this PR.

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
The local synthetic browser and Docker contracts cover the non-root image,
same-mount rename, and a separate UID in the shared group reading/removing a
PDF while private state remains inaccessible. A real provider and the actual
Paperless consume path remain release gates. No existing Selenium dedup state
is migrated or reused.

Offline verification:

```sh
python3 -m unittest discover -s tests -v
```
