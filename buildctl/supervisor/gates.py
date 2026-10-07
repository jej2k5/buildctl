from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .isolation import IsolationBackend, IsolationContext, sanitized_environment
from .safety import contains_secret, ensure_safe_relative, redact, suppress_ephemeral_artifacts


# Acceptance commands request the provisioned product interpreter with this token. A phase
# whose tests need declared third-party dependencies must use it: the gate sandbox has no
# network, so nothing can be installed once the command is already running.
PYTHON_PLACEHOLDER = "{python}"
PRODUCT_MANIFEST = "pyproject.toml"
PROVISION_TIMEOUT = 900.0


@dataclass
class GateResult:
    passed: bool
    details: list[dict[str, Any]]


class ProvisioningError(RuntimeError):
    """The declared product dependency environment could not be built."""


# A requirement must constrain its upper end. Any of these operators bounds it; a bare name
# or a lower bound alone resolves to whatever the index happens to serve at install time.
UPPER_BOUND_OPERATORS = ("===", "==", "~=", "<=", "<")
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _requirement_specifier(value: str) -> tuple[str, str]:
    """Split a requirement into its distribution name and version specifier.

    Rejects the direct-reference form (`name @ url`) outright: it names an arbitrary
    location rather than a distribution the index resolves, which is a supply-chain
    vector no product dependency here needs.
    """
    base = value.split(";", 1)[0].strip()          # drop any environment marker
    if "@" in base:
        raise ProvisioningError(
            f"dependency may not use a direct URL reference: {value}")
    match = REQUIREMENT_NAME.match(base)
    if not match:
        raise ProvisioningError(f"dependency is not a valid requirement: {value}")
    remainder = base[match.end():].strip()
    if remainder.startswith("["):                  # extras, e.g. psycopg[binary]
        close = remainder.find("]")
        if close == -1:
            raise ProvisioningError(f"dependency has unterminated extras: {value}")
        remainder = remainder[close + 1:].strip()
    return match.group(0), remainder.replace(" ", "")


def _is_upper_bounded(specifier: str) -> bool:
    """Does any clause of this specifier constrain the upper end?"""
    return any(clause.startswith(UPPER_BOUND_OPERATORS)
               for clause in specifier.split(",") if clause)


def product_dependencies(raw: bytes) -> tuple[str, ...]:
    """Read `[project].dependencies` out of the product manifest.

    Parsing only — `tomllib` evaluates no code, so an untrusted agent-authored manifest
    cannot execute anything here. Each requirement is validated as a plain requirement
    string so a manifest cannot smuggle pip options into the install command.

    Every requirement must also be upper-bounded. Phase 11 declared a bare `httpx2` — an
    unpinned distribution that nothing imported, was not the canonical HTTP client, and was
    installed regardless, because this function previously checked syntax and not
    provenance. An unbounded requirement resolves to whatever the index serves at install
    time, so a manifest that passes review once can install different code later.
    """
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProvisioningError(f"{PRODUCT_MANIFEST} is not valid TOML: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise ProvisioningError(f"{PRODUCT_MANIFEST} declares no [project] table")
    declared = project.get("dependencies", [])
    if not isinstance(declared, list):
        raise ProvisioningError("[project].dependencies must be an array of requirement strings")
    requirements: list[str] = []
    for entry in declared:
        if not isinstance(entry, str) or not entry.strip():
            raise ProvisioningError("[project].dependencies must contain non-empty strings")
        value = entry.strip()
        if value.startswith("-"):
            raise ProvisioningError(f"dependency entry may not be a pip option: {value}")
        name, specifier = _requirement_specifier(value)
        if not _is_upper_bounded(specifier):
            raise ProvisioningError(
                f"dependency '{name}' is not upper-bounded: {value!r}. Declare a bound such "
                f"as '{name}>=X,<Y' or '{name}==X.Y.Z' so the resolved distribution cannot "
                f"change between installs.")
        requirements.append(value)
    return tuple(requirements)


def provisioning_environment() -> dict[str, str]:
    """Environment for the supervisor-side dependency install.

    `PIP_CONFIG_FILE` is neutralised deliberately: an operator-level `global.target` in
    pip.conf redirects installs out of the virtual environment and into a shared host
    site-packages, which would both poison the host and leave the gate environment empty.
    `PIP_REQUIRE_VIRTUALENV` turns any residual attempt to install outside the environment
    into a loud failure rather than a silent one.
    """
    allow = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "HOME", "TMPDIR")
    env = {key: os.environ[key] for key in allow if key in os.environ}
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_REQUIRE_VIRTUALENV": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1"})
    return env


