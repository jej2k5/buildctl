"""Regression tests for supervisor-owned deterministic finding identity.

Review 1 of Phase 0 raised a substantively valid finding but supplied a SHA-256 that did
not match `stable_finding_id`, so an otherwise correct report was rejected. The model must
never be required to manufacture a cryptographic identifier: it authors the substantive
identity fields and a short local label, and the supervisor computes the canonical id.
"""
import json
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import AgentError, FORMAT_RETRYABLE_SUFFIXES, load_structured
from buildctl.supervisor.review import (FINDING_IDENTITY_FIELDS, REVIEW_PROTOCOL_VERSION,
                                     ReviewValidationError, accumulate_findings,
                                     normalize_finding_identities, reduce_review,
                                     stable_finding_id, validate_review)

ROOT = Path(__file__).resolve().parents[1]
from buildctl.supervisor.protocol import PROTOCOL_ROOT
PROMPT = PROTOCOL_ROOT / "prompts" / "review.md"

# The exact values from the failing Phase 0 review.
REAL_PATH = "docs/constitution/adr-conventions.md"
REAL_CLAIM_KEY = "adr-numbering-conflicts-with-factory-adr-producer"
REAL_DIGEST = "4bb2f0ebd0514b1c9bdc4ffeebae749479be2f02b402ef85c314fd5b795ae6bf"
REAL_INVARIANT = "auditability-of-binding-decision-records"
MODEL_GUESS = "c4d8ce9194b3f90c6ac06f6d040bbba35b55fdd8493ca424679624577dd38d7d"
CANONICAL = "cda8fe1de257a6e5874b9a0c21d7e4198ec3afdb4e9854822f190ff88f82eb40"
REVIEWER = {"vendor": "anthropic", "model": "claude", "prompt_lineage": "factory-v2",
            "execution_boundary": "macos-sandbox-exec-v2-deny-default"}


def finding(label, path=REAL_PATH, claim_key=REAL_CLAIM_KEY, digest=REAL_DIGEST,
            invariant=REAL_INVARIANT, severity="HIGH"):
    return {"id": label, "severity": severity, "kind": "correctness", "path": path, "line": 1,
            "claim_key": claim_key, "claim": "c", "rationale": "r", "required_change": "x",
            "evidence_digest": digest, "violated_invariant": invariant,
            "treatment": "fix", "status": "open"}


def coverage(path, digest, refs, change_type="added"):
    return {"path": path, "digest": digest, "change_type": change_type,
            "disposition": "finding_refs" if refs else "clear",
            "finding_refs": list(refs), "unreviewed": False}


def report(findings, cover, verdict="REMEDIATE"):
    return {"schema_version": 2, "protocol_version": REVIEW_PROTOCOL_VERSION,
            "governance_sha": "G", "snapshot_digest": "S", "base_sha": "B", "phase": 0,
            "delta_id": "DID", "evidence_digest": "E", "reviewer": REVIEWER, "attempt": 1,
            "verdict": verdict, "findings": findings, "coverage": cover}


def expected(obligations):
    return {"governance_sha": "G", "snapshot_digest": "S", "base_sha": "B", "phase": 0,
            "delta_id": "DID", "evidence_digest": "E", "reviewer": REVIEWER,
            "coverage": obligations}


