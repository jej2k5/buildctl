"""Skills: declared in build.yaml, pinned by trust, copied into each agent call, recorded.

The reviewer gets only skills named for it. Every Claude role runs without the operator's
personal settings, skills, plugins or CLAUDE.md. Codex gets skills in its isolated home.
"""
import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor import skills
from buildctl.supervisor.agents import RoleRunner
from buildctl.supervisor.git import GitManager
from buildctl.supervisor.governance import governance_documents, is_governance_path
from buildctl.supervisor.isolation import FakeIsolationBackend, IsolationContext
from buildctl.supervisor.phases import ConfigurationError, validate_config
from buildctl.supervisor.trust import TrustStore
from tests.helpers import git

SKILL = "---\nname: {name}\ndescription: Use when {name} applies.\n---\nDo the {name} thing.\n"

BASE = {"governance_documents": [], "limits": {"max_phase_attempts": 1, "max_review_format_attempts": 1,
        "max_semantic_review_attempts": 1, "max_debate_rounds": 1, "max_phase_minutes": 1,
        "max_daily_cost_usd": 1, "max_consecutive_failures": 1},
        "operation_cost_usd": {"implementation": 1, "review": 1, "remediation": 1, "debate": 1},
        "git": {"push": False, "create_pr": False, "merge": False},
        "checkpoints": {"before_phases": [0], "mvp_completion": 0},
        "phase_defaults": {"required_files": [], "commands": [["true"]], "invariants": [],
                           "claude_review_required": True, "debate_on_architecture_disagreement": True,
                           "max_remediation_attempts": 1, "merge_policy": "human", "allowed_paths": ["src/"]},
        "roles": {"implementer": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": []},
                  "reviewer": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": [],
                               "protocol_version": "buildctl-review-v2", "context_documents": []},
                  "arbiter": {"command": ["true"], "network": False, "auth_mode": "none", "credential_env": []}},
        "phases": [{"number": 0, "name": "p", "prompt": "prompts/phases/p.md"}]}


def write_skill(root: Path, name: str, folder: str | None = None) -> Path:
    path = root / (folder or f"skills/{name}")
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(SKILL.format(name=name))
    return path


def repo_with(root: Path, config: dict) -> None:
    (root / "prompts/phases").mkdir(parents=True, exist_ok=True)
    (root / "prompts/phases/p.md").write_text("p")
    (root / "build.yaml").write_text(json.dumps(config))


