# Spec: a live view and portable evidence of agent work

Status: specified 2026-10-30 against `f8bb41d`. Not built. No agent CLI has been run with event flags yet, so the event claims below are from `--help` output (Codex 0.164.0, Claude Code 2.1.314) until step 1 proves them.

Decided 2026-10-30, by JJ:

| # | Question | Chosen | Rejected |
|---|---|---|---|
| 1 | What is it for? | Watching a phase live, and checking the work before approving. Framed as an audit trail. | Debugging stops as the primary job. |
| 2 | How much weight does it carry? | Evidence, with live visibility. Each entry is labelled observed or agent-reported. | Context only. |
| 3 | Who must be able to accept it? | Anyone with the repo, first. The format is ready for signing. | Local only; outsiders from day one. |
| 4 | What goes in the shared bundle? | Two layers: actions plus a hash of the full record. The full record stays local until JJ prunes it. | Actions only; everything. |
| 5 | When the evidence breaks? | Stop on capture failure. Flag mismatches and keep going. | Flag both; stop on both. |
| 6 | Where do receipts live? | A separate evidence branch. | In the code branch; git notes. |
| 7 | What does the reviewer see? | Only the discrepancy flags. | Nothing new; the full record. |
| 8 | What does the live view show? | A status strip, an activity feed, and click-through to the full record. | Strip only; feed only. |
| 9 | When can the operator review the work? | After the agent finishes, using `merge_policy: human` on the phases worth checking. Confirmed 2026-10-31. | A pause on every phase; a new kind of checkpoint. |
| 10 | Can recording be turned off? | Yes, the operator can turn it off. Added 2026-10-31. | Always on. |

Decided by Claude during discovery, open to change:
- The web console comes first. The terminal UI reads the same model and follows.
- The live view is watch-only. Stopping a phase stays with the existing controls.
- The evidence branch follows `git.push`.

## The question

Can buildctl show what each agent is doing during a phase, and prove afterward what each agent did?

Partly, and the split is the design. buildctl can show everything an agent reports, as it happens. It can prove what it observed itself, and that a record has not changed since it was sealed. It cannot prove an agent's report is true or complete, because the agent writes that report.

## The constraint that drives the design

The agent is the author of most of its own record. Every `read` and `ran` row comes from the agent CLI's event stream, and the sandbox does not trace them. So every row carries its source, and nothing may present a reported row as a fact.

| Source | Rows | Who vouches |
|---|---|---|
| **Observed** | Launch, argv, prompt hash, binary sha256, exit code, duration, time of last output, workspace manifest before and after (`git.py:85`), attributed delta, gate results, ledger entries | buildctl |
| **Agent-reported** | Files read, files edited, commands run and their exit codes, tool calls, reasoning text, token usage | The agent |

Version 1 checks one thing across that line: reported writes against the attributed delta. "I ran the tests" stays agent-reported until processes are traced inside the sandbox, which is out of scope here and may differ between macOS and Linux.

## Capture

`RoleRunner.run` blocks on `subprocess.run(capture_output=True)` (`agents.py:220`), then keeps stdout only when no result file exists and discards stderr on success. That call becomes a streaming launch that writes each line to a record as it arrives.

**Commands.** The flags go in `roles.*.command` in `build.yaml`, which is governed, so each repo needs an edit and a re-trust. With recording on, a role command without them is refused at config validation in every auth mode, so the refusal comes before a run rather than during one.

| Role | Addition | Result comes from |
|---|---|---|
| Implementer (Codex) | `--json` | `--output-last-message`, unchanged |
| Reviewer (Claude) | `--output-format stream-json`, plus `--verbose` if step 1 shows it is required | The final `result` event |
| Arbiter, non-interactive | Whatever its CLI offers; otherwise raw lines only | Unchanged |

The reviewer change has a trap. `agents.py:231` writes stdout to the result file when no file exists, which would turn the event stream into the review result. The reviewer's result must be extracted from the final event by a per-CLI adapter, and that fallback removed for streaming roles.

**Interactive debates have no stream.** With `arbiter.mode: interactive`, a human runs the debate outside buildctl (`debate.py:28`), so nothing can be captured. The receipt records the request and result hashes, labelled `human_run`. This is a declared gap and does not count as a capture failure. Importing the agent-debate transcript is later work.

**Gates** are launched by buildctl, so their results are observed rows.

