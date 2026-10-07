from __future__ import annotations

from pathlib import Path
from typing import Any

from .review import REVIEW_PROTOCOL_VERSION


# Protocol material the kernel owns. It ships inside the package, so the kernel digest in the
# trust anchor pins it along with the code that validates against it.
PROTOCOL_ROOT = Path(__file__).resolve().parents[1] / "protocol"

# Output contracts are not overridable: the validators that enforce them are kernel code, so
# a repository-supplied copy could only drift from what will actually be accepted.
CONTRACTS = {"PHASE_RESULT", "REVIEW_RESULT", "DEBATE_RESULT"}

# Prompts a governed repository may override, as (role, config key, kernel default).
PROMPT_OVERRIDES = {"review": ("reviewer", "review_prompt", "review.md"),
                    "remediation": ("implementer", "remediation_prompt", "remediation.md")}
REVIEW_PROTOCOL_MARKER = f"Protocol version: `{REVIEW_PROTOCOL_VERSION}`"


class ProtocolError(RuntimeError):
    pass


def contract(name: str) -> str:
    """The pinned output contract shown to an agent."""
    if name not in CONTRACTS:
        raise ProtocolError(f"unknown output contract: {name}")
    return (PROTOCOL_ROOT / "templates" / f"{name}.json").read_text()


def prompt(config: dict[str, Any], kind: str, snapshot: Path) -> str:
    """The kernel's default prompt, or the governed repository's configured override.

    An override is read from the trusted snapshot, never the worktree; `validate_config`
    has already required it to be a governed path, so trust init pinned its bytes.
    """
    role, key, default = PROMPT_OVERRIDES[kind]
    configured = config["roles"][role].get(key)
    text = (snapshot / configured).read_text() if configured else (PROTOCOL_ROOT / "prompts" / default).read_text()
    if kind == "review" and REVIEW_PROTOCOL_MARKER not in text:
        source = configured or f"kernel default {default}"
        raise ProtocolError(f"reviewer prompt {source} does not declare {REVIEW_PROTOCOL_MARKER}")
    return text


def context_documents(config: dict[str, Any], snapshot: Path) -> str:
    """The governed documents the reviewer is given as architecture context, in config order."""
    return "\n\n".join((snapshot / path).read_text()
                       for path in config["roles"]["reviewer"]["context_documents"])
