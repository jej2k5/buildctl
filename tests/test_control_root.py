"""Where canonical state lives."""
import os
import unittest
from pathlib import Path
from unittest import mock

from buildctl.buildctl import control_root_for


class ControlRootTests(unittest.TestCase):
    def environment(self, **values):
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("BUILDCTL_STATE_DIR", "XDG_STATE_HOME")}
        clean.update(values)
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_the_override_is_honoured(self):
        with self.environment(BUILDCTL_STATE_DIR="/tmp/buildctl-state"):
            self.assertEqual(control_root_for(Path("/repo")), Path("/tmp/buildctl-state").resolve())

    def test_the_default_is_keyed_by_repository_under_a_buildctl_directory(self):
        with self.environment(XDG_STATE_HOME="/state"):
            first, second = control_root_for(Path("/repo-a")), control_root_for(Path("/repo-b"))
        self.assertEqual(first.parent, Path("/state/buildctl"))
        self.assertNotEqual(first, second)

    def test_the_override_wins_over_xdg(self):
        with self.environment(BUILDCTL_STATE_DIR="/new", XDG_STATE_HOME="/state"):
            self.assertEqual(control_root_for(Path("/repo")), Path("/new").resolve())


if __name__ == "__main__":
    unittest.main()
