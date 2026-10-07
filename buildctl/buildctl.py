from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .supervisor.agents import (AgentError, FORMAT_RETRYABLE_SUFFIXES, RoleRunner, load_structured,
                                operation_output_path)
from .supervisor.auth import AuthPreflight, AuthenticationRequired, HostAuthUnsupported, validate_codex_auth
from .supervisor.budget import BudgetLedger, LimitExceeded, configured_cost
from .supervisor.claude import ClaudeAdapter
from .supervisor.codex import CodexAdapter
from .supervisor.debate import DebateAdapter, validate_debate_result, write_adr
from .supervisor.gates import MechanicalGates, write_gate_result
from .supervisor.git import Delta, GitManager, GitSafetyError
from .supervisor.isolation import (FakeIsolationBackend, IsolationBackend, IsolationContext,
                                   IsolationError, conformance_record, production_backend,
                                   require_fresh_conformance, verify_backend)
from .supervisor.phases import ConfigurationError, Phase, load_config, phases_from, resolve_until
from .supervisor.review import (ReviewValidationError, accumulate_findings, open_finding_obligations,
                                realign_findings_to_delta, reduce_review, strategic_findings,
                                validate_review)
from .supervisor.operations import PaidOperation
from .supervisor.safety import digest_file, redact
from .supervisor.state import (ApprovalRequirement, BuildState, ExtraAttemptGrant, Operation, StateConflict,
                               StateStore, SupervisorLocked, now)
from .supervisor.trust import TrustError, TrustStore


# Bounded length for redacted provider diagnostics persisted to trusted state.
DIAGNOSTIC_LIMIT = 800


class HumanEscalation(RuntimeError):
    pass


def control_root_for(repo: Path) -> Path:
    override = os.environ.get("BUILDCTL_STATE_DIR")
    if override:
        return Path(override).resolve()
    if os.environ.get("LEGACY_FACTORY_STATE_DIR"):
        # The pre-extraction name. Ignoring it would silently put canonical state, the cost
        # ledger and the trust anchor somewhere other than where the operator pointed them.
        raise ValueError("LEGACY_FACTORY_STATE_DIR is no longer read; set BUILDCTL_STATE_DIR instead")
    xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    identity = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return xdg / "buildctl" / identity


