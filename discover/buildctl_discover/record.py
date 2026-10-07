"""The discovery record: an append-only, hash-chained event log per session.

The record is the source of truth. Proposal files are rendered from it and never written by
hand. Ids (D-n, Q-n, P-n, PH-n, RV-n, F-n) are assigned under the session lock, stored in the
event payload, and never reused, so replay is deterministic.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from buildctl.supervisor.audit import AuditChain
from buildctl.supervisor.state import now

ROLES = ("owner", "operator")
CONFIDENCE = ("high", "medium", "low")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
LIST_FIELDS = ("required_files", "commands", "allowed_paths", "invariants")
# Keys the record owns. Everything else in build.yaml comes from the base snapshot.
ROADMAP_KEYS = ("phases", "milestones", "checkpoints")

# The manual's minimal configuration, without its roadmap. Used when the repository has no
# build.yaml yet. The operator tunes it in the session base before finalizing.
BUILTIN_BASE: dict[str, Any] = {
    "limits": {"max_phase_attempts": 6, "max_review_format_attempts": 4,
               "max_semantic_review_attempts": 10, "max_debate_rounds": 3, "max_phase_minutes": 120,
               "max_daily_cost_usd": 50.0, "max_consecutive_failures": 3},
    "operation_cost_usd": {"implementation": 2.0, "review": 2.0, "remediation": 2.0, "debate": 5.0},
    "governance_documents": [],
    "git": {"remote": "origin", "push": False, "create_pr": False, "merge": False,
            "integration_branch": "main"},
    "roles": {
        "implementer": {"network": True, "auth_mode": "host_cli", "credential_env": [],
                        "command": ["codex", "exec", "--ignore-user-config",
                                    "--dangerously-bypass-approvals-and-sandbox",
                                    "--output-last-message", "{output}", "{prompt}"]},
        "reviewer": {"vendor": "anthropic", "model_family": "claude", "prompt_lineage": "v1",
                     "protocol_version": "buildctl-review-v2", "network": True, "auth_mode": "host_cli",
                     "credential_env": [],
                     "command": ["claude", "-p", "{prompt}", "--no-session-persistence", "--tools",
                                 "Read,Grep,Glob"],
                     "context_documents": []},
        "arbiter": {"network": False, "auth_mode": "none", "credential_env": [], "mode": "interactive",
                    "command": ["agent-debate"]},
    },
    "phase_defaults": {
        "required_files": ["docs/HANDOFF_PHASE_{phase}.md"],
        "commands": [["{python}", "-m", "unittest", "discover", "-s", "tests"]],
        "invariants": ["no_credentials_in_source"],
        "claude_review_required": True, "debate_on_architecture_disagreement": True,
        "max_remediation_attempts": 4, "merge_policy": "auto",
        "allowed_paths": ["src/", "tests/", "pyproject.toml", "docs/HANDOFF_PHASE_{phase}.md"],
    },
}


class RecordError(ValueError):
    """A request the record refuses. The message says what to fix."""


@dataclass(frozen=True)
class Identity:
    """Who is speaking. Pinned when the server or CLI starts, never taken from a tool argument."""
    name: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise RecordError(f"role must be one of {', '.join(ROLES)}")
        if not self.name.strip():
            raise RecordError("identity needs a name")

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role}


@dataclass
class State:
    repo: str = ""
    base: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    thesis: dict[str, Any] | None = None
    principles: list[dict[str, Any]] = field(default_factory=list)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    phases: list[dict[str, Any]] = field(default_factory=list)
    withdrawn: dict[str, dict[str, Any]] = field(default_factory=dict)
    milestones: dict[str, Any] | None = None
    reviews: list[dict[str, Any]] = field(default_factory=list)
    accepted_risks: dict[str, dict[str, Any]] = field(default_factory=dict)
    finalized: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    sequence: int = 0

    def current_decisions(self) -> list[dict[str, Any]]:
        return [item for item in self.decisions.values() if not item.get("superseded_by")]

    def phase(self, phase_id: str) -> dict[str, Any]:
        for item in self.phases:
            if item["id"] == phase_id:
                return item
        raise RecordError(f"unknown phase {phase_id}")

    def finding(self, finding_id: str) -> dict[str, Any] | None:
        for review in self.reviews:
            for item in review["findings"]:
                if item["id"] == finding_id:
                    return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "started_at": self.started_at, "thesis": self.thesis,
                "principles": self.principles, "decisions": list(self.decisions.values()),
                "questions": list(self.questions.values()),
                "phases": [dict(item, number=number) for number, item in enumerate(self.phases)],
                "withdrawn_phases": list(self.withdrawn.values()), "milestones": self.milestones,
                "reviews": self.reviews, "accepted_risks": list(self.accepted_risks.values()),
                "finalized": self.finalized, "sequence": self.sequence}


def state_root(repo: Path) -> Path:
    override = os.environ.get("BUILDCTL_DISCOVER_STATE_DIR")
    if override:
        return Path(override).resolve()
    xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return xdg / "buildctl-discover" / hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{what} must be non-empty text")
    return value.strip()


def _strings(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RecordError(f"{what} must be a list of non-empty strings")
    return list(value)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def normalize_acceptance(value: Any) -> dict[str, Any]:
    """Each list field is {"add": [...]} (extend the defaults) or {"set": [...]} (replace them).

    buildctl replaces a default when a phase names the key, so the record has to say which one
    the speaker meant. merge_policy is a plain value."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RecordError("acceptance must be an object")
    unknown = set(value) - {*LIST_FIELDS, "merge_policy"}
    if unknown:
        raise RecordError(f"unknown acceptance fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for name in LIST_FIELDS:
        if name not in value:
            continue
        entry = value[name]
        if not isinstance(entry, dict) or len(entry) != 1 or next(iter(entry)) not in ("add", "set"):
            raise RecordError(f"acceptance.{name} must be {{\"add\": [...]}} or {{\"set\": [...]}}")
        mode, items = next(iter(entry.items()))
        if name == "commands":
            if not isinstance(items, list) or not all(
                    isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv) for argv in items):
                raise RecordError("acceptance.commands entries must be non-empty argv arrays of strings")
            items = [list(argv) for argv in items]
        else:
            items = _strings(items, f"acceptance.{name}.{mode}")
        result[name] = {mode: items}
    if "merge_policy" in value:
        if value["merge_policy"] not in ("auto", "human"):
            raise RecordError("acceptance.merge_policy must be auto or human")
        result["merge_policy"] = value["merge_policy"]
    return result


