# Playwright profile proof of concept (M3)

The launcher now selects one Chromium user-data directory per immutable Bitwarden
item ID, rather than deleting a shared profile before every run.
The directory name is a SHA-256-derived opaque identifier, so account labels do
not become filesystem path components. Existing cookies and Chromium
`Preferences` are preserved. A new profile receives the preference to download
PDFs instead of opening them in-browser.

`BILLCOLLECTOR_PROFILE_DIR` sets the profile root. The Playwright image defaults
to `/var/lib/billcollector/profiles`, outside the `/apps` Docker build context;
a direct local Python run defaults to `${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/profiles`.
`BillCollector.sh` mounts `${BILLCOLLECTOR_HOST_PROFILE_DIR}` or, by default,
`${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/profiles` from the host.
Direct container deployments **must mount persistent storage** at the same
container path to survive replacement. `.dockerignore` excludes legacy
`apps/profiles`, browser, database, and download directories from both image
builds. The static test checks these paths; it does not substitute for a
Docker-engine build-context inspection before release.

The image now defaults to the non-root UID/GID `5678:5678`; the wrapper runs
with the invoking host UID/GID for writable bind mounts and refuses a root
invoker. Direct deployments must provision the private profile mount for the
chosen runtime UID before starting the container. Running the process as
non-root limits container privileges; it does **not** by itself enable the
Chromium sandbox, which remains a separate deployment/security review item.

The profile root contains live authentication material: keep
the mount private to the collector, exclude it from logs and unencrypted
backups, and define a secure backup/retention policy before production use.
The root must already be private (`0700` or stricter); the code refuses a
public root without changing its permissions. New account directories are
`0700`, and symlinked profile directories are rejected. A nonblocking lock
per account is held from browser launch until after browser close; a second
same-account run fails instead of sharing a live Chromium profile. Initial
`Preferences` are written to a private temporary file and published atomically
only if the target does not already exist. The parent of the profile root must
also be trusted. Never place the root on the Paperless
consume or download staging mount.

This local branch does not migrate the previous shared `/apps/browser/profile`
or the earlier name-derived Playwright profile. Users may need to authenticate
again. Renaming an item keeps its profile; deleting and recreating the item
creates a fresh profile. Do not copy the old name-derived profile to the new ID
without reviewing its cookies and account ownership. It does not implement
authenticated backup/restore. A real
Chromium smoke against a local synthetic portal now verifies a persistent
cookie across two process runs; it does not validate any real provider. The
synthetic cookie has an expiry (`Max-Age`): a browser session cookie without
one was not restored after Chromium closed in this test. Profile persistence
therefore cannot promise that a portal's session survives restart, nor automate
an out-of-band MFA approval, SMS, or email challenge.
