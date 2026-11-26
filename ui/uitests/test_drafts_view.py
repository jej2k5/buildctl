"""The Drafts view: reading buildctl-drafts' runs, approving or discarding through its CLI, and the paid lever.

Built on the `drafts` demo: phases 0 to 14 are clear, so the MVP milestone is complete, and one run of
four drafts exists. Release notes and the demo script are sourced and waiting; positioning is approved;
the launch post cites a file that does not exist, so it needs you.
"""
import importlib.util
import json
import unittest

from uitests.test_console import Console

DRAFTS_INSTALLED = importlib.util.find_spec("buildctl_drafts") is not None


class DraftsViewTests(Console):
    scenario = "drafts"

    def drafts(self):
        status, value = self.request("GET", "/api/drafts")
        self.assertEqual(status, 200)
        return value

    def test_a_run_lists_its_drafts_in_build_order_with_their_states(self):
        value = self.drafts()
        self.assertEqual(len(value["runs"]), 1)
        run = value["runs"][0]
        self.assertEqual(run["label"], "mvp")
        self.assertEqual([item["kind"] for item in run["drafts"]],
                         ["release-notes", "positioning", "launch-post", "demo-script"])
        states = {item["kind"]: (item["state"], item["verdict"]) for item in run["drafts"]}
        self.assertEqual(states["positioning"], ("approved", "sourced"))
        self.assertEqual(states["launch-post"], ("proposed", "needs you"))
        self.assertEqual(value["waiting"], 3)
        self.assertEqual(run["cost"], 10.0)  # four drafts, the launch post written twice

    def test_an_unbacked_claim_carries_why_it_failed(self):
        launch = next(item for item in self.drafts()["runs"][0]["drafts"] if item["kind"] == "launch-post")
        self.assertEqual((launch["backed"], len(launch["claims"])), (4, 5))
        failed = [claim for claim in launch["claims"] if claim["why"]]
        self.assertEqual([claim["n"] for claim in failed], [5])
        self.assertIn("does not exist", failed[0]["why"])
        backed = launch["claims"][0]
        self.assertEqual((backed["kind"], backed["ref"], backed["line"]), ("file", "docs/PRODUCT_THESIS.md", 5))
        self.assertTrue(backed["quote"])

    def test_the_lever_names_the_completed_milestone_and_counts_both_spends(self):
        lever = self.drafts()["lever"]
        self.assertEqual(lever["milestones"], ["mvp"])
        held = {item["label"]: item["held"] for item in lever["conditions"]}
        self.assertTrue(held["A milestone is complete"])
        self.assertEqual(held["buildctl-drafts is installed"], DRAFTS_INSTALLED)
        cap = next(item for item in lever["conditions"] if item["label"] == "Fits today's cap")
        self.assertIn("drafts $10.00", cap["detail"])
        self.assertEqual(lever["spent"]["drafts"], 10.0)

    def test_a_paid_run_is_refused_in_the_demo_or_without_the_milestone_typed(self):
        status, body = self.request("POST", "/api/drafts", {"action": "make", "confirm": "roadmap"})
        self.assertIn(status, (409, 422))
        if DRAFTS_INSTALLED:
            self.assertEqual(status, 422)
            self.assertIn("mvp", body["error"])
            status, body = self.request("POST", "/api/drafts", {"action": "make", "confirm": "MVP"})
            self.assertEqual(status, 403)
            self.assertIn("buildctl-drafts make --milestone mvp", body["error"])

    def test_approving_an_unknown_draft_is_refused(self):
        status, _ = self.request("POST", "/api/drafts", {"action": "approve", "draft": "20200101-000000/release-notes"})
        self.assertIn(status, (404, 409))

    def test_posting_drafts_needs_the_token(self):
        status, _ = self.request("POST", "/api/drafts", {"action": "approve", "draft": "x"},
                                 headers={"X-Buildctl-Token": "wrong"})
        self.assertEqual(status, 401)

    @unittest.skipUnless(DRAFTS_INSTALLED, "buildctl-drafts is not importable")
    def test_approve_and_discard_run_the_drafts_cli_and_only_record_an_event(self):
        run = self.drafts()["runs"][0]
        notes = next(item for item in run["drafts"] if item["kind"] == "release-notes")
        sign = {"action": "approve", "draft": notes["id"], "digest": notes["digest"]}
        status, _ = self.request("POST", "/api/drafts", dict(sign, confirm="0000000"))
        self.assertEqual(status, 422)
        status, _ = self.request("POST", "/api/drafts", dict(sign, digest="0" * 64, confirm=notes["digest"][:7]))
        self.assertEqual(status, 409)
        status, job = self.request("POST", "/api/drafts", dict(sign, confirm=notes["digest"][:7].upper()))
        self.assertEqual(status, 200, job)
        self.assertTrue(job["command"].startswith("buildctl-drafts approve"))
        self.assertIn("Recorded, not published", job["output"])
        states = {item["kind"]: item["state"] for item in self.drafts()["runs"][0]["drafts"]}
        self.assertEqual(states["release-notes"], "approved")
        self.request("POST", "/api/drafts", {"action": "discard", "draft": f"{run['id']}/launch-post"})
        events = [json.loads(line) for line in
                  (self.build.control_root / "drafts" / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["approved", "approved", "discarded"])


if __name__ == "__main__":
    unittest.main()
