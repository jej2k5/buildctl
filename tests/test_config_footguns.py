"""Config mistakes that `validate` used to pass and a launched build then died on (kernel seam 3).

wyrit hit three in a row before its first phase, each costing a re-trust: missing recording flags
(already refused), a review prompt whose protocol line lacked backticks (an override is now
appended to the kernel prompt, so it needs no marker), and `reviewer.network: false`, which died
ENOTFOUND reaching the reviewer's API.
"""
import copy
import json
import unittest
from pathlib import Path

from buildctl.supervisor.phases import ConfigurationError, load_config_text

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "target"


def config() -> dict:
    return json.loads((FIXTURE / "build.yaml").read_text())


class HostCliNetworkTests(unittest.TestCase):
    def test_a_host_cli_role_without_network_is_refused(self):
        for role in ("reviewer", "implementer"):
            broken = copy.deepcopy(config())
            broken["roles"][role]["network"] = False
            with self.subTest(role=role), self.assertRaisesRegex(ConfigurationError, "needs network true"):
                load_config_text(json.dumps(broken), FIXTURE)

    def test_an_environment_role_with_api_credentials_needs_network_too(self):
        broken = copy.deepcopy(config())
        broken["roles"]["reviewer"].update(auth_mode="environment", credential_env=["ANTHROPIC_API_KEY"], network=False)
        broken["roles"]["reviewer"]["command"] = ["claude", "-p", "{prompt}", "--no-session-persistence",
                                                  "--tools", "Read,Grep,Glob", "--output-format", "stream-json", "--verbose"]
        with self.assertRaisesRegex(ConfigurationError, "needs network true"):
            load_config_text(json.dumps(broken), FIXTURE)

    def test_a_role_without_a_login_may_run_offline(self):
        loaded = load_config_text(json.dumps(config()), FIXTURE)
        self.assertFalse(loaded["roles"]["arbiter"]["network"])


if __name__ == "__main__":
    unittest.main()
