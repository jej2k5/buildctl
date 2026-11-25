"""Read-only views of one phase for the console: its step track and its documentation.

Everything here is computed from what the kernel recorded: operations in state, artifacts in the
phase folder, audit history and git objects. Nothing is derived that the supervisor did not write,
and nothing mutates anything. The web console and the TUI render these the same way.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import documentation
from .governance import governance_documents
from .levers import WAITING_CODES
from .safety import ensure_safe_relative

STEP_NAMES = ("Implement", "Gates", "Docs", "Review", "Commit")
# Stops that belong to one step whatever ran last.
STOP_STEPS = {"DOC_SCOPE_VIOLATION": "Docs", "DOCUMENTATION_ATTEMPTS_EXHAUSTED": "Docs",
              "REVIEW_BLOCKED": "Review", "UNREVIEWABLE_DELTA": "Review",
              "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED": "Review", "REVIEW_FORMAT_ATTEMPTS_EXHAUSTED": "Review",
              "DELTA_CHANGED_AFTER_REVIEW": "Commit"}
STOP_WORDS = {"DOC_SCOPE_VIOLATION": "stopped: edited files outside the docs",
              "DOCUMENTATION_ATTEMPTS_EXHAUSTED": "stopped: send-back limit"}
DIFF_LINE_LIMIT = 400


def step_of(operation_id: str) -> str | None:
    """Which step an operation belongs to, from its id (`phase-N:<kind>:<attempt>`)."""
    kind = operation_id.split(":")[1] if operation_id.count(":") >= 2 else operation_id
    if kind.startswith(("document-", "doc-gate-")):
        return "Docs"
    if kind.startswith("gate"):
        return "Gates"
    if kind.startswith("review-"):
        return "Review"
    if kind.startswith(("implement", "remediate-", "adr-")):
        return "Implement"
    return None  # debate turns and the like sit between steps


def _seconds(operation: dict[str, Any]) -> float:
    try:
        start = datetime.fromisoformat(operation["started_at"])
        end = datetime.fromisoformat(operation["completed_at"]) if operation.get("completed_at") else None
    except (KeyError, TypeError, ValueError):
        return 0.0
    return (end - start).total_seconds() if end else 0.0


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _label_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.stem))


def _outputs(items: list[dict[str, Any]], marker: str) -> list[Path]:
    """The output files of this attempt's operations of one kind, in the order they ran.

    Artifact names do not carry the attempt (DOC_RESULT-1-0, REVIEW_RESULT-3), so a glob can pick up
    a stale file from an earlier attempt. The operation records the exact file it wrote."""
    ordered = sorted((op for op in items if marker in op.get("id", "")), key=lambda op: str(op.get("started_at") or ""))
    return [Path(op["output_path"]) for op in ordered if op.get("output_path")]


def phase_steps(state: Any, number: int, operations: list[dict[str, Any]], config: dict[str, Any] | None,
                lock_held: bool, control_root: Path) -> dict[str, Any]:
    """The step track for a phase's latest attempt: one entry per step, in order."""
    config = config or {}
    key = str(number)
    attempt = state.phase_attempts.get(key, 0)
    ops = [op for op in operations if op.get("id", "").endswith(f":{attempt}")]
    ops.sort(key=lambda op: str(op.get("started_at") or ""))
    phase_item = next((item for item in config.get("phases", []) if item.get("number") == number), {})
    build_docs = documentation.enabled(config)
    phase_docs = build_docs and phase_item.get("documentation", True)
    names = [name for name in STEP_NAMES if name != "Docs" or build_docs]
    by_step: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for op in ops:
        step = step_of(op.get("id", ""))
        if step in by_step:
            by_step[step].append(op)
    completed = number in state.completed_phases
    current = state.current_phase == number
    committed = completed or (current and state.phase_step == "committed")
    stopped = current and state.status in {"human_escalation", "failed"}
    code = state.escalation_code if stopped else None

    latest = next((step_of(op["id"]) for op in reversed(ops) if step_of(op.get("id", "")) in by_step), None)
    position = STOP_STEPS.get(code, latest) if code in STOP_STEPS else latest
    if committed:
        position = "Commit"
    folder = control_root / "artifacts" / f"phase-{number}"
    steps = []
    reached = position is not None
    for name in names:
        items = by_step[name]
        step: dict[str, Any] = {"name": name, "aspect": "pending", "meta": ""}
        if name == "Docs" and not phase_docs:
            step.update(aspect="pending", meta="off for this phase", skipped=True)
            steps.append(step)
            continue
        before_position = reached and names.index(name) < names.index(position) if position in names else False
        at_position = name == position
        if name == "Commit":
            if committed:
                step.update(aspect="clear", meta=_commit_meta(state, number))
                if current and state.pending_approval and state.pending_approval.checkpoint.startswith("merge-"):
                    step.update(aspect="caution", meta="waiting for your merge approval")
            elif code == "DELTA_CHANGED_AFTER_REVIEW":
                step.update(aspect="danger", meta="stopped: changed after review")
            steps.append(step)
            continue
        last = items[-1] if items else None
        if committed or before_position:
            step["aspect"] = "clear" if items else "pending"
        elif at_position and last:
            status = last.get("status")
            if status == "started":
                step["aspect"] = "occupied" if lock_held else "unknown"
            elif status == "unknown":
                step["aspect"] = "unknown"
            elif stopped:
                step["aspect"] = "caution" if code in WAITING_CODES else "danger"
            else:
                step["aspect"] = "clear" if status == "completed" else "danger"
        elif at_position and code in STOP_STEPS:
            step["aspect"] = "danger"
        step["meta"] = _step_meta(name, items, folder, config, attempt, step["aspect"])
        if at_position and code in STOP_WORDS:
            step.update(aspect="danger", meta=STOP_WORDS[code])
        elif at_position and stopped and step["aspect"] == "danger" and not step["meta"].startswith("stopped"):
            step["meta"] = (step["meta"] + " · " if step["meta"] else "") + "stopped here"
        elif at_position and stopped and step["aspect"] == "caution":
            step["meta"] = (step["meta"] + " · " if step["meta"] else "") + "waiting for you"
        steps.append(step)
    mode = documentation.mode(config) if build_docs else None
    return {"attempt": attempt, "documentation": None if not build_docs else (mode if phase_docs else "off"),
            "steps": steps}


