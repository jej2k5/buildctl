# Phase 22: Self-serve onboarding

## Objective

Build a guided first-run flow in the admin API: create a tenant, register an endpoint, send a test event.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_22.md` records the summary, tests, decisions, known debt and the next action.
