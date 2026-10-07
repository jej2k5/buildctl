import platform
import tempfile
import unittest
import subprocess
import json
import os
from pathlib import Path

from buildctl.buildctl import Supervisor
from buildctl.supervisor.isolation import (IsolationContext, MacOSSandboxBackend, IsolationError,
                                        probe_owned_path, sanitized_environment, verify_backend)
from buildctl.supervisor.safety import ensure_safe_relative
from buildctl.supervisor.trust import TrustStore

TARGET = Path(__file__).resolve().parent / "fixtures" / "target"


class NoDevNullWriteBackend(MacOSSandboxBackend):
    """Regression fixture for the exact permission added in remediation pass 3."""

    def command(self, argv, context):
        command = super().command(argv, context)
        command[2] = command[2].replace(' (allow file-write* (literal "/dev/null"))', "")
        return command


@unittest.skipUnless(platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file(),
                     "production macOS sandbox backend not supported on this host")
class ProductionIsolationConformanceTests(unittest.TestCase):
    def test_real_backend_enforces_runtime_boundary_and_realpaths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); control=root/"control"; repo=root/"repo"
            control.mkdir(); repo.mkdir()
            alias=root/"repo-alias"; alias.symlink_to(repo, target_is_directory=True)
            result = verify_backend(MacOSSandboxBackend(),control,alias)
            self.assertEqual(set(result["roles"]), {"implementer", "reviewer", "gate"})
            for role, observed in result["roles"].items():
                self.assertTrue(observed["devnull_functional"])
                self.assertTrue(observed["git_version"])
                self.assertTrue(observed["git_status"])
                self.assertTrue(observed["git_diff"])
                self.assertTrue(observed["git_push_reached_transport"])
                self.assertFalse(observed["git_push_succeeded"])
                self.assertEqual(observed["git_push_failure_reason"], "protected_remote_denied")
                self.assertTrue(observed["network_probe_prerequisite"])
                self.assertFalse(observed["network"])
                self.assertFalse(observed["host_codex_auth_read"])
                self.assertEqual(observed["staged_codex_auth_read"], role == "implementer")
                self.assertFalse(observed["stale_implementer_auth_read"])
            workspace=root/"network-work"; workspace.mkdir(); output=root/"network-output"; output.mkdir(); home=output/"home"; home.mkdir()
            context=IsolationContext("implementer",workspace,control,alias,output_dir=output,home_dir=home,
                                     network_allowed=True,host_home=Path.home())
            command=MacOSSandboxBackend().command(["/bin/echo","ok"],context)
            completed=subprocess.run(command,text=True,capture_output=True,env=sanitized_environment("implementer",home=home))
            self.assertEqual((completed.returncode,completed.stdout.strip()),(0,"ok"))

    def test_devnull_permission_is_exact_and_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); control = root / "control"; repo = root / "repo"
            workspace = root / "workspace"; output = root / "output"; home = output / "home"
            for path in (control, repo, workspace, home):
                path.mkdir(parents=True, exist_ok=True)
            context = IsolationContext("implementer", workspace, control, repo,
                                       output_dir=output, home_dir=home, host_home=Path.home())
            backend = MacOSSandboxBackend()
            command = backend.command(["/usr/bin/python3", "-c",
                                       "import os; fd=os.open('/dev/null',os.O_RDWR); os.close(fd)"], context)
            profile = command[2]
            self.assertIn('(allow file-write* (literal "/dev/null"))', profile)
            self.assertNotIn('(allow file-write* (subpath "/dev"))', profile)
            ok = subprocess.run(command, text=True, capture_output=True,
                                env=sanitized_environment("implementer", home=home))
            self.assertEqual(ok.returncode, 0, ok.stderr)
            denied_command = NoDevNullWriteBackend().command(
                ["/usr/bin/python3", "-c", "import os; os.open('/dev/null',os.O_RDWR)"], context)
            denied = subprocess.run(denied_command, text=True, capture_output=True,
                                    env=sanitized_environment("implementer", home=home))
            self.assertNotEqual(denied.returncode, 0)

    def test_trusted_snapshot_survives_verify_after_trust_init(self):
        """The release-gate ordering must work against a real 0400 snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); control = root / "control"
            repo = root / "repo"; repo.mkdir(); control.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "probe"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "probe@example.invalid"], check=True)
            (repo / "build.yaml").write_text((TARGET / "build.yaml").read_text())
            subprocess.run(["git", "-C", str(repo), "add", "build.yaml"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "factory"], check=True)
            sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            # The governed tree TrustStore snapshots. The kernel is not part of it: it is the
            # installed package, pinned by identity in the anchor.
            for relative, source in (("templates", TARGET / "templates"),
                                     ("prompts", TARGET / "prompts"), ("docs", TARGET / "docs")):
                target = repo / relative
                if source.is_dir():
                    import shutil; shutil.copytree(source, target, dirs_exist_ok=True,
                                                  ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                elif source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(source.read_bytes())
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "factory kernel"], check=True)
            sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            anchor = TrustStore(repo, control).initialize(sha, sha)
            snapshot = control / "snapshot/build.yaml"
            before = snapshot.read_bytes(); before_hash = __import__("hashlib").sha256(before).hexdigest()
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o400)
            supervisor = Supervisor(repo, backend=MacOSSandboxBackend(), control_root=control)
            record = supervisor.verify_isolation()
            repeated = supervisor.verify_isolation()
            self.assertEqual(record["snapshot_digest"], anchor["snapshot_digest"])
            self.assertEqual(repeated["policy_hash"], record["policy_hash"])
            self.assertTrue((control / "isolation-conformance.json").is_file())
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(__import__("hashlib").sha256(snapshot.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o400)
            supervisor.bootstrap()

    def test_probe_path_rejects_trusted_snapshot_or_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); control = root / "control"; control.mkdir()
            snapshot = control / "snapshot"; snapshot.mkdir(); (snapshot / "build.yaml").write_text("trusted")
            probe = control / ".isolation-probe"; probe.mkdir(mode=0o700)
            self.assertNotEqual(probe.resolve(), snapshot.resolve())
            self.assertEqual((snapshot / "build.yaml").read_text(), "trusted")
            with self.assertRaises(IsolationError):
                probe_owned_path(control, "snapshot/build.yaml")
            probe_target = probe / "trusted-config"
            probe_target.symlink_to(snapshot / "build.yaml")
            self.assertTrue(probe_target.is_symlink())
            with self.assertRaises(ValueError):
                ensure_safe_relative(probe, "trusted-config")

    def test_probe_directory_is_bounded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); control = root / "control"; repo = root / "repo"
            control.mkdir(); repo.mkdir()
            first = verify_backend(MacOSSandboxBackend(), control, repo)
            probe = control / ".isolation-probe"
            stale = probe / "stale-probe-file"; stale.write_text("stale")
            second = verify_backend(MacOSSandboxBackend(), control, repo)
            self.assertEqual(first["roles"].keys(), second["roles"].keys())
            self.assertTrue(stale.exists())
            self.assertEqual(probe.stat().st_mode & 0o777, 0o700)
            self.assertFalse((control / "snapshot").exists())
