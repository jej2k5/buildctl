# Scope: discovery that produces a buildctl roadmap

Status: proposal, 2026-10-07. Nothing here is built. All four scoping decisions are made; next is v1.

Decided 2026-10-07: discovery is **general purpose**. Anyone who wants buildctl to build something uses it to produce the phase documents buildctl executes. It is not tied to one founder or to HONA client work.

## The question

Can buildctl run discovery with an operator and an end user, and turn what they agree into the phase documents buildctl executes?

Yes, but not inside the kernel, and not as a chat feature. Discovery's output *is* buildctl's governed input. That fact sets every other decision below.

## The constraint that drives the design

`build.yaml`, `AGENTS.md`, `CLAUDE.md`, `prompts/`, `templates/` and every file in `governance_documents` are governed (`supervisor/governance.py`). A phase that touches them fails. A change takes effect only after the operator commits it and runs `trust init`, typing the SHA prefix.

So discovery can only **propose**. An agent-run session must never write a live roadmap. It produces files in the working tree. The operator reads them, commits them and trusts the commit. That is the same edit, commit, trust ceremony PRODUCT.md already prescribes for governed files. Discovery adds no new authority. It changes who drafts the files, not who approves them.

That is what keeps the idea consistent with "governed autonomy". A discovery feature that skipped trust would be the first hole in it.

## What discovery must emit

The precedents already exist in two repos.

| Artifact | Precedent | Notes |
|---|---|---|
| `docs/PRODUCT_THESIS.md` | Relay fixture | Who it is for, what success means, in a sentence a test can check. |
| `docs/DISCOVERY_DECISIONS.md` | customer-truth-os (70 lines, 7 sections) | "Agents must treat them as current requirements unless a later ADR supersedes them." Add a stable id per decision (`D-14`) and who made it. |
| `docs/ARCHITECTURE_PRINCIPLES.md` | both | Invariants the reviewer enforces. |
| `docs/ROADMAP.md` | Relay | Phase list, MVP cut, milestone. |
| `build.yaml` `phases`, `milestones`, `checkpoints`, per-phase `acceptance` | Relay | **The hard part.** |
| `prompts/phases/phase-NN-*.md` | both | Short on purpose. customer-truth-os prompts run ~13 lines. |

The lesson from customer-truth-os: phase prompts are thin. The requirements live in governance documents and in acceptance contracts (`required_files`, `commands`, `allowed_paths`, `invariants`, `merge_policy`). Prose is easy to generate. An acceptance contract that is testable, sandboxed, network-free and scoped to the right paths is not. **Discovery is valuable only if it produces acceptance contracts, not just documents.**

Relay's phase 0 proves buildctl can already gate document-shaped work: eight constitution files, a Python shape check, `merge_policy: human`. Discovery is that pattern moved one step earlier, to before trust.

## Two roles, often one person

- **Requirements owner**: knows what should exist. May not know what a phase, gate or nonce is.
- **Operator**: accountable for `main`. Knows buildctl. Approves the roadmap by committing and trusting it.

Because discovery is general purpose, these may be one person (a founder building their own product) or two (a client and the engineer who runs buildctl for them). The design must work for both: the record says who made each decision, and only the operator's commit and `trust init` make the roadmap real.

The operator console binds to 127.0.0.1 behind a per-launch token, and PRODUCT.md says buildctl "isn't an agent chat". The interview should not happen there. It should happen wherever the requirements owner already talks to an agent. The operator reviews the result in the console.

## Where it runs: three options

| Option | Fit | Problem |
|---|---|---|
| **A. Inside the kernel** (`buildctl discover`) | Single tool. | The kernel is dependency-free and deterministic. `validate_config` requires roles to be exactly `{implementer, reviewer, arbiter}`, so a discovery role is a kernel and trust change. An interview is non-deterministic session state, the opposite of what the kernel is for. |
| **B. A separate distribution** (`buildctl-discover`, next to `buildctl-ui`) | Same move as the UI: outside the trust digest, reads the kernel, writes only proposals. | One more package to maintain. |
| **C. hona-studio emits buildctl format** | hona-studio already has a discovery stage (architect plus synthesis to Problem Brief) and an operator vs client role split. | Ties buildctl's roadmap format to a multi-tenant server product. The dependency points the wrong way. |

