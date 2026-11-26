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

**Reviewing before it lands.** Set `merge_policy: human` on a phase. It stops after the commit and before publication, with the receipt already sealed. If what you see should not land, `buildctl reject` discards it and the next run tries again. A `before_phases` checkpoint runs before the agent starts, so it can only show the previous phase's work.

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

A debate interrupted mid-way, by a crash you then reconcile, resumes at its next turn; turns already taken are not paid for again. If `max_debate_turns` pass without a close, the build stops at [DEBATE_AWAITING_DECISION](#DEBATE_AWAITING_DECISION) and the decision is yours. The console's Stop view shows the findings and the debate turn by turn, with three answers: the finding stands, the code stands, or your own ruling. The same attempt resumes with it.

**What it costs.** Each turn reserves `operation_cost_usd.debate_turn`, so a debate costs up to `max_debate_turns` times that: $12 at the values above. Each turn also spends the phase's wall-clock time.

**What it records.** Every turn is a recorded agent call: it shows in the feed, and the commit's receipt lists it with its actions, cost and record hash. Arguments stay in the full records on your machine. Your ruling is in the receipt as `DEBATE_DECISION-<n>.json`, labelled `operator`. Each debate also writes one `debate_outcome` event to the audit chain: `findings_upheld`, `findings_changed`, `findings_dropped` or `operator_decided`. Those tell you, after a few debates, whether they change anything the reviewer asked for, and so whether they are worth their cost.


### Release drafts {#drafts}

`buildctl-drafts` is an optional companion, like the console: it lives in `drafts/` as its own package, outside what `trust init` pins, so changing it never needs a re-trust. When a milestone is done, it writes release material from what the build recorded, checks that every factual claim cites a source that exists, and leaves the result for you to read against that ledger. It never commits, never pushes and never posts.

```sh
buildctl-drafts make --milestone mvp        # or --since v0.4.0 [--until <ref>]
buildctl-drafts list
buildctl-drafts show release-notes          # a type means its newest draft
buildctl-drafts approve release-notes       # or discard
```

- **What it writes:** release notes, positioning (a README opening, a repository description and topics), a launch post, a demo script and a social post. `--only` picks some.
- **What it reads:** the milestone's commits, the changelog, the README and docs, the phase handoffs and the open findings, all at the milestone's commit, never your working tree. Open findings become a "known limits" list, so a draft says what does not work yet.
- **Who writes:** the build's reviewer command, read-only, in buildctl's sandbox, in a throwaway copy of the milestone's commit. Like a phase, a run needs the trust record and a sandbox verified in the last 24 hours. A launch problem (a logged-out or updated CLI) stops the run rather than being paid for once per draft.
- **Claims ledger:** every factual statement cites a commit in the range, a file (with line and exact quote) at the milestone's commit, a changelog line, or an open finding. buildctl checks that each source exists and each quote is really there; whether the source supports the claim is your read, with the ledger beside the draft. A draft with no claims, an unbacked claim or a voice problem is rewritten once with what failed; if it still fails, it is kept and marked "needs you", with each unbacked claim named.
- **Voices:** release notes, positioning, the launch post and the demo script use the product voice, `docs/VOICE.md` in the repository, plus `documentation.forbidden_phrases`. A social post uses your own voice, `~/.config/buildctl/voice.md`, which is never committed; without it, social drafts are skipped. A voice file's `## Rules` bullets are enforced: `Banned: a, b`, `No em dashes`, `Max words: N` and `Max words for <type>: N`.
- **Where drafts live:** in the build's state folder, under `drafts/`, as Markdown with the ledger at the end. Approving or discarding records an event and nothing more.
- **Cost:** each writer call is booked as `release_draft` in `drafts/costs.jsonl`, at `--cost` dollars (default 2). A run is refused when today's build spend, plus today's drafts, plus the run's estimate, would pass `limits.max_daily_cost_usd`.
- **In the console:** the Drafts view reads, signs and discards drafts, and starts a paid run; see [Drafts in the console](#console-drafts).

#### Recorded demos {#demos}

`buildctl-drafts record --milestone mvp` records the product working, from a scenario committed in the repository. It calls no model, so it costs nothing.

```sh
buildctl-drafts record --milestone mvp      # or --until <ref>
```

- **What it needs:** a [`demo` section](#config) in `build.yaml`, naming a command that starts the product on fictional data and serves on the loopback port `{port}`, and the scenario file it names (default `docs/demo/scenario.json`) at the milestone's commit.
- **The scenario:** a title and steps, each with one action (`open` a path, `click` visible text or `css:selector`, `type` into a field, `wait_for` text, `pause`), the text the screen must then show (`expect`), and how many seconds to `hold`. A step that never shows its text fails, and the recording says which.
- **How it runs:** the demo command runs in a throwaway copy of the milestone's commit, under buildctl's sandbox in loopback mode: it may serve on loopback and reaches nothing else, with an environment that carries no credential. On macOS, which has no private loopback, a headless Chrome on your machine drives it, locked by a dead proxy to the demo's address, so the demo's own page cannot reach your other local services or the internet either. On Linux the sandbox has its own network namespace, so Chrome runs inside it with the demo. `isolation verify` checks the loopback mode with the other roles.
- **What you get:** a frame per step, a GIF of the whole (with `ffmpeg` installed), and `recording.json`, under the build's state folder in `drafts/<run>/demo/`. The recording appears in `buildctl-drafts list` as `demo-recording`, and a failed step marks it "needs you".
- **For a project's own docs:** `python -m buildctl_drafts.recorder --demo demo.json --scenario scenario.json --out DIR` records from a working tree, still sandboxed with loopback only, but not from a governed milestone. buildctl's own console vignettes are made this way, from `docs/demo/`.
- **Not yet:** an agent-written scenario (today you write and commit it), an agent check of each frame, and recordings in the console's Drafts view.
