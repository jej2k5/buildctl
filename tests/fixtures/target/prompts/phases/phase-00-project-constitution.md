# Phase 0: Project Constitution

## Objective

Write the project constitution for Relay, a self-hosted webhook delivery service. Record the domain glossary, ADR conventions, MVP scope, security requirements, API contract principles, operational boundaries and the delivery guarantee. Do not write application code.

## Global constraints

Before writing, read `docs/ARCHITECTURE_PRINCIPLES.md`, `docs/API_CONTRACT.md` and `docs/DECISIONS.md`.

## Deliverables and permitted paths

This phase is documentation only. You may create or modify files **only** under `docs/constitution/` and the single file `docs/HANDOFF_PHASE_0.md`. Any change outside those paths will be rejected. Do not modify governance files, the existing documents at the root of `docs/`, `prompts/`, `templates/`, `AGENTS.md` or `CLAUDE.md`.

Create every one of these files, each a substantial Markdown document beginning with a `#` heading:

- `docs/constitution/README.md` — how the constitution is organised and how it is amended
- `docs/constitution/glossary.md` — domain glossary
- `docs/constitution/adr-conventions.md` — ADR format, numbering, and lifecycle
- `docs/constitution/mvp-definition.md` — explicit MVP scope and non-goals
- `docs/constitution/security.md` — security, tenancy, and secret-handling requirements
- `docs/constitution/api-contract-principles.md` — versioned API contract principles
- `docs/constitution/operational-boundaries.md` — what Relay operates and what it leaves to the host
- `docs/constitution/delivery-guarantees.md` — the at-least-once delivery guarantee and its limits

Finally create `docs/HANDOFF_PHASE_0.md` containing an implementation summary, intentional omissions, tests, decisions, known debt, files changed, and the next action. It must include the words "summary", "test", "decision", and "next".

This phase writes no application code and no test code; the acceptance gate for Phase 0 checks documentation only.
