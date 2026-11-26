"""A synthetic build to look at: a real repository and real kernel state, made-up history.

The repository is a copy of the Relay project in `tests/fixtures/target`, a fictional webhook
delivery service.
The state, audit chain, cost ledger and artifacts are written through the kernel's own stores,
so the chain verifies and `buildctl inspect` reads it like any other build. Agent output,
findings and costs are invented; the console labels a demo build as one.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from buildctl.supervisor import state as state_module
from buildctl.supervisor.evidence import AgentRecord
from buildctl.supervisor.isolation import IsolationError, conformance_record, production_backend
from buildctl.supervisor.levers import WAITING_CODES
from buildctl.supervisor.state import (ApprovalRequirement, BuildState, DebateDecision, ExtraAttemptGrant, Operation,
                                       StateStore)
from buildctl.supervisor.trust import TrustError, TrustStore

SCENARIOS = ("blocked", "checkpoint", "ceiling", "unknown", "running", "debate", "docs", "drafts", "fresh")
FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "target"
COST = {"implement": 2.0, "remediate": 2.0, "review": 2.0, "debate": 5.0}

FINDINGS = [
    {"id": "f1", "severity": "HIGH", "kind": "correctness", "path": "src/relay/replay.py", "line": 142,
     "claim_key": "replay-ignores-tenant-scope",
     "claim": "Replay selects events by time range across every tenant, not only the endpoint's owner.",
     "rationale": "select_events() filters on received_at but never on tenant_id, so a replay can send another "
                  "tenant's events to this endpoint.",
     "required_change": "Scope the replay query by the endpoint's tenant and add a cross-tenant test.",
     "violated_invariant": "tenant_isolation", "treatment": "fix", "status": "still_open", "occurrences": 2},
    {"id": "f2", "severity": "HIGH", "kind": "architecture", "path": "src/relay/replay_sender.py", "line": 58,
     "claim_key": "replay-bypasses-delivery-path",
     "claim": "Replays send directly instead of creating deliveries on the normal delivery path.",
     "rationale": "Replayed sends get no retries, no attempt records and no dead-letter handling.",
     "required_change": "Create deliveries for replayed events and let the worker send them, or record an ADR for the exception.",
     "violated_invariant": "recorded_attempts", "treatment": "debate", "status": "open", "occurrences": 1},
    {"id": "f3", "severity": "MEDIUM", "kind": "testing", "path": "tests/product/test_replay.py", "line": 12,
     "claim_key": "replay-tests-share-store",
     "claim": "Tests share one module-level event store, so their order changes the result.",
     "rationale": "Two tests pass alone and fail together.",
     "required_change": "Build a fresh store per test.", "violated_invariant": "deterministic_tests",
     "treatment": "fix", "status": "open", "occurrences": 1},
    {"id": "f4", "severity": "LOW", "kind": "documentation", "path": "docs/HANDOFF_PHASE_12.md", "line": 1,
     "claim_key": "handoff-omits-open-question",
     "claim": "The handoff does not mention the unresolved replay send path.",
     "rationale": "The next phase inherits the question silently.", "required_change": "Name it under Open questions.",
     "violated_invariant": "handoff_completeness", "treatment": "fix", "status": "open", "occurrences": 1},
]


class Clock:
    def __init__(self, start: datetime):
        self.value = start

    def advance(self, minutes: float) -> None:
        self.value += timedelta(minutes=minutes)

    def __call__(self) -> str:
        return self.value.isoformat()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


README = """# Relay

Relay delivers webhooks to customer endpoints with retries.

## Replay

