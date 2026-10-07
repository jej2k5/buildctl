## When it stops {#stops}

buildctl stops with an escalation code whenever it cannot continue safely. `buildctl status` and the console's Stop view show the code and, where relevant, the nonce the fix needs. Each code below says what happened and what clears it.

The signal shapes match the console: a diamond waits for you, a square is a stop, a dashed circle is an outcome nobody knows yet.

{.stop-index}

### Waiting for you {#stops-waiting}

#### CHECKPOINT_REQUIRED {#CHECKPOINT_REQUIRED signal=caution}

A phase in `checkpoints.before_phases`, a `merge_policy: human` merge, or the MVP milestone needs your approval.

**Clear it:** review the evidence, then approve. `buildctl approve --nonce <n>`, with the nonce from `status`. In the console, the Approve lever asks you to type the first 7 characters of the tree hash you reviewed. An approval made against state that has since moved is refused.

### Stopped {#stops-stopped}

#### REVIEW_BLOCKED {#REVIEW_BLOCKED signal=danger}

The reviewer could not clear the phase.

**Clear it:** read the findings in the phase's artifacts directory, adjust the phase prompt or decide, then `buildctl retry`.

#### UNREVIEWABLE_DELTA {#UNREVIEWABLE_DELTA signal=danger}

The change has binary or oversized files the reviewer cannot read.

**Clear it:** adjust the phase so its output is reviewable, then `buildctl retry`.

#### DEBATE_UNRESOLVED {#DEBATE_UNRESOLVED signal=danger}

The debate ended without agreement.

**Clear it:** decide the question, adjust the phase prompt, then `buildctl retry`.

With `roles.arbiter.mode: interactive`, a debate writes `DEBATE_REQUEST.json` to the phase's artifacts directory and stops. It contains the findings, the evidence digest and the exact output format. Run the debate (for example with `agent-debate`), write the result to the `output_path` the request names, then `buildctl retry`. A result that agrees becomes an ADR in `decisions/` and is implemented.

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

An agent call failed. The status detail has a redacted reason.

**Clear it:** fix the cause, then `buildctl retry`.

#### PHASE_DEADLINE_EXCEEDED {#PHASE_DEADLINE_EXCEEDED signal=danger}

The phase ran past `max_phase_minutes`.

**Clear it:** `buildctl retry`. A retry starts a new attempt with a fresh deadline.

#### DELTA_CHANGED_AFTER_REVIEW {#DELTA_CHANGED_AFTER_REVIEW signal=danger}

What would be committed differs from what was reviewed. This should never happen.

**Clear it:** investigate before retrying. Nothing was committed.

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
