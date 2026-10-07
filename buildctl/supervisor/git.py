from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .safety import (UnsafeCleanup, ensure_safe_relative, matches_secret_pattern, purge_ephemeral_artifacts,
                     secret_shaped)
from .governance import governance_documents, is_governance_path


class GitSafetyError(RuntimeError):
    pass


DEFAULT_INTEGRATION_BRANCH = "main"


@dataclass(frozen=True)
class Delta:
    added_or_modified: tuple[str, ...]
    deleted: tuple[str, ...]
    changes: tuple[dict[str, Any], ...]
    digest: str


class GitManager:
    def __init__(self, root: Path, config: dict[str, Any], runner: Callable[..., Any] = subprocess.run):
        self.root, self.config, self.runner = root.resolve(), config, runner

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> Any:
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        result = self.runner(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd or self.root,
                             text=True, capture_output=True, env=env)
        if check and result.returncode:
            raise GitSafetyError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result

    def current_sha(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def tree_hash(self) -> str:
        return self._git("rev-parse", "HEAD^{tree}").stdout.strip()

    def ensure_safe(self, expected_sha: str | None = None) -> None:
        if self._git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise GitSafetyError("not inside a Git worktree")
        if expected_sha and self.current_sha() != expected_sha:
            raise GitSafetyError("authoritative repository moved from pinned base SHA")
        if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout.strip():
            raise GitSafetyError("uncommitted user work detected; authoritative repository is untouched")

    def create_isolated_clone(self, base_sha: str, destination: Path) -> Path:
        destination = destination.resolve()
        self.ensure_safe(base_sha)
        if destination.exists():
            raise GitSafetyError("phase clone already exists; reconcile it instead of recreating")
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = self.runner(["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout",
                                 str(self.root), str(destination)], text=True, capture_output=True,
                                env={"PATH": os.environ.get("PATH", "")})
        if completed.returncode:
            raise GitSafetyError(f"isolated clone failed: {completed.stderr.strip()}")
        self._git("checkout", "--detach", base_sha, cwd=destination)
        self._git("remote", "remove", "origin", cwd=destination)
        if self._git("remote", cwd=destination).stdout.strip():
            raise GitSafetyError("agent clone retains a remote")
        return destination

    def prepare_agent_workspace(self, workspace: Path) -> None:
        """Remove ambient project instruction sources before the agent process starts."""
        for relative in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex"):
            target = workspace.resolve() / relative
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()

    def manifest(self, workspace: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(workspace.rglob("*")):
            parts = path.relative_to(workspace).parts
            if ".git" in parts or ".factory-runtime" in parts or path.is_dir():
                continue
            relative = path.relative_to(workspace).as_posix()
            if path.is_symlink():
                result[relative] = "SYMLINK:" + os.readlink(path)
            else:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def attributed_delta(self, workspace: Path, before: dict[str, str], allowed_paths: tuple[str, ...]) -> Delta:
        # Defence in depth behind the suppression environment: any interpreter the supervisor
        # did not launch (or one launched before this ran) can still leave bytecode and tool
        # caches in the workspace, where they enter the delta as unreviewable binaries or
        # unattributed paths. Only those named artifacts are removed, and only when each can
        # be proven contained and symlink-free; every other stray path still fails the phase.
        try:
            purge_ephemeral_artifacts(workspace)
        except (UnsafeCleanup, OSError) as exc:
            raise GitSafetyError(f"workspace artifact cleanup failed: {exc}") from exc
        after = self.manifest(workspace)
        changed = sorted(path for path, digest in after.items() if before.get(path) != digest)
        deleted = sorted(path for path in before if path not in after)
        all_changes = changed + deleted
        for path in all_changes:
            if is_governance_path(path, governance_documents(self.config)):
                raise GitSafetyError(f"agent modified governance path: {path}")
            if not any(path.startswith(prefix) if prefix.endswith("/") else path == prefix
                       for prefix in allowed_paths):
                raise GitSafetyError(f"phase changed unattributed path: {path}")
            target = ensure_safe_relative(workspace, path)
            if target.is_symlink() or secret_shaped(path):
                raise GitSafetyError(f"unsafe phase path: {path}")
            if target.exists():
                sample = target.read_text(errors="ignore")[:10000]
                if matches_secret_pattern(sample):
                    raise GitSafetyError(f"secret-like content in phase path: {path}")
        changes: list[dict[str, Any]] = []
        remaining_added = set(changed)
        remaining_deleted = set(deleted)
        # A byte-identical delete/add pair is represented as a deterministic rename.
        for old in deleted:
            candidates = sorted(path for path in remaining_added if after[path] == before[old])
            if candidates:
                new = candidates[0]
                changes.append({"path": new, "change_type": "renamed", "previous_path": old,
                                "digest": after[new], "previous_digest": before[old]})
                remaining_added.remove(new); remaining_deleted.remove(old)
        for path in sorted(remaining_added):
            changes.append({"path": path, "change_type": "added" if path not in before else "modified",
                            "previous_path": None, "digest": after[path], "previous_digest": before.get(path)})
        for path in sorted(remaining_deleted):
            changes.append({"path": path, "change_type": "deleted", "previous_path": path,
                            "digest": "DELETED", "previous_digest": before[path]})
        canonical = json_like(changes).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return Delta(tuple(changed), tuple(deleted), tuple(changes), digest)

    def apply_and_commit(self, workspace: Path, delta: Delta, phase: Any, base_sha: str) -> str:
        self.ensure_safe(base_sha)
        branch = f"build/phase-{phase.number}"
        if self._git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
            existing_base = self._git("merge-base", branch, base_sha).stdout.strip()
            if existing_base != base_sha:
                raise GitSafetyError("existing phase branch is not pinned to expected base")
            self._git("switch", branch)
        else:
            self._git("switch", "-c", branch, base_sha)
        staged: list[str] = []
        for relative in delta.added_or_modified:
            source = ensure_safe_relative(workspace, relative)
            target = ensure_safe_relative(self.root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
            staged.append(relative)
        for relative in delta.deleted:
            target = ensure_safe_relative(self.root, relative)
            if target.exists():
                target.unlink()
            staged.append(relative)
        for relative in staged:
            ignored = self._git("check-ignore", "-q", "--", relative, check=False).returncode == 0
            if ignored:
                raise GitSafetyError(f"refusing to stage ignored path: {relative}")
            self._git("add", "--", relative)
        actual = sorted(self._git("diff", "--cached", "--name-only").stdout.splitlines())
        if actual != sorted(staged):
            raise GitSafetyError("staged paths differ from attributed phase delta")
        if not actual:
            raise GitSafetyError("phase produced no attributable changes")
        self._git("commit", "-m", f"Complete phase {phase.number}: {phase.name}",
                  "-m", f"Base-SHA: {base_sha}")
        return branch

    def review_evidence(self, workspace: Path, delta: Delta, gate: dict[str, Any], *, max_text_bytes: int = 100_000) -> dict[str, Any]:
        workspace = workspace.resolve()
        changed_files = []
        tests = []
        for change in delta.changes:
            item = dict(change)
            if change["change_type"] == "deleted":
                item.update({"reviewable": True, "content": None, "content_encoding": "deleted"})
            else:
                target = ensure_safe_relative(workspace, change["path"])
                raw = target.read_bytes()
                try:
                    content = raw.decode("utf-8")
                    reviewable = len(raw) <= max_text_bytes
                except UnicodeDecodeError:
                    content, reviewable = None, False
                item.update({"reviewable": reviewable,
                             "content": content if reviewable else None,
                             "content_encoding": "utf-8" if reviewable else "binary_or_oversized",
                             "size": len(raw)})
                if reviewable and change["path"].startswith("tests/"):
                    tests.append(content or "")
            changed_files.append(item)
        coverage = [{"path": item["path"], "digest": item["digest"],
                     "change_type": item["change_type"], "reviewable": item["reviewable"]}
                    for item in changed_files]
        return {"delta_id": delta.digest, "diff": json_like(changed_files),
                "changed_files": changed_files, "gates": gate, "tests": "\n\n".join(tests),
                "digest": delta.digest, "coverage": coverage,
                "unreviewable": [item["path"] for item in changed_files if not item["reviewable"]]}

    def integration_branch(self) -> str:
        return self.config["git"].get("integration_branch", DEFAULT_INTEGRATION_BRANCH)

    def integrate(self, phase_branch: str) -> str:
        """Fast-forward the integration branch to a finished phase and leave HEAD on it.

        Phases used to stack on their own branches, so the integration branch never moved
        and every phase was based on the previous phase branch. Now the next phase starts
        from the integration branch, which carries everything accepted so far.

        Fast-forward only. If the integration branch holds commits the phase does not, the
        phase was built on a base the integration branch has moved past; merging would put
        an unreviewed combination on it, so this refuses instead. Idempotent, so a restart
        after a crash part-way through completes rather than fails.
        """
        target = self.integration_branch()
        if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout.strip():
            raise GitSafetyError("uncommitted user work detected; integration branch left untouched")
        for branch in (target, phase_branch):
            if self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode:
                raise GitSafetyError(f"branch does not exist: {branch}")
        phase_tip = self._git("rev-parse", f"refs/heads/{phase_branch}").stdout.strip()
        if self._git("merge-base", "--is-ancestor", f"refs/heads/{target}", phase_tip,
                     check=False).returncode:
            raise GitSafetyError(f"{target} has commits that {phase_branch} does not; "
                                 "refusing to merge, fast-forward only")
        self._git("switch", target)
        self._git("merge", "--ff-only", phase_tip)
        if self.current_sha() != phase_tip:
            raise GitSafetyError(f"{target} did not fast-forward to {phase_branch}")
        return target

    def publish(self, phase: Any) -> str:
        git = self.config["git"]
        if not git["push"]:
            return f"integrated:{self.integrate(f'build/phase-{phase.number}')}"
        self._git("push", "-u", git["remote"], f"build/phase-{phase.number}")
        if not git["create_pr"]:
            return "pushed"
        result = self.runner(["gh", "pr", "create", "--fill"], cwd=self.root, text=True, capture_output=True)
        if result.returncode:
            raise GitSafetyError("PR creation failed")
        if phase.merge_policy == "auto" and git["merge"]:
            merged = self.runner(["gh", "pr", "merge", "--auto", "--squash"], cwd=self.root,
                                 text=True, capture_output=True)
            if merged.returncode:
                raise GitSafetyError("PR merge request failed")
            return "auto_merge_requested"
        return "pr_created"


def json_like(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
