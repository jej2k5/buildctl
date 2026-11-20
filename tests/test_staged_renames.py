"""A phase that moves a file commits.

The delta stages a move as a delete plus an add. `git diff --cached --name-only` detects the
rename and lists only the new path, so the staged-paths check refused every phase that moved
a file (replcrm phase 0 moved src/destinations/application-crm/smoke-test.ts into tests/).
"""
import unittest
from types import SimpleNamespace

from tests.helpers import git
from tests.test_integration_branch import Harness


class StagedRenameTests(Harness):
    def test_a_moved_file_commits_as_delete_plus_add(self):
        (self.root / "src").mkdir(exist_ok=True)
        body = "".join(f"LINE_{n} = {n}\n" for n in range(40))
        (self.root / "src/smoke.py").write_text(body)
        git(self.root, "add", "-A"); git(self.root, "commit", "-qm", "seed")
        base = self.manager.current_sha()
        clone = self.clones / "clone-move"
        self.manager.create_isolated_clone(base, clone)
        before = self.manager.manifest(clone)
        (clone / "tests").mkdir(exist_ok=True)
        (clone / "tests/smoke.py").write_text(body + "EXTRA = 1\n")   # moved and lightly edited
        (clone / "src/smoke.py").unlink()
        delta = self.manager.attributed_delta(clone, before, ("src/", "tests/"))
        branch = self.manager.apply_and_commit(clone, delta, SimpleNamespace(number=1, name="move"), base)
        files = git(self.root, "show", "--name-only", "--no-renames", "--format=", branch).split()
        self.assertEqual(sorted(files), ["src/smoke.py", "tests/smoke.py"])


if __name__ == "__main__":
    unittest.main()
