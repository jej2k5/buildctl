from __future__ import annotations

import itertools
import json
import tempfile
from pathlib import Path
from typing import Any

from buildctl_discover.record import LIST_FIELDS, Identity, Session

REPO = Path(__file__).resolve().parents[2]
RELAY = REPO / "tests/fixtures/target"
OPERATOR = Identity("Ada", "operator")
OWNER = Identity("Sam", "owner")


def clock():
    """Deterministic timestamps, one second apart."""
    counter = itertools.count()
    def tick() -> str:
        number = next(counter)
        return f"2026-10-07T{12 + number // 3600:02d}:{number // 60 % 60:02d}:{number % 60:02d}+00:00"
    return tick


def relay_config() -> dict[str, Any]:
    return json.loads((RELAY / "build.yaml").read_text())


def session(base: dict[str, Any] | None = None, repo: Path = RELAY) -> tuple[tempfile.TemporaryDirectory, Session]:
    temporary = tempfile.TemporaryDirectory()
    result = Session(Path(temporary.name) / "session", clock=clock())
    result.start(repo, relay_config() if base is None else base, OPERATOR)
    return temporary, result


def as_record_acceptance(acceptance: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {name: {"set": acceptance[name]} for name in LIST_FIELDS if name in acceptance}
    if "merge_policy" in acceptance:
        result["merge_policy"] = acceptance["merge_policy"]
    return result


def relay_session() -> tuple[tempfile.TemporaryDirectory, Session, list[str]]:
    """Relay's whole roadmap, expressed through the record."""
    temporary, result = session()
    result.set_thesis((RELAY / "docs/PRODUCT_THESIS.md").read_text().split("\n", 2)[2].strip(), OWNER)
    decision = result.record_decision("Relay is a self-hosted webhook delivery service.", OWNER, section="Core")
    ids = []
    for phase in relay_config()["phases"]:
        ids.append(result.propose_phase(phase["name"], f"Build {phase['name'].lower()}.", OWNER,
                                        implements=[decision],
                                        acceptance=as_record_acceptance(phase.get("acceptance", {}))))
    marks = relay_config()
    result.set_milestones({name: ids[number] for name, number in marks["milestones"].items()},
                          [ids[number] for number in marks["checkpoints"]["before_phases"]],
                          ids[marks["checkpoints"]["mvp_completion"]], OPERATOR)
    return temporary, result, ids
