# Phase 23: Operations runbook

## Objective

Build an operations runbook covering upgrades, backups, failover and dead-letter recovery, checked by a script that runs each documented command.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_23.md` records the summary, tests, decisions, known debt and the next action.
