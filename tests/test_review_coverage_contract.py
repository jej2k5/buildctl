"""Regression tests for the reviewer coverage vocabulary.

Phase 0 burned both semantic review attempts on REVIEW_FORMAT_ATTEMPTS_EXHAUSTED because
the pinned prompt named only one of the three `disposition` values, so the reviewer invented
`reviewed` for clean paths. These tests pin the vocabulary in the validator, in the prompt,
and in the pinned output contract, so the three can never drift apart again.
"""
import json
import unittest
from pathlib import Path

from buildctl.supervisor.review import (CLEAN_COVERAGE_DISPOSITION, COVERAGE_DISPOSITIONS,
                                     COVERAGE_RECORD_KEYS, DELETED_COVERAGE_DISPOSITION,
                                     FINDING_COVERAGE_DISPOSITION, REVIEW_PROTOCOL_VERSION,
                                     ReviewValidationError, reduce_review, stable_finding_id,
                                     validate_review)

ROOT = Path(__file__).resolve().parents[1]
from buildctl.supervisor.protocol import PROTOCOL_ROOT
PROMPT = PROTOCOL_ROOT / "prompts" / "review.md"
CONTRACT = PROTOCOL_ROOT / "templates" / "REVIEW_RESULT.json"
ADDED, DELETED = "c" * 64, "d" * 64
REVIEWER = {"vendor": "anthropic", "model": "claude", "prompt_lineage": "factory-v2",
            "execution_boundary": "macos-sandbox-exec-v2-deny-default"}


def finding(path=("src/a.py")):
    item = {"severity": "HIGH", "kind": "correctness", "path": path, "line": 1,
            "claim_key": "bug", "claim": "c", "rationale": "r", "required_change": "x",
            "evidence_digest": ADDED, "violated_invariant": "inv", "treatment": "fix",
            "status": "open"}
    item["id"] = stable_finding_id(item)
    return item


def expected(coverage):
    return {"governance_sha": "G", "snapshot_digest": "S", "base_sha": "B", "phase": 0,
            "delta_id": "DID", "evidence_digest": "E", "reviewer": REVIEWER, "coverage": coverage}


def report(coverage, findings=None, verdict="PASS"):
    return {"schema_version": 2, "protocol_version": REVIEW_PROTOCOL_VERSION,
            "governance_sha": "G", "snapshot_digest": "S", "base_sha": "B", "phase": 0,
            "delta_id": "DID", "evidence_digest": "E", "reviewer": REVIEWER, "attempt": 1,
            "verdict": verdict, "findings": findings or [], "coverage": coverage}


def record(path, digest, change_type, disposition, refs=()):
    return {"path": path, "digest": digest, "change_type": change_type,
            "disposition": disposition, "finding_refs": list(refs), "unreviewed": False}