**The record.** One file per operation: `<control_root>/records/phase-<N>/<operation>.jsonl`, mode 0600. Each line holds a sequence number, a timestamp, the stream (`stdout` or `stderr`), the raw text and a hash link to the previous line, following `discover/buildctl_discover/record.py`. The first row holds the prompt, which the kernel built. Closing the record appends one `agent_record_closed` event to `AuditChain` with the file's sha256 and line count. Individual actions do not go in the chain, because `AuditChain.append` re-verifies the whole file on every call.

**Secret scanning moves earlier.** `classify_secret_exposure` runs once over the finished output today. Once output is written to disk and shown live, each line must be scanned before it reaches the record or the console. A hit kills the process, writes a marker row naming the credential kind but not its value, and raises the existing `{ROLE}_SECRET_OUTPUT` stop.

A line-by-line scan alone would be weaker than today's. A multi-line secret such as a PEM key spans lines, and in JSONL a secret containing quotes, backslashes or newlines arrives escaped, so a raw match misses it. So each event's decoded strings are scanned as well as the raw line, and the whole record is scanned again at close. A hit at close still raises the stop, and the operation's result is not used, but by then the secret is in the local record, so that record must be treated as sensitive.

This scan only knows the credentials buildctl was given. Anything else the agent prints, such as a password in a committed file, goes into the local record.

**What counts as capture failure.** The new stop is `AGENT_RECORD_INCOMPLETE`. The agent is killed when it fires, which stops further spend, though the flat reservation is already booked and the provider bills tokens used up to the kill.

| Event | Result |
|---|---|
| A write to the record fails, for example a full disk | Stop |
| A single line exceeds 64 MiB | Stop |
| Exit 0 with no final event from the adapter | Stop |
| Supervisor crash leaves a record unclosed, and that workspace carries into the commit | Stop |
| The same, but the attempt's workspace was discarded | Labelled incomplete, no stop |
| One unrecognized event type | Kept raw, labelled `unparsed`, counted in the receipt, no stop |
| Timeout kills the agent | The record closes as `terminated: timeout`. It is a complete record of a failed operation, and the existing timeout path handles it. |

Unrecognized events do not stop the build, so most format changes in a CLI update will not either. Losing the final event will. Binary pinning means a format can only change at re-trust, not mid-run, and step 1's fixtures catch it in CI.

## Discrepancy flags

Computed after `attributed_delta` (`buildctl.py:303`) and before review. Flags never stop the build.

| Flag | Meaning |
|---|---|
| `changed_unreported` | The delta holds a file with no reported write, and no command ran in that operation |
| `changed_possibly_by_command` | The same, but the agent ran shell commands, which can write files without a file event |
| `reported_no_net_change` | A reported write that is not in the delta, for example a reverted edit |
| `reviewer_tool_outside_allowlist` | The reviewer's stream shows a tool beyond its `--tools` list |
| `events_unparsed` | The operation has `unparsed` rows, so its comparison is weaker |

Expect noise from the first two, which is why they are separate. Measure the false-positive rate on real runs before any flag is tightened into a stop.

**The reviewer** gets one new section beside `implementer_claims` (`claude.py:48`): `untrusted("evidence_discrepancies", ...)`. File names are agent-controlled, so the section is wrapped like every other input. `protocol_version` and `prompt_lineage` stay as they are, because the output contract and reviewer identity do not change. Flags from remediation attempts are recomputed.

## The receipt

**The branch.** `buildctl/evidence`, an orphan branch that never merges with code. `ensure_safe` refuses any uncommitted work in the repository (`git.py:56`), so the receipt cannot pass through the working tree. It is written with `hash-object`, `mktree`, `commit-tree` and `update-ref`. It is append-only, and each commit carries a `Code-Commit: <sha>` trailer.

**The clone should leave it out.** `create_isolated_clone` copies all branches, and `git remote remove` drops the refs but not the objects, so agents could read earlier receipts from inside their workspace. Because `ensure_safe` has already proved the base is `HEAD`, the clone gains `--single-branch`, and a test checks the workspace with `git cat-file --batch-all-objects`. This is defensive, not load-bearing: receipts are built to be shared, so nothing relies on agents not seeing them. It also removes the `build/phase-N` branches agents see today. If that breaks anything, drop this change rather than design around it.

**Layout:** `phase-<N>/<code-commit>/manifest.json`, canonicalized exactly as `audit.py` does it.

