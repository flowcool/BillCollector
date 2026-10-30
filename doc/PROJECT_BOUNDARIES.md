# Project boundaries

This repository owns the BillCollector engine, generic actions, scheduler,
vault and TOTP handling, delivery behavior, recipe schema and engine contract.

The recipe repository owns every provider recipe, including Bourse Direct,
recipe metadata, catalog validation, catalog releases and provider recipe
qualification. Its current Bourse Direct document is
`docs/bourse-direct.md`; its exported recipe is
`recipe-pw__bourse_direct.yaml`.

Infra owns image and recipe pins, deployment, task configuration, schedules,
vault items, mounts, Paperless workflows and live provider acceptance. The
engine runtime contract is the interface for that handoff.

`recipes-repo/` in this checkout is a read-only reference copy. It is not the
working tree for recipe changes. `archive/` and retained `tmp/` proof artifacts
are historical evidence. Remove a temporary artifact only when its proof is
already retained elsewhere and the owning Bead records the decision.

Provider-specific engine adapters remain where the engine contract requires
them. New engine changes must add a generic option or primitive when the need
is reusable; they must not add a new provider branch to solve a catalog-only
requirement.

Current provider-specific engine paths are deliberate adapter boundaries:

- `apps/billcollector/bourse_direct.py` implements the native Bourse Direct
  action and its portal identity checks.
- `apps/billcollector/notice.py` keeps the provider notice identity and
  revision policy used by that adapter.
- `apps/billcollector/bourse_direct_delivery.py` protects the provider's
  delivery acknowledgement and recovery rule.

These paths stay engine-owned because they execute the engine interface. The
catalog owns selectors, history limits and provider recipe metadata. A future
request that only changes those catalog concerns belongs in the recipe
repository; a reusable engine capability belongs in a generic module and
setting.
