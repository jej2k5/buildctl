"""The read model every front end renders: one JSON document, computed only from kernel state.

Nothing here mutates anything, takes the state lock or calls an agent. It degrades instead of
failing: an untrusted repository, a stale sandbox record or a corrupt audit chain is reported as
a fact the operator can act on, because those are exactly the moments a front end is needed.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import levers as lever_rules
from .audit import AuditChain
from .budget import BudgetLedger, LimitExceeded
from .isolation import IsolationError, production_backend, require_fresh_conformance
from .kernel import kernel_identity
from .phases import ConfigurationError, load_config, merged_metadata
from .review import NON_BLOCKING_FINDING_STATUSES
from .state import BuildState, StateStore
from .trust import TrustError, TrustStore

# What each stop means and what to do, in the README's words.
ESCALATIONS: dict[str, tuple[str, str]] = {
    "CHECKPOINT_REQUIRED": ("A checkpoint, a human-merge phase or the MVP milestone needs you.",
                            "Review the evidence, then approve."),
    "REVIEW_BLOCKED": ("The reviewer could not clear the phase.",
                       "Read the findings, adjust the phase prompt or decide, then retry."),
    "UNREVIEWABLE_DELTA": ("The change has binary or oversized files the reviewer cannot read.",
                           "Adjust the phase so its output is reviewable, then retry."),
    "DEBATE_UNRESOLVED": ("The debate ended without agreement.",
                          "Decide the question, adjust the phase prompt, then retry."),
    "UNKNOWN_EXTERNAL_OPERATION": ("An agent call may or may not have finished.",
                                   "Reconcile: accept its output, or authorize a retry."),
    "OPERATION_TIMEOUT_UNKNOWN": ("An agent call timed out and its outcome is unknown.",
                                  "Reconcile: accept its output, or authorize a retry."),
    "STALE_RUN_UNKNOWN_OPERATION": ("The last run died with agent calls in flight.",
                                    "Reconcile each unknown call, then retry."),
    "STALE_RUN": ("The last run died between agent calls.", "Retry."),
    "AUTHENTICATION_REQUIRED": ("An agent CLI is logged out.", "Log in, then retry."),
    "DAILY_BUDGET_UNAVAILABLE": ("The next agent call would exceed the daily cost limit.",
                                 "Wait for tomorrow (UTC) or raise the limit and re-trust, then retry."),
    "ADVANCE_PRECONDITIONS_UNMET": ("The phase passed review, but the budget ledger or a required "
                                    "approval no longer checks out.", "Fix what it names, then retry."),
    "AGENT_OPERATION_FAILED": ("An agent call failed.", "Fix the cause in the detail, then retry."),
    "PHASE_DEADLINE_EXCEEDED": ("The phase ran past its time limit.", "Retry."),
    "DELTA_CHANGED_AFTER_REVIEW": ("What would be committed differs from what was reviewed.",
                                   "Investigate before retrying. This should never happen."),
    "STALE_DEBATE_RESULT": ("The debate result was written for a different phase or base.",
                            "Re-run the debate for the current request, then retry."),
    "UNEXPECTED_FAILURE": ("The supervisor hit an error it does not classify.",
                           "Read the detail, fix the cause, then retry."),
}
CEILING = ("A retry, review or remediation limit was hit, or too many failures in a row.",
           "Authorize one extra attempt, or fix the cause first.")


def checkpoint_meaning(checkpoint: str) -> str:
    if checkpoint == "mvp_completion":
        return "The MVP milestone is complete and needs your sign-off before the build goes on."
    if checkpoint.startswith("merge-phase-"):
        return f"Phase {checkpoint.removeprefix('merge-phase-')} passed review and is committed. Its merge policy is human, so it waits for you."
    if checkpoint.startswith("phase-"):
        return f"Phase {checkpoint.removeprefix('phase-')} is behind a checkpoint. It needs your approval before it starts."
    return ESCALATIONS["CHECKPOINT_REQUIRED"][0]


def explain(code: str | None, checkpoint: str | None = None) -> dict[str, str] | None:
    if not code:
        return None
    if code == "CHECKPOINT_REQUIRED" and checkpoint:
        return {"code": code, "meaning": checkpoint_meaning(checkpoint), "remedy": ESCALATIONS[code][1]}
    meaning, remedy = CEILING if code.endswith("_EXHAUSTED") else ESCALATIONS.get(
        code, ("buildctl stopped.", "Read the detail."))
    return {"code": code, "meaning": meaning, "remedy": remedy}


def _git(repo: Path, *args: str, runner: Callable[..., Any] = subprocess.run) -> str | None:
    result = runner(["git", "-C", str(repo), *args], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return None


def _lock(control_root: Path, status: str) -> dict[str, Any]:
    """Who last took the lock, and whether that process is alive.

    Probing with flock would briefly take the lock and make a concurrent command fail, so the
    holder is inferred: the lock file names the last taker, and a live `running` process
    holds it for the length of the run.
    """
    path = control_root / "supervisor.lock"
    try:
        value = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        value = {}
    pid = value.get("pid")
    alive = False
    if isinstance(pid, int) and pid != os.getpid():
        try:
            os.kill(pid, 0)
            alive = True
        except PermissionError:
            alive = True
        except OSError:
            alive = False
    return {"pid": pid, "host": value.get("host"), "at": value.get("at"), "alive": alive,
            "held": alive and status == "running"}


def audit_events(control_root: Path, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    path = control_root / "audit.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            break
        if entry.get("sequence", 0) > after:
            result.append(entry)
            if len(result) >= limit:
                break
    return result


def _artifacts(control_root: Path, number: int) -> list[dict[str, Any]]:
    directory = control_root / "artifacts" / f"phase-{number}"
    if not directory.is_dir():
        return []
    return [{"name": item.name, "path": str(item), "bytes": item.stat().st_size}
            for item in sorted(directory.iterdir()) if item.is_file()]


def _latest_review(control_root: Path, number: int) -> dict[str, Any] | None:
    directory = control_root / "artifacts" / f"phase-{number}"
    reviews = sorted(directory.glob("REVIEW_RESULT-*.json"),
                     key=lambda item: int(item.stem.rsplit("-", 1)[-1]) if item.stem.rsplit("-", 1)[-1].isdigit() else 0)
    for review in reversed(reviews):
        try:
            value = json.loads(review.read_text())
        except (OSError, ValueError):
            continue
        return {"file": review.name, "verdict": value.get("verdict"), "attempt": value.get("attempt"),
                "findings": value.get("findings", []), "coverage": value.get("coverage", [])}
    return None


def _gates(control_root: Path, number: int) -> list[dict[str, Any]]:
    directory = control_root / "artifacts" / f"phase-{number}"
    result = []
    for gate in sorted(directory.glob("GATE_RESULT-*.json")):
        try:
            value = json.loads(gate.read_text())
        except (OSError, ValueError):
            continue
        result.append({"file": gate.name, "passed": value.get("passed"), "details": value.get("details")})
    return result


def inspect(repo: Path, control_root: Path, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    repo, control_root = repo.resolve(), control_root.resolve()
    store = StateStore(control_root)
    trust_store = TrustStore(repo, control_root, runner)

    trust: dict[str, Any] = {"initialized": trust_store.anchor_path.is_file(), "ok": False, "error": None}
    config: dict[str, Any] | None = None
    config_source = None
    snapshot_digest = None
    try:
        anchor = trust_store.load()
        trust.update(ok=True, governance_sha=anchor["governance_sha"], snapshot_digest=anchor["snapshot_digest"],
                     governed_files=sorted(anchor["manifest"]), executables=anchor.get("executables", {}),
                     kernel=anchor.get("kernel"))
        snapshot_digest = anchor["snapshot_digest"]
        config, config_source = trust_store.config(), "trusted snapshot"
    except (TrustError, ConfigurationError, KeyError, ValueError) as exc:
        trust["error"] = str(exc)
    trust["running_kernel"] = kernel_identity()
    config_error = None
    if config is None:
        try:
            config, config_source = load_config(repo / "build.yaml"), "working tree (untrusted)"
        except ConfigurationError as exc:
            config_error = str(exc)

    isolation: dict[str, Any] = {"fresh": False, "verified_at": None, "age_hours": None, "error": None}
    record_path = control_root / "isolation-conformance.json"
    try:
        isolation["verified_at"] = json.loads(record_path.read_text()).get("verified_at")
        isolation["age_hours"] = _age_hours(isolation["verified_at"])
    except (OSError, ValueError):
        pass
    try:
        if snapshot_digest is None:
            raise IsolationError("trust is not initialized, so the sandbox record cannot be checked")
        require_fresh_conformance(control_root, production_backend(), snapshot_digest)
        isolation["fresh"] = True
    except IsolationError as exc:
        isolation["error"] = str(exc)

    audit = {"ok": True, "sequence": 0, "head": "0" * 64, "error": None}
    try:
        audit["sequence"], audit["head"] = AuditChain(control_root).verify()
    except RuntimeError as exc:
        audit.update(ok=False, error=str(exc))

    state_error = None
    bootstrapped = store.path.exists()
    try:
        state = store.load()
    except RuntimeError as exc:
        state, state_error = BuildState(), str(exc)

    limit = float(config["limits"]["max_daily_cost_usd"]) if config else 0.0
    ledger = BudgetLedger(control_root, limit)
    budget: dict[str, Any] = {"limit": limit, "spent_today": 0.0, "ok": True, "error": None, "by_phase": {}}
    try:
        entries = ledger._entries()
        budget["spent_today"] = ledger.spent_today()
        for item in entries:
            key = str(item["phase"])
            budget["by_phase"][key] = round(budget["by_phase"].get(key, 0.0) + float(item["cost_usd"]), 2)
        budget["spent_total"] = round(sum(float(item["cost_usd"]) for item in entries), 2)
    except LimitExceeded as exc:
        budget.update(ok=False, error=str(exc))
    cheapest = min((float(v) for v in (config or {}).get("operation_cost_usd", {}).values()), default=0.0)
    budget_ok = budget["ok"] and budget["spent_today"] + cheapest <= limit

    lock = _lock(control_root, state.status)
    head_sha = _git(repo, "rev-parse", "HEAD", runner=runner)
    tree_hash = _git(repo, "rev-parse", "HEAD^{tree}", runner=runner)

    phases: list[dict[str, Any]] = []
    if config:
        before = set(config["checkpoints"].get("before_phases", []))
        stations = {}
        for name, number in config.get("milestones", {}).items():
            stations.setdefault(int(number), []).append(name)
        for item in config["phases"]:
            number = item["number"]
            key = str(number)
            meta = merged_metadata(config["phase_defaults"], item)
            if number in state.completed_phases:
                aspect = "clear"
            elif number == state.current_phase:
                aspect = ("danger" if state.status in lever_rules.STOPPED_STATUSES and not state.pending_approval
                          else "caution" if state.pending_approval else "occupied")
            else:
                aspect = "pending"
            operations = [asdict(op) for op in state.operations.values() if op.phase == number]
            if any(op["status"] == "unknown" for op in operations):
                aspect = "unknown"
            phases.append({
                "number": number, "name": item["name"], "aspect": aspect,
                "merge_policy": meta["merge_policy"], "checkpoint": number in before,
                "milestones": stations.get(number, []),
                "attempts": state.phase_attempts.get(key, 0),
                "remediations": state.remediation_attempts.get(key, 0),
                "reviews": state.semantic_review_attempts.get(key, 0),
                "debates": state.debate_attempts.get(key, 0),
                "cost_usd": budget["by_phase"].get(key, 0.0),
                "operations": operations,
            })
    remaining = [p["number"] for p in phases if p["number"] not in state.completed_phases]

    current = None
    if state.current_phase is not None:
        number = state.current_phase
        debate_request = control_root / "artifacts" / f"phase-{number}" / "DEBATE_REQUEST.json"
        current = {"number": number, "step": state.phase_step, "deadline_at": state.deadline_at,
                   "branch": state.active_branch, "base_sha": state.base_sha,
                   "review": _latest_review(control_root, number), "gates": _gates(control_root, number),
                   "artifacts": _artifacts(control_root, number),
                   "debate_request": str(debate_request) if debate_request.is_file() else None}

    outputs = {}
    for key, op in state.operations.items():
        if op.status == "unknown":
            outputs[key] = any(path and Path(path).is_file() for path in (op.output_path, op.untrusted_output_path))

    findings = [dict(value, key=key) for key, value in state.findings.items()]
    open_findings = [item for item in findings if item.get("status", "open") not in NON_BLOCKING_FINDING_STATUSES]

    facts = {"trust_ok": trust["ok"], "trust_detail": trust["error"] or "",
             "isolation_fresh": isolation["fresh"], "isolation_detail": isolation["error"] or "",
             "bootstrapped": bootstrapped, "lock_held": lock["held"],
             "lock_detail": f"pid {lock['pid']} is running" if lock["held"] else "",
             "budget_ok": budget_ok, "budget_detail": f"${budget['spent_today']:.2f} of ${limit:.2f} spent today",
             "remaining_phases": remaining, "tree_hash": tree_hash, "head_sha": head_sha,
             "operation_outputs": outputs}

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": {"path": str(repo), "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD", runner=runner),
                 "head_sha": head_sha, "tree_hash": tree_hash,
                 "integration_branch": (config or {}).get("git", {}).get("integration_branch", "main")},
        "control_root": str(control_root),
        "config": {"source": config_source, "error": config_error,
                   "limits": (config or {}).get("limits"), "milestones": (config or {}).get("milestones"),
                   "checkpoints": (config or {}).get("checkpoints")},
        "trust": trust, "isolation": isolation, "audit": audit, "budget": budget, "lock": lock,
        "bootstrapped": bootstrapped, "state_error": state_error,
        "state": {"status": state.status, "generation": state.generation, "current_phase": state.current_phase,
                  "phase_step": state.phase_step, "completed_phases": state.completed_phases,
                  "consecutive_failures": state.consecutive_failures, "started_at": state.started_at,
                  "updated_at": state.updated_at,
                  "pending_approval": asdict(state.pending_approval) if state.pending_approval else None,
                  "extra_attempt_grant": asdict(state.extra_attempt_grant) if state.extra_attempt_grant else None},
        "escalation": dict(explain(state.escalation_code,
                                   state.pending_approval.checkpoint if state.pending_approval else None),
                           detail=state.escalation_detail)
        if state.escalation_code else None,
        "current": current,
        "phases": phases,
        "findings": {"open": open_findings, "total": len(findings)},
        "levers": lever_rules.levers(state, facts) if not state_error and audit["ok"] else [],
    }
