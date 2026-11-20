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

Keys `1` to `6` switch views in both surfaces, in rail order. Both open on the Plan.

The Plan view reads `docs/ROADMAP.md`, `docs/DISCOVERY_DECISIONS.md` and `docs/DISCOVERY_RISKS.md` at the commit `trust init` pinned, so an agent cannot rewrite your summary of its own phase. The plain lines are optional: a phase's outcome is the indented line under it in `docs/ROADMAP.md`, and a checkpoint's instruction is a line such as `Before phase 6, the operator: Send a real webhook.` buildctl-discover writes both when the interview gives a phase an outcome and a checkpoint a note.

The feed of what agents did is in the web console only for now. The terminal UI does not show it yet. The feed never claims an agent is stuck, because a long test run and a hang look the same: it says how long the agent has been quiet.

### Levers and their conditions {#levers}

Every action is a numbered lever listing the conditions that allow or lock it. The numbers are fixed: Start is 01, Approve is 04, Retry is 05, and so on, in both surfaces. The conditions come from the kernel, so the console never offers an action buildctl would refuse.

### Typing the evidence {#ceremony}

Approving, pinning trust, buying an extra attempt and reconciling ask you to type the evidence you looked at: the tree-hash prefix for an approval, the commit for trust, the phase number for a grant, the operation for a reconcile. The console sends the nonce it showed you, so an approval made against state that has since moved is refused. A signed action leaves a receipt on screen.

### Runs outlive the console {#detached}

Start runs detached. Closing the console never stops a build.

### Security {#console-security}

The web console listens on 127.0.0.1 only. Every request needs the token in the link `buildctl-ui` prints, reads included, because the state holds pending nonces. The Host header must name the console, and state changes must be JSON from the console's own origin.
