"""The Setup view's skills picker: skills installed on this machine, and the ones in the build.

The console edits only the working tree: it copies a skill into `skills/<name>/` (the kernel's
`buildctl skills add`) and updates `build.yaml`. Committing and trusting stay the operator's acts,
so nothing here changes what a running build uses until the operator pins the new commit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from buildctl.supervisor import skills as kernel
except ImportError:  # an older buildctl without skills support
    kernel = None

ROLES = ("implementer", "reviewer", "documenter")
LARGE_FILE_BYTES = 1024 * 1024
SHELL_ON_LOAD = re.compile(r"!`[^`]+`")


def installed_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    return [("Claude Code", home / ".claude/skills"), ("Codex", home / ".codex/skills"),
            ("Shared", home / ".agents/skills")]


class SkillsRefused(Exception):
    pass


def _display(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _skill_files(folder: Path) -> list[Path]:
    result = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [name for name in dirs if name != ".git"]
        result.extend(Path(base) / name for name in files)
    return result


def scan_installed() -> list[dict[str, Any]]:
    """Every SKILL.md folder under the installed roots, nested up to three levels, dot folders skipped
    (Codex keeps its own bundled skills in `.system`; they ship inside the pinned executable)."""
    found: dict[str, dict[str, Any]] = {}
    for where, root in installed_roots():
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("**/SKILL.md")):
            folder = skill_md.parent
            parts = folder.relative_to(root).parts
            if not parts or len(parts) > 3 or any(part.startswith(".") for part in parts):
                continue
            real = folder.resolve()
            if str(real) in found:
                found[str(real)]["also"].append(where)
                continue
            text = skill_md.read_text(errors="replace")
            meta = _frontmatter(text)
            files = _skill_files(folder)
            large = sorted(f.relative_to(folder).as_posix() for f in files if f.stat().st_size >= LARGE_FILE_BYTES)
            found[str(real)] = {
                "name": meta.get("name") or folder.name, "description": meta.get("description", ""),
                "where": where, "also": [], "source": _display(folder), "files": len(files),
                "bytes": sum(f.stat().st_size for f in files), "large": large,
                "suggested_exclude": sorted({_exclude_for(path) for path in large}),
                "shell_on_load": bool(SHELL_ON_LOAD.search(text))}
    return sorted(found.values(), key=lambda item: (item["name"], item["where"]))


def _exclude_for(path: str) -> str:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    return f"{folder}/*" if folder else path


def _frontmatter(text: str) -> dict[str, str]:
    if kernel is not None:
        return kernel.frontmatter(text)
    return {}


def _read_config(repo: Path) -> tuple[dict[str, Any], str]:
    text = (repo / "build.yaml").read_text()
    return json.loads(text), text


def _write_config(repo: Path, config: dict[str, Any], original: str) -> bool:
    """Write build.yaml back as two-space JSON. Returns whether the file was reformatted beyond the edit."""
    reformatted = json.dumps(json.loads(original), indent=2) + "\n" != original
    (repo / "build.yaml").write_text(json.dumps(config, indent=2) + "\n")
    return reformatted


def _trusted_config(control_root: Path) -> dict[str, Any]:
    try:
        return json.loads((control_root / "snapshot" / "build.yaml").read_text())
    except (OSError, ValueError):
        return {}


def _worktree_changes(repo: Path, paths: list[str]) -> list[str]:
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all", "--", *paths],
                            text=True, capture_output=True)
    changed = []
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        top = path.split("/")[0] if not path.startswith("skills/") else "/".join(path.split("/")[:2]) + "/"
        if top not in changed:
            changed.append(top)
    return changed


def view(repo: Path, control_root: Path) -> dict[str, Any]:
    if kernel is None:
        return {"supported": False, "reason": "This buildctl has no skills support. Update it to use skills."}
    try:
        config, _ = _read_config(repo)
    except (OSError, ValueError) as exc:
        return {"supported": True, "error": f"build.yaml: {exc}", "declared": [], "installed": scan_installed()}
    trusted = kernel.declared(_trusted_config(control_root))
    statuses = {row["name"]: row for row in kernel.status(repo, config)}
    roles = config.get("roles", {})
    explicit_documenter = "documenter" in roles
    documenting = (config.get("documentation") or {}).get("mode", "off") != "off"
    problems = kernel.validate(config, repo) if config.get("skills") else []
    declared = []
    for name, path in sorted(kernel.declared(config).items()):
        folder = repo / path
        meta = _frontmatter((folder / "SKILL.md").read_text(errors="replace")) if (folder / "SKILL.md").is_file() else {}
        status = statuses.get(name, {})
        declared.append({
            "name": name, "path": path, "description": meta.get("description", ""), "present": (folder / "SKILL.md").is_file(),
            "roles": {role: name in kernel.role_skills(config, role) for role in ROLES},
            "phases": [{"number": item.get("number"), "roles": [role for role, names in (item.get("skills") or {}).items()
                                                                 if isinstance(names, list) and name in names]}
                       for item in config.get("phases", []) if any(name in (names or []) for names in (item.get("skills") or {}).values()
                                                                     if isinstance(names, list))],
            "source": status.get("source"), "state": status.get("state", "repository only"),
            "trusted": name in trusted, "shell_on_load": bool((folder / "SKILL.md").is_file()
                                                              and SHELL_ON_LOAD.search((folder / "SKILL.md").read_text(errors="replace")))})
    in_build = {item["source"] for item in declared if item["source"]} | {item["name"] for item in declared}
    installed = [dict(item, in_build=item["source"] in in_build or item["name"] in in_build) for item in scan_installed()]
    return {"supported": True, "declared": declared, "installed": installed, "problems": problems,
            "documenter": "explicit" if explicit_documenter else ("derived" if documenting else "off"),
            "trusted_names": sorted(trusted), "changes": _worktree_changes(repo, ["build.yaml", "skills"])}


# --- edits -----------------------------------------------------------------------------------

def _role_list(config: dict[str, Any], role: str) -> list[str]:
    role_config = config.setdefault("roles", {}).get(role)
    if role_config is None:
        raise SkillsRefused(f"build.yaml has no roles.{role}")
    return role_config.setdefault("skills", [])


def _set_roles(config: dict[str, Any], name: str, chosen: dict[str, bool]) -> None:
    for role, wanted in chosen.items():
        if role not in ROLES:
            raise SkillsRefused(f"unknown role {role}")
        if role == "documenter" and "documenter" not in config.get("roles", {}):
            continue  # a derived documenter takes the implementer's list
        names = _role_list(config, role)
        if wanted and name not in names:
            names.append(name)
        elif not wanted and name in names:
            names.remove(name)
        if not names:
            config["roles"][role].pop("skills", None)


def add(repo: Path, source: str, roles: dict[str, bool], exclude: list[str], name: str | None = None) -> dict[str, Any]:
    if kernel is None:
        raise SkillsRefused("this buildctl has no skills support")
    known = {item["source"]: item for item in scan_installed()}
    if source not in known:
        raise SkillsRefused("pick a skill from the installed list")
    item = known[source]
    if roles.get("reviewer") and item["shell_on_load"]:
        raise SkillsRefused(f"{item['name']} runs shell commands when it loads, which the read-only reviewer cannot do")
    config, original = _read_config(repo)
    result = kernel.add(repo, Path(source).expanduser(), name, exclude)
    config.setdefault("skills", {})[result["name"]] = {}
    _set_roles(config, result["name"], roles)
    reformatted = _write_config(repo, config, original)
    return {"text": f"Copied {result['name']} into {result['path']}/ and added it to build.yaml. Commit both, then pin the commit.",
            "reformatted": reformatted}


def set_roles(repo: Path, name: str, roles: dict[str, bool]) -> dict[str, Any]:
    config, original = _read_config(repo)
    if name not in (config.get("skills") or {}):
        raise SkillsRefused(f"build.yaml does not declare {name}")
    folder = repo / kernel.declared(config)[name]
    if roles.get("reviewer") and (folder / "SKILL.md").is_file() and SHELL_ON_LOAD.search((folder / "SKILL.md").read_text(errors="replace")):
        raise SkillsRefused(f"{name} runs shell commands when it loads, which the read-only reviewer cannot do")
    _set_roles(config, name, roles)
    reformatted = _write_config(repo, config, original)
    who = [role for role, on in roles.items() if on]
    return {"text": f"{name} now goes to {', '.join(who) if who else 'no role'}. Commit build.yaml, then pin the commit.",
            "reformatted": reformatted}


def remove(repo: Path, name: str) -> dict[str, Any]:
    config, original = _read_config(repo)
    paths = kernel.declared(config)
    if name not in paths:
        raise SkillsRefused(f"build.yaml does not declare {name}")
    for role in ROLES:
        role_config = config.get("roles", {}).get(role)
        if role_config and name in role_config.get("skills", []):
            role_config["skills"].remove(name)
            if not role_config["skills"]:
                role_config.pop("skills")
    for item in config.get("phases", []):
        extra = item.get("skills")
        if isinstance(extra, dict):
            for role in list(extra):
                if isinstance(extra[role], list) and name in extra[role]:
                    extra[role].remove(name)
                if not extra[role]:
                    extra.pop(role)
            if not extra:
                item.pop("skills")
    config["skills"].pop(name)
    if not config["skills"]:
        config.pop("skills")
    folder = (repo / paths[name]).resolve()
    if folder.is_relative_to(repo.resolve()) and folder != repo.resolve() and folder.is_dir():
        shutil.rmtree(folder)
    reformatted = _write_config(repo, config, original)
    return {"text": f"Removed {name} from build.yaml and deleted {paths[name]}/. Commit the change, then pin the commit.",
            "reformatted": reformatted}


def sync(repo: Path, name: str) -> dict[str, Any]:
    config, _ = _read_config(repo)
    result = kernel.sync(repo, config, name)
    return {"text": f"Copied {name} in again from {result['source']}. Review the change, commit it, then pin the commit.",
            "reformatted": False}


def act(repo: Path, body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action", ""))
    roles = {str(role): bool(on) for role, on in (body.get("roles") or {}).items()}
    try:
        if action == "add":
            return add(repo, str(body.get("source", "")), roles, [str(x) for x in body.get("exclude") or []],
                       str(body["name"]) if body.get("name") else None)
        if action == "roles":
            return set_roles(repo, str(body.get("name", "")), roles)
        if action == "remove":
            return remove(repo, str(body.get("name", "")))
        if action == "sync":
            return sync(repo, str(body.get("name", "")))
    except (kernel.SkillError if kernel else ValueError) as exc:
        raise SkillsRefused(str(exc)) from exc
    raise SkillsRefused(f"unknown skills action {action}")
