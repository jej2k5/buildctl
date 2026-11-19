"""Node product dependencies: {node}, {npm} and {npx} acceptance commands.

ReplCRM's discovery found that a Node phase could never pass: each phase is a fresh clone with
no node_modules or generated Prisma client, the gate sandbox has no network, and anything the
implementer installs is an out-of-scope edit. The supervisor now installs package-lock.json's
integrity-pinned packages, sandboxed, into an environment outside the workspace, links it in
for the gate commands, and removes the link before the delta is taken.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from buildctl.supervisor import gates
from buildctl.supervisor.gates import (MechanicalGates, ProvisioningError, _resolve_command,
                                       node_lock_packages)
from buildctl.supervisor.isolation import FakeIsolationBackend

REG = "https://registry.npmjs.org/"


def lock(**packages):
    body = {"": {"name": "p"}}
    body.update(packages)
    return json.dumps({"name": "p", "lockfileVersion": 3, "packages": body}).encode()


GOOD = {"node_modules/pg": {"version": "8.0.0", "resolved": REG + "pg/-/pg-8.0.0.tgz", "integrity": "sha512-abc"}}
TOOLS = {"{node}": "/opt/n/node", "{npm}": "/opt/n/npm", "{npx}": "/opt/n/npx"}


class LockValidationTests(unittest.TestCase):
    def test_counts_registry_packages_with_integrity(self):
        self.assertEqual(node_lock_packages(lock(**GOOD, **{"node_modules/x": {"link": True}})), 1)

    def test_refuses_git_tarball_and_file_sources(self):
        for resolved in ("git+ssh://github.com/a/b.git", "https://evil.example/x.tgz", "file:../x"):
            with self.subTest(resolved=resolved), self.assertRaises(ProvisioningError):
                node_lock_packages(lock(**{"node_modules/x": {"resolved": resolved, "integrity": "sha512-a"}}))

    def test_refuses_missing_integrity_and_old_lockfiles(self):
        with self.assertRaises(ProvisioningError):
            node_lock_packages(lock(**{"node_modules/x": {"resolved": REG + "x.tgz"}}))
        with self.assertRaises(ProvisioningError):
            node_lock_packages(json.dumps({"lockfileVersion": 1, "dependencies": {}}).encode())
        with self.assertRaises(ProvisioningError):
            node_lock_packages(b"not json")


class ResolveTests(unittest.TestCase):
    def test_tokens_resolve_to_absolute_tools_or_fail_closed(self):
        self.assertEqual(_resolve_command(["{npm}", "test"], None, TOOLS), ["/opt/n/npm", "test"])
        self.assertIsNone(_resolve_command(["{node}", "x.js"], None, None))
        self.assertEqual(_resolve_command(["python3", "-V"], None, None), ["python3", "-V"])


class GateLinkTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.ws, ctl, repo = root / "ws", root / "ctl", root / "repo"
        for path in (self.ws, ctl, repo):
            path.mkdir()
        (self.ws / "package.json").write_text('{"name":"p"}')
        (self.ws / "package-lock.json").write_bytes(lock(**GOOD))
        self.calls = []
        self.seen_link = []

        def runner(command, cwd=None, **kwargs):
            self.calls.append((list(command), Path(cwd)))
            if command[1:2] == ["ci"]:
                (Path(cwd) / "node_modules" / "pg").mkdir(parents=True)
            else:
                self.seen_link.append((self.ws / "node_modules").is_symlink())
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        self.gates = MechanicalGates(self.ws, ctl, repo, FakeIsolationBackend(), runner=runner)

    def run_phase(self, commands):
        with mock.patch.object(gates, "node_executables", return_value=TOOLS):
            return self.gates.run(SimpleNamespace(required_files=[], commands=commands), 30)

    def test_installs_outside_workspace_links_during_commands_and_unlinks(self):
        result = self.run_phase([["{npm}", "test"]])
        self.assertTrue(result.passed, result.details)
        install_cmd, install_cwd = self.calls[0]
        self.assertEqual(install_cmd[:2], ["/opt/n/npm", "ci"])
        self.assertFalse(install_cwd.resolve().is_relative_to(self.ws.resolve()), "install must happen outside the workspace")
        self.assertEqual(self.seen_link, [True])
        self.assertFalse((self.ws / "node_modules").exists(), "the link must not enter the phase delta")

    def test_refuses_a_committed_node_modules(self):
        (self.ws / "node_modules").mkdir()
        result = self.run_phase([["{npm}", "test"]])
        self.assertFalse(result.passed)
        self.assertTrue((self.ws / "node_modules").is_dir(), "never deletes what the repository holds")

    def test_missing_lockfile_fails_closed_without_running_commands(self):
        (self.ws / "package-lock.json").unlink()
        result = self.run_phase([["{npm}", "test"]])
        self.assertFalse(result.passed)
        self.assertEqual(self.calls, [])

    def test_prisma_schema_triggers_generate_inside_the_environment(self):
        (self.ws / "prisma").mkdir()
        (self.ws / "prisma" / "schema.prisma").write_text("generator client { provider = \"prisma-client-js\" }")
        self.run_phase([["{npm}", "test"]])
        generate = [c for c, _ in self.calls if c[1:2] == ["generate"]]
        self.assertEqual(len(generate), 1)
        self.assertTrue(generate[0][0].endswith("node_modules/.bin/prisma"))
        self.assertFalse(Path(generate[0][0]).resolve().is_relative_to(self.ws.resolve()))

    def test_environment_is_reused_until_the_lockfile_changes(self):
        self.run_phase([["{npm}", "test"]])
        self.run_phase([["{npm}", "test"]])
        self.assertEqual(sum(1 for c, _ in self.calls if c[1:2] == ["ci"]), 1)
        (self.ws / "package-lock.json").write_bytes(lock(**GOOD, **{"node_modules/q": dict(GOOD["node_modules/pg"])}))
        self.run_phase([["{npm}", "test"]])
        self.assertEqual(sum(1 for c, _ in self.calls if c[1:2] == ["ci"]), 2)


if __name__ == "__main__":
    unittest.main()
