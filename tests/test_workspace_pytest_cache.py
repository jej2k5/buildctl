"""Regression tests for the pytest cache leaking into the phase delta.

Phase 1 got past the bytecode fix and then failed with:

    buildctl: phase changed unattributed path: .pytest_cache/.gitignore

The audit log puts the directory's creation at 19:12:20Z, inside `phase-1:implement:1`
(19:08:14Z-19:12:39Z) and before the gate started at 19:12:39Z — the implementer ran pytest
while working. The gate runs `unittest`, so a gate argv flag could never have covered it;
suppression has to travel in the environment, which is what `PYTEST_ADDOPTS` does.

Everything that is not one of the two recognised generated directories must still fail the
phase as an unattributed path.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.git import GitManager, GitSafetyError
from buildctl.supervisor.isolation import sanitized_environment
from buildctl.supervisor.safety import (PYTEST_CACHE_DIRECTORY, UnsafeCleanup,
                                     purge_ephemeral_artifacts, suppress_ephemeral_artifacts)

ALLOWED = ("src/", "migrations/", "tests/product/", "pyproject.toml")

try:  # pytest is not a supervisor dependency; the end-to-end test runs only where it exists.
    import pytest as _pytest
    PYTEST_AVAILABLE = True
except ImportError:
    PYTEST_AVAILABLE = False


def make_workspace(root: Path) -> Path:
    workspace = root / "ws"
    for relative in ("src/relay", "tests/product"):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    return workspace


def write_pytest_cache(workspace: Path, where: str = ".") -> Path:
    """Reproduce exactly what pytest's cacheprovider writes."""
    cache = workspace / where / PYTEST_CACHE_DIRECTORY
    (cache / "v" / "cache").mkdir(parents=True, exist_ok=True)
    (cache / ".gitignore").write_text("# Created by pytest automatically.\n*\n")
    (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (cache / "README.md").write_text("# pytest cache directory #\n")
    (cache / "v" / "cache" / "nodeids").write_text('["tests/product/test_foundation.py::x"]')
    (cache / "v" / "cache" / "lastfailed").write_text("{}")
    return cache


class SuppressionTests(unittest.TestCase):
    def test_the_shared_helper_disables_the_cache_provider(self):
        self.assertEqual(suppress_ephemeral_artifacts({})["PYTEST_ADDOPTS"], "-p no:cacheprovider")

    def test_suppression_still_covers_bytecode(self):
        """The previous fix must not regress while adding this one."""
        env = suppress_ephemeral_artifacts({})
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTEST_ADDOPTS"], "-p no:cacheprovider")

    def test_the_surrounding_environment_is_preserved(self):
        base = sanitized_environment("implementer", {"TOKEN": "x"})
        wrapped = suppress_ephemeral_artifacts(dict(base))
        for key, value in base.items():
            self.assertEqual(wrapped[key], value, f"{key} was lost")

    def test_the_variable_is_not_inherited_from_the_host(self):
        """Nothing ambient can pre-empt or contradict the value the supervisor sets."""
        original = os.environ.get("PYTEST_ADDOPTS")
        os.environ["PYTEST_ADDOPTS"] = "--exitfirst"
        try:
            self.assertNotIn("PYTEST_ADDOPTS", sanitized_environment("implementer"))
            self.assertEqual(suppress_ephemeral_artifacts(sanitized_environment("implementer"))
                             ["PYTEST_ADDOPTS"], "-p no:cacheprovider")
        finally:
            if original is None:
                os.environ.pop("PYTEST_ADDOPTS", None)
            else:
                os.environ["PYTEST_ADDOPTS"] = original


@unittest.skipUnless(PYTEST_AVAILABLE, "pytest is not installed in this environment")
class RealPytestLeavesNoCacheTests(unittest.TestCase):
    """End-to-end where pytest exists: a real run must leave the workspace clean."""

    def _run(self, workspace: Path, env: dict) -> subprocess.CompletedProcess:
        (workspace / "tests" / "product" / "test_x.py").write_text("def test_x():\n    assert True\n")
        return subprocess.run([os.sys.executable, "-m", "pytest", "tests/product", "-q"],
                              cwd=str(workspace), env=env, text=True, capture_output=True)

    def test_a_suppressed_pytest_run_writes_no_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            env = suppress_ephemeral_artifacts(dict(os.environ))
            completed = self._run(workspace, env)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(list(workspace.rglob(PYTEST_CACHE_DIRECTORY)), [])

    def test_an_unsuppressed_pytest_run_does_write_a_cache(self):
        """Negative control: proves the test above measures the setting."""
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            env = dict(os.environ); env.pop("PYTEST_ADDOPTS", None)
            self._run(workspace, env)
            self.assertTrue(list(workspace.rglob(PYTEST_CACHE_DIRECTORY)),
                            "expected an unsuppressed run to create the cache")


class PurgeRemovesPytestCacheTests(unittest.TestCase):
    def test_a_cache_at_the_workspace_root_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            write_pytest_cache(workspace)
            removed = purge_ephemeral_artifacts(workspace)
            self.assertFalse((workspace / PYTEST_CACHE_DIRECTORY).exists())
            self.assertIn(".pytest_cache/.gitignore", removed)
            self.assertIn(".pytest_cache/v/cache/nodeids", removed)

    def test_a_nested_cache_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            write_pytest_cache(workspace, where="tests/product")
            purge_ephemeral_artifacts(workspace)
            self.assertEqual(list(workspace.rglob(PYTEST_CACHE_DIRECTORY)), [])

    def test_bytecode_and_cache_are_both_removed_in_one_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            write_pytest_cache(workspace)
            cache = workspace / "src" / "relay" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "app.cpython-313.pyc").write_bytes(b"\x00")
            purge_ephemeral_artifacts(workspace)
            self.assertEqual(list(workspace.rglob(PYTEST_CACHE_DIRECTORY)), [])
            self.assertEqual(list(workspace.rglob("__pycache__")), [])

    def test_the_exact_escalated_path_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            write_pytest_cache(workspace)
            removed = purge_ephemeral_artifacts(workspace)
            self.assertIn(".pytest_cache/.gitignore", removed)