def apply(state: State, event_type: str, payload: dict[str, Any], timestamp: str) -> None:
    """Replay one event. Validation happens before append; replay trusts the chain."""
    state.sequence += 1
    for key in ("id",):
        if key in payload:
            prefix, number = payload[key].rsplit("-", 1)
            state.counters[prefix] = max(state.counters.get(prefix, 0), int(number))
    by = payload.get("by")
    if event_type == "session_started":
        state.repo, state.base, state.started_at = payload["repo"], payload["base"], timestamp
    elif event_type == "thesis_set":
        state.thesis = {"text": payload["text"], "by": by, "at": timestamp}
    elif event_type == "principle_added":
        state.principles.append({"id": payload["id"], "text": payload["text"], "by": by, "at": timestamp})
    elif event_type == "decision_recorded":
        state.decisions[payload["id"]] = {
            "id": payload["id"], "text": payload["text"], "section": payload["section"],
            "confidence": payload["confidence"], "supersedes": payload.get("supersedes"),
            "superseded_by": None, "deferred": None, "by": by, "at": timestamp}
        if payload.get("supersedes"):
            state.decisions[payload["supersedes"]]["superseded_by"] = payload["id"]
    elif event_type == "decision_deferred":
        state.decisions[payload["id"]]["deferred"] = {"reason": payload["reason"], "by": by, "at": timestamp}
    elif event_type == "question_added":
        state.questions[payload["id"]] = {"id": payload["id"], "text": payload["text"], "status": "open",
                                          "resolution": None, "by": by, "at": timestamp}
    elif event_type == "question_resolved":
        state.questions[payload["id"]].update(status="resolved", resolution={
            "text": payload["resolution"], "decisions": payload["decisions"], "by": by, "at": timestamp})
    elif event_type == "phase_proposed":
        item = {"id": payload["id"], "name": payload["name"], "objective": payload["objective"],
                "details": payload.get("details", ""), "implements": payload["implements"],
                "acceptance": payload["acceptance"], "by": by, "at": timestamp}
        position = payload.get("position")
        state.phases.insert(len(state.phases) if position is None else position, item)
    elif event_type == "phase_revised":
        state.phase(payload["id"]).update(payload["changes"])
    elif event_type == "phase_withdrawn":
        item = state.phase(payload["id"])
        state.phases.remove(item)
        state.withdrawn[item["id"]] = dict(item, withdrawn={"reason": payload["reason"], "by": by, "at": timestamp})
    elif event_type == "phases_reordered":
        index = {item["id"]: item for item in state.phases}
        state.phases = [index[phase_id] for phase_id in payload["order"]]
    elif event_type == "milestones_set":
        state.milestones = {"milestones": payload["milestones"], "checkpoints_before": payload["checkpoints_before"],
                            "mvp_completion": payload["mvp_completion"], "by": by, "at": timestamp}
    elif event_type == "review_completed":
        state.reviews.append({"id": payload["id"], "digest": payload["digest"], "reviewer": payload["reviewer"],
                              "findings": payload["findings"], "warnings": payload.get("warnings", []),
                              "by": by, "at": timestamp})
        for finding in payload["findings"]:
            prefix, number = finding["id"].rsplit("-", 1)
            state.counters[prefix] = max(state.counters.get(prefix, 0), int(number))
    elif event_type == "risk_accepted":
        state.accepted_risks[payload["target"]] = {"target": payload["target"], "reason": payload["reason"],
                                                   "by": by, "at": timestamp}
        if payload["target"] in state.questions:
            state.questions[payload["target"]]["status"] = "accepted_risk"
    elif event_type == "finalized":
        state.finalized.append({"digest": payload["digest"], "files": payload["files"], "by": by, "at": timestamp})
    else:
        raise RecordError(f"unknown event type in record: {event_type}")