class CoverageVocabularyTests(unittest.TestCase):
    def test_invented_dispositions_are_rejected(self):
        """The exact Phase 0 failure: a clean added path marked 'reviewed'."""
        obligations = {("src/a.py", ADDED, "added")}
        for invented in ("reviewed", "ok", "pass", "inspected", "CLEAR", ""):
            coverage = [record("src/a.py", ADDED, "added", invented)]
            with self.assertRaises(ReviewValidationError) as caught:
                validate_review(report(coverage), expected(obligations))
            message = str(caught.exception)
            self.assertIn("disposition", message)
            self.assertIn(repr(invented), message, "the rejection must name the offending value")
            self.assertIn("src/a.py", message, "the rejection must name the offending path")

    def test_clear_with_no_findings_passes_for_added_and_modified_paths(self):
        for change_type in ("added", "modified", "renamed"):
            obligations = {("src/a.py", ADDED, change_type)}
            coverage = [record("src/a.py", ADDED, change_type, CLEAN_COVERAGE_DISPOSITION)]
            validate_review(report(coverage), expected(obligations))

    def test_finding_refs_disposition_requires_real_finding_references(self):
        obligations = {("src/a.py", ADDED, "added")}
        item = finding()
        good = [record("src/a.py", ADDED, "added", FINDING_COVERAGE_DISPOSITION, [item["id"]])]
        validate_review(report(good, [item], verdict="REMEDIATE"), expected(obligations))
        # An unresolvable reference is rejected during identity normalization, which now
        # names the offending reference rather than reporting a bare "unknown finding".
        dangling = [record("src/a.py", ADDED, "added", FINDING_COVERAGE_DISPOSITION, ["0" * 64])]
        with self.assertRaisesRegex(ReviewValidationError, "unresolvable finding"):
            validate_review(report(dangling, [item], verdict="REMEDIATE"), expected(obligations))

    def test_deleted_paths_require_deletion_acknowledged_and_nothing_else(self):
        obligations = {("src/gone.py", DELETED, "deleted")}
        ok = [record("src/gone.py", DELETED, "deleted", DELETED_COVERAGE_DISPOSITION)]
        validate_review(report(ok), expected(obligations))
        for wrong in (CLEAN_COVERAGE_DISPOSITION, FINDING_COVERAGE_DISPOSITION):
            coverage = [record("src/gone.py", DELETED, "deleted", wrong)]
            with self.assertRaisesRegex(ReviewValidationError, "deletion acknowledgement"):
                validate_review(report(coverage), expected(obligations))
        # and the acknowledgement may not be used for a path that was not deleted
        live = {("src/a.py", ADDED, "added")}
        coverage = [record("src/a.py", ADDED, "added", DELETED_COVERAGE_DISPOSITION)]
        with self.assertRaisesRegex(ReviewValidationError, "deletion acknowledgement"):
            validate_review(report(coverage), expected(live))

    def test_unreviewed_true_is_always_rejected(self):
        obligations = {("src/a.py", ADDED, "added")}
        coverage = [{**record("src/a.py", ADDED, "added", CLEAN_COVERAGE_DISPOSITION), "unreviewed": True}]
        with self.assertRaises(ReviewValidationError):
            validate_review(report(coverage), expected(obligations))

    def test_clean_pass_report_validates_and_advances_end_to_end(self):
        """A complete, correctly-worded clean review must reach ADVANCE."""
        obligations = {("docs/HANDOFF_PHASE_0.md", ADDED, "added"),
                       ("docs/constitution/README.md", ADDED, "added"),
                       ("src/gone.py", DELETED, "deleted")}
        coverage = [record("docs/HANDOFF_PHASE_0.md", ADDED, "added", CLEAN_COVERAGE_DISPOSITION),
                    record("docs/constitution/README.md", ADDED, "added", CLEAN_COVERAGE_DISPOSITION),
                    record("src/gone.py", DELETED, "deleted", DELETED_COVERAGE_DISPOSITION)]
        value = report(coverage)
        validate_review(value, expected(obligations))
        transition = reduce_review(report=value, findings=[], mechanical_passed=True,
                                   coverage_valid=True,
                                   policy={"max_remediation_attempts": 2, "debate_enabled": True,
                                           "max_debate_attempts": 6},
                                   attempts={"remediation": 0, "debate": 0},
                                   budget_available=True, approvals_valid=True)
        self.assertEqual(transition.disposition, "ADVANCE")
        self.assertEqual(transition.rule_id, "R7")


class ContractConsistencyTests(unittest.TestCase):
    """The prompt and the pinned contract must teach exactly what the validator accepts."""

    def test_prompt_names_every_accepted_disposition(self):
        text = PROMPT.read_text()
        for value in COVERAGE_DISPOSITIONS:
            self.assertIn(f"`{value}`", text, f"the prompt never teaches {value!r}")
        for key in COVERAGE_RECORD_KEYS:
            self.assertIn(f"`{key}`", text)

    def test_prompt_forbids_the_invented_value_that_caused_the_failure(self):
        self.assertIn("reviewed`", PROMPT.read_text(),
                      "the prompt should explicitly warn against inventing `reviewed`")

    def test_pinned_contract_shows_a_usable_coverage_record(self):
        contract = json.loads(CONTRACT.read_text())
        self.assertTrue(contract["coverage"], "an empty coverage array teaches nothing")
        shown = set()
        for item in contract["coverage"]:
            self.assertEqual(set(item), set(COVERAGE_RECORD_KEYS))
            self.assertIn(item["disposition"], COVERAGE_DISPOSITIONS)
            self.assertFalse(item["unreviewed"])
            shown.add(item["disposition"])
        self.assertEqual(shown, set(COVERAGE_DISPOSITIONS),
                         "the contract must exemplify every accepted disposition")

    def test_trusted_binding_carries_the_validator_vocabulary(self):
        """claude.py must emit the vocabulary from the validator's own constants."""
        source = (ROOT / "buildctl/supervisor/claude.py").read_text()
        self.assertIn("coverage_disposition_values", source)
        self.assertIn("COVERAGE_DISPOSITIONS", source)
        self.assertIn("CLEAN_COVERAGE_DISPOSITION", source)
        self.assertNotIn('"clear"', source, "the value must come from the constant, not a literal")
