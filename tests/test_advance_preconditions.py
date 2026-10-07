"""The ADVANCE conjunction is evaluated, not asserted.

The supervisor used to call the reducer with `budget_available=True, approvals_valid=True` as
literals, so the reducer's R7_FAIL_CLOSED branch was unreachable from its only caller and
`BudgetLedger` was never consulted before a phase advanced. These tests pin the real
evaluation and the reservation ordering that came with it.
"""
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor.budget import BudgetLedger
from buildctl.supervisor.operations import PaidOperation
from buildctl.supervisor.state import ApprovalRequirement, BuildState, StateStore


TREE = "tree-current"
CONFIG = {"limits": {"max_consecutive_failures": 5, "max_daily_cost_usd": 10.0},
          "checkpoints": {"before_phases": [7], "mvp_completion": 99},
          "operation_cost_usd": {"implementation": 4.0, "review": 4.0, "remediation": 4.0, "debate": 4.0},
          "roles": {"implementer": {"auth_mode": "none", "network": False}}}
PLAIN = SimpleNamespace(number=1)
GATED = SimpleNamespace(number=7)


class StubGit:
    def __init__(self, tree=TREE):
        self.tree = tree

    def tree_hash(self):
        return self.tree


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = StateStore(self.root)
        self.supervisor = Supervisor.__new__(Supervisor)
        self.supervisor.store, self.supervisor.control_root = self.store, self.root
        self.supervisor.config, self.supervisor.git = CONFIG, StubGit()
        self.supervisor.budget = BudgetLedger(self.root, CONFIG["limits"]["max_daily_cost_usd"])

    def state(self, **values) -> BuildState:
        state = BuildState(governance_sha="gov", phase_attempts={"1": 1, "7": 1}, **values)
        self.store.save(state)
        return state

    def spend(self, *amounts: float) -> None:
        for index, amount in enumerate(amounts):
            self.supervisor.budget.reserve("implementation", 1, f"seed-{index}", amount)

    def approve(self, state: BuildState, tree=TREE, attempt=1) -> None:
        scope = {"checkpoint": "phase-7", "phase": 7, "phase_attempt": attempt,
                 "governance_sha": "gov", "tree_hash": tree}
        state.history.append({"at": "t", "event": "checkpoint_approved", "scope": scope})


class BudgetPredicateTests(Harness):
    def test_an_untouched_ledger_is_available(self):
        self.assertEqual(self.supervisor._advance_preconditions(self.state(), PLAIN), (True, True))

    def test_spend_exactly_at_the_limit_still_advances(self):
        self.spend(4.0, 4.0, 2.0)
        self.assertTrue(self.supervisor._advance_preconditions(self.state(), PLAIN)[0])

    def test_spend_over_the_limit_blocks_advance(self):
        # reserve() never overshoots, so write a late entry directly, as a second process could.
        self.spend(4.0, 4.0)
        with self.supervisor.budget.path.open("a") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "provider": "x",
                                     "phase": 1, "operation_id": "late", "cost_usd": 5.0}) + "\n")
        self.assertFalse(self.supervisor._advance_preconditions(self.state(), PLAIN)[0])

    def test_a_corrupt_ledger_blocks_advance(self):
        self.supervisor.budget.path.write_text("not json\n")
        self.assertFalse(self.supervisor._advance_preconditions(self.state(), PLAIN)[0])


class ApprovalPredicateTests(Harness):
    def test_a_pending_approval_blocks_advance(self):
        pending = ApprovalRequirement("phase-7", 7, 1, TREE, "gov", 0, "nonce")
        self.assertFalse(self.supervisor._advance_preconditions(self.state(pending_approval=pending), PLAIN)[1])

    def test_a_phase_without_a_checkpoint_needs_no_approval(self):
        self.assertTrue(self.supervisor._advance_preconditions(self.state(), PLAIN)[1])

    def test_a_gated_phase_without_its_approval_is_blocked(self):
        self.assertFalse(self.supervisor._advance_preconditions(self.state(), GATED)[1])

    def test_a_gated_phase_with_a_matching_approval_advances(self):
        state = self.state(); self.approve(state)
        self.assertTrue(self.supervisor._advance_preconditions(state, GATED)[1])

    def test_an_approval_for_a_different_tree_is_blocked(self):
        state = self.state(); self.approve(state, tree="tree-before-someone-committed")
        self.assertFalse(self.supervisor._advance_preconditions(state, GATED)[1])

    def test_an_approval_for_a_previous_attempt_is_blocked(self):
        state = self.state(); self.approve(state, attempt=0)
        self.assertFalse(self.supervisor._advance_preconditions(state, GATED)[1])


class CallSiteTests(unittest.TestCase):
    def test_the_reducer_receives_evaluated_predicates(self):
        source = inspect.getsource(Supervisor._run_phase)
        self.assertIn("budget_available=budget_available, approvals_valid=approvals_valid", source)
        self.assertNotIn("budget_available=True", source)
        self.assertNotIn("approvals_valid=True", source)

    def test_fail_closed_has_its_own_escalation(self):
        source = inspect.getsource(Supervisor._run_phase)
        self.assertIn('"ADVANCE_PRECONDITIONS_UNMET"', source)


class ReservationOrderTests(Harness):
    def test_a_refused_reservation_escalates_and_records_nothing(self):
        self.spend(4.0, 4.0)                           # 8 of 10 spent; the next 4 does not fit
        state = self.state(workspace=str(self.root))
        launched = []
        with self.assertRaises(HumanEscalation) as raised:
            self.supervisor._agent_operation(state, PLAIN, "implement", PaidOperation.IMPLEMENTATION,
                                             self.root / "out.json", lambda timeout: launched.append(1))
        self.assertEqual(str(raised.exception), "DAILY_BUDGET_UNAVAILABLE")
        self.assertEqual(launched, [], "nothing may launch once the reservation is refused")
        self.assertEqual(self.store.load().operations, {}, "a refused reservation left an operation behind")
        self.assertEqual(self.store.load().status, "human_escalation")


if __name__ == "__main__":
    unittest.main()
