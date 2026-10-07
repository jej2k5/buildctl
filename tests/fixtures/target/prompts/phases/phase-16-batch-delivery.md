# Phase 16: Batch delivery

## Objective

Build optional batching of events into one request per endpoint, with a size and time bound.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_16.md` records the summary, tests, decisions, known debt and the next action.
