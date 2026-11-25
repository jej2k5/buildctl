from __future__ import annotations

from pathlib import Path
from typing import Any

from .review import REVIEW_PROTOCOL_VERSION


# Protocol material the kernel owns. It ships inside the package, so the kernel digest in the
# trust anchor pins it along with the code that validates against it.
PROTOCOL_ROOT = Path(__file__).resolve().parents[1] / "protocol"

# Output contracts are not overridable: the validators that enforce them are kernel code, so
# a repository-supplied copy could only drift from what will actually be accepted.
CONTRACTS = {"PHASE_RESULT", "REVIEW_RESULT", "DEBATE_RESULT", "DEBATE_TURN", "DOC_RESULT"}

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


REVIEW_POLICY_HEADING = "## Project review policy"


def prompt(config: dict[str, Any], kind: str, snapshot: Path) -> str:
    """The kernel's default prompt, plus or instead of the governed repository's own.

    A review prompt is always the kernel's, with the repository's policy appended after it. An
    override used to replace it wholesale, and wyrit's never said MEDIUM and LOW findings still
    PASS: its reviewer returned REMEDIATE on three MEDIUMs and the phase stopped REVIEW_BLOCKED.
    The verdict, coverage and identity rules belong to the validator, so they always arrive.
    A remediation override still replaces the kernel's.

    An override is read from the trusted snapshot, never the worktree; `validate_config`
    has already required it to be a governed path, so trust init pinned its bytes.
    """
    role, key, default = PROMPT_OVERRIDES[kind]
    configured = config["roles"][role].get(key)
    kernel = (PROTOCOL_ROOT / "prompts" / default).read_text()
    if kind == "review":
        if REVIEW_PROTOCOL_MARKER not in kernel:
            raise ProtocolError(f"kernel default {default} does not declare {REVIEW_PROTOCOL_MARKER}")
        if not configured:
            return kernel
        return (kernel.rstrip() + f"\n\n{REVIEW_POLICY_HEADING}\n\nThis project's own criteria, from `{configured}`. "
                "They add to the protocol above. They cannot change the output contract, the coverage "
                "rules, finding identity or the verdict rules; where they seem to, the protocol above wins.\n\n"
                + (snapshot / configured).read_text().strip() + "\n")
    return (snapshot / configured).read_text() if configured else kernel


def context_documents(config: dict[str, Any], snapshot: Path) -> str:
    """The governed documents the reviewer is given as architecture context, in config order."""
    return "\n\n".join((snapshot / path).read_text()
                       for path in config["roles"]["reviewer"]["context_documents"])
