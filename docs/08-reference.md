## Reference {#reference}

### Commands {#commands}

Global flags: `--repo <path>` (must be the repository toplevel) and `--dry-run`.

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
| `retry` | Try again after an ordinary failure. Starts a fresh attempt. `--authorize-extra-attempt --nonce <n>` buys exactly one more pass past an exhausted ceiling. |
| `reconcile --operation <id> --action <action>` | Settle an agent call whose outcome is unknown. The action is `accept-output` (keep what it left) or `authorize-retry` (discard it and pay for a new call). |
| `trust init --to <sha> --confirm <sha>` | Pin the governed files at that commit, this buildctl, and both agent executables. |
| `trust show` | Show what is pinned. |
| `isolation verify` | Probe the sandbox and record that it passed. Runs refuse to start once that record is over 24 hours old. |
| `auth check` | Confirm both agent CLIs are logged in. |
| `bootstrap` | Create the build state. Requires fresh isolation conformance. |
| `validate` | Check `build.yaml`. |
| `inspect` | The full read model as one JSON document: state, phases, trust, isolation, budget, the stop and its meaning, and every action with the conditions that allow or lock it. Read-only. |
| `audit --after <seq>` | Audit events after a sequence number, as JSON. `--limit` caps how many. |

### Configuration {#config}

{.wide .anchors prefix=config-}
| Key | Meaning |
|---|---|
| `limits.max_phase_attempts` | Attempts a phase may start before [PHASE_ATTEMPTS_EXHAUSTED](#PHASE_ATTEMPTS_EXHAUSTED). |
| `limits.max_review_format_attempts` | Malformed review reports tolerated before [REVIEW_FORMAT_ATTEMPTS_EXHAUSTED](#REVIEW_FORMAT_ATTEMPTS_EXHAUSTED). |
| `limits.max_semantic_review_attempts` | Reviews of one phase before [SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED](#SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED). |
| `limits.max_debate_rounds` | Debates one phase may hold. |
| `limits.max_phase_minutes` | Wall-clock limit for one attempt, before [PHASE_DEADLINE_EXCEEDED](#PHASE_DEADLINE_EXCEEDED). |
| `limits.max_daily_cost_usd` | The daily budget, in UTC days. |
| `limits.max_consecutive_failures` | Stops in a row before [MAX_CONSECUTIVE_FAILURES_EXHAUSTED](#MAX_CONSECUTIVE_FAILURES_EXHAUSTED). |
| `operation_cost_usd` | Estimated cost of each paid call (`implementation`, `review`, `remediation`, `debate`), reserved before the call runs. |
| `governance_documents` | Files beyond the fixed governed set that `trust init` pins and agents may not modify. Required; may be `[]`. |
| `git.remote` | The remote phase branches are pushed to when `push` is on. |
| `git.push` | Push the phase branch instead of fast-forwarding the integration branch locally. |
| `git.create_pr` | Open a pull request for the pushed phase branch, with the `gh` CLI. |
| `git.merge` | Request a squash auto-merge of that pull request, for phases whose `merge_policy` is `auto`. |
| `git.integration_branch` | The branch fast-forwarded after each phase. Default `main`. Fast-forward only: if it has commits the phase lacks, buildctl stops instead of merging. |
| `roles.*.command` | The argv for each agent. `{prompt}` and `{output}` are substituted. |
| `roles.*.network` | Whether the agent has network access in the sandbox. |
| `roles.*.auth_mode` | How the agent authenticates: `host_cli` (your CLI login), `environment` (the named variables) or `none`. |
| `roles.*.credential_env` | The `*_API_KEY` or `*_TOKEN` variables an `environment` agent receives. Must be empty otherwise. |
| `roles.reviewer.vendor` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.model_family` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.prompt_lineage` | Reviewer identity every review report must echo back exactly. |
| `roles.reviewer.protocol_version` | Must be `buildctl-review-v2`. |
| `roles.reviewer.context_documents` | Governed files the reviewer gets as architecture context. Required; may be `[]`. |
| `roles.reviewer.review_prompt` | Optional. Your own governed review prompt, replacing the built-in one. |
| `roles.implementer.remediation_prompt` | Optional. Your own governed remediation prompt. |
| `roles.arbiter.mode` | `interactive`: debates stop for a human. See [DEBATE_UNRESOLVED](#DEBATE_UNRESOLVED). |
| `milestones` | Names for `run --until`. |
| `checkpoints.before_phases` | Phases that need your approval before they start. |
| `checkpoints.mvp_completion` | The phase after which the build stops for your sign-off. |
| `phase_defaults.required_files` | Files that must exist after the implementer finishes. |
| `phase_defaults.commands` | Acceptance commands, each an argv array run sandboxed with no network. |
| `phase_defaults.invariants` | Named rules the reviewer checks every change against. |
| `phase_defaults.claude_review_required` | Whether the phase must pass review. |
| `phase_defaults.debate_on_architecture_disagreement` | Whether architectural findings go to a debate. |
| `phase_defaults.max_remediation_attempts` | Remediation laps before [REMEDIATION_ATTEMPTS_EXHAUSTED](#REMEDIATION_ATTEMPTS_EXHAUSTED). |
| `phase_defaults.merge_policy` | `auto`, or `human` to wait for approval before integrating. |
| `phase_defaults.allowed_paths` | The only paths a phase may change. |
| `phases` | The ordered list of phases, each with `number`, `name` and `prompt`. |
| `phases[].acceptance` | Per-phase overrides of any `phase_defaults` key. |
