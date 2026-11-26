"""The documentation role: keep a build's docs as true as its code, phase by phase.

After the implementation passes its mechanical gates, a documenter edits documentation paths
in the same workspace and reports what it changed in `DOC_RESULT`. buildctl then checks that it
touched nothing else, re-runs the acceptance commands, runs the documentation gates (links,
changelog, forbidden phrases), and hands the combined delta to the reviewer. See
docs/planning/docs-and-drafts.md, Part 1.

Off by default: a build without a `documentation` section behaves exactly as before.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Callable

from .agents import AgentError, RoleRunner, TRUSTED_PREAMBLE, untrusted
from .governance import governance_documents, is_governance_path
from .protocol import PROTOCOL_ROOT, contract
from .safety import ensure_safe_relative


MODES = ("off", "optional", "required")
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BRIEF = "prompts/documentation.md"
DERIVED_LINEAGE = "documentation-v1"
CHANGE_KINDS = ("added", "updated", "removed", "fixed_stale")
DOC_RESULT_KEYS = {"status", "summary", "user_visible", "changes", "reason"}
CHANGE_KEYS = {"path", "reflects", "kind"}
SECTION_KEYS = {"mode", "paths", "changelog", "unreleased_heading", "forbidden_phrases", "max_attempts", "brief"}


# --- configuration ---------------------------------------------------------------------------

def section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("documentation")
    return value if isinstance(value, dict) else {}


def mode(config: dict[str, Any]) -> str:
    return section(config).get("mode", "off")


def enabled(config: dict[str, Any]) -> bool:
    return mode(config) != "off"


def enabled_for(config: dict[str, Any], phase: Any) -> bool:
    """On for the build, and not switched off for this phase with `documentation: false`."""
    return enabled(config) and getattr(phase, "documentation", True)


def patterns(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(section(config).get("paths", ()))


def max_attempts(config: dict[str, Any]) -> int:
    return int(section(config).get("max_attempts", DEFAULT_MAX_ATTEMPTS))


def matches(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """`README.md` matches itself, `docs/` and `docs/**` match everything under docs/."""
    for pattern in globs:
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        if pattern.endswith("/**") and path.startswith(pattern[:-2]):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


# Where agent instructions and the build's own rules live. A documentation path that reaches
# into these is a configuration mistake. Other governed files (AGENTS.md under `*.md`, or
# governance documents under `docs/`) may sit inside documentation paths: they stay read-only to
# every agent, and the documenter is told which they are.
INSTRUCTION_SAMPLES = ("build.yaml", "prompts/x.md", "templates/x.md")


def read_only(config: dict[str, Any], path: str) -> bool:
    return is_governance_path(path, governance_documents(config))


def validate_section(config: dict[str, Any], root: Path) -> list[str]:
    """Problems with the `documentation` section. An absent section is valid and means off."""
    if "documentation" not in config:
        return []
    value = config["documentation"]
    if not isinstance(value, dict):
        return ["documentation must be an object"]
    problems = []
    unknown = set(value) - SECTION_KEYS
    if unknown:
        problems.append(f"documentation has unknown keys: {', '.join(sorted(unknown))}")
    if value.get("mode", "off") not in MODES:
        problems.append(f"documentation.mode must be one of {', '.join(MODES)}")
    if value.get("mode", "off") == "off":
        return problems
    globs = value.get("paths")
    if not isinstance(globs, list) or not globs or not all(isinstance(item, str) and item for item in globs):
        return problems + ["documentation.paths must be a non-empty list of paths or globs"]
    for pattern in globs:
        try:
            ensure_safe_relative(root, pattern.replace("*", "x").rstrip("/") or "x")
        except ValueError:
            problems.append(f"documentation.paths entry is not a safe relative path: {pattern}")
    covered = [sample.replace("x.md", "") for sample in INSTRUCTION_SAMPLES if matches(sample, globs)]
    if covered:
        problems.append("documentation.paths must not reach governed instructions: " + ", ".join(covered))
    changelog = value.get("changelog")
    if changelog is not None and (not isinstance(changelog, str) or not matches(changelog, globs)):
        problems.append("documentation.changelog must be a path inside documentation.paths")
    heading = value.get("unreleased_heading", "Unreleased")
    if not isinstance(heading, str) or not heading.strip():
        problems.append("documentation.unreleased_heading must be a non-empty string")
    phrases = value.get("forbidden_phrases", [])
    if not isinstance(phrases, list) or not all(isinstance(item, str) and item.strip() for item in phrases):
        problems.append("documentation.forbidden_phrases must be a list of non-empty strings")
    attempts = value.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        problems.append("documentation.max_attempts must be a whole number of at least 1")
    brief = value.get("brief")
    if brief is not None and (not isinstance(brief, str) or not is_governance_path(brief, governance_documents(config))):
        problems.append("documentation.brief must be a governed path (under prompts/ or in governance_documents)")
    elif brief is not None and not (root / brief).is_file():
        problems.append(f"documentation.brief not found: {brief}")
    return problems


def validate_phase_switch(item: dict[str, Any]) -> list[str]:
    if "documentation" in item and not isinstance(item["documentation"], bool):
        return [f"phase {item.get('number')}: documentation must be true or false"]
    return []


def materialize_documenter(config: dict[str, Any]) -> None:
    """With documentation on and no documenter configured, run it as the implementer's family.

    Done at load, so trust init pins the executable under the documenter's own name and every
    later reader sees one shape. The reviewer is never asked to grade its own family's prose
    unless the build says so explicitly; `warnings` names that case.
    """
    roles = config.get("roles", {})
    if not enabled(config) or "documenter" in roles or "implementer" not in roles:
        return
    derived = {key: value for key, value in roles["implementer"].items() if key != "remediation_prompt"}
    derived["prompt_lineage"] = DERIVED_LINEAGE
    derived["derived_from"] = "implementer"
    roles["documenter"] = derived


def warnings(config: dict[str, Any]) -> list[str]:
    roles = config.get("roles", {})
    documenter, reviewer = roles.get("documenter"), roles.get("reviewer", {})
    if not enabled(config) or not documenter:
        return []
    family = documenter.get("model_family")
    if family and family == reviewer.get("model_family"):
        return [f"the documenter and the reviewer are both {family}: the reviewer will grade its own family's prose"]
    if not family and documenter.get("command", [""])[0] == reviewer.get("command", [None])[0]:
        return ["the documenter and the reviewer run the same CLI: the reviewer will grade its own family's prose"]
    return []


# --- the result contract ---------------------------------------------------------------------

def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    def refuse(why: str) -> None:
        raise AgentError(f"DOCUMENTER_OUTPUT_SCHEMA_INVALID:{why}")

    if set(value) != DOC_RESULT_KEYS:
        refuse(f"keys must be exactly {', '.join(sorted(DOC_RESULT_KEYS))}")
    if value["status"] not in {"updated", "no_change"}:
        refuse("status must be updated or no_change")
    if not isinstance(value["summary"], str) or not isinstance(value["reason"], str):
        refuse("summary and reason must be strings")
    if not isinstance(value["user_visible"], bool):
        refuse("user_visible must be true or false")
    if not isinstance(value["changes"], list):
        refuse("changes must be a list")
    for change in value["changes"]:
        if not isinstance(change, dict) or set(change) != CHANGE_KEYS:
            refuse(f"each change has exactly {', '.join(sorted(CHANGE_KEYS))}")
        if change["kind"] not in CHANGE_KINDS or not all(isinstance(change[key], str) and change[key]
                                                         for key in ("path", "reflects")):
            refuse(f"change kind must be one of {', '.join(CHANGE_KINDS)}, with a path and what it reflects")
    if value["status"] == "updated" and not value["changes"]:
        refuse("an updated result lists its changes")
    if value["status"] == "no_change" and (value["changes"] or not value["reason"].strip()):
        refuse("a no_change result lists no changes and gives a reason")
    return value


# --- what the documenter may touch -----------------------------------------------------------

def scope_violations(before: dict[str, str], after: dict[str, str], config: dict[str, Any]) -> list[str]:
    """Paths the documenter changed outside documentation, or inside governance. A hard stop."""
    changed = {path for path, digest in after.items() if before.get(path) != digest}
    changed |= {path for path in before if path not in after}
    globs, documents = patterns(config), governance_documents(config)
    return sorted(path for path in changed
                  if not matches(path, globs) or is_governance_path(path, documents))


# --- documentation gates ---------------------------------------------------------------------

FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<img[^>]+src=\"([^\"]+)\"")
EXTERNAL = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _prose_lines(text: str):
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def heading_anchors(text: str) -> set[str]:
    """GitHub's heading slugs, with -1, -2 for repeats, plus explicit `<a id>` anchors."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _prose_lines(text):
        heading = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            slug = re.sub(r"<[^>]+>", "", heading.group(1)).strip().lower()
            slug = re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", slug))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(f"{slug}-{count}" if count else slug)
        found.update(re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", line))
    return found


def broken_links(workspace: Path, globs: tuple[str, ...], skip: Callable[[str], bool] = lambda path: False) -> list[str]:
    """Relative links and heading anchors in documentation Markdown that don't resolve.

    `skip` leaves out sources the documenter cannot edit (governed files), so a defect it is not
    allowed to fix can never exhaust its attempts."""
    from urllib.parse import unquote

    root = workspace.resolve()
    cache: dict[Path, set[str]] = {}
    broken = []
    for source in sorted(root.rglob("*.md")):
        relative = source.relative_to(root).as_posix()
        if ".git" in source.parts or ".factory-runtime" in source.parts or not matches(relative, globs) \
                or skip(relative):
            continue
        for number, line in _prose_lines(source.read_text(errors="replace")):
            for match in LINK.finditer(line):
                target = match.group(1) or match.group(2)
                if EXTERNAL.match(target):
                    continue
                path, _, anchor = target.partition("#")
                resolved = (source.parent / unquote(path)).resolve() if path else source
                try:
                    resolved.relative_to(root)
                except ValueError:
                    broken.append(f"{relative}:{number} {target} (outside the repository)")
                    continue
                if path and not resolved.exists():
                    broken.append(f"{relative}:{number} {target} (missing file)")
                elif anchor and resolved.suffix == ".md" and resolved.is_file():
                    if resolved not in cache:
                        cache[resolved] = heading_anchors(resolved.read_text(errors="replace"))
                    if anchor not in cache[resolved]:
                        broken.append(f"{relative}:{number} {target} (missing heading)")
    return broken


def unreleased_entries(text: str, heading: str) -> list[str]:
    """List items under the first heading that names `heading`, up to the next heading of its level."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match and heading.lower() in match.group(2).lower():
            level, entries = len(match.group(1)), []
            for following in lines[index + 1:]:
                next_heading = re.match(r"^(#{1,6})\s", following)
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                if re.match(r"^\s*[-*+]\s+\S", following):
                    entries.append(following.strip())
            return entries
    return []


def run_gates(workspace: Path, config: dict[str, Any], result: dict[str, Any], changed: list[str],
              base_text: Callable[[str], str | None]) -> dict[str, Any]:
    """Links, changelog and forbidden phrases. `changed` is the phase's whole delta so far;
    `base_text` reads a path as it was at the phase's base, or None if it did not exist."""
    settings, globs = section(config), patterns(config)
    failures: list[dict[str, Any]] = []
    links = broken_links(workspace, globs, lambda path: read_only(config, path))
    if links:
        failures.append({"gate": "links", "problems": links})
    changelog = settings.get("changelog")
    if changelog and result.get("user_visible"):
        heading = settings.get("unreleased_heading", "Unreleased")
        target = workspace / changelog
        current = target.read_text(errors="replace") if target.is_file() else ""
        entries = unreleased_entries(current, heading)
        before = unreleased_entries(base_text(changelog) or "", heading)
        if changelog not in changed or not entries or set(entries) <= set(before):
            failures.append({"gate": "changelog", "problems": [
                f"a user-visible change needs a new entry under \"{heading}\" in {changelog}"]})
    phrases = [item for item in settings.get("forbidden_phrases", []) if item.strip()]
    gained = []
    for path in sorted(changed):
        target = workspace / path
        if not matches(path, globs) or read_only(config, path) or not target.is_file():
            continue
        try:
            now_text = target.read_text().lower()
        except UnicodeDecodeError:
            continue  # images and other binaries under a documentation path carry no phrases
        was_text = (base_text(path) or "").lower()
        for phrase in phrases:
            if now_text.count(phrase.lower()) > was_text.count(phrase.lower()):
                gained.append(f"{path}: \"{phrase}\"")
    if gained:
        failures.append({"gate": "forbidden_phrases", "problems": gained})
    return {"passed": not failures, "failures": failures}


# --- the documenter --------------------------------------------------------------------------

class DocumenterAdapter:
    def __init__(self, role_runner: RoleRunner, snapshot: Path):
        self.role_runner, self.snapshot = role_runner, snapshot

    def brief(self) -> str | None:
        configured = section(self.role_runner.config).get("brief")
        path = self.snapshot / (configured or DEFAULT_BRIEF)
        return path.read_text() if path.is_file() else None

    def prompt(self, phase: Any, context: dict[str, Any], feedback: str | None = None) -> str:
        config = self.role_runner.config
        settings = section(config)
        rules = {"documentation_paths": list(patterns(config)), "changelog": settings.get("changelog"),
                 "read_only_governed_files": sorted({*governance_documents(config), "AGENTS.md", "CLAUDE.md",
                                                     "build.yaml", "prompts/", "templates/"}),
                 "unreleased_heading": settings.get("unreleased_heading", "Unreleased"),
                 "forbidden_phrases": settings.get("forbidden_phrases", [])}
        result = TRUSTED_PREAMBLE + "\n" + (PROTOCOL_ROOT / "prompts" / "documentation.md").read_text()
        result += "\nTRUSTED DOCUMENTATION RULES\n" + json.dumps(rules, indent=2, sort_keys=True) + "\n"
        brief = self.brief()
        if brief:
            result += untrusted("documentation_brief", brief)
        result += untrusted("phase_prompt", phase.prompt.read_text())
        result += untrusted("implementation_delta", context.get("diff", ""))
        result += untrusted("changed_files", json.dumps(context.get("changed_files", []), indent=2))
        result += untrusted("implementer_claims", json.dumps(context.get("phase_result", {}), indent=2))
        if feedback:
            result += ("\nYour previous documentation did not pass. Fix only what the feedback names, within"
                       " the documentation paths." + untrusted("documentation_feedback", feedback))
        return result + "\nPINNED OUTPUT CONTRACT\n" + contract("DOC_RESULT")

    def document(self, phase: Any, context: dict[str, Any], output: Path, timeout: float,
                 feedback: str | None = None) -> dict[str, Any]:
        value = self.role_runner.run("documenter", self.prompt(phase, context, feedback), output, timeout)
        return validate_result(value)


# --- what the reviewer is told ---------------------------------------------------------------

REVIEW_CRITERIA = """DOCUMENTATION CRITERIA (kernel-owned; they apply whatever review policy is in force)
This delta includes the documentation step. Besides the code, judge the documentation:
1. Every documented statement this phase touched matches the code in front of you.
2. Nothing this phase made stale is left stale: a command, flag, version, path or behavior the code changed is not still described the old way.
3. No persuasion has crept in. Docs describe what exists; marketing language is a defect.
`DOC_RESULT` is the documenter's own account of what it changed and why. Treat it like implementer claims: supplementary, never proof. A `no_change` result is valid only if its reason holds for this delta.
`documentation_rules` in the trusted binding are the operator's rules for the docs: the paths the documenter may change, the changelog and the forbidden phrases. They are trusted. A documenter that leaves something out because a rule forbids it is following the rule, not skipping the work. When a rule makes accurate documentation impossible (for example a forbidden phrase the feature's own name contains), file one finding with `kind` set to `documentation` and `treatment` set to `human`, naming the rule and the conflict in `rationale` and the operator's choice in `required_change` (change the rule, or accept the docs as they are). Do not ask the documenter to break the rule.
Every finding must cite a path in this delta's coverage. A documentation defect in a file this delta changed is filed against that file. A stale statement in a documentation file this delta did not change is filed against the changed code path that made it stale, with `kind` set to `documentation` and the documentation file named in `required_change`. Documentation findings go back to the documenter, not the implementer; when the code itself is wrong, file an ordinary finding on the code.
"""

def review_rules(config: dict[str, Any]) -> dict[str, Any]:
    """The documentation rules the reviewer may rely on: the operator's, from the trusted configuration."""
    settings = section(config)
    return {"paths": list(patterns(config)), "changelog": settings.get("changelog"),
            "unreleased_heading": settings.get("unreleased_heading", "Unreleased"),
            "forbidden_phrases": [item for item in settings.get("forbidden_phrases", []) if str(item).strip()],
            "mode": mode(config)}


SKIPPED_NOTE = ("DOCUMENTATION NOTE (kernel-owned)\nThe documentation step was switched off for this phase by "
                "configuration (`documentation: false`). Do not raise findings for documentation this phase "
                "did not change.\n")
