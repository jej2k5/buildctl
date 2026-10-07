import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import AgentError, RoleRunner
from buildctl.supervisor.auth import (AuthPreflight, AuthenticationRequired, HostAuthUnsupported,
                                   cleanup_staged_auth, parse_claude_status, parse_codex_status,
                                   stage_codex_auth)
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor.budget import BudgetLedger
from buildctl.supervisor.operations import PaidOperation
from buildctl.supervisor.state import BuildState, StateStore


def auth_file(path: Path, token: str = "fixture-token") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                                "tokens": {"access_token": token, "refresh_token": "refresh",
                                            "id_token": "identity", "account_id": "account"}}))
    os.chmod(path, 0o600)


class AuthMvpTests(unittest.TestCase):
    def test_status_parsers_are_deterministic_and_non_secret(self):
        self.assertEqual(parse_codex_status("Logged in using ChatGPT\n")["auth_method"], "ChatGPT")
        result = parse_claude_status(json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                 "subscriptionType": "max"}))
        self.assertEqual(result["subscription_type"], "max")
        with self.assertRaises(AuthenticationRequired):
            parse_codex_status("Not logged in")
        with self.assertRaises(AuthenticationRequired):
            parse_claude_status("not-json")

    def test_auth_check_invokes_only_status_commands(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["codex", "login", "status"]:
                return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True,
                "authMethod": "claude.ai", "subscriptionType": "max"}), "")
        result = AuthPreflight(runner).check()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(calls, [["codex", "login", "status"], ["claude", "auth", "status", "--json"]])
        self.assertTrue(all("exec" not in call and "-p" not in call for call in calls))

    def test_auth_check_fails_closed_for_logged_out_provider_and_missing_executable(self):
        def logged_out(argv, **kwargs):
            if argv[0] == "codex":
                return subprocess.CompletedProcess(argv, 1, "Not logged in", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": False}), "")
        with self.assertRaisesRegex(AuthenticationRequired, "codex login"):
            AuthPreflight(logged_out).check()
        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])
        with self.assertRaisesRegex(AuthenticationRequired, "codex login"):
            AuthPreflight(missing).check()

    def test_host_cli_preflight_reports_claude_login_without_token(self):
        config = {"roles": {"reviewer": {"auth_mode": "host_cli", "credential_env": []}}}
        def runner(argv, **kwargs):
            if argv[0] == "codex":
                return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True,
                "authMethod": "claude.ai", "subscriptionType": "max", "email": "private"}), "")
        result = AuthPreflight(runner, config).check()
        self.assertEqual(result["reviewer"]["auth_method"], "claude.ai")
        self.assertNotIn("private", json.dumps(result))

    def test_reviewer_host_cli_runtime_gate_matches_auth_check(self):
        config = {"roles": {"reviewer": {"auth_mode": "host_cli", "credential_env": []}}}
        def runner(argv, **kwargs):
            if argv[0] == "codex": return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), "")
        supervisor = Supervisor.__new__(Supervisor); supervisor.config = config; supervisor.runner = runner
        self.assertEqual(AuthPreflight(runner, config).check()["status"], "PASS")
        self.assertIsNone(supervisor._require_authentication("reviewer"))

    def test_codex_auth_stages_restrictively_and_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); host = root / "host"; home = root / "isolated"
            source = host / ".codex/auth.json"; auth_file(source)
            target, secrets = stage_codex_auth(home, host)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            # secrets is now keyed by credential kind so a leak can be classified safely.
            self.assertEqual(secrets["access_token"], "fixture-token")
            self.assertNotIn("account_id", secrets, "an account identifier is not credential material")
            self.assertTrue(target.is_file())
            cleanup_staged_auth(home)
            self.assertFalse(target.exists())
            self.assertTrue(source.exists())

    def test_invalid_or_broad_auth_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); host = root / "host"; source = host / ".codex/auth.json"
            auth_file(source); os.chmod(source, 0o644)
            with self.assertRaises(HostAuthUnsupported):
                stage_codex_auth(root / "isolated", host)
            source.write_text("not-json"); os.chmod(source, 0o600)
            with self.assertRaises(HostAuthUnsupported):
                stage_codex_auth(root / "isolated", host)

    def test_role_runner_host_auth_cleanup_and_secret_leak_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); host = root / "host"; workspace = root / "workspace"; workspace.mkdir()
            auth_file(host / ".codex/auth.json")
            context = IsolationContext("implementer", workspace, root / "control", root / "repo", host_home=host)
            config = {"roles": {"implementer": {"command": ["/usr/bin/true", "--output", "{output}", "{prompt}"],
                "auth_mode": "host_cli", "credential_env": [], "network": False}}}
            seen = {}
            def runner(command, **kwargs):
                seen["env"] = kwargs["env"]
                Path(command[2]).write_text('{"status":"complete"}')
                return subprocess.CompletedProcess(command, 0, "", "")
            result = RoleRunner(config, FakeIsolationBackend(), context, runner).run(
                "implementer", "prompt", root / "control/result.json", 1)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(seen["env"]["CODEX_HOME"].endswith("/.codex"))
            self.assertFalse(Path(seen["env"]["CODEX_HOME"]).exists())
            def leaking_runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "fixture-token", "")
            with self.assertRaisesRegex(AgentError, "SECRET_OUTPUT"):
                RoleRunner(config, FakeIsolationBackend(), context, leaking_runner).run(
                    "implementer", "prompt", root / "control/result2.json", 1)

    def test_reviewer_cannot_receive_codex_host_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); host = root / "host"; auth_file(host / ".codex/auth.json")
            context = IsolationContext("reviewer", root / "workspace", root / "control", root / "repo", host_home=host)
            config = {"roles": {"reviewer": {"command": ["/usr/bin/true"], "auth_mode": "host_cli",
                                                "credential_env": [], "network": False}}}
            def runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, '{"status":"complete"}', "")
            result = RoleRunner(config, FakeIsolationBackend(), context, runner).run(
                "reviewer", "prompt", root / "out.json", 1)
            self.assertEqual(result["status"], "complete")

    def test_staged_auth_is_cleaned_when_sandbox_command_construction_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); host = root / "host"; workspace = root / "workspace"; workspace.mkdir()
            auth_file(host / ".codex/auth.json")
            context = IsolationContext("implementer", workspace, root / "control", root / "repo", host_home=host)
            config = {"roles": {"implementer": {"command": ["/usr/bin/true"], "auth_mode": "host_cli",
                                                  "credential_env": [], "network": False}}}
            class BrokenBackend(FakeIsolationBackend):
                def command(self, argv, context):
                    raise RuntimeError("sandbox construction failed")
            role = RoleRunner(config, BrokenBackend(), context)
            with self.assertRaisesRegex(RuntimeError, "sandbox construction"):
                role.run("implementer", "prompt", root / "out.json", 1)
            runtime = workspace / ".factory-runtime"
            self.assertFalse(any(runtime.rglob("auth.json")))

    def test_auth_failure_precedes_budget_and_operation_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = StateStore(root / "control")
            state = BuildState(status="running", current_phase=0, phase_attempts={"0": 1})
            store.save(state)
            def runner(argv, **kwargs):
                if argv[0] == "codex":
                    return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": False}), "")
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.store = store; supervisor.runner = runner
            supervisor.config = {"roles": {"implementer": {"auth_mode": "host_cli"},
                                             "reviewer": {"auth_mode": "host_cli"},
                                             "arbiter": {"auth_mode": "none"}},
                                 "limits": {"max_consecutive_failures": 3}}
            supervisor.budget = BudgetLedger(root / "control", 10)
            phase = type("Phase", (), {"number": 0})()
            with self.assertRaises(HumanEscalation):
                supervisor._agent_operation(state, phase, "review", PaidOperation.REVIEW,
                                            root / "control/result.json", lambda timeout: self.fail("launched"))
            loaded = store.load()
            self.assertEqual(loaded.operations, {})
            self.assertFalse((root / "control/costs.jsonl").exists())
            self.assertTrue(any(item["event"] == "human_escalation" for item in loaded.history))
