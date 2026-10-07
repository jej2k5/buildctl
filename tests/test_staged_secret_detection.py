"""Regression tests for staged Codex credential leak detection.

Phase 1 ran for five minutes and then aborted with a bare IMPLEMENTER_SECRET_OUTPUT.
`account_id` — a 36-character account identifier, not credential material — was being
treated as a secret, so any provider output legitimately echoing it would abort the phase.
It stays in the staged auth.json because Codex may need it; it is simply not a credential
for leak-detection purposes. Real tokens must still fail closed.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.agents import AgentError, RoleRunner, classify_secret_exposure
from buildctl.supervisor.auth import (CREDENTIAL_BEARING_TOKENS, cleanup_staged_auth,
                                   stage_codex_auth)
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from tests.credential_shapes import openai_style_key, staged_api_key

ACCESS = "access-" + "A" * 1600
REFRESH = "refresh-" + "R" * 200
IDTOK = "idtoken-" + "I" * 1700
ACCOUNT = "3f2b1c88-9a4e-4d21-8f60-77c0aa19b3de"
APIKEY = staged_api_key()


def host_fixture(root: Path, with_api_key=False) -> Path:
    home = root / "hosthome"
    codex = home / ".codex"
    codex.mkdir(parents=True, mode=0o700)
    payload = {"auth_mode": "chatgpt",
               "tokens": {"access_token": ACCESS, "refresh_token": REFRESH,
                          "id_token": IDTOK, "account_id": ACCOUNT}}
    if with_api_key:
        payload["OPENAI_API_KEY"] = APIKEY
    (codex / "auth.json").write_text(json.dumps(payload))
    os.chmod(codex / "auth.json", 0o600)
    return home


def implementer_config():
    return {"roles": {"implementer": {"auth_mode": "host_cli", "credential_env": [], "network": False,
                                      "command": ["/bin/echo", "--role", "implementer", "--out", "{output}"]}},
            "_trusted_executables": {}}


def run_with_output(root: Path, stdout: str, result_payload=None, with_api_key=False):
    """Drive the real RoleRunner with a provider that emits `stdout`."""
    host = host_fixture(root, with_api_key)
    workspace, control, repo = root / "ws", root / "ctl", root / "repo"
    for path in (workspace, control, repo):
        path.mkdir(parents=True, exist_ok=True)

    def runner(argv, **kwargs):
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result_payload if result_payload is not None
                                  else {"status": "complete"}))
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    context = IsolationContext("implementer", workspace, control, repo, host_home=host)
    return RoleRunner(implementer_config(), FakeIsolationBackend(), context, runner).run(
        "implementer", "prompt", control / "PHASE_RESULT.json", 30)


class CredentialLeakStillFailsClosedTests(unittest.TestCase):
    def _expect_leak(self, stdout, kind, with_api_key=False, result_payload=None):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AgentError) as caught:
                run_with_output(Path(directory).resolve(), stdout,
                                result_payload=result_payload, with_api_key=with_api_key)
            message = str(caught.exception)
            self.assertTrue(message.startswith("IMPLEMENTER_SECRET_OUTPUT"), message)
            self.assertIn(f"credential_kind={kind}", message)
            for value in (ACCESS, REFRESH, IDTOK, APIKEY, ACCOUNT):
                self.assertNotIn(value, message, "the diagnostic must never carry a value")

    def test_access_token_echoed_to_stdout_fails_closed(self):
        self._expect_leak(f"working... {ACCESS} done", "access_token")

    def test_refresh_token_echoed_to_stdout_fails_closed(self):
        self._expect_leak(f"working... {REFRESH} done", "refresh_token")

    def test_id_token_echoed_to_stdout_fails_closed(self):
        self._expect_leak(f"working... {IDTOK} done", "id_token")

    def test_openai_api_key_echoed_to_stdout_fails_closed(self):
        self._expect_leak(f"working... {APIKEY} done", "api_key", with_api_key=True)

    def test_token_in_the_result_file_still_fails_closed(self):
        self._expect_leak("clean output", "access_token",
                          result_payload={"status": "complete", "note": ACCESS})

    def test_result_file_leak_is_reported_with_its_own_source(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AgentError) as caught:
                run_with_output(Path(directory).resolve(), "clean output",
                                result_payload={"status": "complete", "note": IDTOK})
            self.assertIn("source=result_file", str(caught.exception))
            self.assertIn("credential_kind=id_token", str(caught.exception))


class AccountIdentifierIsNotCredentialMaterialTests(unittest.TestCase):
    def test_account_id_echoed_to_stdout_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_with_output(Path(directory).resolve(),
                                     f"codex session for account {ACCOUNT}\n")
            self.assertEqual(result, {"status": "complete"},
                             "an account identifier in provider output must not abort the phase")

    def test_account_id_is_excluded_from_the_detection_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, secrets = stage_codex_auth(root / "iso", host_fixture(root))
            self.assertNotIn("account_id", secrets)
            self.assertEqual(set(secrets), set(CREDENTIAL_BEARING_TOKENS))

    def test_account_id_remains_present_in_the_staged_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target, _ = stage_codex_auth(root / "iso", host_fixture(root))
            staged = json.loads(target.read_text())
            self.assertEqual(staged["tokens"]["account_id"], ACCOUNT,
                             "Codex may require account_id; only leak detection changed")
            self.assertEqual(set(staged["tokens"]),
                             {"access_token", "refresh_token", "id_token", "account_id"})

    def test_api_key_is_still_collected_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, secrets = stage_codex_auth(root / "iso", host_fixture(root, with_api_key=True))
            self.assertIn("api_key", secrets)


class ExposureClassificationTests(unittest.TestCase):
    def test_classification_names_the_kind_and_never_the_value(self):
        labelled = {"access_token": ACCESS, "id_token": IDTOK}
        self.assertEqual(classify_secret_exposure(f"x {ACCESS} y", labelled), "access_token")
        self.assertEqual(classify_secret_exposure(f"x {IDTOK} y", labelled), "id_token")
        self.assertIsNone(classify_secret_exposure("nothing sensitive here", labelled))

    def test_generic_pattern_match_is_distinguished_from_a_real_credential(self):
        """An unknown-but-credential-shaped value reports as secret_pattern; a staged value
        reports by its kind; ordinary source code reports as nothing at all."""
        labelled = {"access_token": ACCESS}
        self.assertEqual(
            classify_secret_exposure(f"api_key={openai_style_key()}", labelled),
            "secret_pattern")
        self.assertEqual(classify_secret_exposure(f"{ACCESS}", labelled), "access_token")
        self.assertIsNone(classify_secret_exposure("password = getpass()", labelled),
                          "ordinary developer output must not be an exposure")

    def test_account_identifier_alone_is_not_an_exposure(self):
        labelled = {"access_token": ACCESS, "refresh_token": REFRESH, "id_token": IDTOK}
        self.assertIsNone(classify_secret_exposure(f"account {ACCOUNT} ready", labelled))


class StagedAuthLifecycleUnchangedTests(unittest.TestCase):
    def test_staging_scope_permissions_and_cleanup_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "iso"
            home.mkdir(mode=0o700)
            target, _ = stage_codex_auth(home, host_fixture(root))
            tree = [p.relative_to(home).as_posix() for p in sorted(home.rglob("*"))]
            self.assertEqual(tree, [".codex", ".codex/auth.json"])
            self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o600")
            self.assertEqual(oct(os.stat(home / ".codex").st_mode & 0o777), "0o700")
            cleanup_staged_auth(home)
            self.assertFalse((home / ".codex").exists())

    def test_cleanup_runs_even_when_the_provider_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(AgentError):
                run_with_output(root, f"leak {ACCESS}")
            self.assertFalse(any((root / "ws").rglob("auth.json")),
                             "staged credentials must be removed even on a failed operation")