class FindingIdentityNormalizationTests(unittest.TestCase):
    def test_the_real_phase_zero_review_now_validates(self):
        """The reported defect, end to end, with the reported values."""
        value = report([finding(MODEL_GUESS)],
                       [coverage(REAL_PATH, REAL_DIGEST, [MODEL_GUESS])])
        validate_review(value, expected({(REAL_PATH, REAL_DIGEST, "added")}))
        self.assertEqual(value["findings"][0]["id"], CANONICAL)
        self.assertEqual(value["coverage"][0]["finding_refs"], [CANONICAL])

    def test_short_local_label_is_accepted_and_replaced(self):
        value = report([finding("f1")], [coverage(REAL_PATH, REAL_DIGEST, ["f1"])])
        validate_review(value, expected({(REAL_PATH, REAL_DIGEST, "added")}))
        self.assertEqual(value["findings"][0]["id"], CANONICAL)
        self.assertEqual(value["coverage"][0]["finding_refs"], [CANONICAL])

    def test_a_correct_hash_still_works(self):
        value = report([finding(CANONICAL)], [coverage(REAL_PATH, REAL_DIGEST, [CANONICAL])])
        validate_review(value, expected({(REAL_PATH, REAL_DIGEST, "added")}))
        self.assertEqual(value["findings"][0]["id"], CANONICAL)

    def test_a_missing_id_is_supplied_by_the_supervisor(self):
        item = finding("f1")
        del item["id"]
        value = report([item], [coverage(REAL_PATH, REAL_DIGEST, [])])
        normalize_finding_identities(value)
        self.assertEqual(value["findings"][0]["id"], CANONICAL)

    def test_canonical_id_is_stable_across_replay(self):
        first = report([finding("f1")], [coverage(REAL_PATH, REAL_DIGEST, ["f1"])])
        second = report([finding("completely-different-label")],
                        [coverage(REAL_PATH, REAL_DIGEST, ["completely-different-label"])])
        normalize_finding_identities(first)
        normalize_finding_identities(second)
        self.assertEqual(first["findings"][0]["id"], second["findings"][0]["id"])
        self.assertEqual(first["findings"][0]["id"], CANONICAL)
        # and normalizing an already-normalized report is a no-op
        again = json.loads(json.dumps(first))
        normalize_finding_identities(again)
        self.assertEqual(again, first)

    def test_identity_depends_only_on_the_substantive_fields(self):
        base = finding("f1")
        for field in FINDING_IDENTITY_FIELDS:
            changed = dict(base)
            changed[field] = changed[field] + "-x"
            self.assertNotEqual(stable_finding_id(changed), stable_finding_id(base))
        for cosmetic in ("severity", "claim", "rationale", "required_change", "treatment", "status", "line"):
            changed = dict(base)
            changed[cosmetic] = 9 if cosmetic == "line" else "CRITICAL" if cosmetic == "severity" else "z"
            self.assertEqual(stable_finding_id(changed), stable_finding_id(base))

    def test_reducer_only_ever_sees_canonical_ids(self):
        value = report([finding(MODEL_GUESS)], [coverage(REAL_PATH, REAL_DIGEST, [MODEL_GUESS])])
        validate_review(value, expected({(REAL_PATH, REAL_DIGEST, "added")}))
        register: dict = {}
        accumulate_findings(register, value, "S")
        for stored in register.values():
            self.assertEqual(stored["id"], CANONICAL)
        transition = reduce_review(report=value, findings=list(register.values()),
                                   mechanical_passed=True, coverage_valid=True,
                                   policy={"max_remediation_attempts": 2, "debate_enabled": True,
                                           "max_debate_attempts": 6},
                                   attempts={"remediation": 0, "debate": 0},
                                   budget_available=True, approvals_valid=True)
        self.assertEqual(transition.disposition, "REMEDIATE")