**Recommendation: B, with the output format as the contract.** Define the proposal format and a deterministic checker once, in `buildctl-discover`. General purpose rules out C as the home: buildctl users should not need hona-studio. hona-studio can still become one producer of the format later.

## The interview runs over MCP

Decided 2026-10-07. `buildctl-discover serve` is an MCP stdio server. Any agent the requirements owner already uses (Claude Code, Codex, Gemini CLI, Claude Desktop) conducts the interview and calls its tools. Every major agent client already speaks MCP, so there is nothing to build per vendor. agent-event-bridge relies on the same idea.

```
requirements owner
   | talks to any agent
   v
Claude / Codex / Gemini
   | MCP tools
   v
buildctl-discover serve
   |- session record (decisions, open questions, phases)
   |- proposal files (rendered, never hand-written)
   '- check  -->  buildctl validate
   v
operator: review -> commit -> trust init
```

### The record is the source of truth; files are a render

The agent never writes `build.yaml` or a phase prompt. It calls tools that append to a session record. The server renders the proposal files from the record deterministically. `check` re-renders and fails if the files on disk differ, so an agent that edits files with its own tools is caught. This one rule keeps the record, the decision ids and the files from drifting apart.

### Tools (first cut)

| Tool | Who | Does |
|---|---|---|
| `get_session` | any | Current record, open questions, check results. Lets any agent resume a session another agent started. |
| `set_thesis`, `add_principle` | any | Product thesis and the invariants the reviewer will enforce. |
| `record_decision` | any | Appends a decision with an id (`D-14`), its source (who said it, when), its confidence and what it supersedes. Decisions are never edited, only superseded. |
| `add_open_question`, `resolve_open_question` | any | The explicit list of what is still unknown. |
| `propose_phase`, `revise_phase`, `reorder_phases` | any | Name, objective, the decision ids it implements, and its acceptance contract: `required_files`, `commands`, `allowed_paths`, `invariants`, `merge_policy`. |
| `set_milestones` | any | The MVP cut, milestones and checkpoints. |
| `check` | any | Runs the deterministic gate below and returns each failure with the record entry to fix. |
| `render` | any | Writes the proposal into its own directory, outside the repo. |
| `request_review` | any | Runs the roadmap review and appends its findings to the record. |
| `accept_risk` | operator | Marks an open question as an accepted risk. |
| `finalize` | operator | Runs `check`, then copies the proposal into the working tree, but only if buildctl holds no lock and the build is not running. It never commits. |
| `read_trusted_roadmap`, `read_handoffs` | any | Amendment mode: the current trusted roadmap and each handoff's known debt and next action. |

### Rules carried over from buildctl and agent-event-bridge

- **Role is pinned at launch, not passed as a tool argument.** `buildctl-discover serve --role operator` registers the operator tools; the default role does not. An owner's agent cannot call itself the operator. agent-event-bridge pins identity the same way.
- **The session lives outside the repo,** under `$XDG_STATE_HOME/buildctl-discover/<hash>`, like buildctl state. Decided 2026-10-07: the record is one append-only, hash-chained JSONL file per session, reusing the kernel's `AuditChain` (tamper-evident, not a compliance ledger). State is rebuilt by replaying events, and a lock file serializes writers, so it is restart-honest and readable by eye.
- **Every decision records who made it.** When the owner and the operator are different people, the operator can see which requirements came from whom.
- **`buildctl-discover` is its own distribution,** under `discover/` in this repo, packaged like `ui/` (`dependencies = ["buildctl", ...]`). It may depend on the MCP SDK. The buildctl kernel stays dependency-free and outside this package's reach, apart from calling `buildctl validate`.

## The flow