class MechanicalGates:
    def __init__(self, workspace: Path, control_root: Path, authoritative_repo: Path,
                 backend: IsolationBackend, runner: Callable[..., Any] = subprocess.run):
        self.workspace, self.control_root, self.authoritative_repo = workspace, control_root, authoritative_repo
        self.backend, self.runner = backend, runner
        # (manifest digest, interpreter). Rebuilt when the agent edits the manifest between
        # remediation rounds, reused otherwise so one phase installs its dependencies once.
        self._environment: tuple[str, Path] | None = None

    def product_interpreter(self) -> tuple[Path, tuple[str, ...]]:
        """Build the declared dependency environment and return its interpreter.

        The product package itself is deliberately *not* installed. Installing it would run
        the build backend named in the agent-authored manifest, as the supervisor, outside
        the sandbox. Instead only pre-built wheels are installed — they unpack without
        executing project code — and the source tree is placed on the path with a .pth file.
        """
        manifest = self.workspace / PRODUCT_MANIFEST
        try:
            raw = manifest.read_bytes()
        except OSError as exc:
            raise ProvisioningError(
                f"no product dependency manifest: {PRODUCT_MANIFEST} is missing from the phase "
                f"workspace, so declared dependencies cannot be installed ({exc.strerror})") from exc
        digest = hashlib.sha256(raw).hexdigest()
        requirements = product_dependencies(raw)
        if self._environment and self._environment[0] == digest:
            return self._environment[1], requirements
        root = Path(tempfile.mkdtemp(prefix="buildctl-product-env-")).resolve()
        created = self.runner([sys.executable, "-m", "venv", str(root)], text=True,
                              capture_output=True, env=provisioning_environment(),
                              timeout=PROVISION_TIMEOUT)
        if created.returncode:
            raise ProvisioningError(f"could not create the product environment: "
                                    f"{redact((created.stderr or '').strip(), limit=1000)}")
        interpreter = root / "bin" / "python"
        if requirements:
            installed = self.runner([str(interpreter), "-m", "pip", "install", "--no-input",
                                     "--no-cache-dir", "--only-binary=:all:", "--", *requirements],
                                    text=True, capture_output=True, env=provisioning_environment(),
                                    timeout=PROVISION_TIMEOUT)
            if installed.returncode:
                detail = (installed.stderr or "").strip() or (installed.stdout or "").strip()
                raise ProvisioningError(f"declared dependencies could not be installed: "
                                        f"{redact(detail, limit=1500)}")
        self._link_product_sources(root)
        self._environment = (digest, interpreter)
        return interpreter, requirements

    def _link_product_sources(self, root: Path) -> None:
        """Put the workspace source tree on the environment's path without building it."""
        source = (self.workspace / "src").resolve()
        if not source.is_dir():
            return
        for site_packages in sorted((root / "lib").glob("python*/site-packages")):
            (site_packages / "_buildctl_product_src.pth").write_text(f"{source}\n")

    def run(self, phase: Any, timeout: float) -> GateResult:
        details: list[dict[str, Any]] = []
        runtime = Path(tempfile.mkdtemp(prefix="buildctl-gate-runtime-")).resolve()
        isolated_home = runtime / "home"; isolated_home.mkdir(mode=0o700)
        state_before = _digest_if_exists(self.control_root / "state.json")
        # Provisioning runs before the command deadline opens, so a slow dependency install
        # cannot consume the time budget the acceptance tests are entitled to.
        interpreter: Path | None = None
        if any(PYTHON_PLACEHOLDER in part for argv in phase.commands for part in argv):
            try:
                interpreter, requirements = self.product_interpreter()
                details.append({"gate": "dependency_provision", "target": PRODUCT_MANIFEST,
                                "passed": True,
                                "output": f"installed {len(requirements)} declared dependencies"})
            except ProvisioningError as exc:
                details.append({"gate": "dependency_provision", "target": PRODUCT_MANIFEST,
                                "passed": False, "output": redact(str(exc), limit=2000)})
        deadline = time.monotonic() + max(0.01, timeout)
        for required in phase.required_files:
            try:
                target = ensure_safe_relative(self.workspace, required)
                passed = target.is_file() and target.stat().st_size > 0 and not target.is_symlink()
            except (ValueError, OSError):
                passed = False
            details.append({"gate": "required_file", "target": required, "passed": passed})
        for argv in phase.commands:
            resolved = _resolve_command(list(argv), interpreter)
            if resolved is None:
                # Fail closed. Falling back to a system interpreter would let a phase pass
                # against whatever happens to be installed on the host.
                details.append({"gate": "command", "target": list(argv), "passed": False,
                                "output": "product dependency environment unavailable"})
                continue
            context = IsolationContext("gate", self.workspace, self.control_root, self.authoritative_repo,
                                       output_dir=runtime, home_dir=isolated_home,
                                       network_allowed=False, host_home=Path.home())
            command = self.backend.command(resolved, context)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    details.append({"gate": "command", "target": list(argv), "passed": False, "output": "TIMEOUT"})
                    continue
                # Bytecode written beside the sources would enter the phase delta as ignored
                # __pycache__ paths and be refused when the phase is committed.
                env = suppress_ephemeral_artifacts(sanitized_environment("gate", home=isolated_home))
                completed = self.runner(command, cwd=self.workspace, text=True, capture_output=True,
                                        timeout=max(0.01, remaining), env=env)
                raw = completed.stdout + completed.stderr
                if contains_secret(raw):
                    details.append({"gate": "command", "target": list(argv), "passed": False,
                                    "output": "[REDACTED: secret-like output]"})
                else:
                    details.append({"gate": "command", "target": list(argv),
                                    "passed": completed.returncode == 0, "output": redact(raw, limit=2000)})
            except subprocess.TimeoutExpired:
                details.append({"gate": "command", "target": list(argv), "passed": False, "output": "TIMEOUT"})
        state_after = _digest_if_exists(self.control_root / "state.json")
        if state_before != state_after:
            raise RuntimeError("acceptance gate mutated canonical state")
        return GateResult(bool(details) and all(item["passed"] for item in details), details)


def _resolve_command(argv: list[str], interpreter: Path | None) -> list[str] | None:
    """Substitute the provisioned interpreter, or refuse the command outright.

    Plain string replacement rather than str.format, so a command containing unrelated
    braces cannot raise KeyError or be reinterpreted as a format field.
    """
    if not any(PYTHON_PLACEHOLDER in part for part in argv):
        return argv
    if interpreter is None:
        return None
    return [part.replace(PYTHON_PLACEHOLDER, str(interpreter)) for part in argv]


def _digest_if_exists(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def write_gate_result(path: Path, result: GateResult, metadata: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**metadata, "passed": result.passed, "details": result.details}, indent=2) + "\n")
