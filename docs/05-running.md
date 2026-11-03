## Running and watching {#running}

### Starting work {#starting}

`buildctl start` runs every incomplete phase in order. `buildctl run --until <N or milestone>` stops after phase N or after a milestone named in `build.yaml`. `buildctl phase <N>` runs one phase, and refuses a phase that is already complete.

Only one buildctl process changes state at a time. A second one is refused while the first holds the state lock.

### Watching a run {#watching}

`buildctl status` prints the state, the current phase, any pending approval or grant with its nonce, and today's spend. `buildctl inspect` prints everything a front end needs as one JSON document, and works before trust is set up. `buildctl audit --after <seq>` prints audit events after a sequence number.

The console shows the same information live: the phase line, the call in flight and its deadline, and spend by phase. See [The console](#console).

### Pausing {#pausing}

`buildctl pause` stops buildctl starting new work. A call already in flight finishes. `buildctl resume` allows new work again. Both take the state lock, so they cannot interrupt a run that is holding it.

### The budget {#budget}

Each paid agent call has an estimated cost in `operation_cost_usd`. buildctl reserves that amount against `max_daily_cost_usd` before the call runs, and refuses the call if it would go over. The day is measured in UTC.

The ledger records estimates you configured, not your provider's bill.

### Where state lives {#state}

Canonical state, the hash-chained audit log, the cost ledger, the trusted snapshot and every agent artifact live outside your repository, in `$XDG_STATE_HOME/buildctl/<hash of repo path>` (by default `~/.local/state/buildctl/...`). Set `BUILDCTL_STATE_DIR` to put it elsewhere.

| File | What it holds |
|---|---|
| `state.json` | The build state: status, current phase, step, attempts, pending approval, operations. |
| `audit.jsonl` | Every state change, each event hash-chained to the one before. |
| `costs.jsonl` | One line per paid agent call reserved against the budget. |
| `trust.json` and `snapshot/` | The trust anchor and the read-only copy of the governed files. |
| `isolation-conformance.json` | The last sandbox verification. |
| `artifacts/phase-N/` | Agent results, review reports, gate results, debate requests and ADR proposals. |
| `records/phase-N/` | The full record of each agent call, kept until you [prune it](#evidence). |

### What agents did {#evidence}

With `evidence.recording` on, which is the default, buildctl writes each line an agent emits to a record as it arrives. The console shows it live, and each commit gets a receipt you can share.

Most of a record is the agent's own account. Every row says who vouches for it:

| Label | Rows | Who vouches |
|---|---|---|
| Observed | Launch, exit code, duration, time of last output, the change itself, gate results, ledger entries | buildctl |
| Agent-reported | Files read and edited, commands run, tool calls, reasoning, token use | The agent |

**Flags.** Before each review, buildctl compares the writes the implementer reported with what actually changed. It flags a change the agent never mentioned, and a reported write that left no change. A command can write files without reporting them, so a change in a call that also ran commands gets the weaker flag `changed_possibly_by_command`. Flags never stop the build. The reviewer sees them, and so do you at an approval.

**Receipts.** Each commit's receipt is a manifest on the `buildctl/evidence` branch, which never merges with your code, so the code branch still holds only reviewed bytes. It lists every attempt, every agent call's actions and a hash of the full record. Prompts, command output and reasoning stay on your machine, in the [state directory](#state). With `git.push` on, the branch is pushed before the code. With it off, push it yourself to share it.

**Checking one.** `buildctl evidence verify <commit>` works on any clone with the branch. It proves the receipt matches the commit, links to the receipt before it and, given the full record, matches that too. After a squash merge it falls back to matching the commit's tree and says so.

It does not prove that an agent's report is true or complete, that a receipt was not rewritten before you received it, or why an agent did anything. Receipts are not signed yet. Anyone with your machine can rewrite the branch and the audit chain.

**Reviewing before it lands.** Set `merge_policy: human` on a phase. It stops after the commit and before publication, with the receipt already sealed. A `before_phases` checkpoint runs before the agent starts, so it can only show the previous phase's work.

**Secrets.** A credential buildctl was given kills the agent the moment it appears, and never reaches the record. A secret-shaped string, such as a token in a file the agent read, is redacted from the record without stopping the build. The result file is still checked as strictly as before. Receipts hold file paths and command lines, which get the same redaction plus patterns for command-line credentials. That lowers the risk but does not remove it, and on a public repo with `git.push` on, receipts are public too.

**Interactive debates** run outside buildctl, so nothing inside them is captured. The receipt holds the request and result hashes, labelled `human_run`.

**Turning it off.** Set `evidence.recording` to `"off"`, commit and re-trust. An agent cannot do this for itself, because a phase that touches `build.yaml` fails. Agents then run as they did before recording existed: no feed, records, flags or receipts, and the console says "recording off". `verify` reports `no_receipt` for those phases, the same as for a missing receipt, so a missing receipt proves nothing either way.
