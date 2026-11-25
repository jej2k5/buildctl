"""How the console shows the documentation role: the step track, the Docs evidence and the settings.

Built on the `docs` demo: phase 12's documenter was sent back once by its gates, then edited
`src/relay/replay.py` and stopped at DOC_SCOPE_VIOLATION; phase 10 opts out with
`documentation: false`.
"""
import unittest

from uitests.test_console import Console


class DocumentationViewTests(Console):
    scenario = "docs"

    def phase(self, model, number):
        return next(p for p in model["phases"] if p["number"] == number)

    def test_the_step_track_stops_at_docs(self):
        model = self.model()
        track = self.phase(model, 12)["track"]
        self.assertEqual(track["documentation"], "required")
        steps = {step["name"]: step for step in track["steps"]}
        self.assertEqual([step["name"] for step in track["steps"]], ["Implement", "Gates", "Docs", "Review", "Commit"])
        self.assertEqual((steps["Implement"]["aspect"], steps["Gates"]["aspect"]), ("clear", "clear"))
        self.assertEqual(steps["Docs"]["aspect"], "danger")
        self.assertIn("outside the docs", steps["Docs"]["meta"])
        self.assertEqual((steps["Review"]["aspect"], steps["Commit"]["aspect"]), ("pending", "pending"))
        self.assertEqual(self.phase(model, 12)["doc_passes"], 2)

    def test_a_phase_that_opts_out_shows_the_step_off(self):
        phase = self.phase(self.model(), 10)
        docs = next(step for step in phase["track"]["steps"] if step["name"] == "Docs")
        self.assertTrue(docs["skipped"])
        self.assertEqual(docs["meta"], "off for this phase")
        self.assertEqual((phase["documentation"], phase["doc_passes"]), ("off", None))

    def test_the_docs_evidence_leads_with_what_was_out_of_scope(self):
        status, detail = self.request("GET", "/api/documentation?phase=12")
        self.assertEqual(status, 200)
        self.assertEqual(detail["out_of_scope"], ["src/relay/replay.py"])
        self.assertEqual([item["file"] for item in detail["results"]], ["DOC_RESULT-1-0.json", "DOC_RESULT-1-1.json"])
        self.assertFalse(detail["gates"][-1]["passed"])
        code = detail["diffs"]["src/relay/replay.py"]
        self.assertTrue(code["available"])
        self.assertEqual((code["source"], code["added"], code["removed"]), ("workspace", 3, 0))
        readme = detail["diffs"]["README.md"]
        self.assertTrue(any(line.startswith("+") and "normal delivery path" in line for line in readme["lines"]))

    def test_setup_carries_the_settings(self):
        summary = self.model()["documentation"]
        self.assertEqual(summary["mode"], "required")
        self.assertIn("docs/ROADMAP.md", summary["governed_inside"])
        self.assertEqual(summary["documenter"]["model_family"], "codex")
        self.assertEqual(summary["warnings"], [])


class NoDocumentationTests(Console):
    scenario = "checkpoint"

    def test_no_docs_step_and_no_settings(self):
        model = self.model()
        self.assertIsNone(model["documentation"])
        for phase in model["phases"]:
            self.assertNotIn("Docs", [step["name"] for step in phase["track"]["steps"]])
            self.assertIsNone(phase["doc_passes"])


if __name__ == "__main__":
    unittest.main()
