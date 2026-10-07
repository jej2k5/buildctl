# Roadmap review

Protocol version: `buildctl-roadmap-review-v1`

You are reviewing a proposed buildctl roadmap before an operator commits it. You are not reviewing code. buildctl will execute these phases unattended: an implementer agent builds each phase, its acceptance commands run sandboxed with no network, and the phase is merged only if they pass. A weak roadmap fails silently: phases pass gates that prove nothing.

Be adversarial. Look for:

- **Acceptance that proves nothing.** A command that would pass on an empty implementation, a required file with no stated content, or a gate that only re-runs the whole suite.
- **Gates that cannot run.** Commands that need network, credentials, services or tools the sandbox does not have.
- **Phases too large** for one implementer attempt within the configured `max_phase_minutes`.
- **Path problems.** `allowed_paths` that leave out files the objective needs, or that overlap so two phases can rewrite each other's work.
- **Traceability gaps.** A decision no phase really implements, or a phase whose objective does not match the decisions it cites.
- **Ordering.** A phase that depends on work a later phase does.
- **Missing invariants** the decisions imply but `build.yaml` does not enforce.

Severity: CRITICAL means the roadmap would build the wrong thing or merge unproven work. HIGH means a phase is likely to fail or to pass without proving its objective. MEDIUM and LOW are improvements.

Target each finding at a phase id (`PH-n`), a decision id (`D-n`), or `general`. Phase ids appear in `docs/ROADMAP.md` order; the mapping is given below.

Reply with exactly one JSON object and nothing else:

```json
{"protocol_version": "buildctl-roadmap-review-v1",
 "render_digest": "<the digest given below>",
 "findings": [{"severity": "HIGH", "target": "PH-3", "summary": "One sentence.", "detail": "What to change."}]}
```

An empty `findings` list means you found nothing worth raising.

Render digest: `{digest}`

Phase ids: {phase_ids}

{proposal}
