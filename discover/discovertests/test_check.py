from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buildctl_discover.check import check
from buildctl_discover.record import BUILTIN_BASE
from buildctl_discover.render import read_proposal, render, review_digest, write_proposal

from .helpers import OPERATOR, OWNER, session

REVIEWER = {"vendor": "test", "command": ["fake"]}


class GateCase(unittest.TestCase):
    """A small proposal that passes once reviewed. Shared setup, no tests."""

    def setUp(self):
        self.temporary, self.session = session(BUILTIN_BASE)
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.session.set_thesis("Teams can see every delivery.", OWNER)
        self.decision = self.session.record_decision("Deliveries are retried.", OWNER, section="Delivery")
        self.phase = self.session.propose_phase(
            "Retry loop", "Retry failed deliveries.", OWNER, implements=[self.decision],
            acceptance={"commands": {"add": [["{python}", "-m", "unittest", "tests.test_retry"]]}})
        self.session.set_milestones({"mvp": self.phase}, [self.phase], self.phase, OPERATOR)

    def review(self, findings=()):
        files = render(self.session.state())
        self.session.record_review(review_digest(files), REVIEWER, list(findings), OPERATOR)

    def run_check(self, render_first=True):
        state = self.session.state()
        if render_first:
            write_proposal(self.session.proposal_dir, render(state))
        return check(state, self.repo, read_proposal(self.session.proposal_dir))

    def rules(self, result):
        return sorted({item["rule"] for item in result["failures"]})



class Gate(GateCase):
    """Each rule broken on its own."""

    def test_a_complete_reviewed_proposal_passes(self):
        self.review()
        result = self.run_check()
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["passed"])

    def test_an_unimplemented_decision_fails_until_cited_or_deferred(self):
        extra = self.session.record_decision("Logs are kept 30 days.", OWNER)
        self.review()
        self.assertEqual(self.rules(self.run_check()), ["coverage"])
        self.session.defer_decision(extra, "retention comes after the MVP", OPERATOR)
        self.review()
        self.assertTrue(self.run_check()["passed"])

    def test_citing_a_superseded_decision_fails(self):
        replacement = self.session.record_decision("Deliveries are retried with backoff.", OWNER,
                                                   supersedes=self.decision)
        self.review()
        result = self.run_check()
        self.assertIn("citation", self.rules(result))
        self.assertIn("coverage", self.rules(result))
        self.session.revise_phase(self.phase, {"implements": [replacement]}, OWNER)
        self.review()
        self.assertTrue(self.run_check()["passed"])

    def test_a_phase_on_default_acceptance_alone_fails(self):
        self.session.propose_phase("Docs", "Write docs.", OWNER, implements=[self.decision])
        self.review()
        self.assertEqual(self.rules(self.run_check()), ["acceptance"])

    def test_allowed_paths_that_cover_governed_files_fail(self):
        self.session.revise_phase(self.phase, {"acceptance": {
            "commands": {"add": [["make", "test"]]}, "allowed_paths": {"add": ["docs/"]}}}, OWNER)
        self.review()
        failures = self.run_check()["failures"]
        self.assertEqual([item["rule"] for item in failures], ["allowed_paths"])
        self.assertIn("docs/", failures[0]["message"])

    def test_an_open_question_fails_until_resolved_or_accepted(self):
        question = self.session.add_open_question("Which regions?", OWNER)
        self.review()
        self.assertEqual(self.rules(self.run_check()), ["open_question"])
        self.session.accept_risk(question, "one region for v1", OPERATOR)
        self.review()
        self.assertTrue(self.run_check()["passed"])

    def test_a_hand_edited_proposal_file_fails(self):
        self.review()
        state = self.session.state()
        write_proposal(self.session.proposal_dir, render(state))
        target = self.session.proposal_dir / "docs/PRODUCT_THESIS.md"
        target.write_text(target.read_text() + "Also: billing.\n")
        result = check(state, self.repo, read_proposal(self.session.proposal_dir))
        self.assertEqual(self.rules(result), ["rendered"])

    def test_an_unrendered_proposal_fails(self):
        self.review()
        self.assertEqual(self.rules(self.run_check(render_first=False)), ["rendered"])

    def test_a_change_after_review_makes_the_review_stale(self):
        self.review()
        self.session.revise_phase(self.phase, {"objective": "Retry failed deliveries with jitter."}, OWNER)
        self.assertEqual(self.rules(self.run_check()), ["review"])

    def test_accepting_a_risk_does_not_stale_the_review(self):
        self.review([{"severity": "HIGH", "target": self.phase, "summary": "No test proves backoff timing.",
                      "detail": ""}])
        self.assertEqual(self.rules(self.run_check()), ["review"])
        self.session.accept_risk("F-1", "timing is checked in phase 4", OPERATOR)
        self.assertTrue(self.run_check()["passed"])

    def test_medium_and_low_findings_do_not_block(self):
        self.review([{"severity": "MEDIUM", "target": "general", "summary": "Name could be clearer.",
                      "detail": ""}])
        self.assertTrue(self.run_check()["passed"])

    def test_a_base_buildctl_rejects_fails_validate(self):
        temporary, other = session(dict(BUILTIN_BASE, limits={}))
        self.addCleanup(temporary.cleanup)
        state = other.state()
        result = check(state, Path(temporary.name), render(state))
        self.assertIn("validate", self.rules(result))

    def test_missing_thesis_phases_and_milestones_are_reported(self):
        temporary, other = session(BUILTIN_BASE)
        self.addCleanup(temporary.cleanup)
        state = other.state()
        rules = self.rules(check(state, Path(temporary.name), render(state)))
        for rule in ("thesis", "phases", "milestones", "review"):
            self.assertIn(rule, rules)


if __name__ == "__main__":
    unittest.main()
