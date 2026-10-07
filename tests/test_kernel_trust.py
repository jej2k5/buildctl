"""The supervisor kernel is pinned by the trust anchor, not by the governed repository.

In the original project the supervisor lived at build/ inside the repository it governed, so the
governance SHA pinned the enforcer along with the rules. As a standalone package that is no
longer true: the anchor records the installed kernel's identity at `trust init`, and every
trusted load refuses to run under any other kernel.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buildctl import __version__
from buildctl.buildctl import main, resolve_repository
from buildctl.supervisor import trust as trust_module
from buildctl.supervisor.kernel import kernel_digest, kernel_identity
from buildctl.supervisor.trust import TrustError, TrustStore
from tests.helpers import git


CONFIG = {"limits": {"max_phase_attempts": 1, "max_review_format_attempts": 1, "max_semantic_review_attempts": 1,
                     "max_debate_rounds": 1, "max_phase_minutes": 1, "max_daily_cost_usd": 1,
                     "max_consecutive_failures": 1},
          "operation_cost_usd": {"implementation": 1, "review": 1, "remediation": 1, "debate": 1},
          "git": {"push": False, "create_pr": False, "merge": False},
          "checkpoints": {"before_phases": [0], "mvp_completion": 0},
          "phase_defaults": {"required_files": [], "commands": [["true"]], "invariants": [],
                             "claude_review_required": True, "debate_on_architecture_disagreement": True,
                             "max_remediation_attempts": 1, "merge_policy": "human", "allowed_paths": ["src/"]},
          "roles": {"implementer": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": []},
                    "reviewer": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": [],
                                 "protocol_version": "buildctl-review-v2"},
                    "arbiter": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": []}},
          "phases": [{"number": 0, "name": "p", "prompt": "prompts/phases/p.md"}]}


def governed_repository(directory: Path) -> tuple[Path, str]:
    """A repository holding only governance files: no supervisor code anywhere in it."""
    root = directory / "repo"; root.mkdir()
    git(root, "init", "-q"); git(root, "config", "user.email", "x@y"); git(root, "config", "user.name", "x")
    (root / "prompts/phases").mkdir(parents=True); (root / "prompts/phases/p.md").write_text("p")
    (root / "build.yaml").write_text(json.dumps(CONFIG))
    git(root, "add", "."); git(root, "commit", "-qm", "governance")
    return root, git(root, "rev-parse", "HEAD")


class KernelPinningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.repo, self.sha = governed_repository(base)
        self.control = base / "control"

    def test_a_repository_without_supervisor_code_can_be_trusted(self):
        anchor = TrustStore(self.repo, self.control).initialize(self.sha, self.sha)
        self.assertEqual(anchor["governance_sha"], self.sha)

    def test_the_anchor_records_the_running_kernel(self):
        anchor = TrustStore(self.repo, self.control).initialize(self.sha, self.sha)
        self.assertEqual(anchor["kernel"], kernel_identity())
        self.assertEqual(anchor["kernel"]["version"], __version__)

    def test_an_unchanged_kernel_loads(self):
        store = TrustStore(self.repo, self.control); store.initialize(self.sha, self.sha)
        self.assertEqual(store.load()["kernel"]["digest"], kernel_digest())

    def test_a_changed_kernel_is_refused(self):
        store = TrustStore(self.repo, self.control); store.initialize(self.sha, self.sha)
        upgraded = {"version": "9.9.9", "digest": "0" * 64}
        with mock.patch.object(trust_module, "kernel_identity", return_value=upgraded):
            with self.assertRaisesRegex(TrustError, "kernel changed since trust init"):
                store.load()

    def test_re_trusting_accepts_the_new_kernel(self):
        store = TrustStore(self.repo, self.control); store.initialize(self.sha, self.sha)
        upgraded = {"version": "9.9.9", "digest": "0" * 64}
        with mock.patch.object(trust_module, "kernel_identity", return_value=upgraded):
            store.initialize(self.sha, self.sha)
            self.assertEqual(store.load()["kernel"], upgraded)

    def test_an_anchor_from_before_kernel_pinning_is_refused(self):
        store = TrustStore(self.repo, self.control); store.initialize(self.sha, self.sha)
        anchor = json.loads(store.anchor_path.read_text()); del anchor["kernel"]
        store.anchor_path.write_text(json.dumps(anchor))
        with self.assertRaisesRegex(TrustError, "predates kernel pinning"):
            store.load()

    def test_the_trusted_config_path_enforces_the_pin(self):
        store = TrustStore(self.repo, self.control); store.initialize(self.sha, self.sha)
        with mock.patch.object(trust_module, "kernel_identity", return_value={"version": "x", "digest": "1" * 64}):
            with self.assertRaises(TrustError):
                store.config()


class GovernanceScopeTests(unittest.TestCase):
    def test_the_old_in_repository_kernel_paths_are_not_governance(self):
        for path in ("build/buildctl.py", "build/supervisor/trust.py", "buildctl"):
            self.assertFalse(TrustStore.is_governance_path(path), path)

    def test_governed_rules_remain_governance(self):
        for path in ("build.yaml", "prompts/phases/p.md", "templates/REVIEW_RESULT.json", "AGENTS.md"):
            self.assertTrue(TrustStore.is_governance_path(path), path)


class KernelDigestTests(unittest.TestCase):
    def tree(self) -> Path:
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "supervisor").mkdir()
        (root / "a.py").write_text("A = 1\n"); (root / "supervisor/b.py").write_text("B = 2\n")
        return root

    def test_content_changes_the_digest(self):
        root = self.tree(); before = kernel_digest(root)
        (root / "a.py").write_text("A = 2\n")
        self.assertNotEqual(kernel_digest(root), before)

    def test_a_rename_changes_the_digest(self):
        root = self.tree(); before = kernel_digest(root)
        (root / "a.py").rename(root / "c.py")
        self.assertNotEqual(kernel_digest(root), before)

    def test_moving_bytes_between_files_changes_the_digest(self):
        root = self.tree(); before = kernel_digest(root)
        (root / "a.py").write_text("A = 1\nB = 2\n"); (root / "supervisor/b.py").write_text("")
        self.assertNotEqual(kernel_digest(root), before)

    def test_an_added_file_changes_the_digest(self):
        root = self.tree(); before = kernel_digest(root)
        (root / "supervisor/extra.py").write_text("")
        self.assertNotEqual(kernel_digest(root), before)

    def test_bytecode_does_not_change_the_digest(self):
        root = self.tree(); before = kernel_digest(root)
        (root / "__pycache__").mkdir(); (root / "__pycache__/a.cpython-314.pyc").write_bytes(b"\0")
        (root / "stray.pyc").write_bytes(b"\0")
        self.assertEqual(kernel_digest(root), before)


class RepositoryResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.repo, _ = governed_repository(self.base)

    def test_explicit_toplevel_is_accepted(self):
        self.assertEqual(resolve_repository(str(self.repo)), self.repo)

    def test_explicit_nested_path_is_refused(self):
        with self.assertRaisesRegex(ValueError, "toplevel"):
            resolve_repository(str(self.repo / "prompts"))

    def test_default_walks_up_from_the_working_directory(self):
        previous = Path.cwd(); self.addCleanup(os.chdir, previous)
        os.chdir(self.repo / "prompts/phases")
        self.assertEqual(resolve_repository(None), self.repo)

    def test_outside_a_repository_is_refused(self):
        outside = self.base / "outside"; outside.mkdir()
        with self.assertRaisesRegex(ValueError, "not inside a Git repository"):
            resolve_repository(str(outside))

    def test_the_cli_governs_the_named_repository(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--repo", str(self.repo), "validate"])
        self.assertEqual(code, 0)
        self.assertIn("1 phases", output.getvalue())


if __name__ == "__main__":
    unittest.main()
