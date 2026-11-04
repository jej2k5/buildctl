"""Debates buildctl runs itself: `roles.arbiter.mode: debate`.

Codex, as the implementer, argues for the code; Claude, as the reviewer, argues for the findings.
Each turn is its own operation. A debate closes only when one side accepts, by hash, the exact
proposal the other side made on the turn before. If the turns run out, the build waits for the
operator, who rules in the console or with `debate decide`, and the same attempt resumes.
"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor import levers
from buildctl.supervisor.agents import AgentError, RoleRunner
from buildctl.supervisor.budget import BudgetLedger
from buildctl.supervisor.debate import (operator_result, outcome, proposal_digest, side_for_turn, validate_turn,
                                        write_adr)
from buildctl.supervisor.evidence import discrepancies
from buildctl.supervisor.inspect import explain
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from buildctl.supervisor.phases import ConfigurationError, validate_config
from buildctl.supervisor.review import OPERATOR_ACCEPTED_STATUS, accumulate_findings, operator_rulings
from buildctl.supervisor.state import BuildState, DebateDecision, StateStore
from tests.test_kernel_trust import CONFIG as BASE_CONFIG

FINDING = {"id": "f1", "severity": "HIGH", "kind": "architecture", "path": "src/x.py", "line": 1,
           "claim_key": "layering", "claim": "The store reaches into the UI.", "rationale": "r",
           "required_change": "Move the call behind the adapter.", "evidence_digest": "e",
           "violated_invariant": "layers", "treatment": "debate", "status": "open", "occurrences": 1}
EVIDENCE = {"digest": "evidence-1", "delta_id": "delta", "base_sha": "base", "changed_files": ["src/x.py"],
            "diff": "+ change"}
CONFIG = {"roles": {"implementer": {"auth_mode": "none", "network": False, "credential_env": []},
                    "reviewer": {"auth_mode": "none", "network": False, "credential_env": []},
                    "arbiter": {"mode": "debate"}},
          "limits": {"max_consecutive_failures": 1, "max_debate_turns": 4, "max_phase_minutes": 60},
          "operation_cost_usd": {"debate_turn": 0.0, "remediation": 0.0}}
PHASE = SimpleNamespace(number=3, max_remediation_attempts=2)


def proposal(disposition="upheld"):
    return {"status": "agreement", "decision": "Keep the adapter boundary.",
            "finding_dispositions": [{"finding_id": "f1", "disposition": disposition}],
            "required_changes": ["Move the call behind the adapter."], "human_decisions": [], "revisit_when": []}


def turn(argument="My case.", offer=None, accept=None):
    return {"argument": argument, "proposal": offer, "accept": accept}


class ScriptedSides:
    """Stands in for the role runner: each call returns the next scripted turn."""

    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    def run(self, role, prompt, output, timeout, debate=False):
        self.calls.append({"role": role, "debate": debate, "prompt": prompt})
        return self.turns.pop(0)


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.supervisor = Supervisor.__new__(Supervisor)
        self.supervisor.store = StateStore(self.root / "control")
        self.supervisor.control_root = self.root / "control"
        self.supervisor.root = self.root
        self.supervisor.config = copy.deepcopy(CONFIG)
        self.supervisor.budget = BudgetLedger(self.root / "control", 10)
        self.result_path = self.root / "control/artifacts/phase-3/DEBATE_RESULT-1.json"
        self.result_path.parent.mkdir(parents=True)

    def state(self) -> BuildState:
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state = BuildState(status="running", current_phase=3, phase_attempts={"3": 1}, deadline_at=deadline,
                           base_sha="base", governance_sha="gov",
                           findings={"key-1": dict(FINDING)}, debate_attempts={"3": 1})
        self.supervisor.store.save(state)
        return state

    def debate(self, state, sides):
        return self.supervisor._run_debate(state, PHASE, sides, [dict(FINDING)], EVIDENCE, 1, self.result_path)

    def stall(self, state):
        """Four turns, no acceptance: the build waits for the operator."""
        with self.assertRaises(HumanEscalation):
            self.debate(state, ScriptedSides([turn() for _ in range(4)]))


class TurnTakingTests(Harness):
    def test_a_debate_closes_when_one_side_accepts_the_others_proposal(self):
        state = self.state()
        offer = proposal()
        sides = ScriptedSides([turn(offer=offer), turn(accept=proposal_digest(offer))])
        result = self.debate(state, sides)
        self.assertEqual(result, offer)
        self.assertEqual(json.loads(self.result_path.read_text()), offer)
        self.assertEqual([call["role"] for call in sides.calls], ["implementer", "reviewer"])
        self.assertTrue(all(call["debate"] for call in sides.calls), "every turn runs read-only")
        events = [item for item in state.history if item["event"] == "debate_outcome"]
        self.assertEqual(events[-1]["outcome"], "findings_upheld")
        self.assertEqual(events[-1]["decided_by"], "agents")

    def test_each_turn_sees_the_transcript_before_it(self):
        state = self.state()
        offer = proposal()
        sides = ScriptedSides([turn("Codex opens.", offer=offer), turn(accept=proposal_digest(offer))])
        self.debate(state, sides)
        self.assertNotIn("Codex opens.", sides.calls[0]["prompt"])
        self.assertIn("Codex opens.", sides.calls[1]["prompt"])
        self.assertIn(proposal_digest(offer), sides.calls[1]["prompt"])

    def test_each_turn_is_its_own_paid_operation(self):
        state = self.state()
        offer = proposal()
        self.debate(state, ScriptedSides([turn(offer=offer), turn(accept=proposal_digest(offer))]))
        self.assertEqual(sorted(key for key in state.operations if "debate-1-turn" in key),
                         ["phase-3:debate-1-turn-1:1", "phase-3:debate-1-turn-2:1"])
        ledger = [json.loads(line) for line in self.supervisor.budget.path.read_text().splitlines()]
        self.assertEqual([item["provider"] for item in ledger], ["debate_turn", "debate_turn"])

    def test_turns_run_out_and_the_build_waits_without_counting_a_failure(self):
        state = self.state()
        self.stall(state)
        self.assertEqual(state.escalation_code, "DEBATE_AWAITING_DECISION")
        self.assertEqual(state.consecutive_failures, 0)  # max_consecutive_failures is 1
        self.assertEqual(explain(state.escalation_code)["aspect"], "caution")
        pending = state.pending_debate
        self.assertEqual((pending.phase, pending.debate, pending.finding_ids), (3, 1, ["f1"]))
        self.assertIsNone(pending.answer)
        self.assertTrue(levers.is_awaiting_decision(state))

    def test_a_restart_replays_finished_turns_instead_of_paying_again(self):
        state = self.state()
        self.stall(state)
        again = ScriptedSides([])
        with self.assertRaises(HumanEscalation):
            self.debate(state, again)
        self.assertEqual(again.calls, [], "completed turns come from their outputs")

    def test_an_interrupted_debate_is_resumed_not_counted_as_spent(self):
        state = self.state()
        paths = self.supervisor._paths(PHASE)
        self.assertIsNone(self.supervisor._unfinished_debate(state, PHASE, paths), "no turns yet")
        state.operations["phase-3:debate-1-turn-1:1"] = SimpleNamespace(status="completed")
        self.assertEqual(self.supervisor._unfinished_debate(state, PHASE, paths), 1)
        paths["debate"].with_name("DEBATE_DECISION-1.json").write_text("{}")
        self.assertIsNone(self.supervisor._unfinished_debate(state, PHASE, paths), "the operator settled it")

    def test_a_debate_waiting_for_the_operator_is_not_resumed(self):
        state = self.state()
        self.stall(state)
        self.assertIsNone(self.supervisor._unfinished_debate(state, PHASE, self.supervisor._paths(PHASE)))

    def test_codex_opens_and_the_sides_alternate(self):
        self.assertEqual([side_for_turn(n) for n in range(1, 5)],
                         ["implementer", "reviewer", "implementer", "reviewer"])


class TurnContractTests(unittest.TestCase):
    def test_accepting_a_proposal_nobody_made_is_refused(self):
        with self.assertRaisesRegex(AgentError, "DEBATE_TURN_INVALID"):
            validate_turn(turn(accept="0" * 64), [FINDING], turn())

    def test_accepting_anything_but_the_last_proposal_is_refused(self):
        with self.assertRaisesRegex(AgentError, "DEBATE_TURN_INVALID"):
            validate_turn(turn(accept=proposal_digest(proposal("dropped"))), [FINDING], turn(offer=proposal()))

    def test_a_proposal_must_dispose_of_exactly_the_debated_findings(self):
        partial = proposal(); partial["finding_dispositions"] = []
        with self.assertRaisesRegex(AgentError, "DEBATE_TURN_INVALID"):
            validate_turn(turn(offer=partial), [FINDING], None)

    def test_a_proposal_is_an_agreement(self):
        offer = proposal(); offer["status"] = "agree_to_disagree"
        with self.assertRaisesRegex(AgentError, "DEBATE_TURN_INVALID"):
            validate_turn(turn(offer=offer), [FINDING], None)

    def test_a_turn_needs_an_argument(self):
        with self.assertRaisesRegex(AgentError, "DEBATE_TURN_INVALID"):
            validate_turn(turn(argument=" "), [FINDING], None)

    def test_outcome_says_whether_the_debate_changed_the_ask(self):
        self.assertEqual(outcome(proposal("upheld"), "agents"), "findings_upheld")
        self.assertEqual(outcome(proposal("dropped"), "agents"), "findings_dropped")
        self.assertEqual(outcome(proposal("changed"), "agents"), "findings_changed")
        self.assertEqual(outcome(proposal("upheld"), "operator"), "operator_decided")


class OperatorDecisionTests(Harness):
    def waiting(self):
        state = self.state()
        self.stall(state)
        return self.supervisor.store.load()

    def test_a_wrong_nonce_is_refused(self):
        self.waiting()
        with self.assertRaisesRegex(RuntimeError, "nonce"):
            self.supervisor.decide_debate("code-stands", "", "wrong")

    def test_your_own_ruling_needs_its_text(self):
        state = self.waiting()
        with self.assertRaisesRegex(ValueError, "text"):
            self.supervisor.decide_debate("own", "  ", state.pending_debate.nonce)

    def test_a_decision_resumes_the_same_attempt_with_a_fresh_deadline(self):
        state = self.waiting()
        self.supervisor.config["limits"]["max_phase_minutes"] = 600
        self.supervisor.decide_debate("code-stands", "Layering is fine here.", state.pending_debate.nonce)
        after = self.supervisor.store.load()
        self.assertEqual((after.status, after.escalation_code, after.current_phase), ("idle", None, 3))
        self.assertEqual(after.phase_attempts, {"3": 1})
        self.assertEqual(after.pending_debate.answer, "code-stands")
        self.assertGreater(datetime.fromisoformat(after.deadline_at), datetime.fromisoformat(state.deadline_at)
                           + timedelta(hours=8), "the wait for the operator is not charged to the phase")

    def test_a_decision_against_a_build_that_has_since_moved_is_refused(self):
        state = self.waiting()
        state.base_sha = "moved"
        self.supervisor.store.save(state)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.supervisor.decide_debate("code-stands", "", state.pending_debate.nonce)

    def test_retry_discards_the_debate_with_the_attempt(self):
        self.waiting()
        self.supervisor.git = SimpleNamespace()
        self.supervisor.retry()
        self.assertIsNone(self.supervisor.store.load().pending_debate)

    def consume(self, answer, text=""):
        state = self.waiting()
        self.supervisor.decide_debate(answer, text, state.pending_debate.nonce)
        state = self.supervisor.store.load()
        workspace = self.root / "workspace"; workspace.mkdir()
        implemented = []

        class Codex:
            def implement(self, phase, output, timeout, review=None, adr=None):
                implemented.append(adr)
                return {"status": "complete"}

        paths = self.supervisor._paths(PHASE)
        result = self.supervisor._consume_debate_decision(state, PHASE, Codex(), workspace, paths, {"status": "before"})
        return state, result, implemented, paths

    def test_the_code_stands_closes_the_findings_without_new_work(self):
        state, result, implemented, paths = self.consume("code-stands")
        self.assertEqual(result, {"status": "before"})
        self.assertEqual(implemented, [])
        self.assertEqual(state.findings["key-1"]["status"], OPERATOR_ACCEPTED_STATUS)
        self.assertIsNone(state.pending_debate)
        self.assertEqual(json.loads(paths["debate"].with_name("DEBATE_DECISION-1.json").read_text())["answer"],
                         "code-stands")
        self.assertEqual([e["outcome"] for e in state.history if e["event"] == "debate_outcome"][-1],
                         "operator_decided")

    def test_the_finding_stands_becomes_an_operator_adr_that_codex_implements(self):
        state, result, implemented, paths = self.consume("finding-stands", "Do it before phase 4.")
        self.assertEqual(result, {"status": "complete"})
        adr = paths["adr_proposal"].read_text()
        self.assertIn("Decided by: operator", adr)
        self.assertIn("Move the call behind the adapter.", adr)
        self.assertIn("Do it before phase 4.", adr)
        self.assertIn("Operator-authored", adr)
        self.assertEqual(implemented, [adr])
        self.assertEqual(state.remediation_attempts, {"3": 1})
        self.assertIsNone(state.pending_debate)

    def test_the_lever_names_the_debate_and_the_kernel_command(self):
        state = self.waiting()
        facts = {"trust_ok": True, "isolation_fresh": True, "bootstrapped": True, "lock_held": False,
                 "budget_ok": True, "remaining_phases": [3]}
        decide = next(item for item in levers.levers(state, facts) if item["action"] == "decide")
        self.assertTrue(decide["enabled"])
        self.assertEqual(decide["argv"], ["debate", "decide", "--nonce", state.pending_debate.nonce])
        self.assertEqual(decide["number"], 12)


class RulingTests(unittest.TestCase):
    def test_an_agent_agreement_and_an_operator_ruling_are_labelled_apart(self):
        metadata = {"title": "t", "debate_id": "d", "governance_sha": "g", "base_sha": "b",
                    "evidence_digest": "e", "finding_ids": ["f1"]}
        with tempfile.TemporaryDirectory() as directory:
            agents, operator = Path(directory) / "a.md", Path(directory) / "o.md"
            write_adr(agents, metadata, proposal())
            write_adr(operator, {**metadata, "decided_by": "operator"}, operator_result("code-stands", "", [FINDING]))
            self.assertIn("Decided by: agents", agents.read_text())
            self.assertIn("Decided by: operator", operator.read_text())

    def test_a_restated_claim_stays_ruled_on(self):
        register = {"old": dict(FINDING, status=OPERATOR_ACCEPTED_STATUS)}
        restated = dict(FINDING, evidence_digest="e2", id="f2")
        accumulate_findings(register, {"evidence_digest": "e2", "findings": [restated]}, "p")
        self.assertTrue(all(item["status"] == OPERATOR_ACCEPTED_STATUS for item in register.values()))
        self.assertEqual(operator_rulings(register), [{"path": "src/x.py", "claim_key": "layering"}])

    def test_debate_turns_are_not_counted_against_the_change(self):
        def record(operation, edited):
            event = {"type": "item.completed", "item": {"id": "1", "type": "file_change", "status": "completed",
                                                        "changes": [{"path": f"/workspace/{edited}", "kind": "update"}]}}
            return {"header": {"role": "implementer", "operation": operation, "command": ["codex"],
                               "workspace": "/workspace"}, "footer": {},
                    "rows": [{"stream": "stdout", "text": json.dumps(event), "seq": 1, "at": "t"}]}
        records = [record("phase-3:implement:1", "src/x.py"), record("phase-3:debate-1-turn-1:1", "src/y.py")]
        self.assertEqual(discrepancies(records, ["src/x.py"]), [])
        self.assertEqual(discrepancies([records[0], dict(records[1], header={**records[1]["header"],
                                                                            "operation": "phase-3:implement:2"})],
                                       ["src/x.py"]),
                         [{"flag": "reported_no_net_change", "path": "src/y.py"}],
                         "the same record outside a debate is still checked")


class ReadOnlyTurnTests(unittest.TestCase):
    def test_a_codex_turn_runs_against_a_read_only_workspace_and_may_not_change_it(self):
        seen = []

        class Recording(FakeIsolationBackend):
            def command(self, argv, context):
                seen.append(context.read_only_workspace)
                return argv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"; workspace.mkdir()
            config = {"roles": {"implementer": {"command": ["codex"], "auth_mode": "none", "network": False,
                                                "credential_env": []}}}

            def runner(command, **kwargs):
                (workspace / "edited.py").write_text("x")
                return SimpleNamespace(returncode=0, stdout=json.dumps(turn()), stderr="")

            role_runner = RoleRunner(config, Recording(), IsolationContext("implementer", workspace, root / "c", root),
                                     runner)
            executable = root / "codex"; executable.write_text(""); executable.chmod(0o755)
            config["roles"]["implementer"]["command"] = [str(executable)]
            with self.assertRaisesRegex(AgentError, "IMPLEMENTER_MUTATED_WORKSPACE"):
                role_runner.run("implementer", "p", root / "c/out.json", 5, debate=True)
            self.assertEqual(seen, [True])


class ConfigurationTests(unittest.TestCase):
    def validate(self, config):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts/phases").mkdir(parents=True); (root / "prompts/phases/p.md").write_text("p")
            validate_config(config, root)

    def debate_config(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["roles"]["arbiter"] = {"mode": "debate"}
        config["operation_cost_usd"]["debate_turn"] = 2
        return config

    def test_a_debate_arbiter_needs_no_command_of_its_own(self):
        self.validate(self.debate_config())

    def test_a_debate_build_must_price_a_turn(self):
        config = self.debate_config(); del config["operation_cost_usd"]["debate_turn"]
        with self.assertRaisesRegex(ConfigurationError, "operation_cost_usd"):
            self.validate(config)

    def test_other_builds_need_no_turn_price(self):
        self.validate(copy.deepcopy(BASE_CONFIG))

    def test_a_debate_needs_at_least_two_turns(self):
        config = self.debate_config(); config["limits"]["max_debate_turns"] = 1
        with self.assertRaisesRegex(ConfigurationError, "max_debate_turns"):
            self.validate(config)


if __name__ == "__main__":
    unittest.main()
