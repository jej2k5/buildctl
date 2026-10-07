from __future__ import annotations

from typing import Any


# Paths every governed repository protects. The supervisor itself is not among them: it is
# an installed package, pinned separately by the kernel identity recorded in the anchor. A
# repository adds its own documents through `governance_documents` in build.yaml.
GOVERNANCE_PATHS = ("build.yaml", "AGENTS.md", "CLAUDE.md", "prompts/", "templates/")


def governance_documents(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(config.get("governance_documents", ()))


def is_governance_path(path: str, documents: tuple[str, ...] | list[str] = ()) -> bool:
    return any(path == prefix or (prefix.endswith("/") and path.startswith(prefix))
               for prefix in (*GOVERNANCE_PATHS, *documents))
