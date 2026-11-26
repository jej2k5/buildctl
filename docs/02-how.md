## How it works {#how-it-works}

### One phase, end to end {#phase-lifecycle}

Every phase runs the same seven steps, plus a Document step between Gate and Review when [documentation](#documentation) is on. Each step is recorded before and after it runs, so if the process dies, the next run picks up where it stopped or tells you exactly what it cannot account for.

1. **Clone.** A fresh clone of your repository at the current commit, with no remote, and with `AGENTS.md`, `CLAUDE.md`, `.claude` and `.codex` removed.
2. **Implement.** The implementer gets the phase prompt and its acceptance contract and works in the clone, sandboxed.
3. **Gate.** The required files must exist, and every acceptance command must pass, sandboxed and with no network. With documentation on, the Document step follows: the documenter, its scope check and the documentation gates.
4. **Review.** The reviewer, read-only, gets the exact changed bytes and must account for every changed path.
5. **Decide.** The reviewer's findings, not its verdict, decide what happens next.
6. **Commit.** Only paths the phase may touch, none of them governance files, are copied into your repository and committed to `build/phase-N`.
7. **Integrate.** With `git.push` off, the integration branch is fast-forwarded to the phase commit and the repository is left on it, so the next phase starts from there. With it on, the receipts and then `build/phase-N` are pushed to `git.remote` instead; `git.create_pr` opens a pull request with `gh`, and `git.merge` requests a squash auto-merge for an `auto` phase.

### Documentation {#documentation}

Off unless `documentation.mode` turns it on. The settings are under `documentation` in [Configuration](#config). The documentation paths join the allowed paths of every phase that has the step, and `operation_cost_usd` must then price `documentation` and `documentation_remediation`. A `roles.documenter` entry is refused while documentation is off. With it on, a documenter keeps the docs true to the code, phase by phase, under the same review as the code:

1. **Document.** Once the implementation passes its gate, the documenter edits only the documentation paths and writes `DOC_RESULT`: each change and the code it reflects, or why nothing needed changing. By default it runs as the implementer's model family, so the reviewer never grades its own family's prose.
2. **Check its scope.** Any change outside the documentation paths, or to a governed file inside them, stops the phase: [DOC_SCOPE_VIOLATION](#DOC_SCOPE_VIOLATION).
3. **Gate the docs.** The acceptance commands run again, because docs can break tests that read them. Then three documentation gates: every relative link and heading anchor resolves, a user-visible change has a new changelog entry under the unreleased heading (only when `documentation.changelog` is set), and no documentation file gains a forbidden phrase. A failure sends the documenter back with what failed, up to `documentation.max_attempts` times: [DOCUMENTATION_ATTEMPTS_EXHAUSTED](#DOCUMENTATION_ATTEMPTS_EXHAUSTED).
4. **Review it with the code.** The reviewer sees the combined change and `DOC_RESULT`, with three more criteria: what the docs say matches the code, nothing the phase made stale is left stale, and no marketing language crept in. Blocking findings that all cite documentation paths go back to the documenter; any finding on code goes to the implementer, and the documenter runs again on the new code.

In `optional` mode the documentation gates never stop a phase: their failures go to the reviewer and the audit log. A broken acceptance command or an out-of-scope change still does. A phase with `documentation: false` skips the step, and the reviewer is told so.

### How findings route a phase {#findings}

| Finding | What happens |
|---|---|
| A fixable `HIGH` | It goes back to the implementer for remediation, then the gate and review run again. |
| An architectural or recurring `HIGH`, or a contested finding | It goes to a [debate](#debates). An agreement becomes an ADR in `decisions/` and is implemented. With `arbiter.mode: debate`, Codex and Claude argue it out and you decide only what they cannot settle. |
| A `CRITICAL` | The build stops for a human. |
| Nothing open above `MEDIUM` | The phase advances. `MEDIUM` and `LOW` findings are recorded, not blocking. |

A finding is only cleared when a later review re-states it as `resolved`. Leaving it out of a report keeps it open.

### Governed autonomy {#governed-autonomy}

The agents do the work. You decide where a person must look, and buildctl stops there and nowhere else:

- **Checkpoints.** Phases listed in `checkpoints.before_phases` wait for your approval before they start.
- **Human merges.** A phase with `merge_policy: human` is committed, then waits for you before the integration branch moves. `buildctl reject --phase N --confirm N [--reason <text>]` discards it instead: the branch is renamed `build/phase-N-rejected-<attempt>` and the next run builds a fresh attempt. Reject is refused while the build is running, when `git.push` is on, and once the phase is on the integration branch.
- **The MVP milestone.** After the phase named in `checkpoints.mvp_completion`, the build stops for your sign-off.
- **Ceilings.** Every retry, review and remediation loop has a limit. Hitting one stops the build until you buy exactly one more pass.
- **The budget.** Each paid agent call is charged against `max_daily_cost_usd` before it runs. A [buildctl-drafts](#drafts) run is refused when the build's spend today, plus earlier drafts, plus its estimate would pass the same limit.

Everything that crosses into your repository is checked against what was reviewed, and every state change lands in a hash-chained audit log.
