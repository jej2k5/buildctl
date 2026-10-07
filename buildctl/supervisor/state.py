from __future__ import annotations

import fcntl
import json
import os
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .audit import AuditChain


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Operation:
    id: str
    kind: str
    phase: int
    status: str = "pending"  # pending|started|completed|failed|unknown
    attempt: int = 1
    input_digest: str = ""
    output_path: str | None = None
    untrusted_output_path: str | None = None
    output_digest: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    detail_code: str | None = None


@dataclass
class ApprovalRequirement:
    checkpoint: str
    phase: int
    phase_attempt: int
    tree_hash: str
    governance_sha: str
    state_generation: int
    nonce: str


@dataclass
class ExtraAttemptGrant:
    phase: int
    ceiling_code: str
    attempt_generation: int
    nonce: str
    authorized: bool = False
    consumed: bool = False


@dataclass
class BuildState:
    schema_version: int = 2
    generation: int = 0
    audit_sequence: int = 0
    audit_head_hash: str = "0" * 64
    status: str = "idle"
    current_phase: int | None = None
    phase_step: str = "pending"
    completed_phases: list[int] = field(default_factory=list)
    phase_attempts: dict[str, int] = field(default_factory=dict)
    review_format_attempts: dict[str, int] = field(default_factory=dict)
    semantic_review_attempts: dict[str, int] = field(default_factory=dict)
    remediation_attempts: dict[str, int] = field(default_factory=dict)
    debate_attempts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0
    escalation_code: str | None = None
    escalation_detail: str | None = None
    pending_approval: ApprovalRequirement | None = None
    active_branch: str | None = None
    base_sha: str | None = None
    governance_sha: str | None = None
    snapshot_digest: str | None = None
    isolation_backend: str | None = None
    workspace: str | None = None
    deadline_at: str | None = None
    extra_attempt_grant: ExtraAttemptGrant | None = None
    operations: dict[str, Operation] = field(default_factory=dict)
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str | None = None
    updated_at: str = field(default_factory=now)
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BuildState":
        known = cls.__dataclass_fields__
        values = {key: value for key, value in data.items() if key in known}
        values["operations"] = {key: Operation(**value) for key, value in values.get("operations", {}).items()}
        approval = values.get("pending_approval")
        if approval:
            values["pending_approval"] = ApprovalRequirement(**approval)
        grant = values.get("extra_attempt_grant")
        if grant:
            values["extra_attempt_grant"] = ExtraAttemptGrant(**grant)
        return cls(**values)


class StateConflict(RuntimeError):
    pass


class SupervisorLocked(RuntimeError):
    pass


class StateStore:
    """Canonical state lives outside the governed repository."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "supervisor.lock"
        self.audit = AuditChain(self.directory)

    @contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                raise SupervisorLocked("another supervisor process holds the canonical state lock") from exc
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": now()}).encode())
            os.fsync(fd)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def load(self) -> BuildState:
        if not self.path.exists():
            sequence, head = self.audit.verify()
            if sequence != 0 or head != "0" * 64:
                raise RuntimeError("AUDIT_HEAD_MISMATCH: audit exists without canonical state")
            return BuildState()
        try:
            value = json.loads(self.path.read_text())
            if value.get("schema_version") != 2:
                raise RuntimeError("legacy state requires explicit migration")
            state = BuildState.from_dict(value)
            sequence, head = self.audit.verify()
            if (sequence, head) != (state.audit_sequence, state.audit_head_hash):
                raise RuntimeError("AUDIT_HEAD_MISMATCH: canonical state and audit terminal head differ")
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"invalid canonical state at {self.path}: {exc}") from exc

    def save(self, state: BuildState, *, expected_generation: int | None = None,
             _audit_event: str = "state_saved", _audit_payload: dict[str, Any] | None = None) -> None:
        if os.environ.get("BUILDCTL_GATE_PROCESS") == "1":
            raise RuntimeError("gate subprocess cannot mutate canonical state")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        current_state = self.load()
        current = current_state.generation if self.path.exists() else 0
        if expected_generation is not None and current != expected_generation:
            raise StateConflict(f"state generation changed: expected {expected_generation}, found {current}")
        state.generation = current + 1
        state.updated_at = now()
        payload = _audit_payload or {"generation": state.generation, "status": state.status,
                                     "current_phase": state.current_phase, "phase_step": state.phase_step}
        state.audit_sequence, state.audit_head_hash = self.audit.append(state.updated_at, _audit_event, payload)
        self._write_state(state)

    def _write_state(self, state: BuildState) -> None:
        fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(asdict(state), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def event(self, state: BuildState, event: str, **details: Any) -> None:
        generation = state.generation
        timestamp = now()
        state.history.append({"at": timestamp, "event": event, **details})
        state.history = state.history[-1000:]
        self.save(state, expected_generation=generation, _audit_event=event, _audit_payload=details)

    def recover_stale_run(self, state: BuildState) -> bool:
        if state.status != "running":
            return False
        unknown = False
        for operation in state.operations.values():
            if operation.status == "started":
                operation.status = "unknown"
                unknown = True
        state.status = "human_escalation"
        state.escalation_code = "STALE_RUN_UNKNOWN_OPERATION" if unknown else "STALE_RUN"
        state.escalation_detail = "reconcile unknown operations before retry" if unknown else "explicit retry required"
        self.event(state, "stale_run_recovered", unknown_operations=unknown)
        return True
