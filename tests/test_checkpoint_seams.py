"""Checkpoints and committed phases the operator could not get out of (kernel seams 8, 1 and 7).

Seam 8: the phase clock, base commit and trust anchor were stamped when a phase became current,
before its checkpoint waited. ReplCRM waited six hours at phase 9 and died PHASE_DEADLINE_EXCEEDED
on approval; MCPxy fixed a brief at phase 5 and failed "repository moved from pinned base SHA".

Seam 1: a pending approval is bound to the tree it was issued on. An operator commit during a
checkpoint (replcrm `687afe5`) left approve refused with no lever to reissue it.

Seam 7: once a phase reached `committed`, nothing could reject it. wyrit phases 6 and 13 looped on
a fast-forward merge that refused, until the operator rebased and approved a phase known to fail.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor import levers
from buildctl.supervisor.git import GitManager
from buildctl.supervisor.state import ApprovalRequirement, BuildState, StateStore
from tests.helpers import git

CONFIG = {"limits": {"max_phase_attempts": 5, "max_phase_minutes": 120, "max_consecutive_failures": 3},
          "checkpoints": {"before_phases": [1], "mvp_completion": 99}, "git": {}}
PHASE = SimpleNamespace(number=1, merge_policy="human", name="p1")


class StopPhase(RuntimeError):
    pass


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.repo, control = base / "repo", base / "control"
        self.repo.mkdir(); control.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@test.invalid"); git(self.repo, "config", "user.name", "T")
        self.commit("README.md", "v1\n")
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.root, supervisor.control_root, supervisor.config = self.repo, control, CONFIG
        supervisor.store, supervisor.git = StateStore(control), GitManager(self.repo, CONFIG)
        supervisor.anchor = {"governance_sha": "gov-1", "snapshot_digest": "snap-1"}
        supervisor.evidence = None

        def no_clone(*args):
            raise StopPhase("work would start here")
        supervisor.git.create_isolated_clone = no_clone
        self.supervisor, self.store = supervisor, supervisor.store

    def commit(self, path: str, text: str) -> str:
        (self.repo / path).write_text(text)
        git(self.repo, "add", path); git(self.repo, "commit", "-qm", f"edit {path}")
        return git(self.repo, "rev-parse", "HEAD")

    def run_phase(self, state: BuildState) -> str:
        """Run until the checkpoint stops it or work would start; return which."""
        try:
            self.supervisor._run_phase(state, PHASE)
        except HumanEscalation as raised:
            return str(raised)
        except StopPhase:
            return "WORK_STARTED"
        raise AssertionError("neither stopped nor started work")

    def approve(self) -> None:
        self.supervisor.approve(self.store.load().pending_approval.nonce)
        return self.store.load()


class ClockStartsWithWorkTests(Harness):
    def test_a_long_wait_at_the_checkpoint_costs_no_phase_time(self):
        state = BuildState(); self.store.save(state)
        self.assertEqual(self.run_phase(state), "CHECKPOINT_REQUIRED")
        state = self.approve()
        state.deadline_at = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()  # signed six hours later
        self.assertEqual(self.run_phase(state), "WORK_STARTED")
        self.assertGreater(datetime.fromisoformat(state.deadline_at), datetime.now(timezone.utc) + timedelta(minutes=110))
        self.assertEqual(state.phase_attempts, {"1": 1}, "waiting spends no attempt")

    def test_the_base_is_the_repository_when_work_starts(self):
        state = BuildState(); self.store.save(state)
        self.run_phase(state)
        moved = self.commit("brief.md", "fixed at the checkpoint\n")
        state = self.store.load()
        self.supervisor.retry()  # reissue for the moved tree (seam 1), then sign
        state = self.approve()
        self.assertEqual(self.run_phase(state), "WORK_STARTED")
        self.assertEqual(state.base_sha, moved)

    def test_a_retrust_at_the_checkpoint_asks_for_a_fresh_signature(self):
        state = BuildState(); self.store.save(state)
        self.run_phase(state)
        state = self.approve()
        self.supervisor.anchor = {"governance_sha": "gov-2", "snapshot_digest": "snap-2"}
        self.assertEqual(self.run_phase(state), "CHECKPOINT_REQUIRED")
        state = self.approve()
        self.assertEqual(state.pending_approval, None)
        self.assertEqual(self.run_phase(state), "WORK_STARTED")
        self.assertEqual(state.governance_sha, "gov-2")


class StaleCheckpointTests(Harness):
    def stopped_at_checkpoint(self) -> BuildState:
        state = BuildState(); self.store.save(state)
        self.assertEqual(self.run_phase(state), "CHECKPOINT_REQUIRED")
        return self.store.load()

    def test_a_commit_during_the_checkpoint_used_to_wedge_approve(self):
        before = self.stopped_at_checkpoint()
        self.commit("src.py", "operator fix\n")
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            self.supervisor.approve(before.pending_approval.nonce)

    def test_retry_reissues_it_for_the_current_tree(self):
        before = self.stopped_at_checkpoint()
        self.commit("src.py", "operator fix\n")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        facts = {"tree_hash": tree, "trust_ok": True, "isolation_fresh": True, "bootstrapped": True,
                 "lock_held": False, "budget_ok": True, "remaining_phases": [1]}
        retry = next(item for item in levers.levers(before, facts) if item["action"] == "retry")
        self.assertEqual((retry["label"], retry["enabled"]), ("Reissue checkpoint", True))
        self.supervisor.retry()
        after = self.store.load()
        requirement = after.pending_approval
        self.assertEqual((requirement.checkpoint, requirement.phase, requirement.phase_attempt), ("phase-1", 1, 1))
        self.assertEqual(requirement.tree_hash, tree)
        self.assertNotEqual(requirement.nonce, before.pending_approval.nonce)
        self.assertEqual(after.escalation_code, "CHECKPOINT_REQUIRED")
        self.assertEqual(after.consecutive_failures, 0)
        self.assertIn("checkpoint_reissued", [entry["event"] for entry in after.history])
        state = self.approve()
        self.assertEqual(self.run_phase(state), "WORK_STARTED")

    def test_a_checkpoint_left_stale_by_another_command_is_reissued_too(self):
        before = self.stopped_at_checkpoint()
        with self.store.lock():
            state = self.store.load()
            self.store.event(state, "evidence_pruned", phase=0)  # any save while the checkpoint waits
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.supervisor.approve(before.pending_approval.nonce)
        self.supervisor.retry()
        self.approve()

    def test_a_checkpoint_that_still_matches_needs_approve_not_retry(self):
        self.stopped_at_checkpoint()
        with self.assertRaisesRegex(RuntimeError, "needs approve"):
            self.supervisor.retry()


class RejectTests(Harness):
    def committed(self) -> BuildState:
        """Reach `committed` the way a run does: the real commit step, which leaves HEAD on the phase branch."""
        import shutil
        workspace = self.repo.parent / "workspace"
        shutil.copytree(self.repo, workspace, ignore=shutil.ignore_patterns(".git"))
        baseline = self.supervisor.git.manifest(workspace)
        (workspace / "feature.py").write_text("phase work\n")
        delta = self.supervisor.git.attributed_delta(workspace, baseline, ("feature.py",))
        base = git(self.repo, "rev-parse", "HEAD")
        branch = self.supervisor.git.apply_and_commit(workspace, delta, PHASE, base)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "build/phase-1")
        state = BuildState(status="human_escalation", escalation_code="CHECKPOINT_REQUIRED", current_phase=1,
                           phase_step="committed", active_branch=branch, phase_attempts={"1": 2},
                           workspace=str(workspace), governance_sha="gov-1", base_sha=base,
                           pending_approval=ApprovalRequirement("merge-phase-1", 1, 2, "t", "gov-1", 0, "n"))
        self.store.save(state)
        return state

    def test_a_committed_phase_can_be_rejected_and_its_commit_is_kept(self):
        state = self.committed()
        facts = {"tree_hash": "t", "trust_ok": True, "isolation_fresh": True, "bootstrapped": True,
                 "lock_held": False, "budget_ok": True, "remaining_phases": [1]}
        reject = next(item for item in levers.levers(state, facts) if item["action"] == "reject")
        self.assertTrue(reject["enabled"])
        self.assertEqual(reject["argv"], ["reject", "--phase", "1", "--confirm", "1"])
        commit = git(self.repo, "rev-parse", "build/phase-1")
        self.supervisor.reject(1, "fails on the host")
        after = self.store.load()
        self.assertEqual((after.status, after.current_phase, after.phase_step, after.pending_approval),
                         ("idle", None, "pending", None))
        self.assertEqual(after.phase_attempts, {"1": 2}, "the next run counts a new attempt")
        self.assertEqual(git(self.repo, "rev-parse", "build/phase-1-rejected-2"), commit)
        self.assertEqual(git(self.repo, "branch", "--list", "build/phase-1"), "")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main", "the repository steps off the rejected work")
        self.assertFalse((self.repo / "feature.py").exists())
        event = after.history[-1]
        self.assertEqual((event["event"], event["reason"]), ("phase_rejected", "fails on the host"))

    def test_the_next_run_is_a_fresh_attempt_from_the_integration_branch(self):
        self.committed()
        self.supervisor.reject(1)
        head = self.commit("brief.md", "prompt fix on main\n")
        state = self.store.load()
        self.assertEqual(self.run_phase(state), "CHECKPOINT_REQUIRED")
        state = self.approve()
        self.assertEqual(self.run_phase(state), "WORK_STARTED")
        self.assertEqual((state.phase_attempts, state.base_sha), ({"1": 3}, head))

    def test_only_a_committed_phase_that_has_not_merged(self):
        state = BuildState(status="human_escalation", current_phase=1, phase_step="clone_completed")
        self.store.save(state)
        with self.assertRaisesRegex(RuntimeError, "not a committed phase"):
            self.supervisor.reject(1)
        self.committed()
        git(self.repo, "checkout", "-q", "main"); git(self.repo, "merge", "-q", "--ff-only", "build/phase-1")
        with self.assertRaisesRegex(RuntimeError, "already on main"):
            self.supervisor.reject(1)

    def test_refused_with_uncommitted_work_or_push_mode(self):
        self.committed()
        (self.repo / "scratch.txt").write_text("operator notes\n")
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            self.supervisor.reject(1)
        (self.repo / "scratch.txt").unlink()
        self.supervisor.config = dict(CONFIG, git={"push": True})
        with self.assertRaisesRegex(RuntimeError, "git.push"):
            self.supervisor.reject(1)


if __name__ == "__main__":
    unittest.main()
