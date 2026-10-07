from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path


# High-precision credential *shapes*. These identify a secret by what the value looks like,
# never by a nearby word, so they are safe to match anywhere in agent output or source.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

# Candidate `credential-ish name = value` assignments. A match here is only a *candidate*:
# `looks_like_secret_value` decides, because agents that write source code legitimately emit
# lines such as `password = getpass()` all day. `(?!=)` keeps `==` comparisons out.
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|private[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*(?!=)(\S+)")

# Values that name a concept rather than carry a credential.
PLACEHOLDER_VALUES = frozenset({
    "none", "null", "nil", "undefined", "true", "false", "empty", "blank",
    "redacted", "masked", "hidden", "omitted", "removed", "sanitized",
    "changeme", "example", "placeholder", "todo", "tbd", "unset", "required", "optional",
    "xxx", "xxxx", "yyy", "zzz", "dummy", "fake", "test", "sample", "value", "string",
    "secret", "password", "token", "apikey", "key", "credential", "credentials",
})
# `response.token`, `os.environ`, `settings.SECRET_KEY` — a member-access path, not a literal.
DOTTED_REFERENCE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)+")
# A bare identifier such as `configuration_value` or `myConfiguredToken`. Real credentials
# are not snake_case and rarely lack digits, so those two shapes read as variable names.
BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MIN_SECRET_VALUE_LENGTH = 16
DOTTED_REFERENCE_MAX_LENGTH = 40

SECRET_FILENAMES = ("*.pem", "*.key", ".env", ".env.*", "id_rsa", "id_rsa.*")

# Tooling debris that a Python process leaves in whatever directory it runs from. None of it
# is phase content, but all of it lands in the disposable workspace and then in the phase
# delta — bytecode as unreviewable binaries, the pytest cache as an unattributed path.
BYTECODE_DIRECTORY = "__pycache__"
BYTECODE_SUFFIXES = (".pyc", ".pyo")
PYTEST_CACHE_DIRECTORY = ".pytest_cache"
# Exactly what pytest's cacheprovider writes: three marker files plus the `v/` data subtree.
PYTEST_CACHE_FILES = (".gitignore", "CACHEDIR.TAG", "README.md")
PYTEST_CACHE_SUBTREE = "v"
# Never traversed: the agent runtime holds staged credentials, and .git is not phase content.
UNTRAVERSED_DIRECTORIES = (".git", ".factory-runtime")


class UnsafeCleanup(RuntimeError):
    """A cleanup candidate could not be proven safe to remove."""


def suppress_ephemeral_artifacts(environment: dict[str, str]) -> dict[str, str]:
    """Stop a supervisor-launched Python process littering the phase workspace.

    The single mechanism for this, shared by every role the supervisor launches into a
    workspace. Mutates and returns the environment so it composes with the existing
    `sanitized_environment` / `host_auth_environment` construction.

    `PYTEST_ADDOPTS` is the environment equivalent of passing `-p no:cacheprovider`: it
    reaches pytest however the agent chooses to invoke it, which a fixed gate argv cannot.
    """
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return environment


