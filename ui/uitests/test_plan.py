"""The Plan view: the roadmap in the operator's words, read from the governed discovery documents.

    PYTHONPATH=ui python3 -m unittest discover -s ui/uitests -t ui
"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from buildctl_ui.actions import Build, read_plan
from buildctl_ui.demo import build

WEB = Path(__file__).resolve().parents[1] / "buildctl_ui" / "web"

ROADMAP = """# Roadmap

0. Intake
   Senders can deliver webhooks and get a receipt.
1. Delivery
2. Retries
   Failed deliveries retry with backoff.

The MVP is complete after phase 2.
The build stops for operator approval before phases 1.

Before phase 1, the operator: Send a real webhook.
After phase 2, the operator: Check the dead-letter queue.
"""
DECISIONS = """# Discovery decisions

## Delivery

- **D-1** (high confidence; Sam, owner, 2026-10-07): Retry with exponential backoff.

## Superseded

- **D-2**, superseded by D-1: Retry every second.
"""
RISKS = """# Accepted risks

- **F-3**: HIGH finding: Ordering is not tested across regions. Accepted by Ada, operator: covered by the staging checkpoint.
"""


class Parsing(unittest.TestCase):
    def test_outcomes_notes_decisions_and_risks(self):
        plan = read_plan(ROADMAP, DECISIONS, RISKS, "abc")
        self.assertEqual(plan["outcomes"], {0: "Senders can deliver webhooks and get a receipt.",
                                            2: "Failed deliveries retry with backoff."})
        self.assertEqual(plan["notes"], {"before": {1: "Send a real webhook."}, "after": {2: "Check the dead-letter queue."}})
        self.assertEqual([d["id"] for d in plan["decisions"]], ["D-1"])
        self.assertEqual(plan["risks"], [{"id": "F-3", "severity": "HIGH", "text": "Ordering is not tested across regions.",
                                          "accepted_by": "Ada, operator", "reason": "covered by the staging checkpoint."}])
        self.assertTrue(plan["has_lines"])

    def test_a_roadmap_without_lines_says_so(self):
        plan = read_plan("# Roadmap\n\n0. Intake\n1. Delivery\n", "", "", None)
        self.assertEqual((plan["outcomes"], plan["has_lines"]), ({}, False))


class Model(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="buildctl-ui-plan-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        repo, control = build(self.root, "checkpoint")
        self.build = Build.open(repo, control, demo="checkpoint")

    def test_the_model_reads_the_plan_at_the_trusted_commit(self):
        m = self.build.model()
        plan = m["plan"]
        self.assertEqual(len(plan["outcomes"]), len(m["phases"]))
        self.assertEqual(sorted(plan["notes"]["before"]), [0, 6, 14, 15])
        self.assertIn(14, plan["notes"]["after"])
        self.assertTrue(plan["sha"])

    def test_the_terminal_ui_draws_the_plan_tab(self):
        from buildctl_ui.tui import BuildctlTUI
        from textual.widgets import Static

        async def drive():
            app = BuildctlTUI(self.build)
            async with app.run_test(size=(140, 60)) as pilot:
                await pilot.pause(1.0)
                await pilot.press("6")
                await pilot.pause(0.5)
                text = "\n".join(str(w.render()) for w in app.query_one("#plan-view").query(Static))
                return text
        text = asyncio.run(drive())
        self.assertIn("Waiting for you before phase 15", text)
        self.assertIn("Decide whether ordering is worth the extra latency", text)
        self.assertIn("Phases 0 to 14", text)


class Web(unittest.TestCase):
    def test_the_rail_and_router_offer_the_plan(self):
        self.assertIn('href="#/plan" data-route="plan"', (WEB / "index.html").read_text())
        app = (WEB / "app.js").read_text()
        self.assertIn("plan: viewPlan", app)
        self.assertIn('6: "plan"', app)


if __name__ == "__main__":
    unittest.main()
