# External Playwright recipes (local prototype)

The engine can read Playwright YAML recipes from a separately versioned directory.
Set `BILLCOLLECTOR_RECIPES_DIR` to the mounted directory; mount it read-only in
the deployment. If the variable is absent, the existing bundled recipes remain
the default. An external directory is authoritative: a missing recipe is an
error, never a silent fallback to a bundled version.

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
PYTHONPATH=apps python -m helpers.BillCollectorRecipeContract \
  --dir /path/to/recipes sample
```

The complete recipe is checked before opening Chromium. Only the currently
supported Playwright locator/action names and their expected arguments are
permitted; unknown methods such as `evaluate` and extra arguments are rejected.
`goto` accepts only HTTP(S) URLs. This limits the YAML execution surface but
does **not** make an arbitrary recipe source trustworthy: a recipe can still
navigate to an attacker-controlled website and fill credentials there. Pin and
review the separate recipe repository before deployment. Credential-to-origin
binding, redirects, and stronger network policy remain open design decisions.

This prototype does not change profile persistence, download publication,
deduplication, or scheduling. Those are separate engine/runtime contracts.
