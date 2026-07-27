# BillCollector

BillCollector downloads invoices and documents from customer portals by running
versioned Selenium recipes. Downloaded files can then be consumed by
[Paperless-ngx](https://docs.paperless-ngx.com/) or any other document
management system.

> [!IMPORTANT]
> This repository is a maintained fork of
> [s-t-e-f-a-n/BillCollector](https://github.com/s-t-e-f-a-n/BillCollector).
> We intend to keep following upstream and contribute compatible improvements
> back whenever possible. The fork currently carries production-tested changes
> that have not all been accepted upstream.

## Why this fork exists

The original project provided the browser automation engine and its YAML recipe
format. This fork keeps that design and adds:

- a reproducible, non-root `linux/amd64` container published on GHCR;
- support for Bitwarden Cloud through a private local `bw serve` API;
- exact Bitwarden item matching;
- standard container logs without credential values in action traces;
- recipe validation in CI;
- the `DownloadAll` action required by portals exposing invoice histories;
- hardened and direct handling of download links used by modern portals.

The first end-to-end production validation of these additions used the Freebox
Internet portal on 2026-07-27.

## How it works

```text
Bitwarden Cloud or Vaultwarden
              |
              v
     local `bw serve` API
              |
              v
        BillCollector
     + Selenium recipe
              |
              v
       Downloads folder
              |
              v
        Paperless-ngx
```

BillCollector never needs the Bitwarden account password or API credentials.
Those belong to the separate Bitwarden CLI service. BillCollector only receives
the URL of its private local API.

## Current status

- Maintained fork: usable
- Container architecture: `linux/amd64`
- Recipes: beta, because provider portals can change without notice
- Scheduling: only after a recipe has passed repeated manual runs
- Persistent `DownloadAll` deduplication: available when state and output
  directories are configured

Use an immutable image tag, and preferably its digest, in production. Available
tags are published in the repository's GitHub Container Registry.

## Quick start

You need:

- Docker with Compose;
- a healthy and unlocked Bitwarden CLI `bw serve` endpoint reachable only on a
  private container network;
- one Bitwarden login item per account;
- a service list;
- one compatible recipe per service.

The complete example is in
[`examples/docker-compose.yml`](examples/docker-compose.yml).

Create the local directories:

```bash
mkdir -p config recipes downloads staging state
```

Create `config/billcollector.ini`:

```ini
Free [Home]
```

This line has two distinct effects:

- BillCollector loads `bc-recipe__free.yaml`;
- BillCollector requests the exact Bitwarden item named `Free Home`.

Create that Bitwarden login item with:

```text
Name: Free Home
Username: your provider identifier
Password: your provider password
URI: https://subscribe.free.fr/login/
```

Do not put credentials in the INI file, recipe, Compose file, image, or Git
repository.

Install a compatible recipe in `./recipes`, then run:

```bash
docker compose --profile tools run --rm --no-deps billcollector
```

Inspect the logs and `./downloads` before enabling any scheduler.

The example enables persistent deduplication. Downloads first land in temporary
container staging, then unique documents are moved to `./downloads`. Persistent
hashes are stored in `./state`; back it up and never run concurrent jobs
against it. See [`docs/DEDUPLICATION.md`](docs/DEDUPLICATION.md).

## Installing recipes

Recipes are distributed separately from the engine so they can evolve at their
own pace. Production installations should use a tagged recipe release, never a
moving `main` branch.

The recommended layout is:

```text
deployment/
├── compose.yml
├── config/
│   └── billcollector.ini
├── recipes/
│   └── bc-recipe__free.yaml
└── downloads/
```

Mount the recipe directory read-only:

```yaml
volumes:
  - ./recipes:/config/recipes:ro
  - ./config/billcollector.ini:/config/billcollector.ini:ro
  - ./downloads:/apps/Downloads
```

The container command copies the selected read-only recipes into an ephemeral
runtime directory before starting BillCollector. See the supplied Compose
example for the complete command.

Recipe releases, compatibility metadata, update instructions, and rollback
instructions live in the companion
[`billcollector-recipes`](https://github.com/flowcool/billcollector-recipes)
repository.

When `bc-metadata__<service>.yaml` is present, BillCollector checks its recipe
format version and required engine actions before opening the provider portal.
An incompatible recipe stops immediately with an explicit error. Recipes
without metadata remain supported for compatibility with the original project.

## Configuration reference

### `BW_API_URL`

Required. URL of the local `bw serve` API, for example:

```text
http://bitwarden-cli:8087
```

It must resolve to a loopback, private, or container-network address. Do not
publish this API on the Internet.

### `BW_API_HOST`

Optional HTTP `Host` header override. Some `bw serve` versions protect against
DNS rebinding and only accept their loopback host:

```text
127.0.0.1:8087
```

### Service list

Each non-empty line in the INI file selects a recipe:

```ini
KabelDeutschland
Free [Home]
Lichtblick [Strom, Gas]
```

The bracket syntax reuses one provider recipe for several exact Bitwarden
items:

```text
Free [Home]            -> recipe free, item "Free Home"
Lichtblick [Strom, Gas] -> recipe lichtblick, items "Lichtblick Strom"
                           and "Lichtblick Gas"
```

### Recipe filenames

The normalized service name determines the filename:

```text
Free              -> bc-recipe__free.yaml
Freenet Mobilfunk -> bc-recipe__freenet_mobilfunk.yaml
```

## Safe operating model

1. Pin the BillCollector image.
2. Pin a tagged recipes release.
3. Run a new or updated recipe manually.
4. Confirm the expected PDFs and sanitized logs.
5. Keep the previous recipes release available for rollback.
6. Schedule only after repeated successful runs.

Tracking the recipes repository's `main` branch automatically is intentionally
discouraged: a portal or selector change must not silently alter a working
production job.

## Developing and validating a recipe

Recipes use YAML and Selenium locators. The schema is stored in
`apps/bc-recipes/bc-recipe-schema.yaml`.

Validate all bundled recipes:

```bash
python3 -m unittest discover -s tests -v
```

Validate one recipe:

```bash
python3 apps/BillCollectorRecipes.py \
  apps/bc-recipes/bc-recipe__free.yaml \
  apps/bc-recipes/bc-recipe-schema.yaml
```

Supported actions currently include:

- `Click`
- `ClickShadow`
- `SendKeys`
- `SwitchToFrame`
- `SwitchToDefaultFrame`
- `SwitchToParentFrame`
- `Download`
- `DownloadAll`

See the companion recipes repository for contribution rules and metadata.

## Security notes

- Keep `bw serve` on a private network with no published host port.
- Run BillCollector as a one-shot job with `--rm`.
- Mount configuration and recipes read-only.
- Never commit real account names, identifiers, passwords, cookies, HTML dumps,
  downloaded invoices, or Bitwarden exports.
- Account labels are included in operational logs to identify failing accounts;
  sanitize logs before sharing them publicly.
- Chrome currently runs with `--no-sandbox`; compensate with container
  isolation, a non-root user, dropped capabilities, and
  `no-new-privileges:true`.

## Upstream relationship

The `upstream` Git remote should continue to point to the original project:

```bash
git remote add upstream https://github.com/s-t-e-f-a-n/BillCollector.git
git fetch upstream
```

Changes that remain broadly useful and compatible should be proposed upstream
in focused pull requests. Fork-specific releases remain available while those
proposals are pending.

## Contributing

Engine fixes belong in this repository. Provider-specific YAML recipes belong
in the companion recipes repository.

Before opening a change:

```bash
python3 -m py_compile apps/*.py
python3 -m unittest discover -s tests -v
```

Please keep fixtures sanitized and describe how a change was validated without
including subscriber data.

## License and attribution

BillCollector remains available under the [MIT License](LICENSE). The original
project and copyright attribution are preserved.
