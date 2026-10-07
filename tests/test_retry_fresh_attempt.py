"""Regression tests for retry leaving a phase stuck on its previous attempt.

Reported state after a failed Phase 1 and `./buildctl retry`:

    status: human_escalation -> idle
    current_phase: 1
    phase_step: clone_completed
    deadline_at: 2026-08-26T22:08:14Z        (already expired)
    phase_attempts: {'1': 1}

`_run_phase` stamps the attempt number, the base SHA and the wall-clock deadline only inside
`if state.current_phase != phase.number`. retry() cleared the escalation but left
`current_phase = 1`, so the next run skipped that initialisation entirely and inherited the
expired deadline — PHASE_DEADLINE_EXCEEDED before any work happened. Because operation ids
embed the attempt number, the same staleness also replayed the previous attempt's completed
operations instead of re-running them.

retry() now releases the active phase identity so the next run takes the normal new-attempt
path. It still must not bypass any ceiling, checkpoint or reconciliation requirement.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import Supervisor
from buildctl.supervisor.state import (ApprovalRequirement, BuildState, ExtraAttemptGrant, Operation,
                                    StateStore)

CONFIG = {"limits": {"max_phase_minutes": 90, "max_phase_attempts": 3, "max_consecutive_failures": 5},
          "checkpoints": {"before_phases": [], "mvp_completion": 99}}
PHASE = SimpleNamespace(number=1, merge_policy="auto", max_remediation_attempts=1)


class StopPhase(RuntimeError):
    """Halts _run_phase once the parts under test have run."""


class StubGit:
    """Enough of GitManager to drive phase initialisation without any agent or network."""

    def __init__(self, sha="basesha0000"):
        self.sha, self.clones = sha, []

    def current_sha(self):
        return self.sha

    def create_isolated_clone(self, base_sha, destination):
        Path(destination).mkdir(parents=True)
        self.clones.append(Path(destination))
        return destination

    def prepare_agent_workspace(self, workspace):
        pass

    def manifest(self, workspace):
        raise StopPhase("reached the agent stage")


def supervisor_for(directory: Path, git=None, config=None) -> Supervisor:
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.store = StateStore(directory)
    supervisor.control_root = directory
    supervisor.config = config or CONFIG
    supervisor.git = git or StubGit()
    return supervisor


def expired() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()


def failed_phase_one_state(workspace: str) -> BuildState:
    """Exactly the reported post-failure state."""
    return BuildState(status="human_escalation", current_phase=1, phase_step="clone_completed",
                      completed_phases=[0], phase_attempts={"1": 1}, deadline_at=expired(),
                      workspace=workspace, escalation_code="PHASE_DEADLINE_EXCEEDED",
                      escalation_detail="wall-clock phase deadline expired",
                      governance_sha="gov-sha", snapshot_digest="snap-digest",
                      consecutive_failures=1,
                      operations={"phase-1:implement:1": Operation("phase-1:implement:1", "implement",
                                                                   1, status="completed")})


class StaleDeadlineReproductionTests(unittest.TestCase):
    def test_the_bug_reproduces_without_the_reset(self):
        """Guards the diagnosis: keeping the phase identity reproduces the stale deadline."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = supervisor_for(root)
            state = failed_phase_one_state(str(root / "stale"))
            state.status = "idle"                      # what the old retry() left behind
            stale_deadline = state.deadline_at
            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertEqual(state.deadline_at, stale_deadline, "deadline was refreshed unexpectedly")
            self.assertEqual(state.phase_attempts["1"], 1, "attempt was incremented unexpectedly")

    def test_retry_then_run_gets_a_fresh_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stale = root / "stale-workspace"; stale.mkdir()
            store = StateStore(root); store.save(failed_phase_one_state(str(stale)))
            supervisor = supervisor_for(root)

            supervisor.retry()
            state = store.load()
            self.assertIsNone(state.current_phase, "retry must release the active phase identity")
            self.assertIsNone(state.deadline_at)
            self.assertEqual(state.phase_step, "pending")

            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertIsNotNone(state.deadline_at)
            remaining = (datetime.fromisoformat(state.deadline_at) - datetime.now(timezone.utc))
            self.assertGreater(remaining.total_seconds(), 0, "the new attempt inherited an expired deadline")
            self.assertLessEqual(remaining, timedelta(minutes=90))


