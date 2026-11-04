## When it stops {#stops}

buildctl stops with an escalation code whenever it cannot continue safely. `buildctl status` and the console's Stop view show the code and, where relevant, the nonce the fix needs. Each code below says what happened and what clears it.

The signal shapes match the console. A diamond is a wait buildctl planned for: nothing failed. A square is a stop: something failed, or a limit was hit and needs your grant. A dashed circle is an outcome nobody knows yet.

{.stop-index}

### Waiting for you {#stops-waiting}

#### CHECKPOINT_REQUIRED {#CHECKPOINT_REQUIRED signal=caution}

A phase in `checkpoints.before_phases`, a `merge_policy: human` merge, or the MVP milestone needs your approval.

**Clear it:** review the evidence, then approve. `buildctl approve --nonce <n>`, with the nonce from `status`. In the console, the Approve lever asks you to type the first 7 characters of the tree hash you reviewed. An approval made against state that has since moved is refused.

#### DEBATE_REQUIRES_HUMAN {#DEBATE_REQUIRES_HUMAN signal=caution}

With `roles.arbiter.mode: interactive`, the review routed the phase to a debate, and the debate is yours to run. buildctl wrote `DEBATE_REQUEST.json`: the strategic findings, the evidence digest, the exact output format and the `output_path` to write the result to. The status detail is the request's path, in the phase's `artifacts/phase-N/` directory.

**Clear it:** run the debate (for example with `agent-debate`), write the result to the request's `output_path`, then `buildctl retry`. A result that agrees becomes an ADR in `decisions/` and is implemented. One that does not stops at [DEBATE_UNRESOLVED](#DEBATE_UNRESOLVED).

This stop is a wait, not a failure: it does not count toward `max_consecutive_failures`. The debate call is still charged its configured `operation_cost_usd.debate` in the budget ledger. The Ledger lists the call as failed until the result is consumed.

#### DEBATE_AWAITING_DECISION {#DEBATE_AWAITING_DECISION signal=caution}