class FailClosedTests(unittest.TestCase):
    def test_duplicate_substantive_identities_fail_closed(self):
        value = report([finding("f1"), finding("f2")], [coverage(REAL_PATH, REAL_DIGEST, ["f1", "f2"])])
        with self.assertRaisesRegex(ReviewValidationError, "duplicate finding identity"):
            normalize_finding_identities(value)

    def test_one_label_used_for_two_different_findings_fails_closed(self):
        other = finding("f1", claim_key="a-different-claim")
        value = report([finding("f1"), other], [coverage(REAL_PATH, REAL_DIGEST, ["f1"])])
        with self.assertRaisesRegex(ReviewValidationError, "refers to two different findings"):
            normalize_finding_identities(value)

    def test_unknown_coverage_reference_fails_closed(self):
        value = report([finding("f1")], [coverage(REAL_PATH, REAL_DIGEST, ["f9"])])
        with self.assertRaisesRegex(ReviewValidationError, "unresolvable finding"):
            normalize_finding_identities(value)

    def test_non_string_coverage_reference_fails_closed(self):
        value = report([finding("f1")], [coverage(REAL_PATH, REAL_DIGEST, [None])])
        with self.assertRaisesRegex(ReviewValidationError, "unresolvable finding"):
            normalize_finding_identities(value)

    def test_repeated_reference_to_the_same_finding_fails_closed(self):
        value = report([finding("f1")], [coverage(REAL_PATH, REAL_DIGEST, ["f1", "f1"])])
        with self.assertRaisesRegex(ReviewValidationError, "references a finding twice"):
            normalize_finding_identities(value)

    def test_label_colliding_with_another_canonical_id_fails_closed(self):
        """A label that happens to equal a different finding's canonical id is ambiguous."""
        other = finding("f2", claim_key="second-claim")
        value = report([finding(stable_finding_id(other)), other],
                       [coverage(REAL_PATH, REAL_DIGEST, [])])
        with self.assertRaisesRegex(ReviewValidationError, "collides with another finding"):
            normalize_finding_identities(value)

    def test_missing_substantive_identity_fields_fail_closed(self):
        for field in FINDING_IDENTITY_FIELDS:
            item = finding("f1")
            item[field] = ""
            value = report([item], [coverage(REAL_PATH, REAL_DIGEST, [])])
            with self.assertRaisesRegex(ReviewValidationError, "substantive identity fields"):
                normalize_finding_identities(value)

    def test_invalid_citation_still_fails_closed_after_normalization(self):
        """Normalization must not paper over a finding citing bytes outside the delta."""
        value = report([finding("f1", digest="f" * 64)],
                       [coverage(REAL_PATH, REAL_DIGEST, [])])
        with self.assertRaisesRegex(ReviewValidationError, "unresolved citation"):
            validate_review(value, expected({(REAL_PATH, REAL_DIGEST, "added")}))


class PromptContractTests(unittest.TestCase):
    def test_prompt_no_longer_asks_the_model_to_compute_a_hash(self):
        text = PROMPT.read_text()
        self.assertNotIn("Derive `id` as lowercase SHA-256", text)
        self.assertIn("Do **not** compute a hash", text)
        self.assertIn("short local label", text)

    def test_pinned_contract_shows_a_label_not_a_hash(self):
        contract = json.loads((PROTOCOL_ROOT / "templates" / "REVIEW_RESULT.json").read_text())
        self.assertEqual(contract["findings"][0]["id"], "f1")
        self.assertIn("f1", contract["coverage"][1]["finding_refs"])


class StructuredOutputDiagnosticsTests(unittest.TestCase):
    """REVIEWER_OUTPUT_INVALID conflated four distinct failures."""

    def _code(self, write, name="result.json"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            if write is not None:
                path.write_text(write)
            with self.assertRaises(AgentError) as caught:
                load_structured(path, "REVIEWER")
            return str(caught.exception)

    def test_each_failure_mode_has_a_distinct_code_and_reason(self):
        missing = self._code(None)
        empty = self._code("   \n")
        bad_json = self._code('{"schema_version": 2,,}')
        not_object = self._code('["a", "list"]')
        self.assertTrue(missing.startswith("REVIEWER_OUTPUT_MISSING"))
        self.assertTrue(empty.startswith("REVIEWER_OUTPUT_EMPTY"))
        self.assertTrue(bad_json.startswith("REVIEWER_OUTPUT_INVALID_JSON"))
        self.assertTrue(not_object.startswith("REVIEWER_OUTPUT_NOT_OBJECT"))
        codes = {value.split(":", 1)[0] for value in (missing, empty, bad_json, not_object)}
        self.assertEqual(len(codes), 4, "each failure mode must be distinguishable")
        self.assertIn("line", bad_json)
        self.assertIn("list", not_object)

    def test_all_shape_failures_remain_format_retryable(self):
        for value in (self._code(None), self._code("  "), self._code("{,}"), self._code("[1]")):
            code = value.split(":", 1)[0]
            self.assertTrue(code.endswith(FORMAT_RETRYABLE_SUFFIXES),
                            f"{code} would not be retried within the review-format budget")

    def test_a_valid_object_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text('{"status": "complete"}')
            self.assertEqual(load_structured(path, "REVIEWER"), {"status": "complete"})