class AttemptAccountingTests(unittest.TestCase):
    def test_the_attempt_increments_from_one_to_two_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor = supervisor_for(root)

            supervisor.retry()
            state = store.load()
            self.assertEqual(state.phase_attempts["1"], 1,
                             "retry itself must not fabricate an attempt")

            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertEqual(state.phase_attempts["1"], 2)

    def test_re_entering_the_same_running_phase_does_not_increment_again(self):
        """The increment belongs to the run that starts, not to every _run_phase call."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor = supervisor_for(root)
            supervisor.retry()
            state = store.load()
            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertEqual(state.phase_attempts["1"], 2)
            first_deadline = state.deadline_at
            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertEqual(state.phase_attempts["1"], 2, "attempt incremented twice for one attempt")
            self.assertEqual(state.deadline_at, first_deadline)


class WorkspaceLifecycleTests(unittest.TestCase):
    def test_the_stale_workspace_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stale = root / "stale-workspace"; stale.mkdir()
            (stale / "leftover.txt").write_text("from the failed attempt\n")
            store = StateStore(root); store.save(failed_phase_one_state(str(stale)))
            git = StubGit()
            supervisor = supervisor_for(root, git=git)

            supervisor.retry()
            state = store.load()
            self.assertIsNone(state.workspace, "retry must release the discarded workspace")

            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            self.assertIsNotNone(state.workspace)
            self.assertNotEqual(Path(state.workspace), stale)
            self.assertEqual(len(git.clones), 1, "a fresh attempt must clone again")
            self.assertTrue(stale.is_dir(), "retry must not delete the old workspace from disk")

    def test_the_baseline_of_the_discarded_workspace_is_released(self):
        """A retained baseline describes the previous clone and would mis-attribute the delta."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = root / "phase-1-baseline.json"
            baseline.write_text(json.dumps({"src/old.py": "deadbeef"}))
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor_for(root).retry()
            self.assertFalse(baseline.exists())

    def test_a_committed_phase_keeps_its_identity_and_workspace(self):
        """It must resume at publication rather than rebuild, but still needs a live deadline."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = root / "phase-1-baseline.json"; baseline.write_text("{}")
            state = failed_phase_one_state(str(root / "committed-workspace"))
            state.phase_step = "committed"; state.active_branch = "build/phase-1"
            store = StateStore(root); store.save(state)

            supervisor_for(root).retry()
            reloaded = store.load()
            self.assertEqual(reloaded.current_phase, 1)
            self.assertEqual(reloaded.phase_step, "committed")
            self.assertEqual(reloaded.active_branch, "build/phase-1")
            self.assertEqual(reloaded.workspace, str(root / "committed-workspace"))
            self.assertTrue(baseline.exists(), "a committed phase still needs its baseline")
            self.assertGreater(datetime.fromisoformat(reloaded.deadline_at), datetime.now(timezone.utc))


class PreservedStateTests(unittest.TestCase):
    def test_completed_phases_and_provenance_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor_for(root).retry()
            state = store.load()
            self.assertEqual(state.completed_phases, [0])
            self.assertEqual(state.governance_sha, "gov-sha")
            self.assertEqual(state.snapshot_digest, "snap-digest")

    def test_completed_operation_history_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor_for(root).retry()
            state = store.load()
            self.assertEqual(state.operations["phase-1:implement:1"].status, "completed")

    def test_the_new_attempt_does_not_replay_the_previous_operations(self):
        """Operation ids embed the attempt, so attempt 2 gets fresh ids."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor = supervisor_for(root)
            supervisor.retry()
            state = store.load()
            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, PHASE)
            identifier = f"phase-1:implement:{state.phase_attempts['1']}"
            self.assertEqual(identifier, "phase-1:implement:2")
            self.assertNotIn(identifier, state.operations,
                             "the new attempt must not resolve to a completed operation record")

    def test_consecutive_failures_are_not_reset_by_retry(self):
        """Otherwise retry would silently defer MAX_CONSECUTIVE_FAILURES_EXHAUSTED forever."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor_for(root).retry()
            self.assertEqual(store.load().consecutive_failures, 1)

    def test_the_audit_chain_continues_across_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            before = store.load()
            supervisor_for(root).retry()
            after = store.load()
            self.assertGreater(after.audit_sequence, before.audit_sequence)
            self.assertNotEqual(after.audit_head_hash, before.audit_head_hash)

    def test_the_retry_event_still_records_which_phase_was_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = StateStore(root); store.save(failed_phase_one_state(str(root / "stale")))
            supervisor_for(root).retry()
            events = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            retry_events = [e for e in events if e["event_type"] == "retry_authorized"]
            self.assertEqual(retry_events[-1]["payload"]["phase"], 1,
                             "the released phase number must still reach the audit record")


class RetrySafetyTests(unittest.TestCase):
    def test_a_pending_checkpoint_still_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.pending_approval = ApprovalRequirement("phase-1", 1, 1, "tree", "gov", 0, "nonce")
            store = StateStore(root); store.save(state)
            with self.assertRaisesRegex(RuntimeError, "non-checkpoint escalation"):
                supervisor_for(root).retry()
            self.assertEqual(store.load().current_phase, 1, "a refused retry must not mutate state")

    def test_an_unknown_operation_still_requires_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.operations["phase-1:review:1"] = Operation("phase-1:review:1", "review", 1, status="unknown")
            store = StateStore(root); store.save(state)
            with self.assertRaisesRegex(RuntimeError, "requires reconcile"):
                supervisor_for(root).retry()
            self.assertEqual(store.load().current_phase, 1)

    def test_a_ceiling_escalation_still_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.escalation_code = "PHASE_ATTEMPTS_EXHAUSTED"
            store = StateStore(root); store.save(state)
            with self.assertRaisesRegex(RuntimeError, "authorize-extra-attempt"):
                supervisor_for(root).retry()
            self.assertEqual(store.load().current_phase, 1)

    def test_a_ceiling_retry_with_a_bad_nonce_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.escalation_code = "PHASE_ATTEMPTS_EXHAUSTED"
            state.extra_attempt_grant = ExtraAttemptGrant(1, "PHASE_ATTEMPTS_EXHAUSTED",
                                                          state.generation - 1, "real-nonce")
            store = StateStore(root); store.save(state)
            with self.assertRaisesRegex(RuntimeError, "scope or nonce"):
                supervisor_for(root).retry(True, "wrong-nonce")

    def test_an_authorized_grant_survives_the_phase_reset(self):
        """The grant is validated against the phase before the identity is released."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.escalation_code = "PHASE_ATTEMPTS_EXHAUSTED"
            store = StateStore(root); store.save(state)
            state = store.load()
            state.extra_attempt_grant = ExtraAttemptGrant(1, "PHASE_ATTEMPTS_EXHAUSTED",
                                                          state.generation, "grant-nonce")
            state.status, state.escalation_code = "human_escalation", "PHASE_ATTEMPTS_EXHAUSTED"
            store.save(state)

            supervisor_for(root).retry(True, "grant-nonce")
            reloaded = store.load()
            self.assertIsNone(reloaded.current_phase)
            self.assertTrue(reloaded.extra_attempt_grant.authorized)
            self.assertFalse(reloaded.extra_attempt_grant.consumed)
            self.assertEqual(reloaded.extra_attempt_grant.phase, 1,
                             "the grant must stay scoped to the phase it was issued for")

    def test_the_attempt_ceiling_still_stops_a_retried_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = failed_phase_one_state(str(root / "stale"))
            state.phase_attempts = {"1": 3}          # max_phase_attempts is 3
            store = StateStore(root); store.save(state)
            supervisor = supervisor_for(root)
            supervisor.retry()
            state = store.load()
            with self.assertRaises(Exception) as caught:
                supervisor._run_phase(state, PHASE)
            self.assertNotIsInstance(caught.exception, StopPhase,
                                     "the ceiling must stop the phase before any work starts")
            self.assertEqual(state.escalation_code, "PHASE_ATTEMPTS_EXHAUSTED")
