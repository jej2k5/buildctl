"""The console's step track is computed from what the kernel recorded, never guessed."""
import json
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.phaseview import phase_steps, step_of
from buildctl.supervisor.state import BuildState

CONFIG = {"documentation": {"mode": "required", "paths": ["README.md", "docs/**"]},
          "phases": [{"number": 1}, {"number": 2, "documentation": False}]}


def op(kind, status="completed", start="2026-11-25T10:00:00+00:00", end="2026-11-25T10:00:30+00:00", attempt=1, phase=1,
       output=None):
    return {"id": f"phase-{phase}:{kind}:{attempt}", "kind": kind, "status": status, "started_at": start,
            "completed_at": end if status == "completed" else None, "output_path": output}


class StepTrackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.control = Path(directory.name)
        (self.control / "artifacts" / "phase-1").mkdir(parents=True)

    def steps(self, state, ops, config=CONFIG, lock=True, number=1):
        return {step["name"]: step for step in phase_steps(state, number, ops, config, lock, self.control)["steps"]}

    def test_operations_map_to_steps_by_id(self):
        self.assertEqual([step_of(f"phase-1:{kind}:1") for kind in
                          ("implement", "remediate-2", "adr-implement-1", "gate-3", "document-2-1", "doc-gate-2-1", "review-4")],
                         ["Implement", "Implement", "Implement", "Gates", "Docs", "Docs", "Review"])
        self.assertIsNone(step_of("phase-1:debate-1-turn-2:1"))

    def test_the_documenter_in_flight_is_occupied_and_counts_its_pass(self):
        state = BuildState(current_phase=1, phase_attempts={"1": 1}, status="running")
        ops = [op("implement"), op("gate-1", start="2026-11-25T10:01:00+00:00", end="2026-11-25T10:01:02+00:00"),
               op("document-1-0", start="2026-11-25T10:02:00+00:00"), op("doc-gate-1-0", start="2026-11-25T10:03:00+00:00"),
               op("document-1-1", "started", start="2026-11-25T10:04:00+00:00")]
        steps = self.steps(state, ops)
        self.assertEqual([steps[n]["aspect"] for n in ("Implement", "Gates", "Docs", "Review", "Commit")],
                         ["clear", "clear", "occupied", "pending", "pending"])
        self.assertEqual(steps["Docs"]["meta"], "pass 2 of 4 · 1 send-back")
        self.assertEqual(self.steps(state, ops, lock=False)["Docs"]["aspect"], "unknown", "started without the lock")

    def test_optional_mode_reports_a_recorded_gate_failure(self):
        config = dict(CONFIG, documentation={"mode": "optional", "paths": ["README.md"]})
        folder = self.control / "artifacts" / "phase-1"
        (folder / "DOC_RESULT-1-0.json").write_text(json.dumps({"changes": [{"path": "README.md"}]}))
        (folder / "DOC_GATE_RESULT-1-0.json").write_text(json.dumps(
            {"passed": False, "documentation": {"failures": [{"gate": "links", "problems": ["x"]}]}}))
        state = BuildState(current_phase=1, phase_attempts={"1": 1}, status="running")
        ops = [op("implement"), op("gate-1", start="2026-11-25T10:01:00+00:00"),
               op("document-1-0", start="2026-11-25T10:02:00+00:00", output=str(folder / "DOC_RESULT-1-0.json")),
               op("doc-gate-1-0", start="2026-11-25T10:03:00+00:00", output=str(folder / "DOC_GATE_RESULT-1-0.json")),
               op("review-1", "started", start="2026-11-25T10:04:00+00:00")]
        steps = self.steps(state, ops, config)
        self.assertEqual(steps["Docs"]["aspect"], "clear")
        self.assertIn("1 gate failed · recorded", steps["Docs"]["meta"])
        self.assertEqual(steps["Review"]["aspect"], "occupied")

    def test_a_phase_that_opts_out_and_a_build_without_documentation(self):
        state = BuildState(current_phase=2, phase_attempts={"2": 1}, status="running")
        steps = self.steps(state, [op("implement", "started", phase=2)], number=2)
        self.assertEqual((steps["Docs"]["meta"], steps["Docs"].get("skipped")), ("off for this phase", True))
        plain = phase_steps(state, 2, [], {"phases": [{"number": 2}]}, True, self.control)
        self.assertEqual([step["name"] for step in plain["steps"]], ["Implement", "Gates", "Review", "Commit"])
        self.assertIsNone(plain["documentation"])

    def test_a_remediation_after_review_moves_the_track_back_to_implement(self):
        state = BuildState(current_phase=1, phase_attempts={"1": 1}, status="running")
        ops = [op("implement"), op("gate-1", start="2026-11-25T10:01:00+00:00"), op("document-1-0", start="2026-11-25T10:02:00+00:00"),
               op("review-1", start="2026-11-25T10:03:00+00:00"), op("remediate-1", "started", start="2026-11-25T10:04:00+00:00")]
        steps = self.steps(state, ops)
        self.assertEqual(steps["Implement"]["aspect"], "occupied")
        self.assertEqual(steps["Review"]["aspect"], "pending")

    def test_only_the_latest_attempt_counts(self):
        state = BuildState(current_phase=1, phase_attempts={"1": 2}, status="running")
        steps = self.steps(state, [op("implement", attempt=1), op("gate-1", attempt=1), op("implement", "started", attempt=2)])
        self.assertEqual(steps["Gates"]["aspect"], "pending")

    def test_a_committed_phase_is_clear_to_the_end(self):
        state = BuildState(completed_phases=[1], phase_attempts={"1": 1},
                           history=[{"event": "evidence_sealed", "phase": 1, "code_commit": "e654272abc"}])
        steps = self.steps(state, [op("implement"), op("gate-1"), op("document-1-0"), op("review-1")])
        self.assertTrue(all(step["aspect"] == "clear" for step in steps.values()))
        self.assertEqual(steps["Commit"]["meta"], "e654272")


