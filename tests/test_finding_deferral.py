"""Regression tests for parking findings instead of destroying them.

Retirement was terminal. A finding whose file happened to be absent from one delta was
marked `superseded` and never adjudicated again, and the register lost defects nobody had
decided anything about. Two failures were paid for that, both observed:

*Within a phase.* Retirement ran on the first delta of phase 16 — the implementation
touched only `actions.py` — so five carry-over findings on `governance.py` and
`delegation.py` were retired at 14:35:08. A later remediation lap then edited
`governance.py`, which would have made those findings perfectly citable. They never came
back. One of them, `federation-kernel-not-bound-to-any-enforcement-path`, was the carry-over
phase 16 existed to discharge.

*Across phases.* `approvals-unauthenticated-and-perpetual` — approvals accepted on a bare
scope-string match, no lower time bound, no binding to the proposal — was retired at
15:26:59 because phase 17's delta did not include `actions.py`. At 15:32:36 the reviewer
independently reported the identical defect against `playbooks.py`, as a HIGH. The register
cannot carry a lesson it keeps deleting, and the implementer rebuilt the hole in a new file.

Absence from a delta is a statement about the delta, not about the defect.
"""
import unittest

from buildctl.supervisor.review import (CLOSED_FINDING_STATUSES, DEFERRED_STATUS,
                                     NON_BLOCKING_FINDING_STATUSES, accumulate_findings,
                                     open_finding_obligations, realign_findings_to_delta,
                                     reduce_review, stable_finding_id, strategic_findings)

POLICY = {"max_remediation_attempts": 2, "debate_enabled": True, "max_debate_attempts": 1}
FRESH = {"remediation": 0, "debate": 0}


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


def reduce(verdict, register):
    return reduce_review(report={"verdict": verdict}, findings=list(register.values()),
                         mechanical_passed=True, coverage_valid=True, policy=POLICY,
                         attempts=FRESH, budget_available=True, approvals_valid=True)


class TheReportedRegressionsTests(unittest.TestCase):
    def test_a_later_lap_that_touches_the_file_restores_the_finding(self):
        """Phase 16: parked against the implementation delta, citable again after a lap."""
        register = register_with(finding(path="src/relay/domains/governance.py",
                                         claim_key="federation-kernel-not-bound-to-any-enforcement-path",
                                         severity="MEDIUM"))
        # lap 1 -- implementation touched actions.py only
        parked, restored = realign_findings_to_delta(register, ["src/relay/domains/actions.py"])
        self.assertEqual((parked, restored), (("src/relay/domains/governance.py",), ()))
        self.assertEqual(list(register.values())[0]["status"], DEFERRED_STATUS)
        # lap 2 -- remediation edited governance.py, so the reviewer can adjudicate it again
        parked, restored = realign_findings_to_delta(
            register, ["src/relay/domains/actions.py", "src/relay/domains/governance.py"])
        self.assertEqual((parked, restored), ((), ("src/relay/domains/governance.py",)))
        self.assertEqual(list(register.values())[0]["status"], "open")

    def test_a_later_phase_that_touches_the_file_restores_the_finding(self):
        """Phase 17: the defect the register dropped and the reviewer had to rediscover."""
        register = register_with(finding(path="src/relay/domains/actions.py",
                                         claim_key="approvals-unauthenticated-and-perpetual",
                                         severity="MEDIUM"))
        realign_findings_to_delta(register, ["src/relay/domains/playbooks.py"])
        self.assertEqual(open_finding_obligations(register), [])
        # a later phase edits actions.py again
        realign_findings_to_delta(register, ["src/relay/domains/actions.py"])
        obligations = open_finding_obligations(register)
        self.assertEqual([item["claim_key"] for item in obligations],
                         ["approvals-unauthenticated-and-perpetual"])
        self.assertEqual(obligations[0]["status"], "open")

    def test_the_status_held_at_parking_time_is_what_comes_back(self):
        """`still_open` means the reviewer looked twice. Restoring it as `open` would erase
        that, and `is_strategic_finding` counts recurrence."""
        for held in ("open", "still_open", "unverified"):
            register = register_with(finding(path="src/a.py", claim_key=f"k-{held}", status=held))
            realign_findings_to_delta(register, ["src/b.py"])
            realign_findings_to_delta(register, ["src/a.py"])
            self.assertEqual(list(register.values())[0]["status"], held, held)