With `roles.arbiter.mode: debate`, Codex and Claude [debated](#debates) the findings for `limits.max_debate_turns` turns and neither accepted the other's proposal. The decision is yours.

**Clear it:** read the debate in the console's Stop view, then answer. In the console, the Decide lever offers three answers. On the command line, `buildctl debate decide --nonce <n> --answer <answer>`, with the nonce from `status`:

- `finding-stands`: the findings stand, and Codex makes the changes they require.
- `code-stands`: the code stands. The findings close as `operator_accepted`, and the phase is reviewed once more before it commits.
- `own`, with `--text "<your ruling>"`: Codex implements your ruling.

The same attempt resumes, with a fresh phase deadline, and nothing is re-trusted. A ruling becomes an ADR marked `Decided by: operator`, never presented as an agent agreement. `retry` instead discards the debate along with the attempt.

This stop is a wait, not a failure: it does not count toward `max_consecutive_failures`.

### Stopped {#stops-stopped}

#### REVIEW_BLOCKED {#REVIEW_BLOCKED signal=danger}

The reviewer could not clear the phase.

**Clear it:** read the findings in `artifacts/phase-N/REVIEW_RESULT-<n>.json` in the [state directory](#state), or in the console's Stop view. Adjust the phase prompt or decide the question, then `buildctl retry`.

#### UNREVIEWABLE_DELTA {#UNREVIEWABLE_DELTA signal=danger}

The change has binary or oversized files the reviewer cannot read.

**Clear it:** adjust the phase so its output is reviewable, then `buildctl retry`.

#### DEBATE_UNRESOLVED {#DEBATE_UNRESOLVED signal=danger}

The debate ended without agreement. With `roles.arbiter.mode: debate`, this never happens: an unsettled debate waits at [DEBATE_AWAITING_DECISION](#DEBATE_AWAITING_DECISION) instead.

**Clear it:** decide the question, adjust the phase prompt, then `buildctl retry`.

#### STALE_DEBATE_RESULT {#STALE_DEBATE_RESULT signal=danger}

The debate result was written for a different phase or base commit.

**Clear it:** run the debate again for the current request, then `buildctl retry`.

#### AUTHENTICATION_REQUIRED {#AUTHENTICATION_REQUIRED signal=danger}

An agent CLI is logged out.

**Clear it:** log in (`codex login` or `claude auth login`), then `buildctl retry`.

#### DAILY_BUDGET_UNAVAILABLE {#DAILY_BUDGET_UNAVAILABLE signal=danger}

The next agent call would exceed `max_daily_cost_usd`.

**Clear it:** wait for tomorrow (UTC), or raise the limit and trust the new commit, then `buildctl retry`.

#### ADVANCE_PRECONDITIONS_UNMET {#ADVANCE_PRECONDITIONS_UNMET signal=danger}

The phase passed review, but the budget ledger or a required approval no longer checks out.

**Clear it:** fix what the detail names, then `buildctl retry`.

#### AGENT_OPERATION_FAILED {#AGENT_OPERATION_FAILED signal=danger}

An agent call failed. The status detail starts with a detail code, then a redacted reason. Find the code below. `<ROLE>` is `IMPLEMENTER`, `REVIEWER` or `ARBITER`. `<KIND>` is the role, or `DEBATE` for an interactive debate result.

**Clear it:** do what the detail code's row says, then `buildctl retry`.

{.anchors prefix=detail-}
| Detail code | What happened | Clear it |
|---|---|---|
| `<ROLE>_EXECUTABLE_UNAVAILABLE` | The agent CLI is not where trust pinned it, or not on `PATH`. | Reinstall it, run `trust init` again, then retry. |
| `<ROLE>_EXECUTABLE_INTEGRITY_FAILURE` | The agent CLI changed since `trust init`, usually an upgrade. | Run `trust init` again to pin the new binary, then retry. |
| `<ROLE>_AUTH_MODE_INVALID` | `roles.<role>.auth_mode` is not `environment`, `host_cli` or `none`. | Fix `build.yaml`, commit, trust the commit, then retry. |
| `<ROLE>_PROCESS_FAILED` | The agent CLI exited with an error. Its own message follows the colon. | Read that message: a rate limit, an expired login or a model error. Fix it, then retry. |
| `<ROLE>_SECRET_OUTPUT` | A credential appeared in the agent's output or result file, so nothing was stored. The detail names the source and the credential kind, never the value. | Rotate that credential. Find why the agent printed it, then retry. |
| `<ROLE>_MUTATED_WORKSPACE` | The workspace changed during a read-only call: a review, or either side's [debate](#debates) turn. | For the reviewer, check its command still limits it to `--tools Read,Grep,Glob`. For a Codex turn, the sandbox's read-only workspace failed: run `isolation verify`. Then retry. |
| `DEBATE_TURN_INVALID` | A debate turn broke the turn contract: no argument, a proposal that does not dispose of exactly the debated findings, or an acceptance of a proposal the other side never made. | Retry. If it repeats, look at the turn in What agents did. |
| `IMPLEMENTER_OUTPUT_SCHEMA_INVALID` | The implementer's `PHASE_RESULT` did not have the contract's fields. | Retry. If it repeats, check the phase prompt does not ask for another format. |
| `<KIND>_OUTPUT_MISSING` | The agent finished without writing a result. | Retry. If it repeats, check the agent command writes to `{output}` or prints its result. |
| `<KIND>_OUTPUT_EMPTY` | The result file was empty. | Retry. |
| `<KIND>_OUTPUT_INVALID_JSON` | The result was not valid JSON. The detail quotes the text near the error. | Retry. If it repeats, check the phase prompt does not ask for prose around the JSON. |
| `<KIND>_OUTPUT_NOT_OBJECT` | The result was JSON, but not an object. | Retry. |

A reviewer's malformed report never stops here: it is retried, and counts toward [REVIEW_FORMAT_ATTEMPTS_EXHAUSTED](#REVIEW_FORMAT_ATTEMPTS_EXHAUSTED).

#### PHASE_DEADLINE_EXCEEDED {#PHASE_DEADLINE_EXCEEDED signal=danger}

The phase ran past `max_phase_minutes`.

**Clear it:** `buildctl retry`. A retry starts a new attempt with a fresh deadline.

#### AGENT_RECORD_INCOMPLETE {#AGENT_RECORD_INCOMPLETE signal=danger}

Part of the phase went unrecorded, so buildctl will not commit it: a record could not be written (often a full disk), a stream ended with no final event, a supervisor crash left a record unclosed, or a record fails its internal chain. The agent is killed at once, which stops further spend. The flat amount reserved for the call stays booked, and the provider bills what it used. See [What agents did](#evidence).

**Clear it:** fix the cause in the detail, then `buildctl retry`. A retry starts a fresh attempt in a fresh workspace, and the earlier record stays in the receipt, labelled incomplete. If the phase had already committed and only the seal failed, the retry seals it and carries on.

#### DELTA_CHANGED_AFTER_REVIEW {#DELTA_CHANGED_AFTER_REVIEW signal=danger}

What would be committed differs from what the reviewer attested. This should never happen. Nothing was committed.

**Clear it:** investigate before retrying, because a retry starts a fresh attempt and the evidence moves on. In the [state directory](#state), the last `review_reduced` event in `audit.jsonl` holds the `delta_id` the reviewer saw, and `workspace` in `state.json` is the phase workspace that holds what would have been committed. Something changed one of them after review: a process writing into the workspace, or a different `allowed_paths`. Once you know which, `buildctl retry`.

#### STALE_RUN {#STALE_RUN signal=danger}

The last run died between agent calls.

**Clear it:** `buildctl retry`.

#### UNEXPECTED_FAILURE {#UNEXPECTED_FAILURE signal=danger}

The supervisor hit an error it does not classify.

**Clear it:** read the detail, fix the cause, then `buildctl retry`.

### Outcome unknown {#stops-unknown}

An agent call may or may not have finished. buildctl never guesses: you settle each unknown call before anything else runs.

#### UNKNOWN_EXTERNAL_OPERATION {#UNKNOWN_EXTERNAL_OPERATION signal=unknown}

An agent call was started and its outcome was never recorded.

**Clear it:** `buildctl reconcile --operation <id> --action accept-output` to keep the output it left, or `--action authorize-retry` to discard it and pay for a new call.

#### OPERATION_TIMEOUT_UNKNOWN {#OPERATION_TIMEOUT_UNKNOWN signal=unknown}

An agent call timed out, so its outcome is unknown.

**Clear it:** `buildctl reconcile --operation <id> --action accept-output` to keep the output it left, or `--action authorize-retry` to discard it and pay for a new call.

#### STALE_RUN_UNKNOWN_OPERATION {#STALE_RUN_UNKNOWN_OPERATION signal=unknown}

The last run died with agent calls in flight.

**Clear it:** reconcile each unknown call with `buildctl reconcile --operation <id> --action <accept-output or authorize-retry>`, then `buildctl retry`.

### Ceilings {#stops-ceilings}

A ceiling stops the build when a limit is hit. `buildctl retry --authorize-extra-attempt --nonce <n>` buys exactly one more pass past that ceiling, for that phase only, and the grant is spent on use. Or fix the cause first.

#### PHASE_ATTEMPTS_EXHAUSTED {#PHASE_ATTEMPTS_EXHAUSTED signal=danger}

A new attempt would exceed `max_phase_attempts`.

**Clear it:** `buildctl retry --authorize-extra-attempt --nonce <n>`, with the nonce from `status`, buys one more attempt. Or fix the cause first.

#### REMEDIATION_ATTEMPTS_EXHAUSTED {#REMEDIATION_ATTEMPTS_EXHAUSTED signal=danger}

Another remediation lap would exceed the phase's `max_remediation_attempts`.

**Clear it:** `buildctl retry --authorize-extra-attempt --nonce <n>`, with the nonce from `status`, buys one more lap. Or fix the cause first.

#### SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED {#SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED signal=danger}

Another review would exceed `max_semantic_review_attempts`.

**Clear it:** `buildctl retry --authorize-extra-attempt --nonce <n>`, with the nonce from `status`, buys one more review. Or fix the cause first.

#### REVIEW_FORMAT_ATTEMPTS_EXHAUSTED {#REVIEW_FORMAT_ATTEMPTS_EXHAUSTED signal=danger}

The reviewer's report was rejected as malformed `max_review_format_attempts` times. Each rejection's reason is in the audit log as `review_format_rejected`.

**Clear it:** read the reasons, fix the review prompt if it caused them, then `buildctl retry --authorize-extra-attempt --nonce <n>`, with the nonce from `status`.

#### MAX_CONSECUTIVE_FAILURES_EXHAUSTED {#MAX_CONSECUTIVE_FAILURES_EXHAUSTED signal=danger}

The build stopped `max_consecutive_failures` times in a row.

**Clear it:** fix what keeps failing, then `buildctl retry --authorize-extra-attempt --nonce <n>`, with the nonce from `status`. Authorizing it also resets the streak.
