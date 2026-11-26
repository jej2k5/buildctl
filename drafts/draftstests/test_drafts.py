"""buildctl-drafts: drafts are checked against their sources, kept in the state folder, never published.

    PYTHONPATH=.:drafts python3 -m unittest discover -s drafts/draftstests -t drafts
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buildctl_drafts import checks, context as context_module, make as make_module
from buildctl_drafts.__main__ import main
from buildctl_drafts.checks import parse_voice
from buildctl_drafts.store import DraftStore
from buildctl_drafts.writer import build_prompt, writer_config

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "target"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Repo(unittest.TestCase):
    """A repository with two releases, as a trusted build would have."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.repo, self.control = base / "repo", base / "state"
        self.repo.mkdir(); self.control.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@test.invalid"); git(self.repo, "config", "user.name", "T")
        (self.repo / "README.md").write_text("# wordtally\n\nCount words.\n\n## Usage\n\n`--min N` filters.\n")
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n\n- First release.\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "First release")
        git(self.repo, "tag", "v0.1.0")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "cli.py").write_text("def main():\n    parser.add_argument('--top')\n")
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0\n\n- Added `--top N`.\n\n## 0.1.0\n\n- First release.\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "VOICE.md").write_text("# Voice\n\nPlain.\n\n## Rules\n\n- Banned: synergy, leverage\n"
                                                     "- No em dashes\n- Max words for launch-post: 40\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "Add --top")
        self.head = git(self.repo, "rev-parse", "HEAD")
        config = json.loads((FIXTURE / "build.yaml").read_text())
        config["limits"]["max_daily_cost_usd"] = 20
        trusted = mock.patch.object(context_module, "trusted", return_value=(config, {"executables": {}}))
        trusted.start(); self.addCleanup(trusted.stop)
        trusted_make = mock.patch.object(make_module, "trusted", return_value=(config, {"executables": {}}))
        trusted_make.start(); self.addCleanup(trusted_make.stop)
        voice = mock.patch.object(context_module, "OPERATOR_VOICE", base / "no-voice.md")
        voice.start(); self.addCleanup(voice.stop)

    def context(self):
        return context_module.gather(self.repo, self.control, since="v0.1.0")


def draft(*claims, body="Wordtally now shows the top N words.", fields=None):
    return {"title": "wordtally 0.2.0", "body": body, "known_limits": [], "fields": fields or {},
            "claims": [{"text": text, "source": source} for text, source in claims]}


class ContextTests(Repo):
    def test_the_range_and_its_inputs_are_read_at_the_release_commit(self):
        context = self.context()
        self.assertEqual([item["subject"] for item in context.commits], ["Add --top"])
        self.assertIn("src/cli.py", context.files)
        self.assertIn("0.2.0", context.changelog)
        self.assertIn("Banned", context.product_voice)
        (self.repo / "README.md").write_text("an uncommitted edit")
        self.assertNotIn("uncommitted", self.context().readme, "the working tree is never read")


class ClaimTests(Repo):
    def check(self, source):
        context = self.context()
        return checks.check_claim({"text": "x", "source": source}, context,
                                  lambda path: context_module.show(self.repo, context.until, path))

    def test_backed_claims(self):
        self.assertIsNone(self.check({"kind": "commit", "ref": self.head[:9]}))
        self.assertIsNone(self.check({"kind": "file", "ref": "src/cli.py", "line": 2, "quote": "add_argument('--top')"}))
        self.assertIsNone(self.check({"kind": "changelog", "ref": "CHANGELOG.md", "quote": "Added `--top N`."}))

    def test_unbacked_claims_say_why(self):
        first = git(self.repo, "rev-parse", "v0.1.0")
        self.assertIn("not in the milestone's range", self.check({"kind": "commit", "ref": first[:9]}))
        self.assertIn("does not exist", self.check({"kind": "file", "ref": "src/server.py"}))
        self.assertIn("no line 9", self.check({"kind": "file", "ref": "src/cli.py", "line": 9, "quote": "def main"}))
        self.assertIn("must quote", self.check({"kind": "file", "ref": "src/cli.py", "line": 1}))
        self.assertIn("not in src/cli.py", self.check({"kind": "file", "ref": "src/cli.py", "quote": "--bottom"}))
        self.assertIn("not in the changelog", self.check({"kind": "changelog", "ref": "CHANGELOG.md", "quote": "Added SSO"}))
        self.assertIn("no open finding", self.check({"kind": "finding", "ref": "made-up"}))


