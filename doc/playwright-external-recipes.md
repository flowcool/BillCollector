# External Playwright recipes (local prototype)

The engine can read Playwright YAML recipes from a separately versioned directory.
Set `BILLCOLLECTOR_RECIPES_DIR` to the mounted directory; mount it read-only in
the deployment. External recipes are disabled unless
`BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS` is also set to a comma-separated list
of exact, reviewed HTTPS origins (for example,
`https://login.example.test,https://account.example.test`). Keep that list in
reviewed deployment configuration, not in the recipe. If the recipe directory
variable is absent, the existing bundled recipes remain the default. An
external directory is authoritative: a missing or invalid recipe is an error,
never a silent fallback to a bundled version.

For execution, set `BILLCOLLECTOR_RECIPE_APPROVALS_FILE` to an operator-owned
JSON file **outside** the recipe directory. A separate approval is required for
each Bitwarden item/account. The digest pins the exact recipe bytes, and the
account origins must be a subset of the deployment-wide origin ceiling:

```json
{
  "formatVersion": 1,
  "accounts": {
    "sample alice": {
      "service": "sample",
      "sha256": "<64 lowercase hex characters from sha256sum recipe-pw__sample.yaml>",
      "origins": ["https://example.test"]
    }
  }
}
```

Review the recipe and its origins together, calculate the SHA-256 of the
reviewed file, then update the approval file. A changed recipe, unknown
account, wrong service, or widened origin set fails **before** Bitwarden item
or TOTP lookup. The approved YAML is parsed once and passed unchanged to the
runner, avoiding a second file read after credentials are fetched. A final
symlink for the approval file is rejected. Protect the approval file and its
parent directory against writes by the recipe source or browser process.

Example external recipe:

```yaml
formatVersion: 1
capabilities: [browser, download]
services:
  - serviceName: sample
    steps:
      - step: 1
        methods:
          - method: goto
            arguments:
              - url: https://example.test/
      - step: 2
        methods:
          - method: expect_download
        steps:
          - step: 3
            methods:
              - method: get_by_role
                arguments:
                  - role: link
                  - name: Invoice
              - method: click
```

Save this as `recipe-pw__sample.yaml`. The loader always uses the engine's
bundled schema. Legacy `$schema` values, including the absolute paths currently
present in bundled examples, are ignored at runtime.

For an invoice list, use `download_all` as the final method on a locator:

```yaml
- step: 5
  methods:
    - method: locator
      arguments:
        - selector: ".invoice-download"
    - method: download_all
      arguments:
        - timeout_ms: 10000
```

`download_all` clicks every matching link or control, including links that open
a new tab. It re-queries the full locator after each click, checks the list's
order, and clicks a stable element reference. Identical `(href, text)` pairs
are rejected before any click; choose a narrower locator for such a portal.
It restores the listing after navigation. `download_all` cannot be nested under
`expect_download`. Each control must start exactly one download: the runner
waits up to `timeout_ms` for the first event, then observes a further 500 ms
for a second event. Zero or multiple events in those windows fail the service.
An empty, changed, or partially downloaded list also fails. The optional
`timeout_ms` is in milliseconds (100 to 120000; default 30000). A second
download started more than 500 ms after the first is outside this contract and
may be missed; that portal needs a dedicated action or an agreed event policy.
Each completed file goes through the existing content-based publisher, so
already published PDFs are reported as duplicates. Unlike Selenium's URL-based
pre-skip, controls are clicked on every run to obtain and hash their bytes.
On partial failure, the service run is recorded as failed with the number of
files already processed; any published PDF remains safely deduplicated on retry.

Validate without a browser:

```sh
BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS=https://example.test \
PYTHONPATH=apps python -m helpers.BillCollectorRecipeContract \
  --dir /path/to/recipes sample
```

The complete recipe is checked before opening Chromium. Only the currently
supported Playwright locator/action names and their expected arguments are
permitted; unknown methods such as `evaluate`, invalid Page/Locator chains,
and extra arguments are rejected. Playwright `press` uses a `key` argument.
External `goto` steps must target one of the configured HTTPS origins. Secret
placeholders (`{{USERNAME}}`, `{{PASSWORD}}`, `{{OTP}}`) are only accepted in
`fill(value)` on a locator outside a frame. Immediately before each such fill,
the runtime checks the current top-level page origin against the same list.
Missing or malformed origin configuration stops an external recipe before the
browser opens. Bundled recipes retain their original HTTP(S) URL behavior.

`BillCollector.sh` forwards external recipes when `BILLCOLLECTOR_HOST_RECIPES_DIR`,
`BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE` (a regular file outside that directory)
and `BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS` are all set. It mounts the first two
read-only at `/recipes` and `/approvals/recipe-approvals.json` and sets the matching
container variables; with the first variable unset, bundled recipes are used.

This is an operator-review gate, **not** a sandbox for untrusted YAML. A recipe
can still click through to another origin, trigger a redirect, operate on a
compromised allowed page, or cause a page to send entered credentials elsewhere.
The top-level origin check blocks an observed redirect at credential fill, but
still has a navigation race; it is not a network exfiltration boundary. Do not
run arbitrary recipes. Cross-origin SSO, iframe login, signed provenance, and
stronger network policy need a separate design before production use.

This local branch combines the external recipe contract with persistent profiles
and durable publication. Scheduling is a separate engine/runtime contract.