class PurgeStaysNarrowTests(unittest.TestCase):
    def test_a_cache_directory_holding_source_is_refused(self):
        """Fail closed: cleanup must never be a route to hide agent-authored work."""
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            cache = write_pytest_cache(workspace)
            (cache / "smuggled.py").write_text("print('hidden')\n")
            with self.assertRaises(UnsafeCleanup):
                purge_ephemeral_artifacts(workspace)
            self.assertTrue((cache / "smuggled.py").is_file())
            self.assertTrue((cache / ".gitignore").is_file())

    def test_unrelated_hidden_directories_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            for name in (".mypy_cache", ".ruff_cache", ".hidden", ".venv"):
                target = workspace / name
                target.mkdir()
                (target / "marker").write_text("keep\n")
            purge_ephemeral_artifacts(workspace)
            for name in (".mypy_cache", ".ruff_cache", ".hidden", ".venv"):
                self.assertTrue((workspace / name / "marker").is_file(),
                                f"{name} must not be removed by a pytest-cache cleanup")

    def test_ordinary_source_and_untracked_files_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_workspace(Path(directory).resolve())
            write_pytest_cache(workspace)
            keep = {"src/relay/app.py": "app\n", "notes.md": "notes\n"}
            for relative, text in keep.items():
                (workspace / relative).write_text(text)
            purge_ephemeral_artifacts(workspace)
            for relative, text in keep.items():
                self.assertEqual((workspace / relative).read_text(), text)


class PurgeCannotEscapeTests(unittest.TestCase):
    def test_a_symlinked_cache_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            outside = root / "outside"
            outside.mkdir()
            victim = outside / "precious.txt"
            victim.write_text("host data\n")
            (workspace / PYTEST_CACHE_DIRECTORY).symlink_to(outside)
            purge_ephemeral_artifacts(workspace)
            self.assertTrue(victim.is_file(), "cleanup followed a symlink out of the workspace")

    def test_a_cache_reached_through_a_symlinked_parent_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            outside = root / "hostside"
            (outside / PYTEST_CACHE_DIRECTORY).mkdir(parents=True)
            victim = outside / PYTEST_CACHE_DIRECTORY / "README.md"
            victim.write_text("host\n")
            (workspace / "src" / "linked").symlink_to(outside)
            purge_ephemeral_artifacts(workspace)
            self.assertTrue(victim.is_file())

    def test_a_cache_containing_a_symlink_out_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            victim = root / "precious.txt"
            victim.write_text("host data\n")
            cache = write_pytest_cache(workspace)
            (cache / "v" / "escape").symlink_to(victim)
            with self.assertRaises(UnsafeCleanup):
                purge_ephemeral_artifacts(workspace)
            self.assertTrue(victim.is_file())


class DeltaIntegrationTests(unittest.TestCase):
    def _repo(self, root: Path) -> GitManager:
        return GitManager(root, {"git": {"push": False, "create_pr": False, "merge": False}})

    def test_the_reported_failure_no_longer_occurs(self):
        """The exact reproduction: `.pytest_cache/.gitignore` must not fail the delta."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            baseline = self._repo(root).manifest(workspace)
            (workspace / "src" / "relay" / "app.py").write_text("x = 1\n")
            write_pytest_cache(workspace)
            delta = self._repo(root).attributed_delta(workspace, baseline, ALLOWED)
            self.assertIn("src/relay/app.py", delta.added_or_modified)
            self.assertFalse([p for p in delta.added_or_modified if p.startswith(".pytest_cache")])

    def test_an_unrelated_unattributed_path_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            baseline = self._repo(root).manifest(workspace)
            write_pytest_cache(workspace)
            (workspace / ".mypy_cache").mkdir()
            (workspace / ".mypy_cache" / "index.json").write_text("{}")
            with self.assertRaisesRegex(GitSafetyError, "unattributed path"):
                self._repo(root).attributed_delta(workspace, baseline, ALLOWED)

    def test_an_unexpected_binary_still_reaches_the_unreviewable_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            baseline = self._repo(root).manifest(workspace)
            write_pytest_cache(workspace)
            (workspace / "src" / "relay" / "unexpected.bin").write_bytes(bytes(range(256)))
            delta = self._repo(root).attributed_delta(workspace, baseline, ALLOWED)
            evidence = self._repo(root).review_evidence(workspace, delta, {"passed": True})
            self.assertEqual(evidence["unreviewable"], ["src/relay/unexpected.bin"])

    def test_unsafe_cleanup_fails_the_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = make_workspace(root)
            baseline = self._repo(root).manifest(workspace)
            cache = write_pytest_cache(workspace)
            (cache / "smuggled.py").write_text("hidden\n")
            with self.assertRaisesRegex(GitSafetyError, "artifact cleanup failed"):
                self._repo(root).attributed_delta(workspace, baseline, ALLOWED)
