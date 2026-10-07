"""Real-backend conformance for the reviewer middle-path sandbox.

The host-authenticated reviewer must resolve the operator's interactive Claude login while
staying OS-sandboxed: workspace read-only, trusted control root and canonical repository
denied, other operations' staged credentials denied, credential stores denied. Workspace
protection must be enforced by the OS, not only by the post-hoc digest comparison.
"""
import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import host_auth_environment, operation_output_path
from buildctl.supervisor.isolation import IsolationContext, MacOSSandboxBackend, sanitized_environment
from buildctl.supervisor.phases import load_config

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
CLAUDE = Path("/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
DARWIN = platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()
STALE_CREDENTIAL = "STALE-IMPLEMENTER-CREDENTIAL"


def build_fixture(root: Path):
    workspace, control, repo = root / "ws", root / "control", root / "repo"
    for path in (workspace, control, repo):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "source.py").write_text("value = 1\n")
    (control / "state.json").write_text('{"secret":"canonical-state"}')
    (repo / "canary.txt").write_text("canonical repository\n")
    reviewer_runtime = operation_output_path(workspace, control / "REVIEW_RESULT-1.json").parent
    reviewer_runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale = operation_output_path(workspace, control / "PHASE_RESULT.json").parent / "home" / ".codex"
    stale.mkdir(parents=True, exist_ok=True, mode=0o700)
    (stale / "auth.json").write_text(json.dumps({"tokens": {"access_token": STALE_CREDENTIAL}}))
    context = IsolationContext("reviewer", workspace, control, repo, read_only_workspace=True,
                               output_dir=reviewer_runtime, home_dir=None, network_allowed=True,
                               host_home=Path.home(), host_session=True)
    return context, workspace, control, repo, reviewer_runtime, stale / "auth.json"


