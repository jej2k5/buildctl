"""Regression tests for the advisory verdict / findings consistency contract.

Phase 1 attempt 2 escalated REVIEW_BLOCKED with `R1_VERDICT_MISMATCH`: the reviewer returned
`verdict: "PASS"` while filing 14 open findings, two of them HIGH. The reducer refused to let
a free-form verdict override the reviewer's own typed findings — working as designed.

The mismatch was a contract gap, not a model failure. `prompts/reviews/claude-adversarial-review.md`
did not mention `verdict` at all, and `templates/REVIEW_RESULT.json` modelled exactly the
rejected combination: `"verdict": "PASS"` beside an open HIGH finding. The reviewer copied the
canonical shape it was given.

Note what the reducer actually keys on: only PASS vs non-PASS. `contested` and `occurrences`
are supervisor-derived across attempts and are not reviewer-authorable — the finding schema is
closed-world — so the only rule the reviewer controls is "PASS iff no open CRITICAL or HIGH".
"""
import json
import unittest
from pathlib import Path

from buildctl.supervisor.review import reduce_review

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
TEMPLATE = TARGET / "templates" / "REVIEW_RESULT.json"
PROMPT = TARGET / "prompts" / "reviews" / "claude-adversarial-review.md"

POLICY = {"max_remediation_attempts": 2, "debate_enabled": True, "max_debate_attempts": 1}
FRESH = {"remediation": 0, "debate": 0}


def finding(severity="HIGH", kind="correctness", status="open", **extra):
    base = {"id": "f1", "severity": severity, "kind": kind, "path": "src/x.py", "line": 1,
            "claim_key": "k", "claim": "c", "rationale": "r", "required_change": "rc",
            "evidence_digest": "d", "violated_invariant": "i", "treatment": "fix", "status": status}
    base.update(extra)
    return base


def reduce(verdict, findings, *, policy=None, attempts=None, mechanical_passed=True):
    return reduce_review(report={"verdict": verdict}, findings=findings,
                         mechanical_passed=mechanical_passed, coverage_valid=True,
                         policy=policy or POLICY, attempts=attempts or FRESH,
                         budget_available=True, approvals_valid=True)


class ReportedFailureTests(unittest.TestCase):
    def test_pass_with_an_open_high_finding_is_the_reported_block(self):
        transition = reduce("PASS", [finding(severity="HIGH")])
        self.assertEqual(transition.disposition, "HUMAN_ESCALATION")
        self.assertEqual(transition.rule_id, "R1_VERDICT_MISMATCH")
        self.assertEqual(transition.rationale, "unsafe advisory verdict mismatch")

    def test_the_reported_shape_reproduces_exactly(self):
        """Two HIGH plus twelve MEDIUM, all open, verdict PASS — the observed report."""
        findings = ([finding(severity="HIGH", kind="security"),
                     finding(severity="HIGH", kind="correctness")]
                    + [finding(severity="MEDIUM") for _ in range(12)])
        self.assertEqual(reduce("PASS", findings).rule_id, "R1_VERDICT_MISMATCH")

    def test_a_consistent_verdict_self_heals_into_remediation(self):
        """The near-miss: the same findings with an honest verdict route to REMEDIATE."""
        findings = [finding(severity="HIGH", kind="security"), finding(severity="MEDIUM")]
        transition = reduce("REMEDIATE", findings)
        self.assertEqual(transition.disposition, "REMEDIATE")
        self.assertEqual(transition.rule_id, "R5")

    def test_pass_with_an_open_critical_finding_is_blocked(self):
        self.assertEqual(reduce("PASS", [finding(severity="CRITICAL")]).rule_id, "R1_VERDICT_MISMATCH")


class PassIsAllowedWhenNothingBlocksTests(unittest.TestCase):
    def test_pass_with_only_medium_and_low_findings_advances(self):
        findings = [finding(severity="MEDIUM"), finding(severity="LOW")]
        transition = reduce("PASS", findings)
        self.assertEqual(transition.disposition, "ADVANCE")
        self.assertEqual(transition.rule_id, "R7")

    def test_pass_with_resolved_high_findings_advances(self):
        transition = reduce("PASS", [finding(severity="HIGH", status="resolved")])
        self.assertEqual(transition.disposition, "ADVANCE")

    def test_a_restrictive_verdict_without_a_blocking_finding_is_an_unexplained_hold(self):
        """The opposite failure the prompt must also prevent."""
        transition = reduce("REMEDIATE", [finding(severity="MEDIUM")])
        self.assertEqual(transition.rule_id, "R1_UNEXPLAINED_HOLD")


class OpenStatusTests(unittest.TestCase):
    def test_still_open_and_unverified_both_count_as_open(self):
        for status in ("open", "still_open", "unverified"):
            self.assertEqual(reduce("PASS", [finding(severity="HIGH", status=status)]).rule_id,
                             "R1_VERDICT_MISMATCH", f"status={status} must block PASS")

    def test_only_resolved_clears_a_high_finding(self):
        self.assertEqual(reduce("PASS", [finding(severity="HIGH", status="resolved")]).disposition,
                         "ADVANCE")