```json
{
  "schema_version": 1,
  "phase": 12,
  "code_commit": "<sha>", "tree_hash": "<sha>", "base_sha": "<sha>", "governance_sha": "<sha>",
  "previous_manifest_sha256": "<sha256 or null>",
  "attempts": [{
    "attempt": 1,
    "operations": [{
      "role": "implementer", "binary_sha256": "<sha256>", "prompt_sha256": "<sha256>",
      "started_at": "...", "ended_at": "...", "exit_code": 0, "record_state": "closed",
      "record_sha256": "<sha256>", "record_lines": 412, "unparsed_events": 0,
      "actions": [
        {"seq": 18, "at": "...", "kind": "ran", "target": "pytest tests/billing",
         "exit_code": 0, "source": "agent_reported", "redacted": false}
      ],
      "flags": []
    }],
    "gates": [{"file": "GATE_RESULT-1.json", "sha256": "<sha256>", "passed": true, "source": "observed"}],
    "review": {"file": "REVIEW_RESULT-1.json", "sha256": "<sha256>", "verdict": "..."}
  }],
  "signatures": []
}
```

Every attempt is included, failed ones too. An approver should see that the code took three tries. A phase that never commits has no receipt, and its records remain local.

**Signing.** `signatures` stays empty for now. A later signature covers the canonical manifest with that field emptied, so adding one changes no schema and no earlier receipt. Outside timestamping would use the same hash.

**Sealing.** The manifest is sealed right after `apply_and_commit` (`buildctl.py:419`), before the merge approval at `:422` and before `_finish_phase`. An `evidence_sealed` event carries the manifest hash into `AuditChain`. Writing a ref does not move `HEAD` or its tree, so pending approvals stay valid.

The restart path at `buildctl.py:260` must handle a phase already committed. If there is no receipt and its records are intact, it seals one, idempotently. If the records are incomplete, it raises `AGENT_RECORD_INCOMPLETE`. Without this, a crash between commit and seal would publish code with no evidence.

**Publishing.** With `git.push` on, the evidence ref is pushed before the phase branch in `publish`, so code never reaches the remote without its receipt. A failed push follows the existing `GitSafetyError` path. With `git.push` off, the console shows "receipts not pushed".

**Squash merges change the commit.** `publish` can run `gh pr merge --auto --squash` (`git.py:256`), so the commit on `main` will not be the receipt's `code_commit`. `verify` falls back to matching the tree and says so. If `main` moved before the merge, the tree will not match either, and `verify` reports no receipt. That is a known limit of that merge path. `git.push: false` fast-forwards and has neither problem.

**Sensitive content in receipts.** Actions hold file paths and command lines, and an agent can put a password on a command line. Before sealing, each action passes the known-credential scan and a pattern redactor (bearer tokens, `KEY=value`, credentials in URLs), and any replacement sets `redacted: true`. This lowers the risk but does not remove it. On a public repo with `git.push` on, the receipts are public too, and a pushed secret is expensive to remove. The manual must say so.

## Commands

| Command | Does |
|---|---|
| `buildctl evidence show <commit>` | Prints the receipt, attempt by attempt. |
| `buildctl evidence verify <commit>` | Works on any clone with the branch: no sandbox, trust or control root. Checks that the receipt exists, the tree matches, the manifest parses in canonical form, and the `previous_manifest_sha256` links hold. Given a full record, it also checks the record's hash and internal links. |
| `buildctl evidence prune --phase <N> --confirm <N>` | Deletes that phase's full records and appends `evidence_pruned`. Receipts are untouched. Nothing prunes automatically. |

`verify` reports one of `verified`, `verified_with_flags`, `verified_by_tree`, `no_receipt` or `failed`, and always prints what it did not prove. Once a record is pruned, its hash shows only that a record existed, and `verify` says that too.

## What this does not prove

- That an agent's report is true or complete. Only the observed rows and the discrepancy flags go beyond the agent's word.
- That the receipt was not rewritten before it was shared. Anyone with the machine can rewrite the branch and the audit chain. This is the honest-operator assumption of option B, and only signing plus an outside log closes it.
- Why an agent did something. Reasoning text is whatever the CLI chooses to expose.
- Anything that happened in an interactive debate.

## The live view

A read-only, token-protected route: `GET /api/activity?phase=&operation=&after=<seq>`. The console polls it, as it does the audit view. It parses the record on each read, so the record stays the single source.

