## Reference {#reference}

### Commands {#commands}

Global flags: `--repo <path>` (must be the repository toplevel), `--dry-run`, and `--request-id <id>` for a caller that needs to know what its command did. See [Request identity](#request-identity).

{.wide .anchors prefix=cmd-}
| Command | What it does |
|---|---|
| `start` | Run every incomplete phase in order. |
| `run --until <N or milestone>` | Run up to phase N, or a milestone named in `build.yaml`. |
| `phase <N>` | Run one phase. Refused if it is already complete. |
| `status` | State, current phase, any pending approval or grant (with its nonce), and today's spend. |
| `pause` | Stop starting new work. |
| `resume` | Allow it again. |
| `approve --nonce <n>` | Approve the pending checkpoint. The nonce comes from `status`. |
| `debate decide --nonce <n> --answer <answer>` | Settle a debate the agents could not, at [DEBATE_AWAITING_DECISION](#DEBATE_AWAITING_DECISION). The answer is `finding-stands`, `code-stands` or `own`; `--text` carries your ruling (required for `own`) or a note. The nonce comes from `status`. The same attempt resumes. |
| `retry` | Try again after an ordinary failure. Starts a fresh attempt. `--authorize-extra-attempt --nonce <n>` buys exactly one more pass past an exhausted ceiling. |
| `reconcile --operation <id> --action <action>` | Settle an agent call whose outcome is unknown. The action is `accept-output` (keep what it left) or `authorize-retry` (discard it and pay for a new call). |
| `reopen --phase <n> --confirm <n>` | Take a completed phase back so a fresh attempt redoes it, for a phase recorded as done that was not. Nothing is reverted: the next attempt starts from the integration branch. Refused while later phases are complete or the build is not idle. |
| `trust init --to <sha> --confirm <sha>` | Pin the governed files at that commit, this buildctl, and both agent executables. |
| `trust show` | Show what is pinned. |
| `isolation verify` | Probe the sandbox and record that it passed. Runs refuse to start once that record is over 24 hours old. |
| `auth check` | Confirm both agent CLIs are logged in. |
| `bootstrap` | Create the build state. Requires fresh isolation conformance. |
| `validate` | Check `build.yaml`. |
| `inspect` | The full read model as one JSON document: state, phases, trust, isolation, budget, the stop and its meaning, and every action with the conditions that allow or lock it. Read-only. |
| `audit --after <seq>` | Audit events after a sequence number, as JSON. `--limit` caps how many. `--request <id>` keeps only the events that request caused. |
| `request show <id>` | What became of a `--request-id` request: `accepted`, or `terminal` with its outcome. Exits 1 with `not_accepted` if it never landed. Read-only; needs no trust. |
| `evidence show <commit>` | Print a commit's receipt: every attempt, each agent call and its actions. Works on any clone with the branch. |
| `evidence verify <commit>` | Check a commit's receipt on any clone: no sandbox, trust or state needed. Reports `verified`, `verified_with_flags`, `verified_by_tree`, `no_receipt` or `failed`, and what it did not prove. `--records <dir>` also checks full records against their hashes. |
| `evidence prune --phase <n> --confirm <n>` | Delete a phase's full records. Its receipt stays, but its hashes then show only that a record existed. Nothing prunes automatically. |

### Request identity {#request-identity}

An orchestrator that runs buildctl for you needs to know what its command did, even if its own process dies mid-call. Pass `--request-id <id>` (1 to 128 letters, digits, `.`, `_`, `:` or `-`) to any command that changes build state: `start`, `run`, `phase`, `pause`, `resume`, `retry`, `approve`, `debate decide`, `reconcile`, `reopen` and `evidence prune`. Other commands refuse the flag rather than ignore it, and the build must be bootstrapped first.

1. `request_accepted` is written under the state lock before the command does anything. No record means it never took effect, and resending the same id is safe.
2. Every audit event the command causes carries `request_id`.
3. `request_terminal` records the outcome when the command returns: `ok`, `escalated` (with the stop code) or `error`.
4. The same id with the same arguments never runs twice. buildctl prints `{"duplicate": true, "request": ...}` and exits 0 if it finished ok, 2 if it finished otherwise, or 3 if it is still accepted with no outcome. The same id with different arguments is refused.

The request record is authoritative; the exit code is the command's own. If buildctl cannot take the lock to record the outcome within 30 seconds, the command's result stands, the request stays `accepted`, and stderr says the outcome was not recorded.

Accepted with no terminal record means the command is still running, or was interrupted with an unknown outcome. Read `audit --request <id>` and reconcile; never replay it under a new id on that evidence alone. `inspect` reports the contract as `identity_contract.version` (currently 1), so a caller can refuse a buildctl without it.

### Configuration {#config}

{.wide .anchors prefix=config-}
| Key | Meaning |
|---|---|
| `limits.max_phase_attempts` | Attempts a phase may start before [PHASE_ATTEMPTS_EXHAUSTED](#PHASE_ATTEMPTS_EXHAUSTED). |
| `limits.max_review_format_attempts` | Malformed review reports tolerated before [REVIEW_FORMAT_ATTEMPTS_EXHAUSTED](#REVIEW_FORMAT_ATTEMPTS_EXHAUSTED). |
| `limits.max_semantic_review_attempts` | Reviews of one phase before [SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED](#SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED). |
| `limits.max_debate_rounds` | Debates one phase may hold. |
| `limits.max_debate_turns` | Turns in one debate when `roles.arbiter.mode` is `debate`, both sides together. Optional; default 6, at least 2. |
| `limits.max_phase_minutes` | Wall-clock limit for one attempt, before [PHASE_DEADLINE_EXCEEDED](#PHASE_DEADLINE_EXCEEDED). |
| `limits.max_daily_cost_usd` | The daily budget, in UTC days. |
| `limits.max_consecutive_failures` | Stops in a row before [MAX_CONSECUTIVE_FAILURES_EXHAUSTED](#MAX_CONSECUTIVE_FAILURES_EXHAUSTED). |
| `operation_cost_usd` | Estimated cost of each paid call (`implementation`, `review`, `remediation`, `debate`, and `debate_turn` when the arbiter's mode is `debate`), reserved before the call runs. |
| `governance_documents` | Files beyond the fixed governed set that `trust init` pins and agents may not modify. Required; may be `[]`. |
| `git.remote` | The remote phase branches are pushed to when `push` is on. |
| `git.push` | Push the phase branch instead of fast-forwarding the integration branch locally. |
| `git.create_pr` | Open a pull request for the pushed phase branch, with the `gh` CLI. |
| `git.merge` | Request a squash auto-merge of that pull request, for phases whose `merge_policy` is `auto`. |
| `git.integration_branch` | The branch fast-forwarded after each phase. Default `main`. Fast-forward only: if it has commits the phase lacks, buildctl stops instead of merging. |
| `roles.*.command` | The argv for each agent. `{prompt}` and `{output}` are substituted. While recording is on, `codex` needs `--json` and `claude` needs `--output-format stream-json --verbose`; see [Setting it up](#evidence). |
| `roles.*.network` | Whether the agent has network access in the sandbox. |
| `roles.*.auth_mode` | How the agent authenticates: `host_cli` (your CLI login; macOS only), `environment` (the named variables) or `none`. |
| `roles.*.credential_env` | The `*_API_KEY` or `*_TOKEN` variables an `environment` agent receives. Must be empty otherwise. |
| `roles.reviewer.vendor` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.model_family` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.prompt_lineage` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.protocol_version` | Must be `buildctl-review-v2`. |
| `roles.reviewer.context_documents` | Governed files the reviewer gets as architecture context. Required; may be `[]`. |
| `roles.reviewer.review_prompt` | Optional. Your own governed review prompt, replacing the built-in one. |
| `roles.implementer.remediation_prompt` | Optional. Your own governed remediation prompt. |
| `roles.arbiter.mode` | `debate`: Codex and Claude debate the findings inside the build, and you decide only what they cannot settle. See [Debates](#debates). `interactive`: debates stop for a human. See [DEBATE_REQUIRES_HUMAN](#DEBATE_REQUIRES_HUMAN). Any other value: the arbiter's own command rules on the findings in one call. |
| `milestones` | Names for `run --until`. |
| `checkpoints.before_phases` | Phases that need your approval before they start. |
| `checkpoints.mvp_completion` | The phase after which the build stops for your sign-off. |
| `phase_defaults.required_files` | Files that must exist after the implementer finishes. |
| `phase_defaults.commands` | Acceptance commands, each an argv array run sandboxed with no network. |
| `phase_defaults.invariants` | Named rules the reviewer checks every change against. |
| `evidence.recording` | `"on"` (the default) or `"off"`. On records every agent call and seals a receipt for each commit, and needs the event flags in each role's command (`--json` for `codex`; `--output-format stream-json --verbose` for `claude`). Off turns all of that off. See [What agents did](#evidence). |
| `phase_defaults.claude_review_required` | Whether the phase must pass review. |
| `phase_defaults.debate_on_architecture_disagreement` | Whether architectural findings go to a debate. |
| `phase_defaults.max_remediation_attempts` | Remediation laps before [REMEDIATION_ATTEMPTS_EXHAUSTED](#REMEDIATION_ATTEMPTS_EXHAUSTED). |
| `phase_defaults.merge_policy` | `auto`, or `human` to wait for approval before integrating. |
| `phase_defaults.allowed_paths` | The only paths a phase may change. |
| `phases` | The ordered list of phases, each with `number`, `name` and `prompt`. |
| `phases[].acceptance` | Per-phase overrides of any `phase_defaults` key. |
