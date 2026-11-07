# GitHub validation and maintenance

Dependabot groups weekly Python and GitHub Actions updates. Security alerts and
security-update pull requests are enabled in repository settings. Merges remain
manual; merging a validated dependency update publishes a commit-tagged image.

Playwright 1.64.1 requires `pyee>=13,<15`. Dependabot excludes `pyee>=15`
until the Playwright dependency bound changes. The runtime keeps an exact pin.
The qualified `pyee==14.0.0` pin requires Python 3.12 or newer. The Docker
image and assurance workflow both use Python 3.12.

The Playwright assurance workflow runs on pull requests, maintained-branch pushes,
release tags, manual dispatch and a weekly schedule. It checks unit tests, a
synthetic Chromium page, dependency consistency, undefined Python names, known
runtime dependency vulnerabilities, CodeQL for Python and Actions, and the built
container. Markdown-only pull requests skip unit/browser, Python security,
CodeQL and image jobs. Their existing check names remain visible as skipped.
The classifier still runs. Changes to executable recipe examples in `doc/SAFE_LOGIN.md` also run the image job. The workflow
continues to trigger on every PR so required checks cannot remain pending.
The daemon pytest suite uses repository-local Chromium binaries.
The mock regression initializes its private legacy SQLite tables before running. GitHub schedules use UTC and execute the default branch's workflow.

Bandit records medium and high findings and rejects high-severity findings.
CodeQL and Trivy findings are available in GitHub code scanning. CodeQL excludes
only `tests/mock_portal/app.py`, the synthetic portal served on loopback by
`tests/mock_portal/__main__.py`; application code and other tests remain scanned. Image publication
rejects fixable HIGH/CRITICAL vulnerabilities; unresolved lower-severity and
unfixed findings remain visible for review. One complete Trivy JSON scan supplies
both SARIF reporting and the fixable HIGH/CRITICAL gate; conversion does not scan
or query the vulnerability database again. Scanner success does not establish
that an application is vulnerability-free.

Release-tag pushes, pushes to the default branch, and opted-in manual requests
from the default branch can publish. Default-branch pushes publish only the
`sha-<short>` tag. No floating tag is published. The publishing job waits for every validation job and verifies that the
commit is already integrated into the default branch. It publishes the exact
scanned image through a one-day internal workflow artifact, without rebuilding.
The artifact uses compression level 1. This reduces transfer size for uncompressed
Docker layers. Archives with compressed OCI layers gain little; there is no
separate compression/decompression command or change to image loading.
Ordinary PR and scheduled runs do not store image archives or publish packages.

The Playwright image uses a separate `billcollector-playwright` GHCR package.
It does not overwrite the Selenium package currently used by production.
Registry credentials and package-write permissions exist only in the publication job.
All external actions are pinned to immutable commits and maintained by Dependabot.

Portable workflow/configuration changes can be proposed upstream independently
of fork-specific branch rules, package naming, and release settings. No workflow
uses a real vault, portal account, shared database, or document output mount.

The runtime image extracts Ubuntu's noVNC static assets in a separate build stage.
The qualified client is Ubuntu `novnc` version `1:1.3.0-2`. Its package copyright
is retained. These copied assets are absent from runtime dpkg inventory; Trivy
does not track them as an installed Ubuntu package. Reassess the client itself
when its source package changes.
The browser uses the same client through the existing websockify process. Node.js,
Pillow and the noVNC Python tooling are not runtime dependencies. The packaged
runtime gate connects the actual noVNC client to its websocket/RFB endpoint as
well as checking Chromium, daemon startup and the mock-portal regression.
