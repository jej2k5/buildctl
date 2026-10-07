"""Regression tests for the Phase 0 acceptance gate.

Phase 0 deadlocked because its gate demanded a handoff file the phase could not
produce and ran the factory's own supervisor suite, which by design fails inside a
gate subprocess (BUILDCTL_GATE_PROCESS=1 blocks StateStore.save).
"""
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.gates import MechanicalGates
from buildctl.supervisor.isolation import FakeIsolationBackend, sanitized_environment
from buildctl.supervisor.phases import load_config, phases_from

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
CONSTITUTION = ("README.md", "glossary.md", "adr-conventions.md", "mvp-definition.md",
                "security-and-enterprise.md", "semantic-contract-principles.md",
                "governance-boundaries.md", "deterministic-kernel-hypothesis.md")


def phase_zero():
    return phases_from(load_config(TARGET / "build.yaml"), TARGET)[0]


def populate(workspace: Path) -> None:
    constitution = workspace / "docs" / "constitution"
    constitution.mkdir(parents=True)
    filler = "This document records the the original project project constitution. " * 6
    for name in CONSTITUTION:
        (constitution / name).write_text(f"# {name}\n\n{filler}")
    (workspace / "docs" / "HANDOFF_PHASE_0.md").write_text(
        "# Phase 0 Handoff\n\n## Summary\n" + filler +
        "\n## Tests\nDocumentation gate only.\n## Decisions\nRecorded.\n## Next\nPhase 1.\n")


def run_gate(workspace: Path) -> object:
    base = workspace.parent
    control, repo = base / "control", base / "repo"
    control.mkdir(exist_ok=True)
    repo.mkdir(exist_ok=True)
    return MechanicalGates(workspace, control, repo, FakeIsolationBackend()).run(phase_zero(), 60)


class PhaseZeroGateContractTests(unittest.TestCase):
    def test_gate_no_longer_runs_the_factory_supervisor_suite(self):
        commands = [list(command) for command in phase_zero().commands]
        for command in commands:
            self.assertNotIn("unittest", command,
                             "Phase 0 must not run the factory's own suite in a gate subprocess")
        self.assertEqual(commands[0][:2], ["python3", "-c"])

    def test_handoff_and_constitution_are_required_and_permitted(self):
        phase = phase_zero()
        self.assertIn("docs/HANDOFF_PHASE_0.md", phase.required_files)
        for name in CONSTITUTION:
            self.assertIn(f"docs/constitution/{name}", phase.required_files)
        # Every required file must also be writable by the phase, or it can never be produced.
        for required in phase.required_files:
            self.assertTrue(
                any(required == allowed or required.startswith(allowed)
                    for allowed in phase.allowed_paths),
                f"{required} is required but not in allowed_paths")

    def test_phase_zero_prompt_names_the_permitted_paths(self):
        text = phase_zero().prompt.read_text()
        self.assertIn("docs/HANDOFF_PHASE_0.md", text)
        self.assertIn("docs/constitution/", text)

    def test_gate_fails_on_an_empty_workspace_and_passes_when_satisfied(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty" / "ws"
            empty.mkdir(parents=True)
            self.assertFalse(run_gate(empty).passed)
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good" / "ws"
            good.mkdir(parents=True)
            populate(good)
            result = run_gate(good)
            self.assertTrue(result.passed, result.details)

    def test_gate_still_rejects_thin_untitled_and_symlinked_deliverables(self):
        for label, mutate in (
            ("deleted", lambda w: (w / "docs/constitution/glossary.md").unlink()),
            ("emptied", lambda w: (w / "docs/constitution/glossary.md").write_text("")),
            ("no heading", lambda w: (w / "docs/constitution/glossary.md").write_text("x " * 200)),
            ("bad json", lambda w: (w / "docs/constitution/x.json").write_text("{not json")),
            ("symlinked handoff", lambda w: ((w / "docs/HANDOFF_PHASE_0.md").unlink(),
                                             (w / "docs/HANDOFF_PHASE_0.md").symlink_to("/etc/passwd"))),
        ):
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "ws"
                workspace.mkdir(parents=True)
                populate(workspace)
                mutate(workspace)
                self.assertFalse(run_gate(workspace).passed, f"gate accepted a {label} deliverable")

    def test_gate_protections_are_unchanged(self):
        self.assertEqual(sanitized_environment("gate")["BUILDCTL_GATE_PROCESS"], "1")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", sanitized_environment("gate"))

    def test_default_phases_target_the_product_test_tree(self):
        """Product phases run under the provisioned interpreter, never a bare host python3:
        the gate sandbox has no network, so declared dependencies must be installed first."""
        defaults = load_config(TARGET / "build.yaml")["phase_defaults"]["commands"]
        self.assertEqual(defaults, [["{python}", "-m", "unittest", "discover", "-s", "tests/product"]])