Replay resends a time window of events to one endpoint. See [the API contract](docs/API_CONTRACT.md).
"""
DOCS_README = README.replace("Replay resends a time window of events to one endpoint.",
                             "Replay resends a time window of one tenant's events to one of its endpoints, through the "
                             "normal delivery path, so every resend gets retries and an attempt record.")
DOCS_SECTION = {"mode": "required", "paths": ["README.md", "CHANGELOG.md", "docs/**"], "changelog": "CHANGELOG.md",
                "forbidden_phrases": ["production-ready", "seamless", "blazing"]}


def _repository(root: Path, scenario: str = "blocked") -> Path:
    repo = root / "relay"
    shutil.copytree(FIXTURE, repo)
    if scenario == "docs":
        # The documentation role on: the same roadmap, with docs to keep true and phase 10 opted out.
        config = json.loads((repo / "build.yaml").read_text())
        config["documentation"] = DOCS_SECTION
        config["operation_cost_usd"].update({"documentation": 2.0, "documentation_remediation": 2.0})
        for phase in config["phases"]:
            if phase["number"] == 10:
                phase["documentation"] = False
        (repo / "build.yaml").write_text(json.dumps(config, indent=2) + "\n")
        (repo / "README.md").write_text(README)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "add", "-A")
    _git(repo, "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "commit", "-q", "-m", "Roadmap")
    return repo


def _operations(number: int, attempt: int, laps: int) -> Iterator[tuple[str, str]]:
    yield "implement", "implementation"
    for lap in range(1, laps + 1):
        yield f"gate-{lap}", "gate"
        yield f"review-{lap}", "review"
        if lap < laps:
            yield f"remediate-{lap}", "remediation"


def build(root: Path, scenario: str = "blocked") -> tuple[Path, Path]:
    """Create the demo repository and its control root under `root`. Returns (repo, control_root)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario}; choose from {', '.join(SCENARIOS)}")
    repo = _repository(root, scenario)
    control = root / "state"
    control.mkdir(mode=0o700)
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    config = json.loads((repo / "build.yaml").read_text())

    try:
        anchor = TrustStore(repo, control).initialize(head, head)
        backend = production_backend()
        record = conformance_record(control, backend, anchor["snapshot_digest"])
        record["verified_at"] = (datetime.now(timezone.utc) - timedelta(hours=3, minutes=12)).isoformat()
        (control / "isolation-conformance.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    except (TrustError, IsolationError):
        anchor = None  # A machine without both agent CLIs shows the setup path instead.
    if scenario == "fresh":
        return repo, control

    now = datetime.now(timezone.utc)
    clock = Clock(now - timedelta(days=2, hours=5))
    original = state_module.now
    state_module.now = clock
    costs: list[dict] = []
    store = StateStore(control)
    try:
        state = BuildState(governance_sha=head, snapshot_digest=anchor["snapshot_digest"] if anchor else None,
                           isolation_backend="macos-sandbox-exec", status="running")
        store.save(state)
        state.started_at = clock()
        stop_at = 15 if scenario in ("checkpoint", "drafts") else 12
        laps_for = {3: 2, 7: 3, 9: 2, 11: 2}
        for phase in config["phases"]:
            number = phase["number"]
            if number >= stop_at:
                break
            if number == 7:
                clock.value = now - timedelta(hours=26)
            if number == 10:
                clock.value = now - timedelta(hours=6, minutes=40)
            _phase(store, state, clock, costs, number, laps_for.get(number, 1), complete=True)

        if scenario == "drafts":
            state.status, state.current_phase = "paused", stop_at
            store.save(state)
            _drafts(control, head, now)
        elif scenario == "checkpoint":
            clock.value = now - timedelta(minutes=9)
            state.current_phase, state.phase_step = stop_at, "pending"
            state.phase_attempts[str(stop_at)] = 1
            store.event(state, "phase_started", phase=stop_at, attempt=1)
            state.pending_approval = ApprovalRequirement(f"phase-{stop_at}", stop_at, 1, tree, head,
                                                         state.generation, "9f2c4e81a07b3d65c1e8f04b2a9d7c33")
            _escalate(store, state, "CHECKPOINT_REQUIRED", f"phase-{stop_at}")
        else:
            clock.value = now - timedelta(hours=1, minutes=48)
            laps = 4 if scenario == "ceiling" else 1 if scenario == "docs" else 3
            _phase(store, state, clock, costs, 12, laps, complete=False)
            _review_artifacts(control, 12, laps)
            for finding in FINDINGS:
                state.findings[f"demo:{finding['claim_key']}"] = dict(finding)
            if scenario == "blocked":
                _escalate(store, state, "REVIEW_BLOCKED",
                          "R5_STRATEGIC_UNRESOLVED: HIGH architecture finding replay-bypasses-delivery-path "
                          "is open after 2 remediation laps and debate is disabled for this phase")
            elif scenario == "ceiling":
                state.extra_attempt_grant = ExtraAttemptGrant(12, "REMEDIATION_ATTEMPTS_EXHAUSTED", state.generation,
                                                              "4b7e19c2d85a6f03e2c1b9a874d0f516")
                _escalate(store, state, "REMEDIATION_ATTEMPTS_EXHAUSTED",
                          "replay-ignores-tenant-scope recurred after 3 remediation laps")
            elif scenario == "unknown":
                op_id = "phase-12:remediate-3:1"
                state.operations[op_id] = Operation(op_id, "remediate-3", 12, status="unknown",
                                                    output_path=str(control / "artifacts/phase-12/PHASE_RESULT-remediation-3.json"),
                                                    started_at=clock())
                costs.append(_cost(clock, "remediation", 12, op_id))
                clock.advance(181)
                _escalate(store, state, "OPERATION_TIMEOUT_UNKNOWN", op_id)
            elif scenario == "debate":
                _debate(store, state, clock, costs, control)
            elif scenario == "docs":
                _documentation(store, state, clock, costs, control, root, repo)
            elif scenario == "running":
                op_id = "phase-12:remediate-3:1"
                clock.value = now - timedelta(minutes=14)
                state.operations[op_id] = Operation(op_id, "remediate-3", 12, status="started", started_at=clock())
                _running_record(control, op_id)
                costs.append(_cost(clock, "remediation", 12, op_id))
                store.event(state, "operation_started", operation=op_id, paid_operation="remediation",
                            role="implementer", network_allowed=True)
                # Stand-in for the run process: alive, holding the lock, killed by the OS after an hour.
                runner = subprocess.Popen(["sleep", "3600"], start_new_session=True)
                (control / "supervisor.lock").write_text(json.dumps(
                    {"pid": runner.pid, "host": "demo", "at": clock()}))
    finally:
        state_module.now = original

    (control / "costs.jsonl").write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in costs))
    return repo, control


