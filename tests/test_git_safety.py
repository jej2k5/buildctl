import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.git import GitManager, GitSafetyError
from buildctl.supervisor.review import ReviewValidationError, validate_review
from tests.helpers import git, repository
from tests.credential_shapes import private_key_header


class GitSafetyTests(unittest.TestCase):
    def test_dirty_worktree_is_rejected(self):
        temporary,root=repository()
        try:
            (root/"user.txt").write_text("mine")
            with self.assertRaises(GitSafetyError): GitManager(root,{"git":{}}).ensure_safe()
        finally: temporary.cleanup()

    def test_secret_and_unrelated_agent_files_are_rejected(self):
        temporary,root=repository()
        try:
            manager=GitManager(root,{"git":{}}); base=manager.current_sha(); clone=Path(temporary.name)/"clone"
            manager.create_isolated_clone(base,clone); before=manager.manifest(clone)
            (clone/"src").mkdir(); (clone/"src/good.py").write_text("ok")
            (clone/"secrets.pem").write_text(private_key_header(""))
            with self.assertRaises(GitSafetyError): manager.attributed_delta(clone,before,("src/",))
            (clone/"secrets.pem").unlink(); (clone/"unrelated.txt").write_text("user")
            with self.assertRaises(GitSafetyError): manager.attributed_delta(clone,before,("src/",))
        finally: temporary.cleanup()

    def test_isolated_clone_has_no_remote(self):
        temporary,root=repository()
        try:
            manager=GitManager(root,{"git":{}}); clone=Path(temporary.name)/"clone"
            manager.create_isolated_clone(manager.current_sha(),clone)
            self.assertEqual(git(clone,"remote"),"")
        finally: temporary.cleanup()

    def test_git_failure_is_reported_without_state_mutation(self):
        temporary,root=repository()
        try:
            def bad(*args,**kwargs):
                import subprocess
                return subprocess.CompletedProcess(args[0],1,"","denied")
            with self.assertRaises(GitSafetyError): GitManager(root,{"git":{}},bad).ensure_safe()
        finally: temporary.cleanup()

    def test_untracked_file_is_in_exact_review_and_publication_delta(self):
        temporary, root = repository()
        try:
            manager = GitManager(root,{"git":{}}); clone=Path(temporary.name)/"clone"
            manager.create_isolated_clone(manager.current_sha(), clone); before=manager.manifest(clone)
            (clone/"src").mkdir(); (clone/"src/auth.py").write_text("def allowed(): return False\n")
            delta=manager.attributed_delta(clone,before,("src/",))
            evidence=manager.review_evidence(clone,delta,{"passed":True})
            self.assertEqual(delta.added_or_modified,("src/auth.py",))
            self.assertEqual(evidence["delta_id"],delta.digest)
            self.assertEqual(evidence["changed_files"][0]["path"],"src/auth.py")
            self.assertTrue(evidence["coverage"])
            expected={"governance_sha":"g","snapshot_digest":"s","base_sha":"b","phase":1,
                      "delta_id":delta.digest,"evidence_digest":delta.digest,
                      "reviewer":{},
                      "coverage":{("src/auth.py",delta.changes[0]["digest"],"added")}}
            unattested={"schema_version":2,"protocol_version":"buildctl-review-v2","governance_sha":"g",
                        "snapshot_digest":"s","base_sha":"b","phase":1,"delta_id":"wrong",
                        "evidence_digest":"wrong","reviewer":{},"attempt":1,"verdict":"PASS",
                        "findings":[],"coverage":[]}
            with self.assertRaises(ReviewValidationError): validate_review(unattested,expected)
        finally: temporary.cleanup()
