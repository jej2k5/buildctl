"""Caller request identity (`--request-id`), contract version 1.

An orchestrator that ran a buildctl command and lost its own process could only guess from
audit events with no caller identity whether the command landed, is running or finished,
and could not resend safely. These tests pin the contract it relies on:
accepted-before-effect, tagged events, a terminal outcome, and no double execution.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from buildctl import buildctl as cli
from buildctl.buildctl import HumanEscalation, main
from buildctl.supervisor import requests
from buildctl.supervisor.inspect import audit_events
from buildctl.supervisor.state import BuildState, StateStore

from .helpers import repository


class FakeSupervisor:
    """Stands in for a trusted Supervisor: real state store, scripted command effects."""
    calls: list[str] = []
    escalate = False

    def __init__(self, root, dry_run=False):
        self.store = StateStore(Path(os.environ["BUILDCTL_STATE_DIR"]))

    def pause(self):
        FakeSupervisor.calls.append("pause")
        with self.store.lock():
            state = self.store.load()
            state.status = "paused"
            self.store.event(state, "paused")
            if FakeSupervisor.escalate:
                state.status, state.escalation_code = "human_escalation", "PHASE_BLOCKED"
                self.store.event(state, "human_escalation", code="PHASE_BLOCKED")
                raise HumanEscalation("PHASE_BLOCKED")


class Harness(unittest.TestCase):
    def setUp(self):
        temporary, self.repo = repository(); self.addCleanup(temporary.cleanup)
        self.state_dir = self.repo / ".state"
        patcher = mock.patch.dict(os.environ, {"BUILDCTL_STATE_DIR": str(self.state_dir)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.store = StateStore(self.state_dir)
        FakeSupervisor.calls, FakeSupervisor.escalate = [], False

    def bootstrap(self):
        self.store.save(BuildState(governance_sha="gov"))

    def run_cli(self, *argv: str, fake: bool = True) -> tuple[int, str]:
        out = io.StringIO()
        patch = mock.patch.object(cli, "Supervisor", FakeSupervisor) if fake else contextlib.nullcontext()
        with patch, contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--repo", str(self.repo), *argv])
        return code, out.getvalue()

    def record(self, request_id: str) -> dict:
        return self.store.load().requests[request_id]


class AcceptanceTests(Harness):
    def test_a_successful_request_is_accepted_tagged_and_terminal(self):
        self.bootstrap()
        code, _ = self.run_cli("--request-id", "centra-1", "pause")
        self.assertEqual(code, 0)
        record = self.record("centra-1")
        self.assertEqual((record["status"], record["outcome"], record["command"]), ("terminal", "ok", "pause"))
        events = audit_events(self.state_dir, request="centra-1")
        self.assertEqual([e["event_type"] for e in events], ["request_accepted", "paused", "request_terminal"])
        self.assertTrue(all(e["payload"]["request_id"] == "centra-1" for e in events))
        self.assertEqual(self.store.load().status, "paused")

    def test_acceptance_precedes_every_effect(self):
        self.bootstrap()
        self.run_cli("--request-id", "centra-1", "pause")
        types = [e["event_type"] for e in audit_events(self.state_dir)]
        self.assertLess(types.index("request_accepted"), types.index("paused"))

    def test_an_escalation_is_a_terminal_outcome_with_its_code(self):
        self.bootstrap(); FakeSupervisor.escalate = True
        code, _ = self.run_cli("--request-id", "centra-2", "pause")
        self.assertEqual(code, 2)
        record = self.record("centra-2")
        self.assertEqual((record["outcome"], record["code"]), ("escalated", "PHASE_BLOCKED"))

    def test_a_refusal_after_acceptance_is_an_error_outcome_with_no_effect(self):
        """No trust anchor: the real Supervisor refuses. The request is settled, nothing changed."""
        self.bootstrap()
        code, _ = self.run_cli("--request-id", "centra-3", "pause", fake=False)
        self.assertEqual(code, 2)
        record = self.record("centra-3")
        self.assertEqual((record["status"], record["outcome"]), ("terminal", "error"))
        self.assertEqual(self.store.load().status, "idle")

    def test_untagged_commands_write_untagged_events(self):
        self.bootstrap()
        self.run_cli("pause")
        self.assertNotIn("request_id", audit_events(self.state_dir)[-1]["payload"])
        self.assertEqual(self.store.load().requests, {})


class DeduplicationTests(Harness):
    def test_the_same_request_never_runs_twice(self):
        self.bootstrap()
        self.run_cli("--request-id", "centra-1", "pause")
        code, out = self.run_cli("--request-id", "centra-1", "pause")
        self.assertEqual(code, 0)
        self.assertEqual(FakeSupervisor.calls, ["pause"])
        reply = json.loads(out)
        self.assertTrue(reply["duplicate"])
        self.assertEqual(reply["request"]["outcome"], "ok")

    def test_a_duplicate_of_an_escalated_request_exits_nonzero(self):
        self.bootstrap(); FakeSupervisor.escalate = True
        self.run_cli("--request-id", "centra-1", "pause")
        code, _ = self.run_cli("--request-id", "centra-1", "pause")
        self.assertEqual(code, 2)
        self.assertEqual(FakeSupervisor.calls, ["pause"])

    def test_the_same_id_for_different_arguments_is_refused(self):
        self.bootstrap()
        self.run_cli("--request-id", "centra-1", "pause")
        code, _ = self.run_cli("--request-id", "centra-1", "resume")
        self.assertEqual(code, 2)
        self.assertEqual(self.record("centra-1")["command"], "pause")

    def test_nonces_are_bound_but_never_recorded(self):
        args = cli.parser().parse_args(["approve", "--nonce", "secret-nonce"])
        record_args = requests._public(requests.arguments(args))
        self.assertEqual(record_args["nonce"], "<redacted>")
        other = cli.parser().parse_args(["approve", "--nonce", "other-nonce"])
        self.assertNotEqual(requests.digest(requests.arguments(args)), requests.digest(requests.arguments(other)))


class InterruptionTests(Harness):
    def interrupted(self, request_id: str) -> int:
        """A real child process that is killed (os._exit) after acceptance, mid-command."""
        script = (
            "import os, sys\n"
            "from buildctl import buildctl as cli\n"
            "def dying(*a, **k): os._exit(9)\n"
            "cli.dispatch = dying\n"
            f"sys.exit(cli.main(['--repo', {str(self.repo)!r}, '--request-id', {request_id!r}, 'pause']))\n")
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(root)}).returncode

    def test_a_killed_command_stays_accepted_and_is_never_replayed(self):
        self.bootstrap()
        self.assertEqual(self.interrupted("centra-9"), 9)
        self.assertEqual(self.record("centra-9")["status"], "accepted")
        code, out = self.run_cli("--request-id", "centra-9", "pause")
        self.assertEqual(code, 3, "accepted with no outcome must report in-flight, not run again")
        self.assertEqual(FakeSupervisor.calls, [])
        self.assertEqual(json.loads(out)["request"]["status"], "accepted")

    def test_a_resend_while_the_original_still_holds_the_lock_reports_in_flight(self):
        self.bootstrap()
        self.assertIsNone(requests.accept(self.store, "centra-7", "pause", {}))
        with self.store.lock():                     # the original command, still running
            prior = requests.accept(StateStore(self.state_dir), "centra-7", "pause", {})
        self.assertEqual(requests.exit_code(prior), 3)

    def test_a_request_that_never_landed_reports_not_accepted(self):
        self.bootstrap()
        code, out = self.run_cli("request", "show", "never-sent")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "not_accepted")


class BoundaryTests(Harness):
    def test_read_only_commands_refuse_the_flag(self):
        self.bootstrap()
        for argv in (["status"], ["inspect"], ["audit"], ["validate"]):
            code, _ = self.run_cli("--request-id", "centra-1", *argv)
            self.assertEqual(code, 2, argv)
        self.assertEqual(self.store.load().requests, {})

    def test_evidence_prune_is_covered_and_evidence_show_is_not(self):
        self.assertTrue(requests.is_mutating(cli.parser().parse_args(["evidence", "prune", "--phase", "1",
                                                                     "--confirm", "1"])))
        self.assertFalse(requests.is_mutating(cli.parser().parse_args(["evidence", "show", "HEAD"])))

    def test_a_request_requires_bootstrapped_state(self):
        code, _ = self.run_cli("--request-id", "centra-1", "pause")
        self.assertEqual(code, 2)
        self.assertFalse(self.store.path.exists(), "a request must not create build state")

    def test_malformed_ids_are_refused(self):
        self.bootstrap()
        for bad in ("", "-leading", "has space", "x" * 129, "semi;colon"):
            code, _ = self.run_cli(f"--request-id={bad}", "pause")
            self.assertEqual(code, 2, bad)
        self.assertEqual(FakeSupervisor.calls, [])

    def test_request_show_returns_the_record(self):
        self.bootstrap()
        self.run_cli("--request-id", "centra-1", "pause")
        code, out = self.run_cli("request", "show", "centra-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "ok")

    def test_state_written_before_the_contract_still_loads(self):
        self.bootstrap()
        value = json.loads(self.store.path.read_text()); value.pop("requests")
        self.store.path.write_text(json.dumps(value))
        self.assertEqual(self.store.load().requests, {})

    def test_a_held_lock_leaves_the_request_accepted_rather_than_hanging(self):
        self.bootstrap()
        self.assertIsNone(requests.accept(self.store, "centra-1", "pause", {}))
        with self.store.lock():
            with self.assertRaises(cli.SupervisorLocked):
                requests.finish(StateStore(self.state_dir), "centra-1", "ok", None, None, wait_seconds=0.3)
        self.assertEqual(self.record("centra-1")["status"], "accepted")

    def test_the_contract_is_versioned(self):
        self.assertEqual(requests.contract()["version"], 1)


if __name__ == "__main__":
    unittest.main()
