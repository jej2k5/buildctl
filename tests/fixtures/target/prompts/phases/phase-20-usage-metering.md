# Phase 20: Usage metering

## Objective

Build per-tenant usage metering of events accepted and deliveries attempted.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_20.md` records the summary, tests, decisions, known debt and the next action.
