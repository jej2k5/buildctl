"""The implementer's reported status decides whether a phase continues.

Reported on a governed repository: Phase 1's implementer returned `status: blocked` (the
upstream contract it needed did not exist). buildctl validated the field, then reviewed,
merged and recorded the phase complete anyway. Phase 2's prompt said "complete preceding
phases first", so every Phase 2 attempt came back blocked too, was rejected by review, and
the build hit MAX_CONSECUTIVE_FAILURES_EXHAUSTED after $39 of agent calls.

A `blocked` result now stops before review as a wait, and `reopen` takes back a phase that
was recorded complete when it was not.
"""
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor, parser
from buildctl.supervisor import levers
from buildctl.supervisor.inspect import explain
from buildctl.supervisor.state import BuildState, StateStore

CONFIG = {"limits": {"max_consecutive_failures": 3}, "checkpoints": {"before_phases": [], "mvp_completion": 99}}
COMPLETE = {"status": "complete", "summary": "done", "risks": [], "architecture_questions": [],
            "files_changed": [], "tests": []}


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = StateStore(self.root)
        self.supervisor = Supervisor.__new__(Supervisor)
        self.supervisor.store, self.supervisor.control_root = self.store, self.root
        self.supervisor.config = CONFIG

    def state(self, **values) -> BuildState:
        values.setdefault("current_phase", 2)
        values.setdefault("phase_attempts", {"2": 1})
        state = BuildState(governance_sha="gov", **values)
        self.store.save(state)
        return state


class ReportedStatusTests(Harness):
    def test_a_complete_result_continues_unchanged(self):
        state = self.state()
        self.assertIs(self.supervisor._implementer_result(state, COMPLETE), COMPLETE)
        self.assertEqual(self.store.load().status, "idle")

    def test_a_blocked_result_stops_as_a_wait(self):
        state = self.state()
        result = dict(COMPLETE, status="blocked", summary="prerequisite missing",
                      risks=["acceptance unsatisfied", "Unblock by shipping the upstream contract"],
                      architecture_questions=["Which revision supplies it?"])
        with self.assertRaises(HumanEscalation) as raised:
            self.supervisor._implementer_result(state, result)
        self.assertEqual(str(raised.exception), "PHASE_BLOCKED")
        saved = self.store.load()
        self.assertEqual(saved.status, "human_escalation")
        self.assertEqual(saved.consecutive_failures, 0, "a blocked phase is a decision, not a failure")
        self.assertTrue(saved.escalation_detail.startswith("Unblock by shipping the upstream contract"),
                        "the unblock action must survive truncation")
        self.assertIn("phase_result_refused", [entry["event"] for entry in saved.history])
        self.assertTrue(levers.is_waiting(saved))
        self.assertEqual(explain("PHASE_BLOCKED")["aspect"], "caution")
        self.assertIn("prerequisite", explain("PHASE_BLOCKED")["meaning"])

    def test_blocked_results_never_reach_the_failure_ceiling(self):
        state = self.state(consecutive_failures=2)
        with self.assertRaises(HumanEscalation) as raised:
            self.supervisor._implementer_result(state, dict(COMPLETE, status="blocked"))
        self.assertEqual(str(raised.exception), "PHASE_BLOCKED")

    def test_a_failed_result_stops_and_counts(self):
        state = self.state()
        with self.assertRaises(HumanEscalation) as raised:
            self.supervisor._implementer_result(state, dict(COMPLETE, status="failed", summary="tests broke"))
        self.assertEqual(str(raised.exception), "PHASE_FAILED")
        self.assertEqual(self.store.load().consecutive_failures, 1)

    def test_retry_is_the_way_out_of_a_blocked_stop(self):
        state = self.state()
        with self.assertRaises(HumanEscalation):
            self.supervisor._implementer_result(state, dict(COMPLETE, status="blocked"))
        self.supervisor._release_phase_baseline = lambda phase: None
        self.supervisor._phase_deadline = lambda: None
        self.supervisor.retry()
        self.assertEqual(self.store.load().status, "idle")


class CallSiteTests(unittest.TestCase):
    def test_every_implementer_result_is_checked(self):
        """Implement, each remediation, each ADR implementation and both debate resumptions."""
        source = inspect.getsource(Supervisor._run_phase)
        self.assertNotIn("phase_result = self._agent_operation(", source)
        self.assertNotIn("phase_result = self._consume", source)
        self.assertEqual(source.count("phase_result = self._implementer_result(state, "), 6)


class ReopenTests(Harness):
    def test_reopen_takes_back_the_last_complete_phase(self):
        self.state(current_phase=None, completed_phases=[0, 1], phase_attempts={"1": 1},
                   remediation_attempts={"1": 2}, semantic_review_attempts={"1": 3})
        self.supervisor.reopen(1)
        state = self.store.load()
        self.assertEqual(state.completed_phases, [0])
        self.assertEqual(state.phase_attempts["1"], 1, "attempt numbers keep counting: operation ids embed them")
        self.assertNotIn("1", state.remediation_attempts)
        self.assertNotIn("1", state.semantic_review_attempts)
        self.assertEqual(state.history[-1]["event"], "phase_reopened")

    def test_reopen_refuses_with_later_complete_phases(self):
        self.state(current_phase=None, completed_phases=[0, 1, 2])
        with self.assertRaisesRegex(RuntimeError, "later complete phases"):
            self.supervisor.reopen(1)

    def test_reopen_refuses_an_incomplete_phase(self):
        self.state(current_phase=None, completed_phases=[0])
        with self.assertRaisesRegex(RuntimeError, "not complete"):
            self.supervisor.reopen(1)

    def test_reopen_refuses_unless_idle(self):
        self.state(current_phase=None, completed_phases=[0, 1], status="human_escalation")
        with self.assertRaisesRegex(RuntimeError, "idle"):
            self.supervisor.reopen(1)

    def test_the_command_requires_confirmation(self):
        args = parser().parse_args(["reopen", "--phase", "1", "--confirm", "1"])
        self.assertEqual((args.phase, args.confirm), (1, 1))


if __name__ == "__main__":
    unittest.main()
