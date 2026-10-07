"""`buildctl phase N` on a finished phase, and the retired operation fields.

`run(only=N)` skipped the completed-phase filter, so re-running a finished phase started a new
attempt on top of the accepted one and appended N to completed_phases a second time. Because
"complete" was judged by list length, a duplicate could declare the build finished early.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import Supervisor
from buildctl.supervisor.state import BuildState, StateStore


PHASES = [SimpleNamespace(number=0), SimpleNamespace(number=1), SimpleNamespace(number=2)]


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = StateStore(self.root)
        self.supervisor = Supervisor.__new__(Supervisor)
        self.supervisor.store, self.supervisor.dry_run, self.supervisor.phases = self.store, False, PHASES
        self.ran = []
        self.supervisor._run_phase = lambda state, phase: self.ran.append(phase.number)

    def save(self, **values) -> None:
        self.store.save(BuildState(**values))


class RerunTests(Harness):
    def test_a_completed_phase_is_refused(self):
        self.save(completed_phases=[0, 1])
        with self.assertRaisesRegex(RuntimeError, "phase 1 is already complete"):
            self.supervisor.run(only=1)
        self.assertEqual(self.ran, [])

    def test_the_refusal_leaves_state_untouched(self):
        self.save(completed_phases=[0, 1])
        generation = self.store.load().generation
        with self.assertRaises(RuntimeError):
            self.supervisor.run(only=1)
        state = self.store.load()
        self.assertEqual((state.generation, state.status), (generation, "idle"))

    def test_an_incomplete_phase_still_runs(self):
        self.save(completed_phases=[0])
        self.supervisor.run(only=2)
        self.assertEqual(self.ran, [2])

    def test_an_unknown_phase_is_still_refused(self):
        self.save()
        with self.assertRaisesRegex(RuntimeError, "unknown phase: 9"):
            self.supervisor.run(only=9)


class CompletionTests(Harness):
    def test_a_duplicate_entry_does_not_declare_the_build_complete(self):
        # Three entries for three phases, but phase 2 has never run.
        self.save(completed_phases=[0, 1, 1])
        self.supervisor._run_phase = lambda state, phase: None
        self.supervisor.run(until=1)
        self.assertEqual(self.store.load().status, "idle")

    def test_every_phase_done_is_complete(self):
        self.save(completed_phases=[0, 1])
        self.supervisor._run_phase = lambda state, phase: state.completed_phases.append(phase.number)
        self.supervisor.run()
        self.assertEqual(self.store.load().status, "complete")

    def test_finishing_a_phase_twice_records_it_once(self):
        self.save()
        state = self.store.load()
        self.supervisor.git = SimpleNamespace(publish=lambda phase: "integrated:main")
        self.supervisor.config = {"checkpoints": {"mvp_completion": 99}}
        phase = SimpleNamespace(number=1)
        self.supervisor._finish_phase(state, phase, "build/phase-1")
        self.supervisor._finish_phase(state, phase, "build/phase-1")
        self.assertEqual(self.store.load().completed_phases, [1])


class RetiredOperationFieldTests(unittest.TestCase):
    def state_with(self, **extra) -> Path:
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = StateStore(root); store.save(BuildState())
        value = json.loads(store.path.read_text())
        value["operations"] = {"op": {"id": "op", "kind": "implement", "phase": 1, "status": "completed", **extra}}
        store.path.write_text(json.dumps(value))
        return root

    def test_state_written_by_the_previous_supervisor_still_loads(self):
        root = self.state_with(attempt=1, input_digest="")
        operation = StateStore(root).load().operations["op"]
        self.assertEqual(operation.status, "completed")
        self.assertFalse(hasattr(operation, "input_digest"))

    def test_any_other_unknown_field_is_still_an_error(self):
        root = self.state_with(surprise=True)
        with self.assertRaisesRegex(RuntimeError, "invalid canonical state"):
            StateStore(root).load()


if __name__ == "__main__":
    unittest.main()