def _commit_meta(state: Any, number: int) -> str:
    for event in reversed(state.history):
        if event.get("event") == "evidence_sealed" and event.get("phase") == number and event.get("code_commit"):
            return str(event["code_commit"])[:7]
    return "committed"


def _step_meta(name: str, items: list[dict[str, Any]], folder: Path, config: dict[str, Any], attempt: int,
               aspect: str) -> str:
    if not items:
        return ""
    if aspect == "occupied":
        running = "in flight"
    else:
        running = ""
    if name == "Implement":
        fixes = sum(1 for op in items if not op["id"].split(":")[1].startswith("implement"))
        parts = [running or _duration(_seconds(items[0]))]
        if fixes:
            parts.append(f"{fixes} fix{'es' if fixes > 1 else ''}")
        return " · ".join(parts)
    if name == "Gates":
        laps = len(items)
        return " · ".join([running or _duration(_seconds(items[-1]))] + ([f"{laps} laps"] if laps > 1 else []))
    if name == "Review":
        reviews = len(items)
        files = _outputs(items, ":review-")
        verdict = _json(files[-1]).get("verdict") if files and not running else None
        return " · ".join([running or _duration(_seconds(items[-1]))] + ([verdict] if verdict else [])
                          + ([f"{reviews} reviews"] if reviews > 1 else []))
    # Docs: the documenter's passes and what its gates said.
    passes = [op for op in items if op["id"].split(":")[1].startswith("document-")]
    limit = documentation.max_attempts(config) + 1
    parts = []
    if running:
        parts.append(f"pass {len(passes)} of {limit}" if passes else "in flight")
    else:
        parts.append(_duration(sum(_seconds(op) for op in items)))
        gates = _outputs(items, ":doc-gate-")
        result = _outputs(items, ":document-")
        latest_gate = _json(gates[-1]) if gates else {}
        failures = (latest_gate.get("documentation") or {}).get("failures") or []
        if latest_gate and not latest_gate.get("passed") and failures and documentation.mode(config) == "optional":
            parts.append(f"{len(failures)} gate{'s' if len(failures) > 1 else ''} failed · recorded")
        elif result:
            changes = _json(result[-1]).get("changes") or []
            parts.append(f"{len(changes)} file{'s' if len(changes) != 1 else ''}" if changes else "no change")
    if len(passes) > 1:
        parts.append(f"{len(passes) - 1} send-back{'s' if len(passes) > 2 else ''}")
    return " · ".join(parts)