def _running_record(control: Path, operation: str) -> None:
    """An invented agent call still in flight, so the live view has something to show."""
    record = AgentRecord(control / "records" / "phase-12" / f"{operation.replace(':', '_')}.1.jsonl",
                         {"operation": operation, "phase": 12, "attempt": 1, "role": "implementer", "cli": "codex",
                          "binary_sha256": "0" * 64, "prompt_sha256": "0" * 64, "command": ["codex", "exec", "--json"],
                          "workspace": "/workspace", "started_at": datetime.now(timezone.utc).isoformat()})
    items = [{"id": "1", "type": "agent_message", "text": "Reading the failing delivery test first."},
             {"id": "2", "type": "command_execution", "command": "pytest tests/test_delivery.py", "exit_code": 1,
              "aggregated_output": "FAILED tests/test_delivery.py::test_retry_backoff"},
             {"id": "3", "type": "file_change", "status": "completed",
              "changes": [{"path": "/workspace/src/relay/delivery.py", "kind": "update"}]},
             {"id": "4", "type": "command_execution", "command": "pytest tests/test_delivery.py", "exit_code": 0,
              "aggregated_output": "4 passed"}]
    for item in items:
        record.append("stdout", json.dumps({"type": "item.completed", "item": item}))
    record.abandon()


def _cost(clock: Clock, provider: str, phase: int, operation_id: str) -> dict:
    return {"at": clock(), "provider": provider, "phase": phase, "operation_id": operation_id,
            "cost_usd": COST.get(provider.replace("implementation", "implement").replace("remediation", "remediate"), 2.0)}


