## How it works {#how-it-works}

### One phase, end to end {#phase-lifecycle}

Every phase runs the same seven steps. Each step is recorded before and after it runs, so if the process dies, the next run picks up where it stopped or tells you exactly what it cannot account for.

1. **Clone.** A fresh clone of your repository at the current commit, with no remote, and with `AGENTS.md`, `CLAUDE.md`, `.claude` and `.codex` removed.
2. **Implement.** The implementer gets the phase prompt and its acceptance contract and works in the clone, sandboxed.
3. **Gate.** The required files must exist, and every acceptance command must pass, sandboxed and with no network.
4. **Review.** The reviewer, read-only, gets the exact changed bytes and must account for every changed path.
5. **Decide.** The reviewer's findings, not its verdict, decide what happens next.
6. **Commit.** Only paths the phase may touch, none of them governance files, are copied into your repository and committed to `build/phase-N`.
7. **Integrate.** The integration branch is fast-forwarded to the phase commit and the repository is left on it, so the next phase starts from there.

### How findings route a phase {#findings}

| Finding | What happens |
|---|---|
| A fixable `HIGH` | It goes back to the implementer for remediation, then the gate and review run again. |
| An architectural or recurring `HIGH` | It goes to a debate. An agreement becomes an ADR in `decisions/` and is implemented. |
| A `CRITICAL` | The build stops for a human. |
| Nothing open above `MEDIUM` | The phase advances. `MEDIUM` and `LOW` findings are recorded, not blocking. |

A finding is only cleared when a later review re-states it as `resolved`. Leaving it out of a report keeps it open.

### Governed autonomy {#governed-autonomy}

The agents do the work. You decide where a person must look, and buildctl stops there and nowhere else:

- **Checkpoints.** Phases listed in `checkpoints.before_phases` wait for your approval before they start.
- **Human merges.** A phase with `merge_policy: human` is committed, then waits for you before the integration branch moves.
- **The MVP milestone.** After the phase named in `checkpoints.mvp_completion`, the build stops for your sign-off.
- **Ceilings.** Every retry, review and remediation loop has a limit. Hitting one stops the build until you buy exactly one more pass.
- **The budget.** Each paid agent call is charged against `max_daily_cost_usd` before it runs.

Everything that crosses into your repository is checked against what was reviewed, and every state change lands in a hash-chained audit log.