class TemplateIsSelfConsistentTests(unittest.TestCase):
    """The template is the shape the reviewer copies; an invalid example teaches the failure."""

    def setUp(self):
        self.template = json.loads(TEMPLATE.read_text())

    def test_the_template_verdict_agrees_with_its_own_example_findings(self):
        transition = reduce(self.template["verdict"], self.template["findings"])
        self.assertNotIn(transition.rule_id, {"R1_VERDICT_MISMATCH", "R1_UNEXPLAINED_HOLD"},
                         "the canonical template models a report the reducer rejects")

    def test_the_template_does_not_pair_pass_with_an_open_blocking_finding(self):
        blocking = [f for f in self.template["findings"]
                    if f["severity"] in {"CRITICAL", "HIGH"} and f["status"] != "resolved"]
        if blocking:
            self.assertNotEqual(self.template["verdict"], "PASS")

    def test_the_template_verdict_is_a_permitted_value(self):
        self.assertIn(self.template["verdict"], {"PASS", "REMEDIATE", "DEBATE", "HUMAN_ESCALATION"})


class PromptStatesTheContractTests(unittest.TestCase):
    """The gap was silence: the reviewer prompt never mentioned `verdict`."""

    def setUp(self):
        self.text = PROMPT.read_text()

    def test_the_prompt_documents_the_verdict_field(self):
        self.assertIn("verdict", self.text)

    def test_the_prompt_lists_every_permitted_verdict(self):
        for value in ("PASS", "REMEDIATE", "DEBATE", "HUMAN_ESCALATION"):
            self.assertIn(value, self.text)

    def test_the_prompt_states_the_binding_pass_rule(self):
        self.assertRegex(self.text, r"(?is)Return .PASS.*only.*when no open finding has severity")

    def test_the_prompt_warns_about_both_rejection_directions(self):
        self.assertIn("unsafe verdict mismatch", self.text)
        self.assertIn("unexplained hold", self.text)

    def test_the_prompt_says_medium_and_low_do_not_block(self):
        self.assertRegex(self.text, r"(?is)`MEDIUM`\s+and\s+`LOW`\s+findings\s+do\s+\*\*not\*\*\s+block")

    def test_the_prompt_defines_which_statuses_count_as_open(self):
        for status in ("still_open", "unverified", "resolved"):
            self.assertIn(status, self.text)


class UnrelatedRulesUnaffectedTests(unittest.TestCase):
    def test_failed_mechanical_gates_still_route_to_remediation_first(self):
        transition = reduce("REMEDIATE", [], mechanical_passed=False)
        self.assertEqual(transition.rule_id, "R2")

    def test_a_missing_report_still_escalates(self):
        transition = reduce_review(report=None, findings=[], mechanical_passed=True,
                                   coverage_valid=True, policy=POLICY, attempts=FRESH,
                                   budget_available=True, approvals_valid=True)
        self.assertEqual(transition.rule_id, "R1")

    def test_incomplete_coverage_still_escalates(self):
        transition = reduce_review(report={"verdict": "PASS"}, findings=[], mechanical_passed=True,
                                   coverage_valid=False, policy=POLICY, attempts=FRESH,
                                   budget_available=True, approvals_valid=True)
        self.assertEqual(transition.rule_id, "R1")

    def test_an_exhausted_budget_still_fails_closed_on_advance(self):
        """Observed with a non-PASS verdict, because the mismatch check runs afterwards."""
        transition = reduce_review(report={"verdict": "HUMAN_ESCALATION"}, findings=[],
                                   mechanical_passed=True, coverage_valid=True, policy=POLICY,
                                   attempts=FRESH, budget_available=False, approvals_valid=True)
        self.assertEqual(transition.disposition, "HUMAN_ESCALATION")
        self.assertEqual(transition.rule_id, "R7_FAIL_CLOSED")

    def test_the_mismatch_check_masks_the_underlying_rule_when_the_verdict_is_pass(self):
        """Pre-existing behaviour, recorded rather than changed: a PASS verdict over a
        fail-closed advance is reported as a verdict mismatch, not as R7_FAIL_CLOSED. The
        phase still stops, so the outcome is safe; only the stated reason is less specific."""
        transition = reduce_review(report={"verdict": "PASS"}, findings=[], mechanical_passed=True,
                                   coverage_valid=True, policy=POLICY, attempts=FRESH,
                                   budget_available=False, approvals_valid=True)
        self.assertEqual(transition.disposition, "HUMAN_ESCALATION")
        self.assertEqual(transition.rule_id, "R1_VERDICT_MISMATCH")

    def test_a_repeated_high_finding_still_routes_to_debate(self):
        """`occurrences` is supervisor-derived across attempts, not reviewer-authored."""
        transition = reduce("DEBATE", [finding(severity="HIGH", occurrences=2)])
        self.assertEqual(transition.disposition, "DEBATE")
        self.assertEqual(transition.rule_id, "R4")
