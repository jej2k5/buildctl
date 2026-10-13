"""An interactive debate is a wait for a human, not an agent failure.

With `roles.arbiter.mode: interactive` the arbiter writes `DEBATE_REQUEST.json` and stops. That
used to surface as AGENT_OPERATION_FAILED, so the operator was told to "fix the cause" of
something that had worked exactly as configured, and each debate counted toward
`max_consecutive_failures`. It now stops at caution with its own code, names the request, and
leaves the failure streak alone.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor import levers
from buildctl.supervisor.agents import AgentError
from buildctl.supervisor.budget import BudgetLedger, PaidOperation
from buildctl.supervisor.debate import DebateAdapter, DebateRequiresHuman
from buildctl.supervisor.inspect import explain
from buildctl.supervisor.state import BuildState, StateStore

from .test_levers_and_inspect import by_action

EVIDENCE = {"digest": "e", "delta_id": "d", "governance_sha": "g", "base_sha": "b"}


def supervisor_for(root: Path) -> Supervisor:
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.store = StateStore(root / "control")
    supervisor.control_root = root / "control"
    supervisor.root = root
    supervisor.config = {"roles": {"arbiter": {"auth_mode": "none", "network": False, "mode": "interactive"}},
                         "limits": {"max_consecutive_failures": 1},
                         "operation_cost_usd": {PaidOperation.DEBATE.value: 0.0}}
    supervisor.budget = BudgetLedger(root / "control", 10)
    return supervisor


class DebateRequiresHumanTests(unittest.TestCase):
    def test_the_adapter_names_the_request_it_wrote(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "DEBATE_RESULT-1.json"
            adapter = DebateAdapter(None, Path(directory), {"roles": {"arbiter": {"mode": "interactive"}}})
            with self.assertRaises(DebateRequiresHuman) as caught:
                adapter.run(SimpleNamespace(number=2), [], EVIDENCE, output, 1)
            self.assertIsInstance(caught.exception, AgentError)
            self.assertEqual(caught.exception.request, output.with_name("DEBATE_REQUEST.json"))
            self.assertTrue(caught.exception.request.is_file())

    def test_the_supervisor_waits_at_caution_without_counting_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = supervisor_for(root)
            deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            state = BuildState(status="running", current_phase=2, phase_attempts={"2": 1}, deadline_at=deadline)
            supervisor.store.save(state)
            output = root / "control/artifacts/phase-2/DEBATE_RESULT-1.json"
            adapter = DebateAdapter(None, root, supervisor.config)
            # max_consecutive_failures is 1, so a counted failure would become a ceiling here.
            with self.assertRaises(HumanEscalation):
                supervisor._agent_operation(state, SimpleNamespace(number=2), "debate-1", PaidOperation.DEBATE,
                                            output, lambda timeout: adapter.run(SimpleNamespace(number=2), [],
                                                                                EVIDENCE, output, timeout))
            self.assertEqual(state.escalation_code, "DEBATE_REQUIRES_HUMAN")
            self.assertEqual(state.escalation_detail, str(output.with_name("DEBATE_REQUEST.json")))
            self.assertEqual(state.consecutive_failures, 0)
            operation = state.operations["phase-2:debate-1:1"]
            self.assertEqual(operation.status, "failed")   # closed, so retry is not blocked as unknown
            self.assertEqual(operation.output_path, str(output))
            self.assertTrue(levers.is_waiting(state))
            self.assertEqual(explain(state.escalation_code)["aspect"], "caution")
            self.assertTrue(by_action(state)["retry"]["enabled"])
            self.assertEqual(json.loads(output.with_name("DEBATE_REQUEST.json").read_text())["output_path"],
                             str(output))

    def test_a_real_failure_still_counts_and_shows_at_danger(self):
        state = BuildState(status="human_escalation", escalation_code="AGENT_OPERATION_FAILED")
        self.assertFalse(levers.is_waiting(state))
        self.assertEqual(explain("AGENT_OPERATION_FAILED")["aspect"], "danger")


if __name__ == "__main__":
    unittest.main()
