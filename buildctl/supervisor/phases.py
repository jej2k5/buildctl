from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import documentation
from .evidence import validate_evidence_config
from .gates import ALLOWED_DIRECT_REFERENCE, ProvisioningError, constraint_bounds
from .governance import is_governance_path
from .safety import ensure_safe_relative
from .operations import DEBATE_OPERATION_IDS, DOCUMENTATION_OPERATION_IDS, PAID_OPERATION_IDS
from .review import REVIEW_PROTOCOL_VERSION


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    prompt: Path
    required_files: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]
    invariants: tuple[str, ...]
    claude_review_required: bool
    debate_on_architecture_disagreement: bool
    max_remediation_attempts: int
    merge_policy: str
    allowed_paths: tuple[str, ...]
    # False switches the documentation step off for this phase (test-only or tooling changes).
    documentation: bool = True


class ConfigurationError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        return load_config_text(path.read_text(), path.parent)
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc


def load_config_text(text: str, root: Path) -> dict[str, Any]:
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON-compatible YAML: {exc}") from exc
    validate_config(config, root)
    documentation.materialize_documenter(config)
    return config


def host_cli_supported() -> bool:
    """host_cli reads a personal login on the operator's machine, which only macOS has.

    Linux hosts are servers, containers and CI runners: agents there authenticate per
    operation with keys or cloud identity, never with someone's interactive session.
    """
    return platform.system() == "Darwin"


