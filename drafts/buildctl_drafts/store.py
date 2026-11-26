"""Where drafts live: the build's state folder, never the repository. Plus their cost ledger and events.

Approving or discarding a draft records an event and nothing else. Nothing here posts anywhere.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from buildctl.supervisor.budget import BudgetLedger, LimitExceeded

DEFAULT_COST_USD = 2.0
OPERATION = "release_draft"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreError(RuntimeError):
    pass


class DraftStore:
    def __init__(self, control_root: Path):
        self.root = control_root / "drafts"

    # --- cost ---------------------------------------------------------------------------------
    def _ledger(self) -> list[dict[str, Any]]:
        path = self.root / "costs.jsonl"
        if not path.exists():
            return []
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except ValueError as exc:
            raise StoreError("the drafts cost ledger is corrupt; refusing paid work") from exc

    def spent_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(float(item["cost_usd"]) for item in self._ledger() if str(item.get("at", "")).startswith(today))

    def ensure_budget(self, control_root: Path, limit: float, estimate: float) -> None:
        """Refuse when the build's spend today, plus drafts today, plus this estimate, passes the cap."""
        try:
            build = BudgetLedger(control_root, limit).spent_today()
        except LimitExceeded as exc:
            raise StoreError(str(exc)) from exc
        total = build + self.spent_today() + estimate
        if total > limit:
            raise StoreError(f"drafts would bring today's spend to ${total:.2f}, past the ${limit:.2f} daily limit "
                             f"(build ${build:.2f}, drafts ${self.spent_today():.2f}, this run up to ${estimate:.2f})")

    def reserve(self, draft: str, cost: float) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "costs.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": now(), "operation": OPERATION, "draft": draft, "cost_usd": cost},
                                    sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    # --- drafts -------------------------------------------------------------------------------
    def new_run(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run, number = stamp, 1
        while (self.root / run).exists():  # two runs in one second
            number += 1
            run = f"{stamp}-{number}"
        (self.root / run).mkdir(mode=0o700)
        return run

    def save(self, run: str, kind: str, record: dict[str, Any], markdown: str) -> str:
        directory = self.root / run
        (directory / f"{kind}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        (directory / f"{kind}.md").write_text(markdown)
        return f"{run}/{kind}"

    def all(self) -> list[dict[str, Any]]:
        states = self.states()
        result = []
        for path in sorted(self.root.glob("*/*.json")):
            if not re.fullmatch(r"\d{8}-\d{6}(-\d+)?", path.parent.name) or ".result-" in path.name:
                continue
            record = json.loads(path.read_text())
            identity = f"{path.parent.name}/{path.stem}"
            result.append(dict(record, id=identity, state=states.get(identity, "proposed"),
                               markdown=str(path.with_suffix(".md"))))
        return result

    def get(self, identity: str) -> dict[str, Any]:
        # A bare type ("release-notes") means its newest draft.
        match = next((item for item in reversed(self.all()) if item["id"] == identity
                      or ("/" not in identity and item["id"].endswith("/" + identity))), None)
        if not match:
            raise StoreError(f"no draft {identity}; run buildctl-drafts list")
        return match

    # --- events -------------------------------------------------------------------------------
    def states(self) -> dict[str, str]:
        path = self.root / "events.jsonl"
        result: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    event = json.loads(line)
                    result[event["draft"]] = event["event"]
        return result

    def record(self, identity: str, event: str) -> None:
        if event not in {"approved", "discarded"}:
            raise StoreError("a draft is approved or discarded")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": now(), "event": event, "draft": identity}, sort_keys=True) + "\n")


def render(kind: str, title: str, value: dict[str, Any], report: dict[str, Any], meta: dict[str, Any]) -> str:
    """The draft as Markdown, with its claims ledger and what the checks found, for the operator."""
    lines = [f"<!-- buildctl-drafts {kind} · {meta['label']} · {meta['until'][:12]} · not published -->", ""]
    lines += [f"# {value.get('title') or title}", "", _body(value.get("body", "")), ""]
    fields = value.get("fields") or {}
    if kind == "positioning" and fields:
        lines += ["## Repository", "", f"- Description: {fields.get('description', '')}",
                  f"- Topics: {', '.join(fields.get('topics') or [])}", ""]
    if value.get("known_limits") and not re.search(r"(?im)^#{1,6}\s+known limits\s*$", value.get("body", "")):
        lines += ["## Known limits", ""] + [f"- {item}" for item in value["known_limits"]] + [""]
    unbacked = {item["claim"]: item["why"] for item in report.get("unbacked", [])}
    lines += ["## Claims ledger", "", "| # | Claim | Source | Check |", "|---|---|---|---|"]
    for number, claim in enumerate(value.get("claims") or [], 1):
        source = claim.get("source", {})
        where = source.get("ref", "")
        if source.get("line"):
            where += f":{source['line']}"
        check = f"UNBACKED: {unbacked[number]}" if number in unbacked else "backed"
        lines.append(f"| {number} | {_cell(claim.get('text', ''))} | {source.get('kind', '')} `{_cell(where)}` | {_cell(check)} |")
    if report.get("lint"):
        lines += ["", "## Voice", ""] + [f"- {item}" for item in report["lint"]]
    if report.get("contract"):
        lines += ["", "## Contract", ""] + [f"- {item}" for item in report["contract"]]
    return "\n".join(lines) + "\n"


def _body(text: str) -> str:
    """The body without a leading `#` title of its own: the draft's title is printed above it."""
    lines = text.strip().splitlines()
    if lines and re.match(r"^#\s", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")
