import hashlib
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.supervisor.gates import MechanicalGates
from buildctl.supervisor.isolation import FakeIsolationBackend
from buildctl.supervisor.phases import load_config
from buildctl.supervisor.safety import ensure_safe_relative
from buildctl.supervisor.state import BuildState, StateStore


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(__file__).resolve().parent / "fixtures" / "target"


class GateAndPathTests(unittest.TestCase):
    def test_exact_acceptance_command_cannot_mutate_canonical_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); workspace, control, repo = base / "work", base / "control", base / "repo"
            (workspace / "tests/product").mkdir(parents=True); control.mkdir(); repo.mkdir()
            # The default acceptance command runs under the provisioned product interpreter,
            # so the workspace must declare a manifest. No dependencies: nothing is fetched.
            (workspace / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\ndependencies = []\n')
            store = StateStore(control); store.save(BuildState(status="running", completed_phases=[1,2]))
            before = store.path.read_bytes()
            probe = f'''\nimport os, sys, unittest\nfrom pathlib import Path\nsys.path.insert(0, {str(ROOT)!r})\nfrom buildctl.supervisor.state import BuildState, StateStore\nclass Probe(unittest.TestCase):\n def test_guard(self):\n  with self.assertRaises(RuntimeError): StateStore(Path({str(control)!r})).save(BuildState())\n'''
            (workspace / "tests/product/test_probe.py").write_text(probe)
            phase = SimpleNamespace(required_files=(), commands=(tuple(load_config(TARGET / "build.yaml")["phase_defaults"]["commands"][0]),))
            result = MechanicalGates(workspace, control, repo, FakeIsolationBackend()).run(phase, 10)
            self.assertTrue(result.passed)
            self.assertEqual(before, store.path.read_bytes())

    def test_gate_uses_argv_not_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "control").mkdir(); (root / "repo").mkdir()
            calls=[]
            def runner(command, **kwargs):
                calls.append((command, kwargs)); return subprocess.CompletedProcess(command, 0, "", "")
            phase=SimpleNamespace(required_files=(), commands=(("true;touch", "/tmp/nope"),))
            MechanicalGates(root, root/"control", root/"repo", FakeIsolationBackend(), runner).run(phase, 1)
            self.assertFalse(calls[0][1].get("shell", False))
            self.assertEqual(calls[0][0][0], "true;touch")

    def test_symlink_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); outside=root.parent/"outside"
            with self.assertRaises(ValueError): ensure_safe_relative(root, "../outside")
            (root/"link").symlink_to(outside)
            with self.assertRaises(ValueError): ensure_safe_relative(root, "link")