def validate_config(config: dict[str, Any], root: Path) -> None:
    required_limits = {
        "max_phase_attempts", "max_review_format_attempts", "max_semantic_review_attempts",
        "max_debate_rounds", "max_phase_minutes", "max_daily_cost_usd", "max_consecutive_failures",
    }
    limits = config.get("limits", {})
    missing = required_limits - set(limits)
    if missing or any(not isinstance(limits.get(key), (int, float)) or limits[key] <= 0 for key in required_limits):
        raise ConfigurationError(f"invalid or missing safety limits: {', '.join(sorted(missing))}")
    git = config.get("git", {})
    if set(("push", "create_pr", "merge")) - set(git) or any(not isinstance(git[key], bool) for key in ("push", "create_pr", "merge")):
        raise ConfigurationError("git publication flags must be explicit booleans")
    branch = git.get("integration_branch", "main")
    if (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or ".." in branch or branch.endswith((".", "/", ".lock")) or "//" in branch):
        raise ConfigurationError("git integration_branch must be a plain branch name")
    checkpoints = config.get("checkpoints", {})
    if not isinstance(checkpoints.get("before_phases"), list) or not isinstance(checkpoints.get("mvp_completion"), int):
        raise ConfigurationError("checkpoints must define before_phases and mvp_completion")
    defaults = config.get("phase_defaults", {})
    required_metadata = {"required_files", "commands", "invariants", "claude_review_required",
                         "debate_on_architecture_disagreement", "max_remediation_attempts",
                         "merge_policy", "allowed_paths"}
    if required_metadata - set(defaults):
        raise ConfigurationError("phase_defaults does not define the complete acceptance contract")
    for command in defaults["commands"]:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ConfigurationError("gate commands must be non-empty argv arrays")
    phases = config.get("phases", [])
    numbers = [item.get("number") for item in phases]
    if not phases or numbers != sorted(set(numbers)):
        raise ConfigurationError("phase numbers must be unique and ordered")
    if any(value not in numbers for value in checkpoints["before_phases"] + [checkpoints["mvp_completion"]]):
        raise ConfigurationError("checkpoint references an unknown phase")
    for item in phases:
        prompt = ensure_safe_relative(root, item["prompt"])
        if not prompt.is_file():
            raise ConfigurationError(f"phase prompt not found: {prompt}")
        metadata = merged_metadata(defaults, item)
        if metadata["merge_policy"] not in {"auto", "human"}:
            raise ConfigurationError("merge_policy must be auto or human")
        for path in metadata["required_files"] + metadata["allowed_paths"]:
            ensure_safe_relative(root, path.rstrip("/"))
        problems = documentation.validate_phase_switch(item)
        if problems:
            raise ConfigurationError("; ".join(problems))
    problems = documentation.validate_section(config, root)
    if problems:
        raise ConfigurationError("; ".join(problems))
    documenting = documentation.enabled(config)
    roles = config.get("roles", {})
    if set(roles) - {"documenter"} != {"implementer", "reviewer", "arbiter"}:
        raise ConfigurationError("roles must declare implementer, reviewer, and arbiter")
    if "documenter" in roles and not documenting:
        raise ConfigurationError("roles.documenter is configured but documentation.mode is off")
    debate = roles["arbiter"].get("mode") == "debate"
    if "max_review_file_bytes" in limits and (not isinstance(limits["max_review_file_bytes"], int)
                                              or limits["max_review_file_bytes"] < 1000):
        raise ConfigurationError("limits.max_review_file_bytes must be a whole number of at least 1000")
    if "max_debate_turns" in limits and (not isinstance(limits["max_debate_turns"], int)
                                          or limits["max_debate_turns"] < 2):
        raise ConfigurationError("limits.max_debate_turns must be a whole number of at least 2")
    for name, role in roles.items():
        if name == "arbiter" and debate:
            continue  # The debate's turns run as the implementer and the reviewer.
        command = role.get("command")
        if not isinstance(command, list) or not command:
            raise ConfigurationError(f"role {name} requires an argv command")
        if not isinstance(role.get("network"), bool):
            raise ConfigurationError(f"role {name} requires an explicit network boolean")
        if not isinstance(role.get("credential_env"), list) or not all(
                isinstance(item, str) and item.endswith(("_API_KEY", "_TOKEN")) for item in role["credential_env"]):
            raise ConfigurationError(f"role {name} requires an explicit credential_env allowlist")
        if role.get("auth_mode") not in {"host_cli", "environment", "none"}:
            raise ConfigurationError(f"role {name} requires a supported explicit auth_mode")
        if role["auth_mode"] == "host_cli" and not host_cli_supported():
            raise ConfigurationError(f"role {name}: auth_mode host_cli is macOS only; use environment")
        if role["auth_mode"] == "environment" and role["credential_env"] and role["network"] is False:
            raise ConfigurationError(f"role {name}: credentials in credential_env mean it calls an API, so it "
                                     "needs network true")
        if role["auth_mode"] == "host_cli" and role["network"] is False:
            # wyrit's reviewer had network false and died ENOTFOUND reaching its API, after
            # `validate` had passed. A role that logs in through its CLI calls the vendor's API.
            raise ConfigurationError(f"role {name}: auth_mode host_cli needs network true, because the "
                                     "CLI calls its provider's API")
        if role["auth_mode"] == "host_cli" and role["credential_env"]:
            raise ConfigurationError(f"role {name} host_cli authentication cannot use credential_env")
        if role["auth_mode"] == "none" and role["credential_env"]:
            raise ConfigurationError(f"role {name} none authentication cannot use credential_env")
    if roles["implementer"].get("auth_mode") == "host_cli" and "--ignore-user-config" not in roles["implementer"]["command"]:
        raise ConfigurationError("host_cli Codex implementer must ignore ambient user config")
    # The documenter writes, so host_cli means the staged Codex login, the same as the implementer.
    if roles.get("documenter", {}).get("auth_mode") == "host_cli" and \
            "--ignore-user-config" not in roles["documenter"]["command"]:
        raise ConfigurationError("host_cli documenter must be a Codex command that ignores ambient user config")
    if roles["reviewer"].get("protocol_version") != REVIEW_PROTOCOL_VERSION:
        raise ConfigurationError(f"reviewer protocol_version must be {REVIEW_PROTOCOL_VERSION}")
    if roles["reviewer"].get("auth_mode") == "host_cli":
        reviewer_command = roles["reviewer"].get("command", [])
        forbidden = {"Bash", "Edit", "Write", "NotebookEdit", "Web", "MCP"}
        command_text = " ".join(str(item) for item in reviewer_command)
        if "--no-session-persistence" not in reviewer_command or "--tools" not in reviewer_command:
            raise ConfigurationError("reviewer must be non-persistent and tool-restricted")
        if any(tool in command_text for tool in forbidden):
            raise ConfigurationError("reviewer command contains a mutation capability")
    evidence_problems = validate_evidence_config(config)
    if evidence_problems:
        raise ConfigurationError("; ".join(evidence_problems))
    validate_governed_inputs(config, root)
    validate_python(config, root)
    validate_demo(config, root)
    costs = config.get("operation_cost_usd")
    # Only a build whose arbiter runs debates pays per turn, so only it must price one; only a
    # build with documentation on pays for documentation.
    optional = (set() if debate else DEBATE_OPERATION_IDS) | (set() if documenting else DOCUMENTATION_OPERATION_IDS)
    required = PAID_OPERATION_IDS - optional
    if not isinstance(costs, dict) or not required <= set(costs) <= PAID_OPERATION_IDS:
        raise ConfigurationError("operation_cost_usd must define exactly every paid operation")
    if any(not isinstance(value, (int, float)) or value < 0 for value in costs.values()):
        raise ConfigurationError("operation costs must be explicit non-negative numbers")