class Session:
    """One discovery session. Every mutation validates, takes the lock and appends one event."""

    def __init__(self, directory: Path, *, clock: Callable[[], str] = now):
        self.directory = directory.resolve()
        self.chain = AuditChain(self.directory)
        self.lock_path = self.directory / "session.lock"
        self.proposal_dir = self.directory / "proposal"
        self.clock = clock

    @classmethod
    def for_repo(cls, repo: Path, name: str = "default", **kwargs: Any) -> "Session":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            raise RecordError("session names use lowercase letters, digits and hyphens")
        return cls(state_root(repo) / "sessions" / name, **kwargs)

    @property
    def exists(self) -> bool:
        return self.chain.path.exists()

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def events(self) -> list[dict[str, Any]]:
        self.chain.verify()
        if not self.chain.path.exists():
            return []
        return [json.loads(line) for line in self.chain.path.read_text().splitlines()]

    def state(self) -> State:
        state = State()
        for entry in self.events():
            apply(state, entry["event_type"], entry["payload"], entry["timestamp"])
        if not state.started_at:
            raise RecordError("no discovery session here; run `buildctl-discover start` first")
        return state

    def _append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.chain.append(self.clock(), event_type, payload)

    def _next(self, state: State, prefix: str) -> str:
        number = state.counters.get(prefix, 0) + 1
        state.counters[prefix] = number
        return f"{prefix}-{number}"

    def _mutate(self, fn: Callable[[State], tuple[str, dict[str, Any], Any]]) -> Any:
        with self.lock():
            state = self.state()
            event_type, payload, result = fn(state)
            self._append(event_type, payload)
            return result

    # Session lifecycle

    def start(self, repo: Path, base: dict[str, Any], by: Identity) -> None:
        if by.role != "operator":
            raise RecordError("only the operator starts a session")
        base = {key: copy.deepcopy(value) for key, value in base.items() if key not in ROADMAP_KEYS}
        with self.lock():
            if self.exists:
                raise RecordError(f"a session already exists at {self.directory}")
            self._append("session_started", {"repo": str(repo.resolve()), "base": base, "by": by.as_dict()})

    # Requirements

    def set_thesis(self, text: Any, by: Identity) -> None:
        payload = {"text": _text(text, "thesis"), "by": by.as_dict()}
        self._mutate(lambda state: ("thesis_set", payload, None))

    def add_principle(self, text: Any, by: Identity) -> str:
        text = _text(text, "principle")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            ident = self._next(state, "P")
            return "principle_added", {"id": ident, "text": text, "by": by.as_dict()}, ident
        return self._mutate(fn)

    def record_decision(self, text: Any, by: Identity, *, section: Any = "General", confidence: Any = "high",
                        supersedes: Any = None) -> str:
        text, section = _text(text, "decision"), _text(section, "section")
        if confidence not in CONFIDENCE:
            raise RecordError(f"confidence must be one of {', '.join(CONFIDENCE)}")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            if supersedes is not None:
                old = state.decisions.get(supersedes)
                if old is None:
                    raise RecordError(f"unknown decision {supersedes}")
                if old["superseded_by"]:
                    raise RecordError(f"{supersedes} is already superseded by {old['superseded_by']}")
            ident = self._next(state, "D")
            return "decision_recorded", {"id": ident, "text": text, "section": section, "confidence": confidence,
                                         "supersedes": supersedes, "by": by.as_dict()}, ident
        return self._mutate(fn)

    def defer_decision(self, decision_id: Any, reason: Any, by: Identity) -> None:
        reason = _text(reason, "reason")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            decision = state.decisions.get(decision_id)
            if decision is None or decision["superseded_by"]:
                raise RecordError(f"{decision_id} is not a current decision")
            return "decision_deferred", {"id": decision_id, "reason": reason, "by": by.as_dict()}, None
        self._mutate(fn)

    def add_open_question(self, text: Any, by: Identity) -> str:
        text = _text(text, "question")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            ident = self._next(state, "Q")
            return "question_added", {"id": ident, "text": text, "by": by.as_dict()}, ident
        return self._mutate(fn)

    def resolve_open_question(self, question_id: Any, resolution: Any, by: Identity, *,
                              decisions: Any = ()) -> None:
        resolution, decisions = _text(resolution, "resolution"), _strings(list(decisions), "decisions")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            question = state.questions.get(question_id)
            if question is None or question["status"] != "open":
                raise RecordError(f"{question_id} is not an open question")
            missing = [item for item in decisions if item not in state.decisions]
            if missing:
                raise RecordError(f"unknown decisions: {', '.join(missing)}")
            return "question_resolved", {"id": question_id, "resolution": resolution, "decisions": decisions,
                                         "by": by.as_dict()}, None
        self._mutate(fn)

    # Roadmap

    def _check_phase_fields(self, state: State, name: str, implements: list[str], exclude: str | None) -> None:
        clash = [item["id"] for item in state.phases if item["id"] != exclude and slug(item["name"]) == slug(name)]
        if not slug(name):
            raise RecordError("phase name needs letters or digits")
        if clash:
            raise RecordError(f"phase name collides with {clash[0]}")
        missing = [item for item in implements if item not in state.decisions]
        if missing:
            raise RecordError(f"unknown decisions: {', '.join(missing)}")

    def propose_phase(self, name: Any, objective: Any, by: Identity, *, implements: Any = (),
                      acceptance: Any = None, details: Any = "", position: Any = None) -> str:
        name, objective = _text(name, "phase name"), _text(objective, "objective")
        implements, acceptance = _strings(list(implements), "implements"), normalize_acceptance(acceptance)
        if not isinstance(details, str):
            raise RecordError("details must be text")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            self._check_phase_fields(state, name, implements, None)
            if position is not None and (not isinstance(position, int) or not 0 <= position <= len(state.phases)):
                raise RecordError(f"position must be between 0 and {len(state.phases)}")
            ident = self._next(state, "PH")
            return "phase_proposed", {"id": ident, "name": name, "objective": objective, "details": details.strip(),
                                      "implements": implements, "acceptance": acceptance, "position": position,
                                      "by": by.as_dict()}, ident
        return self._mutate(fn)

    def revise_phase(self, phase_id: Any, changes: Any, by: Identity) -> None:
        if not isinstance(changes, dict) or not changes:
            raise RecordError("changes must be a non-empty object")
        unknown = set(changes) - {"name", "objective", "details", "implements", "acceptance"}
        if unknown:
            raise RecordError(f"cannot revise: {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        for key in ("name", "objective"):
            if key in changes:
                clean[key] = _text(changes[key], key)
        if "details" in changes:
            if not isinstance(changes["details"], str):
                raise RecordError("details must be text")
            clean["details"] = changes["details"].strip()
        if "implements" in changes:
            clean["implements"] = _strings(changes["implements"], "implements")
        if "acceptance" in changes:
            clean["acceptance"] = normalize_acceptance(changes["acceptance"])

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            current = state.phase(phase_id)
            self._check_phase_fields(state, clean.get("name", current["name"]),
                                     clean.get("implements", current["implements"]), phase_id)
            return "phase_revised", {"id": phase_id, "changes": clean, "by": by.as_dict()}, None
        self._mutate(fn)

    def withdraw_phase(self, phase_id: Any, reason: Any, by: Identity) -> None:
        reason = _text(reason, "reason")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            state.phase(phase_id)
            return "phase_withdrawn", {"id": phase_id, "reason": reason, "by": by.as_dict()}, None
        self._mutate(fn)

    def reorder_phases(self, order: Any, by: Identity) -> None:
        order = _strings(order, "order")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            if sorted(order) != sorted(item["id"] for item in state.phases):
                raise RecordError("order must list every current phase id exactly once")
            return "phases_reordered", {"order": order, "by": by.as_dict()}, None
        self._mutate(fn)

    def set_milestones(self, milestones: Any, checkpoints_before: Any, mvp_completion: Any, by: Identity) -> None:
        if not isinstance(milestones, dict) or not all(
                isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", key) and isinstance(value, str)
                for key, value in milestones.items()):
            raise RecordError("milestones map lowercase names to phase ids, e.g. {\"mvp\": \"PH-14\"}")
        checkpoints_before = _strings(list(checkpoints_before), "checkpoints_before")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            known = {item["id"] for item in state.phases}
            missing = [item for item in [*milestones.values(), *checkpoints_before, mvp_completion] if item not in known]
            if missing:
                raise RecordError(f"unknown phases: {', '.join(map(str, missing))}")
            return "milestones_set", {"milestones": dict(milestones), "checkpoints_before": checkpoints_before,
                                      "mvp_completion": mvp_completion, "by": by.as_dict()}, None
        self._mutate(fn)

    # Review, risk and finalization

    def record_review(self, digest: str, reviewer: dict[str, Any], findings: list[dict[str, Any]],
                      by: Identity, warnings: list[str] = ()) -> str:
        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            ident = self._next(state, "RV")
            numbered = []
            for finding in findings:
                numbered.append(dict(finding, id=self._next(state, "F")))
            return "review_completed", {"id": ident, "digest": digest, "reviewer": reviewer, "findings": numbered,
                                        "warnings": list(warnings), "by": by.as_dict()}, ident
        return self._mutate(fn)

    def accept_risk(self, target: Any, reason: Any, by: Identity) -> None:
        if by.role != "operator":
            raise RecordError("only the operator accepts a risk")
        reason = _text(reason, "reason")

        def fn(state: State) -> tuple[str, dict[str, Any], Any]:
            question = state.questions.get(target)
            if question is not None:
                if question["status"] != "open":
                    raise RecordError(f"{target} is not open")
            elif state.finding(target) is None:
                raise RecordError(f"{target} is neither an open question nor a review finding")
            return "risk_accepted", {"target": target, "reason": reason, "by": by.as_dict()}, None
        self._mutate(fn)

    def record_finalized(self, digest: str, files: list[str], by: Identity) -> None:
        self._mutate(lambda state: ("finalized", {"digest": digest, "files": files, "by": by.as_dict()}, None))
