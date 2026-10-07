# Phase 2: Versioned API contract

## Objective

Build the versioned public API contract as schemas under `src/relay/contract/`, with a frozen-version check that fails when a published version changes.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_2.md` records the summary, tests, decisions, known debt and the next action.
