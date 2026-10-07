# GitHub validation and maintenance

Dependabot groups weekly Python and GitHub Actions updates. Security alerts and
security-update pull requests are enabled in repository settings. Merges remain
manual; a green dependency update is evidence to review, not an automatic release.

The Playwright assurance workflow runs on pull requests, maintained-branch pushes,
release tags, manual dispatch and a weekly schedule. It checks unit tests, a
synthetic Chromium page, dependency consistency, undefined Python names, known
runtime dependency vulnerabilities, CodeQL for Python and Actions, and the built
container. GitHub schedules use UTC and execute the default branch's workflow.

Bandit records medium and high findings and rejects high-severity findings.
CodeQL and Trivy findings are available in GitHub code scanning. Image publication
rejects fixable HIGH/CRITICAL vulnerabilities; unresolved lower-severity and
unfixed findings remain visible for review. Scanner success does not establish
that an application is vulnerability-free.

Only release-tag runs and explicit manual requests from the default branch can
publish. The publishing job waits for every validation job and verifies that the
commit is already integrated into the default branch. It publishes the exact
scanned image through a one-day internal workflow artifact, without rebuilding.
Ordinary PR and scheduled runs do not store image archives or publish packages.

The Playwright image uses a separate `billcollector-playwright` GHCR package.
It does not overwrite the Selenium package currently used by production.
Registry credentials and package-write permissions exist only in the publication job.
All external actions are pinned to immutable commits and maintained by Dependabot.

Portable workflow/configuration changes can be proposed upstream independently
of fork-specific branch rules, package naming, and release settings. No workflow
uses a real vault, portal account, shared database, or document output mount.
