"""Plain operator lines: a phase's outcome and a checkpoint's note, rendered into docs/ROADMAP.md.

The console's Plan view reads them from the governed roadmap, so an operator can follow a build
without reading phase prompts. Both are optional; a record without them renders as before.
"""
from __future__ import annotations

import unittest

from buildctl_discover.record import RecordError
from buildctl_discover.render import ROADMAP, render

from .helpers import OPERATOR, OWNER, session


class OperatorLines(unittest.TestCase):
    def setUp(self):
        self.temporary, self.session = session()
        self.addCleanup(self.temporary.cleanup)
        decision = self.session.record_decision("Ship it.", OWNER)
        self.one = self.session.propose_phase("Intake", "Accept webhooks.", OWNER, implements=[decision],
                                              outcome="Senders can deliver   webhooks and get a receipt.")
        self.two = self.session.propose_phase("Delivery", "Deliver them.", OWNER, implements=[decision])

    def roadmap(self) -> str:
        return render(self.session.state())[ROADMAP].decode()

    def test_outcome_renders_under_its_phase_and_is_optional(self):
        text = self.roadmap()
        self.assertIn("0. Intake\n   Senders can deliver webhooks and get a receipt.\n1. Delivery\n", text)

    def test_outcome_can_be_revised_and_cleared(self):
        self.session.revise_phase(self.two, {"outcome": "Webhooks reach their targets, with retries."}, OWNER)
        self.assertIn("1. Delivery\n   Webhooks reach their targets, with retries.\n", self.roadmap())
        self.session.revise_phase(self.two, {"outcome": ""}, OWNER)
        self.assertIn("1. Delivery\n\n", self.roadmap())

    def test_outcome_is_one_short_line(self):
        with self.assertRaises(RecordError):
            self.session.propose_phase("Long", "x", OWNER, outcome="y" * 161)
        with self.assertRaises(RecordError):
            self.session.revise_phase(self.one, {"outcome": 7}, OWNER)

    def test_checkpoint_notes_render_in_phase_order(self):
        self.session.set_milestones({"mvp": self.two}, [self.two, self.one], self.two, OWNER,
                                    notes={self.two: "Send a real webhook.", "mvp": "Check the dead-letter queue."})
        text = self.roadmap()
        self.assertIn("Before phase 1, the operator: Send a real webhook.\n"
                      "After phase 1, the operator: Check the dead-letter queue.\n", text)

    def test_a_note_must_name_a_checkpoint(self):
        with self.assertRaises(RecordError):
            self.session.set_milestones({}, [self.two], self.two, OWNER, notes={self.one: "Not a checkpoint."})

    def test_records_without_lines_render_unchanged(self):
        self.session.revise_phase(self.one, {"outcome": ""}, OWNER)
        self.session.set_milestones({}, [self.one], self.two, OWNER)
        text = self.roadmap()
        self.assertNotIn("the operator:", text)
        self.assertIn("0. Intake\n1. Delivery\n", text)


if __name__ == "__main__":
    unittest.main()
