# Phase 4: Durable event store

## Objective

Build an append-only event store: events are written once, never edited, and every delivery attempt references an event by id.

## Constraints

Read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and the previous phase handoff first. Keep every invariant in `build.yaml`. Change only the paths this phase allows.

## Acceptance

- Tests under `tests/product/` cover the new behaviour, including its failure paths.
- `python -m unittest discover -s tests/product` passes with no network.
- `docs/HANDOFF_PHASE_4.md` records the summary, tests, decisions, known debt and the next action.
