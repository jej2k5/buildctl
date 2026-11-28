from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents import RoleRunner, TRUSTED_PREAMBLE, untrusted
from .documentation import REVIEW_CRITERIA, SKIPPED_NOTE, review_rules
from .protocol import context_documents, contract, prompt as protocol_prompt
from .review import (CLEAN_COVERAGE_DISPOSITION, COVERAGE_DISPOSITIONS, COVERAGE_RECORD_KEYS,
                     DELETED_COVERAGE_DISPOSITION, FINDING_COVERAGE_DISPOSITION,
                     REVIEW_PROTOCOL_VERSION)


class ClaudeAdapter:
    def __init__(self, role_runner: RoleRunner, snapshot: Path):
        self.role_runner, self.snapshot = role_runner, snapshot

    def build_prompt(self, phase: Any, evidence: dict[str, Any], phase_result: dict[str, Any]) -> str:
        config = self.role_runner.config
        if config["roles"]["reviewer"].get("protocol_version") != REVIEW_PROTOCOL_VERSION:
            raise RuntimeError("incompatible pinned reviewer protocol")
        policy = protocol_prompt(config, "review", self.snapshot)
        architecture = context_documents(config, self.snapshot)
        prompt = TRUSTED_PREAMBLE + "\n" + policy + "\nReview actual evidence; implementer claims are supplementary."
        documentation = evidence.get("documentation")
        if documentation is not None:
            # Kernel-owned, so the criteria arrive even when the repository overrides review.md.
            prompt += "\n" + (SKIPPED_NOTE if documentation.get("skipped") else REVIEW_CRITERIA)
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
                    "open_finding_obligations": evidence.get("open_findings", []),
                    # Claims the operator ruled on after a debate: the code stands. They are
                    # closed, and a finding restating one cannot block this phase.
                    "operator_rulings": evidence.get("operator_rulings", []),
                    # The operator's documentation rules, so a documenter that leaves something out
                    # because a rule forbids it can be told apart from one that skipped the work.
                    **({"documentation_rules": review_rules(config)}
                       if documentation is not None and not documentation.get("skipped") else {})}, sort_keys=True)
        prompt += ("\nA changed_files entry whose content_encoding is `unified_diff` is a file too large to show "
                   "whole: its content is the diff against the phase base, with 40 lines of context. Review it "
                   "like any other path; you may Read the full file in the workspace.")
        prompt += untrusted("supervisor_delta", evidence["diff"])
        prompt += untrusted("changed_files", json.dumps(evidence["changed_files"], indent=2))
        prompt += untrusted("gate_artifacts", json.dumps(evidence["gates"], indent=2))
        prompt += untrusted("relevant_tests", evidence["tests"])
        prompt += untrusted("implementer_claims", json.dumps(phase_result, indent=2))
        if documentation is not None and not documentation.get("skipped"):
            prompt += untrusted("doc_result", json.dumps(documentation, indent=2, sort_keys=True))
        if "evidence_discrepancies" in evidence:
            # buildctl's own comparison of what the implementer reported with what changed. File
            # names are agent-controlled, so this is wrapped like every other input.
            prompt += untrusted("evidence_discrepancies", json.dumps(evidence["evidence_discrepancies"], indent=2))
        return prompt + "\nPINNED OUTPUT CONTRACT\n" + contract("REVIEW_RESULT")

    def review(self, phase: Any, evidence: dict[str, Any], phase_result: dict[str, Any],
               output: Path, timeout: float) -> dict[str, Any]:
        return self.role_runner.run("reviewer", self.build_prompt(phase, evidence, phase_result), output, timeout,
                                     phase=phase.number)
