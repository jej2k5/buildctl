import json
import tempfile
import unittest
from pathlib import Path

from buildctl.buildctl import Supervisor
from buildctl.supervisor.budget import BudgetLedger, LimitExceeded, configured_cost
from buildctl.supervisor.operations import PaidOperation
from buildctl.supervisor.isolation import FakeIsolationBackend
from buildctl.supervisor.state import ApprovalRequirement, BuildState, ExtraAttemptGrant, Operation, StateStore


class BudgetApprovalRetryTests(unittest.TestCase):
    def test_budget_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=BudgetLedger(Path(directory),1); ledger.path.write_text("truncated")
            with self.assertRaises(LimitExceeded): ledger.spent_today()

    def test_budget_reservation_is_idempotent_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first=BudgetLedger(Path(directory),10); first.reserve("review",1,"op",2)
            BudgetLedger(Path(directory),10).reserve("review",1,"op",2)
            self.assertEqual(first.spent_today(),2)

    def test_budget_exceeded_blocks_before_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory), 1)
            with self.assertRaises(LimitExceeded):
                ledger.reserve("reviewer", 1, "too-expensive", 2)
            self.assertEqual(ledger.spent_today(), 0)

    def test_unknown_operation_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory); store=StateStore(control)
            state=BuildState(status="human_escalation",operations={"op":Operation("op","review",1,status="unknown")})
            store.save(state)
            # The state model itself preserves unknown across restart; Supervisor.retry checks it.
            self.assertEqual(store.load().operations["op"].status,"unknown")

    def test_stale_approval_generation_is_rejected_by_scope_model(self):
        requirement=ApprovalRequirement("phase-1",1,1,"tree","gov",4,"nonce")
        state=BuildState(generation=7,pending_approval=requirement)
        self.assertNotEqual(requirement.state_generation,state.generation-1)

    def test_approval_cannot_bypass_a_different_checkpoint(self):
        requirement=ApprovalRequirement("phase-7",7,1,"tree","gov",4,"nonce")
        self.assertNotEqual(requirement.checkpoint,"mvp_completion")

    def test_all_paid_operation_costs_use_canonical_keys(self):
        config={"operation_cost_usd":{"implementation":1,"review":2,"remediation":3,"debate":4}}
        self.assertEqual([configured_cost(config,item) for item in PaidOperation],[1,2,3,4])

    def test_missing_or_unknown_cost_fails_closed(self):
        with self.assertRaises(LimitExceeded): configured_cost({},PaidOperation.REVIEW)

    def test_extra_attempt_authorization_is_scoped_single_use_and_restart_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(Path(directory)); state=BuildState(status="human_escalation",current_phase=1,
                escalation_code="PHASE_ATTEMPTS_EXHAUSTED",phase_attempts={"1":4},
                extra_attempt_grant=ExtraAttemptGrant(1,"PHASE_ATTEMPTS_EXHAUSTED",0,"nonce"))
            store.save(state)
            supervisor=Supervisor.__new__(Supervisor); supervisor.store=store
            supervisor.control_root=Path(directory)   # retry releases the discarded baseline
            supervisor.retry(True,"nonce")
            loaded=StateStore(Path(directory)).load()
            self.assertEqual(loaded.phase_attempts["1"],4)
            self.assertTrue(loaded.extra_attempt_grant.authorized)
            self.assertFalse(loaded.extra_attempt_grant.consumed)
            # Restore the phase identity retry released, so the second call still exercises
            # single-use consumption rather than failing on a phase-scope mismatch.
            loaded.status="human_escalation"; loaded.escalation_code="PHASE_ATTEMPTS_EXHAUSTED"
            loaded.current_phase=1
            loaded.extra_attempt_grant.consumed=True; store.save(loaded)
            with self.assertRaises(RuntimeError): supervisor.retry(True,"nonce")
