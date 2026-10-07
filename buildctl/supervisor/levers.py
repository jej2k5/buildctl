"""Which actions the supervisor will accept right now, and the conditions that decide it.

The predicates here are the ones `Supervisor.retry`, `approve`, `reconcile`, `pause`, `resume`
and `run` enforce, so a front end that renders levers from this module cannot offer an action
the kernel would refuse, or hide one it would take. The kernel still re-checks everything when
the command runs; this is the explanation, not the enforcement.

A lever's `confirm` names the evidence the operator must type before a front end runs it. The
nonce is never typed: the front end submits the nonce it displayed, so an approval made against
state that has since moved is refused by the kernel.
"""
from __future__ import annotations

from typing import Any

from .state import BuildState

# A lever keeps its number whatever state the build is in, like a lever in a signal-box frame.
# Start and its stale-run form share a lever; so do the two forms of retry.
LEVER_NUMBERS = {"start": 1, "recover": 1, "pause": 2, "resume": 3, "approve": 4, "retry": 5, "grant": 5,
                 "reconcile-accept": 6, "reconcile-retry": 7, "trust": 8, "isolation": 9, "auth": 10,
                 "bootstrap": 11}

STOPPED_STATUSES = frozenset({"human_escalation", "failed"})
UNRUNNABLE_STATUSES = frozenset({"paused", "human_escalation", "failed", "running"})


def is_stopped(state: BuildState) -> bool:
    return state.status in STOPPED_STATUSES


def can_pause(state: BuildState) -> bool:
    return state.status in {"running", "idle"}


def can_resume(state: BuildState) -> bool:
    return state.status == "paused"


def unknown_operations(state: BuildState) -> list[str]:
    return [key for key, value in state.operations.items() if value.status == "unknown"]


def is_ceiling(state: BuildState) -> bool:
    return bool(state.escalation_code and state.escalation_code.endswith("_EXHAUSTED"))


def grant_matches(state: BuildState) -> bool:
    """The grant in state names this phase, this ceiling and the attempt that hit it."""
    grant = state.extra_attempt_grant
    return bool(grant and grant.phase == state.current_phase and grant.ceiling_code == state.escalation_code
                and grant.attempt_generation == state.generation - 1)


def approval_is_current(state: BuildState) -> bool:
    requirement = state.pending_approval
    return bool(requirement) and requirement.state_generation == state.generation - 1


def approval_scope_holds(state: BuildState, tree_hash: str | None) -> bool:
    requirement = state.pending_approval
    if not requirement:
        return False
    attempt = state.phase_attempts.get(str(requirement.phase), 0)
    return attempt == requirement.phase_attempt and tree_hash == requirement.tree_hash


def _condition(identifier: str, label: str, held: bool | None, detail: str = "") -> dict[str, Any]:
    # held is None when the kernel only learns the answer at call time (agent CLI login).
    return {"id": identifier, "label": label, "held": held, "detail": detail}


def _lever(action: str, label: str, argv: list[str], conditions: list[dict[str, Any]], *,
           confirm: dict[str, Any] | None = None, detached: bool = False, spends: bool = False,
           summary: str = "") -> dict[str, Any]:
    return {"action": action, "number": LEVER_NUMBERS[action], "label": label, "argv": argv, "summary": summary,
            "enabled": all(item["held"] is not False for item in conditions),
            "conditions": conditions, "confirm": confirm, "detached": detached, "spends": spends}


