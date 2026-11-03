"""The console server: who may talk to it, and what pulling a lever really does.

Run from the repository root with the UI on the path:
    PYTHONPATH=ui python3 -m unittest discover -s ui/uitests -t ui
"""
import http.client
import json
import os
import shutil
import signal
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from buildctl.supervisor.inspect import audit_events
from buildctl_ui.actions import Build, Refused
from buildctl_ui.demo import build
from buildctl_ui.server import handler_for

TOKEN = "test-token"


class Console(unittest.TestCase):
    scenario = "checkpoint"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="buildctl-ui-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        repo, control = build(self.root, self.scenario)
        self.build = Build.open(repo, control, demo=self.scenario)
        port = [0]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.build, TOKEN, port))
        port[0] = self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Host": f"127.0.0.1:{self.port}", "X-Buildctl-Token": TOKEN}
        if body is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, (json.loads(payload) if payload and response.getheader("Content-Type", "").startswith("application/json") else payload)

    def model(self):
        return self.request("GET", "/api/model")[1]


class AccessTests(Console):
    def test_reads_need_the_token(self):
        self.assertEqual(self.request("GET", "/api/model", headers={"X-Buildctl-Token": "wrong"})[0], 401)
        self.assertEqual(self.request("GET", "/api/model")[0], 200)

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.request("GET", "/api/model", headers={"Host": "evil.example:80"})[0], 421)

    def test_state_change_from_another_origin_is_refused(self):
        status, _ = self.request("POST", "/api/pull", {"action": "pause", "generation": 0},
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_form_posts_are_refused(self):
        status, _ = self.request("POST", "/api/pull", None, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)

    def test_static_files_carry_a_strict_policy(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse(); response.read()
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_artifacts_cannot_escape_their_directory(self):
        status, _ = self.request("GET", "/api/artifact?path=/etc/hosts")
        self.assertEqual(status, 403)


class ActivityTests(Console):
    scenario = "running"

    def setUp(self):
        super().setUp()
        # The demo's stand-in for a run process would otherwise outlive the test by an hour.
        pid = json.loads((self.build.control_root / "supervisor.lock").read_text())["pid"]
        self.addCleanup(os.kill, pid, signal.SIGKILL)

    def get(self, path):
        status, body = self.request("GET", path)
        return status, json.loads(body) if isinstance(body, (bytes, str)) else body

    def test_the_feed_needs_the_token(self):
        status, _ = self.request("GET", "/api/activity?phase=12", headers={"X-Buildctl-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_the_feed_labels_who_vouches_for_each_row(self):
        status, body = self.get("/api/activity?phase=12")
        self.assertEqual(status, 200)
        self.assertEqual({row["source"] for row in body["rows"]}, {"observed", "agent_reported"})
        self.assertEqual(body["operations"][0]["state"], "open")
        self.assertEqual(body["operations"][0]["files_reported"], 1)

    def test_a_row_opens_its_part_of_the_full_record(self):
        _, body = self.get("/api/activity?phase=12")
        row = next(item for item in body["rows"] if item["kind"] == "ran")
        status, lines = self.get(f"/api/record?phase=12&record={row['record']}&seq={row['seq']}")
        self.assertEqual(status, 200)
        self.assertIn("aggregated_output", lines[0]["text"])

    def test_a_record_name_is_not_a_path(self):
        status, _ = self.request("GET", "/api/record?phase=12&record=..%2F..%2Fstate&seq=0")
        self.assertEqual(status, 404)

    def test_the_model_says_recording_is_on(self):
        self.assertTrue(self.model()["evidence"]["recording"])


class CeremonyTests(Console):
    def setUp(self):
        super().setUp()
        model = self.model()
        if not model["trust"]["ok"]:
            self.skipTest("this machine cannot pin both agent CLIs, so the demo build is untrusted")
        self.approval = model["state"]["pending_approval"]
        self.generation = model["state"]["generation"]

    def test_wrong_evidence_is_refused_before_the_kernel_runs(self):
        status, body = self.request("POST", "/api/pull", {"action": "approve", "generation": self.generation,
                                                          "confirm": "0000000"})
        self.assertEqual(status, 422)
        self.assertIn("does not match", body["error"])
        self.assertIsNotNone(self.model()["state"]["pending_approval"])

    def test_stale_view_is_refused(self):
        status, body = self.request("POST", "/api/pull", {"action": "approve", "generation": self.generation - 1,
                                                          "confirm": self.approval["tree_hash"][:7]})
        self.assertEqual(status, 409)
        self.assertIn("changed since you looked", body["error"])

    def test_signing_the_evidence_approves_through_the_kernel(self):
        status, body = self.request("POST", "/api/pull", {"action": "approve", "generation": self.generation,
                                                          "confirm": self.approval["tree_hash"][:7].upper()})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["command"].startswith("buildctl approve --nonce "))
        self.assertNotIn(self.approval["nonce"], body["command"], "the nonce is shortened for display")
        model = self.model()
        self.assertIsNone(model["state"]["pending_approval"])
        events = [item["event_type"] for item in audit_events(self.build.control_root)]
        self.assertEqual(events[-1], "checkpoint_approved")

    def test_demo_never_spends(self):
        with self.assertRaises(Refused) as caught:
            self.build.pull("trust", generation=self.generation, confirm=self.model()["repo"]["head_sha"][:12])
        self.assertIn("demo build", str(caught.exception))


class DebateDecisionTests(Console):
    scenario = "debate"

    def setUp(self):
        super().setUp()
        self.generation = self.model()["state"]["generation"]

    def decide(self, **body):
        return self.request("POST", "/api/pull", {"action": "decide", "generation": self.generation, **body})

    def test_the_stop_shows_the_debate_turn_by_turn(self):
        model = self.model()
        debate = model["current"]["debate"]
        self.assertTrue(debate["awaiting_decision"])
        self.assertEqual([turn["side"] for turn in debate["turns"]],
                         ["implementer", "reviewer", "implementer", "reviewer"])
        self.assertEqual([item["id"] for item in debate["findings"]], ["f2"])
        self.assertEqual(model["escalation"]["aspect"], "caution")

    def test_a_decision_needs_an_answer(self):
        status, body = self.decide()
        self.assertEqual(status, 422)
        self.assertIn("choose an answer", body["error"])

    def test_your_own_ruling_needs_its_text(self):
        status, body = self.decide(answer="own", text="  ")
        self.assertEqual(status, 422)
        self.assertIn("write your ruling", body["error"])

    def test_a_decision_goes_through_the_kernel(self):
        if not self.model()["trust"]["ok"]:
            self.skipTest("this machine cannot pin both agent CLIs, so the demo build is untrusted")
        status, body = self.decide(answer="own", text="Record an attempt from the replay sender.")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["command"].startswith("buildctl debate decide --nonce "))
        state = self.model()["state"]
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["pending_debate"]["answer"], "own")
        events = [item["event_type"] for item in audit_events(self.build.control_root)]
        self.assertEqual(events[-1], "debate_decided")


if __name__ == "__main__":
    unittest.main()
