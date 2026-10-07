"""Regression tests for the host-authenticated reviewer launch path.

The reviewer is an explicit MVP exception: it is read-only by pinned command and must
resolve the operator's interactive `claude auth login` session. Running it through the
isolation backend with a replaced HOME made that session invisible, so every review died
instantly with REVIEWER_PROCESS_FAILED. These tests pin the exception precisely and prove
the implementer and gate paths did not move with it.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import (AgentError, HOST_AUTH_ENV_ALLOW, PROCESS_DIAGNOSTIC_LIMIT,
                                     RoleRunner, host_auth_environment, operation_output_path)
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext, sanitized_environment

REVIEW_RESULT = '{"status": "complete"}'


class RecordingBackend(FakeIsolationBackend):
    """Records whether the isolation wrapper was consulted for a launch."""

    def __init__(self):
        self.calls = []
        self.contexts = []

    def command(self, argv, context):
        self.calls.append((list(argv), context.role))
        self.contexts.append(context)
        return ["/usr/bin/env", "BUILDCTL_SANDBOX_WRAPPED=1", *argv]


def _harness(root: Path, role: str, auth_mode: str, network=False):
    workspace, control, repo = root / "ws", root / "control", root / "repo"
    for path in (workspace, control, repo):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "source.py").write_text("value = 1\n")
    config = {"roles": {role: {"auth_mode": auth_mode, "credential_env": [], "network": network,
                               "command": ["/bin/echo", "--role", role, "--out", "{output}"]}},
              "_trusted_executables": {}}
    context = IsolationContext(role, workspace, control, repo, role == "reviewer",
                               host_home=Path.home())
    return config, context, workspace, control


def _capturing_runner(seen: dict, write_result=True):
    def runner(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs.get("env") or {})
        seen["cwd"] = str(kwargs.get("cwd"))
        if write_result:
            out = Path(argv[argv.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(REVIEW_RESULT)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return runner


class ReviewerHostRuntimeTests(unittest.TestCase):
    def test_reviewer_host_cli_is_sandboxed_under_the_host_session_profile(self):
        """The reviewer is OS-sandboxed, but under the host-session profile, not the
        implementer profile: it keeps the operator HOME and gets no isolated home."""
        with tempfile.TemporaryDirectory() as directory:
            config, context, workspace, control = _harness(Path(directory).resolve(), "reviewer", "host_cli")
            backend, seen = RecordingBackend(), {}
            RoleRunner(config, backend, context, _capturing_runner(seen)).run(
                "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            self.assertEqual(len(backend.calls), 1, "reviewer must still be OS-sandboxed")
            self.assertTrue(backend.contexts[0].host_session, "reviewer needs the host-session profile")
            self.assertTrue(backend.contexts[0].read_only_workspace)
            self.assertIsNone(backend.contexts[0].home_dir, "no isolated home for the reviewer")

    def test_reviewer_host_cli_keeps_the_operator_home_and_sets_no_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "host_cli")
            seen = {}
            RoleRunner(config, RecordingBackend(), context, _capturing_runner(seen)).run(
                "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            self.assertEqual(seen["env"].get("HOME"), os.environ.get("HOME"))
            runtime = operation_output_path(workspace, control / "REVIEW_RESULT-1.json").parent
            self.assertNotEqual(seen["env"].get("HOME"), str(runtime / "home"))
            self.assertNotIn("CODEX_HOME", seen["env"])
            self.assertFalse((runtime / "home").exists(), "no isolated HOME should be created")

    def test_reviewer_runs_with_cwd_set_to_the_disposable_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "host_cli")
            seen = {}
            RoleRunner(config, RecordingBackend(), context, _capturing_runner(seen)).run(
                "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            self.assertEqual(seen["cwd"], str(workspace))

    def test_reviewer_still_uses_the_pinned_executable_and_verifies_its_digest(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "host_cli")
            pinned = Path("/bin/echo")
            digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
            config["_trusted_executables"] = {"reviewer": {"path": str(pinned), "sha256": digest}}
            seen = {}
            RoleRunner(config, RecordingBackend(), context, _capturing_runner(seen)).run(
                "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            # argv[0] is the sandbox wrapper; the pinned executable must be the program it runs.
            self.assertIn(str(pinned), seen["argv"])
            config["_trusted_executables"]["reviewer"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(AgentError, "EXECUTABLE_INTEGRITY_FAILURE"):
                RoleRunner(config, RecordingBackend(), context, _capturing_runner({})).run(
                    "reviewer", "prompt", control / "REVIEW_RESULT-2.json", 30)

    def test_reviewer_mutation_detection_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "host_cli")

            def mutating(argv, **kwargs):
                (workspace / "source.py").write_text("value = 999  # reviewer wrote this\n")
                out = Path(argv[argv.index("--out") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(REVIEW_RESULT)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with self.assertRaisesRegex(AgentError, "REVIEWER_MUTATED_WORKSPACE"):
                RoleRunner(config, RecordingBackend(), context, mutating).run(
                    "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)

    def test_reviewer_environment_does_not_inherit_unrelated_host_secrets(self):
        planted = {"GITHUB_TOKEN": "gh-should-not-appear", "AWS_SECRET_ACCESS_KEY": "aws-nope",
                   "ANTHROPIC_API_KEY": "anthropic-nope", "SSH_AUTH_SOCK": "/tmp/agent.sock"}
        previous = {key: os.environ.get(key) for key in planted}
        os.environ.update(planted)
        try:
            environment = host_auth_environment("reviewer")
            for key in planted:
                self.assertNotIn(key, environment, f"{key} must not reach the reviewer")
            self.assertIn("HOME", HOST_AUTH_ENV_ALLOW)
            self.assertEqual(environment.get("BUILDCTL_SUBPROCESS_ROLE"), "reviewer")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_reviewer_process_failure_retains_a_redacted_bounded_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "host_cli")

            def failing(argv, **kwargs):
                # Reason on stdout only, which the previous implementation discarded.
                return subprocess.CompletedProcess(argv, 1, "Invalid API key · Please run /login\n" + "x" * 4000, "")

            with self.assertRaises(AgentError) as caught:
                RoleRunner(config, RecordingBackend(), context, failing).run(
                    "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            message = str(caught.exception)
            self.assertTrue(message.startswith("REVIEWER_PROCESS_FAILED"))
            self.assertIn("Please run /login", message)
            self.assertLessEqual(len(message.split(":", 1)[1]), PROCESS_DIAGNOSTIC_LIMIT)


class UnchangedPathsTests(unittest.TestCase):
    def test_implementer_still_uses_the_isolation_backend_and_isolated_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "implementer", "none")
            backend, seen = RecordingBackend(), {}
            RoleRunner(config, backend, context, _capturing_runner(seen)).run(
                "implementer", "prompt", control / "PHASE_RESULT.json", 30)
            self.assertEqual([role for _, role in backend.calls], ["implementer"])
            self.assertIn("BUILDCTL_SANDBOX_WRAPPED=1", seen["argv"])
            runtime = operation_output_path(workspace, control / "PHASE_RESULT.json").parent
            self.assertEqual(seen["env"]["HOME"], str((runtime / "home").resolve()))
            self.assertNotEqual(seen["env"]["HOME"], os.environ.get("HOME"))

    def test_reviewer_in_a_non_host_cli_mode_is_still_fully_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config, context, workspace, control = _harness(root, "reviewer", "environment")
            backend, seen = RecordingBackend(), {}
            RoleRunner(config, backend, context, _capturing_runner(seen)).run(
                "reviewer", "prompt", control / "REVIEW_RESULT-1.json", 30)
            self.assertEqual([role for _, role in backend.calls], ["reviewer"],
                             "the exception must be scoped to host_cli only")
            self.assertNotEqual(seen["env"]["HOME"], os.environ.get("HOME"))

    def test_gate_environment_and_guard_are_unchanged(self):
        environment = sanitized_environment("gate")
        self.assertEqual(environment["BUILDCTL_GATE_PROCESS"], "1")
        self.assertNotIn("HOME", environment)
        self.assertNotIn("CODEX_HOME", environment)


class PreflightOrderingTests(unittest.TestCase):
    def test_logged_out_claude_fails_before_any_provider_invocation(self):
        from buildctl.buildctl import Supervisor
        from buildctl.supervisor.auth import AuthenticationRequired
        calls = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "codex":
                return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")
            if argv[:3] == ["claude", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 1, "", "not logged in")
            raise AssertionError(f"provider invoked despite failed preflight: {argv[:2]}")

        supervisor = Supervisor.__new__(Supervisor)
        supervisor.config = {"roles": {"reviewer": {"auth_mode": "host_cli", "credential_env": []}}}
        supervisor.runner = runner
        with self.assertRaises(AuthenticationRequired):
            supervisor._require_authentication("reviewer")
        self.assertNotIn(["claude", "-p"], [call[:2] for call in calls])
