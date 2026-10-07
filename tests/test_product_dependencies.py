"""Regression tests for product dependency provisioning.

Phase 1 wrote code importing pydantic and sqlalchemy, then failed acceptance with
`ModuleNotFoundError: No module named 'pydantic'`. Two things were missing: the repository
declared no dependency manifest at all, and the acceptance command ran a bare `python3`
inside a sandbox with no network, so nothing could ever be installed for it.

Dependencies are now declared in pyproject.toml and installed by the supervisor into an
isolated environment before the gate opens. The gate fails closed when that environment
cannot be built — it must never quietly fall back to whatever the host happens to have.
"""
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.gates import (PRODUCT_MANIFEST, PYTHON_PLACEHOLDER, MechanicalGates,
                                    ProvisioningError, _resolve_command, product_dependencies,
                                    provisioning_environment)
from buildctl.supervisor.isolation import FakeIsolationBackend

ROOT = Path(__file__).resolve().parents[1]

MINIMAL = b'[project]\nname = "p"\nversion = "0"\ndependencies = ["pydantic>=2.8,<3"]\n'


def workspace_with(directory: Path, manifest: bytes | None, with_src: bool = True) -> tuple[Path, Path, Path]:
    workspace, control, repo = directory / "ws", directory / "ctl", directory / "repo"
    for path in (workspace, control, repo):
        path.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (workspace / PRODUCT_MANIFEST).write_bytes(manifest)
    if with_src:
        (workspace / "src").mkdir(exist_ok=True)
    return workspace, control, repo


def recording_runner(calls: list, *, venv_rc: int = 0, install_rc: int = 0, gate_rc: int = 0):
    """Stand in for subprocess.run. Never touches the network."""
    def runner(command, **kwargs):
        command = list(command)
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            root = Path(command[3])
            (root / "bin").mkdir(parents=True, exist_ok=True)
            (root / "bin" / "python").write_text("#!/bin/sh\n")
            (root / "lib" / "python3.12" / "site-packages").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(command, venv_rc, "", "venv failed" if venv_rc else "")
        if "install" in command:
            return subprocess.CompletedProcess(command, install_rc, "",
                                               "No matching distribution" if install_rc else "")
        return subprocess.CompletedProcess(command, gate_rc, "", "")
    return runner


def phase(commands, required_files=()):
    return SimpleNamespace(required_files=required_files, commands=commands)


PRODUCT_COMMAND = ((PYTHON_PLACEHOLDER, "-m", "unittest", "discover", "-s", "tests/product"),)


class DependencyParsingTests(unittest.TestCase):
    """Parsing is data-only: an agent-authored manifest must not be able to execute code."""

    def test_declared_dependencies_are_returned_in_order(self):
        self.assertEqual(product_dependencies(MINIMAL), ("pydantic>=2.8,<3",))

    def test_a_manifest_without_dependencies_yields_nothing(self):
        self.assertEqual(product_dependencies(b'[project]\nname = "p"\nversion = "0"\n'), ())

    def test_invalid_toml_is_refused(self):
        with self.assertRaisesRegex(ProvisioningError, "not valid TOML"):
            product_dependencies(b"[project\nname =")

    def test_a_manifest_without_a_project_table_is_refused(self):
        with self.assertRaisesRegex(ProvisioningError, "no \\[project\\] table"):
            product_dependencies(b'[tool.other]\nx = 1\n')

    def test_dependencies_must_be_an_array(self):
        with self.assertRaisesRegex(ProvisioningError, "must be an array"):
            product_dependencies(b'[project]\nname="p"\nversion="0"\ndependencies = "pydantic"\n')

    def test_a_dependency_entry_may_not_be_a_pip_option(self):
        """Otherwise a manifest could inject --index-url or --editable into the install."""
        for hostile in ('["--index-url=http://evil.example/simple"]',
                        '["-e /tmp/x"]', '["--target=/usr/local/lib"]'):
            with self.assertRaisesRegex(ProvisioningError, "may not be a pip option"):
                product_dependencies(f'[project]\nname="p"\nversion="0"\ndependencies = {hostile}\n'.encode())

    def test_non_string_entries_are_refused(self):
        with self.assertRaisesRegex(ProvisioningError, "non-empty strings"):
            product_dependencies(b'[project]\nname="p"\nversion="0"\ndependencies = [{a = 1}]\n')