@unittest.skipUnless(DARWIN, "production macOS sandbox backend not supported on this host")
class ReviewerMiddlePathSandboxTests(unittest.TestCase):
    def _run(self, context, argv, workspace, timeout=60):
        command = MacOSSandboxBackend().command(argv, context)
        return subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                              env=host_auth_environment("reviewer"), timeout=timeout)

    def test_reviewer_is_actually_sandboxed(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, *_ = build_fixture(Path(directory).resolve())
            command = MacOSSandboxBackend().command(["/bin/echo", "x"], context)
            self.assertEqual(command[0], "/usr/bin/sandbox-exec")
            self.assertIn("(deny default)", command[2])

    @unittest.skipUnless(CLAUDE.is_file(), "pinned Claude CLI is not installed")
    def test_reviewer_host_authentication_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, *_ = build_fixture(Path(directory).resolve())
            result = self._run(context, [str(CLAUDE), "auth", "status", "--json"], workspace)
            self.assertEqual(result.returncode, 0,
                             "host login session must resolve inside the reviewer sandbox")

    def test_reviewer_can_read_but_never_write_the_disposable_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, *_ = build_fixture(Path(directory).resolve())
            read = self._run(context, ["/bin/cat", str(workspace / "source.py")], workspace)
            self.assertEqual(read.returncode, 0)
            self.assertIn("value = 1", read.stdout)
            create = self._run(context, ["/usr/bin/touch", str(workspace / "injected.py")], workspace)
            append = self._run(context, ["/bin/sh", "-c", f"echo x >> {workspace}/source.py"], workspace)
            self.assertNotEqual(create.returncode, 0, "workspace creation must be denied by the OS")
            self.assertNotEqual(append.returncode, 0, "workspace modification must be denied by the OS")
            self.assertFalse((workspace / "injected.py").exists())
            self.assertEqual((workspace / "source.py").read_text(), "value = 1\n")

    def test_reviewer_cannot_reach_control_root_or_canonical_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, control, repo, *_ = build_fixture(Path(directory).resolve())
            for label, argv in (
                ("read control root", ["/bin/cat", str(control / "state.json")]),
                ("write control root", ["/usr/bin/touch", str(control / "evil")]),
                ("read canonical repo", ["/bin/cat", str(repo / "canary.txt")]),
                ("write canonical repo", ["/usr/bin/touch", str(repo / "evil")]),
            ):
                result = self._run(context, argv, workspace)
                self.assertNotEqual(result.returncode, 0, f"{label} must be denied")
                self.assertNotIn("canonical", result.stdout)

    def test_reviewer_cannot_read_another_operations_staged_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, control, repo, runtime, stale = build_fixture(Path(directory).resolve())
            self.assertTrue(stale.is_file(), "the stale credential must exist, or this proves nothing")
            result = self._run(context, ["/bin/cat", str(stale)], workspace)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(STALE_CREDENTIAL, result.stdout)
            listing = self._run(context, ["/bin/ls", str(workspace / ".factory-runtime")], workspace)
            self.assertNotEqual(listing.returncode, 0, "runtime enumeration must be denied")

    def test_reviewer_operation_runtime_remains_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, control, repo, runtime, _ = build_fixture(Path(directory).resolve())
            write = self._run(context, ["/usr/bin/touch", str(runtime / "result.json")], workspace)
            self.assertEqual(write.returncode, 0, "the reviewer's own runtime must stay writable")
            self.assertTrue((runtime / "result.json").exists())

    def test_reviewer_credential_stores_are_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, *_ = build_fixture(Path(directory).resolve())
            home = Path.home()
            for label, target in (("ssh", home / ".ssh"), ("aws", home / ".aws"),
                                  ("gh", home / ".config/gh")):
                if not target.exists():
                    continue
                result = self._run(context, ["/bin/ls", str(target)], workspace)
                self.assertNotEqual(result.returncode, 0, f"~/.{label} must be denied")
            if (home / ".gitconfig").exists():
                self.assertNotEqual(
                    self._run(context, ["/bin/cat", str(home / ".gitconfig")], workspace).returncode, 0)
            self.assertNotEqual(
                self._run(context, ["/usr/bin/touch", str(home / ".buildctl-reviewer-probe")], workspace).returncode,
                0, "host HOME must not be writable")
            self.assertFalse((home / ".buildctl-reviewer-probe").exists())

    def test_reviewer_profile_denies_keychain_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, *_ = build_fixture(Path(directory).resolve())
            profile = MacOSSandboxBackend().command(["/bin/echo"], context)[2]
            self.assertIn("com.apple.securityd", profile)


class ReviewerPolicyContractTests(unittest.TestCase):
    def test_reviewer_command_keeps_read_only_tooling(self):
        reviewer = load_config(TARGET / "build.yaml")["roles"]["reviewer"]["command"]
        self.assertIn("--no-session-persistence", reviewer)
        self.assertIn("--tools", reviewer)
        self.assertEqual(reviewer[reviewer.index("--tools") + 1], "Read,Grep,Glob")
        for forbidden in ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"):
            self.assertNotIn(forbidden, reviewer[reviewer.index("--tools") + 1])

    def test_reviewer_environment_keeps_host_home_and_no_codex_home(self):
        environment = host_auth_environment("reviewer")
        self.assertEqual(environment.get("HOME"), os.environ.get("HOME"))
        self.assertNotIn("CODEX_HOME", environment)

    def test_implementer_and_gate_profiles_are_not_host_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context, workspace, control, repo, runtime, _ = build_fixture(root)
            for role in ("implementer", "gate"):
                other = IsolationContext(role, workspace, control, repo, False, runtime,
                                         runtime / "home", False, Path.home())
                self.assertFalse(other.host_session, f"{role} must not use the host-session profile")
            self.assertEqual(sanitized_environment("gate")["BUILDCTL_GATE_PROCESS"], "1")
            self.assertNotIn("HOME", sanitized_environment("gate"))
