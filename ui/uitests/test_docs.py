"""The manual: generated from its Markdown, and complete against the kernel.

    PYTHONPATH=ui python3 -m unittest discover -s ui/uitests -t ui
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from buildctl.buildctl import parser
from buildctl.supervisor.inspect import ESCALATIONS

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "ui" / "buildctl_ui" / "web" / "docs" / "index.html"
EXAMPLE = ROOT / "tests" / "fixtures" / "target" / "build.yaml"


def ids() -> set[str]:
    return set(re.findall(r'\sid="([^"]+)"', PAGE.read_text()))


def kernel_codes() -> set[str]:
    """Every escalation code the kernel can raise: the explained ones and every ceiling."""
    source = "\n".join(path.read_text() for path in (ROOT / "buildctl").rglob("*.py"))
    return set(ESCALATIONS) | set(re.findall(r'"([A-Z_]+_EXHAUSTED)"', source))


def detail_codes() -> set[str]:
    """Every detail code an agent call can fail with, without its `{role}_` or `{kind}_` prefix."""
    source = "\n".join(path.read_text() for path in (ROOT / "buildctl").rglob("*.py"))
    found = re.findall(r'AgentError\(f?"((?:\{[^}]+\}_)?[A-Z_]+)', source)
    # The interactive debate is not a failure: it has its own escalation code.
    return {re.sub(r"^\{[^}]+\}_", "", code) for code in found} - {"INTERACTIVE_DEBATE_REQUIRES_HUMAN"}


def commands() -> set[str]:
    names = set()
    for action in parser()._subparsers._group_actions:
        for name, sub in action.choices.items():
            nested = [a for a in sub._actions if a.__class__.__name__ == "_SubParsersAction"]
            names |= {f"{name}-{child}" for a in nested for child in a.choices} if nested else {name}
    return names


class ManualTests(unittest.TestCase):
    def test_the_page_matches_its_markdown(self):
        result = subprocess.run([sys.executable, str(ROOT / "docs" / "build.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_escalation_code_has_a_section(self):
        missing = sorted(kernel_codes() - ids())
        self.assertEqual(missing, [], "escalation codes with no section in the manual")

    def test_every_agent_detail_code_has_a_row(self):
        codes = detail_codes()
        self.assertIn("EXECUTABLE_INTEGRITY_FAILURE", codes)  # the scan still sees templated codes
        missing = sorted(code for code in codes if f"detail-{code.lower()}" not in ids())
        self.assertEqual(missing, [], "agent detail codes with no row under AGENT_OPERATION_FAILED")

    def test_every_command_has_a_reference_row(self):
        missing = sorted(f"cmd-{name}" for name in commands() if f"cmd-{name}" not in ids())
        self.assertEqual(missing, [], "commands with no row in the reference")

    def test_every_configuration_key_has_a_reference_row(self):
        import json
        config = json.loads(EXAMPLE.read_text())
        keys = {key for key in config if key not in ("version", "roles", "phase_defaults", "phases", "git",
                                                     "limits", "checkpoints")}
        keys |= {f"limits.{k}" for k in config["limits"]} | {f"git.{k}" for k in config["git"]}
        keys |= {f"checkpoints.{k}" for k in config["checkpoints"]}
        keys |= {f"phase_defaults.{k}" for k in config["phase_defaults"]}
        keys |= {"git.integration_branch", "phases", "phases[].acceptance"}
        for role, values in config["roles"].items():
            for key in values:
                if key in ("command", "network", "auth_mode", "credential_env"):
                    keys.add(f"roles.*.{key}")
                elif key in ("vendor", "model_family", "prompt_lineage") and role != "reviewer":
                    continue  # only the reviewer's identity is checked, so only it is documented
                else:
                    keys.add(f"roles.{role}.{key}")
        expected = {"config-" + re.sub(r"[^a-z0-9_]+", "-", key.lower()).strip("-") for key in keys}
        self.assertEqual(sorted(expected - ids()), [], "configuration keys with no row in the reference")

    def test_the_page_reads_cleanly_to_a_screen_reader(self):
        page = PAGE.read_text()
        every_id = re.findall(r'\sid="([^"]+)"', page)
        self.assertEqual(sorted({i for i in every_id if every_id.count(i) > 1}), [], "duplicate ids")
        permalinks = re.findall(r'class="permalink"[^>]*aria-label="([^"]+)"', page)
        self.assertEqual(len(permalinks), len(set(permalinks)), "permalinks a screen reader cannot tell apart")
        # The stop index is navigation: its group labels are not headings, its shapes are not read out
        index = re.search(r'<nav class="stop-index".*?</nav>', page, re.S).group(0)
        self.assertNotRegex(index, r"<h\d")
        self.assertNotIn('role="img"', index)
        self.assertNotRegex(page, r'<span class="num">\d+</span>(?!<span class="vh">)')
        self.assertIn('"location"', (PAGE.parent / "docs.js").read_text())

    def test_linux_refusals_point_at_a_fix_that_ci_proves(self):
        from buildctl.supervisor.isolation import UNSUPPORTED
        self.assertIn(UNSUPPORTED, ids())
        manual = (ROOT / "docs" / "01-start.md").read_text()
        block = re.search(r"```sh\n(sudo apt-get install -y bubblewrap.*?)```", manual, re.S).group(1)
        fix = [line for line in block.splitlines() if not line.startswith("sudo apt-get")]
        workflow = (ROOT / ".github" / "workflows" / "linux.yml").read_text()
        for line in fix:
            self.assertIn(line, workflow, "the manual's Linux fix is not the one CI runs")

    def test_the_console_serves_the_manual(self):
        self.assertTrue(PAGE.is_file())
        for name in ("docs.css", "docs.js", "icon.svg"):
            self.assertTrue((PAGE.parent / name).is_file(), name)
        self.assertIn('href="/docs/', (ROOT / "ui" / "buildctl_ui" / "web" / "app.js").read_text())


if __name__ == "__main__":
    unittest.main()