class CommandResolutionTests(unittest.TestCase):
    def test_the_placeholder_is_replaced_with_the_provisioned_interpreter(self):
        resolved = _resolve_command([PYTHON_PLACEHOLDER, "-m", "unittest"], Path("/env/bin/python"))
        self.assertEqual(resolved, ["/env/bin/python", "-m", "unittest"])

    def test_a_command_without_the_placeholder_is_untouched(self):
        argv = ["python3", "-c", "print(1)"]
        self.assertEqual(_resolve_command(list(argv), None), argv)

    def test_an_unresolvable_placeholder_refuses_the_command(self):
        self.assertIsNone(_resolve_command([PYTHON_PLACEHOLDER, "-m", "unittest"], None))

    def test_unrelated_braces_do_not_raise(self):
        """Substitution is a string replace, not str.format."""
        argv = ["python3", "-c", "print({'a': 1})", "{phase}"]
        self.assertEqual(_resolve_command(list(argv), Path("/env/bin/python")), argv)


class GateFailsClosedTests(unittest.TestCase):
    def _run(self, manifest, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), manifest)
            calls: list = []
            result = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                     recording_runner(calls, **kwargs)).run(phase(PRODUCT_COMMAND), 30)
            return result, calls

    def test_a_missing_manifest_fails_the_gate(self):
        result, calls = self._run(None)
        self.assertFalse(result.passed)
        provision = [d for d in result.details if d["gate"] == "dependency_provision"]
        self.assertEqual(len(provision), 1)
        self.assertFalse(provision[0]["passed"])
        self.assertIn("no product dependency manifest", provision[0]["output"])

    def test_a_missing_manifest_never_runs_the_tests_against_the_host(self):
        """The original bug: a bare python3 silently used whatever the host had installed."""
        result, calls = self._run(None)
        self.assertEqual(calls, [], "no command may run without a provisioned environment")
        command = [d for d in result.details if d["gate"] == "command"][0]
        self.assertFalse(command["passed"])
        self.assertIn("dependency environment unavailable", command["output"])

    def test_a_failed_dependency_install_fails_the_gate(self):
        result, calls = self._run(MINIMAL, install_rc=1)
        self.assertFalse(result.passed)
        provision = [d for d in result.details if d["gate"] == "dependency_provision"][0]
        self.assertFalse(provision["passed"])
        self.assertIn("could not be installed", provision["output"])
        self.assertFalse(any("unittest" in " ".join(call) for call in calls),
                         "acceptance tests must not run when dependencies are missing")

    def test_a_failed_environment_creation_fails_the_gate(self):
        result, _ = self._run(MINIMAL, venv_rc=1)
        self.assertFalse(result.passed)
        self.assertIn("could not create the product environment",
                      [d for d in result.details if d["gate"] == "dependency_provision"][0]["output"])

    def test_an_unparseable_manifest_fails_the_gate_rather_than_the_supervisor(self):
        result, _ = self._run(b"[project\n")
        self.assertFalse(result.passed)
        self.assertIn("not valid TOML",
                      [d for d in result.details if d["gate"] == "dependency_provision"][0]["output"])


