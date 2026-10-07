from __future__ import annotations

import json
from pathlib import Path
import unittest

from buildctl.supervisor.audit import AuditIntegrityError
from buildctl_discover.record import BUILTIN_BASE, Identity, RecordError

from .helpers import OPERATOR, OWNER, session


class Record(unittest.TestCase):
    def setUp(self):
        self.temporary, self.session = session(BUILTIN_BASE)
        self.addCleanup(self.temporary.cleanup)

    def test_ids_are_sequential_per_kind_and_survive_replay(self):
        self.assertEqual(self.session.record_decision("A", OWNER), "D-1")
        self.assertEqual(self.session.add_open_question("Q?", OWNER), "Q-1")
        self.assertEqual(self.session.record_decision("B", OWNER), "D-2")
        state = self.session.state()
        self.assertEqual([item["id"] for item in state.current_decisions()], ["D-1", "D-2"])

    def test_superseding_retires_the_old_decision_once(self):
        old = self.session.record_decision("Postgres", OWNER)
        new = self.session.record_decision("SQLite", OWNER, supersedes=old)
        state = self.session.state()
        self.assertEqual(state.decisions[old]["superseded_by"], new)
        self.assertEqual([item["id"] for item in state.current_decisions()], [new])
        with self.assertRaises(RecordError):
            self.session.record_decision("MySQL", OWNER, supersedes=old)

    def test_a_withdrawn_phase_id_is_never_reused(self):
        decision = self.session.record_decision("A", OWNER)
        first = self.session.propose_phase("One", "Do one.", OWNER, implements=[decision])
        self.session.withdraw_phase(first, "merged into two", OWNER)
        self.assertEqual(self.session.propose_phase("Two", "Do two.", OWNER, implements=[decision]), "PH-2")

    def test_the_speaker_is_recorded_from_the_pinned_identity(self):
        self.session.record_decision("A", OWNER)
        self.assertEqual(self.session.state().decisions["D-1"]["by"], {"name": "Sam", "role": "owner"})

    def test_only_the_operator_accepts_a_risk_or_starts_a_session(self):
        question = self.session.add_open_question("Which region?", OWNER)
        with self.assertRaises(RecordError):
            self.session.accept_risk(question, "later", OWNER)
        self.session.accept_risk(question, "single region for v1", OPERATOR)
        self.assertEqual(self.session.state().questions[question]["status"], "accepted_risk")

    def test_a_second_start_is_refused(self):
        with self.assertRaises(RecordError):
            self.session.start(Path(self.temporary.name), BUILTIN_BASE, OPERATOR)

    def test_the_base_snapshot_drops_roadmap_keys(self):
        temporary, other = session(dict(BUILTIN_BASE, phases=[{"number": 0}], milestones={"mvp": 0}))
        self.addCleanup(temporary.cleanup)
        self.assertNotIn("phases", other.state().base)
        self.assertNotIn("milestones", other.state().base)

    def test_acceptance_must_say_add_or_set(self):
        decision = self.session.record_decision("A", OWNER)
        with self.assertRaises(RecordError):
            self.session.propose_phase("One", "Do one.", OWNER, implements=[decision],
                                       acceptance={"commands": [["make", "test"]]})
        with self.assertRaises(RecordError):
            self.session.propose_phase("One", "Do one.", OWNER, implements=[decision],
                                       acceptance={"commands": {"add": ["make test"]}})

    def test_phase_names_must_not_collide_once_slugged(self):
        self.session.propose_phase("Admin API", "x", OWNER)
        with self.assertRaises(RecordError):
            self.session.propose_phase("admin  api!", "y", OWNER)

    def test_unknown_decisions_cannot_be_cited(self):
        with self.assertRaises(RecordError):
            self.session.propose_phase("One", "x", OWNER, implements=["D-9"])

    def test_reorder_must_name_every_phase_once(self):
        a = self.session.propose_phase("A", "x", OWNER)
        b = self.session.propose_phase("B", "y", OWNER)
        with self.assertRaises(RecordError):
            self.session.reorder_phases([a], OWNER)
        self.session.reorder_phases([b, a], OWNER)
        self.assertEqual([item["id"] for item in self.session.state().phases], [b, a])

    def test_an_edited_record_is_detected(self):
        self.session.record_decision("A", OWNER)
        lines = self.session.chain.path.read_text().splitlines()
        entry = json.loads(lines[-1]); entry["payload"]["text"] = "B"
        self.session.chain.path.write_text("\n".join([*lines[:-1], json.dumps(entry)]) + "\n")
        with self.assertRaises(AuditIntegrityError):
            self.session.state()

    def test_identity_rejects_unknown_roles(self):
        with self.assertRaises(RecordError):
            Identity("Mallory", "admin")


if __name__ == "__main__":
    unittest.main()
