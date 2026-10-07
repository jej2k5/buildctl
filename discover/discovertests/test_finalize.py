from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buildctl.supervisor.phases import load_config
from buildctl.supervisor.state import StateStore
from buildctl_discover.__main__ import main
from buildctl_discover.finalize import finalize
from buildctl_discover.record import RecordError, Session
from buildctl_discover.render import read_proposal, render, write_proposal
from buildctl_discover.review import request_review

from tests.helpers import git, repository

from .fakes import reviewer
from .helpers import OPERATOR, OWNER


class Finalize(unittest.TestCase):
    """From an empty repository to a build.yaml buildctl loads, through the CLI and the record."""

    def setUp(self):
        self.temporary, self.repo = repository()
        self.addCleanup(self.temporary.cleanup)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state_dir = Path(state.name)
        patcher = mock.patch.dict(os.environ, {"BUILDCTL_DISCOVER_STATE_DIR": str(self.state_dir / "discover"),
                                               "BUILDCTL_STATE_DIR": str(self.state_dir / "buildctl")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--repo", str(self.repo), "--as", "Ada", *argv])
        return code, out.getvalue() + err.getvalue()

    def interview(self) -> Session:
        code, output = self.cli("start")
        self.assertEqual(code, 0, output)
        session = Session.for_repo(self.repo)
        session.set_thesis("Teams can see every delivery.", OWNER)
        decision = session.record_decision("Deliveries are retried.", OWNER)
        phase = session.propose_phase("Retry loop", "Retry failed deliveries.", OWNER, implements=[decision],
                                      acceptance={"commands": {"add": [["{python}", "-m", "unittest", "tests.t"]]}})
        session.set_milestones({"mvp": phase}, [phase], phase, OPERATOR)
        request_review(session, OWNER, runner=reviewer())
        write_proposal(session.proposal_dir, render(session.state()))
        return session

    def digest(self) -> str:
        code, output = self.cli("status")
        self.assertEqual(code, 0, output)
        return next(line.split()[1] for line in output.splitlines() if line.startswith("digest"))

    def test_the_whole_flow_produces_a_build_yaml_buildctl_loads(self):
        self.interview()
        code, output = self.cli("check")
        self.assertEqual(code, 0, output)
        code, output = self.cli("finalize", "--confirm", self.digest()[:8])
        self.assertEqual(code, 0, output)
        self.assertIn("trust init", output)
        config = load_config(self.repo / "build.yaml")
        self.assertEqual([phase["name"] for phase in config["phases"]], ["Retry loop"])
        self.assertTrue((self.repo / "prompts/phases/phase-00-retry-loop.md").is_file())
        self.assertIn("build.yaml", git(self.repo, "status", "--porcelain"))
        self.assertEqual(git(self.repo, "log", "--oneline").count("\n"), 0, "finalize must not commit")

    def test_a_wrong_or_short_confirmation_is_refused(self):
        self.interview()
        for confirm in ("0" * 8, self.digest()[:7]):
            code, output = self.cli("finalize", "--confirm", confirm)
            self.assertEqual(code, 2)
            self.assertIn("--confirm", output)
        self.assertFalse((self.repo / "build.yaml").exists())

    def test_a_failing_check_blocks_finalize(self):
        self.cli("start")
        code, output = self.cli("check")
        self.assertEqual(code, 1)
        code, output = self.cli("finalize", "--confirm", self.digest()[:8])
        self.assertEqual(code, 2)
        self.assertIn("check fails", output)

    def test_a_held_buildctl_lock_blocks_finalize(self):
        session = self.interview()
        store = StateStore(self.state_dir / "buildctl")
        with store.lock():
            with self.assertRaisesRegex(RecordError, "lock"):
                finalize(session, self.repo, self.digest()[:8], OPERATOR)
        self.assertFalse((self.repo / "build.yaml").exists())

    def test_a_running_build_blocks_finalize(self):
        session = self.interview()
        (self.state_dir / "buildctl").mkdir(parents=True)
        (self.state_dir / "buildctl/state.json").write_text('{"schema_version": 2, "status": "running"}')
        with self.assertRaisesRegex(RecordError, "running"):
            finalize(session, self.repo, self.digest()[:8], OPERATOR)

    def test_render_repairs_a_stale_proposal(self):
        session = self.interview()
        (session.proposal_dir / "docs/ROADMAP.md").write_text("edited\n")
        self.assertEqual(self.cli("check")[0], 1)
        self.assertEqual(self.cli("render")[0], 0)
        self.assertEqual(self.cli("check")[0], 0)

    def test_uncommitted_changes_to_target_paths_block_finalize(self):
        session = self.interview()
        (self.repo / "build.yaml").write_text("{}\n")
        with self.assertRaisesRegex(RecordError, "uncommitted"):
            finalize(session, self.repo, self.digest()[:8], OPERATOR)
        self.assertEqual((self.repo / "build.yaml").read_text(), "{}\n")

    def test_an_owner_cannot_finalize(self):
        session = self.interview()
        with self.assertRaises(RecordError):
            finalize(session, self.repo, self.digest()[:8], OWNER)

    def test_start_takes_the_base_from_an_existing_build_yaml(self):
        import json
        from buildctl_discover.record import BUILTIN_BASE
        (self.repo / "build.yaml").write_text(json.dumps(dict(BUILTIN_BASE, limits=dict(
            BUILTIN_BASE["limits"], max_phase_minutes=45), phases=[])))
        git(self.repo, "add", "build.yaml"); git(self.repo, "commit", "-qm", "base")
        code, output = self.cli("start")
        self.assertIn("base from build.yaml", output)
        self.assertEqual(Session.for_repo(self.repo).state().base["limits"]["max_phase_minutes"], 45)


if __name__ == "__main__":
    unittest.main()
