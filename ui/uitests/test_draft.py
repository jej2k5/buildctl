"""Draft view: `buildctl-ui --session NAME` opens a buildctl-discover proposal read-only.

    PYTHONPATH=ui python3 -m unittest discover -s ui/uitests -t ui
"""
import json
import tempfile
import unittest
from pathlib import Path

from buildctl_ui.__main__ import main
from buildctl_ui.actions import Refused
from buildctl_ui.draft import Draft, event_line

WEB = Path(__file__).resolve().parents[1] / "buildctl_ui" / "web"

ROADMAP = """# Roadmap

0. Intake
   Senders can deliver webhooks and get a receipt.
1. Delivery

Before phase 1, the operator: Send a real webhook.
After phase 1, the operator: Check the dead-letter queue.
"""
CHECK = """open_question [Q-2]: Q-2 is still open: Which queue backs retries? Resolve it before finalizing.
review [F-4, PH-1]: F-4 (HIGH, PH-1): Delivery has no idempotency key.
review: The latest review (RV-1) covers an earlier proposal. Call request_review again.
digest abc123: 3 failure(s)
"""


class FakeDraft(Draft):
    """A Draft whose buildctl-discover answers come from strings, over a real session folder."""
    check_output = CHECK

    def _discover(self, *args: str) -> str:
        if args[0] == "status":
            return f"session   {self.root}\nproposal  {self.root / 'proposal'}\ndigest    abc123def456789\n"
        return self.check_output

    def _git(self, *args: str) -> str:
        return ""


class DraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "proposal" / "docs").mkdir(parents=True)
        (root / "proposal" / "docs" / "ROADMAP.md").write_text(ROADMAP)
        (root / "proposal" / "build.yaml").write_text(json.dumps({
            "phases": [{"number": 0, "name": "Intake"}, {"number": 1, "name": "Delivery"}],
            "checkpoints": {"before_phases": [1], "mvp_completion": 1}, "milestones": {"mvp": 1}}))
        (root / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"timestamp": "2026-01-01T00:00:00Z", "event_type": "session_started", "payload": {}},
            {"timestamp": "2026-01-01T00:00:01Z", "event_type": "phase_proposed", "payload": {"name": "Intake", "outcome": "Senders get a receipt."}},
            {"timestamp": "2026-01-01T00:00:02Z", "event_type": "question_added", "payload": {"id": "Q-2", "text": "Which queue?"}},
        ]))
        self.draft = FakeDraft(root, "next")
        self.draft.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_model_reads_the_gate_and_the_proposal(self):
        m = self.draft.model()
        d = m["draft"]
        self.assertEqual(d["questions"], [{"id": "Q-2", "text": "Which queue backs retries?"}])
        self.assertEqual(d["findings"], [{"id": "F-4", "severity": "HIGH", "target": "PH-1", "text": "Delivery has no idempotency key."}])
        self.assertEqual([f["rule"] for f in d["failures"]], ["review"])
        self.assertFalse(d["passes"])
        self.assertEqual(m["plan"]["outcomes"], {0: "Senders can deliver webhooks and get a receipt."})
        self.assertEqual(m["plan"]["notes"]["before"], {1: "Send a real webhook."})
        self.assertEqual([p["checkpoint"] for p in m["phases"]], [False, True])
        self.assertEqual(m["phases"][1]["milestones"], ["mvp"])
        self.assertEqual(m["config"]["checkpoints"]["mvp_completion"], 1)
        self.assertEqual([e["text"] for e in d["events"]], ["Session opened.", "Phase Intake: Senders get a receipt.", "Your call Q-2: Which queue?"])

    def test_a_passing_gate_is_ready(self):
        self.draft.check_output = "digest abc123: passes\n"
        d = self.draft.model()["draft"]
        self.assertTrue(d["passes"])
        self.assertEqual((d["questions"], d["findings"], d["failures"]), ([], [], []))

    def test_a_draft_has_no_levers(self):
        with self.assertRaises(Refused):
            self.draft.pull("start", generation=0)
        with self.assertRaises(Refused):
            self.draft.read_artifact("README.md")

    def test_unknown_events_still_read_plainly(self):
        self.assertEqual(event_line({"event_type": "something_new"}), "Something new.")

    def test_drafts_open_in_the_web_console_only(self):
        self.assertEqual(main(["tui", "--session", "next", "--repo", self.tmp.name]), 2)

    def test_the_page_renders_drafts_and_never_finalizes(self):
        js = (WEB / "app.js").read_text()
        self.assertIn("function viewDraft", js)
        self.assertIn("m.draft ? viewDraft", js)
        self.assertIn("finalize --confirm", js)
        self.assertNotIn('api("/api/pull"', js[js.index("function viewDraft"):js.index("const DAY_FMT")])
        self.assertIn('body.is-draft .rail a[data-route]:not([data-route="plan"])', (WEB / "app.css").read_text())


if __name__ == "__main__":
    unittest.main()
