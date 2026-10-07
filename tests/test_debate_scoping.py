"""Regression tests for what an interactive debate is actually asked to decide.

Phase 3 routed to DEBATE on R4 and, with `roles.arbiter.mode: interactive`, wrote a
DEBATE_REQUEST.json for a human arbiter. The request carried **128 findings** — 76
superseded, 39 resolved, 6 open, 7 still_open — when exactly **3** satisfied R4 and were the
reason for the debate. `buildctl` was passing `list(state.findings.values())`, the whole
accumulated register.

Two harms: a human is asked to arbitrate a register instead of a question, and `write_adr`
stamps `finding_ids` for every finding handed over, so the decision record claims to have
decided things the debate never considered.

The R4 predicate now lives in one place, `is_strategic_finding`, used by both the reducer and
the debate hand-off, so the set debated is by construction the set that triggered the debate.
"""
import unittest

from buildctl.supervisor.review import (SUPERSEDED_STATUS, is_strategic_finding, reduce_review,
                                     strategic_findings)

POLICY = {"max_remediation_attempts": 2, "debate_enabled": True, "max_debate_attempts": 1}
FRESH = {"remediation": 0, "debate": 0}


def finding(severity="HIGH", kind="correctness", status="open", occurrences=1, **extra):
    base = {"id": "f", "severity": severity, "kind": kind, "path": "src/x.py", "line": 1,
            "claim_key": "k", "claim": "c", "rationale": "r", "required_change": "rc",
            "evidence_digest": "d", "violated_invariant": "i", "treatment": "fix",
            "status": status, "occurrences": occurrences}
    base.update(extra)
    return base


class StrategicPredicateTests(unittest.TestCase):
    def test_a_recurring_high_finding_is_strategic(self):
        """A defect that survived remediation and came back is a disagreement, not a bug."""
        self.assertTrue(is_strategic_finding(finding(severity="HIGH", occurrences=2)))

    def test_an_architectural_high_finding_is_strategic(self):
        self.assertTrue(is_strategic_finding(finding(severity="HIGH", kind="architecture")))

    def test_a_contested_finding_is_strategic_at_any_severity(self):
        self.assertTrue(is_strategic_finding(finding(severity="LOW", contested=True)))

    def test_a_first_occurrence_high_finding_is_not_strategic(self):
        self.assertFalse(is_strategic_finding(finding(severity="HIGH", occurrences=1)))

    def test_a_recurring_medium_finding_is_not_strategic(self):
        self.assertFalse(is_strategic_finding(finding(severity="MEDIUM", occurrences=5)))


class DebatedSetMatchesTheTriggerTests(unittest.TestCase):
    """The set handed to the arbiter must be exactly the set that caused the routing."""

    def test_only_the_triggering_findings_are_selected(self):
        register = [finding(severity="HIGH", occurrences=2, claim_key="recurring"),
                    finding(severity="HIGH", kind="architecture", claim_key="architectural"),
                    finding(severity="MEDIUM", claim_key="advisory"),
                    finding(severity="LOW", claim_key="minor")]
        selected = strategic_findings(register)
        self.assertEqual([f["claim_key"] for f in selected], ["recurring", "architectural"])

    def test_closed_findings_are_never_debated(self):
        for status in ("resolved", SUPERSEDED_STATUS):
            register = [finding(severity="HIGH", occurrences=2, status=status)]
            self.assertEqual(strategic_findings(register), [],
                             f"{status} findings must not reach the arbiter")

    def test_still_open_and_unverified_are_debatable(self):
        for status in ("still_open", "unverified"):
            register = [finding(severity="HIGH", occurrences=2, status=status)]
            self.assertEqual(len(strategic_findings(register)), 1, status)

    def test_the_reported_shape_narrows_from_the_whole_register(self):
        """128 accumulated findings, 3 of them strategic — the Phase 3 request."""
        register = ([finding(severity="MEDIUM", status=SUPERSEDED_STATUS) for _ in range(76)]
                    + [finding(severity="MEDIUM", status="resolved") for _ in range(39)]
                    + [finding(severity="MEDIUM", status="open") for _ in range(10)]
                    + [finding(severity="HIGH", occurrences=2, claim_key=f"strategic-{i}")
                       for i in range(3)])
        self.assertEqual(len(register), 128)
        self.assertEqual(len(strategic_findings(register)), 3)

    def test_selection_agrees_with_the_reducer_that_routed_to_debate(self):
        """One predicate, so the debated set cannot drift from the routing decision."""
        register = [finding(severity="HIGH", occurrences=2), finding(severity="MEDIUM")]
        transition = reduce_review(report={"verdict": "DEBATE"}, findings=register,
                                   mechanical_passed=True, coverage_valid=True, policy=POLICY,
                                   attempts=FRESH, budget_available=True, approvals_valid=True)
        self.assertEqual(transition.rule_id, "R4")
        self.assertTrue(strategic_findings(register),
                        "R4 fired, so the debated set must not be empty")

    def test_nothing_strategic_means_the_reducer_did_not_route_to_debate(self):
        register = [finding(severity="HIGH", occurrences=1)]
        transition = reduce_review(report={"verdict": "REMEDIATE"}, findings=register,
                                   mechanical_passed=True, coverage_valid=True, policy=POLICY,
                                   attempts=FRESH, budget_available=True, approvals_valid=True)
        self.assertEqual(transition.rule_id, "R5")
        self.assertEqual(strategic_findings(register), [])

    def test_ordering_is_stable(self):
        register = [finding(severity="HIGH", occurrences=2, claim_key=k) for k in ("a", "b", "c")]
        self.assertEqual([f["claim_key"] for f in strategic_findings(register)],
                         [f["claim_key"] for f in strategic_findings(register)])
