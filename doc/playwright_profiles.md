# Playwright profile proof of concept (M3)

The launcher now selects one Chromium user-data directory per Bitwarden item
name (`service_user`), rather than deleting a shared profile before every run.
The directory name is a SHA-256-derived opaque identifier, so account labels do
not become filesystem path components. Existing cookies and Chromium
`Preferences` are preserved. A new profile receives the preference to download
PDFs instead of opening them in-browser.

`BILLCOLLECTOR_PROFILE_DIR` sets the profile root (default: `/apps/profiles` in
the container). This directory **must be mounted on persistent storage** to
survive container replacement. It contains live authentication material: keep
the mount private to the collector, exclude it from logs and unencrypted
backups, and define a secure backup/retention policy before production use.
The root must already be private (`0700` or stricter); the code refuses a
public root without changing its permissions. New account directories are
`0700`, and symlinked profile directories are rejected. The parent of the
profile root must also be trusted. Never place the root on the Paperless
consume or download staging mount.

This is a local, mocked prototype. It does not migrate the previous shared
`/apps/browser/profile`, and users may need to authenticate again. A changed
Bitwarden item name creates a new profile. It does not implement same-account
execution locking (M4 prerequisite), authenticated backup/restore, or a browser
integration test with a real portal. Profile persistence cannot automate an
out-of-band MFA approval, SMS, or email challenge; it only reuses a session
while the portal still accepts it.
