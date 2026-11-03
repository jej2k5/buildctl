# Spec: debates buildctl runs itself

Status: specified 2026-11-03 against `f7ccdd7`. Built 2026-11-03 on `feature/headless-debate`, tested with scripted agents and the console's demo build (`buildctl-ui --demo debate`). No real Codex or Claude debate has run yet, and the console page has not been checked by eye.

Decided 2026-11-03, by JJ:

| # | Question | Chosen | Rejected |
|---|---|---|---|
| 1 | Who runs the debate? | buildctl, unattended, inside the build. | A human in agent-debate's two terminals (stays available as `interactive`). |
| 2 | Does buildctl depend on agent-debate? | No. It carries its own copy of the rules: turn order, a two-sided close, submitted text matches shown text. | Driving agent-debate's store and bridge. |
| 3 | Is the debate recorded? | Yes, through agent evidence. Every turn is an ordinary recorded agent call. | A separate transcript format. |
| 4 | What if the agents don't agree? | Stop and wait for the operator. | The finding wins; the code wins. |
| 5 | Where does the operator answer? | In the console, from a stop that shows the transcript. The same decision is a CLI command. | CLI only; editing a result file and retrying. |

Decided by Claude, open to change:
- Codex argues for the code as built, Claude argues for the reviewer's findings. Each side uses the role it already has, so its login, pinned binary and record need no new setup.
- Codex speaks first, because the findings already stand as the opening case.
- The operator's three answers are: the finding stands, the code stands, or a decision in their own words.
- When the code stands, the phase is reviewed once more before it commits.
- A debate is capped at 6 turns, 3 each.
- The web console comes first; the terminal UI follows, as it did for the feed.

## The question

When the reviewer and the implementer disagree about design, can the agents settle it inside the build, with the same sandbox, budget and evidence as every other step, and hand the operator only what they could not settle?

Yes. The kernel already routes to a debate, consumes a `DEBATE_RESULT`, writes an ADR and has Codex implement it (`buildctl.py:392`). What is missing is the debate itself. Today it is either one arbiter call, which is a ruling and not a debate, or a human-run session outside buildctl that leaves only `human_run` in the receipt.

## The mode

A new `roles.arbiter.mode`: `"debate"`. It retires the limitation in `09-limits.md`: non-interactive arbiters receive only `{prompt}` and `{output}`. In debate mode the arbiter has no command of its own; the turns use the implementer and reviewer commands.

```json
"arbiter": {"mode": "debate"},
"limits": {"max_debate_rounds": 1, "max_debate_turns": 6},
"operation_cost_usd": {"debate_turn": 2.0}
```

`max_debate_rounds` keeps its meaning: debates one phase may hold (`buildctl.py:374`). `max_debate_turns` is new and caps turns within one debate. `debate_turn` is the reservation for each turn. The existing `debate` cost key stays for the other modes.

## A turn

Each turn is one `_agent_operation` with a stable id, `phase-N:debate-D-turn-T:A`, and its own output file `DEBATE_TURN-D-T.json`. A restart replays finished turns from cache like any other operation, so a stopped debate resumes at the turn it reached.

The prompt carries the strategic findings (`review.py:278`), the evidence digest, the transcript so far as untrusted data, and the pinned contract. A new contract, `DEBATE_TURN`, joins `protocol.py`:

```json
{"argument": "...", "proposal": null, "accept": null}
```

- `argument`: the turn's case, free text.
- `proposal`: optional. A full `DEBATE_RESULT` with status `agreement`, offered to the other side.
- `accept`: optional. The sha256 of a proposal the other side made on the previous turn.

The debate closes when one side accepts the other's proposal by hash, the same two-sided handshake agent-debate uses. buildctl then writes that proposal as `DEBATE_RESULT-D.json`. From there nothing changes: `validate_debate_result`, `write_adr` and `adr-implement` run as they do now.

A side that accepts a proposal never offered, or accepts its own, fails the turn check with `DEBATE_TURN_INVALID`, and the build stops at `AGENT_OPERATION_FAILED` like any malformed agent result. (Built simpler than first specified: no format retries for turns.)

## Roles, sandbox and auth

The turns reuse the implementer and reviewer roles: their commands, `auth_mode`, pinned binaries and logins. Three places in `agents.py` key on the role name, and debate turns change each one:

| Path | Today | For a debate turn |
|---|---|---|
| Read-only workspace (`agents.py:196`) | `role == "reviewer"` | Also when the context is marked `debate=True`. Codex cannot edit code mid-debate. |
| Codex login staging (`agents.py:184`) | `role == "implementer"` | Unchanged. Codex debates as the implementer and keeps its staged login. |
| Host session profile (`agents.py:179`) | Host-authenticated reviewer | Unchanged. Claude debates as the reviewer. |

`_agent_operation` maps `PaidOperation.DEBATE` to the role `"arbiter"` for the login check and the `operation_started` event (`buildctl.py:508`). A new `PaidOperation.DEBATE_TURN` takes the speaking role as an argument.

