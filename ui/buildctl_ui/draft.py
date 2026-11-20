"""A discovery draft, read-only: the roadmap buildctl-discover is building, before anyone finalizes it.

`buildctl-ui --session <name>` opens one. Everything comes from the `buildctl-discover` CLI (status and
check) and the session's own files (the rendered proposal and the session audit log). Nothing here
writes: finalizing a draft stays the operator's act in a terminal.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .actions import Refused, read_plan

FINDING = re.compile(r"^review \[(F-\d+)[^\]]*\]: F-\d+ \((CRITICAL|HIGH|MEDIUM|LOW), ([^)]*)\): (.+)$")
QUESTION = re.compile(r"^open_question \[(Q-\d+)\]: Q-\d+ is still open: (.+?)(?: Resolve it.*)?$")
FAILURE = re.compile(r"^([a-z_]+)(?: \[[^\]]*\])?: (.+)$")


def event_line(event: dict[str, Any]) -> str:
    """One plain line for a session audit event: what the interviewer recorded."""
    kind, p = event.get("event_type", ""), event.get("payload") or {}
    lines = {
        "session_started": lambda: "Session opened.",
        "thesis_set": lambda: f"Thesis: {p.get('thesis') or p.get('text', '')}",
        "principle_added": lambda: f"Principle: {p.get('text', '')}",
        "decision_recorded": lambda: f"{p.get('id')}: {p.get('text', '')}",
        "decision_deferred": lambda: f"{p.get('id')} deferred: {p.get('reason', '')}",
        "question_added": lambda: f"Your call {p.get('id')}: {p.get('text', '')}",
        "question_resolved": lambda: f"{p.get('id')} resolved: {p.get('resolution', '')}",
        "phase_proposed": lambda: f"Phase {p.get('name')}" + (f": {p['outcome']}" if p.get("outcome") else ""),
        "phase_revised": lambda: f"Phase {p.get('id')} revised ({', '.join(p.get('changes', {}))}).",
        "phase_withdrawn": lambda: f"Phase {p.get('id')} withdrawn: {p.get('reason', '')}",
        "phases_reordered": lambda: "Phases reordered.",
        "milestones_set": lambda: "Milestones and checkpoints set.",
        "review_completed": lambda: f"Review {p.get('id')}: {len(p.get('findings', []))} findings.",
        "risk_accepted": lambda: f"Risk accepted: {p.get('target')}.",
    }
    return lines.get(kind, lambda: kind.replace("_", " ").capitalize() + ".")()


@dataclass
class Draft:
    repo: Path
    session: str
    demo: str | None = None

    @classmethod
    def open(cls, repo: Path, session: str) -> "Draft":
        draft = cls(repo.resolve(), session)
        if "proposal" not in draft._status():
            raise ValueError(f"no discovery session '{session}' for {draft.repo}")
        return draft

    def _discover(self, *args: str) -> str:
        result = subprocess.run(["buildctl-discover", "--repo", str(self.repo), "--session", self.session, *args],
                                capture_output=True, text=True)
        return result.stdout

    def _status(self) -> dict[str, str]:
        found = {}
        for line in self._discover("status").splitlines():
            key, _, value = line.partition(" ")
            if key:
                found[key] = value.strip()
        return found

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    def model(self) -> dict[str, Any]:
        status = self._status()
        proposal = Path(status.get("proposal", "."))
        session_dir = Path(status.get("session", "."))

        def read(path: str) -> str:
            target = proposal / path
            return target.read_text() if target.is_file() else ""
        try:
            config = json.loads(read("build.yaml") or "{}")
        except ValueError:
            config = {}
        check_lines = self._discover("check").splitlines()
        findings, questions, failures = [], [], []
        for line in check_lines:
            if (m := FINDING.match(line)):
                findings.append({"id": m.group(1), "severity": m.group(2), "target": m.group(3), "text": m.group(4)})
            elif (m := QUESTION.match(line)):
                questions.append({"id": m.group(1), "text": m.group(2)})
            elif (m := FAILURE.match(line)) and not line.startswith("digest "):
                failures.append({"rule": m.group(1), "text": m.group(2)})
        passes = any(line.startswith("digest ") and line.endswith(": passes") for line in check_lines)
        events = []
        try:
            for raw in (session_dir / "audit.jsonl").read_text().splitlines()[-200:]:
                event = json.loads(raw)
                events.append({"at": event.get("timestamp"), "text": event_line(event)})
        except (OSError, ValueError):
            pass
        checkpoints = config.get("checkpoints") or {"before_phases": [], "mvp_completion": None}
        milestones = config.get("milestones") or {}
        before = set(checkpoints.get("before_phases") or [])
        phases = [{"number": p["number"], "name": p.get("name", ""), "aspect": "pending",
                   "checkpoint": p["number"] in before, "milestones": [k for k, v in milestones.items() if v == p["number"]],
                   "attempts": 0, "reviews": 0, "remediations": 0, "debates": 0, "cost_usd": 0, "operations": [],
                   "merge_policy": (p.get("acceptance") or {}).get("merge_policy", "auto")}
                  for p in config.get("phases") or []]
        limits = config.get("limits") or {}
        return {
            "draft": {"session": self.session, "digest": status.get("digest"), "passes": passes,
                      "questions": questions, "findings": findings, "failures": failures,
                      "events": events[-60:], "thesis": read("docs/PRODUCT_THESIS.md").strip()},
            "plan": read_plan(read("docs/ROADMAP.md"), read("docs/DISCOVERY_DECISIONS.md"),
                              read("docs/DISCOVERY_RISKS.md"), status.get("digest")),
            "phases": phases,
            "config": {"checkpoints": checkpoints, "milestones": milestones, "limits": limits, "source": "discovery draft"},
            "state": {"completed_phases": [], "current_phase": None, "status": "draft", "generation": len(events),
                      "phase_step": "pending", "pending_approval": None},
            "repo": {"path": str(self.repo), "branch": self._git("branch", "--show-current"),
                     "head_sha": self._git("rev-parse", "HEAD")},
            "escalation": None, "levers": [], "bootstrapped": True, "demo": None, "jobs": [],
            "trust": {"ok": True}, "audit": {"ok": True, "sequence": 0}, "lock": {"held": False},
            "budget": {"spent_today": 0, "limit": limits.get("max_daily_cost_usd") or 0},
            "isolation": {"fresh": True, "verified_at": None}, "current": None, "findings": {"open": []},
            "control_root": str(session_dir), "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # The console's other reads have nothing to show for a draft.
    def audit(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return []

    def read_artifact(self, path: str, limit: int = 200_000) -> str:
        raise Refused("a discovery draft has no artifacts")

    def activity(self, phase: int, operation: str | None = None) -> dict[str, Any]:
        return {}

    def record(self, phase: int, record: str, seq: int) -> list[dict[str, Any]]:
        return []

    def trust_diff(self) -> dict[str, Any]:
        return {}

    def pull(self, *args: Any, **kwargs: Any) -> Any:
        raise Refused("a discovery draft has no levers; finalize it from a terminal with buildctl-discover")
