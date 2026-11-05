from __future__ import annotations

import json
import unittest
from pathlib import Path

from buildctl_discover.check import check
from buildctl_discover.record import RecordError
from buildctl_discover.render import read_proposal, render, write_proposal
from buildctl_discover.review import parse, request_review, vendor_warnings

from .fakes import reviewer
from .helpers import OWNER
from .test_check import GateCase


class Review(GateCase):
    def passes(self):
        state = self.session.state()
        write_proposal(self.session.proposal_dir, render(state))
        return check(state, self.repo, read_proposal(self.session.proposal_dir))["passed"]

    def test_a_clean_review_lets_the_gate_pass(self):
        runner = reviewer()
        result = request_review(self.session, self.session_identity(), runner=runner)
        self.assertEqual(result["findings"], [])
        self.assertTrue(self.passes())
        self.assertEqual(runner.calls[0][0], "claude")
        self.assertIn("--no-session-persistence", runner.calls[0])

    def test_findings_are_numbered_and_block_at_high(self):
        result = request_review(self.session, self.session_identity(), runner=reviewer(
            [{"severity": "HIGH", "target": self.phase, "summary": "The gate passes on an empty module."}]))
        self.assertEqual(result["findings"][0]["id"], "F-1")
        self.assertFalse(self.passes())

    def test_a_fenced_answer_is_accepted(self):
        request_review(self.session, self.session_identity(), runner=reviewer(wrap=True))
        self.assertTrue(self.passes())

    def test_a_review_of_another_digest_is_refused(self):
        with self.assertRaises(RecordError):
            request_review(self.session, self.session_identity(), runner=reviewer(digest="0" * 64))
        self.assertEqual(self.session.state().reviews, [])

    def test_a_failing_reviewer_records_nothing(self):
        with self.assertRaises(RecordError):
            request_review(self.session, self.session_identity(), runner=reviewer(returncode=3))
        self.assertEqual(self.session.state().reviews, [])

    def test_the_prompt_carries_the_proposal_but_not_the_risk_list(self):
        runner = reviewer()
        request_review(self.session, self.session_identity(), runner=runner)
        prompt = runner.calls[0][2]
        self.assertIn("## `build.yaml`", prompt)
        self.assertIn("Retry failed deliveries.", prompt)
        self.assertNotIn("## `docs/DISCOVERY_RISKS.md`", prompt)

    def test_parse_rejects_the_wrong_protocol_and_bad_findings(self):
        with self.assertRaises(RecordError):
            parse('{"protocol_version": "buildctl-review-v2", "render_digest": "x", "findings": []}', "x")
        with self.assertRaises(RecordError):
            parse('{"protocol_version": "buildctl-roadmap-review-v1", "render_digest": "x", '
                  '"findings": [{"severity": "SEVERE", "target": "PH-1", "summary": "s"}]}', "x")
        with self.assertRaises(RecordError):
            parse("no json here", "x")

    def test_parse_reads_the_answer_out_of_an_event_stream(self):
        answer = ('```json\n{"protocol_version": "buildctl-roadmap-review-v1", "render_digest": "x", '
                  '"findings": []}\n```')
        claude = "\n".join(json.dumps(event) for event in (
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "{not the answer}"}]}},
            {"type": "result", "subtype": "success", "result": answer}))
        codex = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "{draft}"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
            {"type": "turn.completed"}))
        self.assertEqual(parse(claude, "x"), [])
        self.assertEqual(parse(codex, "x"), [])
        with self.assertRaises(RecordError):
            parse(json.dumps({"type": "system"}) + "\n" + json.dumps({"type": "assistant"}), "x")

    def test_same_vendor_interviews_are_flagged(self):
        self.assertTrue(vendor_warnings("claude-code", {"vendor": "anthropic"}))
        self.assertEqual(vendor_warnings("codex-mcp-client", {"vendor": "anthropic"}), [])

    def session_identity(self):
        return OWNER


if __name__ == "__main__":
    unittest.main()
