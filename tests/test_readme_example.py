"""The configuration example in README.md must be a configuration buildctl accepts."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from buildctl.buildctl import parser
from buildctl.supervisor.phases import phases_from, validate_config


README = Path(__file__).resolve().parents[1] / "README.md"


def example() -> dict:
    match = re.search(r"<!-- example:build.yaml -->\n```json\n(.*?)\n```", README.read_text(), re.S)
    if not match:
        raise AssertionError("README has no marked build.yaml example")
    return json.loads(match.group(1))


class ReadmeExampleTests(unittest.TestCase):
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
        text = README.read_text()
        for command in parser()._subparsers._group_actions[0].choices:
            self.assertTrue(f"| `{command}" in text, f"command {command} is missing from the README table")


if __name__ == "__main__":
    unittest.main()