def _phase(store: StateStore, state: BuildState, clock: Clock, costs: list, number: int, laps: int,
           complete: bool) -> None:
    key = str(number)
    state.current_phase, state.phase_step = number, "pending"
    state.phase_attempts[key] = 1
    state.deadline_at = (clock.value + timedelta(minutes=180)).isoformat()
    store.event(state, "phase_started", phase=number, attempt=1)
    clock.advance(1)
    state.phase_step = "clone_completed"
    store.event(state, "clone_completed", workspace=f"/private/var/folders/demo/buildctl-phase-{number}/workspace")
    for kind, paid in _operations(number, 1, laps):
        op_id = f"phase-{number}:{kind}:1"
        state.operations[op_id] = Operation(op_id, kind, number, status="started", started_at=clock())
        if paid != "gate":
            costs.append(_cost(clock, paid, number, op_id))
        store.event(state, "operation_started", operation=op_id)
        clock.advance({"implementation": 34, "remediation": 19, "review": 11, "gate": 3}[paid])
        operation = state.operations[op_id]
        operation.status, operation.completed_at = "completed", clock()
        operation.output_digest = hashlib.sha256(op_id.encode()).hexdigest()
        store.event(state, "operation_completed", operation=op_id)
        if paid == "review":
            last = kind == f"review-{laps}"
            if paid == "review" and kind.startswith("review"):
                state.semantic_review_attempts[key] = int(kind.split("-")[1])
            if not last:
                state.remediation_attempts[key] = int(kind.split("-")[1])
            if complete or not last:
                store.event(state, "review_reduced", rule="R1_ADVANCE" if last else "R3_REMEDIATE",
                            disposition="ADVANCE" if last else "REMEDIATE")
    if not complete:
        return
    state.phase_step = "committed"
    state.active_branch = f"build/phase-{number}"
    store.event(state, "phase_committed", phase=number, branch=state.active_branch)
    clock.advance(1)
    state.completed_phases.append(number)
    state.current_phase, state.active_branch, state.phase_step = None, None, "pending"
    store.event(state, "phase_completed", phase=number, branch=f"build/phase-{number}",
                publication={"integration_branch": "main", "fast_forwarded": True})
    clock.advance(2)


DEBATE_TURNS = [
    "Replays are operator-initiated and bounded to one endpoint. Routing them through the worker queues them "
    "behind live traffic, which defeats the point of a manual replay. The direct send is deliberate.",
    "The delivery path is where retries, attempt records and dead-lettering live. A replay that fails today "
    "leaves no record at all, so the operator cannot tell whether it reached the endpoint.",
    "Then record attempts from the replay sender too. That keeps replays immediate and still auditable, without "
    "pushing them through the queue.",
    "A second place that writes attempt records is a second place to keep correct. The invariant is one "
    "delivery path. Queue priority solves the latency concern without a bypass.",
]


def _debate(store: StateStore, state: BuildState, clock: Clock, costs: list, control: Path) -> None:
    """Four turns about the architecture finding, no accepted proposal: the operator decides."""
    directory = control / "artifacts" / "phase-12"
    offer = {"status": "agreement", "decision": "Keep direct replay sends and record an attempt for each one.",
             "finding_dispositions": [{"finding_id": "f2", "disposition": "changed"}],
             "required_changes": ["Write a delivery attempt record from the replay sender."],
             "human_decisions": [], "revisit_when": ["Replays need retries"]}
    for turn, argument in enumerate(DEBATE_TURNS, 1):
        op_id = f"phase-12:debate-1-turn-{turn}:1"
        kind = f"debate-1-turn-{turn}"
        state.operations[op_id] = Operation(op_id, kind, 12, status="completed", started_at=clock())
        costs.append(dict(_cost(clock, "debate_turn", 12, op_id), cost_usd=2.0))
        clock.advance(3)
        (directory / f"DEBATE_TURN-1-{turn}.json").write_text(json.dumps({
            "argument": argument, "proposal": offer if turn == 3 else None, "accept": None}, indent=2) + "\n")
        store.event(state, "operation_completed", operation=op_id)
    state.debate_attempts["12"] = 1
    state.pending_debate = DebateDecision(12, 1, 1, ["f2"], "demo-evidence", state.base_sha or "",
                                          state.governance_sha or "", state.generation,
                                          "c31d7a0e94b25f86d1e3a7c09b4f2e58")
    _escalate(store, state, "DEBATE_AWAITING_DECISION",
              "phase 12, debate 1: 4 turns without an accepted proposal")


