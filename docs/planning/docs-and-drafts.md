# Spec: a documentation role and release drafts

Status: specified 2026-11-25 against `1b58651`. Not built.

buildctl governs code well and documentation not at all. The implementer writes what the phase brief asks for, the reviewer judges the code, and whatever the docs say afterwards is nobody's job. One governed build, ReplCRM, finished 20 phases with every review passing, then needed a full day of manual work before a public launch:

- **Dead links.** Six links in the README and docs pointed at files the release didn't ship.
- **Stale claims.** The docs said "Node 20+" while `package.json` required `>=22 <23`, and said the Docker baseline still needed review when every stage was already on Node 22.
- **A changelog two releases behind.** Its release pull requests recorded only test counts, so the entries had to be rebuilt from diffs.
- **No launch material.** The README opening was abstract, and there was no release summary, no launch post and no repository description until someone wrote them.

None of this was a review failure. The reviewer was never asked whether the story was still true. This spec adds two things:
1. **A documentation role inside the governed loop.** Docs are kept true phase by phase, under the same review as code.
2. **A release-drafts tool outside it.** It turns a finished milestone into drafts that an operator approves.

Decided 2026-11-25, by JJ:

| # | Question | Chosen | Rejected |
|---|---|---|---|
| 1 | Keep docs true during a build, or produce launch material at release time? | Both, in one design. Documentation ships first, because drafts are written from the docs. | Only one of the two. |
| 2 | Whose voice do drafts use? | It depends on the draft type: the product's voice for anything published as the project (release notes, launch posts, README positioning), and the operator's voice for anything posted under their own name. | One voice for everything. |

Decided 2026-11-25, by JJ: release drafts include demos (a recorded walkthrough of the product and a script for presenting it).

Decided by Claude, open to change:
- **The documentation role commits bytes, so it's a kernel role.** It runs under isolation, records evidence, its diff is reviewed, and its spend is budgeted, the same as the implementer.
- **Drafts never commit and never publish.** They're files and console entries for the operator to approve, edit and post. Because they never touch the governed repository, the drafts tool lives outside the kernel digest and can change without forcing a retrust.
- **Authors and reviewer come from different model families.** The documentation role defaults to the implementer's model family, not the reviewer's, so the reviewer is never grading its own family's prose. buildctl warns when a build configures the same family for both.
- **Marketing language is a documentation defect.** Docs describe what exists. Persuasion belongs in drafts, where a person approves it.
- **Existing builds don't change.** Documentation mode defaults to `off`. Turning it on is a governed change to `build.yaml`.

## The question

Can a build end with documentation as true as its code, and with launch material that only needs an operator's yes, without letting an agent publish anything?

## Part 1: the documentation role (kernel)

### Where it runs

Today a phase runs: checkpoint, clone, implement, mechanical gates (with remediation), review, then commit. Documentation adds one step after the gates pass and before review:

```
implement -> gates (remediate until they pass) -> document -> doc gates -> review -> commit
```

- **When the phase starts:** the documenter starts once the implementation has passed its mechanical gates, so it never documents code that won't stand.
- **What it works in:** the same phase workspace, under a new isolation context, `documenter`. It can write, and it has no network access.
- **What it reads:**
  - the phase brief,
  - the implementation delta and its gate result,
  - the current docs,
  - the project's documentation brief, an optional governed file at `prompts/documentation.md` that sets the docs' structure, voice and what to keep current.
