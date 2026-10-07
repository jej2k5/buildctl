"""The manual's build.yaml example must be a configuration buildctl accepts, and its command
reference must name every command."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from buildctl.buildctl import parser
from buildctl.supervisor.phases import phases_from, validate_config


DOCS = Path(__file__).resolve().parents[1] / "docs"


def example() -> dict:
    match = re.search(r"\{#example\}.*?```json\n(.*?)\n```", (DOCS / "03-roadmap.md").read_text(), re.S)
    if not match:
        raise AssertionError("the manual has no build.yaml example")
    return json.loads(match.group(1))


class ManualExampleTests(unittest.TestCase):
    def test_the_example_validates(self):
        config = example()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for phase in config["phases"]:
                (root / phase["prompt"]).parent.mkdir(parents=True, exist_ok=True)
                (root / phase["prompt"]).write_text("phase")
            for path in config["governance_documents"]:
                (root / path).parent.mkdir(parents=True, exist_ok=True); (root / path).write_text("doc")
            validate_config(config, root)
            self.assertEqual([phase.number for phase in phases_from(config, root)], [0, 1])

    def test_every_command_is_documented(self):
        text = (DOCS / "08-reference.md").read_text()
        for command in parser()._subparsers._group_actions[0].choices:
            self.assertTrue(f"| `{command}" in text, f"command {command} is missing from the manual's reference")


if __name__ == "__main__":
    unittest.main()