class LatestAttemptTests(unittest.TestCase):
    """Artifact names carry no attempt, so a retried phase must read what its own operations wrote."""

    def test_a_stale_higher_numbered_file_from_an_earlier_attempt_is_ignored(self):
        from buildctl.supervisor.phaseview import documentation_detail
        from buildctl.supervisor.state import Operation
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            folder = control / "artifacts" / "phase-1"; folder.mkdir(parents=True)
            (folder / "DOC_RESULT-1-1.json").write_text(json.dumps({"summary": "attempt 1, rejected", "changes": []}))
            (folder / "DOC_RESULT-1-0.json").write_text(json.dumps({"summary": "attempt 2", "changes": []}))
            (folder / "REVIEW_RESULT-2.json").write_text(json.dumps({"verdict": "REMEDIATE"}))
            (folder / "REVIEW_RESULT-3.json").write_text(json.dumps({"verdict": "PASS"}))
            state = BuildState(current_phase=1, phase_attempts={"1": 2}, status="running", operations={
                "phase-1:document-1-1:1": Operation("phase-1:document-1-1:1", "document-1-1", 1, "completed",
                                                    output_path=str(folder / "DOC_RESULT-1-1.json"), started_at="2026-11-25T09:00:00+00:00"),
                "phase-1:document-1-0:2": Operation("phase-1:document-1-0:2", "document-1-0", 1, "completed",
                                                    output_path=str(folder / "DOC_RESULT-1-0.json"), started_at="2026-11-25T10:00:00+00:00")})
            detail = documentation_detail(control, control, state, 1, CONFIG)
            self.assertEqual([item["summary"] for item in detail["results"]], ["attempt 2"])
            ops = [op("implement", attempt=2), op("review-3", attempt=2, start="2026-11-25T10:05:00+00:00",
                                                    output=str(folder / "REVIEW_RESULT-3.json"))]
            steps = {s["name"]: s for s in phase_steps(state, 1, ops, CONFIG, True, control)["steps"]}
            self.assertIn("PASS", steps["Review"]["meta"])

    def test_a_documenter_path_outside_the_repository_is_never_read(self):
        from buildctl.supervisor.phaseview import _diff
        with tempfile.TemporaryDirectory() as directory:
            state = BuildState(current_phase=1, workspace=directory, base_sha="HEAD")
            for path in ("/etc/hosts", "../outside.md"):
                self.assertEqual(_diff(Path(directory), state, 1, path, lambda *a, **k: None)["available"], False, path)

    def test_a_wait_is_drawn_as_waiting_not_stopped(self):
        state = BuildState(current_phase=1, phase_attempts={"1": 1}, status="human_escalation",
                           escalation_code="DEBATE_AWAITING_DECISION")
        with tempfile.TemporaryDirectory() as directory:
            steps = {s["name"]: s for s in phase_steps(state, 1, [op("implement"), op("review-1")], CONFIG, False, Path(directory))["steps"]}
        self.assertEqual(steps["Review"]["aspect"], "caution")
        self.assertIn("waiting for you", steps["Review"]["meta"])


if __name__ == "__main__":
    unittest.main()
