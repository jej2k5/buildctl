"""The Setup view's skills picker: installed skills found, build.yaml edited, nothing committed.

Run from the repository root with the UI on the path:
    PYTHONPATH=.:ui:drafts python3 -m unittest discover -s ui/uitests -t ui
"""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from buildctl_ui import skills_view

from .test_console import Console


def skill(folder: Path, name: str, body: str = "Do it.", description: str = "Use when it fits.") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")
    return folder


class SkillsPicker(Console):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        skill(self.home / ".claude/skills/impeccable", "impeccable")
        (self.home / ".claude/skills/impeccable/bin").mkdir()
        (self.home / ".claude/skills/impeccable/bin/tool").write_bytes(b"x" * (skills_view.LARGE_FILE_BYTES + 1))
        skill(self.home / ".claude/skills/synced/abc/loader", "loader", "Context: !`git log -1`")
        skill(self.home / ".codex/skills/.system/imagegen", "imagegen")
        skill(self.home / ".agents/skills/folded", "folded", description=">\n  Long words\n  folded.")
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.repo = self.build.repo

    def get(self):
        status, body = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        return body

    def post(self, body):
        return self.request("POST", "/api/skills", body)

    def test_installed_skills_are_found_and_bundled_ones_skipped(self):
        found = {item["name"]: item for item in self.get()["installed"]}
        self.assertEqual(sorted(found), ["folded", "impeccable", "loader"])
        self.assertEqual(found["impeccable"]["source"], "~/.claude/skills/impeccable")
        self.assertEqual(found["impeccable"]["suggested_exclude"], ["bin/*"])
        self.assertTrue(found["loader"]["shell_on_load"])
        self.assertEqual(found["folded"]["description"], "Long words folded.")

    def test_adding_a_skill_edits_only_the_working_tree(self):
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True, capture_output=True).stdout
        status, body = self.post({"action": "add", "source": "~/.claude/skills/impeccable",
                                  "roles": {"implementer": True, "reviewer": True}, "exclude": ["bin/*"]})
        self.assertEqual(status, 200, body)
        config = json.loads((self.repo / "build.yaml").read_text())
        self.assertEqual(config["skills"], {"impeccable": {}})
        self.assertEqual(config["roles"]["implementer"]["skills"], ["impeccable"])
        self.assertEqual(config["roles"]["reviewer"]["skills"], ["impeccable"])
        self.assertFalse((self.repo / "skills/impeccable/bin/tool").exists(), "the large file was left out")
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True,
                                        capture_output=True).stdout, head, "nothing is committed")
        view = self.get()
        declared = view["declared"][0]
        self.assertEqual((declared["name"], declared["trusted"], declared["state"]), ("impeccable", False, "in sync"))
        self.assertIn("build.yaml", view["changes"])
        self.assertEqual(view["problems"], [])
        self.assertTrue(next(i for i in view["installed"] if i["name"] == "impeccable")["in_build"])

    def test_roles_change_and_remove_clean_up_build_yaml(self):
        self.post({"action": "add", "source": "~/.claude/skills/impeccable", "roles": {"implementer": True}, "exclude": []})
        status, body = self.post({"action": "roles", "name": "impeccable", "roles": {"implementer": False, "reviewer": True}})
        self.assertEqual(status, 200, body)
        config = json.loads((self.repo / "build.yaml").read_text())
        self.assertNotIn("skills", config["roles"]["implementer"])
        self.assertEqual(config["roles"]["reviewer"]["skills"], ["impeccable"])
        status, body = self.post({"action": "remove", "name": "impeccable"})
        self.assertEqual(status, 200, body)
        config = json.loads((self.repo / "build.yaml").read_text())
        self.assertNotIn("skills", config)
        self.assertNotIn("skills", config["roles"]["reviewer"])
        self.assertFalse((self.repo / "skills/impeccable").exists())

    def test_a_shell_on_load_skill_never_goes_to_the_reviewer(self):
        status, body = self.post({"action": "add", "source": "~/.claude/skills/synced/abc/loader",
                                  "roles": {"reviewer": True}, "exclude": []})
        self.assertEqual(status, 409)
        self.assertIn("read-only reviewer", body["error"])
        self.assertFalse((self.repo / "skills/loader").exists())

    def test_only_listed_installed_skills_can_be_copied(self):
        status, body = self.post({"action": "add", "source": "/etc", "roles": {"implementer": True}, "exclude": []})
        self.assertEqual(status, 409)
        self.assertIn("installed list", body["error"])

    def test_the_picker_needs_the_token(self):
        status, _ = self.request("GET", "/api/skills", headers={"X-Buildctl-Token": "wrong"})
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/api/skills", {"action": "remove", "name": "x"}, headers={"X-Buildctl-Token": "wrong"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