class ParkedFindingsDoNotBlockTests(unittest.TestCase):
    """The failure mode that would be worse than the bug: an undischargeable obligation.

    If a parked finding still blocked, the reducer would route to R5 or R4 forever while
    `validate_review` refused the reviewer any accepted way to cite the path.
    """

    def test_a_parked_high_does_not_block_advance(self):
        register = register_with(finding(path="src/gone.py", claim_key="g", severity="HIGH"))
        self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH")
        realign_findings_to_delta(register, ["src/here.py"])
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_a_parked_critical_does_not_escalate(self):
        register = register_with(finding(path="src/gone.py", claim_key="g", severity="CRITICAL"))
        realign_findings_to_delta(register, ["src/here.py"])
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_a_parked_architectural_high_does_not_route_to_debate(self):
        register = register_with(finding(path="src/gone.py", claim_key="g", severity="HIGH",
                                         kind="architecture"))
        self.assertEqual(reduce("FAIL", register).disposition, "DEBATE")
        realign_findings_to_delta(register, ["src/here.py"])
        self.assertEqual(strategic_findings(list(register.values())), [])
        self.assertEqual(reduce("PASS", register).disposition, "ADVANCE")

    def test_the_reviewer_is_never_asked_to_adjudicate_a_parked_finding(self):
        register = register_with(finding(path="src/gone.py", claim_key="g"))
        realign_findings_to_delta(register, ["src/here.py"])
        self.assertEqual(open_finding_obligations(register), [])

    def test_verdict_binding_stays_consistent_for_a_parked_finding(self):
        """The reviewer cannot see it, so it returns PASS; the reducer must agree."""
        register = register_with(finding(path="src/gone.py", claim_key="g", severity="HIGH"))
        realign_findings_to_delta(register, ["src/here.py"])
        self.assertEqual(open_finding_obligations(register), [])
        self.assertEqual(reduce("PASS", register).rule_id, "R7")

    def test_a_restored_finding_blocks_again(self):
        register = register_with(finding(path="src/gone.py", claim_key="g", severity="HIGH"))
        realign_findings_to_delta(register, ["src/here.py"])
        realign_findings_to_delta(register, ["src/gone.py"])
        self.assertEqual(reduce("PASS", register).rule_id, "R1_VERDICT_MISMATCH")


class ResolutionStillWinsTests(unittest.TestCase):
    def test_resolving_a_parked_finding_closes_it_for_good(self):
        """A fix landing while the finding is parked must not be undone by a later restore."""
        register = register_with(finding(path="src/a.py", claim_key="k"))
        realign_findings_to_delta(register, ["src/b.py"])
        accumulate_findings(register, {"findings": [finding(path="src/a.py", claim_key="k",
                                                            status="resolved")],
                                       "evidence_digest": "e2"}, "policy")
        for entry in register.values():
            self.assertEqual(entry["status"], "resolved")
        self.assertEqual(realign_findings_to_delta(register, ["src/a.py"]), ((), ()))
        self.assertTrue(all(entry["status"] == "resolved" for entry in register.values()))

    def test_parking_is_never_recorded_as_a_resolution(self):
        register = register_with(finding(path="src/a.py", claim_key="k"))
        realign_findings_to_delta(register, ["src/b.py"])
        entry = list(register.values())[0]
        self.assertNotIn(entry["status"], CLOSED_FINDING_STATUSES)
        self.assertNotIn("resolved_by_evidence", entry)


class IdempotenceTests(unittest.TestCase):
    def test_parking_twice_reports_the_path_once(self):
        register = register_with(finding(path="src/a.py", claim_key="k"))
        self.assertEqual(realign_findings_to_delta(register, ["src/b.py"])[0], ("src/a.py",))
        self.assertEqual(realign_findings_to_delta(register, ["src/b.py"]), ((), ()))

    def test_restoring_twice_reports_the_path_once(self):
        register = register_with(finding(path="src/a.py", claim_key="k"))
        realign_findings_to_delta(register, ["src/b.py"])
        self.assertEqual(realign_findings_to_delta(register, ["src/a.py"])[1], ("src/a.py",))
        self.assertEqual(realign_findings_to_delta(register, ["src/a.py"]), ((), ()))

    def test_a_superseded_finding_from_an_older_register_is_left_alone(self):
        """Registers written before deferral existed still carry the terminal status."""
        register = register_with(finding(path="src/a.py", claim_key="k"))
        list(register.values())[0]["status"] = "superseded"
        self.assertEqual(realign_findings_to_delta(register, ["src/a.py"]), ((), ()))
        self.assertEqual(list(register.values())[0]["status"], "superseded")

    def test_a_finding_without_a_usable_path_is_skipped(self):
        register = register_with(finding(path="src/a.py", claim_key="k"))
        list(register.values())[0]["path"] = None
        self.assertEqual(realign_findings_to_delta(register, ["src/b.py"]), ((), ()))


class NonBlockingSetIsTheSingleFilterTests(unittest.TestCase):
    """One set, used everywhere. Three hand-written filters is how they drift apart."""

    def test_every_blocking_decision_uses_the_same_set(self):
        self.assertEqual(NON_BLOCKING_FINDING_STATUSES, CLOSED_FINDING_STATUSES | {DEFERRED_STATUS})

    def test_no_blocking_filter_still_names_the_closed_set_alone(self):
        import inspect

        from buildctl.supervisor import review
        for function in (review.open_finding_obligations, review.strategic_findings,
                         review.reduce_review):
            source = inspect.getsource(function)
            self.assertNotIn("CLOSED_FINDING_STATUSES", source, function.__name__)