class ValidationTests(unittest.TestCase):
    def config(self, root: Path, **changes) -> dict:
        config = copy.deepcopy(BASE)
        config.update(changes)
        repo_with(root, config)
        return config

    def test_a_declared_skill_with_its_folder_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); write_skill(root, "impeccable")
            config = self.config(root, skills={"impeccable": {}})
            config["roles"]["reviewer"]["command"] = ["claude", "-p", "{prompt}"]
            config["roles"]["reviewer"]["skills"] = ["impeccable"]
            self.assertEqual(skills.validate(config, root), [])

    def test_front_matter_reads_block_scalar_descriptions(self):
        self.assertEqual(skills.frontmatter("---\nname: x\ndescription: >\n  Long text\n  over lines.\nother: 1\n---\nbody"),
                         {"name": "x", "description": "Long text over lines."})
        self.assertEqual(skills.frontmatter('---\nname: "y"\ndescription: |\n  a\n  b\n---\n'),
                         {"name": "y", "description": "a\nb"})

    def test_bad_names_missing_files_and_mismatched_front_matter_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_skill(root, "other", "skills/renamed")
            config = self.config(root, skills={"Bad_Name": {}, "missing": {}, "renamed": {}, "extra": {"source": "x"}})
            problems = " | ".join(skills.validate(config, root))
            self.assertIn("lowercase letters", problems)
            self.assertIn("skills/missing/SKILL.md not found", problems)
            self.assertIn("must say name: renamed", problems)
            self.assertIn("may set only path", problems)

    def test_roles_and_phases_may_name_only_declared_skills_on_supported_harnesses(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); write_skill(root, "impeccable")
            config = self.config(root, skills={"impeccable": {}})
            config["roles"]["implementer"]["skills"] = ["impeccable", "ghost"]
            config["roles"]["arbiter"]["skills"] = ["impeccable"]
            config["phases"][0]["skills"] = {"reviewer": ["impeccable"], "arbiter": ["impeccable"]}
            problems = " | ".join(skills.validate(config, root))
            self.assertIn("roles.implementer.skills names 'ghost'", problems)
            self.assertIn("roles.arbiter cannot take skills", problems)
            self.assertIn("supported for claude and codex", problems)
            self.assertIn("phase 0 skills must map", problems)

    def test_a_reviewer_skill_that_runs_shell_commands_on_load_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = write_skill(root, "loader")
            (folder / "SKILL.md").write_text(SKILL.format(name="loader") + "Context: !`git log -1`\n")
            config = self.config(root, skills={"loader": {}})
            config["roles"]["reviewer"]["command"] = ["claude", "-p", "{prompt}"]
            config["phases"][0]["skills"] = {"reviewer": ["loader"]}
            self.assertIn("runs shell commands when it loads", " ".join(skills.validate(config, root)))
            config["phases"][0]["skills"] = {"implementer": ["loader"]}
            config["roles"]["implementer"]["command"] = ["codex", "exec"]
            self.assertEqual(skills.validate(config, root), [])

    def test_validate_config_raises_on_skill_problems(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = self.config(root, skills={"missing": {}})
            with self.assertRaisesRegex(ConfigurationError, "SKILL.md not found"):
                validate_config(config, root)

    def test_a_skill_folder_is_governed_so_agents_cannot_change_it(self):
        config = {"governance_documents": ["docs/X.md"], "skills": {"impeccable": {}, "ux": {"path": "kit/ux/"}}}
        documents = governance_documents(config)
        self.assertTrue(is_governance_path("skills/impeccable/SKILL.md", documents))
        self.assertTrue(is_governance_path("kit/ux/scripts/run.sh", documents))
        self.assertFalse(is_governance_path("skills/other/SKILL.md", documents))
        self.assertTrue(is_governance_path("docs/X.md", documents))


class TrustTests(unittest.TestCase):
    def test_trust_pins_skill_bytes_and_executable_bits(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"; control = Path(d) / "control"; root.mkdir()
            git(root, "init", "-q"); git(root, "config", "user.email", "x@y"); git(root, "config", "user.name", "x")
            folder = write_skill(root, "impeccable")
            binary = bytes(range(256)) * 4 + b"\r\n\x00end"
            (folder / "bin").mkdir(); (folder / "bin/tool").write_bytes(binary)
            os.chmod(folder / "bin/tool", 0o755)
            config = copy.deepcopy(BASE); config["skills"] = {"impeccable": {}}
            repo_with(root, config)
            git(root, "add", "."); git(root, "commit", "-qm", "with skills")
            sha = git(root, "rev-parse", "HEAD")
            anchor = TrustStore(root, control).initialize(sha, sha)
            self.assertIn("skills/impeccable/SKILL.md", anchor["manifest"])
            self.assertEqual((control / "snapshot/skills/impeccable/bin/tool").read_bytes(), binary)
            self.assertEqual(anchor["skill_modes"], {"skills/impeccable/bin/tool": "100755"})
            # A later edit to the installed or worktree copy cannot reach the pinned bytes.
            (folder / "SKILL.md").write_text("tampered")
            TrustStore(root, control).load()

    def test_builds_without_skills_keep_the_same_anchor_shape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"; control = Path(d) / "control"; root.mkdir()
            git(root, "init", "-q"); git(root, "config", "user.email", "x@y"); git(root, "config", "user.name", "x")
            repo_with(root, copy.deepcopy(BASE))
            git(root, "add", "."); git(root, "commit", "-qm", "plain")
            sha = git(root, "rev-parse", "HEAD")
            anchor = TrustStore(root, control).initialize(sha, sha)
            self.assertNotIn("skill_modes", anchor)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.snapshot, self.workspace, self.control, self.repo, self.bin = (
            self.root / "snapshot", self.root / "ws", self.root / "control", self.root / "repo", self.root / "bin")
        for path in (self.snapshot, self.workspace, self.control, self.repo, self.bin):
            path.mkdir()
        for name in ("impeccable", "critique", "phase-only"):
            write_skill(self.snapshot, name)
        (self.snapshot / "skills/impeccable/run.sh").write_text("#!/bin/sh\necho hi\n")
        for name in ("claude", "codex"):
            (self.bin / name).write_text("#!/bin/sh\n"); os.chmod(self.bin / name, 0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self) -> dict:
        return {"skills": {"impeccable": {}, "critique": {}, "phase-only": {}},
                "phases": [{"number": 3, "skills": {"implementer": ["phase-only"]}}],
                "roles": {
                    "implementer": {"auth_mode": "environment", "credential_env": [], "network": True,
                                    "command": [str(self.bin / "codex"), "exec", "--out", "{output}"],
                                    "skills": ["impeccable"]},
                    "reviewer": {"auth_mode": "host_cli", "credential_env": [], "network": True,
                                 "command": [str(self.bin / "claude"), "-p", "{prompt}", "--no-session-persistence",
                                             "--tools", "Read,Grep,Glob", "--out", "{output}"],
                                 "skills": ["critique"]}},
                "_trusted_executables": {}, "_skills_root": str(self.snapshot),
                "_skill_modes": {"skills/impeccable/run.sh": "100755"}}

    def launch(self, config: dict, role: str, phase=None) -> list[str]:
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"] = list(argv)
            out = Path(argv[argv.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"status": "complete"}')
            return subprocess.CompletedProcess(argv, 0, "", "")

        context = IsolationContext(role, self.workspace, self.control, self.repo, role == "reviewer",
                                   host_home=Path.home())
        RoleRunner(config, FakeIsolationBackend(), context, runner).run(
            role, "prompt", self.control / f"{role}.json", 30, phase=phase)
        return seen["argv"]

    def runtime(self) -> Path:
        return next((self.workspace / ".factory-runtime").iterdir())

    def test_codex_gets_its_role_and_phase_skills_in_the_isolated_home(self):
        self.launch(self.config(), "implementer", phase=3)
        home_skills = self.runtime() / "home/.codex/skills"
        self.assertEqual(sorted(p.name for p in home_skills.iterdir()), ["impeccable", "phase-only"])
        script = home_skills / "impeccable/run.sh"
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR, "the trust record's executable bit is kept")
        self.assertFalse(os.stat(home_skills / "impeccable/SKILL.md").st_mode & stat.S_IWUSR)

    def test_the_reviewer_gets_only_its_named_skills_through_a_plugin(self):
        argv = self.launch(self.config(), "reviewer", phase=3)
        plugin = Path(argv[argv.index("--plugin-dir") + 1])
        self.assertEqual(sorted(p.name for p in (plugin / "skills").iterdir()), ["critique"])
        self.assertEqual(json.loads((plugin / ".claude-plugin/plugin.json").read_text())["name"], skills.PLUGIN_NAME)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Skill")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")

    def test_a_reviewer_without_skills_still_drops_personal_settings(self):
        config = self.config(); config["roles"]["reviewer"].pop("skills")
        argv = self.launch(config, "reviewer")
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob", "no Skill tool without skills")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")

    def test_the_operator_s_own_setting_sources_win(self):
        argv = skills.adapt_claude_argv(["claude", "--setting-sources", "project"], None)
        self.assertEqual(argv.count("--setting-sources"), 1)

    def test_a_debate_turn_gets_no_skills(self):
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"] = list(argv)
            out = Path(argv[argv.index("--out") + 1]); out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"status": "complete"}')
            return subprocess.CompletedProcess(argv, 0, "", "")
        context = IsolationContext("reviewer", self.workspace, self.control, self.repo, True, host_home=Path.home())
        RoleRunner(self.config(), FakeIsolationBackend(), context, runner).run(
            "reviewer", "prompt", self.control / "debate.json", 30, debate=True)
        self.assertNotIn("--plugin-dir", seen["argv"])

    def test_a_skill_missing_from_the_snapshot_stops_the_call(self):
        config = self.config(); config["roles"]["implementer"]["skills"] = ["ghost"]; config["skills"]["ghost"] = {}
        with self.assertRaisesRegex(Exception, "SKILL_UNAVAILABLE"):
            self.launch(config, "implementer")

    def test_invocations_are_read_from_the_agent_s_own_events(self):
        lines = [
            # Claude's start-up event names every available skill; that is not a use.
            '{"type":"system","subtype":"init","skills":["buildctl-skills:critique","buildctl-skills:unused"]}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill",'
            '"input":{"skill":"buildctl-skills:critique"}}]}}',
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"command":"/bin/zsh -lc \'cat /h/.codex/skills/impeccable/SKILL.md\'"}}',
            'not json']
        self.assertEqual(skills.invoked("reviewer", lines, ["critique", "impeccable", "unused"]),
                         ["critique", "impeccable"])


