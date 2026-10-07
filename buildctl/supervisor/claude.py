from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents import RoleRunner, TRUSTED_PREAMBLE, untrusted
from .review import (CLEAN_COVERAGE_DISPOSITION, COVERAGE_DISPOSITIONS, COVERAGE_RECORD_KEYS,
                     DELETED_COVERAGE_DISPOSITION, FINDING_COVERAGE_DISPOSITION,
                     REVIEW_PROTOCOL_VERSION)


class ClaudeAdapter:
    def __init__(self, role_runner: RoleRunner, snapshot: Path):
        self.role_runner, self.snapshot = role_runner, snapshot

    def build_prompt(self, phase: Any, evidence: dict[str, Any], phase_result: dict[str, Any]) -> str:
        policy = (self.snapshot / "prompts/reviews/claude-adversarial-review.md").read_text()
        configured = self.role_runner.config["roles"]["reviewer"].get("protocol_version")
        if configured != REVIEW_PROTOCOL_VERSION or f"Protocol version: `{REVIEW_PROTOCOL_VERSION}`" not in policy:
            raise RuntimeError("incompatible pinned reviewer protocol")
        contract = (self.snapshot / "templates/REVIEW_RESULT.json").read_text()
        architecture = "\n\n".join((self.snapshot / path).read_text() for path in (
            "docs/ARCHITECTURE_PRINCIPLES.md", "docs/HEADLESS_AGENT_CONTRACT.md",
            "docs/DISCOVERY_DECISIONS.md", "docs/PRODUCT_THESIS.md"))
        prompt = TRUSTED_PREAMBLE + "\n" + policy + "\nReview actual evidence; implementer claims are supplementary."
        prompt += untrusted("phase_prompt", phase.prompt.read_text())
        prompt += untrusted("architecture_docs", architecture)
        prompt += "\nTRUSTED DELTA BINDING\n" + json.dumps({"protocol_version": REVIEW_PROTOCOL_VERSION,
                    "delta_id": evidence["delta_id"], "evidence_digest": evidence["digest"],
                    "governance_sha": evidence.get("governance_sha"),
                    "snapshot_digest": evidence.get("snapshot_digest"), "base_sha": evidence.get("base_sha"),
                    "phase": evidence.get("phase"), "reviewer": evidence.get("reviewer"),
                    # Emitted from the validator's own constants so the instructions can
                    # never drift from what will be accepted.
                    "coverage_record_keys": list(COVERAGE_RECORD_KEYS),
                    "coverage_disposition_values": list(COVERAGE_DISPOSITIONS),
                    "disposition_for_path_with_no_finding": CLEAN_COVERAGE_DISPOSITION,
                    "disposition_for_path_with_findings": FINDING_COVERAGE_DISPOSITION,
                    "disposition_for_deleted_path": DELETED_COVERAGE_DISPOSITION,
                    "coverage_obligations": evidence["coverage"],
                    # Findings from earlier reviews of this phase that are still blocking.
                    # Supervisor-held records: the reviewer must adjudicate each one, and
                    # can only do so by re-stating its exact path and claim_key.
                    "open_finding_obligations": evidence.get("open_findings", [])}, sort_keys=True)
        prompt += untrusted("supervisor_delta", evidence["diff"])
        prompt += untrusted("changed_files", json.dumps(evidence["changed_files"], indent=2))
        prompt += untrusted("gate_artifacts", json.dumps(evidence["gates"], indent=2))
        prompt += untrusted("relevant_tests", evidence["tests"])
        prompt += untrusted("implementer_claims", json.dumps(phase_result, indent=2))
        return prompt + "\nPINNED OUTPUT CONTRACT\n" + contract

    def review(self, phase: Any, evidence: dict[str, Any], phase_result: dict[str, Any],
               output: Path, timeout: float) -> dict[str, Any]:
        return self.role_runner.run("reviewer", self.build_prompt(phase, evidence, phase_result), output, timeout)
