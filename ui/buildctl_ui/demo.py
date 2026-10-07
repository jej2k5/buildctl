"""A synthetic build to look at: a real repository and real kernel state, made-up history.

The repository is a copy of the the original project governed files in `tests/fixtures/target`.
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
from buildctl.supervisor.isolation import IsolationError, conformance_record, production_backend
from buildctl.supervisor.state import ApprovalRequirement, BuildState, ExtraAttemptGrant, Operation, StateStore
from buildctl.supervisor.trust import TrustError, TrustStore

SCENARIOS = ("blocked", "checkpoint", "ceiling", "unknown", "running", "fresh")
FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "target"
COST = {"implement": 2.0, "remediate": 2.0, "review": 2.0, "debate": 5.0}

FINDINGS = [
    {"id": "f1", "severity": "HIGH", "kind": "correctness", "path": "src/relay/capabilities/discovery.py", "line": 142,
     "claim_key": "discovery-ignores-tenant-scope",
     "claim": "Capability discovery returns capabilities registered by every tenant, not only the caller's.",
     "rationale": "resolve_capabilities() filters on agent_id but never on tenant_id, so a pilot tenant can "
                  "enumerate another tenant's tools.",
     "required_change": "Scope the registry query by the caller's tenant and add a cross-tenant test.",
     "violated_invariant": "tenant_isolation", "treatment": "fix", "status": "still_open", "occurrences": 2},
    {"id": "f2", "severity": "HIGH", "kind": "architecture", "path": "src/relay/capabilities/registry.py", "line": 58,
     "claim_key": "registry-bypasses-semantic-contract",
     "claim": "The registry writes capability records without going through the versioned semantic contract.",
     "rationale": "Phase 2 made the contract the only write path. This adds a second one.",
     "required_change": "Route registration through contract.write() or record an ADR for the exception.",
     "violated_invariant": "single_write_path", "treatment": "debate", "status": "open", "occurrences": 1},
    {"id": "f3", "severity": "MEDIUM", "kind": "testing", "path": "tests/test_capability_discovery.py", "line": 12,
     "claim_key": "discovery-tests-share-fixture-state",
     "claim": "Tests mutate a module-level registry fixture, so their order changes the result.",
     "rationale": "Two tests pass alone and fail together.",
     "required_change": "Build a fresh registry per test.", "violated_invariant": "deterministic_tests",
     "treatment": "fix", "status": "open", "occurrences": 1},
    {"id": "f4", "severity": "LOW", "kind": "documentation", "path": "docs/HANDOFF_PHASE_12.md", "line": 1,
     "claim_key": "handoff-omits-open-question",
     "claim": "The handoff does not mention the unresolved registry write path.",
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


def _repository(root: Path) -> Path:
    repo = root / "the original project"
    shutil.copytree(FIXTURE, repo)
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
    repo = _repository(root)
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
        stop_at = 12 if scenario != "checkpoint" else 15
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

        if scenario == "checkpoint":
            clock.value = now - timedelta(minutes=9)
            state.current_phase, state.phase_step = stop_at, "pending"
            state.phase_attempts[str(stop_at)] = 1
            store.event(state, "phase_started", phase=stop_at, attempt=1)
            state.pending_approval = ApprovalRequirement(f"phase-{stop_at}", stop_at, 1, tree, head,
                                                         state.generation, "9f2c4e81a07b3d65c1e8f04b2a9d7c33")
            _escalate(store, state, "CHECKPOINT_REQUIRED", f"phase-{stop_at}")
        else:
            clock.value = now - timedelta(hours=1, minutes=48)
            laps = 4 if scenario == "ceiling" else 3
            _phase(store, state, clock, costs, 12, laps, complete=False)
            _review_artifacts(control, 12, laps)
            for finding in FINDINGS:
                state.findings[f"demo:{finding['claim_key']}"] = dict(finding)
            if scenario == "blocked":
                _escalate(store, state, "REVIEW_BLOCKED",
                          "R5_STRATEGIC_UNRESOLVED: HIGH architecture finding registry-bypasses-semantic-contract "
                          "is open after 2 remediation laps and debate is disabled for this phase")
            elif scenario == "ceiling":
                state.extra_attempt_grant = ExtraAttemptGrant(12, "REMEDIATION_ATTEMPTS_EXHAUSTED", state.generation,
                                                              "4b7e19c2d85a6f03e2c1b9a874d0f516")
                _escalate(store, state, "REMEDIATION_ATTEMPTS_EXHAUSTED",
                          "discovery-ignores-tenant-scope recurred after 3 remediation laps")
            elif scenario == "unknown":
                op_id = "phase-12:remediate-3:1"
                state.operations[op_id] = Operation(op_id, "remediate-3", 12, status="unknown",
                                                    output_path=str(control / "artifacts/phase-12/PHASE_RESULT-remediation-3.json"),
                                                    started_at=clock())
                costs.append(_cost(clock, "remediation", 12, op_id))
                clock.advance(181)
                _escalate(store, state, "OPERATION_TIMEOUT_UNKNOWN", op_id)
            elif scenario == "running":
                op_id = "phase-12:remediate-3:1"
                clock.value = now - timedelta(minutes=14)
                state.operations[op_id] = Operation(op_id, "remediate-3", 12, status="started", started_at=clock())
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


def _escalate(store: StateStore, state: BuildState, code: str, detail: str) -> None:
    state.status, state.escalation_code, state.escalation_detail = "human_escalation", code, detail
    if code != "CHECKPOINT_REQUIRED":
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
            + [{"path": "src/relay/capabilities/__init__.py", "change_type": "added", "disposition": "clear",
                "finding_refs": []}]}, indent=2) + "\n")
        (directory / f"GATE_RESULT-{lap}.json").write_text(json.dumps({
            "passed": True, "details": [
                {"command": ["{python}", "-m", "unittest", "discover", "-s", "tests"], "exit_code": 0, "seconds": 41.2},
                {"command": ["required_files"], "exit_code": 0, "seconds": 0.0},
                {"command": ["no_credentials_in_source"], "exit_code": 0, "seconds": 0.4}]}, indent=2) + "\n")
    (directory / "PHASE_RESULT.json").write_text(json.dumps({"status": "complete", "phase": number}) + "\n")


def build_temporary(scenario: str = "blocked") -> tuple[Path, Path]:
    return build(Path(tempfile.mkdtemp(prefix="buildctl-demo-")), scenario)
