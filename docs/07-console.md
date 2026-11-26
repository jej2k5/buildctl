## The console {#console}

`buildctl-ui` is an optional companion: a local web console and a terminal UI over the same build. It lives in `ui/` as its own package, so editing it never changes what `trust init` pins.

```sh
buildctl-ui          # the web console, for the repository you are in
buildctl-ui tui      # the same in the terminal
```

### What it shows {#console-views}

| View | What it is for |
|---|---|
| Plan | The roadmap in plain words: one sentence on where the build stands, what waits on you, the phase line, each phase with its outcome, each checkpoint with what you do there, and the accepted risks and decisions. A long cleared run folds into one row. |
| Line | The phase line, the call in flight with what it has reported and how long it has been quiet, spend by phase, and the levers you can pull now. |
| Stop | Why the build stopped, the evidence (what agents did, findings, agent calls, gates, artifacts, and a [debate](#debates) turn by turn), and only the levers the kernel will accept. At [DEBATE_AWAITING_DECISION](#DEBATE_AWAITING_DECISION), the Decide lever takes your ruling. |
| Phases | Every phase with its attempts, reviews, fixes, debates and cost, and its feed of [what agents did](#evidence). Selecting a row opens that part of the full record. |
| Ledger | The audit chain, newest first, filterable by phase and event. |
| Setup | Readiness in the order buildctl checks it, and the trust levers. |

Keys `1` to `6` switch views in both surfaces, in rail order; `7` opens Release drafts, which is in the web console only. Both open on the Plan.

The current call, the Stop view and each phase's detail show the phase's steps: Implement, Gates, Docs when [documentation](#documentation) is on, Review and Commit, each with its signal shape and what it took. The step where the build stopped is marked, and a documenter sent back is counted there ("pass 2 of 4 · 1 send-back"). A phase with `documentation: false` shows its Docs step as off. When documentation is on, a Docs tab shows the documenter's account, each documentation file's change as a diff, and the documentation gates; on [DOC_SCOPE_VIOLATION](#DOC_SCOPE_VIOLATION) it opens first, leading with the files that were outside the documentation paths. Setup lists the documentation settings, and the Phases table counts documenter passes. The terminal UI shows the steps; the Docs tab is web only for now.

The Plan view reads `docs/ROADMAP.md`, `docs/DISCOVERY_DECISIONS.md` and `docs/DISCOVERY_RISKS.md` at the commit `trust init` pinned, so an agent cannot rewrite your summary of its own phase. The plain lines are optional: a phase's outcome is the indented line under it in `docs/ROADMAP.md`, and a checkpoint's instruction is a line such as `Before phase 6, the operator: Send a real webhook.` buildctl-discover writes both when the interview gives a phase an outcome and a checkpoint a note.

### Drafts in the console {#console-drafts}

Release drafts (key 7) shows what [buildctl-drafts](#drafts) wrote, one run at a time. Each draft is a tab, marked by its state: a caution diamond while it waits for you, a danger square if it failed a check, a clear circle once approved. A draft you discard leaves the tabs and is listed under them. The claims ledger sits at the left, with any unbacked claim first and why it failed; open a claim to see the text it quotes. The draft reads at the right, and your decision comes after its last line.

Approving is signed: the checks prove each claim cites a source that exists, not that the source supports it, and that read is yours. **Review and sign** shows what you are signing, lists any check the draft failed, and asks for the first 7 characters of the draft's digest. If the draft changed since the page showed it, the console refuses. Discard records without a signature. Neither one posts anything.

**Write drafts** starts a paid run. Its conditions are the ones `buildctl-drafts make` checks: the package is installed in the console's Python, the build is trusted, the sandbox was checked in the last 24 hours, a milestone is complete, and the run fits today's cap counting both the build's spend and earlier drafts. It names the ceiling (about $2 per draft, each may be rewritten once), asks you to type the milestone's name, and runs detached.

## Watching a discovery draft

`buildctl-ui --session NAME` opens a buildctl-discover session before it is finalized, read-only, in the web console. The rail shows only the Plan, and the bar says DRAFT. The page shows what the gate (`buildctl-discover check`) still counts against the draft: your open calls, open review findings and other failures. It also shows the proposed phases with their outcome lines, the checkpoint notes, the decisions so far, and a feed of what the interviewer records, newest first, refreshing every two seconds. It ends on the next command to run, `buildctl-discover --session NAME check` while the gate fails and `buildctl-discover --session NAME finalize --confirm <digest>` once it passes. The console never finalizes, accepts a risk or answers a call: those stay terminal acts. Nothing in a draft is governed until it is finalized and trusted.

The feed of what agents did is in the web console only for now. The terminal UI does not show it yet. The feed never claims an agent is stuck, because a long test run and a hang look the same: it says how long the agent has been quiet.

### Levers and their conditions {#levers}

Every action is a numbered lever listing the conditions that allow or lock it. The numbers are fixed: Start is 01, Approve is 04, Retry is 05, and so on, in both surfaces. The conditions come from the kernel, so the console never offers an action buildctl would refuse.

### Typing the evidence {#ceremony}

Approving, pinning trust, buying an extra attempt and reconciling ask you to type the evidence you looked at: the tree-hash prefix for an approval, the commit for trust, the phase number for a grant, the operation for a reconcile. The console sends the nonce it showed you, so an approval made against state that has since moved is refused. A signed action leaves a receipt on screen.

### Runs outlive the console {#detached}

Start runs detached. Closing the console never stops a build.

### Security {#console-security}

The web console listens on 127.0.0.1 only. Every request needs the token in the link `buildctl-ui` prints, reads included, because the state holds pending nonces. The Host header must name the console, and state changes must be JSON from the console's own origin.
