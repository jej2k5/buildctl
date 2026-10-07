"""Regression tests for the findings register lifecycle.

Phase 1 review-7 returned `PASS` with zero blocking findings — the remediation had genuinely
worked — and was still rejected as `R1_VERDICT_MISMATCH`. The reducer does not read the
report; it reads `state.findings`, accumulated across every review of the phase. Five HIGH
findings from earlier reviews were still recorded open.

Three mechanisms made the register write-only:

1. `accumulate_findings` clears a finding only on an explicit `status: "resolved"` matched by
   (path, claim_key). Omission never resolves.
2. The reviewer was never shown the register, so it could not retire what it had fixed and
   could not guess a prior `claim_key`.
3. `validate_review` refuses any finding citing a path outside the current delta, so a
   finding whose file no longer exists — `database.py`, from an abandoned attempt — could
   never be adjudicated by any acceptable report.

Once any HIGH finding entered the register, the phase could never reach ADVANCE.
"""
import json
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.review import (CLOSED_FINDING_STATUSES, DEFERRED_STATUS,
                                     NON_BLOCKING_FINDING_STATUSES, REVIEWER_FINDING_STATUSES,
                                     SUPERSEDED_STATUS, accumulate_findings,
                                     open_finding_obligations, reduce_review,
                                     realign_findings_to_delta, stable_finding_id)

from buildctl.supervisor.protocol import PROTOCOL_ROOT

POLICY = {"max_remediation_attempts": 2, "debate_enabled": True, "max_debate_attempts": 1}
FRESH = {"remediation": 0, "debate": 0}
PROMPT = PROTOCOL_ROOT / "prompts" / "review.md"


def finding(path="src/x.py", claim_key="k", severity="HIGH", status="open", **extra):
    base = {"id": "f1", "severity": severity, "kind": "correctness", "path": path, "line": 1,
            "claim_key": claim_key, "claim": "c", "rationale": "r", "required_change": "rc",
            "evidence_digest": "d", "violated_invariant": "i", "treatment": "fix", "status": status}
    base.update(extra)
    base["id"] = stable_finding_id(base)
    return base


def register_with(*findings, evidence="d"):
    register: dict = {}
    accumulate_findings(register, {"findings": list(findings), "evidence_digest": evidence}, "policy")
    return register


def reduce(verdict, findings):
    return reduce_review(report={"verdict": verdict}, findings=list(findings.values()),
                         mechanical_passed=True, coverage_valid=True, policy=POLICY,
                         attempts=FRESH, budget_available=True, approvals_valid=True)


class ReportedFailureTests(unittest.TestCase):
    def test_a_clean_report_is_still_blocked_by_a_stale_open_finding(self):
        """The exact reported failure: PASS, zero blocking in the report, mismatch anyway."""
        register = register_with(finding(severity="HIGH"))
        transition = reduce("PASS", register)
        self.assertEqual(transition.rule_id, "R1_VERDICT_MISMATCH")

    def test_omitting_a_fixed_finding_does_not_resolve_it(self):
        register = register_with(finding(severity="HIGH", claim_key="old"))
        accumulate_findings(register, {"findings": [finding(severity="MEDIUM", claim_key="new")],
                                       "evidence_digest": "d2"}, "policy")
        still_open = [v for v in register.values() if v["status"] != "resolved"]
        self.assertEqual(len(still_open), 2, "omission must not clear the earlier finding")

    def test_explicitly_resolving_it_clears_the_way_to_advance(self):
        register = register_with(finding(severity="HIGH", claim_key="old"))
        accumulate_findings(register, {"findings": [finding(severity="HIGH", claim_key="old",
                                                            status="resolved")],
                                       "evidence_digest": "d2"}, "policy")
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")


class ObligationsHandedToTheReviewerTests(unittest.TestCase):
    """Mechanism 2: the reviewer must be shown what it has to adjudicate."""

    def test_open_findings_are_listed_with_the_identity_needed_to_retire_them(self):
        register = register_with(finding(path="src/a.py", claim_key="alpha", severity="HIGH"))
        obligations = open_finding_obligations(register)
        self.assertEqual(len(obligations), 1)
        item = obligations[0]
        self.assertEqual(item["path"], "src/a.py")
        self.assertEqual(item["claim_key"], "alpha")
        self.assertEqual(item["severity"], "HIGH")
        self.assertIn("status", item)

    def test_resolved_and_superseded_findings_are_not_listed(self):
        register = register_with(finding(path="src/a.py", claim_key="a"),
                                 finding(path="src/b.py", claim_key="b"))
        for entry, status in zip(register.values(), ("resolved", SUPERSEDED_STATUS)):
            entry["status"] = status
        self.assertEqual(open_finding_obligations(register), [])

    def test_obligations_are_ordered_deterministically(self):
        register = register_with(finding(path="src/z.py", claim_key="z"),
                                 finding(path="src/a.py", claim_key="a"))
        self.assertEqual([i["path"] for i in open_finding_obligations(register)],
                         ["src/a.py", "src/z.py"])

    def test_claim_text_is_bounded(self):
        register = register_with(finding(claim_key="k"))
        list(register.values())[0]["claim"] = "x" * 5000
        self.assertLessEqual(len(open_finding_obligations(register, claim_limit=100)[0]["claim"]), 100)

    def test_the_reviewer_prompt_states_the_adjudication_obligation(self):
        text = PROMPT.read_text()
        self.assertIn("open_finding_obligations", text)
        self.assertIn("claim_key", text)
        for status in ("resolved", "still_open", "unverified"):
            self.assertIn(status, text)
        self.assertRegex(text, r"(?is)never cleared by leaving it out")


