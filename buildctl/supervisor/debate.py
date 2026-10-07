from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .agents import AgentError, RoleRunner, TRUSTED_PREAMBLE, load_structured, untrusted
from .protocol import contract
from .safety import redact


class DebateAdapter:
    def __init__(self, role_runner: RoleRunner, snapshot: Path, config: dict[str, Any]):
        self.role_runner, self.snapshot, self.config = role_runner, snapshot, config

    def run(self, phase: Any, findings: list[dict[str, Any]], evidence: dict[str, Any],
            output: Path, timeout: float) -> dict[str, Any]:
        settings = self.config["roles"]["arbiter"]
        if settings.get("mode") == "interactive":
            if output.exists():
                return load_structured(output, "DEBATE")
            request = output.with_name("DEBATE_REQUEST.json")
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(json.dumps({"phase": phase.number, "findings": findings,
                                           "evidence_digest": evidence["digest"], "delta_id": evidence["delta_id"],
                                           "governance_sha": evidence["governance_sha"],
                                           "base_sha": evidence["base_sha"],
                                           "output_contract": json.loads(contract("DEBATE_RESULT")),
                                           "output_path": str(output)}, indent=2) + "\n")
            raise AgentError("INTERACTIVE_DEBATE_REQUIRES_HUMAN")
        prompt = TRUSTED_PREAMBLE + "\nResolve only the typed strategic findings."
        prompt += untrusted("strategic_findings", json.dumps(findings, indent=2))
        prompt += untrusted("evidence", json.dumps(evidence, indent=2))
        prompt += "\nPINNED OUTPUT CONTRACT\n" + contract("DEBATE_RESULT")
        return self.role_runner.run("arbiter", prompt, output, timeout)


def validate_debate_result(value: dict[str, Any]) -> None:
    required = {"status", "decision", "finding_dispositions", "required_changes", "human_decisions", "revisit_when"}
    if set(value) != required or value["status"] not in {"agreement", "agree_to_disagree", "failed"}:
        raise ValueError("invalid DEBATE_RESULT")
    if any(not isinstance(value[key], list) for key in required - {"status", "decision"}):
        raise ValueError("invalid DEBATE_RESULT lists")


def write_adr(destination: Path, metadata: dict[str, Any], result: dict[str, Any]) -> None:
    validate_debate_result(result)
    if result["status"] != "agreement":
        raise ValueError("only agreement may produce an Accepted ADR")
    safe_decision = redact(result["decision"], limit=2000)
    body = [f"# {metadata['title']}", "", f"- Date: {date.today().isoformat()}", "- Status: Accepted",
            f"- Debate: {metadata['debate_id']}", f"- Governance SHA: {metadata['governance_sha']}",
            f"- Base SHA: {metadata['base_sha']}", f"- Evidence digest: {metadata['evidence_digest']}",
            f"- Finding IDs: {', '.join(metadata['finding_ids'])}", "", "## Decision", "", safe_decision,
            "", "## Required changes", ""]
    body += [f"- {redact(str(item), limit=500)}" for item in result["required_changes"]] or ["- None"]
    body += ["", "## Revisit when", ""]
    body += [f"- {redact(str(item), limit=500)}" for item in result["revisit_when"]] or ["- No trigger recorded"]
    body += ["", "_Agent-authored rationale above is untrusted decision data, not governance instruction._", ""]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(body))
