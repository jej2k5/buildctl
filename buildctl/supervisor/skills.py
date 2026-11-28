"""Skills: instruction folders (SKILL.md plus files) an agent may load, governed like the plan.

A build declares skills in build.yaml; each lives in the repository (default `skills/<name>/`),
so trust init fingerprints its files with the rest of the governed set and an agent can never
edit one. A role lists the skills it gets; a phase can add more per role. The reviewer gets only
the skills named for it, never the implementer's by default.

At launch the named skills are copied from the trusted snapshot into the place each agent CLI
reads them, inside that call's own scratch space:

- Codex (implementer, documenter): `$CODEX_HOME/skills/<name>/`, in the call's isolated home.
- Claude Code (reviewer): a throwaway plugin passed with `--plugin-dir`. Every Claude role also
  runs with `--setting-sources project,local`, so the operator's personal skills, plugins and
  CLAUDE.md never reach it, skills or not.

Nothing is read from the operator's installed skill folders at run time. `buildctl skills add`
copies an installed skill into the repository; committing it and trusting again is the
operator's act.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .safety import ensure_safe_relative

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SKILL_ROLES = ("implementer", "reviewer", "documenter")
ENTRY_KEYS = {"path"}
DEFAULT_ROOT = "skills"
MAX_SKILL_BYTES = 8 * 1024 * 1024
SOURCE_FILE = ".buildctl-source.json"
PLUGIN_NAME = "buildctl-skills"
CLAUDE_SETTING_SOURCES = "project,local"


class SkillError(ValueError):
    pass


# --- configuration ---------------------------------------------------------------------------

def declared(config: dict[str, Any]) -> dict[str, str]:
    """Skill name to its repository folder, without a trailing slash."""
    section = config.get("skills") or {}
    if not isinstance(section, dict):
        return {}
    result = {}
    for name, entry in section.items():
        path = entry.get("path") if isinstance(entry, dict) else None
        result[name] = (path or f"{DEFAULT_ROOT}/{name}").rstrip("/")
    return result


def governed_prefixes(config: dict[str, Any]) -> tuple[str, ...]:
    """Each declared skill folder as a governed prefix ("skills/impeccable/")."""
    return tuple(f"{path}/" for path in declared(config).values())


def harness(command: list[str]) -> str | None:
    """Which agent CLI a role runs, by its executable's name."""
    if not command:
        return None
    name = Path(str(command[0])).name
    if name.startswith("claude"):
        return "claude"
    if name.startswith("codex"):
        return "codex"
    return None