def _documentation(store: StateStore, state: BuildState, clock: Clock, costs: list, control: Path, root: Path,
                   repo: Path) -> None:
    """Phase 12's documenter: one pass sent back by the links gate, then a second pass that edited code."""
    directory = control / "artifacts" / "phase-12"
    # The documenter runs before review, so this attempt never reached it.
    state.operations.pop("phase-12:review-1:1", None)
    for name in ("REVIEW_RESULT-1.json",):
        (directory / name).unlink(missing_ok=True)
    state.findings.clear()
    workspace = root / "workspace"
    shutil.copytree(repo, workspace, ignore=shutil.ignore_patterns(".git"))
    (workspace / "README.md").write_text(DOCS_README)
    (workspace / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n- Replay is scoped to the endpoint's "
                                            "tenant and goes through the normal delivery path.\n")
    (workspace / "src" / "relay").mkdir(parents=True)
    (workspace / "src" / "relay" / "replay.py").write_text(
        '"""Replay a window of events to one endpoint."""\n\nREPLAY_DOC = "see README.md#replay"\n')
    state.workspace, state.base_sha = str(workspace), _git(repo, "rev-parse", "HEAD")
    first = {"status": "updated", "summary": "Documented that replay is tenant-scoped and uses the delivery path.",
             "user_visible": True, "reason": "", "changes": [
                 {"path": "README.md", "reflects": "src/relay/replay.py:142 scopes by tenant", "kind": "fixed_stale"}]}
    second = dict(first, summary="Documented tenant-scoped replay, added the changelog entry, and fixed the link "
                                 "the gate reported.", changes=[
        {"path": "README.md", "reflects": "src/relay/replay.py:142 scopes by tenant", "kind": "fixed_stale"},
        {"path": "CHANGELOG.md", "reflects": "this phase's delta", "kind": "added"}])
    for label, result, seconds in (("1-0", first, 41), ("1-1", second, 37)):
        op_id = f"phase-12:document-{label}:1"
        state.operations[op_id] = Operation(op_id, f"document-{label}", 12, status="started", started_at=clock(),
                                            output_path=str(directory / f"DOC_RESULT-{label}.json"))
        costs.append(dict(_cost(clock, "documentation" if label == "1-0" else "documentation_remediation", 12, op_id),
                          cost_usd=2.0))
        clock.advance(seconds / 60)
        state.operations[op_id].status, state.operations[op_id].completed_at = "completed", clock()
        (directory / f"DOC_RESULT-{label}.json").write_text(json.dumps(result, indent=2) + "\n")
        store.event(state, "operation_completed", operation=op_id)
        if label == "1-0":
            gate_id = "phase-12:doc-gate-1-0:1"
            state.operations[gate_id] = Operation(gate_id, "doc-gate", 12, status="completed", started_at=clock(),
                                                  completed_at=clock(),
                                                  output_path=str(directory / "DOC_GATE_RESULT-1-0.json"))
            (directory / "DOC_GATE_RESULT-1-0.json").write_text(json.dumps({
                "passed": False, "acceptance": {"passed": True, "details": []},
                "documentation": {"passed": False, "failures": [
                    {"gate": "links", "problems": ["README.md:7 docs/API.md (missing file)"]},
                    {"gate": "changelog", "problems": ['a user-visible change needs a new entry under "Unreleased" in CHANGELOG.md']}]}},
                indent=2) + "\n")
            state.documentation_attempts["12"] = 1
            store.event(state, "documentation_remediation", phase=12, source="gates", attempt=1)
    store.event(state, "documentation_scope_violation", phase=12, paths=["src/relay/replay.py"])
    _escalate(store, state, "DOC_SCOPE_VIOLATION", "src/relay/replay.py")


def _claim(text: str, kind: str, ref: str, quote: str | None = None, line: int | None = None) -> dict:
    return {"text": text, "source": {"kind": kind, "ref": ref, "line": line, "quote": quote}}


