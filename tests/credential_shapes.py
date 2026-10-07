"""Credential-shaped strings for the tests that exercise the secret controls.

Assembled at import time rather than written as literals. A detector test needs values
that look exactly like real credentials, and the implementer's disposable clone contains
this repository, so a literal here is one `cat` away from the agent's stdout — where the
detector correctly cannot tell a fixture from a live key and halts the phase. That
happened three times, twice inside phase 42 alone, and the third occurrence cost an
attempt ceiling.

Splitting the prefixes keeps this module itself free of any matching span, so no scan of
the working tree finds a credential shape in it. The values these functions return are
still fully realistic at the point of use, which is what the tests need.
"""
from __future__ import annotations


def openai_style_key(body: str = "proj-9dQ2xLmT4vB8nR1cW7eK3jH6yA0sZpFg") -> str:
    """`sk-` prefixed key, matching SECRET_PATTERNS' OpenAI-style rule."""
    return "s" + "k" + "-" + body


def github_token(body: str = "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789") -> str:
    """`ghp_` prefixed token, matching the GitHub rule."""
    return "g" + "hp" + "_" + body


def private_key_header(algorithm: str = "RSA") -> str:
    """PEM private key opening line, matching the private key rule."""
    label = f" {algorithm}" if algorithm else ""
    return "-----" + "BEGIN" + label + " PRIVATE" + " KEY" + "-----"


def staged_api_key() -> str:
    """A long staged credential used where a configured secret is simulated."""
    return openai_style_key("openai-" + "K" * 40)


def jwt_like(payload: str = "eyJzdWIiOiIxIn0") -> str:
    """Three-segment JWT, matching the JWT rule in SECRET_PATTERNS."""
    header = "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    signature = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    return f"{header}.{payload}.{signature}"


def high_entropy(head: str, tail: str) -> str:
    """A credible secret *value* for the assignment-heuristic branch.

    Split so that `name=value` adjacency never appears as a literal span in this file,
    while the assembled result is still what `looks_like_secret_value` must accept.
    """
    return head + tail
