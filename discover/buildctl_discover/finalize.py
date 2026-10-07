"""Copy a passing proposal into the working tree. Never commits, never trusts.

Finalize is the one step that writes into the governed repository, so it is CLI-only and
takes a typed confirmation: a prefix of the proposal digest, the same shape as
`buildctl trust init --confirm`. An agent calling it over MCP would be ceremony as theater.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from buildctl.buildctl import control_root_for
from buildctl.supervisor.safety import ensure_safe_relative
from buildctl.supervisor.state import StateStore, SupervisorLocked

from .check import check
from .record import Identity, RecordError, Session
from .render import read_proposal, render

MIN_CONFIRM = 8


def _buildctl_idle(repo: Path) -> None:
    control = control_root_for(repo)
    if not control.exists():
        return  # buildctl has never run here
    try:
        with StateStore(control).lock():
            pass
    except SupervisorLocked as exc:
        raise RecordError("buildctl holds its lock. Wait for it, or pause the build, then finalize.") from exc
    state = control / "state.json"
    if state.exists():
        try:
            status = json.loads(state.read_text()).get("status")
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordError(f"cannot read buildctl state: {exc}") from exc
        if status == "running":
            raise RecordError("buildctl records a running build. Pause it before changing the roadmap.")


def finalize(session: Session, repo: Path, confirm: str, by: Identity, *,
             runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    if by.role != "operator":
        raise RecordError("only the operator finalizes")
    state = session.state()
    result = check(state, repo, read_proposal(session.proposal_dir))
    if not result["passed"]:
        raise RecordError(f"check fails with {len(result['failures'])} problem(s); run `buildctl-discover check`")
    digest = result["digest"]
    if len(confirm) < MIN_CONFIRM or not digest.startswith(confirm):
        raise RecordError(f"--confirm must be at least the first {MIN_CONFIRM} characters of the proposal digest "
                          "that `buildctl-discover status` shows")
    _buildctl_idle(repo)
    files = render(state)
    dirty = runner(["git", "-C", str(repo), "status", "--porcelain", "--", *files], text=True, capture_output=True)
    if dirty.returncode:
        raise RecordError(f"git status failed: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise RecordError("these paths have uncommitted changes; commit or discard them first:\n" + dirty.stdout.rstrip())
    for path, content in files.items():
        target = ensure_safe_relative(repo, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    prompts = repo / "prompts/phases"
    orphans = sorted(path.relative_to(repo).as_posix() for path in prompts.glob("*.md")
                     if path.relative_to(repo).as_posix() not in files) if prompts.is_dir() else []
    session.record_finalized(digest, sorted(files), by)
    return {"digest": digest, "files": sorted(files), "orphans": orphans,
            "next": ["Review the change: git diff", "Commit it",
                     "Pin it: buildctl trust init --to <commit> --confirm <sha prefix>"]}
