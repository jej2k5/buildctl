"""Regression tests for the Phase 0 implementer launch failure.

These reproduce the three concrete defects that prevented `codex exec` from starting:
  1. the sandbox denied `sysctl`, so the Rust CLI aborted before argument parsing;
  2. the deny on `.factory-runtime` blocked canonicalization of `CODEX_HOME`;
  3. `--full-auto` is not a flag of the pinned Codex CLI.
Only non-model provider commands are used.
"""
import json
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.buildctl import DIAGNOSTIC_LIMIT
from buildctl.supervisor.agents import operation_output_path
from buildctl.supervisor.isolation import IsolationContext, MacOSSandboxBackend, sanitized_environment
from buildctl.supervisor.phases import load_config
from tests.credential_shapes import openai_style_key

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
PINNED_CODEX = Path("/opt/homebrew/Caskroom/codex/0.147.0/bin/codex")
DARWIN = platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()


def _context(root: Path, role: str = "implementer"):
    workspace, control, repo = root / "ws", root / "control", root / "repo"
    for path in (workspace, control, repo):
        path.mkdir(parents=True, exist_ok=True)
    runtime = operation_output_path(workspace, control / "PHASE_RESULT.json").parent
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = runtime / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    return IsolationContext(role, workspace, control, repo, role == "reviewer",
                            runtime, home, True, Path.home()), workspace, runtime, home


class ImplementerLaunchProfileTests(unittest.TestCase):
    def test_profile_grants_sysctl_read_and_runtime_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, runtime, _ = _context(Path(directory).resolve())
            profile = MacOSSandboxBackend().command(["/usr/bin/true"], context)[2]
            runtime_area = (workspace / ".factory-runtime").resolve()
            self.assertIn("(allow sysctl-read)", profile)
            self.assertIn(f'(allow file-read-metadata (literal "{runtime_area}"))', profile)
            # The credential boundary itself must be unchanged.
            self.assertIn(f'(deny file-read* file-write* (subpath "{runtime_area}"))', profile)
            self.assertNotIn(f'(allow file-read* (subpath "{runtime_area}"))', profile)
            self.assertNotIn("(allow sysctl*)", profile)


@unittest.skipUnless(DARWIN, "production macOS sandbox backend not supported on this host")
class RealBackendLaunchTests(unittest.TestCase):
    @unittest.skipUnless(PINNED_CODEX.is_file(), "pinned Codex CLI is not installed")
    def test_pinned_codex_starts_inside_production_sandbox(self):
        """Reproduces defect 1: the CLI aborted with a stack-guard-page panic."""
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, _, home = _context(Path(directory).resolve())
            command = MacOSSandboxBackend().command([str(PINNED_CODEX), "--version"], context)
            result = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                                    env=sanitized_environment("implementer", home=home), timeout=60)
            self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
            self.assertNotIn("guard page", (result.stderr or ""))

    @unittest.skipUnless(PINNED_CODEX.is_file(), "pinned Codex CLI is not installed")
    def test_codex_home_under_runtime_can_be_canonicalized(self):
        """Reproduces defect 2: CODEX_HOME failed to canonicalize through .factory-runtime."""
        with tempfile.TemporaryDirectory() as directory:
            context, workspace, _, home = _context(Path(directory).resolve())
            codex_home = home / ".codex"
            codex_home.mkdir(mode=0o700)
            (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
            environment = sanitized_environment("implementer", home=home)
            environment["CODEX_HOME"] = str(codex_home.resolve())
            command = MacOSSandboxBackend().command([str(PINNED_CODEX), "exec", "--help"], context)
            result = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                                    env=environment, timeout=60)
            combined = (result.stdout or "") + (result.stderr or "")
            self.assertNotIn("failed to canonicalize CODEX_HOME", combined)
            self.assertEqual(result.returncode, 0, combined)

    def test_runtime_metadata_allow_does_not_reopen_cross_role_reads(self):
        """The canonicalization fix must not weaken credential isolation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context, workspace, runtime, _ = _context(root)
            secret = runtime / "home" / ".codex"
            secret.mkdir(parents=True, exist_ok=True, mode=0o700)
            (secret / "auth.json").write_text('{"tokens":{"access_token":"FIXTURE-ONLY"}}')
            other = operation_output_path(workspace, root / "control" / "REVIEW_RESULT-1.json").parent
            other.mkdir(parents=True, exist_ok=True, mode=0o700)
            other_home = other / "home"
            other_home.mkdir(exist_ok=True, mode=0o700)
            reviewer = IsolationContext("reviewer", workspace, root / "control", root / "repo",
                                        True, other, other_home, False, Path.home())
            backend = MacOSSandboxBackend()
            environment = sanitized_environment("reviewer", home=other_home)
            read = subprocess.run(backend.command(["/bin/cat", str(secret / "auth.json")], reviewer),
                                  cwd=workspace, text=True, capture_output=True, env=environment, timeout=30)
            listing = subprocess.run(backend.command(["/bin/ls", str(workspace / ".factory-runtime")], reviewer),
                                     cwd=workspace, text=True, capture_output=True, env=environment, timeout=30)
            self.assertNotEqual(read.returncode, 0)
            self.assertNotIn("FIXTURE-ONLY", read.stdout or "")
            self.assertNotEqual(listing.returncode, 0, "runtime enumeration must stay denied")


class ConfiguredCodexFlagsTests(unittest.TestCase):
    def test_configured_implementer_command_has_no_removed_full_auto_flag(self):
        """Reproduces defect 3: --full-auto is rejected by the pinned CLI.

        The replacement must also not ask Codex to build its own macOS sandbox
        inside ours, because macOS refuses to nest sandboxes; the factory's own
        deny-by-default profile remains the authorization boundary.
        """
        command = load_config(TARGET / "build.yaml")["roles"]["implementer"]["command"]
        self.assertNotIn("--full-auto", command)
        self.assertNotIn("--sandbox", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--ignore-user-config", command)

    @unittest.skipUnless(PINNED_CODEX.is_file(), "pinned Codex CLI is not installed")
    def test_every_configured_flag_is_supported_by_the_pinned_cli(self):
        command = load_config(TARGET / "build.yaml")["roles"]["implementer"]["command"]
        help_text = subprocess.run([str(PINNED_CODEX), "exec", "--help"], text=True,
                                   capture_output=True, timeout=60).stdout
        for part in command:
            if part.startswith("--"):
                self.assertIn(part, help_text, f"{part} is not supported by the pinned Codex CLI")


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_provider_failure_detail_is_retained_redacted_and_bounded(self):
        from buildctl.supervisor.safety import redact
        leaked = openai_style_key("live-SHOULD-NOT-APPEAR")
        stderr = f"error: unexpected argument '--full-auto' found\napi_key: {leaked}\n" + "x" * 4000
        message = f"IMPLEMENTER_PROCESS_FAILED:{redact(stderr, [leaked], 500)}"
        diagnostic = redact(message, limit=DIAGNOSTIC_LIMIT)
        self.assertTrue(diagnostic.startswith("IMPLEMENTER_PROCESS_FAILED"))
        self.assertIn("unexpected argument", diagnostic, "the actionable provider reason must survive")
        self.assertNotIn(leaked, diagnostic)
        self.assertLessEqual(len(diagnostic), DIAGNOSTIC_LIMIT)
        self.assertEqual(message.split(":", 1)[0], "IMPLEMENTER_PROCESS_FAILED",
                         "the stable control-flow code must be unchanged")
