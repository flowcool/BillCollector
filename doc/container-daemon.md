# Install the Playwright daemon with Compose

This guide covers the daemon in this fork's `playwright-next` branch. It does
not migrate a Selenium scheduler or connect downloads to Paperless. Keep that
working installation until a separate cutover is validated.

## Select an image

The runtime image supports **linux/amd64** and uses UID/GID **1000:1000**.
The daemon command is explicit in Compose. The image's default command remains
bash, so existing batch and local-build commands keep their behavior.

Until a published release includes this guide and its configuration changes,
build this checkout. Do not use an earlier published image with these examples.

```bash
git switch playwright-next
docker build --build-arg SOURCE=https://github.com/flowcool/BillCollector \
  --build-arg REVISION="$(git rev-parse HEAD)" \
  --build-arg VERSION=local-daemon -t billcollector:daemon-local .
cp compose/.env.example compose/.env
```

The fork's package is `ghcr.io/flowcool/billcollector-playwright`. The legacy
Selenium package `ghcr.io/flowcool/billcollector` is a different installation.
The assurance workflow publishes only for release tags or an explicit publish
request on the default branch, after its gates pass. A green PR build does not
publish an image. The publication job transfers the tested image; it does not
rebuild it. No official upstream image is implied.

For a validated release, replace `BC_IMAGE` in `compose/.env` with its documented
version/SHA tag **and digest**. Verify package access with `docker pull` before
changing an instance. A private package needs registry credentials. Do not infer
anonymous access from a passing build or put registry passwords in Compose.

```text
BC_IMAGE=ghcr.io/flowcool/billcollector-playwright:<release-tag>@sha256:<release-digest>
```

Check the pulled image, using the complete reference from that release:

```bash
docker image inspect IMAGE --format '{{index .Config.Labels "org.opencontainers.image.source"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

The revision must match the release source. A local build without `REVISION`
reports `unknown`. A local fork build should also supply `SOURCE` as above.

## Start with zero tasks

The primary [Compose example](../compose/daemon.compose.yaml) uses named volumes
and the image's default UID. It needs no vault credentials just to start.

```bash
docker compose --env-file compose/.env -f compose/daemon.compose.yaml \
  -p billcollector up -d --wait
```

Open `http://127.0.0.1:8000`. For a remote host, use an SSH tunnel or the
authenticated proxy variant below. The first login sets the native password.
Its hash and the session secret persist in SQLite. `BC_UI_PASSWORD` can initialize
a missing password hash instead; it is optional and never replaces a saved hash.

The example sets `BC_IMPORT_INI=false`. This skips INI import on every daemon
start. It does not delete or disable tasks already stored in an existing DB.
With a fresh volume, the task list stays empty after restart/recreation.

For an existing INI installation, change that value to `true` in a Compose
override and mount your INIs read-only at `/apps/bc_default.ini` and
`/apps/bc_test.ini`. The default is `true` outside this example. Imported tasks
are disabled. The import marker prevents deleted tasks from returning.
Confirm each task and schedule in the UI before enabling it.

The container has no portal tasks by default. Before a first collection, add a
recipe, configure a reachable vault endpoint, and verify the download directory.
Test against staging storage before selecting a downstream document consumer.

## State and permissions

| Container path in the example | Content | Persistence |
| --- | --- | --- |
| `/apps/data/db` | SQLite, WAL/SHM, tasks, run/download history, Settings, password hash and session secret | `state` volume |
| `/apps/data/logs`, `/apps/data/runs` | Logs, screenshots, pause artifacts and control sockets | `state` volume |
| `/apps/data/displays` | Display locks; orphan cleanup handles interrupted runners | `state` volume |
| `/apps/data/profiles` | Private persistent Chromium profiles and login cookies | `state` volume |
| `/apps/data/downloads` | Collected documents | `state` volume |
| `/apps/data/home`, `/apps/data/nicegui` | Writable HOME and NiceGUI storage location | `state` volume |
| `/apps/recipes` | Operator recipe edits | `recipes` volume |
| `/apps/recipes_playwright` | Bundled recipe sources from the selected image | Image content |
| `/opt/billcollector/recipe-pw-schema.yaml` | Trusted schema from the selected image | Image content |
| `/apps/browser` | Installed Chromium/FFmpeg binaries | Image content; do not cover with an empty state mount |

The schema stays outside the writable recipe mount. A fresh empty recipe volume
supports validation and editing without a copy step. Recipes can be created in
the UI. To start from a bundled recipe, copy the selected file inside the container
to `/apps/recipes` and edit that copy. Upgrades preserve operator edits. They do
not merge new bundled recipes into existing edits. Explicit schema arguments in
the Python recipe API remain supported.

Git auto-commit stays disabled by default. A plain recipe volume is not a Git
checkout. Enable auto-commit only with a repository layout that supports it;
atomic recipe saves do not require Git.

### Use pre-owned bind mounts or a different UID

Runtime `user: 1003:1000` does not change the image's build-time ownership.
The isolated checks cover default UID 1000 and UID/GID 1003:1000 with pre-owned
bind mounts. They do not claim support for every UID or host filesystem.