class ReceiptTests(unittest.TestCase):
    def test_a_receipt_lists_the_skills_a_call_was_offered_and_verifies(self):
        from buildctl.supervisor import evidence as ev
        from tests.helpers import repository
        temporary, repo = repository(); self.addCleanup(temporary.cleanup)
        holder = tempfile.TemporaryDirectory(); self.addCleanup(holder.cleanup)
        control = Path(holder.name).resolve()
        base = git(repo, "rev-parse", "HEAD"); git(repo, "branch", "-M", "main")
        offered = [{"name": "impeccable", "path": "skills/impeccable", "digest": "d" * 64}]
        record = ev.AgentRecord(ev.EvidenceStore(control).path_for("phase-1:review-1:1", 1), {
            "operation": "phase-1:review-1:1", "phase": 1, "attempt": 1, "role": "reviewer", "cli": "claude",
            "binary_sha256": "b", "prompt_sha256": "p", "command": ["claude", "--plugin-dir", "/x"],
            "workspace": "/workspace", "started_at": ev.now(), "skills": offered})
        record.close(exit_code=0, terminated=None)
        (repo / "a.txt").write_text("a"); git(repo, "add", "a.txt"); git(repo, "commit", "-qm", "a")
        code = git(repo, "rev-parse", "HEAD")
        branch = ev.EvidenceBranch(repo)
        manifest = ev.build_manifest(control, {}, phase=1, code_commit=code,
                                     tree_hash=git(repo, "rev-parse", f"{code}^{{tree}}"), base_sha=base,
                                     governance_sha="g", attempt=1, previous=branch.latest_manifest_sha(),
                                     flags=[], ledger=[])
        self.assertEqual(manifest["attempts"][0]["operations"][0]["skills"], offered)
        branch.append(manifest, control / "tmp")
        self.assertEqual(ev.verify(repo, code, control / "records")["status"], "verified")


