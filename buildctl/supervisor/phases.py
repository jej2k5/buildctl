from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import ensure_safe_relative
from .operations import PAID_OPERATION_IDS
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
    return config


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
    roles = config.get("roles", {})
    if set(roles) != {"implementer", "reviewer", "arbiter"}:
        raise ConfigurationError("roles must declare implementer, reviewer, and arbiter")
    for name, role in roles.items():
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
        if role["auth_mode"] == "host_cli" and role["credential_env"]:
            raise ConfigurationError(f"role {name} host_cli authentication cannot use credential_env")
        if role["auth_mode"] == "none" and role["credential_env"]:
            raise ConfigurationError(f"role {name} none authentication cannot use credential_env")
    if roles["implementer"].get("auth_mode") == "host_cli" and "--ignore-user-config" not in roles["implementer"]["command"]:
        raise ConfigurationError("host_cli Codex implementer must ignore ambient user config")
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
    costs = config.get("operation_cost_usd")
    if not isinstance(costs, dict) or set(costs) != PAID_OPERATION_IDS:
        raise ConfigurationError("operation_cost_usd must define exactly every paid operation")
    if any(not isinstance(value, (int, float)) or value < 0 for value in costs.values()):
        raise ConfigurationError("operation costs must be explicit non-negative numbers")


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
                            meta["merge_policy"], tuple(meta["allowed_paths"])))
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
