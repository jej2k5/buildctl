from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .kernel import kernel_identity
from .phases import load_config_text


# Paths inside the governed repository. The supervisor itself is not among them: it is an
# installed package, pinned separately by the kernel identity recorded in the anchor.
GOVERNANCE_PATHS = (
    "build.yaml", "AGENTS.md", "CLAUDE.md",
    "prompts/", "templates/", "docs/ARCHITECTURE_PRINCIPLES.md",
    "docs/HEADLESS_AGENT_CONTRACT.md", "docs/DISCOVERY_DECISIONS.md",
    "docs/PRODUCT_THESIS.md", "docs/ROADMAP.md",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class TrustError(RuntimeError):
    pass


class TrustStore:
    def __init__(self, repo: Path, control_root: Path, runner: Callable[..., Any] = subprocess.run):
        self.repo = repo.resolve()
        self.root = control_root.resolve()
        self.anchor_path = self.root / "trust.json"
        self.snapshot = self.root / "snapshot"
        self.runner = runner

    def _git(self, *args: str, check: bool = True) -> Any:
        result = self.runner(["git", *args], cwd=self.repo, text=True, capture_output=True)
        if check and result.returncode:
            raise TrustError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result

    def initialize(self, sha: str, confirmation: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or confirmation != sha:
            raise TrustError("trust initialization requires typed confirmation of a full 40-character SHA")

        resolved = self._git("rev-parse", f"{sha}^{{commit}}").stdout.strip()
        if resolved != sha:
            raise TrustError("trust target did not resolve to the exact commit")

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        files = self._git("ls-tree", "-r", "--name-only", sha).stdout.splitlines()
        governed = [path for path in files if self.is_governance_path(path)]
        if "build.yaml" not in governed:
            raise TrustError("trusted commit does not contain build.yaml")

        staging = self.root / f".snapshot-staging-{sha}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, mode=0o700)

        try:
            manifest: dict[str, str] = {}

            for governed_path in governed:
                content = self._git("show", f"{sha}:{governed_path}").stdout.encode()
                destination = staging / governed_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                os.chmod(destination, 0o400)
                manifest[governed_path] = sha256_bytes(content)

            digest = sha256_bytes(json.dumps(manifest, sort_keys=True).encode())
            config = load_config_text((staging / "build.yaml").read_text(), staging)

            executables: dict[str, dict[str, str]] = {}
            for role, role_config in config["roles"].items():
                if role_config.get("mode") == "interactive":
                    continue

                requested = role_config["command"][0]
                resolved_executable = (
                    shutil.which(requested)
                    if not Path(requested).is_absolute()
                    else requested
                )

                if not resolved_executable or not Path(resolved_executable).is_file():
                    raise TrustError(f"cannot pin unavailable {role} executable: {requested}")

                executable_path = Path(resolved_executable).resolve()
                executables[role] = {
                    "path": str(executable_path),
                    "sha256": sha256_bytes(executable_path.read_bytes()),
                }

            anchor = {
                "governance_sha": sha,
                "snapshot_digest": digest,
                "manifest": manifest,
                "executables": executables,
                "kernel": kernel_identity(),
            }

            previous_snapshot = self.root / ".snapshot-previous"
            if previous_snapshot.exists():
                shutil.rmtree(previous_snapshot)

            if self.snapshot.exists():
                self.snapshot.rename(previous_snapshot)

            staging.rename(self.snapshot)

            anchor_tmp = self.root / ".trust.json.tmp"
            anchor_tmp.write_text(json.dumps(anchor, indent=2, sort_keys=True) + "\n")
            os.chmod(anchor_tmp, 0o600)
            os.replace(anchor_tmp, self.anchor_path)

            if previous_snapshot.exists():
                shutil.rmtree(previous_snapshot)

            return anchor

        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

            previous_snapshot = self.root / ".snapshot-previous"

            if previous_snapshot.exists():
                if self.snapshot.exists():
                    shutil.rmtree(self.snapshot, ignore_errors=True)
                previous_snapshot.rename(self.snapshot)

            anchor_tmp = self.root / ".trust.json.tmp"
            if anchor_tmp.exists():
                anchor_tmp.unlink(missing_ok=True)

            raise

    def load(self) -> dict[str, Any]:
        try:
            anchor = json.loads(self.anchor_path.read_text())
        except (OSError, ValueError) as exc:
            raise TrustError("factory trust is not initialized; run trust init with an exact commit SHA") from exc
        for path, expected in anchor["manifest"].items():
            candidate = self.snapshot / path
            try:
                actual = sha256_bytes(candidate.read_bytes())
            except OSError as exc:
                raise TrustError(f"trusted snapshot missing: {path}") from exc
            if actual != expected:
                raise TrustError(f"trusted snapshot integrity failure: {path}")
        digest = sha256_bytes(json.dumps(anchor["manifest"], sort_keys=True).encode())
        if digest != anchor["snapshot_digest"]:
            raise TrustError("trusted manifest digest mismatch")
        self._verify_kernel(anchor)
        return anchor

    @staticmethod
    def _verify_kernel(anchor: dict[str, Any]) -> None:
        """Refuse to run under a supervisor other than the one the operator trusted.

        The governance SHA pins the repository's rules, but the code enforcing them is
        installed separately. Upgrading, editing or reinstalling the package changes the
        enforcer, so it requires the same explicit `trust init` a governance change does.
        """
        pinned = anchor.get("kernel")
        if not isinstance(pinned, dict) or not isinstance(pinned.get("digest"), str):
            raise TrustError("trust anchor predates kernel pinning; re-run trust init with an exact commit SHA")
        current = kernel_identity()
        if current["digest"] != pinned["digest"]:
            raise TrustError(f"supervisor kernel changed since trust init (trusted {pinned.get('version')} "
                             f"{pinned['digest'][:12]}, running {current['version']} {current['digest'][:12]}); "
                             "re-run trust init to accept it")

    def config(self) -> dict[str, Any]:
        self.load()
        return load_config_text((self.snapshot / "build.yaml").read_text(), self.snapshot)

    @staticmethod
    def is_governance_path(path: str) -> bool:
        return any(path == prefix or (prefix.endswith("/") and path.startswith(prefix)) for prefix in GOVERNANCE_PATHS)