1. **Interview (requirements owner).** Any MCP-capable agent, one question at a time. Each answer becomes a decision record with an id, its source ("end user, 2026-10-07") and a confidence level. Unresolved items go to an explicit open-questions list.
2. **Synthesis.** Thesis, decisions and principles drafted from the record.
3. **Phasing.** The agent proposes phases, the MVP cut, checkpoints, and an acceptance contract per phase. Every phase prompt cites the decision ids it implements.
4. **Adversarial review.** A second model reviews the roadmap, not code, under its own protocol (`buildctl-roadmap-review-v1`). It reuses the project's read-only reviewer command and, where possible, a different vendor from the agent that ran the interview. It looks for untestable acceptance, phases too large for `max_phase_minutes`, `allowed_paths` that overlap or leave gaps, decisions no phase implements, and gates that need network. Disagreements go to agent-debate, as they do today.
5. **Deterministic gate.** `buildctl-discover check` must pass before the operator sees anything (see below).
6. **Operator sign-off.** `finalize` copies the files into the working tree only. The operator reads them, commits them and runs `trust init`. The ceremony is unchanged.

## The deterministic gate

Like Relay's phase 0 shape check, but for the proposal:

- `buildctl validate` passes on the proposed `build.yaml`.
- Every decision id is implemented by at least one phase, or is marked deferred with a reason.
- Every phase prompt cites at least one decision id, and every cited id exists.
- No CRITICAL or HIGH roadmap-review finding is open.
- The open-questions list is empty, or each remaining entry is marked as accepted risk by the operator.
- No phase relies on the default acceptance alone. Each one has at least one command, or one required file, of its own.
- `allowed_paths` never include governed paths.
- The proposal files on disk are byte-identical to a fresh render of the record.
- `docs/DISCOVERY_DECISIONS.md` is listed in `governance_documents` and in `roles.reviewer.context_documents`.

## Amendment mode is probably the bigger use case

customer-truth-os did not come from a single discovery session. Its roadmap arrived in one commit (`cf23723`, 2026-08-20). Phases 34 to 38 were then added one or two at a time between 2026-09-06 and 2026-09-08, after earlier phases had run.

So discovery should also run against a live project: read the trusted roadmap, the handoffs (their "known debt" and "next action" sections) and the ADRs, then propose new phases with the same traceability. Two rules apply:

- Amendment runs only while the build is paused or stopped at a checkpoint, because the new commit needs a fresh `trust init`.
- An amendment can append phases or change phases that have not run. It can never rewrite a completed phase.

## Phasing the work

- **v1: record, render, check, over MCP.** The session record, the deterministic renderer, `check`, and `buildctl-discover serve` with the tools above. Plus a short interview guide that the server exposes as an MCP prompt, so every agent starts the same way. No UI. This is the smallest version that proves acceptance contracts can be drafted well by any agent.
- **v2: amendment mode.** `read_trusted_roadmap` and `read_handoffs`, plus the rule that completed phases are read-only.
- **v3: console view.** A "Proposal" view in `buildctl-ui` showing the roadmap diff, decision traceability and checker results, next to the existing trust ceremony.
- **Later: other producers.** Anything that can emit the record format, such as hona-studio, can feed `check` and `finalize`.

## Decisions

1. ~~Who is the end user?~~ Decided: general purpose. Anyone, as founder or on someone else's behalf.
2. ~~Are decision records governed?~~ Decided: yes. `render` adds `docs/DISCOVERY_DECISIONS.md` to `governance_documents` and to the reviewer's `context_documents`, so no phase can edit requirements and every change goes through discovery, a commit and `trust init`. (customer-truth-os did not govern it; only `allowed_paths` stopped a phase from rewriting its decisions.)
3. ~~Where does the interview run?~~ Decided: an MCP server.
4. ~~Which protocol reviews a roadmap?~~ Decided: a separate one, `buildctl-roadmap-review-v1`, owned by `buildctl-discover`. `buildctl-review-v2` is built around code changes and tree hashes and stays untouched, so the kernel needs no change. The review runs through a `request_review` tool; findings land in the record, and `check` fails while a CRITICAL or HIGH finding is open.