class Supervisor:
    def __init__(self, root: Path, *, dry_run: bool = False, runner: Any = subprocess.run,
                 backend: IsolationBackend | None = None, control_root: Path | None = None,
                 trusted: bool = True):
        self.root = root.resolve()
        self.control_root = (control_root or control_root_for(root)).resolve()
        self.store = StateStore(self.control_root)
        self.trust = TrustStore(self.root, self.control_root, runner)
        self.dry_run = dry_run
        if dry_run or not trusted:
            self.config = load_config(self.root / "build.yaml")
            self.snapshot = self.root
            self.anchor = {"governance_sha": "DRY_RUN", "snapshot_digest": "DRY_RUN"}
            self.backend = backend or FakeIsolationBackend()
        else:
            self.anchor = self.trust.load()
            self.config = self.trust.config()
            self.config["_trusted_executables"] = self.anchor.get("executables", {})
            self.snapshot = self.trust.snapshot
            self.backend = backend or production_backend()
            if isinstance(self.backend, FakeIsolationBackend):
                raise IsolationError("test-only isolation backend is forbidden for autonomous execution")
        self.phases = phases_from(self.config, self.snapshot)
        self.git = GitManager(self.root, self.config, runner)
        self.budget = BudgetLedger(self.control_root, self.config["limits"]["max_daily_cost_usd"])
        self.runner = runner

    def bootstrap(self) -> None:
        if self.dry_run:
            return
        if (self.root / ".build/state.json").exists():
            raise TrustError("legacy in-repository state detected; explicit migration/discard is required")
        require_fresh_conformance(self.control_root, self.backend, self.anchor["snapshot_digest"])
        with self.store.lock():
            if not self.store.path.exists():
                state = BuildState(governance_sha=self.anchor["governance_sha"],
                                   snapshot_digest=self.anchor["snapshot_digest"],
                                   isolation_backend=self.backend.name)
                self.store.save(state)

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        return {"status": state.status, "generation": state.generation,
                "current_phase": state.current_phase, "phase_step": state.phase_step,
                "completed_phases": state.completed_phases, "escalation_code": state.escalation_code,
                "pending_approval": vars(state.pending_approval) if state.pending_approval else None,
                "extra_attempt_grant": vars(state.extra_attempt_grant) if state.extra_attempt_grant else None,
                "unknown_operations": [key for key, value in state.operations.items() if value.status == "unknown"],
                "daily_cost_usd": self.budget.spent_today()}

    def pause(self) -> None:
        with self.store.lock():
            state = self.store.load()
            if state.status not in {"running", "idle"}:
                raise RuntimeError(f"cannot pause from {state.status}")
            state.status = "paused"
            self.store.event(state, "paused")

    def resume(self) -> None:
        with self.store.lock():
            state = self.store.load()
            if state.status != "paused":
                raise RuntimeError(f"cannot resume from {state.status}")
            state.status = "idle"
            self.store.event(state, "resumed")

    def retry(self, authorize_extra_attempt: bool = False, nonce: str | None = None) -> None:
        with self.store.lock():
            state = self.store.load()
            if state.status not in {"human_escalation", "failed"} or state.pending_approval:
                raise RuntimeError("retry requires a non-checkpoint escalation")
            if any(op.status == "unknown" for op in state.operations.values()):
                raise RuntimeError("unknown operation requires reconcile, not retry")
            if state.escalation_code and state.escalation_code.endswith("_EXHAUSTED"):
                if not authorize_extra_attempt:
                    raise RuntimeError("ceiling retry requires --authorize-extra-attempt")
                grant = state.extra_attempt_grant
                if (not grant or grant.phase != state.current_phase or grant.ceiling_code != state.escalation_code
                        or grant.attempt_generation != state.generation - 1 or not nonce
                        or not secrets.compare_digest(grant.nonce, nonce)):
                    raise RuntimeError("extra-attempt authorization scope or nonce is invalid")
                grant.authorized = True
                if state.escalation_code == "MAX_CONSECUTIVE_FAILURES_EXHAUSTED":
                    # Every other ceiling is a per-operation budget checked at a call site
                    # that can spend the grant. This one is a running total with no such
                    # site, so it is settled here: the human intervening has broken the
                    # streak, and the grant is spent rather than left dangling. Without
                    # this the next escalation of any kind is rewritten to this code again
                    # and the build can never leave the wall.
                    state.consecutive_failures = 0
                    grant.consumed = True
            state.status = "idle"
            state.escalation_code = state.escalation_detail = None
            # A retry has to run as a *new* attempt. `_run_phase` stamps the attempt number and
            # the wall-clock deadline only when it sees a phase it is not already running, and
            # operation ids embed that attempt number — so leaving the active phase identity in
            # place made the retry inherit the expired deadline and replay the previous
            # attempt's completed operations instead of re-running them. Releasing the identity
            # puts the next run back on the normal new-attempt path, which increments the
            # attempt exactly once and stamps a fresh deadline. The attempt is deliberately not
            # incremented here: it belongs to the run that actually starts.
            retried_phase = state.current_phase
            if state.phase_step == "committed":
                # The phase already produced a commit; it must resume at publication rather
                # than rebuild, so only the expired deadline is refreshed.
                state.deadline_at = self._phase_deadline()
            else:
                state.current_phase, state.phase_step, state.workspace = None, "pending", None
                state.deadline_at = None
                self._release_phase_baseline(retried_phase)
            self.store.event(state, "retry_authorized", extra_attempt=authorize_extra_attempt,
                             phase=retried_phase)

    def reconcile(self, operation_id: str, action: str) -> None:
        with self.store.lock():
            state = self.store.load()
            operation = state.operations.get(operation_id)
            if not operation or operation.status != "unknown":
                raise RuntimeError("operation is not awaiting reconciliation")
            if action == "accept-output":
                trusted = Path(operation.output_path) if operation.output_path else None
                untrusted = Path(operation.untrusted_output_path) if operation.untrusted_output_path else None
                if trusted and not trusted.is_file() and untrusted and untrusted.is_file():
                    value = load_structured(untrusted, operation.kind.upper())
                    trusted.parent.mkdir(parents=True, exist_ok=True)
                    trusted.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
                if not trusted or not trusted.is_file():
                    raise RuntimeError("operation output is unavailable")
                operation.output_digest = digest_file(trusted)
                operation.status = "completed"
                operation.completed_at = now()
            elif action == "authorize-retry":
                operation.status = "failed"
                operation.detail_code = "HUMAN_AUTHORIZED_RETRY"
            else:
                raise RuntimeError("unknown reconciliation action")
            state.status = "idle"
            state.escalation_code = state.escalation_detail = None
            self.store.event(state, "operation_reconciled", operation=operation_id, action=action)

    def approve(self, nonce: str) -> None:
        with self.store.lock():
            state = self.store.load()
            requirement = state.pending_approval
            if not requirement or not secrets.compare_digest(requirement.nonce, nonce):
                raise RuntimeError("approval nonce does not match pending checkpoint")
            if requirement.state_generation != state.generation - 1:
                raise RuntimeError("approval is stale for the current state generation")
            phase_attempt = state.phase_attempts.get(str(requirement.phase), 0)
            if phase_attempt != requirement.phase_attempt or self.git.tree_hash() != requirement.tree_hash:
                raise RuntimeError("approval scope no longer matches phase attempt or tree")
            state.pending_approval = None
            state.status = "idle"
            state.escalation_code = state.escalation_detail = None
            self.store.event(state, "checkpoint_approved", scope=vars(requirement))

    def run(self, until: int | None = None, only: int | None = None) -> None:
        if self.dry_run:
            raise RuntimeError("dry-run does not execute phases")
        with self.store.lock():
            state = self.store.load()
            if self.store.recover_stale_run(state):
                raise HumanEscalation(state.escalation_code or "STALE_RUN")
            if state.status in {"paused", "human_escalation", "failed", "running"}:
                raise RuntimeError(f"build is {state.status}")
            selected = [p for p in self.phases if p.number not in state.completed_phases]
            if only is not None:
                selected = [p for p in self.phases if p.number == only]
                if not selected:
                    raise RuntimeError(f"unknown phase: {only}")
                if only in state.completed_phases:
                    # Re-running would build a second attempt on top of the accepted one and
                    # count the phase twice.
                    raise RuntimeError(f"phase {only} is already complete")
            if until is not None:
                selected = [p for p in selected if p.number <= until]
            state.status = "running"
            state.started_at = state.started_at or now()
            self.store.save(state, expected_generation=state.generation)
            try:
                for phase in selected:
                    self._run_phase(state, phase)
                done = {phase.number for phase in self.phases} <= set(state.completed_phases)
                state.status = "complete" if done else "idle"
                self.store.save(state, expected_generation=state.generation)
            except HumanEscalation:
                raise
            except Exception as exc:
                state.status = "failed"
                state.escalation_code = "UNEXPECTED_FAILURE"
                state.escalation_detail = redact(str(exc), limit=500)
                self.store.event(state, "phase_failed", code=state.escalation_code)
                raise

    def _run_phase(self, state: BuildState, phase: Phase) -> None:
        key = str(phase.number)
        if state.current_phase != phase.number:
            state.current_phase, state.phase_step = phase.number, "pending"
            state.phase_attempts[key] = state.phase_attempts.get(key, 0) + 1
            state.base_sha = self.git.current_sha()
            state.deadline_at = self._phase_deadline()
            self.store.event(state, "phase_started", phase=phase.number, attempt=state.phase_attempts[key])
        if state.phase_attempts[key] > self.config["limits"]["max_phase_attempts"]:
            self._ceiling_reached(state, phase, "PHASE_ATTEMPTS_EXHAUSTED", "phase attempt ceiling")
        if state.phase_step == "committed":
            if phase.merge_policy == "human" and not self._approval_recorded(state, phase, f"merge-phase-{phase.number}"):
                self._require_approval(state, phase, f"merge-phase-{phase.number}")
            self._finish_phase(state, phase, state.active_branch or f"build/phase-{phase.number}")
            return
        self._checkpoint(state, phase)
        if not state.workspace:
            workspace = Path(tempfile.mkdtemp(prefix=f"buildctl-phase-{phase.number}-")) / "workspace"
            self.git.create_isolated_clone(state.base_sha or "", workspace)
            self.git.prepare_agent_workspace(workspace)
            state.workspace = str(workspace)
            state.phase_step = "clone_completed"
            self.store.event(state, "clone_completed", workspace=str(workspace))
        workspace = Path(state.workspace)
        baseline_path = self.control_root / f"phase-{phase.number}-baseline.json"
        if not baseline_path.exists():
            baseline_path.write_text(json.dumps(self.git.manifest(workspace), sort_keys=True))
        baseline = json.loads(baseline_path.read_text())
        context = IsolationContext("implementer", workspace, self.control_root, self.root)
        role_runner = RoleRunner(self.config, self.backend, context, self.runner)
        codex, claude = CodexAdapter(role_runner, self.snapshot), ClaudeAdapter(role_runner, self.snapshot)
        debate = DebateAdapter(role_runner, self.snapshot, self.config)
        paths = self._paths(phase)
        phase_result = self._agent_operation(state, phase, "implement", PaidOperation.IMPLEMENTATION, paths["result"],
                                             lambda timeout: codex.implement(phase, paths["result"], timeout))
        phase_result = self._consume_interactive_debate_if_ready(
            state, phase, codex, workspace, paths, phase_result)
        gate_runner = MechanicalGates(workspace, self.control_root, self.root, self.backend, self.runner)
        while True:
            remediation = state.remediation_attempts.get(key, 0)
            round_id = remediation + 1
            gate_path = paths["gate"].with_name(f"GATE_RESULT-{round_id}.json")
            gate = self._gate_operation(state, phase, gate_runner, gate_path, f"gate-{round_id}")
            if not gate["passed"]:
                if remediation >= phase.max_remediation_attempts:
                    self._ceiling_reached(state, phase, "REMEDIATION_ATTEMPTS_EXHAUSTED", "mechanical remediation ceiling")
                state.remediation_attempts[key] = remediation + 1
                review_text = json.dumps({"mechanical_gate_failures": gate["details"]}, indent=2)
                remediation_path = paths["result"].with_name(f"PHASE_RESULT-remediation-{remediation + 1}.json")
                phase_result = self._agent_operation(
                    state, phase, f"remediate-{remediation + 1}", PaidOperation.REMEDIATION, remediation_path,
                    lambda timeout, rp=remediation_path, rt=review_text: codex.implement(phase, rp, timeout, review=rt))
                continue
            delta = self.git.attributed_delta(workspace, baseline, self._allowed_paths(phase, workspace, paths))
            evidence = self.git.review_evidence(workspace, delta, gate)
            # Park the findings the reviewer could not adjudicate even if it tried — the
            # validator refuses a citation outside this delta's coverage — and un-park the
            # ones this delta has made citable again. Runs on every lap, and only here,
            # because the reviewable set changes with each remediation: a defect fixed by
            # reverting a file drops that path out of the delta, and a later lap that
            # touches a new file brings its parked findings back into scope.
            parked, restored = realign_findings_to_delta(
                state.findings, [item["path"] for item in evidence["coverage"]])
            if parked:
                self.store.event(state, "findings_deferred", phase=phase.number,
                                 count=len(parked), paths=list(parked))
            if restored:
                self.store.event(state, "findings_restored", phase=phase.number,
                                 count=len(restored), paths=list(restored))
            evidence.update({"governance_sha": state.governance_sha, "snapshot_digest": state.snapshot_digest,
                             "base_sha": state.base_sha, "phase": phase.number,
                             "reviewer": {"vendor": self.config["roles"]["reviewer"]["vendor"],
                                          "model": self.config["roles"]["reviewer"]["model_family"],
                                          "prompt_lineage": self.config["roles"]["reviewer"]["prompt_lineage"],
                                          "execution_boundary": self.backend.name},
                             # The reviewer cannot retire a finding it has never been shown,
                             # and cannot guess a prior claim_key. Hand it the register.
                             "open_findings": open_finding_obligations(state.findings)})
            if evidence["unreviewable"] and phase.claude_review_required:
                self._escalate(state, "UNREVIEWABLE_DELTA", ", ".join(evidence["unreviewable"]))
            semantic = state.semantic_review_attempts.get(key, 0) + 1
            if semantic > self.config["limits"]["max_semantic_review_attempts"]:
                self._ceiling_reached(state, phase, "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED", "semantic review ceiling")
            state.semantic_review_attempts[key] = semantic
            review_path = paths["review"].with_name(f"REVIEW_RESULT-{semantic}.json")
            review = self._agent_operation(state, phase, f"review-{semantic}", PaidOperation.REVIEW, review_path,
                                           lambda timeout, rp=review_path: claude.review(phase, evidence, phase_result, rp, timeout),
                                           format_retry=True)
            expected = {"governance_sha": state.governance_sha, "snapshot_digest": state.snapshot_digest,
                        "base_sha": state.base_sha, "phase": phase.number, "delta_id": delta.digest,
                        "evidence_digest": evidence["digest"],
                        "reviewer": {"vendor": self.config["roles"]["reviewer"]["vendor"],
                                     "model": self.config["roles"]["reviewer"]["model_family"],
                                     "prompt_lineage": self.config["roles"]["reviewer"]["prompt_lineage"],
                                     "execution_boundary": self.backend.name},
                        "coverage": {(item["path"], item["digest"], item["change_type"])
                                     for item in evidence["coverage"]}}
            try:
                validate_review(review, expected)
                accumulate_findings(state.findings, review, state.snapshot_digest or "")
            except ReviewValidationError as exc:
                formats = state.review_format_attempts.get(key, 0) + 1
                state.review_format_attempts[key] = formats
                # Record why the report was rejected; an attempt counter alone hid a
                # trivial vocabulary mismatch behind REVIEW_FORMAT_ATTEMPTS_EXHAUSTED.
                self.store.event(state, "review_format_rejected", attempt=formats,
                                 reason=redact(str(exc), limit=DIAGNOSTIC_LIMIT))
                if formats >= self.config["limits"]["max_review_format_attempts"]:
                    self._ceiling_reached(state, phase, "REVIEW_FORMAT_ATTEMPTS_EXHAUSTED", str(exc))
                continue
            findings = list(state.findings.values())
            budget_available, approvals_valid = self._advance_preconditions(state, phase)
            transition = reduce_review(report=review, findings=findings, mechanical_passed=True, coverage_valid=True,
                                       policy={"max_remediation_attempts": phase.max_remediation_attempts,
                                               "debate_enabled": phase.debate_on_architecture_disagreement,
                                               "max_debate_attempts": self.config["limits"]["max_debate_rounds"]},
                                       attempts={"remediation": remediation,
                                                 "debate": state.debate_attempts.get(key, 0)},
                                       budget_available=budget_available, approvals_valid=approvals_valid)
            self.store.event(state, "review_reduced", rule=transition.rule_id, disposition=transition.disposition,
                             evidence_digest=evidence["digest"], delta_id=delta.digest,
                             reviewer=review["reviewer"])
            if transition.disposition == "ADVANCE":
                break
            if transition.disposition == "REMEDIATE":
                if remediation >= phase.max_remediation_attempts:
                    self._ceiling_reached(state, phase, "REMEDIATION_ATTEMPTS_EXHAUSTED", transition.rationale)
                state.remediation_attempts[key] = remediation + 1
                remediation_path = paths["result"].with_name(f"PHASE_RESULT-remediation-{remediation + 1}.json")
                phase_result = self._agent_operation(
                    state, phase, f"remediate-{remediation + 1}", PaidOperation.REMEDIATION, remediation_path,
                    lambda timeout, rp=remediation_path, rt=json.dumps(review): codex.implement(phase, rp, timeout, review=rt))
                continue
            if transition.disposition == "DEBATE":
                debate_attempt = state.debate_attempts.get(key, 0) + 1
                state.debate_attempts[key] = debate_attempt
                debate_path = paths["debate"].with_name(f"DEBATE_RESULT-{debate_attempt}.json")
                # Only the findings that routed the phase here. Handing over the whole
                # register buries the question among resolved and superseded entries, and
                # would make the resulting ADR claim to decide findings it never considered.
                strategic = strategic_findings(findings)
                result = self._agent_operation(state, phase, f"debate-{debate_attempt}", PaidOperation.DEBATE, debate_path,
                                               lambda timeout, dp=debate_path: debate.run(phase, strategic, evidence, dp, timeout))
                validate_debate_result(result)
                if result["status"] != "agreement":
                    self._escalate(state, "DEBATE_UNRESOLVED", result["status"])
                write_adr(paths["adr_proposal"], {"title": f"Phase {phase.number} decision", "debate_id": f"phase-{phase.number}",
                                          "governance_sha": state.governance_sha, "base_sha": state.base_sha,
                                          "evidence_digest": evidence["digest"], "finding_ids": [x["id"] for x in strategic]}, result)
                adr_target = workspace / "decisions" / f"ADR-phase-{phase.number}.md"
                adr_target.parent.mkdir(parents=True, exist_ok=True)
                adr_target.write_bytes(paths["adr_proposal"].read_bytes())
                if remediation >= phase.max_remediation_attempts:
                    self._ceiling_reached(state, phase, "REMEDIATION_ATTEMPTS_EXHAUSTED", "ADR implementation ceiling")
                state.remediation_attempts[key] = remediation + 1
                remediation_path = paths["result"].with_name(f"PHASE_RESULT-adr-{debate_attempt}.json")
                phase_result = self._agent_operation(
                    state, phase, f"adr-implement-{debate_attempt}", PaidOperation.REMEDIATION, remediation_path,
                    lambda timeout, rp=remediation_path: codex.implement(phase, rp, timeout, adr=paths["adr_proposal"].read_text()))
                continue
            if transition.rule_id == "R7_FAIL_CLOSED":
                unmet = [name for name, held in (("budget", budget_available), ("approvals", approvals_valid))
                         if not held]
                self._escalate(state, "ADVANCE_PRECONDITIONS_UNMET", ", ".join(unmet))
            self._escalate(state, "REVIEW_BLOCKED", transition.rationale)
        publication_delta = self.git.attributed_delta(workspace, baseline, self._allowed_paths(phase, workspace, paths))
        if publication_delta.digest != delta.digest:
            self._escalate(state, "DELTA_CHANGED_AFTER_REVIEW", "publication delta does not match reviewer attestation")
        delta = publication_delta
        branch = self.git.apply_and_commit(workspace, delta, phase, state.base_sha or "")
        state.active_branch, state.phase_step = branch, "committed"
        self.store.event(state, "phase_committed", phase=phase.number, branch=branch)
        if phase.merge_policy == "human":
            self._require_approval(state, phase, f"merge-phase-{phase.number}")
        self._finish_phase(state, phase, branch)

    def _finish_phase(self, state: BuildState, phase: Phase, branch: str) -> None:
        publication = self.git.publish(phase)
        if phase.number not in state.completed_phases:
            state.completed_phases.append(phase.number)
        state.current_phase, state.workspace, state.active_branch, state.phase_step = None, None, None, "pending"
        state.consecutive_failures = 0
        self.store.event(state, "phase_completed", phase=phase.number, branch=branch, publication=publication)
        if phase.number == self.config["checkpoints"]["mvp_completion"]:
            self._require_approval(state, phase, "mvp_completion")

    def _agent_operation(self, state: BuildState, phase: Phase, kind: str, paid_operation: PaidOperation,
                         output: Path, function: Any,
                         format_retry: bool = False) -> dict[str, Any]:
        operation_id = f"phase-{phase.number}:{kind}:{state.phase_attempts[str(phase.number)]}"
        operation = state.operations.get(operation_id)
        if operation and operation.status == "completed":
            return json.loads(Path(operation.output_path or output).read_text())
        if operation and operation.status in {"started", "unknown"}:
            self._escalate(state, "UNKNOWN_EXTERNAL_OPERATION", operation_id)
        role_name = {PaidOperation.IMPLEMENTATION: "implementer", PaidOperation.REMEDIATION: "implementer",
                     PaidOperation.REVIEW: "reviewer", PaidOperation.DEBATE: "arbiter"}[paid_operation]
        try:
            self._require_authentication(role_name)
        except (AuthenticationRequired, HostAuthUnsupported) as exc:
            self._escalate(state, "AUTHENTICATION_REQUIRED", str(exc))
        # Reserve before the operation is recorded as started. A refused reservation means
        # nothing was launched, so it must not leave a `started` operation behind that no
        # later run can account for.
        try:
            self.budget.reserve(paid_operation.value, phase.number, operation_id,
                                configured_cost(self.config, paid_operation))
        except LimitExceeded as exc:
            self._escalate(state, "DAILY_BUDGET_UNAVAILABLE", str(exc))
        operation = Operation(operation_id, kind, phase.number, status="started", output_path=str(output),
                              untrusted_output_path=str(operation_output_path(Path(state.workspace or self.root), output)),
                              started_at=now())
        state.operations[operation_id] = operation
        self.store.event(state, "operation_started", operation=operation_id,
                         paid_operation=paid_operation.value, role=role_name,
                         network_allowed=self.config["roles"][role_name]["network"])
        try:
            result = function(self._remaining_seconds(state))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            operation.output_digest = digest_file(output)
            operation.status, operation.completed_at = "completed", now()
            self.store.event(state, "operation_completed", operation=operation_id, digest=operation.output_digest)
            return result
        except subprocess.TimeoutExpired:
            operation.status = "unknown"
            self._escalate(state, "OPERATION_TIMEOUT_UNKNOWN", operation_id)
        except AgentError as exc:
            # RoleRunner has already redacted staged/configured secrets out of this message;
            # keep the stable code for control flow and a bounded detail for the operator.
            message = str(exc)
            operation.status, operation.detail_code = "failed", message.split(":", 1)[0]
            diagnostic = redact(message, limit=DIAGNOSTIC_LIMIT)
            self.store.event(state, "operation_failed", operation=operation_id,
                             code=operation.detail_code, diagnostic=diagnostic)
            if format_retry and operation.detail_code.endswith(FORMAT_RETRYABLE_SUFFIXES):
                return {}
            self._escalate(state, "AGENT_OPERATION_FAILED", diagnostic)
        raise AssertionError("unreachable")

    def _advance_preconditions(self, state: BuildState, phase: Phase) -> tuple[bool, bool]:
        """The two ADVANCE predicates the reducer cannot see: (budget_available, approvals_valid).

        Both used to be passed as literal True, which left the reducer's R7_FAIL_CLOSED branch
        unreachable from its only caller. Each is evaluated fail-closed: an unreadable ledger
        or an unverifiable approval counts as not held.

        Budget: the ledger must be intact and today's spend within the daily limit. Advancing
        spends nothing, so reaching the limit exactly does not block it.
        Approvals: nothing may be pending, and a phase behind a checkpoint must still carry an
        approval scoped to this attempt, this governance SHA and the current tree.
        """
        try:
            budget_available = self.budget.spent_today() <= self.budget.daily_limit
        except LimitExceeded:
            budget_available = False
        approvals_valid = state.pending_approval is None
        if approvals_valid and phase.number in self.config["checkpoints"]["before_phases"]:
            try:
                approvals_valid = self._approval_recorded(state, phase, f"phase-{phase.number}")
            except GitSafetyError:
                approvals_valid = False
        return budget_available, approvals_valid

    def _require_authentication(self, role: str) -> None:
        role_config = self.config["roles"][role]
        mode = role_config.get("auth_mode", "environment")
        if mode == "none":
            return
        if mode == "environment":
            missing = [key for key in role_config.get("credential_env", []) if not os.environ.get(key)]
            if missing:
                raise AuthenticationRequired(role, "set the configured credential environment")
            return
        if mode == "host_cli":
            result = AuthPreflight(self.runner, self.config).check()
            if role == "implementer" and not result.get("implementer", {}).get("authenticated"):
                raise AuthenticationRequired("openai", "codex login")
            if role == "implementer":
                validate_codex_auth()
                return
            if role == "reviewer":
                return
            raise HostAuthUnsupported(f"{role} host_cli authentication is unsupported")
        raise HostAuthUnsupported(f"{role} authentication mode is unsupported")

    def _gate_operation(self, state: BuildState, phase: Phase, gate_runner: MechanicalGates, output: Path,
                        kind: str = "gate") -> dict[str, Any]:
        operation_id = f"phase-{phase.number}:{kind}:{state.phase_attempts[str(phase.number)]}"
        existing = state.operations.get(operation_id)
        if existing and existing.status == "completed":
            return json.loads(Path(existing.output_path or output).read_text())
        if existing and existing.status in {"started", "unknown"}:
            self._escalate(state, "UNKNOWN_EXTERNAL_OPERATION", operation_id)
        operation = Operation(operation_id, "gate", phase.number, status="started", output_path=str(output), started_at=now())
        state.operations[operation_id] = operation
        self.store.event(state, "operation_started", operation=operation_id)
        result = gate_runner.run(phase, self._remaining_seconds(state))
        value = {"passed": result.passed, "details": result.details}
        write_gate_result(output, result, self._metadata(state))
        operation.status, operation.output_digest, operation.completed_at = "completed", digest_file(output), now()
        self.store.event(state, "operation_completed", operation=operation_id)
        return value

    def _phase_deadline(self) -> str:
        """Wall-clock deadline for a phase attempt that is starting now."""
        return (datetime.now(timezone.utc)
                + timedelta(minutes=self.config["limits"]["max_phase_minutes"])).isoformat()

    def _release_phase_baseline(self, phase_number: int | None) -> None:
        """Drop the manifest baseline belonging to a workspace that is being discarded.

        The baseline describes the clone the previous attempt started from. A fresh attempt
        clones again — potentially from a newer base SHA — so a retained baseline would
        mis-attribute the next delta.
        """
        if phase_number is None:
            return
        (self.control_root / f"phase-{phase_number}-baseline.json").unlink(missing_ok=True)

    def _remaining_seconds(self, state: BuildState) -> float:
        if not state.deadline_at:
            raise RuntimeError("phase deadline is missing")
        remaining = (datetime.fromisoformat(state.deadline_at) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            self._escalate(state, "PHASE_DEADLINE_EXCEEDED", "wall-clock phase deadline expired")
        return remaining

    def _checkpoint(self, state: BuildState, phase: Phase) -> None:
        if phase.number in self.config["checkpoints"]["before_phases"]:
            if not self._approval_recorded(state, phase, f"phase-{phase.number}"):
                self._require_approval(state, phase, f"phase-{phase.number}")

    def _approval_recorded(self, state: BuildState, phase: Phase, checkpoint: str) -> bool:
        return any(item["event"] == "checkpoint_approved"
                   and item.get("scope", {}).get("checkpoint") == checkpoint
                   and item.get("scope", {}).get("phase") == phase.number
                   and item.get("scope", {}).get("phase_attempt") == state.phase_attempts[str(phase.number)]
                   and item.get("scope", {}).get("governance_sha") == state.governance_sha
                   and item.get("scope", {}).get("tree_hash") == self.git.tree_hash()
                   for item in state.history)

    def _require_approval(self, state: BuildState, phase: Phase, checkpoint: str) -> None:
        requirement = ApprovalRequirement(checkpoint, phase.number, state.phase_attempts[str(phase.number)],
                                          self.git.tree_hash(), state.governance_sha or "", state.generation,
                                          secrets.token_hex(16))
        state.pending_approval = requirement
        self._escalate(state, "CHECKPOINT_REQUIRED", checkpoint)

    def _ceiling_reached(self, state: BuildState, phase: Phase, code: str, detail: str) -> None:
        """A bounded budget is exhausted: escalate, unless a human authorized this exact one.

        Every ceiling routes through here so the extra-attempt grant means the same thing
        everywhere. A grant only lets execution continue when it names this phase and this
        ceiling code, has been authorized through `retry --authorize-extra-attempt` with a
        matching nonce, and has not already been spent — and it is consumed on use, so it
        buys exactly one pass. Anything else escalates.
        """
        grant = state.extra_attempt_grant
        if (grant and grant.phase == phase.number and grant.ceiling_code == code
                and grant.authorized and not grant.consumed):
            grant.consumed = True
            self.store.event(state, "extra_attempt_consumed", phase=phase.number, ceiling=code)
            return
        self._escalate(state, code, detail)

    def _escalate(self, state: BuildState, code: str, detail: str) -> None:
        if code != "CHECKPOINT_REQUIRED":
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.config["limits"]["max_consecutive_failures"]:
                code = "MAX_CONSECUTIVE_FAILURES_EXHAUSTED"
                detail = "consecutive failure ceiling reached; explicit extra-attempt authorization required"
        state.status, state.escalation_code = "human_escalation", code
        if code.endswith("_EXHAUSTED") and state.current_phase is not None:
            state.extra_attempt_grant = ExtraAttemptGrant(state.current_phase, code, state.generation,
                                                          secrets.token_hex(16))
        state.escalation_detail = redact(detail, limit=500)
        self.store.event(state, "human_escalation", code=code)
        raise HumanEscalation(code)

    def _metadata(self, state: BuildState) -> dict[str, str]:
        return {"governance_sha": state.governance_sha or "", "snapshot_digest": state.snapshot_digest or "",
                "base_sha": state.base_sha or "", "isolation_backend": state.isolation_backend or ""}

    def _paths(self, phase: Phase) -> dict[str, Path]:
        directory = self.control_root / "artifacts" / f"phase-{phase.number}"
        directory.mkdir(parents=True, exist_ok=True)
        return {"result": directory / "PHASE_RESULT.json", "review": directory / "REVIEW_RESULT.json",
                "debate": directory / "DEBATE_RESULT.json", "gate": directory / "GATE_RESULT.json",
                "adr_proposal": directory / f"ADR-phase-{phase.number}.proposed.md"}

    def _allowed_paths(self, phase: Phase, workspace: Path, paths: dict[str, Path]) -> tuple[str, ...]:
        relative = f"decisions/ADR-phase-{phase.number}.md"
        target = workspace / relative
        if paths["adr_proposal"].is_file() and target.is_file():
            if digest_file(paths["adr_proposal"]) != digest_file(target):
                raise GitSafetyError("ADR import differs from validated supervisor proposal")
            return (*phase.allowed_paths, relative)
        return phase.allowed_paths

    def verify_isolation(self) -> dict[str, Any]:
        if self.dry_run:
            raise IsolationError("dry-run cannot certify production isolation")
        verify_backend(self.backend, self.control_root, self.root, self.runner)
        record = conformance_record(self.control_root, self.backend, self.anchor["snapshot_digest"])
        path = self.control_root / "isolation-conformance.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        os.chmod(path, 0o600)
        return record

    def _consume_interactive_debate_if_ready(self, state: BuildState, phase: Phase, codex: CodexAdapter,
                                             workspace: Path, paths: dict[str, Path],
                                             phase_result: dict[str, Any]) -> dict[str, Any]:
        request_path = paths["debate"].with_name("DEBATE_REQUEST.json")
        if not request_path.is_file():
            return phase_result
        request = json.loads(request_path.read_text())
        result_path = Path(request.get("output_path", ""))
        if not result_path.is_file():
            return phase_result
        if (request.get("phase") != phase.number or request.get("governance_sha") != state.governance_sha
                or request.get("base_sha") != state.base_sha):
            self._escalate(state, "STALE_DEBATE_RESULT", "interactive debate binding mismatch")
        result = json.loads(result_path.read_text()); validate_debate_result(result)
        if result["status"] != "agreement":
            self._escalate(state, "DEBATE_UNRESOLVED", result["status"])
        paths["adr_proposal"].parent.mkdir(parents=True, exist_ok=True)
        write_adr(paths["adr_proposal"], {"title": f"Phase {phase.number} decision",
                  "debate_id": f"phase-{phase.number}", "governance_sha": state.governance_sha,
                  "base_sha": state.base_sha, "evidence_digest": request["evidence_digest"],
                  "finding_ids": [item["id"] for item in request["findings"]]}, result)
        target = workspace / "decisions" / f"ADR-phase-{phase.number}.md"
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(paths["adr_proposal"].read_bytes())
        operation = next((item for item in state.operations.values()
                          if item.kind.startswith("debate-") and item.phase == phase.number
                          and item.output_path == str(result_path)), None)
        if operation:
            operation.status, operation.output_digest, operation.completed_at = "completed", digest_file(result_path), now()
            self.store.event(state, "interactive_debate_consumed", operation=operation.id,
                             evidence_digest=request["evidence_digest"])
        key = str(phase.number); remediation = state.remediation_attempts.get(key, 0) + 1
        if remediation > phase.max_remediation_attempts:
            self._ceiling_reached(state, phase, "REMEDIATION_ATTEMPTS_EXHAUSTED", "ADR implementation ceiling")
        state.remediation_attempts[key] = remediation
        output = paths["result"].with_name(f"PHASE_RESULT-adr-resume-{remediation}.json")
        return self._agent_operation(state, phase, f"adr-resume-{remediation}", PaidOperation.REMEDIATION,
                                     output, lambda timeout: codex.implement(
                                         phase, output, timeout, adr=paths["adr_proposal"].read_text()))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="buildctl")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--repo", help="governed repository (default: Git toplevel of the current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    commands.add_parser("start")
    run = commands.add_parser("run"); run.add_argument("--until")
    commands.add_parser("status"); commands.add_parser("pause"); commands.add_parser("resume")
    retry = commands.add_parser("retry"); retry.add_argument("--authorize-extra-attempt", action="store_true")
    retry.add_argument("--nonce")
    phase = commands.add_parser("phase"); phase.add_argument("number", type=int)
    approve = commands.add_parser("approve"); approve.add_argument("--nonce", required=True)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--operation", required=True)
    reconcile.add_argument("--action", choices=("accept-output", "authorize-retry"), required=True)
    trust = commands.add_parser("trust"); trust_sub = trust.add_subparsers(dest="trust_command", required=True)
    init = trust_sub.add_parser("init"); init.add_argument("--to", required=True); init.add_argument("--confirm", required=True)
    trust_sub.add_parser("show")
    isolation = commands.add_parser("isolation"); isolation_sub = isolation.add_subparsers(dest="isolation_command", required=True)
    isolation_sub.add_parser("verify")
    auth = commands.add_parser("auth"); auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("check")
    commands.add_parser("validate")
    return result


def resolve_repository(value: str | None, runner: Any = subprocess.run) -> Path:
    """The governed repository: explicit, or the Git toplevel containing the working directory.

    The supervisor is installed separately from what it governs, so the repository can no
    longer be inferred from where this file lives. It must be the toplevel itself: a nested
    path would shift every governed path the trust snapshot is keyed on.
    """
    start = Path(value).resolve() if value else Path.cwd()
    result = runner(["git", "-C", str(start), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"not inside a Git repository: {start}")
    toplevel = Path(result.stdout.strip()).resolve()
    if value and toplevel != start:
        raise ValueError(f"--repo must name the repository toplevel ({toplevel}), not {start}")
    return toplevel


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = resolve_repository(args.repo)
        control_root = control_root_for(root)
        if args.command == "trust":
            trust = TrustStore(root, control_root)
            if args.trust_command == "init": print(json.dumps(trust.initialize(args.to, args.confirm), indent=2))
            else: print(json.dumps(trust.load(), indent=2))
            return 0
        if args.command == "validate":
            trust = TrustStore(root, control_root)
            if trust.anchor_path.is_file():
                config = trust.config(); source = "trusted snapshot"
            else:
                config = load_config(root / "build.yaml"); source = "untrusted worktree (trust not initialized)"
            print(f"build.yaml: valid ({len(config['phases'])} phases; {source})")
            return 0
        if args.command == "auth":
            if args.auth_command != "check":
                raise ValueError("unknown auth command")
            try:
                auth_config = load_config(root / "build.yaml")
                print(json.dumps(AuthPreflight(config=auth_config).check(), indent=2, sort_keys=True))
                return 0
            except AuthenticationRequired as exc:
                print(json.dumps({"status": "AUTHENTICATION_REQUIRED", "provider": exc.provider,
                                  "remediation": exc.command}, indent=2, sort_keys=True))
                return 2
        supervisor = Supervisor(root, dry_run=args.dry_run)
        if args.command == "isolation":
            print(json.dumps(supervisor.verify_isolation(), indent=2, sort_keys=True)); return 0
        if args.command == "bootstrap": supervisor.bootstrap()
        elif args.command in {"start", "run"}:
            supervisor.bootstrap(); supervisor.run(until=resolve_until(supervisor.config, getattr(args, "until", None)))
        elif args.command == "phase": supervisor.bootstrap(); supervisor.run(only=args.number)
        elif args.command == "status": print(json.dumps(supervisor.status(), indent=2, sort_keys=True))
        elif args.command == "pause": supervisor.pause()
        elif args.command == "resume": supervisor.resume()
        elif args.command == "retry": supervisor.retry(args.authorize_extra_attempt, args.nonce)
        elif args.command == "approve": supervisor.approve(args.nonce)
        elif args.command == "reconcile": supervisor.reconcile(args.operation, args.action)
        return 0
    except (ConfigurationError, AgentError, GitSafetyError, LimitExceeded, HumanEscalation,
            IsolationError, ReviewValidationError, RuntimeError, StateConflict, SupervisorLocked,
            TrustError, ValueError) as exc:
        print(f"buildctl: {redact(str(exc), limit=500)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"buildctl: UNEXPECTED_FAILURE: {redact(str(exc), limit=500)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
