"""The documentation role: a documenter keeps docs true to the code inside the governed loop.

Specified in docs/planning/docs-and-drafts.md, Part 1. ReplCRM finished 20 reviewed phases and
still needed a day of documentation cleanup before launch: six dead links, a stale "Node 20+"
claim, a changelog two releases behind. Nobody was asked whether the story was still true.
"""
import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor import documentation
from buildctl.supervisor.agents import AgentError
from buildctl.supervisor.budget import BudgetLedger
from buildctl.supervisor.claude import ClaudeAdapter
from buildctl.supervisor.gates import GateResult
from buildctl.supervisor.git import GitManager, path_allowed
from buildctl.supervisor.phases import ConfigurationError, load_config_text
from buildctl.supervisor.state import BuildState, StateStore
from tests.helpers import git

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "target"
DOCS = {"mode": "required", "paths": ["README.md", "CHANGELOG.md", "docs/**"], "changelog": "CHANGELOG.md",
        "unreleased_heading": "Unreleased", "forbidden_phrases": ["production-ready", "seamless"]}
UPDATED = {"status": "updated", "summary": "documented --dry-run", "user_visible": True, "reason": "",
           "changes": [{"path": "README.md", "reflects": "src/app.py:1", "kind": "updated"}]}
NO_CHANGE = {"status": "no_change", "summary": "internal", "user_visible": False, "changes": [],
             "reason": "internal refactor, no user-visible change"}


def fixture_config() -> dict:
    return json.loads((FIXTURE / "build.yaml").read_text())


def load(config: dict) -> dict:
    return load_config_text(json.dumps(config), FIXTURE)


def with_docs(config: dict, **section) -> dict:
    config = copy.deepcopy(config)
    config["documentation"] = {**DOCS, **section}
    config["operation_cost_usd"].update({"documentation": 1.0, "documentation_remediation": 1.0})
    return config


class ConfigurationTests(unittest.TestCase):
    def test_a_build_without_the_section_is_unchanged(self):
        config = load(fixture_config())
        self.assertFalse(documentation.enabled(config))
        self.assertNotIn("documenter", config["roles"])

    def test_on_without_a_documenter_runs_as_the_implementers_family(self):
        config = load(with_docs(fixture_config()))
        documenter, implementer = config["roles"]["documenter"], config["roles"]["implementer"]
        self.assertEqual(documenter["command"], implementer["command"])
        self.assertEqual(documenter["model_family"], "codex")
        self.assertEqual(documenter["prompt_lineage"], documentation.DERIVED_LINEAGE)
        self.assertNotIn("remediation_prompt", documenter)
        self.assertEqual(documentation.warnings(config), [])

    def test_the_documentation_costs_are_required_only_when_on(self):
        config = with_docs(fixture_config())
        del config["operation_cost_usd"]["documentation_remediation"]
        with self.assertRaisesRegex(ConfigurationError, "operation_cost_usd"):
            load(config)

    def test_a_documenter_with_documentation_off_is_refused(self):
        config = fixture_config()
        config["roles"]["documenter"] = copy.deepcopy(config["roles"]["implementer"])
        with self.assertRaisesRegex(ConfigurationError, "documentation.mode is off"):
            load(config)

    def test_documentation_paths_never_reach_governed_instructions(self):
        for paths in (["prompts/**"], ["build.yaml"], ["*"], ["templates/"]):
            with self.subTest(paths=paths), self.assertRaisesRegex(ConfigurationError, "governed instructions"):
                load(with_docs(fixture_config(), paths=paths, changelog=None))

    def test_governance_documents_inside_documentation_paths_stay_read_only(self):
        # The fixture keeps docs/ROADMAP.md and four other governance documents under docs/.
        config = load(with_docs(fixture_config()))
        self.assertTrue(documentation.read_only(config, "docs/ROADMAP.md"))
        self.assertFalse(documentation.read_only(config, "docs/install.md"))
        before, after = {"docs/ROADMAP.md": "a"}, {"docs/ROADMAP.md": "b"}
        self.assertEqual(documentation.scope_violations(before, after, config), ["docs/ROADMAP.md"])

    def test_unknown_mode_and_keys_are_refused(self):
        with self.assertRaisesRegex(ConfigurationError, "documentation.mode"):
            load(with_docs(fixture_config(), mode="sometimes"))
        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            load(with_docs(fixture_config(), voice="bold"))

    def test_the_reviewers_family_documenting_is_a_warning(self):
        config = with_docs(fixture_config(), paths=["README.md", "CHANGELOG.md"])
        config["roles"]["documenter"] = dict(copy.deepcopy(config["roles"]["implementer"]), model_family="claude")
        loaded = load(config)
        self.assertIn("grade its own family", documentation.warnings(loaded)[0])

    def test_a_phase_can_switch_documentation_off(self):
        config = with_docs(fixture_config(), paths=["README.md", "CHANGELOG.md"])
        config["phases"][0]["documentation"] = "no"
        with self.assertRaisesRegex(ConfigurationError, "true or false"):
            load(config)


