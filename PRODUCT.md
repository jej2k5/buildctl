# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

The operator UI ships as two surfaces over one backend: a local web console (`buildctl ui`, browser on 127.0.0.1) and a terminal TUI. The web console is the `web` platform; the TUI is a terminal surface with its own constraints (monospace grid, 16/256/truecolor terminals, keyboard only).

## Stack

Delegated: the core package stays dependency-free (`dependencies = []`). The UI ships as a separate distribution, `buildctl-ui` (package `buildctl_ui/`), never inside `buildctl/`. `trust init` digests every file under `buildctl/` (`kernel.kernel_files`), so UI code in the kernel would invalidate trust on every UI edit. The web console is static, vanilla HTML/CSS/JS with no build step, served by the standard library's `http.server`. The TUI uses Textual, a dependency of `buildctl-ui` only. Both surfaces read state through kernel read functions and perform every mutation by invoking the `buildctl` CLI, so the kernel stays the only thing that changes state and the CLI, TUI and web console cannot disagree.

## Users

One operator: a senior engineer who wrote the roadmap (`build.yaml` plus phase prompts) and is accountable for what lands on `main`. They run builds of tens of phases that take hours to days, unattended for long stretches. They come to the UI in four situations:

1. **Setting up**: writing config, then working through trust, isolation and auth until `bootstrap` succeeds.
2. **Watching a run**: which phase and step it's on, which agent call is in flight, and what it has spent today against the cap.
3. **Handling an escalation**: buildctl stopped with a code. They need to know why, read the findings, diff or debate request, and act.
4. **Reviewing history**: what happened across phases, including remediation loops, debates, cost per phase and the audit chain.

## Product Purpose

buildctl is a deterministic, restart-safe supervisor for agent-built software. One agent (Codex) implements each phase, another (Claude) reviews it adversarially, acceptance commands run sandboxed with no network, and only reviewed, allowed bytes are committed. buildctl owns state, git history and budget; agents never touch them.

The UI exists because today every answer comes back as JSON on stdout and every action needs a nonce copied out of `status`. Success means the operator understands why a build stopped, and acts on it correctly, in under a minute without reading raw JSON, while every safety property of the CLI still holds.

## Positioning

buildctl isn't a CI dashboard or an agent chat. Its distinctive claim is **governed autonomy**: the human approves only what matters, and everything that crosses into the repository is checked, pinned and recorded in a hash-chained audit log. The UI has to make that governance visible and legible (trust anchors, nonces, reviewed-vs-committed tree hashes, the budget ledger), not hide it behind convenience.

## Operating Context

- Runs on macOS only (agents run under `sandbox-exec`) against a local Git repository. State lives outside the repo in `$XDG_STATE_HOME/buildctl/<hash>` or `BUILDCTL_STATE_DIR`.
- Real data the UI renders: `BuildState` (status, generation, current phase, phase_step, attempts per counter, escalation code and detail, pending approval with nonce, extra-attempt grant, operations with status `pending|started|completed|failed|unknown`), `audit.jsonl` (hash-chained events such as `phase_started`, `operation_started/completed/failed`, `review_reduced`, `findings_deferred`, `human_escalation`, `checkpoint_approved`, `phase_committed`, `phase_completed`), the budget ledger, the trust anchor (governance SHA, snapshot digest, package digest, executable hashes), the isolation conformance record (24h freshness), per-phase artifacts (`PHASE_RESULT`, `REVIEW_RESULT` with CRITICAL/HIGH/MEDIUM/LOW findings, `DEBATE_REQUEST`, ADRs, gate results).
- Escalation codes are a fixed vocabulary (CHECKPOINT_REQUIRED, REVIEW_BLOCKED, UNREVIEWABLE_DELTA, DEBATE_UNRESOLVED, *_EXHAUSTED, UNKNOWN_EXTERNAL_OPERATION, OPERATION_TIMEOUT_UNKNOWN, STALE_RUN_UNKNOWN_OPERATION, AUTHENTICATION_REQUIRED, DAILY_BUDGET_UNAVAILABLE, ADVANCE_PRECONDITIONS_UNMET, AGENT_OPERATION_FAILED, PHASE_DEADLINE_EXCEEDED, DELTA_CHANGED_AFTER_REVIEW), each with a documented remedy in README.md.
- Long runs: the operator glances at the UI between other work, often hours apart, and sometimes after a crash or restart.

## Capabilities and Constraints

- **Full control**: the UI can perform every CLI action: start, run --until, phase, pause, resume, approve, retry, retry with an extra attempt, reconcile, trust init and show, isolation verify, auth check, bootstrap and validate.
- **Ceremony is preserved, not removed.** Trust-critical actions (trust init, approve, authorizing an extra attempt, reconcile accept-output) show what is being pinned or approved (commit, diff, findings, tree hash) and need a typed confirmation of the evidence (the tree-hash prefix for an approval, the SHA prefix for trust init, the phase number for an extra attempt, the operation kind for reconcile). The UI submits the nonce it displayed, so the kernel refuses an approval made against stale state; typing a nonce shown on the same screen would be theater. Runs (`start`, `run`, `phase`) are spawned detached, so closing the UI never stops a build. Governed files are edited in the operator's editor, not in the UI.
- **Local only**: the web server binds to 127.0.0.1 and requires a per-launch token (printed or opened by `buildctl ui`); state-changing requests are rejected without it.
- A single supervisor lock means only one actor mutates state at a time. The UI must show when the CLI or another session holds the lock, and must not offer actions the state machine will refuse.
- Secrets: all provider diagnostics are already redacted (`redact`); the UI must not show unredacted agent output beyond what buildctl stores.
- Terminology to keep exactly: phase, step, attempt, generation, escalation, checkpoint, nonce, grant, reconcile, trust anchor, governed files, isolation conformance, integration branch.

## Brand Commitments

- Name: `buildctl`, lowercase, monospace in prose.
- README voice: plain, declarative, short sentences, says exactly what happens and what to do. No marketing language. UI copy follows it.

## Evidence on Hand

- README.md (full command, escalation and configuration reference).
- `tests/fixtures/target/` is a complete 24-phase configuration for Relay, a fictional webhook delivery service, with phase prompts and governance documents. The console demo runs on it.
- No screenshots, users, metrics or testimonials exist. Don't invent any.

## Product Principles

1. **Governance is the feature.** Make trust, review and budget state more visible than progress bars.
2. **The stop is the product moment.** The UI earns its keep when buildctl halts: explain the code, show the evidence, and offer exactly the actions the state machine will accept.
3. **One source of truth.** The UI never derives state the supervisor doesn't record; the CLI, TUI and web console must agree byte for byte.
4. **Friction where it protects.** Fast for observing; deliberate for pinning, approving and spending.
5. **Restart-honest.** Unknown is a first-class state. Never paper over an operation whose outcome isn't known.

## Accessibility & Inclusion

Keyboard-complete on both surfaces. Severity and status are never conveyed by color alone, so they stay readable on 16-color terminals and under color-vision deficiency. Web console targets WCAG 2.2 AA.