class VoiceTests(unittest.TestCase):
    def test_rules_come_from_the_voice_file(self):
        voice = parse_voice("product", "# Voice\n\n## Rules\n\n- Banned: synergy, \"leverage\"\n- No em dashes\n"
                                       "- Max words: 200\n- Max words for social: 50\n- Prefer short lines.", ["seamless"])
        self.assertIn("synergy", voice.banned); self.assertIn("leverage", voice.banned)
        self.assertIn("seamless", voice.banned); self.assertIn("blazing", voice.banned, "product voice defaults")
        self.assertTrue(voice.no_em_dashes)
        self.assertEqual(voice.max_words, {"*": 200, "social": 50})

    def test_lint(self):
        voice = parse_voice("operator", "## Rules\n- No em dashes\n- Banned: synergy\n- Max words: 5")
        problems = checks.lint("social", draft(body="Real synergy — here, in six words."), voice)
        self.assertEqual(len(problems), 3)
        self.assertEqual(checks.lint("social", draft(body="Short and plain."), voice), [])

    def test_positioning_fields(self):
        voice = parse_voice("product", None)
        long = draft(fields={"description": "x" * 400, "topics": ["Good-Topic", "ok"] + [f"t{i}" for i in range(20)]})
        problems = checks.lint("positioning", long, voice)
        self.assertEqual(len(problems), 3)


class ContractTests(unittest.TestCase):
    def test_a_draft_with_no_claims_is_not_sourced(self):
        self.assertIn("cites no claims", " ".join(checks.validate_contract("release-notes", draft())))


class FakeWriter:
    def __init__(self, *drafts):
        self.drafts, self.prompts = list(drafts), []

    def __call__(self, context, config, workspace, prompt, output, timeout):
        self.prompts.append(prompt)
        assert (workspace / "src" / "cli.py").is_file(), "the writer reads a copy of the release commit"
        return self.drafts.pop(0)


class MakeTests(Repo):
    def make(self, writer, kinds=("release-notes",), **kwargs):
        lines = []
        result = make_module.make(self.repo, self.control, kinds=list(kinds), since="v0.1.0", call=writer,
                                  echo=lines.append, **kwargs)
        return result, lines

    def test_an_unbacked_claim_is_rewritten_once_then_passes(self):
        bad = draft(("Adds --top", {"kind": "commit", "ref": self.head[:9]}),
                    ("Adds SSO", {"kind": "changelog", "ref": "CHANGELOG.md", "quote": "Added SSO"}))
        good = draft(("Adds --top", {"kind": "changelog", "ref": "CHANGELOG.md", "quote": "Added `--top N`."}))
        writer = FakeWriter(bad, good)
        result, lines = self.make(writer)
        item = result["drafts"][0]
        self.assertEqual((item["passed"], item["attempts"], item["unbacked"]), (True, 2, 0))
        self.assertIn("not in the changelog", writer.prompts[1], "the rewrite is told what failed")
        self.assertIn("after a rewrite", lines[0])
        store = DraftStore(self.control)
        self.assertEqual(len(store._ledger()), 2)
        self.assertEqual({entry["operation"] for entry in store._ledger()}, {"release_draft"})
        shown = Path(store.get("release-notes")["markdown"]).read_text()
        self.assertIn("## Claims ledger", shown)
        self.assertIn("not published", shown)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "", "nothing touches the repository")

    def test_a_draft_that_still_fails_is_kept_and_marked(self):
        bad = draft(("Adds SSO", {"kind": "file", "ref": "src/sso.py"}))
        result, lines = self.make(FakeWriter(bad, bad))
        self.assertEqual((result["drafts"][0]["passed"], result["drafts"][0]["unbacked"]), (False, 1))
        self.assertIn("needs you", lines[0])
        self.assertIn("UNBACKED", Path(DraftStore(self.control).get("release-notes")["markdown"]).read_text())

    def test_the_product_voice_rules_apply(self):
        long = draft(("x", {"kind": "commit", "ref": self.head[:9]}), body="word " * 41)
        result, _ = self.make(FakeWriter(long, long), kinds=("launch-post",))
        record = DraftStore(self.control).get("launch-post")
        self.assertIn("the limit is 40", record["checks"]["lint"][0])

    def test_a_launch_failure_stops_the_run_and_books_one_call(self):
        from buildctl.supervisor.agents import AgentError

        def broken(*args):
            raise AgentError("REVIEWER_EXECUTABLE_INTEGRITY_FAILURE")
        with self.assertRaisesRegex(Exception, "changed since trust init"):
            self.make(broken, kinds=("release-notes", "positioning"))
        self.assertEqual(len(DraftStore(self.control)._ledger()), 1)

    def test_malformed_output_is_rewritten(self):
        from buildctl.supervisor.agents import AgentError
        good = draft(("Adds --top", {"kind": "commit", "ref": self.head[:9]}))
        replies = [AgentError("REVIEWER_OUTPUT_INVALID_JSON:no"), good]

        def flaky(*args):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        result, _ = self.make(flaky)
        self.assertEqual((result["drafts"][0]["passed"], result["drafts"][0]["attempts"]), (True, 2))

    def test_social_needs_an_operator_voice(self):
        result, lines = self.make(FakeWriter(), kinds=("social",))
        self.assertEqual(result["drafts"], [])
        self.assertIn("social", result["skipped"])
        self.assertIn("skipped", lines[-1])

    def test_the_daily_limit_is_respected(self):
        with self.assertRaisesRegex(Exception, "past the \\$20.00 daily limit"):
            self.make(FakeWriter(), kinds=list(checks.TYPES)[:4], cost=6.0)
        self.assertEqual(DraftStore(self.control)._ledger(), [], "nothing is booked when refused")