THESIS = "docs/PRODUCT_THESIS.md"
ROADMAP = "docs/ROADMAP.md"
DEMO_DRAFTS = {
    "release-notes": {
        "title": "Relay MVP release notes",
        "body": "## Delivery you can trust\n\nRelay accepts an event once and delivers it to every subscriber. "
                "Accepted events survive a restart. Each payload is signed, so a receiver can verify it came "
                "from Relay.\n\n## When a receiver fails\n\nFailed deliveries retry with backoff. Events that "
                "never deliver land in a dead-letter queue you can inspect.\n\n## Operating it\n\nOne noisy "
                "endpoint can no longer slow everyone else down. Each tenant sees only its own endpoints and "
                "events. Delivery logs show what was delivered, when, and how long it took.",
        "claims": [
            _claim("Relay accepts an event once and delivers it to every subscriber.", "file", THESIS,
                   "a team sends an event once and gets reliable, inspectable delivery to every subscriber", 3),
            _claim("Accepted events survive a restart.", "file", ROADMAP, "Accepted events survive a restart and are never lost.", 12),
            _claim("Receivers can verify each payload came from Relay.", "file", ROADMAP,
                   "Receivers can verify that each payload really came from Relay.", 14),
            _claim("Failed deliveries retry with backoff.", "file", ROADMAP,
                   "Failed deliveries retry with backoff instead of hammering a receiver.", 18),
            _claim("Undeliverable events land in an inspectable dead-letter queue.", "file", ROADMAP,
                   "Events that never deliver land in a dead-letter queue you can inspect.", 20),
            _claim("One noisy endpoint no longer slows everyone else.", "file", ROADMAP,
                   "One noisy endpoint can no longer slow down everyone else.", 22),
            _claim("Each tenant sees only its own endpoints and events.", "file", ROADMAP,
                   "Each tenant sees and touches only its own endpoints and events.", 24),
            _claim("Delivery logs show what was delivered, when and how long it took.", "file", ROADMAP,
                   "You can see what was delivered, when, and how long it took.", 26),
        ],
        "known_limits": ["Replaying past events is not in this release."],
    },
    "positioning": {
        "title": "Relay positioning",
        "body": "Relay is a self-hosted webhook delivery service. Teams that send webhooks rebuild the same "
                "machinery: retries, signing, dead letters, replay and logs. Relay is that machinery as one "
                "service, so a team sends an event once and can see whether each subscriber got it.",
        "claims": [
            _claim("Relay is one self-hosted service for webhook delivery.", "file", THESIS,
                   "Relay is that machinery as one self-hosted service", 3),
            _claim("Teams that send webhooks rebuild retries, signing, dead letters, replay and logs.", "file", THESIS,
                   "Teams that send webhooks rebuild the same delivery machinery: retries, signing, dead letters, replay and logs.", 3),
        ],
        "known_limits": [],
        "fields": {"description": "Self-hosted webhook delivery: retries, signing, dead letters and delivery logs in one service.",
                   "topics": ["webhooks", "webhook-delivery", "self-hosted", "retries", "dead-letter-queue"]},
    },
    "launch-post": {
        "title": "Relay: send a webhook once, know it arrived",
        "body": "Did this subscriber get this event, and if not, why? Most teams cannot answer that in under a "
                "minute.\n\nRelay is a self-hosted service that handles webhook delivery end to end. It "
                "retries with backoff, signs every payload, and keeps a dead-letter queue you can inspect. "
                "Relay is already used in production by three teams.\n\n## What it does not do yet\n\n"
                "Replay is not in this release.",
        "claims": [
            _claim("Teams want to answer whether a subscriber got an event in under a minute.", "file", THESIS,
                   "did this subscriber get this event, and if not, why?", 5),
            _claim("Relay retries failed deliveries with backoff.", "file", ROADMAP,
                   "Failed deliveries retry with backoff instead of hammering a receiver.", 18),
            _claim("Relay signs every payload.", "file", ROADMAP, "Receivers can verify that each payload really came from Relay.", 14),
            _claim("Relay keeps a dead-letter queue you can inspect.", "file", ROADMAP,
                   "Events that never deliver land in a dead-letter queue you can inspect.", 20),
            _claim("Relay is used in production by three teams.", "file", "docs/CUSTOMERS.md", "three teams", None),
        ],
        "known_limits": ["Replaying past events is not in this release."],
    },
    "demo-script": {
        "title": "Relay demo script",
        "body": "About three minutes.\n\n1. **Register an endpoint.** On screen: the endpoint registry. Say: "
                "\"Tenants register, update and remove their webhook endpoints.\"\n2. **Break the receiver.** "
                "On screen: delivery attempts backing off. Say: \"Failed deliveries retry with backoff instead of "
                "hammering a receiver.\"\n3. **Open the dead-letter queue.** Say: \"Events that never deliver "
                "land here, where you can inspect them.\"",
        "claims": [
            _claim("Tenants can register, update and remove endpoints.", "file", ROADMAP,
                   "Tenants can register, update and remove webhook endpoints.", 10),
            _claim("Failed deliveries retry with backoff.", "file", ROADMAP,
                   "Failed deliveries retry with backoff instead of hammering a receiver.", 18),
            _claim("Undeliverable events land in an inspectable dead-letter queue.", "file", ROADMAP,
                   "Events that never deliver land in a dead-letter queue you can inspect.", 20),
        ],
        "known_limits": [],
    },
}