class GateProvisioningTests(unittest.TestCase):
    def test_the_tests_run_under_the_provisioned_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            result = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                     recording_runner(calls)).run(phase(PRODUCT_COMMAND), 30)
            self.assertTrue(result.passed)
            gate_call = [c for c in calls if "unittest" in c][0]
            self.assertTrue(gate_call[0].endswith("/bin/python"), gate_call[0])
            self.assertNotEqual(gate_call[0], "python3")
            self.assertNotIn(PYTHON_PLACEHOLDER, gate_call)

    def test_the_declared_dependencies_are_what_gets_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                            recording_runner(calls)).run(phase(PRODUCT_COMMAND), 30)
            install = [c for c in calls if "install" in c][0]
            self.assertIn("pydantic>=2.8,<3", install)
            self.assertIn("--only-binary=:all:", install)

    def test_the_agent_authored_package_is_never_built_or_installed(self):
        """Installing the project would execute the build backend it names, as the
        supervisor, unsandboxed. Only pre-built wheels of declared dependencies are
        installed; the source tree is put on the path with a .pth file instead."""
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                            recording_runner(calls)).run(phase(PRODUCT_COMMAND), 30)
            install = [c for c in calls if "install" in c][0]
            self.assertNotIn("-e", install)
            self.assertNotIn(".", install)
            self.assertFalse(any(str(workspace) in part for part in install),
                             "the workspace project must never be handed to pip")

    def test_the_product_source_tree_is_linked_onto_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            gates = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                    recording_runner(calls))
            interpreter, _ = gates.product_interpreter()
            pth = list((interpreter.parent.parent / "lib").glob("python*/site-packages/_buildctl_product_src.pth"))
            self.assertEqual(len(pth), 1)
            self.assertEqual(pth[0].read_text().strip(), str((workspace / "src").resolve()))

    def test_dependencies_are_installed_once_across_remediation_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            gates = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                    recording_runner(calls))
            for _ in range(3):
                gates.run(phase(PRODUCT_COMMAND), 30)
            self.assertEqual(len([c for c in calls if "install" in c]), 1)

    def test_editing_the_manifest_between_rounds_rebuilds_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            calls: list = []
            gates = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                    recording_runner(calls))
            gates.run(phase(PRODUCT_COMMAND), 30)
            (workspace / PRODUCT_MANIFEST).write_bytes(
                b'[project]\nname="p"\nversion="0"\ndependencies = ["pydantic>=2.8,<3", "structlog>=24.4,<26"]\n')
            gates.run(phase(PRODUCT_COMMAND), 30)
            installs = [c for c in calls if "install" in c]
            self.assertEqual(len(installs), 2)
            self.assertIn("structlog>=24.4,<26", installs[1])

    def test_a_phase_that_needs_no_interpreter_provisions_nothing(self):
        """Phase 0 gates a documentation tree with a self-contained python3 -c script."""
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), None)
            calls: list = []
            result = MechanicalGates(workspace, control, repo, FakeIsolationBackend(),
                                     recording_runner(calls)).run(
                phase((("python3", "-c", "print('ok')"),)), 30)
            self.assertTrue(result.passed)
            self.assertEqual([d["gate"] for d in result.details], ["command"])
            self.assertEqual(calls, [["python3", "-c", "print('ok')"]])


class GateEnvironmentTests(unittest.TestCase):
    def test_the_gate_does_not_write_bytecode_beside_the_sources(self):
        """__pycache__ lands under an allowed path, is gitignored, and is then refused at
        commit time as an ignored path — failing the phase after every gate passed."""
        with tempfile.TemporaryDirectory() as directory:
            workspace, control, repo = workspace_with(Path(directory).resolve(), MINIMAL)
            seen: list = []

            def runner(command, **kwargs):
                command = list(command)
                if command[1:3] == ["-m", "venv"]:
                    root = Path(command[3]); (root / "bin").mkdir(parents=True, exist_ok=True)
                    (root / "bin" / "python").write_text("#!/bin/sh\n")
                    (root / "lib" / "python3.12" / "site-packages").mkdir(parents=True, exist_ok=True)
                elif "unittest" in command:
                    seen.append(kwargs["env"])
                return subprocess.CompletedProcess(command, 0, "", "")

            MechanicalGates(workspace, control, repo, FakeIsolationBackend(), runner).run(
                phase(PRODUCT_COMMAND), 30)
            self.assertEqual(seen[0]["PYTHONDONTWRITEBYTECODE"], "1")


class ProvisioningEnvironmentTests(unittest.TestCase):
    def test_ambient_pip_configuration_is_neutralised(self):
        """A user-level `global.target` in pip.conf redirects installs into a shared host
        site-packages: the environment would come out empty and the host would be mutated."""
        self.assertEqual(provisioning_environment()["PIP_CONFIG_FILE"], os.devnull)

    def test_installing_outside_the_environment_is_refused(self):
        self.assertEqual(provisioning_environment()["PIP_REQUIRE_VIRTUALENV"], "1")

    def test_ambient_pip_variables_are_not_inherited(self):
        original = os.environ.get("PIP_TARGET")
        os.environ["PIP_TARGET"] = "/usr/local/lib/python3.13/site-packages"
        try:
            self.assertNotIn("PIP_TARGET", provisioning_environment())
        finally:
            if original is None:
                os.environ.pop("PIP_TARGET", None)
            else:
                os.environ["PIP_TARGET"] = original
