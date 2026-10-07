import tempfile
import unittest
import subprocess
from pathlib import Path

from buildctl.supervisor.debate import DebateAdapter, validate_debate_result, write_adr
from buildctl.supervisor.agents import AgentError
from types import SimpleNamespace


def result(status):
    return {"status":status,"decision":"d","finding_dispositions":[],"required_changes":[],"human_decisions":[],"revisit_when":[]}


class DebateTests(unittest.TestCase):
    def test_failed_and_agree_to_disagree_never_create_accepted_adr(self):
        with tempfile.TemporaryDirectory() as directory:
            for status in ("failed","agree_to_disagree"):
                with self.assertRaises(ValueError): write_adr(Path(directory)/status,{"title":"t","debate_id":"d","governance_sha":"g","base_sha":"b","evidence_digest":"e","finding_ids":[]},result(status))

    def test_agreement_adr_is_typed_and_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"adr.md"; write_adr(path,{"title":"t","debate_id":"d","governance_sha":"g","base_sha":"b","evidence_digest":"e","finding_ids":["f"]},result("agreement"))
            self.assertIn("Status: Accepted",path.read_text()); self.assertIn("Finding IDs: f",path.read_text())

    def test_interactive_request_is_bound_and_resume_consumes_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); output=root/"DEBATE_RESULT-1.json"
            adapter=DebateAdapter(None,root,{"roles":{"arbiter":{"mode":"interactive"}}})
            evidence={"digest":"e","delta_id":"d","governance_sha":"g","base_sha":"b"}
            with self.assertRaisesRegex(AgentError,"INTERACTIVE"):
                adapter.run(SimpleNamespace(number=2),[],evidence,output,1)
            request=(root/"DEBATE_REQUEST.json").read_text()
            self.assertIn('"delta_id": "d"',request); self.assertIn('"governance_sha": "g"',request)
            output.write_text(__import__('json').dumps(result("agreement")))
            self.assertEqual(adapter.run(SimpleNamespace(number=2),[],evidence,output,1)["status"],"agreement")

    def test_headless_evidence_is_json_serializable_and_malformed_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"templates").mkdir(); (root/"templates/DEBATE_RESULT.json").write_text("{}")
            class Runner:
                def run(self,*args): return result("agreement")
            adapter=DebateAdapter(Runner(),root,{"roles":{"arbiter":{"mode":"headless"}}})
            evidence={"digest":"e","delta_id":"d","governance_sha":"g","base_sha":"b","coverage":[{"path":"x"}]}
            self.assertEqual(adapter.run(SimpleNamespace(number=1),[],evidence,root/"o",1)["status"],"agreement")
            with self.assertRaises(ValueError): validate_debate_result({"status":"agreement"})

    def test_headless_timeout_propagates_without_forging_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"templates").mkdir(); (root/"templates/DEBATE_RESULT.json").write_text("{}")
            class Runner:
                def run(self,*args): raise subprocess.TimeoutExpired("arbiter",1)
            adapter=DebateAdapter(Runner(),root,{"roles":{"arbiter":{"mode":"headless"}}})
            evidence={"digest":"e","delta_id":"d","governance_sha":"g","base_sha":"b","coverage":[]}
            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.run(SimpleNamespace(number=1),[],evidence,root/"missing",1)
