"""A finished phase moves the integration branch forward.

Phases used to commit to `build/phase-N` and leave the repository there, so every phase was
based on the previous phase branch and the integration branch never moved: a long build
ended up dozens of commits ahead of main. With push disabled the supervisor now
fast-forwards the integration branch to each finished phase and leaves HEAD on it.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.git import GitManager, GitSafetyError
from buildctl.supervisor.phases import ConfigurationError, validate_config
from tests.helpers import git, repository


LOCAL = {"git": {"push": False, "create_pr": False, "merge": False}}


class Harness(unittest.TestCase):
    def setUp(self):
        temporary, self.root = repository()
        self.addCleanup(temporary.cleanup)
        clones = tempfile.TemporaryDirectory(); self.addCleanup(clones.cleanup)
        self.clones = Path(clones.name)                 # outside the repository, like production
        git(self.root, "branch", "-M", "main")         # independent of the host's init.defaultBranch
        self.manager = GitManager(self.root, LOCAL)

    def finish_phase(self, number: int, content: str) -> str:
        """Drive a phase through the real clone -> delta -> commit path."""
        base = self.manager.current_sha()
        clone = self.clones / f"clone-{number}"
        self.manager.create_isolated_clone(base, clone)
        before = self.manager.manifest(clone)
        (clone / "src").mkdir(exist_ok=True); (clone / f"src/phase{number}.py").write_text(content)
        delta = self.manager.attributed_delta(clone, before, ("src/",))
        return self.manager.apply_and_commit(clone, delta, SimpleNamespace(number=number, name=f"p{number}"), base)

    def tip(self, branch: str) -> str:
        return git(self.root, "rev-parse", f"refs/heads/{branch}")

    def head_branch(self) -> str:
        return git(self.root, "symbolic-ref", "--short", "HEAD")


class FastForwardTests(Harness):
    def test_a_finished_phase_moves_main_and_returns_to_it(self):
        branch = self.finish_phase(1, "ONE = 1\n")
        self.assertEqual(self.manager.publish(SimpleNamespace(number=1, merge_policy="auto")), "integrated:main")
        self.assertEqual(self.tip("main"), self.tip(branch))
        self.assertEqual(self.head_branch(), "main")

    def test_the_next_phase_is_based_on_main(self):
        self.finish_phase(1, "ONE = 1\n"); self.manager.integrate("build/phase-1")
        second = self.finish_phase(2, "TWO = 2\n"); self.manager.integrate(second)
        self.assertEqual(git(self.root, "rev-parse", "main~1"), self.tip("build/phase-1"))
        self.assertEqual(self.tip("main"), self.tip("build/phase-2"))
        self.assertTrue((self.root / "src/phase1.py").is_file() and (self.root / "src/phase2.py").is_file())

    def test_no_merge_commit_is_ever_created(self):
        self.finish_phase(1, "ONE = 1\n"); self.manager.integrate("build/phase-1")
        self.assertEqual(git(self.root, "rev-list", "--merges", "main"), "")

    def test_the_phase_branch_is_kept_as_a_record(self):
        branch = self.finish_phase(1, "ONE = 1\n"); self.manager.integrate(branch)
        self.assertTrue(self.tip(branch))

    def test_integrating_twice_is_harmless(self):
        """A crash after the fast-forward but before state is saved replays it on restart."""
        branch = self.finish_phase(1, "ONE = 1\n")
        self.manager.integrate(branch); tip = self.tip("main")
        self.manager.integrate(branch)
        self.assertEqual(self.tip("main"), tip)
        self.assertEqual(self.head_branch(), "main")

    def test_a_crash_after_switching_but_before_merging_completes(self):
        branch = self.finish_phase(1, "ONE = 1\n")
        git(self.root, "switch", "-q", "main")
        self.manager.integrate(branch)
        self.assertEqual(self.tip("main"), self.tip(branch))

    def test_a_configured_integration_branch_is_used(self):
        git(self.root, "branch", "trunk")
        manager = GitManager(self.root, {"git": {**LOCAL["git"], "integration_branch": "trunk"}})
        self.manager = manager
        branch = self.finish_phase(1, "ONE = 1\n")
        main_before = self.tip("main")
        manager.integrate(branch)
        self.assertEqual(self.tip("trunk"), self.tip(branch))
        self.assertEqual(self.tip("main"), main_before)


class RefusalTests(Harness):
    def test_main_that_moved_on_is_not_merged(self):
        branch = self.finish_phase(1, "ONE = 1\n")
        git(self.root, "switch", "-q", "main")
        (self.root / "HOTFIX").write_text("x\n"); git(self.root, "add", "HOTFIX"); git(self.root, "commit", "-qm", "hotfix")
        before = self.tip("main")
        with self.assertRaisesRegex(GitSafetyError, "fast-forward only"):
            self.manager.integrate(branch)
        self.assertEqual(self.tip("main"), before)
        self.assertEqual(git(self.root, "rev-list", "--merges", "main"), "")

    def test_a_dirty_worktree_is_left_alone(self):
        branch = self.finish_phase(1, "ONE = 1\n")
        before = self.tip("main")
        (self.root / "scratch.txt").write_text("user work\n")
        with self.assertRaisesRegex(GitSafetyError, "uncommitted"):
            self.manager.integrate(branch)
        self.assertEqual(self.tip("main"), before)
        self.assertEqual(self.head_branch(), branch)

    def test_a_missing_integration_branch_is_refused(self):
        manager = GitManager(self.root, {"git": {**LOCAL["git"], "integration_branch": "develop"}})
        branch = self.finish_phase(1, "ONE = 1\n")
        with self.assertRaisesRegex(GitSafetyError, "branch does not exist: develop"):
            manager.integrate(branch)

    def test_a_missing_phase_branch_is_refused(self):
        with self.assertRaisesRegex(GitSafetyError, "branch does not exist: build/phase-9"):
            self.manager.integrate("build/phase-9")


class ConfigTests(unittest.TestCase):
    def config(self, branch):
        from tests.test_kernel_trust import CONFIG
        value = {**CONFIG, "git": {**CONFIG["git"], "integration_branch": branch}}
        return value

    def validate(self, branch):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts/phases").mkdir(parents=True); (root / "prompts/phases/p.md").write_text("p")
            validate_config(self.config(branch), root)

    def test_plain_branch_names_are_accepted(self):
        for branch in ("main", "trunk", "release/2026", "dev-1.0"):
            self.validate(branch)

    def test_option_like_or_malformed_names_are_refused(self):
        for branch in ("-main", "a..b", "main.lock", "main/", "", "a b", "a//b", 7):
            with self.assertRaises(ConfigurationError, msg=repr(branch)):
                self.validate(branch)


if __name__ == "__main__":
    unittest.main()
