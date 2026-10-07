from __future__ import annotations

import json
import unittest

from buildctl_discover.check import validate
from buildctl_discover.render import DECISIONS, config, render

from .helpers import RELAY, relay_config, relay_session


class RelayRoundTrip(unittest.TestCase):
    """If the record cannot express Relay's real roadmap, the format is too weak."""

    def setUp(self):
        self.temporary, self.session, self.ids = relay_session()
        self.addCleanup(self.temporary.cleanup)
        self.state = self.session.state()

    def test_rendered_phases_equal_the_fixture(self):
        self.assertEqual(config(self.state)["phases"], relay_config()["phases"])

    def test_milestones_and_checkpoints_equal_the_fixture(self):
        rendered, fixture = config(self.state), relay_config()
        self.assertEqual(rendered["milestones"], fixture["milestones"])
        self.assertEqual(rendered["checkpoints"], fixture["checkpoints"])

    def test_the_rendered_build_yaml_passes_buildctl_validate(self):
        self.assertIsNone(validate(RELAY, render(self.state)))

    def test_decisions_are_governed_and_given_to_the_reviewer(self):
        rendered = config(self.state)
        self.assertIn(DECISIONS, rendered["governance_documents"])
        self.assertIn(DECISIONS, rendered["roles"]["reviewer"]["context_documents"])

    def test_the_base_outside_the_roadmap_is_kept(self):
        rendered, fixture = config(self.state), relay_config()
        for key in ("limits", "operation_cost_usd", "git", "phase_defaults", "version"):
            self.assertEqual(rendered[key], fixture[key])

    def test_render_is_byte_identical_across_calls(self):
        self.assertEqual(render(self.state), render(self.session.state()))

    def test_phase_zero_keeps_its_narrow_paths_and_gate(self):
        phase = config(self.state)["phases"][0]["acceptance"]
        self.assertEqual(phase["allowed_paths"], ["docs/constitution/", "docs/HANDOFF_PHASE_0.md"])
        self.assertIn("phase0_gate", phase["commands"][0][2])

    def test_rendered_prose_has_no_em_dashes(self):
        for path, content in render(self.state).items():
            if path != "build.yaml":
                self.assertNotIn("—", content.decode(), path)


if __name__ == "__main__":
    unittest.main()