def _drafts(control: Path, head: str, now: datetime) -> None:
    """One buildctl-drafts run for the MVP milestone: drafts in every state the console shows."""
    stamp = now - timedelta(minutes=42)
    run = stamp.strftime("%Y%m%d-%H%M%S")
    directory = control / "drafts" / run
    directory.mkdir(parents=True, mode=0o700)
    costs, events = [], []
    for kind, draft in DEMO_DRAFTS.items():
        draft = {"fields": {}, **draft}
        unbacked, lint = [], []
        if kind == "launch-post":
            unbacked = [{"claim": 5, "text": draft["claims"][4]["text"], "why": "docs/CUSTOMERS.md does not exist at the milestone's commit"}]
        passed = not unbacked and not lint
        record = {"kind": kind, "title": kind.replace("-", " ").capitalize(), "label": "mvp", "since": None,
                  "until": head, "commits": 15, "voice": "plain (no voice file)", "attempts": 2 if unbacked else 1,
                  "draft": draft, "checks": {"passed": passed, "contract": [], "unbacked": unbacked, "lint": lint,
                                             "claims": len(draft["claims"])}}
        (directory / f"{kind}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        (directory / f"{kind}.md").write_text(f"# {draft['title']}\n\n{draft['body']}\n")
        for attempt in range(record["attempts"]):
            costs.append({"at": (stamp + timedelta(seconds=40 * len(costs))).isoformat(), "operation": "release_draft",
                          "draft": f"{run}/{kind}", "cost_usd": 2.0})
    events.append({"at": (now - timedelta(minutes=20)).isoformat(), "event": "approved", "draft": f"{run}/positioning"})
    (control / "drafts" / "costs.jsonl").write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in costs))
    (control / "drafts" / "events.jsonl").write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in events))


def _escalate(store: StateStore, state: BuildState, code: str, detail: str) -> None:
    state.status, state.escalation_code, state.escalation_detail = "human_escalation", code, detail
    if code not in WAITING_CODES:
        state.consecutive_failures += 1
    store.event(state, "human_escalation", code=code)


def _review_artifacts(control: Path, number: int, laps: int) -> None:
    directory = control / "artifacts" / f"phase-{number}"
    directory.mkdir(parents=True, exist_ok=True)
    for lap in range(1, laps + 1):
        findings = FINDINGS if lap == laps else [dict(FINDINGS[0], status="open", occurrences=1), FINDINGS[2]]
        (directory / f"REVIEW_RESULT-{lap}.json").write_text(json.dumps({
            "verdict": "REMEDIATE", "attempt": lap, "findings": findings,
            "coverage": [{"path": item["path"], "change_type": "modified", "disposition": "finding_refs",
                          "finding_refs": [item["id"]]} for item in findings]
            + [{"path": "src/relay/replay_window.py", "change_type": "added", "disposition": "clear",
                "finding_refs": []}]}, indent=2) + "\n")
        (directory / f"GATE_RESULT-{lap}.json").write_text(json.dumps({
            "passed": True, "details": [
                {"command": ["{python}", "-m", "unittest", "discover", "-s", "tests"], "exit_code": 0, "seconds": 41.2},
                {"command": ["required_files"], "exit_code": 0, "seconds": 0.0},
                {"command": ["no_credentials_in_source"], "exit_code": 0, "seconds": 0.4}]}, indent=2) + "\n")
    (directory / "PHASE_RESULT.json").write_text(json.dumps({"status": "complete", "phase": number}) + "\n")


def build_temporary(scenario: str = "blocked") -> tuple[Path, Path]:
    return build(Path(tempfile.mkdtemp(prefix="buildctl-demo-")), scenario)
