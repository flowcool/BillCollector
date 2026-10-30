## Behavior

Describe the problem and resulting behavior. Keep this PR focused on one change.

## Verification

- Command:
- Expected result:
- Observed result:

Use synthetic credentials and isolated state. Do not attach private logs, browser
profiles, screenshots containing credentials, or downloaded customer documents.

## Infra handoff

For an Infra-facing release, complete the [runtime contract handoff](https://github.com/flowcool/BillCollector/blob/playwright-next/doc/INFRA_RUNTIME_CONTRACT.md#required-infra-handoff)
in the owning Infra bead: image@digest, schema SHA-256, migrations, required
Settings/environment and contract deltas. State the daemon qualification path,
each bypass, and the interface it does not qualify. For other changes, state N/A.

## AI assistance

State whether AI assisted this change and what the author verified.

## Pullfrog review

Pullfrog is the sole reviewer. Require a completed review of the final head,
resolve actionable threads and keep all CI checks green. Ignore CodeRabbit and
the known sticky `pullfrog-approval` status only. PRs that change only
`*.md` files need CI only.

Review evidence: <!-- completed review URL and reviewed commit SHA -->
