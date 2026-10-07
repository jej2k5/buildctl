import json
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext, MacOSSandboxBackend, sanitized_environment
from buildctl.supervisor.trust import TrustStore
from tests.helpers import git


class TrustBoundaryTests(unittest.TestCase):
    def test_agent_modified_governance_config_does_not_change_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"; control = Path(directory) / "control"
            root.mkdir(); git(root, "init", "-q"); git(root, "config", "user.email", "x@y"); git(root, "config", "user.name", "x")
            (root / "build").mkdir(); (root / "build/buildctl.py").write_text("trusted = True\n")
            (root / "prompts/phases").mkdir(parents=True); (root / "prompts/phases/p.md").write_text("p")
            config = {"limits":{"max_phase_attempts":1,"max_review_format_attempts":1,"max_semantic_review_attempts":1,"max_debate_rounds":1,"max_phase_minutes":1,"max_daily_cost_usd":1,"max_consecutive_failures":1},"operation_cost_usd":{"implementation":1,"review":1,"remediation":1,"debate":1},"git":{"push":False,"create_pr":False,"merge":False},"checkpoints":{"before_phases":[0],"mvp_completion":0},"phase_defaults":{"required_files":[],"commands":[["true"]],"invariants":[],"claude_review_required":True,"debate_on_architecture_disagreement":True,"max_remediation_attempts":1,"merge_policy":"human","allowed_paths":["src/"]},"roles":{"implementer":{"command":["true"],"network":False,"auth_mode":"none","credential_env":[]},"reviewer":{"command":["true"],"network":False,"auth_mode":"none","credential_env":[],"protocol_version":"buildctl-review-v2"},"arbiter":{"command":["true"],"network":False,"auth_mode":"none","credential_env":[]}},"phases":[{"number":0,"name":"p","prompt":"prompts/phases/p.md"}]}
            (root / "build.yaml").write_text(json.dumps(config)); (root / "buildctl").write_text("x")
            git(root, "add", "."); git(root, "commit", "-qm", "factory")
            sha = git(root, "rev-parse", "HEAD"); trust = TrustStore(root, control); trust.initialize(sha, sha)
            config["git"]["push"] = True; (root / "build.yaml").write_text(json.dumps(config))
            self.assertFalse(trust.config()["git"]["push"])

    def test_gate_environment_has_no_credentials(self):
        environment = sanitized_environment("gate", {"TOKEN": "should-not-be-passed"})
        self.assertEqual(environment["TOKEN"], "should-not-be-passed")
        # Gate callers intentionally pass no credential mapping.
        self.assertNotIn("TOKEN", sanitized_environment("gate"))
        self.assertEqual(sanitized_environment("gate")["BUILDCTL_GATE_PROCESS"], "1")
