"""Loopback isolation for demo recordings, and the build.yaml `demo` section.

A demo listens on loopback for a recorder and reaches nothing: not the host's other loopback
services, not the internet. Every agent role keeps the profile it had before the mode existed.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buildctl.supervisor.isolation import (LOOPBACK_LISTEN_RULES, IsolationContext, LinuxBubblewrapBackend,
                                           MacOSSandboxBackend)
from buildctl.supervisor.phases import ConfigurationError, validate_demo

FIXTURE = Path(__file__).parent / "fixtures" / "target"


class MacProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for name in ("ws", "control", "repo"):
            (self.root / name).mkdir()

    def profile(self, **flags):
        context = IsolationContext("demo", self.root / "ws", self.root / "control", self.root / "repo", True, **flags)
        with mock.patch.object(MacOSSandboxBackend, "available", return_value=True):
            return MacOSSandboxBackend().command(["/usr/bin/true"], context)[2]

    def test_agent_roles_keep_their_profile(self):
        plain = self.profile()
        self.assertNotIn("network", plain)
        self.assertEqual(plain, self.profile(loopback=False))

    def test_loopback_allows_listening_on_localhost_and_nothing_outbound(self):
        profile = self.profile(loopback=True)
        for rule in LOOPBACK_LISTEN_RULES:
            self.assertIn(rule, profile)
        self.assertNotIn("network-outbound", profile)
        self.assertEqual(profile, self.profile() + " " + " ".join(LOOPBACK_LISTEN_RULES))

    def test_a_role_with_network_does_not_also_get_loopback_rules(self):
        profile = self.profile(network_allowed=True, loopback=True)
        self.assertIn("(allow network-outbound)", profile)
        self.assertNotIn("network-bind", profile)

    def test_the_canonical_context_keeps_the_mode(self):
        context = IsolationContext("demo", self.root / "ws", self.root / "control", self.root / "repo", loopback=True)
        self.assertTrue(context.canonical().loopback)


class LinuxLoopbackTests(unittest.TestCase):
    def test_loopback_is_the_private_namespace_with_the_offline_filter(self):
        # No special arguments: --unshare-net already gives the sandbox a loopback no one outside reaches.
        backend = LinuxBubblewrapBackend.__new__(LinuxBubblewrapBackend)
        backend.executable = Path("/usr/bin/bwrap")
        self.assertIn("--unshare-net", backend._base(network_allowed=False))


class DemoSectionTests(unittest.TestCase):
    def config(self, **demo):
        value = {"command": ["{python}", "-m", "app", "--port", "{port}"]}
        value.update(demo)
        return {"demo": value}

    def refused(self, **demo):
        with self.assertRaises(ConfigurationError) as caught:
            validate_demo(self.config(**demo), FIXTURE)
        return str(caught.exception)

    def test_no_section_is_fine(self):
        validate_demo({}, FIXTURE)

    def test_a_complete_section_is_accepted(self):
        validate_demo(self.config(url="http://127.0.0.1:{port}/#/plan", ready_pattern=r"(http://\S+)",
                                  scenario="docs/demo/console.json", timeout_seconds=60, viewport=[1440, 900]), FIXTURE)

    def test_the_command_must_take_its_port(self):
        self.assertIn("{port}", self.refused(command=["{python}", "-m", "app"]))

    def test_the_url_must_be_loopback(self):
        self.assertIn("demo url", self.refused(url="https://example.com/"))
        self.assertIn("demo url", self.refused(url="http://127.0.0.1:8080/"))

    def test_the_ready_pattern_needs_a_group(self):
        self.assertIn("one group", self.refused(ready_pattern="http://\\S+"))
        self.assertIn("valid regex", self.refused(ready_pattern="(unclosed"))

    def test_the_scenario_stays_in_the_repository(self):
        self.refused(scenario="../outside.json")

    def test_unknown_keys_and_bad_numbers_are_refused(self):
        self.assertIn("may set only", self.refused(env={"TOKEN": "x"}))
        self.refused(timeout_seconds=5)
        self.refused(timeout_seconds=True)
        self.refused(viewport=[100, 100])

    def test_every_live_shape_of_build_yaml_without_demo_still_loads(self):
        from buildctl.supervisor.phases import load_config
        load_config(FIXTURE / "build.yaml")


if __name__ == "__main__":
    unittest.main()