def _governed_file(root: Path, path: Any, documents: tuple[str, ...], what: str) -> None:
    if not isinstance(path, str) or not path:
        raise ConfigurationError(f"{what} must be a non-empty path")
    try:
        target = ensure_safe_relative(root, path)
    except ValueError as exc:
        raise ConfigurationError(f"{what} is not a safe relative path: {path}") from exc
    if not is_governance_path(path, documents):
        # Only governed paths are copied into the trusted snapshot. Anything else would be
        # read from nowhere, or worse, from the agent-writable worktree.
        raise ConfigurationError(f"{what} must be a governed path (under prompts/ or listed in "
                                 f"governance_documents): {path}")
    if not target.is_file():
        raise ConfigurationError(f"{what} not found: {path}")


def validate_governed_inputs(config: dict[str, Any], root: Path) -> None:
    """Repository-supplied inputs the kernel reads: governance documents, reviewer context and
    prompt overrides. Each must be governed, so trust init pins its bytes."""
    documents = config.get("governance_documents")
    if not isinstance(documents, list):
        raise ConfigurationError("governance_documents must be declared as a list of paths "
                                 "(it may be empty)")
    for path in documents:
        if not isinstance(path, str) or not path or path.endswith("/"):
            raise ConfigurationError(f"governance_documents entries must be file paths: {path!r}")
    documents = tuple(documents)
    for path in documents:
        _governed_file(root, path, documents, "governance document")
    reviewer = config["roles"]["reviewer"]
    context = reviewer.get("context_documents")
    if not isinstance(context, list):
        raise ConfigurationError("roles.reviewer.context_documents must be declared as a list of "
                                 "paths (it may be empty)")
    for path in context:
        _governed_file(root, path, documents, "reviewer context document")
    for role, key in (("reviewer", "review_prompt"), ("implementer", "remediation_prompt")):
        if key in config["roles"][role]:
            _governed_file(root, config["roles"][role][key], documents, f"roles.{role}.{key}")


DEMO_KEYS = {"command", "url", "ready_pattern", "scenario", "timeout_seconds", "viewport"}
DEMO_HOSTS = ("http://127.0.0.1:{port}", "http://localhost:{port}")


