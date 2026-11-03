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
| `artifacts/phase-N/` | Agent results, review reports, gate results, debate turns, requests and decisions, and ADR proposals. |
| `records/phase-N/` | The full record of each agent call, kept until you [prune it](#evidence). |

### What agents did {#evidence}

With `evidence.recording` on, which is the default, buildctl writes each line an agent emits to a record as it arrives. The console shows it live, and each commit gets a receipt you can share.

**Setting it up.** Recording reads each agent's event stream, so each agent must be asked to emit one. Add these flags to each role's `command`:

| Agent | Add | What it changes |
|---|---|---|
| `codex` | `--json` | Codex prints one JSON event per line instead of plain text. `--output-last-message` still writes the result file. |
| `claude` | `--output-format stream-json --verbose` | Claude prints one JSON event per line. It refuses `stream-json` with `-p` unless `--verbose` is also set. |
| Any other CLI | Nothing | Its output is recorded line by line as plain text, with no actions picked out of it. |

```json
"implementer": {
  "command": ["codex", "exec", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox",
              "--json", "--output-last-message", "{output}", "{prompt}"]
},
"reviewer": {
  "command": ["claude", "-p", "{prompt}", "--no-session-persistence", "--tools", "Read,Grep,Glob",
              "--output-format", "stream-json", "--verbose"]
}
```

An `interactive` arbiter needs nothing, since debates run outside buildctl, and neither does a `debate` arbiter, whose turns use the two commands above. Without the flags, `validate`, `trust init` and every build command refuse before anything is spent:

```
role implementer command needs --json because recording is on; add them, or set evidence.recording to "off"
```

**Upgrading a build that predates recording.** Recording is on unless `build.yaml` says otherwise, so a build set up before it existed is refused until you choose. Either add the flags above or add `"evidence": {"recording": "off"}`. Then check it, commit it and re-trust, because `build.yaml` is a governed file and `trust init` refuses the old one too:

```sh
buildctl validate                  # prints "build.yaml: valid (...)" once the flags are in
git commit -am "Record agent work"
buildctl trust init --to "$(git rev-parse HEAD)" --confirm "$(git rev-parse HEAD)"
buildctl isolation verify
```

The new kernel needs that re-trust anyway, so make the change in the same pass.

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

**Interactive debates** run outside buildctl, so nothing inside them is captured. The receipt holds the request and result hashes, labelled `human_run`. A [debate buildctl runs](#debates) is captured like any other agent work.

**Turning it off.** Set `evidence.recording` to `"off"`, commit and re-trust. An agent cannot do this for itself, because a phase that touches `build.yaml` fails. Agents then run as they did before recording existed: no feed, records, flags or receipts, and the console says "recording off". `verify` reports `no_receipt` for those phases, the same as for a missing receipt, so a missing receipt proves nothing either way.

### Debates {#debates}

When a review raises an architectural finding, or a `HIGH` finding that keeps coming back, the phase goes to a debate. With `roles.arbiter.mode` set to `debate`, buildctl runs it itself:

```json
"arbiter": {"mode": "debate"},
"limits": {"max_debate_turns": 6},
"operation_cost_usd": {"debate_turn": 2.0}
```

**Who argues what.** Codex argues for the code as built, and Claude for the findings. Each uses its own role: its command, login and pinned binary, so a debate needs no setup of its own. Codex speaks first. Both see the findings, the change and every earlier turn, and neither can edit anything: the workspace is read-only for both, and a turn that changes it stops the build.

**How it ends.** Any turn may carry a proposal: a full decision that names every debated finding as upheld, changed or dropped. The debate closes when the other side accepts that exact proposal, by its hash, on the next turn. That decision becomes an ADR marked `Decided by: agents`, and Codex implements it, as after any agreement.

If `max_debate_turns` pass without a close, the build stops at [DEBATE_AWAITING_DECISION](#DEBATE_AWAITING_DECISION) and the decision is yours. The console's Stop view shows the findings and the debate turn by turn, with three answers: the finding stands, the code stands, or your own ruling. The same attempt resumes with it.

**What it costs.** Each turn reserves `operation_cost_usd.debate_turn`, so a debate costs up to `max_debate_turns` times that: $12 at the values above. Each turn also spends the phase's wall-clock time.

**What it records.** Every turn is a recorded agent call: it shows in the feed, and the commit's receipt lists it with its actions, cost and record hash. Arguments stay in the full records on your machine. Your ruling is in the receipt as `DEBATE_DECISION-<n>.json`, labelled `operator`. Each debate also writes one `debate_outcome` event to the audit chain: `findings_upheld`, `findings_changed`, `findings_dropped` or `operator_decided`. Those tell you, after a few debates, whether they change anything the reviewer asked for, and so whether they are worth their cost.