class MatchingTests(unittest.TestCase):
    def test_documentation_globs(self):
        globs = ("README.md", "docs/**", "guides/")
        for path in ("README.md", "docs/a.md", "docs/deep/b.md", "guides/x.md"):
            self.assertTrue(documentation.matches(path, globs), path)
        for path in ("src/README.md", "docs.md", "READMEXmd"):
            self.assertFalse(documentation.matches(path, globs), path)

    def test_allowed_paths_accept_globs_and_keep_prefix_rules(self):
        self.assertTrue(path_allowed("docs/x/y.md", ("docs/**",)))
        self.assertTrue(path_allowed("src/a.py", ("src/",)))
        self.assertFalse(path_allowed("src2/a.py", ("src/",)))
        self.assertFalse(path_allowed("srca.py", ("src",)))


class ResultContractTests(unittest.TestCase):
    def test_valid_results(self):
        documentation.validate_result(copy.deepcopy(UPDATED))
        documentation.validate_result(copy.deepcopy(NO_CHANGE))

    def test_invalid_results_are_refused_with_a_reason(self):
        cases = [dict(UPDATED, extra=1), dict(UPDATED, status="done"), dict(UPDATED, changes=[]),
                 dict(NO_CHANGE, reason=" "), dict(UPDATED, user_visible="yes"),
                 dict(UPDATED, changes=[{"path": "README.md", "reflects": "x", "kind": "rewrote"}])]
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(AgentError, "DOCUMENTER_OUTPUT_SCHEMA_INVALID"):
                documentation.validate_result(copy.deepcopy(value))


class GateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs/guide.md").write_text("# Guide\n\n## Install it\n")
        (self.workspace / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n- Adds --dry-run.\n\n## 0.1.0\n\n- First.\n")
        self.config = {"documentation": DOCS}
        self.base = {"CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n## 0.1.0\n\n- First.\n"}

    def gates(self, result=UPDATED, changed=("CHANGELOG.md",)):
        return documentation.run_gates(self.workspace, self.config, result, list(changed), self.base.get)

    def test_clean_docs_pass(self):
        (self.workspace / "README.md").write_text("See [the guide](docs/guide.md#install-it).\n```\n[x](gone.md)\n```\n")
        self.assertEqual(self.gates(), {"passed": True, "failures": []})

    def test_a_dead_link_or_heading_fails(self):
        (self.workspace / "README.md").write_text("[a](docs/missing.md) [b](docs/guide.md#nowhere) [c](https://x.test/y)\n")
        failures = self.gates()["failures"]
        self.assertEqual(failures[0]["gate"], "links")
        self.assertEqual(len(failures[0]["problems"]), 2)

    def test_a_user_visible_change_needs_a_new_changelog_entry(self):
        failures = self.gates(changed=())["failures"]
        self.assertEqual([item["gate"] for item in failures], ["changelog"])
        self.base["CHANGELOG.md"] = (self.workspace / "CHANGELOG.md").read_text()
        self.assertEqual(self.gates()["failures"][0]["gate"], "changelog", "an old entry does not count")
        self.assertTrue(self.gates(result=NO_CHANGE, changed=())["passed"], "no user-visible change, no entry")

    def test_a_forbidden_phrase_fails_only_when_gained(self):
        (self.workspace / "README.md").write_text("A seamless, Production-Ready tool.\n")
        failures = self.gates(changed=("CHANGELOG.md", "README.md"))["failures"]
        self.assertEqual(failures[0]["gate"], "forbidden_phrases")
        self.assertEqual(len(failures[0]["problems"]), 2)
        self.base["README.md"] = "A seamless, production-ready tool.\n"
        self.assertTrue(self.gates(changed=("CHANGELOG.md", "README.md"))["passed"], "already there is not gained")

    def test_binary_files_under_documentation_paths_are_left_alone(self):
        (self.workspace / "docs/shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe production-ready")
        self.assertTrue(self.gates(changed=("CHANGELOG.md", "docs/shot.png"))["passed"])

    def test_scope_violations(self):
        before = {"README.md": "a", "src/app.py": "b", "docs/x.md": "c"}
        after = {"README.md": "a2", "src/app.py": "b2", "prompts/p.md": "d"}
        self.assertEqual(documentation.scope_violations(before, after, self.config), ["prompts/p.md", "src/app.py"])


class FakeDocumenter:
    """Edits the workspace the way a documenter would, from a script of passes."""

    def __init__(self, workspace: Path, passes):
        self.workspace, self.passes, self.calls = workspace, list(passes), []

    def document(self, phase, context, output, timeout, feedback=None):
        self.calls.append({"feedback": feedback, "changed": [item["path"] for item in context["changed_files"]]})
        edits, result = self.passes.pop(0)
        for path, text in edits.items():
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return documentation.validate_result(copy.deepcopy(result))


class StubGates:
    def __init__(self, *outcomes):
        self.outcomes, self.runs = list(outcomes) or [True], 0

    def run(self, phase, timeout):
        self.runs += 1
        passed = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return GateResult(passed, [] if passed else [{"command": "test", "returncode": 1}])


class LoopHarness(unittest.TestCase):
    """The real `_document` against a real Git repository and workspace, with agents stubbed."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.root, self.workspace, control = base / "repo", base / "workspace", base / "control"
        self.root.mkdir(); control.mkdir()
        git(self.root, "init", "-q"); git(self.root, "config", "user.email", "t@test.invalid")
        git(self.root, "config", "user.name", "T")
        (self.root / "src").mkdir(); (self.root / "src/app.py").write_text("print('v1')\n")
        (self.root / "README.md").write_text("# App\n")
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n")
        git(self.root, "add", "."); git(self.root, "commit", "-qm", "base")
        shutil.copytree(self.root, self.workspace, ignore=shutil.ignore_patterns(".git"))
        self.config = {"documentation": dict(DOCS), "limits": {"max_consecutive_failures": 9, "max_daily_cost_usd": 100},
                       "operation_cost_usd": {"documentation": 1.0, "documentation_remediation": 1.0},
                       "roles": {"documenter": {"auth_mode": "none", "network": False}}, "git": {}}
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.root, supervisor.control_root, supervisor.config = self.root, control, self.config
        supervisor.store, supervisor.git = StateStore(control), GitManager(self.root, self.config)
        supervisor.budget, supervisor.evidence = BudgetLedger(control, 100), None
        self.supervisor = supervisor
        self.baseline = supervisor.git.manifest(self.workspace)
        (self.workspace / "src/app.py").write_text("print('v2 with --dry-run')\n")  # the implementation
        self.state = BuildState(current_phase=1, phase_attempts={"1": 1}, base_sha=git(self.root, "rev-parse", "HEAD"),
                                workspace=str(self.workspace),
                                deadline_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        supervisor.store.save(self.state)
        self.phase = SimpleNamespace(number=1, prompt=self.root / "README.md", allowed_paths=("src/",),
                                     documentation=True)
        self.paths = supervisor._paths(self.phase)

    def document(self, documenter, gates=None, round_id=1, feedback=None):
        return self.supervisor._document(self.state, self.phase, self.workspace, self.baseline, self.paths,
                                         gates or StubGates(), documenter, {"status": "complete"},
                                         {"passed": True, "details": []}, round_id, feedback)

    def escalation(self, documenter, gates=None) -> str:
        with self.assertRaises(HumanEscalation) as raised:
            self.document(documenter, gates)
        return str(raised.exception)


GOOD_DOCS = {"README.md": "# App\n\nRun with `--dry-run`.\n",
             "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- Adds --dry-run.\n"}


class DocumentStepTests(LoopHarness):
    def test_docs_that_pass_go_to_review_with_the_documenters_account(self):
        documenter = FakeDocumenter(self.workspace, [(GOOD_DOCS, UPDATED)])
        view = self.document(documenter)
        self.assertEqual(view["mode"], "required")
        self.assertTrue(view["gates"]["passed"])
        self.assertEqual(view["result"]["status"], "updated")
        self.assertEqual(documenter.calls[0]["changed"], ["src/app.py"], "the documenter sees the implementation delta")
        ledger = [entry["provider"] for entry in self.supervisor.budget._entries()]
        self.assertEqual(ledger, ["documentation"])

    def test_failed_gates_send_the_documenter_back_with_what_failed(self):
        documenter = FakeDocumenter(self.workspace, [({"README.md": "[x](docs/nope.md)\n"}, UPDATED),
                                                     (GOOD_DOCS, UPDATED)])
        view = self.document(documenter)
        self.assertTrue(view["gates"]["passed"])
        self.assertIsNone(documenter.calls[0]["feedback"])
        feedback = json.loads(documenter.calls[1]["feedback"])
        self.assertEqual([item["gate"] for item in feedback["failed_gates"]], ["links", "changelog"])
        self.assertEqual(self.state.documentation_attempts, {"1": 1})
        self.assertEqual([entry["provider"] for entry in self.supervisor.budget._entries()],
                         ["documentation", "documentation_remediation"])

    def test_docs_that_break_the_acceptance_commands_go_back_too(self):
        documenter = FakeDocumenter(self.workspace, [(GOOD_DOCS, UPDATED), (GOOD_DOCS, UPDATED)])
        self.document(documenter, StubGates(False, True))
        self.assertTrue(json.loads(documenter.calls[1]["feedback"])["acceptance_failures"])

    def test_the_ceiling_stops_the_phase(self):
        self.config["documentation"]["max_attempts"] = 1
        bad = ({"README.md": "[x](docs/nope.md)\n"}, UPDATED)
        self.assertEqual(self.escalation(FakeDocumenter(self.workspace, [bad, bad])), "DOCUMENTATION_ATTEMPTS_EXHAUSTED")
        saved = self.supervisor.store.load()
        self.assertEqual(saved.extra_attempt_grant.ceiling_code, "DOCUMENTATION_ATTEMPTS_EXHAUSTED")

    def test_optional_mode_records_gate_failures_without_stopping(self):
        self.config["documentation"]["mode"] = "optional"
        view = self.document(FakeDocumenter(self.workspace, [({"README.md": "[x](docs/nope.md)\n"}, UPDATED)]))
        self.assertFalse(view["gates"]["passed"])
        events = [entry["event"] for entry in self.supervisor.store.load().history]
        self.assertIn("documentation_gates_failed", events)

    def test_optional_mode_still_stops_on_broken_acceptance(self):
        self.config["documentation"]["mode"] = "optional"
        self.config["documentation"]["max_attempts"] = 1
        documenter = FakeDocumenter(self.workspace, [(GOOD_DOCS, UPDATED), (GOOD_DOCS, UPDATED)])
        self.assertEqual(self.escalation(documenter, StubGates(False)), "DOCUMENTATION_ATTEMPTS_EXHAUSTED")

    def test_touching_code_is_a_hard_stop(self):
        documenter = FakeDocumenter(self.workspace, [({"src/app.py": "print('docs did this')\n", **GOOD_DOCS}, UPDATED)])
        self.assertEqual(self.escalation(documenter), "DOC_SCOPE_VIOLATION")
        self.assertIn("src/app.py", self.supervisor.store.load().escalation_detail)

    def test_touching_governance_is_a_hard_stop(self):
        documenter = FakeDocumenter(self.workspace, [({"prompts/phase.md": "rewritten\n"}, NO_CHANGE)])
        self.assertEqual(self.escalation(documenter), "DOC_SCOPE_VIOLATION")

    def test_nothing_to_document_is_a_valid_result(self):
        view = self.document(FakeDocumenter(self.workspace, [({}, NO_CHANGE)]))
        self.assertTrue(view["gates"]["passed"])

    def test_a_replayed_lap_pays_for_nothing_twice(self):
        documenter = FakeDocumenter(self.workspace, [(GOOD_DOCS, UPDATED)])
        first = self.document(documenter)
        gates = StubGates()
        second = self.document(documenter, gates)
        self.assertEqual(first, second)
        self.assertEqual(len(documenter.calls), 1)
        self.assertEqual(gates.runs, 0, "the documentation gates replay from their recorded result")
        self.assertEqual(len(self.supervisor.budget._entries()), 1)

    def test_a_new_code_lap_documents_again(self):
        documenter = FakeDocumenter(self.workspace, [(GOOD_DOCS, UPDATED), ({}, NO_CHANGE)])
        self.document(documenter, round_id=1)
        self.document(documenter, round_id=2)
        self.assertEqual(len(documenter.calls), 2)

    def test_a_phase_switched_off_is_reported_as_skipped(self):
        self.phase.documentation = False
        documenter = FakeDocumenter(self.workspace, [])
        self.assertEqual(self.document(documenter), {"skipped": True})
        self.assertEqual(documenter.calls, [])

    def test_documentation_paths_join_the_phase_allowed_paths(self):
        allowed = self.supervisor._allowed_paths(self.phase, self.workspace, self.paths)
        self.assertEqual(allowed, ("src/", "README.md", "CHANGELOG.md", "docs/**"))
        self.phase.documentation = False
        self.assertEqual(self.supervisor._allowed_paths(self.phase, self.workspace, self.paths), ("src/",))


class ReviewRoutingTests(LoopHarness):
    def finding(self, path, severity="HIGH", status="open"):
        return {"path": path, "severity": severity, "status": status}

    def test_only_documentation_findings_go_to_the_documenter(self):
        only = self.supervisor._documentation_findings_only
        self.assertTrue(only([self.finding("README.md"), self.finding("src/app.py", "MEDIUM")]))
        self.assertFalse(only([self.finding("README.md"), self.finding("src/app.py")]))
        self.assertFalse(only([self.finding("README.md", "LOW")]), "nothing blocking, nothing to route")
        self.assertTrue(only([self.finding("docs/a.md"), self.finding("src/app.py", status="resolved")]))

    def test_a_stale_doc_the_phase_did_not_touch_is_cited_on_the_code_with_kind_documentation(self):
        stale = dict(self.finding("src/app.py"), kind="documentation")
        self.assertTrue(self.supervisor._documentation_findings_only([stale]))
        self.assertEqual(self.supervisor._documentation_findings([stale, self.finding("src/app.py")]), [stale])


class ReviewPromptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "prompts").mkdir()
        (self.root / "prompts/p.md").write_text("phase")
        (self.root / "prompts/review.md").write_text("Protocol version: `buildctl-review-v2`\nPROJECT POLICY")
        self.config = {"roles": {"reviewer": {"protocol_version": "buildctl-review-v2", "context_documents": [],
                                              "review_prompt": "prompts/review.md"}}}

    def prompt(self, documentation_view):
        evidence = {"delta_id": "d", "digest": "d", "coverage": [], "diff": "", "changed_files": [], "gates": {},
                    "tests": ""}
        if documentation_view is not None:
            evidence["documentation"] = documentation_view
        runner = SimpleNamespace(config=self.config)
        return ClaudeAdapter(runner, self.root).build_prompt(SimpleNamespace(prompt=self.root / "prompts/p.md"),
                                                             evidence, {})

    def test_the_criteria_arrive_even_under_a_repository_review_prompt(self):
        text = self.prompt({"mode": "required", "result": UPDATED, "gates": {"passed": True}})
        self.assertIn("PROJECT POLICY", text)
        self.assertIn(documentation.REVIEW_CRITERIA, text)
        self.assertIn('label="doc_result"', text)

    def test_the_reviewer_sees_the_operators_rules_as_trusted(self):
        # Found in the 2026-11-26 trial: a documenter that obeyed a forbidden phrase was taken for one
        # that skipped the work, because the reviewer had no way to see the rule.
        self.config["documentation"] = {"mode": "required", "paths": ["README.md", "docs/**"],
                                        "forbidden_phrases": ["--exclude", " "], "changelog": "CHANGELOG.md"}
        text = self.prompt({"mode": "required", "result": UPDATED, "gates": {"passed": True}})
        binding = text.split("TRUSTED DELTA BINDING\n", 1)[1].split("\n", 1)[0]
        rules = json.loads(binding)["documentation_rules"]
        self.assertEqual(rules["forbidden_phrases"], ["--exclude"])
        self.assertEqual((rules["changelog"], rules["mode"]), ("CHANGELOG.md", "required"))
        self.assertIn("`treatment` set to `human`", text)

    def test_a_skipped_phase_has_no_rules_in_the_binding(self):
        self.config["documentation"] = {"mode": "required", "paths": ["docs/**"]}
        self.assertNotIn("documentation_rules", self.prompt({"skipped": True}))

    def test_a_skipped_phase_tells_the_reviewer_why(self):
        text = self.prompt({"skipped": True})
        self.assertIn(documentation.SKIPPED_NOTE, text)
        self.assertNotIn("DOCUMENTATION CRITERIA", text)

    def test_documentation_off_changes_nothing(self):
        self.assertNotIn("DOCUMENTATION", self.prompt(None))


if __name__ == "__main__":
    unittest.main()


class StopPhase(RuntimeError):
    pass


class RunPhaseTests(LoopHarness):
    """The real `_run_phase`: a review that blocks only on docs re-runs the documenter, not Codex."""

    def test_a_docs_only_remediation_goes_to_the_documenter_then_advances(self):
        from unittest import mock
        from buildctl.supervisor.review import Transition

        supervisor, state = self.supervisor, self.state
        self.config.update({"limits": {"max_consecutive_failures": 9, "max_daily_cost_usd": 100,
                                       "max_semantic_review_attempts": 9, "max_debate_rounds": 1,
                                       "max_phase_attempts": 3, "max_review_format_attempts": 3},
                            "checkpoints": {"before_phases": [], "mvp_completion": 99}})
        self.config["operation_cost_usd"].update({"implementation": 1.0, "review": 1.0, "remediation": 1.0})
        for role in ("implementer", "reviewer", "arbiter"):
            self.config["roles"][role] = {"auth_mode": "none", "network": False, "vendor": "v", "model_family": role,
                                          "prompt_lineage": "l"}
        supervisor.backend, supervisor.snapshot, supervisor.runner = SimpleNamespace(name="fake"), self.root, None
        (supervisor.control_root / "phase-1-baseline.json").write_text(json.dumps(self.baseline))
        state.governance_sha = state.snapshot_digest = "gov"
        supervisor.store.save(state)
        phase = SimpleNamespace(number=1, prompt=self.root / "README.md", allowed_paths=("src/",), documentation=True,
                                merge_policy="auto", max_remediation_attempts=2, claude_review_required=True,
                                debate_on_architecture_disagreement=False, name="p1")
        documenter = FakeDocumenter(self.workspace, [({"README.md": "# App\n\nRun it as a production-ready tool.\n",
                                                       "CHANGELOG.md": GOOD_DOCS["CHANGELOG.md"]}, UPDATED),
                                                     (GOOD_DOCS, UPDATED),
                                                     ({"README.md": GOOD_DOCS["README.md"] + "\nSee CHANGELOG.md.\n"},
                                                      UPDATED)])
        implementations = []

        class Codex:
            def __init__(self, *args):
                pass

            def implement(self, phase, output, timeout, **kwargs):
                implementations.append(kwargs)
                return {"status": "complete", "summary": "s", "tests": [], "files_changed": [], "risks": [],
                        "architecture_questions": []}

        class Claude:
            def __init__(self, *args):
                pass

            def review(self, phase, evidence, phase_result, output, timeout):
                return {"verdict": "PASS", "reviewer": {}, "seen": sorted(evidence["documentation"])}

        stale = {"path": "README.md", "severity": "HIGH", "kind": "correctness", "status": "open", "claim_key": "c"}

        def accumulate(register, review, digest):
            register["f"] = dict(stale, status="open" if len(register) == 0 else "resolved")

        def committed(*args):
            raise StopPhase("reached the commit")

        supervisor._checkpoint = lambda state, phase: None
        supervisor._gate_operation = lambda *args, **kwargs: {"passed": True, "details": []}
        supervisor._advance_preconditions = lambda state, phase: (True, True)
        supervisor.git.apply_and_commit = committed
        reductions = [Transition("REMEDIATE", "R5", "high fixable finding"), Transition("ADVANCE", "R7", "clear")]
        with mock.patch("buildctl.buildctl.CodexAdapter", Codex), mock.patch("buildctl.buildctl.ClaudeAdapter", Claude), \
                mock.patch("buildctl.buildctl.DebateAdapter"), mock.patch("buildctl.buildctl.RoleRunner"), \
                mock.patch("buildctl.buildctl.IsolationContext"), \
                mock.patch("buildctl.buildctl.DocumenterAdapter", lambda *args: documenter), \
                mock.patch("buildctl.buildctl.MechanicalGates", lambda *args, **kwargs: StubGates()), \
                mock.patch("buildctl.buildctl.validate_review"), \
                mock.patch("buildctl.buildctl.accumulate_findings", accumulate), \
                mock.patch("buildctl.buildctl.reduce_review", side_effect=reductions):
            with self.assertRaises(StopPhase):
                supervisor._run_phase(state, phase)
        self.assertEqual(len(implementations), 1, "the implementer is not re-run for a docs-only finding")
        self.assertEqual(len(documenter.calls), 3, "first pass, a gate send-back, then the review send-back")
        self.assertIn("forbidden_phrases", documenter.calls[1]["feedback"])
        self.assertIn('"verdict": "PASS"', documenter.calls[2]["feedback"])
        self.assertEqual(state.documentation_attempts, {"1": 2})
        self.assertNotIn("1", state.remediation_attempts)
        events = [entry["event"] for entry in supervisor.store.load().history]
        self.assertEqual(events.count("documentation_remediation"), 2)
