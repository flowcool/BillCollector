# Playwright profile proof of concept (M3)

The launcher now selects one Chromium user-data directory per Bitwarden item
name (`service_user`), rather than deleting a shared profile before every run.
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

This is a local, mocked prototype. It does not migrate the previous shared
`/apps/browser/profile`, and users may need to authenticate again. A changed
Bitwarden item name creates a new profile. It does not implement authenticated
backup/restore or a browser integration test with a real portal. Upstream's
The local integration branch combines this profile work with M1's error
contract; offline tests cover failure propagation, but no real browser run
has verified the combined path. Profile persistence cannot automate an
out-of-band MFA approval, SMS, or email challenge; it only reuses a session
while the portal still accepts it.
