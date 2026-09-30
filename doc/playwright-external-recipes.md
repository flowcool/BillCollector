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
present in bundled examples, are ignored at runtime. Validate without a browser:

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

This is an operator-review gate, **not** a sandbox for untrusted YAML. A recipe
can still click through to another origin, trigger a redirect, operate on a
compromised allowed page, or cause a page to send entered credentials elsewhere.
The top-level origin check also has a navigation race. Do not run arbitrary
recipes: pin and review the separate recipe repository and its origin list
together. Cross-origin SSO, iframe login, provenance/signatures, and stronger
network policy need a separate design before production use.

This prototype does not change profile persistence, download publication,
deduplication, or scheduling. Those are separate engine/runtime contracts.