For that bind-mount variant, provision **new** private directories first:

```bash
sudo install -d -o 1003 -g 1000 -m 0700 /srv/billcollector/state /srv/billcollector/recipes
sudo chmod 0700 /srv/billcollector/state /srv/billcollector/recipes
stat -c '%u:%g %a %n' /srv/billcollector/state /srv/billcollector/recipes
```

Expected owner/mode: `1003:1000 700`. Verify the actual mode: NAS behavior can
differ from the requested `install` mode. Do not use these commands to take
ownership of another application's directories.

Set `BC_UID=1003`, `BC_GID=1000`, `BC_STATE_PATH=/srv/billcollector/state` and
`BC_RECIPES_PATH=/srv/billcollector/recipes` in `compose/.env`. Then add the
[bind override](../compose/daemon.bind.compose.yaml):

```bash
docker compose --env-file compose/.env -f compose/daemon.compose.yaml \
  -f compose/daemon.bind.compose.yaml -p billcollector up -d --wait
```

Use the same files for later commands. The override refuses absent host paths.
The application reports unwritable runtime directories and does not chown
mounted trees or start as root. Changing the UID on populated named volumes
needs a planned ownership migration or a restored pre-owned bind mount.

## Reuse an existing Bitwarden sidecar

`BW_API_URL` points to a local **bw serve HTTP API**, not a Bitwarden/Vaultwarden
server URL. Example values in `compose/.env`:

```text
BW_API_URL=http://bitwarden-cli:8087
BW_API_HOST=127.0.0.1:8087
```

The second variable supplies the loopback Host header required by `bw serve`.
For a sidecar on another Docker network, set `BC_VAULT_NETWORK` to that existing
trusted private network and add `-f compose/daemon.vault.compose.yaml` to the
Compose command. The sidecar must have the `bitwarden-cli` DNS alias there, or
use its actual alias in `BW_API_URL`. A private host endpoint can also work.
No second vault or new login is created by the example.

Keep the sidecar private. Read the existing [transport contract](vault_transport.md)
for allowed addresses, redirects, timeouts, Host checks and optional TOTP.
Do not include vault data, credentials or complete environment dumps in reports.

## Protected HTTPS browser access

The basic example exposes only the UI on host loopback. It does not provide
remote browser access. To use an existing authenticated proxy, set
`BC_PUBLIC_URL=https://your-collector.example` and `BC_PROXY_NETWORK` to the
private proxy network, then add the [proxy override](../compose/daemon.proxy.compose.yaml).
The proxy and vault overrides attach only their explicit networks; they remove
the implicit default-network attachment. Combine both overrides when needed.
Websockify listens on all attached networks. Every peer on the proxy or vault
network must therefore be trusted to access the browser directly. Use dedicated
networks with only the proxy and vault sidecar; attach no unrelated containers.

Route and authenticate both backends on the same origin:

| External route | Backend | Required behavior |
| --- | --- | --- |
| `/` and the UI WebSocket routes | `billcollector:8000` | Forward HTTP/WebSockets; preserve the session cookie and origin; enforce proxy authentication. |
| `/browser/` | `billcollector:6080` | Authenticate first, remove `/browser`, then forward HTTP/WebSockets. `/browser/websockify` must reach `/websockify`. |

The override sets `BC_NOVNC_PATH=/browser`, `BC_NOVNC_HOST=0.0.0.0` and uses the
primary example's single-display defaults. The explicit bind makes websockify
reachable from a proxy in another container. Without an explicit bind, existing
behavior stays: loopback without VNC password; all interfaces with VNC password.
An explicit non-loopback bind without a VNC password requires `BC_NOVNC_PATH`.
That opt-in does not verify your external authentication setup.

Keep **effective** `display_count=1` and `max_concurrency=1` for the fixed backend.
Stored Settings take priority over environment defaults. Startup rejects a saved
conflict before the scheduler starts; the UI rejects incompatible edits. For an
older DB, first start without the proxy override, set both values to 1 in the UI,
then recreate with the override. No database edit is needed.

Native application authentication does not protect the separate websockify
process. Protect both routes with the proxy. Publish no raw VNC or browser port
on the host. Test anonymous rejection, authenticated frame loading and its live
WebSocket. A redirect alone does not prove backend connectivity. Details and
verification: [noVNC routing](novnc-reverse-proxy.md).

## Configuration reference

Application `.env` is loaded from `apps/.env` before paths are resolved. Exported
environment values win. In the container this location is `/apps/.env`; mount it
read-only if needed. The image excludes `.env` files.
The host `compose/.env` provides Compose interpolation values. It is not the
application `.env`. The example explicitly maps the required values into the
container. Restart/recreate to apply changes. Do not paste resolved `compose config`
output when it can contain secrets.

