"""Release drafts in the console: read buildctl-drafts' files, and pull its three commands.

Reading needs nothing from the drafts package: every draft is a JSON record under
`control_root/drafts/<run>/`, its state is the last event in `events.jsonl`, and its cost is in
`costs.jsonl`. Approving, discarding and making all run the `buildctl-drafts` CLI, so the console
and the terminal cannot disagree. Approve and discard record an event; nothing here publishes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TYPES = ("release-notes", "positioning", "launch-post", "demo-script", "social")
TITLES = {"release-notes": "Release notes", "positioning": "Positioning", "launch-post": "Launch post",
          "demo-script": "Demo script", "social": "Social post"}
RUN = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
DIGEST_PREFIX = 7         # characters of a draft's digest the operator types to sign it
COST_PER_DRAFT = 2.0      # buildctl-drafts' default booking per writer call
REWRITE_FACTOR = 2        # each draft may be rewritten once, so a run can book twice its estimate
OPERATOR_VOICE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "buildctl" / "voice.md"


def installed() -> bool:
    return importlib.util.find_spec("buildctl_drafts") is not None


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def spent_today(root: Path) -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    return round(sum(float(item.get("cost_usd", 0)) for item in _lines(root / "costs.jsonl")
                     if str(item.get("at", "")).startswith(today)), 2)


def _draft(path: Path, states: dict[str, dict[str, Any]], costs: dict[str, float]) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        record = json.loads(raw)
    except (OSError, ValueError):
        return None
    identity = f"{path.parent.name}/{path.stem}"
    checks = record.get("checks") or {}
    draft = record.get("draft") or {}
    unbacked = {item.get("claim"): item.get("why") for item in checks.get("unbacked") or []}
    claims = []
    for number, claim in enumerate(draft.get("claims") or [], 1):
        source = claim.get("source") or {} if isinstance(claim, dict) else {}
        claims.append({"n": number, "text": str(claim.get("text", "")) if isinstance(claim, dict) else "",
                       "kind": source.get("kind"), "ref": source.get("ref"), "line": source.get("line"),
                       "quote": source.get("quote"), "why": unbacked.get(number)})
    event = states.get(identity) or {}
    passed = bool(checks.get("passed"))
    return {"id": identity, "digest": hashlib.sha256(raw).hexdigest(), "kind": record.get("kind") or path.stem,
            "title": TITLES.get(record.get("kind") or path.stem, path.stem),
            "state": event.get("event", "proposed"), "decided_at": event.get("at"),
            "verdict": "sourced" if passed else "needs you",
            "heading": draft.get("title") or record.get("title") or "", "body": draft.get("body") or "",
            "fields": draft.get("fields") or {}, "known_limits": draft.get("known_limits") or [],
            "claims": claims, "backed": len(claims) - len(unbacked), "contract": checks.get("contract") or [],
            "lint": checks.get("lint") or [], "attempts": record.get("attempts"), "voice": record.get("voice"),
            "label": record.get("label"), "until": record.get("until"), "since": record.get("since"),
            "cost": costs.get(identity, 0.0)}


def waiting(control_root: Path) -> int:
    """Drafts no one has approved or discarded yet: the rail's flag. Reads file names only."""
    root = control_root / "drafts"
    decided = {item.get("draft") for item in _lines(root / "events.jsonl")}
    return sum(1 for path in root.glob("*/*.json") if RUN.match(path.parent.name) and ".result-" not in path.name
               and f"{path.parent.name}/{path.stem}" not in decided)


def view(control_root: Path, model: dict[str, Any]) -> dict[str, Any]:
    """Every run, newest first, and what a new run needs."""
    root = control_root / "drafts"
    states = {item["draft"]: item for item in _lines(root / "events.jsonl") if "draft" in item}
    costs: dict[str, float] = {}
    for item in _lines(root / "costs.jsonl"):
        key = str(item.get("draft", ""))
        costs[key] = round(costs.get(key, 0.0) + float(item.get("cost_usd", 0)), 2)
    runs = []
    for directory in sorted((path for path in root.glob("*") if path.is_dir() and RUN.match(path.name)), reverse=True):
        drafts = [draft for path in sorted(directory.glob("*.json"))
                  if ".result-" not in path.name and (draft := _draft(path, states, costs))]
        if not drafts:
            continue
        drafts.sort(key=lambda item: TYPES.index(item["kind"]) if item["kind"] in TYPES else 99)
        first = drafts[0]
        runs.append({"id": directory.name, "label": first["label"], "until": first["until"],
                     "since": first["since"], "drafts": drafts, "cost": round(sum(item["cost"] for item in drafts), 2),
                     "at": datetime.strptime(directory.name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).isoformat()})
    return {"installed": installed(), "runs": runs, "waiting": sum(1 for run in runs for item in run["drafts"]
                                                               if item["state"] == "proposed"),
            "lever": lever(control_root, model, root), "operator_voice": OPERATOR_VOICE.is_file()}


def lever(control_root: Path, model: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """The paid 'Write drafts' lever and the conditions buildctl-drafts itself checks before it spends."""
    root = root or control_root / "drafts"
    budget = model.get("budget") or {}
    limit = float(budget.get("limit") or 0)
    build = float(budget.get("spent_today") or 0)
    drafts = spent_today(root)
    kinds = [kind for kind in TYPES if kind != "social" or OPERATOR_VOICE.is_file()]
    need = COST_PER_DRAFT * len(kinds) * REWRITE_FACTOR
    left = round(limit - build - drafts, 2)
    trust = model.get("trust") or {}
    trusted = bool(trust.get("ok")) or "supervisor kernel changed" in str(trust.get("error") or "")
    isolation = model.get("isolation") or {}
    milestones = (model.get("config") or {}).get("milestones") or {}
    completed = set((model.get("state") or {}).get("completed_phases") or [])
    reached = sorted(name for name, number in milestones.items() if int(number) in completed)
    conditions = [
        {"label": "buildctl-drafts is installed", "held": installed(),
         "detail": "" if installed() else "pip install -e drafts/ in the console's Python"},
        # Drafts read the trusted snapshot without the running-kernel check, as buildctl-drafts does.
        {"label": "Build is trusted", "held": trusted,
         "detail": (trust.get("snapshot_digest") or "")[:12] if trust.get("ok")
         else "kernel changed since trust init; drafts still read the snapshot" if trusted
         else (trust.get("error") or "run buildctl trust init")},
        {"label": "Sandbox checked in the last 24 hours", "held": bool(isolation.get("fresh")),
         "detail": isolation.get("error") or (f"{isolation['age_hours']:.0f}h ago" if isolation.get("age_hours") is not None else "")},
        {"label": "A milestone is complete", "held": bool(reached),
         "detail": ", ".join(reached) if reached else "no milestone's phase has finished"},
        {"label": "Fits today's cap", "held": budget.get("ok", False) and need <= left,
         "detail": f"up to ${need:.2f} needed · ${max(left, 0):.2f} left of ${limit:.2f} "
                   f"(build ${build:.2f}, drafts ${drafts:.2f})"},
    ]
    return {"enabled": all(item["held"] for item in conditions), "conditions": conditions, "milestones": reached,
            "kinds": kinds, "estimate": need, "spent": {"build": build, "drafts": drafts, "limit": limit}}
