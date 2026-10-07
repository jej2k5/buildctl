"""The read model and its levers agree with what the supervisor will actually accept.

A front end renders `inspect()` and offers only enabled levers. If a lever said yes where the
kernel says no, or the other way round, the operator would be shown a choice that does not
exist. These tests hold the two together on the states that matter.
"""
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor import levers
from buildctl.supervisor.inspect import audit_events, explain, inspect
from buildctl.supervisor.state import ApprovalRequirement, BuildState, ExtraAttemptGrant, Operation, StateStore

from .helpers import git, repository

FACTS = {"trust_ok": True, "isolation_fresh": True, "bootstrapped": True, "lock_held": False, "budget_ok": True,
         "remaining_phases": [3, 4], "tree_hash": "tree", "head_sha": "a" * 40, "operation_outputs": {}}


def by_action(state: BuildState, **facts) -> dict:
    return {lever["action"]: lever for lever in levers.levers(state, {**FACTS, **facts})}


class LeverTests(unittest.TestCase):
    def test_idle_build_can_start_but_not_retry(self):
        result = by_action(BuildState(status="idle"))
        self.assertTrue(result["start"]["enabled"])
        self.assertFalse(result["retry"]["enabled"])
        self.assertNotIn("approve", result)

    def test_start_is_locked_by_each_failed_precondition(self):
        for fact, value in (("trust_ok", False), ("isolation_fresh", False), ("lock_held", True),
                            ("budget_ok", False), ("remaining_phases", [])):
            with self.subTest(fact=fact):
                start = by_action(BuildState(status="idle"), **{fact: value})["start"]
                self.assertFalse(start["enabled"])

    def test_agent_login_is_reported_but_never_locks(self):
        start = by_action(BuildState(status="idle"))["start"]
        auth = next(item for item in start["conditions"] if item["id"] == "auth")
        self.assertIsNone(auth["held"])
        self.assertTrue(start["enabled"])

    def test_checkpoint_offers_approval_bound_to_the_tree_and_refuses_retry(self):
        state = BuildState(status="human_escalation", generation=8, current_phase=2, phase_attempts={"2": 1},
                           escalation_code="CHECKPOINT_REQUIRED",
                           pending_approval=ApprovalRequirement("phase-2", 2, 1, "tree", "gov", 7, "n" * 32))
        result = by_action(state)
        self.assertTrue(result["approve"]["enabled"])
        self.assertEqual(result["approve"]["argv"], ["approve", "--nonce", "n" * 32])
        self.assertEqual(result["approve"]["confirm"]["kind"], "tree_hash")
        self.assertFalse(result["retry"]["enabled"])
        moved = by_action(state, tree_hash="other")["approve"]
        self.assertFalse(moved["enabled"])

    def test_stale_approval_is_locked(self):
        state = BuildState(status="human_escalation", generation=12, current_phase=2, phase_attempts={"2": 1},
                           pending_approval=ApprovalRequirement("phase-2", 2, 1, "tree", "gov", 7, "n" * 32))
        self.assertFalse(by_action(state)["approve"]["enabled"])

    def test_ceiling_offers_the_grant_instead_of_retry(self):
        state = BuildState(status="human_escalation", generation=5, current_phase=3,
                           escalation_code="REMEDIATION_ATTEMPTS_EXHAUSTED",
                           extra_attempt_grant=ExtraAttemptGrant(3, "REMEDIATION_ATTEMPTS_EXHAUSTED", 4, "g" * 32))
        result = by_action(state)
        self.assertNotIn("retry", result)
        self.assertTrue(result["grant"]["enabled"])
        self.assertEqual(result["grant"]["argv"][-1], "g" * 32)
        self.assertEqual(result["grant"]["confirm"]["value"], "3")

    def test_unknown_operation_requires_reconcile_not_retry(self):
        state = BuildState(status="human_escalation", generation=3, current_phase=1,
                           escalation_code="OPERATION_TIMEOUT_UNKNOWN",
                           operations={"phase-1:review-1:1": Operation("phase-1:review-1:1", "review-1", 1, "unknown")})
        result = levers.levers(state, FACTS)
        actions = {item["action"]: item for item in result}
        self.assertFalse(actions["retry"]["enabled"])
        self.assertFalse(actions["reconcile-accept"]["enabled"], "no output on disk")
        self.assertTrue(actions["reconcile-retry"]["enabled"])
        with_output = {item["action"]: item for item in levers.levers(
            state, {**FACTS, "operation_outputs": {"phase-1:review-1:1": True}})}
        self.assertTrue(with_output["reconcile-accept"]["enabled"])

    def test_a_run_that_died_offers_recovery(self):
        result = by_action(BuildState(status="running"))
        self.assertIn("recover", result)
        self.assertNotIn("start", result)
        live = by_action(BuildState(status="running"), lock_held=True)
        self.assertNotIn("recover", live)
        self.assertFalse(live["start"]["enabled"])

    def test_ceiling_codes_share_one_explanation(self):
        self.assertEqual(explain("PHASE_ATTEMPTS_EXHAUSTED")["remedy"], explain("REVIEW_FORMAT_ATTEMPTS_EXHAUSTED")["remedy"])
        self.assertIsNone(explain(None))


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temporary, self.repo = repository()
        self.addCleanup(self.temporary.cleanup)
        control = tempfile.TemporaryDirectory(); self.addCleanup(control.cleanup)
        self.control = Path(control.name)

    def test_untrusted_repository_degrades_instead_of_failing(self):
        model = inspect(self.repo, self.control)
        self.assertFalse(model["trust"]["ok"])
        self.assertFalse(model["bootstrapped"])
        self.assertEqual(model["phases"], [])
        self.assertEqual(model["repo"]["head_sha"], git(self.repo, "rev-parse", "HEAD"))
        trust = next(item for item in model["levers"] if item["action"] == "trust")
        self.assertTrue(trust["enabled"])
        self.assertEqual(trust["confirm"]["kind"], "sha")

    def test_reads_state_and_audit_without_taking_the_lock(self):
        store = StateStore(self.control)
        state = BuildState(status="idle")
        store.save(state)
        store.event(state, "paused")
        with store.lock():
            model = inspect(self.repo, self.control)
        self.assertEqual(model["audit"]["sequence"], 2)
        self.assertTrue(model["audit"]["ok"])
        self.assertEqual([item["event_type"] for item in audit_events(self.control, after=1)], ["paused"])

    def test_corrupt_audit_chain_withdraws_every_lever(self):
        store = StateStore(self.control)
        store.save(BuildState())
        (self.control / "audit.jsonl").write_text("{broken\n")
        model = inspect(self.repo, self.control)
        self.assertFalse(model["audit"]["ok"])
        self.assertEqual(model["levers"], [])


if __name__ == "__main__":
    unittest.main()
