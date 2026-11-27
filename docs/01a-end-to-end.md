## A real build, end to end {#end-to-end}

This chapter follows one real build from a written brief to the checkpoint where it waits for a signature: discovery, trust, an unattended run with review and documentation, a recorded demo and release drafts. Nothing in it is staged. The project is a toy, so the whole run fits on a page, but every agent call, gate, finding and dollar below happened, on 27 November 2026, on a Mac.

| Stage | What ran | Cost | Time |
|---|---|---|---|
| Discovery | Claude Code as the interviewer, 12 roadmap reviews | about $8.50 for the interviewer, plus 12 review calls, all on the Claude login and outside buildctl's budget | 43 minutes over three sittings |
| Trust | `trust init`, `isolation verify`, `auth check` | $0 | seconds |
| The run | 2 phases: build, gates, docs, review, merge | $16.00 of the $25.00 daily cap | 8 minutes 19 seconds |
| Recorded demo | the product, driven in a browser | $0 | 10 seconds |
| Release drafts | release notes and a demo script | $4.00 | 79 seconds |

### The project {#e2e-project}

splitbill splits a restaurant bill between friends, tip included, down to the cent: a core, a command line, and a small local web page. The repository started with a package skeleton, a base `build.yaml` holding the roles, limits, documentation rules and a [`demo` section](#demos), and one file the operator wrote by hand, the demo scenario:

```json
{"title": "Split a dinner bill three ways", "steps": [
  {"name": "The page opens", "open": "/", "expect": "Split the bill", "hold": 1.5},
  {"name": "Enter the total", "type": {"target": "css:#total", "text": "84.50"}},
  {"name": "Three people", "type": {"target": "css:#people", "text": "3"}},
  {"name": "Eighteen percent tip", "type": {"target": "css:#tip", "text": "18"}},
  {"name": "Split it", "click": "Split", "expect": "Each person pays", "hold": 3}]}
```

The requirements came from Sam, the requirements owner, as a one-page written brief: who it is for, the money rules (whole cents, tip rounded half up, leftover cents to the first people), the two pieces in order, what is out of scope, and where to stop. Sam could not join live, so the interviewing agent recorded the brief's answers instead of asking them one at a time. That is the only departure from a normal discovery session. Sam is a stand-in: the operator wrote the brief, and the two amended answers below, in Sam's name.

### Discovery {#e2e-discovery}

The operator opened a session, and an agent ran the interview through `buildctl-discover`'s MCP tools:

```sh
buildctl-discover start
claude -p "Run discovery for the splitbill repository…" \
  --mcp-config mcp.json --allowedTools mcp__discover
```

The agent recorded a thesis, 18 decisions in Sam's words (16 standing, after two were superseded), 4 principles, one open question (uneven splits, "not now"), two phases with acceptance contracts, and the MVP stop. It never wrote a proposal file; the files were rendered from that record. Each time `check` passed its own rules, the agent asked for a roadmap review, which buildctl-discover runs as a separate, read-only Claude session under its own protocol, and worked the findings. There were 12 reviews in all. Three of the things they caught would have cost a paid attempt each:

- **Sockets.** Sam asked for a test that the server answers on its port. On macOS the acceptance sandbox cannot bind or connect even on 127.0.0.1, so that test could never pass. Sam's amended answer moved the page into plain functions the gate calls directly, and left the real port to the recorded demo, which runs with loopback allowed.
- **The amount box.** A plain `<input type="number">` refuses `84.50` in a browser. The contract now requires `step="0.01"` on the total, or the scripted demo would have stalled on submit.
- **A silent ban.** One gate rejected every `urllib` import, including `urllib.parse`, the standard way to read a query string, while the prompt never said so. The operator found it by writing a throwaway implementation that follows both prompts and running all 13 gate commands against it, outside the sandbox. Twelve passed; the thirteenth was the ban. A third discovery sitting allowed `urllib.parse` and changed nothing else.

The phase contracts that came out are specific. Phase 0's prompt pins the exact command line output, the integer arithmetic, and which inputs must be refused; its gate parses the source to prove there is no `float(`, `round(` or `/` in the money code, patches the core to prove the command line goes through it, and sweeps totals to prove the shares always add up. `buildctl-ui --session default` [shows the draft](#console-discovery) while it is open; this is the finished one, gate passing, with what the interviewer recorded, newest first:

![The discovery draft in the console: the gate passes, the two phases and the checkpoint in plain words, no open findings, and the interviewer's record ending in the accepted risk and the finalize.](media/e2e-discovery.gif)

After trust, the console's Plan view shows the same phases, one plain line each:

![The plan discovery wrote, before the run: two phases in plain words, the checkpoint after the web page with what to check there, nothing spent, sandbox and trust green.](media/e2e-plan.gif)

### Trust {#e2e-trust}

The open question was accepted as a risk by the operator (only the operator can), and the proposal was finalized into the working tree, committed and trusted. `finalize` refused at first: `build.yaml` was not yet committed, and it will not overwrite a file Git cannot restore. Committing the starting files first fixed that.

```sh
$ buildctl-discover finalize --confirm 16f65177
Wrote 8 files from proposal 16f6517796c8.
$ git commit -q -m "Roadmap from discovery"
$ buildctl trust init --to f343765e8db7… --confirm f343765e8db7…
{ "governance_sha": "f343765e8db744c8390e53895fea0ed3a84a44fc", …  (manifest, executables, kernel)
$ buildctl isolation verify
{ "backend": "macos-sandbox-exec-v2-deny-default", … "status": "PASS", … }
$ buildctl auth check
{ "implementer": { "auth_method": "ChatGPT", "authenticated": true, … },
  "reviewer": { "auth_method": "claude.ai", "authenticated": true, … }, "status": "PASS" }
```

Trust pinned the roadmap commit, the governed files, both agent executables and the kernel. From here, editing any of them stops the build until it is trusted again.

### The run {#e2e-run}

```sh
buildctl start
```

Codex implemented each phase; the gates ran in the sandbox with no network; a documenter updated the README and changelog and passed the documentation gates; Claude reviewed the combined change read-only; and buildctl merged it. The documenter's work is in the same commits as the code; after both phases the changelog read:

```markdown
## Unreleased

### Added

- Added `python -m splitbill` to print individual shares and the total with tip.
- Added a Python API for parsing bill inputs and splitting bills in whole cents,
  with half-up tip rounding and leftover cents assigned to the first people.
- Added `python -m splitbill.web PORT` to serve a self-contained bill-splitting
  form on `127.0.0.1` that displays each person's share and the total with tip.
```

![Phase 0 in flight: the step track at Implement, $2 spent, and the Start lever locked because a run already holds the state lock.](media/e2e-running.gif)

- **Phase 0, core and command line.** The first gate failed: `tests/__init__.py` was empty, and a required file must have content. The operator's skeleton had committed it empty, and the dry run had not modelled that rule. Codex needed two remediations to notice, at $2 each. The third gate passed, the docs passed, and the review passed with one medium finding: the parsers convert numbers of any length, which steps around Python's 4,300-digit conversion limit. On the command line the operator types the input, but the reviewer pointed out that the next phase reuses these parsers on form input anyone can send, which makes huge inputs a way to burn CPU. It was filed as debt, not a blocker, and it is still open at the checkpoint: one of the things to weigh before signing.
- **Phase 1, the web page.** Clean on the first attempt: implement, gates, docs, review, merge. One low finding: an error page does not say which field was wrong.

| Phase | Calls | Cost | Time |
|---|---|---|---|
| 0 · Core split and command line | implement, 2 remediations, document, review | $10.00 | 4 min 49 s |
| 1 · Local web page | implement, document, review | $6.00 | 3 min 28 s |

The build then stopped on its own, at the checkpoint discovery placed after the web page.

### The checkpoint {#e2e-checkpoint}

![Stopped at the checkpoint: the plan shows what waits on you, the Stop view gives the code and what to do, the phases and ledger hold the evidence, and the Drafts view holds the recording.](media/e2e-checkpoint.gif)

[CHECKPOINT_REQUIRED](#CHECKPOINT_REQUIRED) means the MVP is complete and needs a signature before anything else happens. The Stop view shows exactly what an approval binds to (checkpoint, phase attempt, tree hash, governance commit, state generation), the findings, and only the levers the kernel will accept. Approving means typing the first seven characters of the tree hash you reviewed:

![The signature: phase 1's open finding filed as debt, and the approval that binds to the reviewed tree hash.](media/e2e-sign.gif)

### The recorded demo {#e2e-demo}

The checkpoint's instruction was to watch the product work, so the next step cost nothing:

```sh
$ buildctl-drafts record --milestone mvp
  ok    1. The page opens
  ok    2. Enter the total
  ok    3. Three people
  ok    4. Eighteen percent tip
  ok    5. Split it
Recording in …/drafts/20261127-193122/demo. Nothing was committed or published.
```

The app ran from a throwaway copy of the milestone's commit, sandboxed with loopback only, and a headless Chrome locked to its address drove the operator's scenario:

![The product the build made: splitbill's page splitting $84.50 three ways at 18%, $33.24, $33.24 and $33.23, total $99.71.](media/e2e-product.gif)

### Drafts from the milestone {#e2e-drafts}

The console's Write drafts lever stayed locked, for two reasons it named: `buildctl-drafts` was not installed in the console's Python, and four drafts at up to $4 each (one rewrite allowed) would have needed $16 with only $9 of the day's $25 left. From the terminal, two fit:

```sh
$ buildctl-drafts make --milestone mvp --only release-notes,demo-script
  release-notes  sourced    27 of 27 claims backed
  demo-script    sourced    31 of 31 claims backed
```

Every factual sentence cites a line in the README or changelog at the milestone's commit, and buildctl checked that each quoted line is really there. Whether the source supports the sentence is the operator's read, with the ledger beside the draft:

![The real release notes: 27 of 27 claims cite a source, each with its file and line, what signing a draft means, and the paid lever locked by its conditions.](media/e2e-drafts.gif)

### What this run shows {#e2e-lessons}

- **Discovery's value is in the acceptance contracts.** The prompts are short; the gates are what made "it works" checkable, and the reviews that hardened them were worth more than the code they guarded.
- **Run the gates before you pay.** A throwaway implementation and one loop over `build.yaml`'s commands found a gate that would have failed correct code. It missed the empty-file rule, which cost $4. Model the gate's file checks too.
- **The sandbox shapes the tests.** No sockets in the gate on macOS means testable plain functions and a recorded demo for the real port. That split held up.
- **Caps are real.** The run stopped on its own at the checkpoint with $9 left, and the drafts lever refused a run that could have passed the cap.
- **What it did not exercise:** a debate, a blocked review, an exhausted attempt budget, a documenter out of scope. The [demo builds](#demo) show those.