def levers(state: BuildState, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Every lever, enabled or locked, with the interlocking that decides it.

    `facts` carries what state alone cannot: trust, isolation, bootstrap, lock, budget, the
    current tree hash, the remaining phases and the commit a trust init would pin.
    """
    trusted = facts["trust_ok"]
    fresh = facts["isolation_fresh"]
    bootstrapped = facts["bootstrapped"]
    lock_free = not facts["lock_held"]
    budget_ok = facts["budget_ok"]
    unknown = unknown_operations(state)
    result: list[dict[str, Any]] = []

    trust_condition = _condition("trust", "Trust anchor matches the kernel and snapshot", trusted,
                                 facts.get("trust_detail", ""))
    isolation_condition = _condition("isolation", "Sandbox verified in the last 24 hours", fresh,
                                     facts.get("isolation_detail", ""))
    lock_condition = _condition("lock", "No other buildctl process holds the state lock", lock_free,
                                facts.get("lock_detail", ""))

    stale_run = state.status == "running" and lock_free
    run_conditions = [
        trust_condition, isolation_condition, lock_condition,
        _condition("status", "Build is idle", state.status not in UNRUNNABLE_STATUSES or stale_run,
                   f"status is {state.status}"),
        _condition("remaining", "Phases remain to run", bool(facts["remaining_phases"]),
                   f"{len(facts['remaining_phases'])} remaining"),
        _condition("budget", "Daily budget has room for the next call", budget_ok, facts.get("budget_detail", "")),
        _condition("auth", "Both agent CLIs are logged in", None, "checked when each agent call starts"),
    ]
    if stale_run:
        result.append(_lever("recover", "Recover stale run", ["start"], run_conditions[:3],
                             detached=True, summary="The last run died while holding the build. Starting "
                             "again marks its in-flight calls unknown and stops for you to reconcile."))
    else:
        result.append(_lever("start", "Start", ["start"], run_conditions, detached=True, spends=True,
                             summary="Run every incomplete phase in order. Runs detached; closing the "
                             "console does not stop it."))

    # Pause takes the state lock like every command, so it cannot interrupt a run in progress.
    result.append(_lever("pause", "Pause", ["pause"], [
        lock_condition, _condition("status", "Build is running or idle", can_pause(state), f"status is {state.status}")],
        summary="Stop starting new work. A call already in flight finishes."))
    result.append(_lever("resume", "Resume", ["resume"], [
        lock_condition, _condition("status", "Build is paused", can_resume(state), f"status is {state.status}")],
        summary="Allow new work again."))

    requirement = state.pending_approval
    if requirement:
        tree = facts.get("tree_hash")
        result.append(_lever("approve", "Approve checkpoint", ["approve", "--nonce", requirement.nonce], [
            lock_condition,
            _condition("pending", "A checkpoint is waiting", True, requirement.checkpoint),
            _condition("current", "Approval matches the current state generation", approval_is_current(state),
                       f"issued at generation {requirement.state_generation + 1}, now {state.generation}"),
            _condition("tree", "Repository tree is the one under review", approval_scope_holds(state, tree),
                       f"expected {requirement.tree_hash[:12]}, found {(tree or 'unknown')[:12]}"),
        ], confirm={"kind": "tree_hash", "value": requirement.tree_hash, "length": 7,
                    "prompt": "Type the first 7 characters of the tree hash you reviewed"},
            summary=f"Sign {requirement.checkpoint} for phase {requirement.phase}, attempt {requirement.phase_attempt}."))

    stopped = is_stopped(state) and not requirement
    if is_ceiling(state):
        grant = state.extra_attempt_grant
        result.append(_lever(
            "grant", "Authorize one extra attempt",
            ["retry", "--authorize-extra-attempt", "--nonce", grant.nonce if grant else ""], [
                lock_condition,
                _condition("stopped", "Build is stopped at a ceiling", stopped, state.escalation_code or ""),
                _condition("reconciled", "No operation awaits reconciliation", not unknown,
                           f"{len(unknown)} unknown" if unknown else ""),
                _condition("grant", "Grant names this phase and this ceiling", grant_matches(state),
                           f"phase {grant.phase}, {grant.ceiling_code}" if grant else "no grant issued"),
            ], confirm={"kind": "phase", "value": str(state.current_phase), "length": None,
                        "prompt": "Type the phase number to buy exactly one more pass"},
            spends=True, summary="Buy exactly one more pass past the ceiling. The grant is spent on use."))
    else:
        result.append(_lever("retry", "Retry", ["retry"], [
            lock_condition,
            _condition("stopped", "Build stopped on an ordinary failure", stopped,
                       "a checkpoint needs approval, not retry" if requirement else f"status is {state.status}"),
            _condition("reconciled", "No operation awaits reconciliation", not unknown,
                       f"{len(unknown)} unknown" if unknown else ""),
        ], summary="Start a fresh attempt of the stopped phase."))

    for operation_id in unknown:
        operation = state.operations[operation_id]
        output = facts.get("operation_outputs", {}).get(operation_id, False)
        base = [lock_condition, _condition("unknown", "Outcome is unknown", True, operation_id)]
        result.append(_lever("reconcile-accept", "Accept output", ["reconcile", "--operation", operation_id,
                                                                    "--action", "accept-output"],
                             base + [_condition("output", "The call left an output file", bool(output),
                                                "found" if output else "no output on disk")],
                             confirm={"kind": "operation", "value": operation.kind, "length": None,
                                      "prompt": f"Type {operation.kind} to accept this output as the call's result"},
                             summary=f"Treat {operation_id} as completed with the output it left."))
        result.append(_lever("reconcile-retry", "Authorize retry", ["reconcile", "--operation", operation_id,
                                                                     "--action", "authorize-retry"], base,
                             confirm={"kind": "operation", "value": operation.kind, "length": None,
                                      "prompt": f"Type {operation.kind} to discard it and pay for a new call"},
                             spends=True, summary=f"Mark {operation_id} failed so the next run calls the agent again."))

    head = facts.get("head_sha") or ""
    result.append(_lever("trust", "Trust this commit", ["trust", "init", "--to", head, "--confirm", head], [
        lock_condition,
        _condition("idle", "Build is not running", state.status != "running", f"status is {state.status}"),
        _condition("commit", "Repository has a commit to pin", bool(head), head[:12]),
    ], confirm={"kind": "sha", "value": head, "length": 12,
                "prompt": "Type the first 12 characters of the commit you reviewed"},
        summary="Pin the governed files at HEAD, this buildctl and both agent executables."))
    result.append(_lever("isolation", "Verify sandbox", ["isolation", "verify"], [trust_condition, lock_condition],
                         summary="Probe the sandbox on this machine and record the result. Spends nothing."))
    result.append(_lever("auth", "Check agent logins", ["auth", "check"], [],
                         summary="Confirm codex and claude are logged in. Spends nothing."))
    result.append(_lever("bootstrap", "Bootstrap", ["bootstrap"], [
        trust_condition, isolation_condition, lock_condition,
        _condition("absent", "No build state yet", not bootstrapped, "state exists" if bootstrapped else "")],
        summary="Create the build state."))
    return result