class PromptTests(Repo):
    def test_the_prompt_carries_the_catalog_as_data_and_the_voice(self):
        context = self.context()
        voice = parse_voice("product", context.product_voice)
        text = build_prompt("positioning", context, voice)
        self.assertIn('label="source_catalog"', text)
        self.assertIn('label="product_voice"', text)
        self.assertIn("description", text)
        self.assertNotIn("failed_checks", text)
        self.assertIn('label="failed_checks"', build_prompt("release-notes", context, voice, {"lint": ["x"]}))

    def test_the_writer_is_the_reviewer_reading_plain_output(self):
        config = json.loads((FIXTURE / "build.yaml").read_text())
        configured = writer_config(config, {"executables": {"reviewer": {"path": "/x", "sha256": "y"}}})
        command = configured["roles"]["reviewer"]["command"]
        self.assertNotIn("stream-json", command); self.assertNotIn("--verbose", command)
        self.assertIn("Read,Grep,Glob", command)
        self.assertEqual(configured["_trusted_executables"]["reviewer"]["path"], "/x")
        self.assertIn("stream-json", config["roles"]["reviewer"]["command"], "the build's own config is untouched")


class ApprovalTests(MakeTests):
    def test_approve_and_discard_record_an_event_and_nothing_else(self):
        good = draft(("Adds --top", {"kind": "commit", "ref": self.head[:9]}))
        self.make(FakeWriter(good))
        with mock.patch("buildctl_drafts.__main__.resolve_repository", return_value=self.repo), \
                mock.patch("buildctl_drafts.__main__.control_root_for", return_value=self.control):
            self.assertEqual(main(["approve", "release-notes"]), 0)
            store = DraftStore(self.control)
            self.assertEqual(store.get("release-notes")["state"], "approved")
            self.assertEqual(main(["discard", "release-notes"]), 0)
            self.assertEqual(store.get("release-notes")["state"], "discarded")
            self.assertEqual(main(["show", "nothing-here"]), 2)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()


class RenderTest(unittest.TestCase):
    def test_a_title_or_limits_section_the_writer_repeats_is_printed_once(self):
        from buildctl_drafts.store import render
        value = {"title": "Notes", "body": "# Notes\n\nIt counts words.\n\n## Known limits\n\n- Ties sort by code point.",
                 "claims": [], "known_limits": ["Ties sort by code point."], "fields": {}}
        text = render("release-notes", "Release notes", value, {"unbacked": []}, {"label": "v1", "until": "a" * 40})
        self.assertEqual(text.count("# Notes"), 1)
        self.assertEqual(text.lower().count("known limits"), 1)
        self.assertIn("It counts words.", text)

