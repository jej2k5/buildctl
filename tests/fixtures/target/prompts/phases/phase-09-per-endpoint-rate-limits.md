# Phase 9: Per-endpoint rate limits

## Objective

Build per-endpoint rate limits that defer delivery instead of dropping it.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_9.md` records the summary, tests, decisions, known debt and the next action.
