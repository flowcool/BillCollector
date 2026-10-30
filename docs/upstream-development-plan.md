# BillCollector upstream development plan

## Current project boundary

This repository owns the BillCollector engine and its engine contract. The
Playwright provider recipes are owned by
[`flowcool/billcollector-recipes`](https://github.com/flowcool/billcollector-recipes).
Deployment, task configuration, schedules, vault items, Paperless and live
provider acceptance are owned by Infra. This plan records engine and upstream
work only; it is not a recipe queue or a deployment runbook.

The Bourse Direct delivery and source documents below are retained as historical
evidence. Their operational instructions are superseded by the engine runtime
contract and the recipe catalog's current provider document. Do not use their
old bundled recipe path, engine pin or month bound for a new rollout.

This file defines scope, sequence and contribution boundaries. Beads owns live
execution state. GitHub issues own maintainer agreements. Do not infer approval,
integration or deployment from a historical plan.

The [2026-10-28 reconciliation](upstream-reconciliation-20261028.md) verifies
public date/TZ integration and records retired prototype PRs. Contributor guides
landed later at public `484048d03b86500c9abdb548ef052763050630e4`.
The [2026-10-29 reconciliation](upstream-reconciliation-20261029.md) verifies
input-triggered login PR #39 and runtime-directory PR #42 at public `ad18f7a`.
The [2026-10-30 reconciliation](upstream-reconciliation-20261030.md) verifies
calendar PR #40 at public `f26b2a9` and the local fork synchronization.
Its evening addendum verifies notice PR #41 at public `e83c8e2` and keeps
archive receipts separate. The [correction inventory](upstream-corrections-20261030.yaml)
records the disposition of the October 30 fork changes.
Beads owns the remaining gates and the submitted C3 contribution.

The fork includes runtime dates/TZ and C3 validation through
[fork PR #60](https://github.com/flowcool/BillCollector/pull/60). Tested contributor
guides are integrated through [fork PR #61](https://github.com/flowcool/BillCollector/pull/61).
The guides are also integrated into public upstream `main` through the
[maintainer's PR #35 receipt](https://github.com/s-t-e-f-a-n/BillCollector/pull/35#issuecomment-6474460727).
C3 retains its separate upstream integration boundary.

The Bourse Direct study concerns daily notice acquisition for the sibling
Ghostfolio importer. Use the [delivery plan](bourse-direct-delivery-plan.md)
and its [draft behavior spec](specs/bourse-direct-acquisition.md).
The [source inventory](bourse-direct-preparation.yaml) records recovered portal
facts and proof. A new portal capture is not a prerequisite for offline work.
The three generic extensions now have local implementations and submitted
upstream PRs #39-#41. All three generic extensions are now integrated in public
main. The provider adapter and archive receipt/source-evidence contract remain
separate. Beads holds the current verification state.

## Contribution lanes

| Owner | Deliverable | Remaining completion boundary |
| --- | --- | --- |
| `infra-zlth.21` | Chromium sandbox contribution, upstream PR #32 | Public integration verified at `93bfcc852a7ad36edbd3a9bbd359f5aead36cfb6`; maintainer container qualification reported. No remaining contribution boundary. |
| `infra-zlth.69` | Independent onboarding switches, upstream PR #31 | Public source and acceptance test match the candidate. No remaining contribution boundary. |
| `infra-zlth.70` | Five-field cron correction, upstream PR #30 | Exact accepted example is public. No remaining contribution boundary. |
| `infra-zlth.68` | Crash recovery and collision-safe publication, upstream PR #33 | Public integration is verified at `511662772ec294c981966912e2155fe5bdf86493`. No remaining contribution boundary. |
| `infra-zlth.64` | Runtime date variables and user TZ, upstream PR #34 | Public integration verified at `6bb33486a57ed64c81ef58e9524e2358897c76fd`; no remaining contribution boundary. |
| `infra-zlth.71` | C3 method/argument validation, upstream PR #44 | Fork PR #60 is delivered. Focused upstream PR [#44](https://github.com/s-t-e-f-a-n/BillCollector/pull/44) published head `9018880` includes public notice compatibility and awaits maintainer review; full pinned and legacy acceptance is recorded. |
| `infra-zlth.72` | Contributor guides, upstream PR #35 | Public integration verified at `484048d03b86500c9abdb548ef052763050630e4`. The maintainer adapted the timezone and shipped-feature wording. No remaining contribution boundary. |
| `infra-zlth.52` | Bourse Direct recipe and compatibility proof | Local recipe and two real-source passes are delivered. Interface proposal [#51](https://github.com/s-t-e-f-a-n/BillCollector/issues/51) awaits maintainer agreement; recipe acceptance remains separate. |
| `infra-zlth.73` | Input-triggered protected login, upstream issue #36 / PR #39 | Public integration verified at `ad18f7a85d70079072ed9b7ffde6bfff22f56c74`; issue #36 closed with contract agreed and shipped. No remaining contribution boundary. |
| `infra-zlth.74` | Replacement-calendar traversal, upstream issue #37 / PR #40 | Public integration verified at `f26b2a9d04bd89b45260b5ba9b13e90418b912d5`; implementation and tests match PR #40 exactly. No remaining contribution boundary. |
| `infra-zlth.78` | Archive receipts and optional source evidence | Dedicated proposal [#52](https://github.com/s-t-e-f-a-n/BillCollector/issues/52) is published. Retained qualification does not replace maintainer interface agreement. |
| `infra-zlth.75` | Notice identity and revisions, upstream issue #38 / PR #41 | Collector identity is integrated at public `e83c8e2`; PR #41 and issue #38 are closed. The maintainer now assigns opt-in repeat suppression to the collector. Collector acceptance is complete; archive receipts and optional source evidence continue independently in `infra-zlth.78`. |

The October 24 ready-only batch has been followed by later explicit instructions
for dates/TZ, C3, guides and Bourse Direct study. Do not apply that old batch's
implementation hold to work already authorized later. This documentation task
adds no new engine implementation or live-operation authority.

Closed fork PRs #50-#52 remain sources of prior recovery, sandbox and onboarding proof.
Do not use their old snapshot branches as a current upstream base. Fork #53
installs Pullfrog and is not an upstream feature contribution.

October 30 retained-fix submissions: `infra-zlth.79`–`.82` own upstream PRs #53–#56 (numeric cron, persistent jobs, exact vault/transport, terminal notifications). `infra-zlth.83` owns incremental/ZIP proposal #50. All wait for maintainer review or agreement. See the [publication receipt](upstream-reconciliation-20261030.md#upstream-publication-receipt).

## Bourse Direct sequence

1. Track the input-triggered login extension in issue #36 / PR #39, calendar
   traversal in #37 / PR #40, and notice identity/revisions in #38 / PR #41.
   Local implementation and submission followed later explicit operator GO.
   Upstream contract acceptance remains a maintainer decision.
2. Preserve the completed local recipe and two real-source qualification passes
   recorded in `.52` and the source inventory. They prove the local candidate,
   not the public upstream candidate or an installed production instance.
3. Reconcile the recipe with the eventual public interfaces. Issue #36 confirms
   input-triggered login agreed and shipped through PR #39; #37 and PR #40
   now provide the public replacement-calendar interface. Issue #38 keeps
   raw-byte deduplication in the collector and assigns notice identity/revisions
   to the archive in its earlier decision. The October 30 decision supersedes
   that collector boundary: opt-in notice identity is now public at `e83c8e2`,
   and PR #41 is closed. The archive receipt/source-evidence proposal remains
   unanswered and needs a dedicated issue. Provider adapter and delivery-policy
   interfaces are still absent upstream. Public `6bb3348` satisfies
   the date prerequisite for calendar PR #40, now public at `f26b2a9`.
   Login #39 is public at `ad18f7a`.
4. Qualify real collection and Paperless delivery in the existing Infra owners.
   Join the importer through its existing approved OpenSpec and acceptance work.

The sibling contains observed source, anonymized fixtures and private originals.
Do not ask for those facts again before checking the recorded inventory.
Historical captures do not prove current live-site behavior or authorize login.

## Source reconciliation boundaries

Compare the public upstream tree with each focused contribution, and separately
with the fork default branch `playwright-next`. The engine checkout's local
`main` is the Selenium instance; it is not the fork's Playwright development tip.

Public snapshot `e83c8e2` adds opt-in notice identity PR #41 to recovery PR #33, sandbox PR #32, dates/TZ
PR #34, guides PR #35, runtime-directory fix PR #42, input-triggered login
PR #39 and replacement calendars PR #40. Published fork `886eba1` includes
the October 30 local changes through PRs #71-#80; see the correction inventory.
Its earlier `dfa86c7` synchronization includes
the October 30 synchronization through
[fork PR #70](https://github.com/flowcool/BillCollector/pull/70), and
date integration through
[fork PR #69](https://github.com/flowcool/BillCollector/pull/69), recovery/sandbox through
[fork PR #67](https://github.com/flowcool/BillCollector/pull/67) and CI cost
changes through #68. It already contains dates, C3, guides, input-triggered
login and calendars. PR #69 imports the doctor's instruction to preserve date
placeholders. The October 28 reconciliation records source verification, CI
and Pullfrog review. The October 30 synchronization imports the runtime-path
fix and current public documentation while preserving fork behavior. PR #70
is merged after exact-head Pullfrog review and green required CI. Its merge tree
matches the locally tested candidate. The dated reconciliation records proof.
Beads holds exact source pointers, proof and publication state. This work does
not change a deployed instance.

## Upstream agreements and publication

- [#20](https://github.com/s-t-e-f-a-n/BillCollector/issues/20) and [#36](https://github.com/s-t-e-f-a-n/BillCollector/issues/36#issuecomment-6449977765): phased login; the bounded fill-submit extension is agreed and shipped through PR #39; exact public source verified at `ad18f7a`.
- [#37](https://github.com/s-t-e-f-a-n/BillCollector/issues/37) and [PR #40 receipt](https://github.com/s-t-e-f-a-n/BillCollector/pull/40#issuecomment-6509093900): replacement-calendar interface accepted and publicly integrated at `f26b2a9`.
- [#38 current decision](https://github.com/s-t-e-f-a-n/BillCollector/issues/38#issuecomment-6531165804): the collector owns opt-in source/revision repeat suppression; PR #41 is integrated at `e83c8e2`. Archive receipt and source-evidence proposals need a separate issue.
- [#26](https://github.com/s-t-e-f-a-n/BillCollector/issues/26#issuecomment-6385845191): accepted dates, task window and timezone contract.
- [#27 C3](https://github.com/s-t-e-f-a-n/BillCollector/issues/27#issuecomment-6388757576): accepted method-validation contract.
- [#24](https://github.com/s-t-e-f-a-n/BillCollector/issues/24): crash recovery and publication.

Target upstream public `main`. Fetch and record its current SHA before code and
publication. The sanitized mirror can replace history. Compare public content,
not ancestry alone. Reconcile the focused contribution without restoring retired
files. One behavior per PR; declare AI assistance and reproducible proof.
An upstream PR's approval or closure is not exact public integration evidence.
The maintainer closes delivered PRs without GitHub merges. Verify the receipt's
public commit and exact source. Issue #25 records this publication routine; its
closure does not establish co-maintainership.

## Maintainer-owned work and operational boundaries

Keep Stefan's storage, noVNC, notification-default, example-account and run-result
decisions separate, as assigned in issue #27. Provider-specific changes must be
coordinated with his existing work. Do not replace generic session, redaction,
login or collection mechanisms that already exist. The official container
proposal remains separate under issue #21.

Use isolated databases, profiles, downloads and loopback portals for engine
proof. Reuse retained private evidence read-only. No live vault, provider,
Paperless or Ghostfolio call is needed for the proposed offline work.
Run affected browser/unit tests and applicable legacy regression on the exact
candidate. Preserve retries, timeouts, optional TOTP, graceful behavior and
current result semantics outside agreed changes.

Upstream shared-interface acceptance remains separate from fork work that
Florent explicitly authorized later. Publication, upstream merge, release,
production migration and financial writes retain their own authority. Code
rollback is a focused revert. Real-data conversion or restoration requires its
reviewed operational recovery path.