| Input | Default outside the Compose example | Rule |
| --- | --- | --- |
| `BC_DATA_DIR` | `/apps/data` | Logs, displays and runs derive from it; it does not relocate SQLite/profiles/downloads. |
| `BC_DB_DIR` | `/apps/db` | Default SQLite directory. |
| `BC_DB_FILE` | `${BC_DB_DIR}/bc.db` | Explicit file wins; its parent is created. Setting only `BC_DB_DIR` now relocates the default file. Check older deployments that set only this variable. |
| `BC_LOG_DIR` | `${BC_DATA_DIR}/logs` | Process path. |
| `BC_BROWSER_DIR` | `/apps/browser` | Browser binaries and default profile parent. |
| `PLAYWRIGHT_BROWSERS_PATH` | `${BC_BROWSER_DIR}` when unset | Explicit Playwright browser path wins. |
| `BC_PROFILE_ROOT` | `${BC_BROWSER_DIR}/profiles` | Persistent profiles. Keep it separate from binary mounts. |
| `BC_DOWNLOAD_DIR` | `/apps/Downloads` | Default `download_dir`; stored Setting wins. |
| `BC_RECIPE_DIR` | `/apps/recipes_playwright` | Writable recipes, independently of the trusted schema. |
| `BC_UI_HOST` | `127.0.0.1` | `--host` wins. Compose binds `0.0.0.0` inside the container. |
| `BC_UI_PORT` | `8000` | Integer 1..65535; `--port` wins. |
| `BC_IMPORT_INI` | `true` | Accepts `true` or `false`; controls daemon import only. |
| `BC_UI_BASE_URL` | `http://127.0.0.1:8000` | Default `ui_base_url`; stored Setting wins. Empty keeps relative notification links. |
| `BC_DISPLAY_COUNT` | `16` | Positive integer default; stored `display_count` wins. |
| `BC_MAX_CONCURRENCY` | `1` | Positive integer default; stored `max_concurrency` wins. Restart to resize the semaphore. |
| `BC_NOVNC_PATH` | Empty | Optional absolute prefix such as `/browser`. Requires one effective display and one concurrent task. |
| `BC_NOVNC_HOST` | Auth-dependent legacy bind | Explicit IP address. IPv6 is accepted. Network exposure requires review. |
| `BC_UI_PASSWORD` | Unset | Optional first-hash initialization. The first-start form remains available. |
| `HOME`, `NICEGUI_STORAGE_PATH`, `TZ` | Image/library defaults | The example sets writable persistent locations and timezone. |
| `BW_API_URL`, `BW_API_HOST` | Existing transport behavior | See the sidecar section. |
| `BC_CHROMIUM_ARGS`, `NTFY_HOST` | Existing browser/notification behavior | Advanced inputs; not needed for container startup. |

Stored Settings remain authoritative. Environment defaults never overwrite them
on restart. Invalid explicit values stop startup with a bounded diagnostic. The
image build arguments `APP_UID`/`APP_GID` change ownership at build time; Compose
`BC_UID`/`BC_GID` select Docker `user` at runtime. They are not application aliases.

## Upgrade, backup and restore

Use the same Compose project name and overrides throughout. Record the current
image digest, source revision, UID and mounts. Pull/build the candidate before
stopping the service. Disable unattended deployment during this procedure.

Stop the daemon with Compose and verify it has stopped. The example allows two
minutes for runners and display processes to terminate. Back up both state and
recipes with your volume backup tool **while stopped**. A live copy of `bc.db`
alone is not a consistent backup in WAL mode. Include the whole state volume,
profiles, configuration and recipe edits. Treat the backup as secret material.
Store it outside the build context and preserve owner/mode.

For the named-volume example, the volumes are `billcollector_state` and
`billcollector_recipes`. Verify their names with `docker volume inspect` before
backup. For bind mounts, back up the two configured host paths. Restore into
separate storage first and verify ownership and readable state before cutover.

Change only `BC_IMAGE`, then `up -d --wait`. Verify health, task count, effective
settings, recipe validation and profile/download paths before enabling tasks.
The health endpoint `/_alive` proves HTTP liveness, not vault access, Chromium,
proxy integration or collection success. Run those checks separately.

To roll back, stop the candidate, select the previous image digest and recreate
with the same mounts **only if the database is backward compatible**. Otherwise
restore the stopped pre-upgrade backup with the previous image. This change adds
no database schema migration. A later release may do so. Never use `down -v` on
an instance whose volumes you must retain.

## Reproduce the isolated acceptance check

After building the image, from the repository root:

```bash
python3 tests/check_container_compose.py --image billcollector:daemon-local
```

This requires local Docker Compose and OpenSSL. It tests default named volumes
at UID 1000 and private pre-owned binds at UID/GID 1003:1000. It uses a unique
Compose project on an internal network with no published ports. A separate
synthetic vault/portal/TLS proxy container proves authentication, the real noVNC
frame and RFB WebSocket. A headed task downloads one PDF, the container is stopped
and recreated, then a second run proves profile-cookie persistence and dedup.
It also checks zero-import recreation and recipe validation without a schema
copy. It removes only its private lab containers, volumes and temporary files.
No real vault, provider, Beads database or Paperless endpoint is accepted.