def validate_demo(config: dict[str, Any], root: Path) -> None:
    """The optional `demo` section: how buildctl-drafts starts the product on fictional data for a recording.

    The command runs in a disposable copy of a milestone's commit, sandboxed with loopback only and a
    sanitized environment, so it never sees a credential. `{port}` is the loopback port it must serve
    on; `ready_pattern`, when set, is a regex whose first group is the URL to open (for a URL with a
    token), read from the command's output."""
    if "demo" not in config:
        return
    section = config["demo"]
    if not isinstance(section, dict) or set(section) - DEMO_KEYS:
        raise ConfigurationError(f"demo may set only {', '.join(sorted(DEMO_KEYS))}")
    command = section.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ConfigurationError("demo command must be a non-empty argv array")
    if not any("{port}" in part for part in command):
        raise ConfigurationError("demo command must take its port as {port}")
    url = section.get("url", DEMO_HOSTS[0] + "/")
    if not isinstance(url, str) or not url.startswith(DEMO_HOSTS):
        raise ConfigurationError("demo url must be http://127.0.0.1:{port}/... or http://localhost:{port}/...")
    pattern = section.get("ready_pattern")
    if pattern is not None:
        try:
            if not isinstance(pattern, str) or re.compile(pattern).groups < 1:
                raise ConfigurationError("demo ready_pattern must be a regex with one group, the URL")
        except re.error as exc:
            raise ConfigurationError(f"demo ready_pattern is not a valid regex: {exc}") from exc
    scenario = section.get("scenario", "docs/demo/scenario.json")
    if not isinstance(scenario, str):
        raise ConfigurationError("demo scenario must be a path in the repository")
    try:
        ensure_safe_relative(root, scenario)
    except ValueError as exc:
        raise ConfigurationError(f"demo scenario must stay inside the repository: {exc}") from exc
    timeout = section.get("timeout_seconds", 120)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 10 <= timeout <= 900:
        raise ConfigurationError("demo timeout_seconds must be a whole number from 10 to 900")
    viewport = section.get("viewport", [1280, 800])
    if (not isinstance(viewport, list) or len(viewport) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and 320 <= n <= 3840 for n in viewport)):
        raise ConfigurationError("demo viewport must be [width, height] in pixels, 320 to 3840")


def validate_python(config: dict[str, Any], root: Path) -> None:
    """The optional `python` section: how `{python}` gates provision a real project (seam 4)."""
    if "python" not in config:
        return
    section = config["python"]
    if not isinstance(section, dict) or set(section) - {"extras", "constraints", "direct_references"}:
        raise ConfigurationError("python may set only extras, constraints and direct_references")
    extras = section.get("extras", [])
    if not isinstance(extras, list) or not all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item)
                                               for item in extras):
        raise ConfigurationError("python.extras must be a list of extra names")
    if "constraints" in section:
        _governed_file(root, section["constraints"], tuple(config.get("governance_documents", ())),
                       "python.constraints")
        for number, line in enumerate((root / section["constraints"]).read_text().splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if line.startswith("-"):
                # constraint_bounds skips option lines, but pip would obey them (--index-url).
                raise ConfigurationError(f"python.constraints line {number} is a pip option; only requirements are allowed")
            if line:
                try:
                    constraint_bounds(line)
                except ProvisioningError as exc:
                    raise ConfigurationError(f"python.constraints line {number}: {exc}") from exc
    references = section.get("direct_references", [])
    if not isinstance(references, list) or not all(isinstance(item, str) and ALLOWED_DIRECT_REFERENCE.match(item.strip())
                                                   for item in references):
        raise ConfigurationError("python.direct_references entries must be 'name @ https://...whl#sha256=<64 hex>': "
                                 "a pre-built wheel pinned by its hash")


def merged_metadata(defaults: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    result.update(item.get("acceptance", {}))
    result["required_files"] = [value.format(phase=item["number"]) for value in result["required_files"]]
    result["allowed_paths"] = [value.format(phase=item["number"]) for value in result["allowed_paths"]]
    return result


def phases_from(config: dict[str, Any], root: Path) -> list[Phase]:
    result = []
    for item in config["phases"]:
        meta = merged_metadata(config["phase_defaults"], item)
        result.append(Phase(item["number"], item["name"], root / item["prompt"],
                            tuple(meta["required_files"]), tuple(tuple(x) for x in meta["commands"]),
                            tuple(meta["invariants"]), meta["claude_review_required"],
                            meta["debate_on_architecture_disagreement"], meta["max_remediation_attempts"],
                            meta["merge_policy"], tuple(meta["allowed_paths"]),
                            item.get("documentation", True)))
    return result


def resolve_until(config: dict[str, Any], value: str | None) -> int | None:
    if value is None:
        return None
    candidate = config.get("milestones", {}).get(value.lower(), value)
    try:
        number = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"unknown phase or milestone: {value}") from exc
    valid = {phase["number"] for phase in config["phases"]}
    if number not in valid:
        raise ConfigurationError(f"phase is out of configured range: {number}")
    return number
