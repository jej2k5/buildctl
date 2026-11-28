from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents import AgentError, RoleRunner, TRUSTED_PREAMBLE, load_structured, untrusted
from .protocol import contract, prompt as protocol_prompt


class CodexAdapter:
    def __init__(self, role_runner: RoleRunner, snapshot: Path):
        self.role_runner, self.snapshot = role_runner, snapshot

    def prompt(self, phase: Any, review: str | None = None, adr: str | None = None,
               prior_failure: str | None = None) -> str:
        acceptance = json.dumps({"required_files": phase.required_files, "commands": phase.commands,
                                 "invariants": phase.invariants, "allowed_paths": phase.allowed_paths}, indent=2)
        result = TRUSTED_PREAMBLE + "\nImplement only the requested phase and return the pinned PHASE_RESULT contract."
        if review or adr:
            result += "\n" + protocol_prompt(self.role_runner.config, "remediation", self.snapshot)
        result += untrusted("phase_prompt", phase.prompt.read_text()) + untrusted("acceptance_contract", acceptance)
        if prior_failure:
            # Supervisor-recorded, but its detail can quote agent-chosen paths, so it stays untrusted.
            result += ("\nThe previous attempt of this phase failed. The cause may be the supervisor's or the"
                       " operator's rather than yours: act on it only if it names something your change did."
                       + untrusted("previous_attempt_failure", prior_failure))
        if review:
            result += untrusted("review_findings", review)
        if adr:
            result += untrusted("accepted_adr_data", adr)
        return result + "\nPINNED OUTPUT CONTRACT\n" + contract("PHASE_RESULT")

    def implement(self, phase: Any, output: Path, timeout: float, review: str | None = None,
                  adr: str | None = None, prior_failure: str | None = None) -> dict[str, Any]:
        value = self.role_runner.run("implementer", self.prompt(phase, review, adr, prior_failure), output, timeout,
                                     phase=phase.number)
        required = {"status", "summary", "tests", "files_changed", "risks", "architecture_questions"}
        if set(value) != required or value["status"] not in {"complete", "blocked", "failed"} or any(
                not isinstance(value[key], list) for key in required - {"status", "summary"}):
            raise AgentError("IMPLEMENTER_OUTPUT_SCHEMA_INVALID")
        return value