class WorkspaceTests(unittest.TestCase):
    def test_repository_agent_folders_are_stripped_before_an_agent_starts(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            for folder in (".claude/skills/x", ".codex", ".agents/skills/y"):
                (workspace / folder).mkdir(parents=True)
            GitManager(workspace, {"governance_documents": []}).prepare_agent_workspace(workspace)
            self.assertEqual(sorted(p.name for p in workspace.iterdir()), [])


class AddSyncTests(unittest.TestCase):
    def test_add_copies_an_installed_skill_and_sync_follows_its_source(self):
        with tempfile.TemporaryDirectory() as d:
            installed, repo = Path(d) / "installed/impeccable", Path(d) / "repo"
            write_skill(installed.parent, "impeccable", "impeccable"); repo.mkdir()
            (installed / "bin").mkdir(); (installed / "bin/huge").write_bytes(b"x" * 10)
            result = skills.add(repo, installed, exclude=["bin/*"])
            self.assertEqual(result["path"], "skills/impeccable")
            self.assertFalse((repo / "skills/impeccable/bin/huge").exists())
            config = {"skills": {"impeccable": {}}}
            self.assertEqual(skills.status(repo, config)[0]["state"], "in sync")
            (installed / "SKILL.md").write_text(SKILL.format(name="impeccable") + "More.\n")
            self.assertEqual(skills.status(repo, config)[0]["state"], "source changed")
            skills.sync(repo, config, "impeccable")
            self.assertIn("More.", (repo / "skills/impeccable/SKILL.md").read_text())
            self.assertEqual(skills.status(repo, config)[0]["state"], "in sync")
            with self.assertRaisesRegex(skills.SkillError, "already exists"):
                skills.add(repo, installed)


if __name__ == "__main__":
    unittest.main()
