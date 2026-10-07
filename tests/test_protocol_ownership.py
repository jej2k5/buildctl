"""Protocol material belongs to the kernel; repository material comes from config.

The supervisor used to read its output contracts and both prompts from fixed paths in the
governed repository, ignored the `review_prompt` and `remediation_prompt` keys that build.yaml
declared, and hardcoded one project's documents as reviewer context, so no other repository
could run a review at all.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.agents import RoleRunner
from buildctl.supervisor.claude import ClaudeAdapter
from buildctl.supervisor.codex import CodexAdapter
from buildctl.supervisor.debate import DebateAdapter
from buildctl.supervisor.git import GitManager, GitSafetyError
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from buildctl.supervisor.kernel import kernel_files
from buildctl.supervisor.phases import ConfigurationError, load_config, validate_config
from buildctl.supervisor.protocol import (PROTOCOL_ROOT, REVIEW_PROTOCOL_MARKER, ProtocolError, contract,
                                          prompt)
from buildctl.supervisor.trust import TrustStore
from tests.helpers import git, repository
from tests.test_kernel_trust import CONFIG


TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
EVIDENCE = {"delta_id": "d", "digest": "d", "diff": "", "coverage": [], "changed_files": [],
            "gates": {}, "tests": "", "governance_sha": "g", "base_sha": "b"}


def governed_tree(root: Path, documents=("docs/ARCH.md",)) -> Path:
    (root / "prompts/phases").mkdir(parents=True, exist_ok=True); (root / "prompts/phases/p.md").write_text("p")
    for path in documents:
        (root / path).parent.mkdir(parents=True, exist_ok=True); (root / path).write_text(f"CONTENT OF {path}")
    return root


def config_with(**reviewer) -> dict:
    value = copy.deepcopy(CONFIG)
    value["governance_documents"] = ["docs/ARCH.md"]
    value["roles"]["reviewer"].update(reviewer)
    return value


class Workspace(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = governed_tree(Path(directory.name).resolve())
        self.phase = SimpleNamespace(prompt=self.root / "prompts/phases/p.md", required_files=(), commands=(),
                                     invariants=(), allowed_paths=(), number=1)

    def runner(self, config) -> RoleRunner:
        return RoleRunner(config, FakeIsolationBackend(), IsolationContext("reviewer", self.root, self.root / "c", self.root / "r"))


class ContractTests(Workspace):
    def test_contracts_come_from_the_kernel(self):
        for name in ("PHASE_RESULT", "REVIEW_RESULT", "DEBATE_RESULT"):
            self.assertEqual(contract(name), (PROTOCOL_ROOT / "templates" / f"{name}.json").read_text())

    def test_an_unknown_contract_is_refused(self):
        with self.assertRaises(ProtocolError):
            contract("ADR")

    def test_a_repository_template_cannot_replace_a_contract(self):
        (self.root / "templates").mkdir(); (self.root / "templates/PHASE_RESULT.json").write_text('{"EVIL": 1}')
        (self.root / "templates/REVIEW_RESULT.json").write_text('{"EVIL": 1}')
        implementer = CodexAdapter(self.runner(config_with()), self.root).prompt(self.phase)
        reviewer = ClaudeAdapter(self.runner(config_with()), self.root).build_prompt(self.phase, EVIDENCE, {})
        self.assertNotIn("EVIL", implementer); self.assertNotIn("EVIL", reviewer)
        self.assertIn(contract("PHASE_RESULT"), implementer)
        self.assertIn(contract("REVIEW_RESULT"), reviewer)

    def test_the_interactive_debate_request_carries_the_contract_itself(self):
        config = config_with(); config["roles"]["arbiter"]["mode"] = "interactive"
        output = self.root / "artifacts/DEBATE_RESULT.json"
        with self.assertRaises(Exception):
            DebateAdapter(self.runner(config), self.root, config).run(self.phase, [], EVIDENCE, output, 1)
        request = json.loads((output.parent / "DEBATE_REQUEST.json").read_text())
        self.assertEqual(request["output_contract"], json.loads(contract("DEBATE_RESULT")))

    def test_protocol_files_are_part_of_the_pinned_kernel(self):
        names = {path.relative_to(PROTOCOL_ROOT.parent).as_posix() for path in kernel_files()}
        self.assertIn("protocol/templates/REVIEW_RESULT.json", names)
        self.assertIn("protocol/prompts/review.md", names)


class PromptTests(Workspace):
    def test_the_kernel_review_prompt_is_the_default(self):
        text = prompt(config_with(), "review", self.root)
        self.assertEqual(text, (PROTOCOL_ROOT / "prompts/review.md").read_text())
        self.assertIn(REVIEW_PROTOCOL_MARKER, text)
        self.assertNotIn("Relay", text, "the kernel prompt must not carry any project's policy")

    def test_a_configured_review_prompt_overrides_it(self):
        (self.root / "prompts/review.md").write_text(f"{REVIEW_PROTOCOL_MARKER}\nPROJECT POLICY")
        reviewer = ClaudeAdapter(self.runner(config_with(review_prompt="prompts/review.md")), self.root)
        self.assertIn("PROJECT POLICY", reviewer.build_prompt(self.phase, EVIDENCE, {}))

    def test_an_override_without_the_protocol_marker_is_refused(self):
        (self.root / "prompts/review.md").write_text("PROJECT POLICY")
        with self.assertRaisesRegex(ProtocolError, "does not declare"):
            prompt(config_with(review_prompt="prompts/review.md"), "review", self.root)

    def test_remediation_uses_the_kernel_default_then_the_override(self):
        config = config_with()
        default = CodexAdapter(self.runner(config), self.root).prompt(self.phase, review="r")
        self.assertIn((PROTOCOL_ROOT / "prompts/remediation.md").read_text(), default)
        (self.root / "prompts/fix.md").write_text("PROJECT REMEDIATION")
        config["roles"]["implementer"]["remediation_prompt"] = "prompts/fix.md"
        self.assertIn("PROJECT REMEDIATION", CodexAdapter(self.runner(config), self.root).prompt(self.phase, review="r"))

    def test_first_implementation_carries_no_remediation_prompt(self):
        first = CodexAdapter(self.runner(config_with()), self.root).prompt(self.phase)
        self.assertNotIn((PROTOCOL_ROOT / "prompts/remediation.md").read_text(), first)


class ContextDocumentTests(Workspace):
    def test_only_the_configured_documents_reach_the_reviewer(self):
        (self.root / "docs/OTHER.md").write_text("NOT CONTEXT")
        text = ClaudeAdapter(self.runner(config_with(context_documents=["docs/ARCH.md"])), self.root) \
            .build_prompt(self.phase, EVIDENCE, {})
        self.assertIn("CONTENT OF docs/ARCH.md", text); self.assertNotIn("NOT CONTEXT", text)

    def test_a_repository_without_product_docs_can_be_reviewed(self):
        ClaudeAdapter(self.runner(config_with(context_documents=[])), self.root).build_prompt(self.phase, EVIDENCE, {})


class ValidationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = governed_tree(Path(directory.name).resolve())

    def refused(self, config, message):
        with self.assertRaisesRegex(ConfigurationError, message):
            validate_config(config, self.root)

    def test_a_complete_config_validates(self):
        validate_config(config_with(context_documents=["docs/ARCH.md"]), self.root)

    def test_governance_documents_must_be_declared(self):
        config = config_with(); del config["governance_documents"]
        self.refused(config, "governance_documents must be declared")

    def test_governance_documents_must_be_a_list(self):
        config = config_with(); config["governance_documents"] = "docs/ARCH.md"
        self.refused(config, "governance_documents must be declared")

    def test_context_documents_must_be_declared(self):
        config = config_with(); del config["roles"]["reviewer"]["context_documents"]
        self.refused(config, "context_documents must be declared")

    def test_context_must_be_governed(self):
        (self.root / "docs/LOOSE.md").write_text("x")
        self.refused(config_with(context_documents=["docs/LOOSE.md"]), "must be a governed path")

    def test_a_missing_governance_document_is_refused(self):
        config = config_with(); config["governance_documents"].append("docs/GONE.md")
        self.refused(config, "not found: docs/GONE.md")

    def test_a_prompt_override_must_be_governed(self):
        (self.root / "review.md").write_text(REVIEW_PROTOCOL_MARKER)
        self.refused(config_with(review_prompt="review.md"), "must be a governed path")

    def test_a_prompt_override_must_exist(self):
        self.refused(config_with(review_prompt="prompts/gone.md"), "not found")

    def test_an_escaping_path_is_refused(self):
        config = config_with(); config["governance_documents"] = ["../outside.md"]
        self.refused(config, "not a safe relative path")

    def test_a_full_project_configuration_validates(self):
        """The fixture is a complete multi-phase project: overrides, context and governed docs."""
        config = load_config(TARGET / "build.yaml")
        self.assertEqual(len(config["roles"]["reviewer"]["context_documents"]), 4)
        self.assertIn("docs/ROADMAP.md", config["governance_documents"])


class GovernanceTests(unittest.TestCase):
    def test_declared_documents_are_snapshotted_and_others_are_not(self):
        temporary, root = repository(); self.addCleanup(temporary.cleanup)
        governed_tree(root, documents=("docs/ARCH.md", "docs/NOTES.md"))
        config = config_with(); config["roles"]["reviewer"]["context_documents"] = ["docs/ARCH.md"]
        (root / "build.yaml").write_text(json.dumps(config))
        git(root, "add", "."); git(root, "commit", "-qm", "governed")
        sha = git(root, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as control:
            anchor = TrustStore(root, Path(control)).initialize(sha, sha)
        self.assertIn("docs/ARCH.md", anchor["manifest"])
        self.assertNotIn("docs/NOTES.md", anchor["manifest"])

    def test_an_agent_may_not_edit_a_declared_document(self):
        temporary, root = repository(); self.addCleanup(temporary.cleanup)
        governed_tree(root); git(root, "add", "."); git(root, "commit", "-qm", "base")
        with tempfile.TemporaryDirectory() as clones:
            manager = GitManager(root, {"git": {}, "governance_documents": ["docs/ARCH.md"]})
            clone = Path(clones) / "clone"; manager.create_isolated_clone(manager.current_sha(), clone)
            before = manager.manifest(clone)
            (clone / "docs/ARCH.md").write_text("rewritten by the agent")
            with self.assertRaisesRegex(GitSafetyError, "governance path: docs/ARCH.md"):
                manager.attributed_delta(clone, before, ("docs/",))

    def test_an_undeclared_document_is_ordinary_phase_content(self):
        temporary, root = repository(); self.addCleanup(temporary.cleanup)
        governed_tree(root); git(root, "add", "."); git(root, "commit", "-qm", "base")
        with tempfile.TemporaryDirectory() as clones:
            manager = GitManager(root, {"git": {}, "governance_documents": []})
            clone = Path(clones) / "clone"; manager.create_isolated_clone(manager.current_sha(), clone)
            before = manager.manifest(clone)
            (clone / "docs/ARCH.md").write_text("updated by the phase")
            self.assertEqual(manager.attributed_delta(clone, before, ("docs/",)).added_or_modified, ("docs/ARCH.md",))


if __name__ == "__main__":
    unittest.main()