class RealignFindingsToDeltaTests(unittest.TestCase):
    """Mechanism 3: a finding the validator would refuse to accept a citation for.

    `validate_review` requires every finding to cite a path in the current delta's
    coverage, so a finding outside coverage cannot be marked resolved, still_open or
    unverified by any acceptable report. It is parked, not retired.
    """

    def test_a_finding_outside_the_delta_is_deferred(self):
        register = register_with(finding(path="src/relay/database.py", claim_key="ghost"))
        parked, restored = realign_findings_to_delta(register, ["src/relay/db.py"])
        self.assertEqual((parked, restored), (("src/relay/database.py",), ()))
        entry = list(register.values())[0]
        self.assertEqual(entry["status"], DEFERRED_STATUS)
        self.assertEqual(entry["deferred_from"], "open")
        self.assertIn("deferred_reason", entry)

    def test_a_finding_inside_the_delta_is_left_blocking(self):
        register = register_with(finding(path="src/relay/db.py", claim_key="real"))
        self.assertEqual(realign_findings_to_delta(register, ["src/relay/db.py"]), ((), ()))
        self.assertEqual(list(register.values())[0]["status"], "open")

    def test_a_finding_fixed_by_reverting_a_file_stops_blocking(self):
        """The Phase 3 failure: remediation reverted a published schema, which removed that
        path from the delta and stranded the finding the fix had addressed."""
        reverted = "src/relay/schemas/exported/AgentSession-1.0.0.schema.json"
        register = register_with(finding(path=reverted, severity="HIGH",
                                         claim_key="published-contract-version-mutated-in-place"))
        self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH")
        # after the revert the path is no longer in the delta the reviewer is shown
        self.assertEqual(realign_findings_to_delta(register, ["src/relay/sessions.py"])[0],
                         (reverted,))
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_deferring_unblocks_advance(self):
        register = register_with(finding(path="src/gone.py", claim_key="ghost", severity="HIGH"))
        self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH")
        realign_findings_to_delta(register, ["src/keep.py"])
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_already_closed_findings_are_untouched(self):
        for status in ("resolved", SUPERSEDED_STATUS):
            register = register_with(finding(path="src/gone.py", claim_key="g"))
            list(register.values())[0]["status"] = status
            self.assertEqual(realign_findings_to_delta(register, ["src/keep.py"]), ((), ()))
            self.assertEqual(list(register.values())[0]["status"], status)

    def test_empty_coverage_parks_every_open_finding(self):
        """A delta with no reviewable paths leaves nothing citable."""
        register = register_with(finding(path="src/a.py", claim_key="a"),
                                 finding(path="src/b.py", claim_key="b"))
        self.assertEqual(len(realign_findings_to_delta(register, [])[0]), 2)

    def test_non_string_coverage_entries_are_ignored_safely(self):
        register = register_with(finding(path="src/a.py", claim_key="a"))
        self.assertEqual(realign_findings_to_delta(register, [None, 3, "src/a.py"]), ((), ()))


class SupervisorOwnedStatusesTests(unittest.TestCase):
    def test_a_model_may_not_author_either_supervisor_status(self):
        self.assertNotIn(SUPERSEDED_STATUS, REVIEWER_FINDING_STATUSES)
        self.assertNotIn(DEFERRED_STATUS, REVIEWER_FINDING_STATUSES)

    def test_deferred_is_not_terminal(self):
        """The distinction the whole mechanism rests on: parked, not decided."""
        self.assertNotIn(DEFERRED_STATUS, CLOSED_FINDING_STATUSES)
        self.assertIn(DEFERRED_STATUS, NON_BLOCKING_FINDING_STATUSES)

    def test_both_closed_statuses_stop_blocking(self):
        self.assertEqual(CLOSED_FINDING_STATUSES, {"resolved", SUPERSEDED_STATUS})
        for status in CLOSED_FINDING_STATUSES:
            register = register_with(finding(severity="HIGH"))
            list(register.values())[0]["status"] = status
            self.assertEqual(reduce("PASS", register).disposition, "ADVANCE", status)

    def test_still_open_and_unverified_keep_blocking(self):
        for status in ("open", "still_open", "unverified"):
            register = register_with(finding(severity="HIGH"))
            list(register.values())[0]["status"] = status
            self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH", status)


class UnrelatedProtectionsIntactTests(unittest.TestCase):
    def test_a_genuinely_open_high_finding_still_blocks(self):
        register = register_with(finding(severity="HIGH"))
        self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH")

    def test_medium_findings_never_blocked_advance(self):
        register = register_with(finding(severity="MEDIUM"))
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_severity_upgrade_protection_is_unchanged(self):
        from buildctl.supervisor.review import ReviewValidationError
        register = register_with(finding(severity="HIGH", claim_key="k"))
        with self.assertRaises(ReviewValidationError):
            accumulate_findings(register, {"findings": [finding(severity="LOW", claim_key="k")],
                                           "evidence_digest": "d"}, "policy")