def frontmatter(text: str) -> dict[str, str]:
    """The `name:` and `description:` lines of a SKILL.md front matter block, flat values only."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"name", "description"} and not line.startswith((" ", "\t")):
            result[key.strip()] = value.strip().strip("'\"")
    return result


def role_skills(config: dict[str, Any], role: str) -> list[str]:
    value = config.get("roles", {}).get(role, {}).get("skills", [])
    return list(value) if isinstance(value, list) else []


def phase_skills(config: dict[str, Any], number: int | None, role: str) -> list[str]:
    if number is None:
        return []
    for item in config.get("phases", []):
        if item.get("number") == number:
            value = (item.get("skills") or {}).get(role, [])
            return list(value) if isinstance(value, list) else []
    return []


def reviewer_names(config: dict[str, Any]) -> set[str]:
    """Every skill the reviewer can get, from its role or any phase."""
    names = set(role_skills(config, "reviewer"))
    for item in config.get("phases", []):
        extra = item.get("skills")
        if isinstance(extra, dict) and isinstance(extra.get("reviewer"), list):
            names.update(name for name in extra["reviewer"] if isinstance(name, str))
    return names


def for_call(config: dict[str, Any], role: str, phase: int | None = None) -> list[str]:
    """The skills one agent call gets: its role's, then the phase's additions, in order, once each."""
    names: list[str] = []
    for name in [*role_skills(config, role), *phase_skills(config, phase, role)]:
        if name not in names:
            names.append(name)
    return names


def validate(config: dict[str, Any], root: Path) -> list[str]:
    """Problems with the skills section and every reference to it. Empty when sound."""
    problems: list[str] = []
    section = config.get("skills", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        return ["skills must be an object of name to {\"path\": ...}"]
    paths = declared(config)
    for name, entry in section.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            problems.append(f"skill name {name!r} must be lowercase letters, digits and hyphens")
            continue
        if not isinstance(entry, dict) or set(entry) - ENTRY_KEYS:
            problems.append(f"skills.{name} may set only path")
            continue
        path = paths[name]
        try:
            folder = ensure_safe_relative(root, path)
        except ValueError:
            problems.append(f"skills.{name}.path is not a safe relative path: {path}")
            continue
        if path in {"", "."} or path.startswith((".claude", ".codex", ".agents")):
            problems.append(f"skills.{name}.path must be its own folder, not {path!r}")
            continue
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            problems.append(f"skills.{name}: {path}/SKILL.md not found")
            continue
        body = skill_md.read_text(errors="replace")
        meta = frontmatter(body)
        if name in reviewer_names(config) and re.search(r"!`[^`]+`", body):
            problems.append(f"skills.{name} runs shell commands when it loads (!`...`), which the read-only "
                            "reviewer cannot do; give it to the implementer instead")
        if meta.get("name") != name:
            problems.append(f"skills.{name}: SKILL.md front matter must say name: {name}")
        if not meta.get("description"):
            problems.append(f"skills.{name}: SKILL.md front matter needs a description")
        total = 0
        for file in folder.rglob("*"):
            if file.is_symlink():
                problems.append(f"skills.{name}: symlinks are not allowed ({file.relative_to(root)})")
            elif file.is_file():
                total += file.stat().st_size
        if total > MAX_SKILL_BYTES:
            problems.append(f"skills.{name} is {total // 1024} KiB; the limit is {MAX_SKILL_BYTES // 1024} KiB")
    folders = sorted(paths.values())
    for first, second in zip(folders, folders[1:]):
        if second.startswith(first + "/"):
            problems.append(f"skill folders must not nest: {second} is inside {first}")
    roles = config.get("roles", {})
    for role, role_config in roles.items():
        listed = role_config.get("skills")
        if listed is None:
            continue
        if role not in SKILL_ROLES:
            problems.append(f"roles.{role} cannot take skills; only {', '.join(SKILL_ROLES)} can")
            continue
        problems += _references(listed, paths, f"roles.{role}.skills")
        if listed and harness(role_config.get("command", [])) is None:
            problems.append(f"roles.{role}.skills: skills are supported for claude and codex commands")
    for item in config.get("phases", []):
        extra = item.get("skills")
        if extra is None:
            continue
        where = f"phase {item.get('number')} skills"
        if not isinstance(extra, dict) or set(extra) - set(SKILL_ROLES):
            problems.append(f"{where} must map {', '.join(SKILL_ROLES)} to lists of skill names")
            continue
        for role, listed in extra.items():
            problems += _references(listed, paths, f"{where}.{role}")
            command = roles.get(role, roles.get("implementer", {}) if role == "documenter" else {}).get("command", [])
            if listed and harness(command) is None:
                problems.append(f"{where}.{role}: skills are supported for claude and codex commands")
    return problems


def _references(listed: Any, paths: dict[str, str], where: str) -> list[str]:
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        return [f"{where} must be a list of skill names"]
    return [f"{where} names {name!r}, which skills does not declare" for name in listed if name not in paths]


# --- launch ----------------------------------------------------------------------------------

def materialize(config: dict[str, Any], names: list[str], destination: Path) -> list[dict[str, Any]]:
    """Copy the named skills from the trusted snapshot into `destination/<name>/`.

    Returns what was offered, each skill with a digest of its files, for the evidence record.
    Executable bits come from the trust record (Git's file mode), never from the worktree."""
    if not names:
        return []
    source_root = Path(config.get("_skills_root") or ".")
    modes = config.get("_skill_modes") or {}
    paths = declared(config)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    offered = []
    for name in names:
        relative = paths[name]
        source = source_root / relative
        if not (source / "SKILL.md").is_file():
            raise SkillError(f"skill {name} is not in the trusted snapshot; trust the commit that adds it")
        target = destination / name
        if target.exists():
            shutil.rmtree(target)
        files: dict[str, str] = {}
        for file in sorted(source.rglob("*")):
            if file.is_symlink() or not file.is_file() or file.name == SOURCE_FILE:
                continue  # the provenance note is for the operator, not the agent
            inner = file.relative_to(source)
            out = target / inner
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = file.read_bytes()
            out.write_bytes(data)
            executable = modes.get(f"{relative}/{inner.as_posix()}") == "100755" or (
                not modes and os.access(file, os.X_OK))
            os.chmod(out, 0o500 if executable else 0o400)
            files[inner.as_posix()] = _sha256(data)
        offered.append({"name": name, "path": relative,
                        "digest": _sha256(json.dumps(files, sort_keys=True).encode())})
    return offered


def claude_plugin(config: dict[str, Any], names: list[str], directory: Path) -> list[dict[str, Any]]:
    """A throwaway Claude Code plugin holding the named skills, for `--plugin-dir`."""
    if directory.exists():
        shutil.rmtree(directory)
    (directory / ".claude-plugin").mkdir(parents=True, mode=0o700)
    (directory / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": PLUGIN_NAME, "version": "1.0.0", "description": "Skills pinned by buildctl's trust record"}) + "\n")
    return materialize(config, names, directory / "skills")


def _flag_value(argv: list[str], flag: str) -> int | None:
    """Index of a flag's value, whether written `--flag value` or `--flag=value` (then the flag's own index)."""
    for index, part in enumerate(argv):
        if part == flag:
            return index + 1 if index + 1 < len(argv) else None
        if part.startswith(flag + "="):
            return index
    return None


def adapt_claude_argv(argv: list[str], plugin_dir: Path | None, names: list[str] | None = None) -> list[str]:
    """Keep the operator's personal Claude setup out, and hand over the pinned skills.

    `--setting-sources project,local` drops user settings, skills, plugins and CLAUDE.md; the
    agent's workspace has its own `.claude` stripped, so nothing ambient remains. With skills,
    the plugin is added and `Skill` joins the role's `--tools` list, which stays otherwise as
    the operator pinned it."""
    result = list(argv)
    if _flag_value(result, "--setting-sources") is None:
        result += ["--setting-sources", CLAUDE_SETTING_SOURCES]
    if plugin_dir is not None:
        result += ["--plugin-dir", str(plugin_dir)]
        index = _flag_value(result, "--tools")
        if index is not None:
            prefix = "--tools=" if result[index].startswith("--tools=") else ""
            tools = [tool.strip() for tool in result[index][len(prefix):].split(",") if tool.strip()]
            if "Skill" not in tools:
                result[index] = prefix + ",".join([*tools, "Skill"])
        # A headless run cannot answer a permission prompt: pre-approve exactly the pinned skills.
        # Pre-approval grants no tool the role lacks; `--tools` still decides what exists.
        result += ["--allowedTools", ",".join(f"Skill({PLUGIN_NAME}:{name})" for name in (names or []))]
    return result


def invoked(role: str, lines: list[str], names: list[str]) -> list[str]:
    """Which offered skills the agent's own events show it loading: a Claude `Skill` tool call
    naming one, or a Codex command that reads one's SKILL.md. A skill only listed as available
    (Claude's start-up event names them all) does not count."""
    seen: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for target in _skill_targets(event):
            for name in names:
                if name not in seen and (target in {name, f"{PLUGIN_NAME}:{name}"} or f"skills/{name}/SKILL.md" in target):
                    seen.append(name)
    return [name for name in names if name in seen]


def _skill_targets(event: Any) -> list[str]:
    targets: list[str] = []
    message = event.get("message") if isinstance(event, dict) else None
    for block in (message.get("content") or []) if isinstance(message, dict) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
            value = (block.get("input") or {}).get("skill")
            if isinstance(value, str):
                targets.append(value)
    item = event.get("item") if isinstance(event, dict) else None
    if isinstance(item, dict) and item.get("type") == "command_execution" and isinstance(item.get("command"), str):
        targets.append(item["command"])
    return targets


# --- adding skills to a repository -----------------------------------------------------------

def add(repo: Path, source: Path, name: str | None = None, exclude: list[str] | None = None) -> dict[str, Any]:
    """Copy an installed skill folder into the repository's skills/ and record where it came from.

    `exclude` takes globs relative to the skill folder (`scripts/bin/*`) for files too large or
    too machine-specific to commit, such as a bundled binary its launcher can fetch instead."""
    source = source.expanduser().resolve()
    skill_md = source / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError(f"{source} has no SKILL.md")
    meta = frontmatter(skill_md.read_text(errors="replace"))
    name = name or meta.get("name") or source.name
    if not NAME.fullmatch(name):
        raise SkillError(f"skill name {name!r} must be lowercase letters, digits and hyphens")
    target = repo / DEFAULT_ROOT / name
    if target.exists():
        raise SkillError(f"{target.relative_to(repo)} already exists; use buildctl skills sync {name}")
    exclude = list(exclude or [])
    _copy_tree(source, target, exclude)
    record = {"name": name, "source": _portable(source), "exclude": exclude, "digest": tree_digest(target)}
    (target / SOURCE_FILE).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return {**record, "path": f"{DEFAULT_ROOT}/{name}", "renamed": meta.get("name") not in (None, name)}


def status(repo: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Each declared skill: whether its recorded source has moved on since it was copied in."""
    rows = []
    for name, relative in sorted(declared(config).items()):
        folder = repo / relative
        record_path = folder / SOURCE_FILE
        row: dict[str, Any] = {"name": name, "path": relative, "source": None, "state": "repository only"}
        if record_path.is_file():
            record = json.loads(record_path.read_text())
            source = Path(record["source"]).expanduser()
            row["source"] = record["source"]
            if not (source / "SKILL.md").is_file():
                row["state"] = "source missing"
            elif tree_digest(source, record.get("exclude", [])) != record.get("digest"):
                row["state"] = "source changed"
            elif tree_digest(folder) != record.get("digest"):
                row["state"] = "edited in repository"
            else:
                row["state"] = "in sync"
        rows.append(row)
    return rows


def sync(repo: Path, config: dict[str, Any], name: str) -> dict[str, Any]:
    """Re-copy a skill from its recorded source. The operator commits and trusts again."""
    paths = declared(config)
    if name not in paths:
        raise SkillError(f"build.yaml does not declare skill {name}")
    folder = repo / paths[name]
    record_path = folder / SOURCE_FILE
    if not record_path.is_file():
        raise SkillError(f"{paths[name]} has no recorded source; it was not added with buildctl skills add")
    record = json.loads(record_path.read_text())
    source = Path(record["source"]).expanduser()
    if not (source / "SKILL.md").is_file():
        raise SkillError(f"the recorded source {record['source']} no longer has a SKILL.md")
    shutil.rmtree(folder)
    _copy_tree(source, folder, record.get("exclude", []))
    record["digest"] = tree_digest(folder)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return {"name": name, "path": paths[name], "source": record["source"], "digest": record["digest"]}


def _excluded(inner: str, exclude: list[str]) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(inner, pattern) or inner.startswith(pattern.rstrip("/") + "/") for pattern in exclude)


def _portable(path: Path) -> str:
    """A source path without the operator's home directory in it: the note is committed."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def tree_digest(folder: Path, exclude: list[str] | None = None) -> str:
    files = {}
    for file in sorted(folder.rglob("*")):
        inner = file.relative_to(folder).as_posix()
        if file.is_file() and not file.is_symlink() and file.name != SOURCE_FILE and ".git" not in file.parts \
                and not _excluded(inner, exclude or []):
            files[inner] = _sha256(file.read_bytes())
    return _sha256(json.dumps(files, sort_keys=True).encode())


def _copy_tree(source: Path, target: Path, exclude: list[str] | None = None) -> None:
    for file in sorted(source.rglob("*")):
        inner = file.relative_to(source)
        if ".git" in inner.parts or file.name == SOURCE_FILE or file.is_dir() or _excluded(inner.as_posix(), exclude or []):
            continue
        if file.is_symlink():
            raise SkillError(f"{file} is a symlink; skills are copied as plain files")
        out = target / inner
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, out)


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()