def documentation_summary(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """The build's documentation settings, for Setup. None when documentation is off."""
    config = config or {}
    if not documentation.enabled(config):
        return None
    settings = documentation.section(config)
    roles = config.get("roles", {})
    documenter, reviewer = roles.get("documenter", {}), roles.get("reviewer", {})
    globs = documentation.patterns(config)
    return {"mode": documentation.mode(config), "paths": list(globs),
            "governed_inside": [path for path in governance_documents(config) if documentation.matches(path, globs)],
            "changelog": settings.get("changelog"), "unreleased_heading": settings.get("unreleased_heading", "Unreleased"),
            "forbidden_phrases": settings.get("forbidden_phrases", []),
            "max_attempts": documentation.max_attempts(config), "brief": settings.get("brief") or documentation.DEFAULT_BRIEF,
            "documenter": {key: documenter.get(key) for key in ("vendor", "model_family", "prompt_lineage", "derived_from")},
            "reviewer": {key: reviewer.get(key) for key in ("vendor", "model_family")},
            "warnings": documentation.warnings(config)}


def documentation_detail(repo: Path, control_root: Path, state: Any, number: int, config: dict[str, Any] | None,
                         runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Everything the Docs tab shows for one phase: the documenter's results, its gates, and diffs."""
    config = config or {}
    attempt = state.phase_attempts.get(str(number), 0)
    items = [{"id": key, "started_at": op.started_at, "output_path": op.output_path}
             for key, op in state.operations.items() if op.phase == number and key.endswith(f":{attempt}")]
    results = [{"file": path.name, **_json(path)} for path in _outputs(items, ":document-") if path.is_file()]
    gates = [{"file": path.name, **{k: v for k, v in _json(path).items() if k in {"passed", "acceptance", "documentation"}}}
             for path in _outputs(items, ":doc-gate-") if path.is_file()]
    out_of_scope: list[str] = []
    for event in reversed(state.history):
        if event.get("event") == "documentation_scope_violation" and event.get("phase") == number:
            out_of_scope = [str(path) for path in event.get("paths") or []]
            break
    if out_of_scope and not (state.current_phase == number and state.escalation_code == "DOC_SCOPE_VIOLATION"):
        out_of_scope = []  # a later attempt superseded that stop
    reported = [change.get("path") for change in (results[-1].get("changes") or [])] if results else []
    diffs = {path: _diff(repo, state, number, path, runner) for path in dict.fromkeys([*out_of_scope, *reported]) if path}
    return {"phase": number, "summary": documentation_summary(config), "results": results, "gates": gates,
            "out_of_scope": out_of_scope, "diffs": diffs}


def _git_bytes(repo: Path, revision: str, path: str, runner: Callable[..., Any]) -> bytes | None:
    shown = runner(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "show", f"{revision}:{path}"],
                   capture_output=True, env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null",
                                             "GIT_CONFIG_NOSYSTEM": "1"})
    return shown.stdout if shown.returncode == 0 else None


def _diff(repo: Path, state: Any, number: int, path: str, runner: Callable[..., Any]) -> dict[str, Any]:
    """One file's change in this phase: against the attempt's base while it runs, the commit's parent after."""
    try:
        # The path comes from the documenter's DOC_RESULT: never let it name a file outside the repository.
        ensure_safe_relative(repo, path)
    except ValueError:
        return {"available": False, "reason": "not a path inside the repository"}
    before = after = None
    if state.current_phase == number and state.workspace and state.phase_step != "committed":
        before = _git_bytes(repo, state.base_sha or "HEAD", path, runner)
        target = ensure_safe_relative(Path(state.workspace), path)
        try:
            after = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        except OSError:
            after = None
        source = "workspace"
    else:
        commit = _commit_of(repo, state, number, runner)
        if not commit:
            return {"available": False, "reason": "no commit recorded for this phase"}
        before, after = _git_bytes(repo, f"{commit}^", path, runner), _git_bytes(repo, commit, path, runner)
        source = commit[:7]
    try:
        old = (before or b"").decode("utf-8").splitlines(keepends=True)
        new = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return {"available": False, "reason": "binary file"}
    lines = list(difflib.unified_diff(old, new, n=2, lineterm="\n"))[2:]
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    return {"available": True, "source": source, "added": added, "removed": removed,
            "lines": [line.rstrip("\n") for line in lines[:DIFF_LINE_LIMIT]],
            "truncated": max(0, len(lines) - DIFF_LINE_LIMIT)}


def _commit_of(repo: Path, state: Any, number: int, runner: Callable[..., Any]) -> str | None:
    for event in reversed(state.history):
        if event.get("event") == "evidence_sealed" and event.get("phase") == number and event.get("code_commit"):
            return str(event["code_commit"])
    resolved = runner(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/build/phase-{number}"],
                      capture_output=True, text=True)
    return resolved.stdout.strip() or None if resolved.returncode == 0 else None