A Codex turn under a read-only workspace with `--dangerously-bypass-approvals-and-sandbox` relies on buildctl's sandbox alone, as the reviewer does. `REVIEWER_MUTATED_WORKSPACE`'s digest check is extended to every debate turn.

## When they don't agree

After `max_debate_turns` turns without a close, the build stops at a new code, `DEBATE_AWAITING_DECISION`. It joins `WAITING_CODES`: caution, not danger, and not counted toward `max_consecutive_failures`.

It replaces `DEBATE_UNRESOLVED` for this mode, for a concrete reason: that stop's only fix is "decide the question, adjust the phase prompt, then retry". The phase prompt is a governed file, so every unresolved debate means a commit, a re-trust and an isolation pass. The new stop needs none of that.

The kernel offers one decision, in three forms:

| Answer | What buildctl writes | What happens next |
|---|---|---|
| The finding stands | A `DEBATE_RESULT` whose dispositions require the findings' fixes | ADR, then `adr-implement`, as after an agreement |
| The code stands | The strategic findings closed with a new supervisor-owned status, `operator_accepted` | One more review, which cannot cite those findings. A clean review advances. |
| The operator's own decision | A `DEBATE_RESULT` carrying their text as the decision and required changes | ADR, then `adr-implement` |

`operator_accepted` joins `CLOSED_FINDING_STATUSES` (`review.py:33`), as `superseded` did. Reviewers cannot author it. The extra review keeps the core rule that only reviewed bytes commit: the earlier review's verdict was `DEBATE`, not `PASS`, and the reducer refuses to advance on that verdict (`R1_UNEXPLAINED_HOLD`).

Every ADR gains a `Decided by` line: `agents` after a close, `operator` after a decision. An operator ruling is never presented as an agent agreement. `write_adr` takes the field in its metadata.

## The decision, kernel first

The kernel command comes first, then the lever, then the console. That is the rule `levers.py` states: a front end may only offer what the kernel accepts.

1. **Command.** `buildctl debate decide --nonce N --answer finding-stands|code-stands|own [--text TEXT]`. Like `approve`, the nonce binds the decision to the state it was made against, so a decision made against a build that has since moved is refused.
2. **Lever.** `decide` takes lever number 12. Its conditions: the build is stopped at `DEBATE_AWAITING_DECISION`, and the debate's binding (phase, base SHA, governance SHA, evidence digest) still holds.
3. **Console.** The stop panel shows the findings, the transcript turn by turn with each side's last proposal, and a form with the three answers and a text box for the third. The text goes through the same redaction as receipts. The form posts the displayed nonce. Levers today carry only a confirm token, so the server's POST grows a decision body (`server.py:94`), still same-origin JSON only.

The terminal UI gets the same lever, with the decision text read from an editor. It follows the console, as with the feed.

## Evidence

Most of this comes from agent evidence unchanged: each turn is a recorded call, shows in "What agents did" as it runs, and is listed in the receipt with its actions, cost and record hash. Four things change:

- **The change check.** `discrepancies()` selects implementer records by `header.role` (`evidence.py:584`). A Codex debate turn is still the implementer, so the check filters by operation kind instead, or every debate produces false `unreported_change` flags.
- **Labels.** Debate turn artifacts are `agent_reported`. The operator's decision is a new source, `operator`, added to the "who vouches" table in `05-running.md`. `human_run` stays for `interactive` mode.
- **What a receipt says it does not prove.** The `not_proved` text that names interactive debates (`evidence.py:40`) applies only to that mode.
- **The transcript.** Arguments stay in the full records on the operator's machine, as all agent text does. The receipt proves them by hash and does not carry them.

## Is it worth it

Nothing yet shows that two agents arguing catch more than one review. Debate mode costs up to `max_debate_turns × debate_turn` per dispute, $12 at the defaults above, against $5 for one arbiter call today.

So the build records one number per debate: whether the outcome changed what the reviewer asked for. The values are `findings_upheld`, `findings_changed`, `findings_dropped` and `operator_decided`. It is an audit event and a column in the console's ledger. After a handful of real debates it answers whether the mode pays for itself.

## Risks

- **The deadline.** Turns spend the phase's wall-clock budget (`_remaining_seconds`). With bug #1 (the deadline starts before the checkpoint wait), a debate late in a long phase may time out. Fixing #1 first is the safer order.
- **Two copies of the rules.** agent-debate and buildctl will each hold the turn and close rules. If they drift, pull them into a shared package. Not before.
- **Re-trust.** It is a kernel change, so every governed repo runs `trust init` and `isolation verify` again.

## Docs touched

`02-how.md` (the debate row), `05-running.md` (the interactive gap paragraph and the source table), `06-stops.md` (`DEBATE_AWAITING_DECISION`, and `DEBATE_UNRESOLVED` scoped to the other modes), `07-console.md` (the decision panel), `08-reference.md` (the mode, the limit, the cost key, `debate decide`), `09-limits.md` (retire the arbiter placeholder limitation).

## Out of scope

- Moderator interjection during an unattended debate.
- Showing a buildctl debate in agent-debate's control room.
- More than two sides, or agents other than Codex and Claude.