def _contained(root: Path, path: Path) -> bool:
    """True only when `path` lies inside `root` with no symlink anywhere on the way down."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _bytecode_only(root: Path, directory: Path) -> bool:
    """Does this directory hold nothing but contained, regular bytecode files?"""
    for entry in directory.rglob("*"):
        if not _contained(root, entry):
            return False
        if entry.is_dir():
            continue
        if not entry.is_file() or entry.suffix not in BYTECODE_SUFFIXES:
            return False
    return True


def _pytest_cache_only(root: Path, directory: Path) -> bool:
    """Does this directory hold nothing but pytest's own cache artifacts?

    Anything else — a source file, a stray binary — means this is not simply a tool cache,
    so the caller refuses to delete it rather than guessing.
    """
    for entry in directory.rglob("*"):
        if not _contained(root, entry):
            return False
        relative = entry.relative_to(directory)
        if entry.is_dir():
            if relative.parts[0] != PYTEST_CACHE_SUBTREE:
                return False
            continue
        if not entry.is_file():
            return False
        if relative.parts[0] != PYTEST_CACHE_SUBTREE and relative.as_posix() not in PYTEST_CACHE_FILES:
            return False
    return True


# Disposable directories, each paired with the proof its contents really are that artifact.
EPHEMERAL_DIRECTORIES = {BYTECODE_DIRECTORY: _bytecode_only,
                         PYTEST_CACHE_DIRECTORY: _pytest_cache_only}


def purge_ephemeral_artifacts(workspace: Path) -> tuple[str, ...]:
    """Remove Python tooling debris from a disposable phase workspace.

    Deliberately narrow: only the two directory names in `EPHEMERAL_DIRECTORIES` and loose
    `.pyc`/`.pyo` files, only inside the workspace, and never through a symlink. A candidate
    that cannot be shown to be exactly that raises instead of being deleted, so this can
    neither reach outside the workspace nor quietly remove agent-authored work before it is
    reviewed. Every other unattributed path still fails the phase.
    """
    root = workspace.resolve(strict=True)
    caches: list[Path] = []
    strays: list[Path] = []
    for current, subdirectories, files in os.walk(root, followlinks=False):
        here = Path(current)
        subdirectories[:] = [name for name in subdirectories
                             if name not in UNTRAVERSED_DIRECTORIES and not (here / name).is_symlink()]
        caches.extend(here / name for name in subdirectories if name in EPHEMERAL_DIRECTORIES)
        subdirectories[:] = [name for name in subdirectories if name not in EPHEMERAL_DIRECTORIES]
        strays.extend(here / name for name in files if Path(name).suffix in BYTECODE_SUFFIXES)
    removed: list[str] = []
    for directory in caches:
        if not _contained(root, directory):
            raise UnsafeCleanup(f"disposable directory escapes the workspace: {directory}")
        if not EPHEMERAL_DIRECTORIES[directory.name](root, directory):
            raise UnsafeCleanup(f"{directory.name} holds content that is not a generated artifact, "
                                f"refusing to delete it: {directory.relative_to(root).as_posix()}")
        removed.extend(sorted(path.relative_to(root).as_posix()
                              for path in directory.rglob("*") if path.is_file()))
        shutil.rmtree(directory)
    for candidate in strays:
        if not _contained(root, candidate) or not candidate.is_file():
            raise UnsafeCleanup(f"bytecode file is not a contained regular file: {candidate}")
        removed.append(candidate.relative_to(root).as_posix())
        candidate.unlink()
    return tuple(sorted(removed))


def looks_like_secret_value(raw: str) -> bool:
    """Does this assigned value plausibly carry a credential, rather than name one?

    Structural and deterministic: no entropy estimation, no wordlists beyond an explicit
    placeholder set. Errs towards *not* flagging, because exact staged-secret matching —
    not this heuristic — is the authoritative protection for known credentials.
    """
    value = raw.strip().strip("\"'`").rstrip(",;.").strip("\"'`")
    if not value or value.startswith("<") or value.endswith(">"):
        return False                                   # <redacted>, <your-token-here>
    if value.startswith("$") or value.startswith("%"):
        return False                                   # $TOKEN, ${VAR}, %VAR%
    if any(character in value for character in "()[]{}"):
        return False                                   # getpass(), env["KEY"], {...}
    if "=" in value or "," in value:
        return False                                   # comparisons and argument lists
    if set(value) <= set(".-_*#"):
        return False                                   # ..., ---, ***, ###
    if value.strip("._-").lower() in PLACEHOLDER_VALUES:
        return False
    if len(value) < MIN_SECRET_VALUE_LENGTH:
        return False                                   # too short to be a real credential
    if len(value) < DOTTED_REFERENCE_MAX_LENGTH and DOTTED_REFERENCE.fullmatch(value):
        return False                                   # response.token, settings.SECRET_KEY
    if BARE_IDENTIFIER.fullmatch(value) and ("_" in value or not any(c.isdigit() for c in value)):
        return False                                   # api_response_data_v2, myConfiguredToken
    classes = sum(bool(re.search(expression, value))
                  for expression in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 or len(value) >= 32


def matches_secret_pattern(text: str) -> bool:
    """True when text carries a credential-shaped value or a credible secret assignment."""
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return True
    return any(looks_like_secret_value(match.group(2)) for match in SECRET_ASSIGNMENT.finditer(text))



def _redact_assignment(match: re.Match) -> str:
    """Redact only the value, and only when it plausibly carries a credential."""
    if not looks_like_secret_value(match.group(2)):
        return match.group(0)
    return match.group(0).replace(match.group(2), "[REDACTED]")


def redact(value: str, secrets: list[str] | None = None, limit: int = 4000) -> str:
    text = value[:limit]
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return SECRET_ASSIGNMENT.sub(_redact_assignment, text)


def contains_secret(value: str, secrets: list[str] | None = None) -> bool:
    if any(secret and secret in value for secret in secrets or []):
        return True
    return matches_secret_pattern(value)


def ensure_safe_relative(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe path: {relative}")
    target = root / path
    resolved_parent = target.parent.resolve()
    if not resolved_parent.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes root: {relative}")
    if target.is_symlink():
        raise ValueError(f"symlink is not allowed: {relative}")
    return target


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secret_shaped(path: str) -> bool:
    candidate = Path(path)
    return any(candidate.match(pattern) for pattern in SECRET_FILENAMES)