- **What it produces:** edits to documentation paths, plus `DOC_RESULT.json`. That file lists every documentation change and the code change it reflects (a file and line, or the phase's own delta), plus any documented statement the documenter found stale and fixed.
- **When nothing needs changing:** it says so in `DOC_RESULT.json` with a reason, such as "internal refactor, no user-visible change". That's a valid result, and the reviewer checks it.

### What it may touch

`build.yaml` declares which paths count as documentation. After the documenter finishes, buildctl compares the delta against that list. If the documenter touched anything outside it, the phase stops with `DOC_SCOPE_VIOLATION`. The documenter can't change code, tests or governance files.

The implementer may still edit docs as part of its own change. The documenter sees those edits and keeps or corrects them.

### Documentation gates

These run after the documenter, alongside a re-run of the phase's acceptance commands, because docs can break tests that read them:

| Gate | Fails when |
|---|---|
| Links | A relative Markdown link or heading anchor doesn't resolve in the phase's tree. Links outside the repository are not fetched. |
| Changelog | `DOC_RESULT.json` records a user-visible change and the configured changelog has no entry for it under its unreleased heading. |
| Forbidden phrases | A documentation file gains a phrase from the build's configured list, for example "production-ready", "blazing" or "seamless". |
| Scope | The delta includes paths outside the documentation list. This is the same check as above, reported as a gate. |

Failed documentation gates send the documenter back for remediation, with its own attempt ceiling. Exhausting the ceiling stops the phase with `DOCUMENTATION_ATTEMPTS_EXHAUSTED`.

### Review

The reviewer sees the combined delta (implementation plus documentation) and `DOC_RESULT.json`. The review prompt gains three documentation criteria:
1. Every documented statement the phase touched matches the code.
2. Nothing the phase made stale is left stale.
3. No persuasion has crept into the docs.

Findings carry an area, `code` or `docs`:
- **Docs findings** go to documenter remediation.
- **Code findings** go to the implementer, as today. When code changes in remediation, the documenter runs again on the new delta, so the docs follow the final code.

### Configuration

```yaml
documentation:
  mode: off            # off | optional | required
  paths: ["README.md", "CHANGELOG.md", "docs/**"]
  changelog: CHANGELOG.md
  unreleased_heading: "Unreleased"
  forbidden_phrases: ["production-ready", "blazing", "seamless"]
roles:
  documenter:
    command: ["codex", "exec", "..."]   # same shape as the other roles
    vendor: openai
    model_family: codex
    prompt_lineage: documentation-v1
```

- **The three modes:**
  - `off` keeps today's behavior.
  - `optional` runs the documenter but never stops a phase on documentation gates; it records failures as findings.
  - `required` runs the full loop.
- **Per-phase override:** a phase can set `documentation: false` for phases with nothing to document, such as test-only or build-tooling changes. The reviewer is told documentation was skipped by configuration.

### Records and cost

- **Paid operations:** two new ones, `documentation` and `documentation_remediation`. Both count against `max_daily_cost_usd`.
- **Evidence:** the documenter's actions are recorded like the implementer's, and `DOC_RESULT.json` goes into the phase receipt on the evidence branch.
- **Console:** the phase timeline shows the documentation step and its gate results.
- **New stops:** `DOC_SCOPE_VIOLATION` and `DOCUMENTATION_ATTEMPTS_EXHAUSTED`.

### Kernel impact

This touches isolation (a new context), auth (the role's CLI), the protocol (a new prompt and result schema), state (attempt counters), operations, evidence and the review prompt. That changes the kernel digest, so every build needs `trust init` once. Ship it in the same batch as the other pending kernel fixes and the fleet view's `repo.json`, not on its own.

## Part 2: release drafts (outside the kernel)

### What it makes

`buildctl-drafts` is a separate package beside `ui` and `discover`, outside the kernel digest. When a milestone in `build.yaml` completes, or when the operator asks (`buildctl-drafts make --milestone live`, or `--since <ref>`), it writes drafts:

| Draft | Voice | Typical use |
|---|---|---|
| Release notes | Product | GitHub release body, changelog summary |
| Launch post | Product | Show HN, a project blog, an announcement |
| Positioning | Product | README opening, repository description and topics |
| Social post | Operator | A post or direct note under the operator's own name |
| Demo recording | None (visual) | README GIF, still screenshots, a short video |
| Demo script | Product | A talk track for a live demo, or captions for a recording |

Each written draft comes with a claims ledger: every factual statement in the draft, and the source that backs it (a changelog line, a commit in the milestone's range, a file and line, or a verification count from the phase receipts).

### Inputs

All inputs are read-only:
- the milestone's commit range and phase receipts,
- the changelog section,
- the README and docs as the documentation role left them,
- the phase handoffs,
- open review findings.

The open findings feed a "known limits" list, so drafts say what doesn't work yet.

### Checks before a draft is shown

- **Every claim cites a source that exists.** A cited commit must be in the milestone's range, and a cited file and line must exist at the milestone's commit.
- **The voice's lint passes.** It checks forbidden phrases and length limits, and enforces each voice's own rules, such as a voice that bans em dashes.
- **The draft gets regenerated or flagged.** A draft with an unbacked claim is regenerated once. If it still fails, it's shown with the claim marked "unbacked".

### Voices

- **Product voice:** a governed file in the repository, for example `docs/VOICE.md`. It sets register, banned phrases and examples, so contributors and agents share it.
- **Operator voice:** a file on the operator's machine, for example `~/.config/buildctl/voice.md`, never committed. A public repository shouldn't carry its maintainer's personal writing rules. With no operator voice configured, social drafts are skipped.

### Demos

A demo shows the product working, which is the strongest claim a launch can make. It's also the one draft that has to run the product, while every other draft only reads files. So demos get their own rules.

**A demo needs a demo entry point.** `build.yaml` names a command that starts the product on fictional data, with no credentials and no external services, for example `npm run demo`. With no demo command, buildctl skips demo drafts and says why. It never records the product against real configuration. Before starting the demo command, buildctl clears the environment of anything the build declares as credentials or connection settings.

**A demo is a scenario, then a recording.**
1. **The scenario is a draft.** The agent writes a scenario file from the docs and the product's interface: the steps to show, in order, with what each step should prove. For example: open the replica view; show one object out of step; run a full sync so the pipeline animates; show every object matching. The operator approves the scenario like any other draft. An approved scenario can be committed to the repository (for example `docs/demo/scenario.yaml`) so later releases re-record the same story.
2. **The recording follows the approved scenario.**
   - **Process:** buildctl starts the demo command fresh, drives a headless browser through the steps and records it. A recording must start from a fresh demo, because a demo already synced by an earlier attempt shows the wrong opening.
   - **Outputs:** a GIF (1280 px wide by default, under 5 MB, one theme throughout), one still screenshot per scenario step, and optionally an MP4.
   - **Tools:** the browser and the video encoder are operator-installed (Chrome and ffmpeg). buildctl adds no dependency to the project. If a tool is missing, buildctl says which one and stops; it doesn't download a substitute.
3. **The recording is checked before it's shown.** The agent inspects frames sampled at each scenario step and confirms each shows the expected screen with data, not a blank, loading or error page. Frames that fail are flagged in the Drafts panel with the step they belong to.

**Where the demo runs.** The demo command runs under buildctl's isolation backend in a disposable copy of the milestone's commit, with no outbound network.
- **Today's isolation is all or nothing.** A role either gets outbound network (`network_allowed`) or none. On macOS, "none" denies loopback connections too. On Linux, it means a private network namespace (`--unshare-net`) that a browser on the host can't reach.
- **Demos need a third mode:** a loopback listener the recorder can reach, with nothing outbound. On Linux, the simplest way is to run the headless browser inside the same sandbox as the demo. On macOS, the profile allows local loopback while still denying everything else.
- **Kernel impact:** either way it's an isolation change, so it belongs to the kernel batch in Part 1, with its own containment probe proving outbound connections still fail.

**The demo script** is a written draft in product voice: a talk track for presenting the same scenario live, or captions for the recording. Like other written drafts, its claims must cite sources, and the scenario steps count as sources once recorded.

**Later:** terminal demos for command-line products (a recorded shell session from a scripted scenario), using the same scenario, approval and frame-check steps.

### Approval

- **Where drafts live:** in the build's state folder, never in the repository.
- **The console:** it shows them in a Drafts panel with their claims ledger.
- **What the operator does:** edit, copy, or mark a draft approved or discarded. Approval records an event, and nothing more.
- **Never posts:** buildctl doesn't post anywhere and has no credentials for any outlet. A positioning draft that suggests a repository description stays a suggestion, applied by the operator.

### Cost

The drafts tool has its own paid operation, `release_draft`, recorded in its own ledger file in the state folder. It refuses to run if the build's spend today plus the drafts' estimate would pass `max_daily_cost_usd`.

## Build order

1. **Documentation role (kernel batch).** Ship it with mode `off` by default, the link and changelog gates, `DOC_RESULT.json`, and the review prompt criteria. Turn it on in one build first and compare its next release against the manual cleanup it would have needed.
2. **Release drafts (outside the kernel).** Start with release notes and positioning, both in product voice, then demos (scenario, recording, frame check and script), then the launch post, then social drafts once an operator voice file exists. Demos come before the launch post because a launch post is stronger with a recording to point at.

## How we'll know it works

- **Docs need no cleanup at release.** A build in `required` mode reaches a release with zero manual documentation fixes: no dead links, no stale version claims, and a changelog current through its last phase.
- **Drafts need no fact-checking.** A milestone's release notes and launch post need edits for taste only, because every claim already maps to a source.
- **Demos re-record themselves.** After a UI change, re-recording an approved scenario produces a correct GIF without hand-driving the browser, and every sampled frame passes the frame check.
- **The safety boundary holds.** No draft is ever published, no documentation diff ever lands unreviewed, and no demo ever runs with real credentials or network beyond loopback.

## Open

- **Gate strictness:** whether the changelog gate should also require an entry to name the phase's user-visible change, or only check that one exists. The first version only checks existence, and the reviewer judges content.
- **Draft sharing:** whether approved drafts should be exportable to the operator's own tools, such as a notes inbox. That's left to the operator's own scripts, the same way the fleet view leaves notes out.
