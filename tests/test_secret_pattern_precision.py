"""Regression tests for the generic secret-pattern detector.

Phase 1 aborted with `IMPLEMENTER_SECRET_OUTPUT: credential_kind=secret_pattern` — no staged
value matched. The old pattern `(api[_-]?key|token|password|secret)\\s*[:=]\\s*[^\\s]+` flagged
any credential-ish word followed by anything, so an agent writing authentication code killed
its own phase by printing `password = getpass()`.

The detector now identifies plausible credential *values*. Exact staged-secret matching stays
authoritative and strict; this heuristic is the second line, and it errs towards silence.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import AgentError, RoleRunner, classify_secret_exposure
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from buildctl.supervisor.safety import (contains_secret, looks_like_secret_value,
                                     matches_secret_pattern, redact)
from tests.credential_shapes import (github_token, high_entropy, jwt_like,
                                     openai_style_key, private_key_header)

# Ordinary output from an agent that writes authentication code. None may abort a phase.
BENIGN = (
    "password = getpass()",
    "secret: none",
    "token = response.token",
    "assert response.token == expected",
    'API_KEY = os.getenv("API_KEY")',
    "export ANTHROPIC_API_KEY=...",
    "token: redacted",
    "password: <redacted>",
    "SECRET_KEY = settings.SECRET_KEY",
    "tokens used: 12345",
    "total tokens: 4096",
    "def make_token(): ...",
    "Authorization: Bearer <token>",
    "thinking about the token budget",
    "refresh_token handling in auth.py",
    'password = input("pw: ")',
    "api_key = config.api_key",
    'secret = os.environ["SECRET"]',
    "token=${GITHUB_TOKEN}",
    "password: changeme",
    "client_secret = None",
    "auth_token: TODO",
    "api-key: placeholder",
    "token = api_response_data_v2",
    "secret = myConfiguredValue",
    "password = configuration_value",
    "access_token = self.session.access_token",
    "password = hashlib.sha256(pw).hexdigest()",
    "api_key: %API_KEY%",
    "secret = ***",
)

# Values that plausibly carry a credential. Every one must still fail closed.
SECRET_LIKE = (
    (f"api_key={openai_style_key()}", "openai-style key"),
    (f"token={jwt_like()}", "jwt"),
    (f"password={high_entropy('Tr0ub4dor', '&3xK9qLmZq')}", "strong password"),
    (f"secret={high_entropy('9f8a7b6c5d4e', '3f2a1b0c9d8e7f6a5b4c')}", "opaque hex"),
    (f"client_secret: {high_entropy('aB3dE6gH9jK2', 'mN5pQ8sT1vW4xY7zC0eF')}", "client secret"),
    (github_token(), "github token"),
    (private_key_header(), "private key header"),
    (f"refresh_token = {high_entropy('1//0eXaMpLe-LoNg', '-OpAqUe-VaLuE-9876543210')}", "oauth refresh token"),
)


class BenignDeveloperOutputTests(unittest.TestCase):
    def test_no_benign_line_is_flagged(self):
        flagged = [line for line in BENIGN if matches_secret_pattern(line)]
        self.assertEqual(flagged, [], "these would abort a healthy phase")

    def test_a_whole_scaffolding_transcript_is_clean(self):
        transcript = "\n".join(BENIGN)
        self.assertFalse(matches_secret_pattern(transcript))
        self.assertFalse(contains_secret(transcript))

    def test_benign_lines_survive_redaction_unchanged(self):
        for line in BENIGN:
            self.assertEqual(redact(line, limit=500), line,
                             f"redaction must not mangle ordinary source: {line!r}")


class SecretLikeValueTests(unittest.TestCase):
    def test_every_credential_shaped_value_still_fails_closed(self):
        missed = [label for text, label in SECRET_LIKE if not matches_secret_pattern(text)]
        self.assertEqual(missed, [])

    def test_credential_values_are_redacted_from_diagnostics(self):
        for text, label in SECRET_LIKE:
            redacted = redact(text, limit=500)
            self.assertIn("[REDACTED]", redacted, f"{label} was not redacted")
            if "=" in text or ":" in text:
                secret_value = text.split("=", 1)[-1].split(":", 1)[-1].strip()
                if len(secret_value) > 20:
                    self.assertNotIn(secret_value, redacted)

    def test_value_heuristic_rejects_references_and_placeholders(self):
        for value in ("getpass()", "none", "response.token", "<redacted>", "${VAR}",
                      "...", "os.environ", "changeme", "short", "configuration_value"):
            self.assertFalse(looks_like_secret_value(value), value)

    def test_value_heuristic_accepts_opaque_material(self):
        for value in (openai_style_key(),
                      "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c",
                      "Tr0ub4dor&3xK9qLmZq",
                      "aB3dE6gH9jK2mN5pQ8sT1vW4xY7zC0eF"):
            self.assertTrue(looks_like_secret_value(value), value)


class ExactStagedDetectionRemainsAuthoritativeTests(unittest.TestCase):
    def test_an_exact_staged_value_is_caught_even_though_it_looks_benign(self):
        """Exact matching does not consult the heuristic at all."""
        staged = "configuration_value"          # would be rejected by the generic heuristic
        self.assertFalse(matches_secret_pattern(f"echo {staged}"))
        self.assertTrue(contains_secret(f"echo {staged}", [staged]))
        self.assertEqual(classify_secret_exposure(f"echo {staged}", {"access_token": staged}),
                         "access_token")

    def test_exact_match_is_reported_by_kind_not_as_a_pattern(self):
        token = "access-" + "A" * 1600
        labelled = {"access_token": token}
        self.assertEqual(classify_secret_exposure(f"oops {token}", labelled), "access_token")


class PhaseOneEndToEndTests(unittest.TestCase):
    """The reproduction: auth scaffolding output must not abort the implementer."""

    SCAFFOLDING = "\n".join((
        "Creating src/auth/session.py",
        "  password = getpass()",
        '  API_KEY = os.getenv("API_KEY")',
        "  token = response.token",
        "  assert response.token == expected",
        "Creating tests/product/test_auth.py",
        "  secret: none",
        "Done. tokens used: 12345",
    ))

    def _run(self, stdout, result_payload=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, control, repo = root / "ws", root / "ctl", root / "repo"
            for path in (workspace, control, repo):
                path.mkdir(parents=True)
            config = {"roles": {"implementer": {"auth_mode": "none", "credential_env": [],
                                                "network": False,
                                                "command": ["/bin/echo", "--out", "{output}"]}},
                      "_trusted_executables": {}}

            def runner(argv, **kwargs):
                out = Path(argv[argv.index("--out") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result_payload or {"status": "complete"}))
                return subprocess.CompletedProcess(argv, 0, stdout, "")

            context = IsolationContext("implementer", workspace, control, repo, host_home=root)
            return RoleRunner(config, FakeIsolationBackend(), context, runner).run(
                "implementer", "prompt", control / "PHASE_RESULT.json", 30)

    def test_auth_scaffolding_output_does_not_abort_the_phase(self):
        self.assertEqual(self._run(self.SCAFFOLDING), {"status": "complete"})

    def test_the_same_output_carrying_a_real_key_still_aborts(self):
        leaking = self.SCAFFOLDING + f"\n  api_key={openai_style_key()}\n"
        with self.assertRaises(AgentError) as caught:
            self._run(leaking)
        message = str(caught.exception)
        self.assertIn("IMPLEMENTER_SECRET_OUTPUT", message)
        self.assertIn("credential_kind=secret_pattern", message)
        self.assertNotIn("sk-proj", message, "the diagnostic must not carry the value")

    def test_a_credential_in_the_result_file_still_aborts(self):
        with self.assertRaises(AgentError) as caught:
            self._run("clean", result_payload={"status": "complete",
                                               "note": f"token={jwt_like()}"})
        self.assertIn("source=result_file", str(caught.exception))


class GitContentScanTests(unittest.TestCase):
    def test_committed_source_with_credential_words_is_not_blocked(self):
        """attributed_delta scans changed files with the same detector."""
        source = ("import os\n\n"
                  "def login(password = None):\n"
                  "    API_KEY = os.getenv('API_KEY')\n"
                  "    token = response.token\n"
                  "    return {'secret': None}\n")
        self.assertFalse(matches_secret_pattern(source))

    def test_committed_source_with_a_hardcoded_key_is_blocked(self):
        source = f"API_KEY = '{openai_style_key()}'\n"
        self.assertTrue(matches_secret_pattern(source))