- **Status strip, on the Line view.** Phase, role, elapsed time, time since last output (observed), reported file count (labelled), and spend. It says "quiet for 12 min", never "stuck", because a long test run and a hang look the same.
- **Spend is not live cost.** The ledger reserves a configured flat amount per operation (`operation_cost_usd`), so the strip shows that figure, plus token usage if the agent reports it, labelled as reported. It cannot show a figure like the `$1.84` in the discovery mockup.
- **Activity feed, on the phase detail.** One row per action with its source label. Selecting a row opens that part of the local record.
- **At an approval, on the Stop view.** Flags sit beside the existing evidence and the approve lever.

**Agent text is hostile input.** The console holds the levers and the session token. `app.js` already uses `innerHTML` and the terminal UI uses `Text.from_markup`. Every agent-authored string goes through `textContent` on the web and plain `Text` in the terminal, with control and ANSI characters stripped. A test feeds a hostile payload through both.

## When you can check before approving

A `before_phases` checkpoint runs before the phase's clone exists (`buildctl.py:265-268`). No agent has run yet, so it can only show the previous phase's receipt. The checkpoint that fits this job is `merge_policy: human`: it fires after the commit and before publication (`:422`), when the phase's whole record exists.

So no new kind of checkpoint is added. A phase you want to inspect before it lands gets `merge_policy: human`.

## Turning recording off

`evidence.recording` in `build.yaml` is `on` or `off`, and applies to the whole build. It defaults to `on`, so leaving it out never drops evidence silently. An existing repo that upgrades without the event flags is refused, and the refusal names both ways forward: add the flags, or set `off`.

`build.yaml` is governed, so turning recording off takes a commit and a re-trust. A phase that touches the file fails, so an agent cannot turn off its own recording.

With `off`, agents run through today's blocking launch and today's secret scan, unchanged, and the event flags are not required.

| With recording off | |
|---|---|
| Activity feed and full records | None |
| Discrepancy flags, and the reviewer's flag section | None |
| Receipts and the evidence branch | Nothing written |
| `AGENT_RECORD_INCOMPLETE` | Cannot fire |
| Status strip | Elapsed time, role and ledger spend only. Time since last output is unavailable, because the launch blocks. The console says "recording off". |
| Audit chain, gates, reviews, ledger | Unchanged |

**The evidence gap is visible but not explained.** `verify` reports `no_receipt` for a phase built with recording off, the same as for a missing receipt, and says it cannot tell which. A receipt saying "recording was off" would need the branch that an operator who turned this off did not ask for. The `previous_manifest_sha256` links skip those phases, so turning recording back on does not break the chain.

## Build steps

1. **Spike, no kernel change.** Run real Codex and Claude with the event flags on a throwaway repo. Save sanitized streams as test fixtures. Confirm whether Claude needs `--verbose`, which events report commands and file edits, and which event is final. Stop and revise this spec if either CLI cannot report commands and writes. This spends real API money.
2. **Capture.** The `evidence.recording` setting, streaming launch in `RoleRunner` alongside today's launch for `off`, line-by-line scan, records, the `AGENT_RECORD_INCOMPLETE` stop, entries in `inspect.py` and `levers.py`. Streaming needs its own injected runner, so tests of the new path need new fakes, and tests with recording off must prove today's behavior is unchanged.
3. **Adapters and flags.** A parser per CLI, result extraction for the reviewer, the discrepancy pass.
4. **Sealing.** The manifest, the evidence ref, the restart path, the `--single-branch` clone, the push order.
5. **Commands.** `evidence show`, `verify`, `prune`.
6. **Reviewer section.**
7. **Console, then terminal UI.** With the `/impeccable` skill, under `DESIGN.md`.
8. **Docs and migration.** `discover/buildctl_discover/record.py` defaults, the roadmap example, the manual chapters on stops, console, reference and limits, and the `build.yaml` change for existing repos.

## Cost to operators

- **Re-trust in every governed repo.** The kernel and `build.yaml` both change, and one pass covers both. The Linux reviewer gap noted on 2026-10-20 still awaits JJ's go, and fixing it before that pass would share the re-trust. It is not part of this spec.
- **More disk.** Full records are kept until pruned and include prompts and command output. The size per phase is unknown until step 1 produces real streams.
- **New stops.** `AGENT_RECORD_INCOMPLETE` can halt a long run, by decision 5.
