# Phase 12: Delivery replay

## Objective

Build replay of past events to an endpoint over a time range, reusing the normal delivery path and its guarantees.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_12.md` records the summary, tests, decisions, known debt and the next action.
