from __future__ import annotations

import io
import json
import unittest
from pathlib import Path

from buildctl_discover.record import BUILTIN_BASE
from buildctl_discover.render import digest, read_proposal
from buildctl_discover.server import Server

from .fakes import reviewer
from .helpers import OPERATOR, OWNER, session


class Mcp(unittest.TestCase):
    def setUp(self):
        self.temporary, self.session = session(BUILTIN_BASE)
        self.addCleanup(self.temporary.cleanup)
        self.server = Server(self.session, Path(self.temporary.name), OWNER, runner=reviewer())
        self.ids = iter(range(1, 1000))

    def request(self, method, params=None, server=None):
        message = {"jsonrpc": "2.0", "id": next(self.ids), "method": method}
        if params is not None:
            message["params"] = params
        return (server or self.server).handle(message)

    def call(self, tool, **arguments):
        result = self.request("tools/call", {"name": tool, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def test_initialize_echoes_a_known_protocol_version_and_records_the_client(self):
        result = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "codex-mcp-client", "version": "1"}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "buildctl-discover")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(self.server.client, "codex-mcp-client")

    def test_an_unknown_protocol_version_gets_the_newest(self):
        result = self.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_an_owner_is_not_offered_operator_tools(self):
        names = {tool["name"] for tool in self.request("tools/list")["result"]["tools"]}
        self.assertNotIn("accept_risk", names)
        self.assertIn("record_decision", names)
        self.assertNotIn("finalize", names)
        operator = Server(self.session, Path(self.temporary.name), OPERATOR)
        names = {tool["name"] for tool in self.request("tools/list", server=operator)["result"]["tools"]}
        self.assertIn("accept_risk", names)
        self.assertNotIn("finalize", names)

    def test_an_owner_calling_an_operator_tool_is_refused(self):
        response = self.request("tools/call", {"name": "accept_risk", "arguments": {"target": "Q-1", "reason": "x"}})
        self.assertIn("error", response)

    def test_tool_schemas_require_what_handlers_need(self):
        for tool in self.request("tools/list")["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])

    def test_a_mutation_records_the_pinned_speaker_and_rerenders(self):
        failed, result = self.call("record_decision", text="Deliveries are retried.", section="Delivery")
        self.assertFalse(failed)
        self.assertEqual(result["id"], "D-1")
        self.assertEqual(self.session.state().decisions["D-1"]["by"], OWNER.as_dict())
        self.assertEqual(result["proposal_digest"], digest(read_proposal(self.session.proposal_dir)))

    def test_record_errors_come_back_as_tool_errors(self):
        failed, text = self.call("record_decision", text="x", supersedes="D-9")
        self.assertTrue(failed)
        self.assertIn("D-9", text)

    def test_bad_arguments_are_tool_errors_not_crashes(self):
        failed, text = self.call("record_decision", words="x")
        self.assertTrue(failed)
        self.assertIn("unknown argument: words", text)
        self.assertIn("missing argument: text", text)

    def test_the_interview_runs_end_to_end_over_mcp(self):
        self.call("set_thesis", text="Teams can see every delivery.")
        _, decision = self.call("record_decision", text="Deliveries are retried.")
        _, phase = self.call("propose_phase", name="Retry loop", objective="Retry failed deliveries.",
                             implements=[decision["id"]],
                             acceptance={"commands": {"add": [["{python}", "-m", "unittest", "tests.test_retry"]]}})
        self.call("set_milestones", milestones={"mvp": phase["id"]}, checkpoints_before=[phase["id"]],
                  mvp_completion=phase["id"])
        _, result = self.call("check")
        self.assertEqual([item["rule"] for item in result["failures"]], ["review"])
        failed, review = self.call("request_review")
        self.assertFalse(failed, review)
        _, result = self.call("check")
        self.assertTrue(result["passed"], result["failures"])

    def test_the_interview_guide_is_served_as_a_prompt(self):
        self.assertEqual(self.request("prompts/list")["result"]["prompts"][0]["name"], "discovery_interview")
        text = self.request("prompts/get", {"name": "discovery_interview"})["result"]["messages"][0]["content"]["text"]
        self.assertIn("one question at a time", text.lower())
        self.assertNotIn("—", text)

    def test_unknown_methods_are_jsonrpc_errors(self):
        self.assertEqual(self.request("resources/list")["error"]["code"], -32601)

    def test_serve_speaks_one_json_message_per_line(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})]
        out = io.StringIO()
        self.server.serve(io.StringIO("\n".join(lines) + "\n"), out, io.StringIO())
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([reply.get("id") for reply in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
