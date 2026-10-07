from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable


class AuthenticationRequired(RuntimeError):
    def __init__(self, provider: str, command: str, detail: str = ""):
        self.provider, self.command, self.detail = provider, command, detail
        super().__init__(f"AUTHENTICATION_REQUIRED: run {command}")


class HostAuthUnsupported(RuntimeError):
    pass


def _run(argv: list[str], runner: Callable[..., Any], provider: str, remediation: str) -> Any:
    try:
        return runner(argv, text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthenticationRequired(provider, remediation) from exc


def parse_codex_status(output: str, returncode: int = 0) -> dict[str, Any]:
    match = re.search(r"logged in using\s+(.+?)\s*$", output, re.IGNORECASE | re.MULTILINE)
    if returncode != 0 or not match:
        raise AuthenticationRequired("openai", "codex login")
    return {"authenticated": True, "provider": "openai", "auth_method": match.group(1).strip()}


def parse_claude_status(output: str, returncode: int = 0) -> dict[str, Any]:
    if returncode != 0:
        raise AuthenticationRequired("anthropic", "claude auth login")
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuthenticationRequired("anthropic", "claude auth login") from exc
    if not isinstance(value, dict) or value.get("loggedIn") is not True:
        raise AuthenticationRequired("anthropic", "claude auth login")
    result = {"authenticated": True, "provider": "anthropic",
              "auth_method": str(value.get("authMethod", "unknown"))}
    if value.get("subscriptionType") is not None:
        result["subscription_type"] = str(value["subscriptionType"])
    return result


class AuthPreflight:
    def __init__(self, runner: Callable[..., Any] = subprocess.run,
                 config: dict[str, Any] | None = None):
        self.runner, self.config = runner, config or {}

    def check(self) -> dict[str, Any]:
        codex = _run(["codex", "login", "status"], self.runner, "openai", "codex login")
        codex_result = parse_codex_status((codex.stdout or "") + (codex.stderr or ""), codex.returncode)
        claude = _run(["claude", "auth", "status", "--json"], self.runner, "anthropic", "claude auth login")
        claude_result = parse_claude_status(claude.stdout, claude.returncode)
        return {"status": "PASS", "implementer": codex_result, "reviewer": claude_result}


def _read_codex_auth(host_home: Path | None = None) -> tuple[Path, bytes, dict[str, Any]]:
    host_home = (host_home or Path.home()).resolve()
    codex_dir = host_home / ".codex"
    if codex_dir.is_symlink() or not codex_dir.is_dir():
        raise AuthenticationRequired("openai", "codex login")
    source = codex_dir / "auth.json"
    if source.is_symlink() or not source.is_file():
        raise AuthenticationRequired("openai", "codex login")
    if stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise HostAuthUnsupported("Codex auth.json must not be group/world accessible")
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise HostAuthUnsupported("Codex auth.json is invalid") from exc
    tokens = value.get("tokens") if isinstance(value, dict) else None
    required = ("access_token", "refresh_token", "id_token", "account_id")
    if not isinstance(value, dict) or value.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict) \
            or any(not isinstance(tokens.get(key), str) or not tokens[key] for key in required):
        raise HostAuthUnsupported("Codex auth.json is not a supported ChatGPT login")
    return source, raw, value


def validate_codex_auth(host_home: Path | None = None) -> None:
    _read_codex_auth(host_home)


# Values that actually bear credentials and must never appear in agent output. `account_id`
# is deliberately excluded: it is an account *identifier*, not credential material, and a
# 36-character UUID is short enough to appear legitimately in provider output, which would
# abort an otherwise healthy phase.
CREDENTIAL_BEARING_TOKENS = ("access_token", "refresh_token", "id_token")
STAGED_AUTH_FIELDS = ("access_token", "refresh_token", "id_token", "account_id")


def stage_codex_auth(isolated_home: Path, host_home: Path | None = None) -> tuple[Path, dict[str, str]]:
    _, raw, value = _read_codex_auth(host_home)
    target_dir = isolated_home.resolve() / ".codex"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_dir, 0o700)
    target = target_dir / "auth.json"
    if target.is_symlink():
        raise HostAuthUnsupported("staged Codex auth path is a symlink")
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    # account_id stays in the staged auth.json because Codex may require it; it is simply
    # not treated as credential material for leak detection.
    tokens = value["tokens"]
    secrets = {key: tokens[key] for key in CREDENTIAL_BEARING_TOKENS}
    if isinstance(value.get("OPENAI_API_KEY"), str) and value["OPENAI_API_KEY"]:
        secrets["api_key"] = value["OPENAI_API_KEY"]
    return target, secrets


def cleanup_staged_auth(isolated_home: Path) -> None:
    codex = (isolated_home.resolve() / ".codex")
    if not codex.exists():
        return
    if codex.is_symlink() or not codex.is_dir() or not codex.is_relative_to(isolated_home.resolve()):
        raise HostAuthUnsupported("staged authentication path escaped operation home")
    shutil.rmtree(codex)
