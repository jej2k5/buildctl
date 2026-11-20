"""A retry tells the next implementer why the previous attempt failed.

replcrm phase 6, attempt 1: Codex edited src/destinations/application-crm/.env.example, the delta
refused it ("unsafe phase path"), and `retry` started attempt 2 with the same prompt and no reason.
A blind retry repeats the mistake. The failure now survives `retry` in `state.last_failure` and the
first implementation of the next attempt receives it as untrusted data.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.codex import CodexAdapter
from buildctl.supervisor.git import GitSafetyError
from buildctl.supervisor.state import BuildState, StateStore
from unittest import mock

from tests.test_retry_fresh_attempt import CONFIG, PHASE, StopPhase, StubGit, supervisor_for

DETAIL = "unsafe phase path: src/destinations/application-crm/.env.example"


def failing_supervisor(root: Path):
    supervisor = supervisor_for(root)
    supervisor.dry_run, supervisor.phases = False, [PHASE]

    def run_phase(state, phase):
        state.current_phase, state.phase_attempts[str(phase.number)] = phase.number, 1
        raise GitSafetyError(DETAIL)
    supervisor._run_phase = run_phase
    return supervisor


class RetryFailureFeedbackTests(unittest.TestCase):
    def test_failure_is_recorded_kept_across_retry_and_offered_to_the_next_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); StateStore(root).save(BuildState(completed_phases=[0]))
            supervisor = failing_supervisor(root)
            with self.assertRaises(GitSafetyError):
                supervisor.run()
            recorded = StateStore(root).load().last_failure
            self.assertEqual((recorded["phase"], recorded["attempt"], recorded["code"]), (1, 1, "UNEXPECTED_FAILURE"))
            self.assertIn(".env.example", recorded["detail"])
            supervisor.retry()
            state = StateStore(root).load()
            self.assertEqual(state.last_failure, recorded)
            text = supervisor._prior_failure(state, PHASE)
            self.assertIn("Attempt 1", text); self.assertIn(DETAIL, text)
            self.assertIsNone(supervisor._prior_failure(state, SimpleNamespace(number=2)))

    def test_the_real_run_phase_passes_it_to_the_first_implementation(self):
        """The wiring: a retried attempt's first codex.implement call receives the recorded failure."""
        class Git(StubGit):
            def manifest(self, workspace):
                return {}
        captured = {}

        class FakeCodex:
            def __init__(self, *args):
                pass

            def implement(self, phase, output, timeout, **kwargs):
                captured.update(kwargs)
                raise StopPhase("captured")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phase = SimpleNamespace(number=1, merge_policy="auto", max_remediation_attempts=1, name="p1")
            supervisor = supervisor_for(root, git=Git())
            supervisor.backend = supervisor.runner = supervisor.evidence = supervisor.snapshot = None
            supervisor.root = root
            supervisor._agent_operation = lambda state, phase, kind, paid, output, function, **kw: function(60)
            state = BuildState(completed_phases=[0], phase_attempts={"1": 1},
                               last_failure={"phase": 1, "attempt": 1, "code": "UNEXPECTED_FAILURE", "detail": DETAIL})
            with mock.patch("buildctl.buildctl.CodexAdapter", FakeCodex), \
                    mock.patch("buildctl.buildctl.ClaudeAdapter"), mock.patch("buildctl.buildctl.DebateAdapter"), \
                    mock.patch("buildctl.buildctl.RoleRunner"), mock.patch("buildctl.buildctl.IsolationContext"):
                with self.assertRaises(StopPhase):
                    supervisor._run_phase(state, phase)
            self.assertEqual(state.phase_attempts["1"], 2)
            self.assertIn(DETAIL, captured.get("prior_failure") or "")

    def test_the_implementer_prompt_carries_it_as_untrusted_data(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt_file = Path(directory) / "phase.md"; prompt_file.write_text("Build the thing.")
            phase = SimpleNamespace(required_files=[], commands=[], invariants=[], allowed_paths=["src/"],
                                    prompt=prompt_file)
            adapter = CodexAdapter(SimpleNamespace(config=CONFIG), Path(directory))
            plain = adapter.prompt(phase)
            told = adapter.prompt(phase, prior_failure=f"Attempt 1 failed with UNEXPECTED_FAILURE: {DETAIL}")
            self.assertNotIn("previous_attempt_failure", plain)
            self.assertIn('label="previous_attempt_failure"', told)
            self.assertIn(".env.example", told)

    def test_old_state_files_load_without_the_field(self):
        self.assertIsNone(BuildState.from_dict({"status": "idle"}).last_failure)


if __name__ == "__main__":
    unittest.main()
