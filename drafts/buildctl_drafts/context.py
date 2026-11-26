"""What a draft may be written from: read-only facts about one milestone, gathered from Git and state.

Nothing here writes anything. Every input is read at the milestone's commit (`until`), never from the
working tree, so a draft describes what was built, not what someone is editing.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buildctl.supervisor.review import NON_BLOCKING_FINDING_STATUSES
from buildctl.supervisor.state import StateStore
from buildctl.supervisor.trust import TrustError, TrustStore

PRODUCT_VOICE = "docs/VOICE.md"
OPERATOR_VOICE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "buildctl" / "voice.md"
TEXT_LIMIT = 40_000  # characters of any one document handed to the writer


class ContextError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, text: bool = True) -> Any:
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args], capture_output=True,
                            text=text, env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null",
                                            "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"})
    if check and result.returncode:
        raise ContextError((result.stderr if text else result.stderr.decode(errors="replace")).strip()
                           or f"git {' '.join(args)} failed")
    return result


def show(repo: Path, commit: str, path: str) -> str | None:
    result = git(repo, "show", f"{commit}:{path}", check=False, text=False)
    if result.returncode:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


class _ReadOnlyTrust(TrustStore):
    """The trusted snapshot, integrity-checked, without the running-kernel check.

    Drafts never run a phase or touch the repository, so a kernel upgraded since `trust init` must not
    stop them from reading the build's governed configuration. The snapshot's own digests still hold."""

    @staticmethod
    def _verify_kernel(anchor: dict[str, Any]) -> None:
        return None


def trusted(repo: Path, control_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(config, anchor) from the trusted snapshot. Drafts refuse an untrusted build."""
    store = _ReadOnlyTrust(repo, control_root)
    try:
        anchor = store.load()
        return store.config(), anchor
    except (TrustError, KeyError, ValueError) as exc:
        raise ContextError(f"the build is not trusted: {exc}. Run buildctl trust init first.") from exc


@dataclass
class Context:
    repo: Path
    control_root: Path
    since: str | None
    until: str
    label: str
    commits: list[dict[str, str]]
    files: set[str]
    changelog: str | None
    readme: str | None
    docs: list[str]
    handoffs: dict[str, str]
    findings: list[dict[str, Any]]
    product_voice: str | None
    operator_voice: str | None
    config: dict[str, Any] = field(default_factory=dict)

    def catalog(self) -> dict[str, Any]:
        """What the writer is told it may cite, as data."""
        return {"range": {"since": self.since, "until": self.until, "label": self.label},
                "commits": self.commits,
                "files_at_until": sorted(self.files)[:2000],
                "changelog": (self.changelog or "")[:TEXT_LIMIT],
                "readme": (self.readme or "")[:TEXT_LIMIT],
                "docs": self.docs,
                "handoffs": {path: text[:8000] for path, text in self.handoffs.items()},
                "open_findings": [{"claim_key": item.get("claim_key"), "severity": item.get("severity"),
                                   "path": item.get("path"), "claim": str(item.get("claim", ""))[:400]}
                                  for item in self.findings]}


def _milestone_range(repo: Path, control_root: Path, config: dict[str, Any], name: str) -> tuple[str | None, str]:
    milestones = config.get("milestones", {})
    if name.lower() not in milestones:
        raise ContextError(f"unknown milestone {name}; build.yaml names {', '.join(sorted(milestones)) or 'none'}")
    number = int(milestones[name.lower()])
    commits = _phase_commits(repo, control_root)
    if number not in commits:
        raise ContextError(f"milestone {name} is phase {number}, which has no recorded commit yet")
    earlier = [int(value) for value in milestones.values() if int(value) < number and int(value) in commits]
    return (commits[max(earlier)] if earlier else None), commits[number]


def _phase_commits(repo: Path, control_root: Path) -> dict[int, str]:
    """Each completed phase's commit: from its sealed receipt, else its phase branch."""
    found: dict[int, str] = {}
    store = StateStore(control_root)
    state = store.load() if store.path.exists() else None
    for event in (state.history if state else []):
        if event.get("event") == "evidence_sealed" and event.get("code_commit"):
            found[int(event["phase"])] = str(event["code_commit"])
    for number in (state.completed_phases if state else []):
        if number not in found:
            tip = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/build/phase-{number}", check=False)
            if tip.returncode == 0:
                found[number] = tip.stdout.strip()
    return found


def gather(repo: Path, control_root: Path, *, since: str | None = None, until: str = "HEAD",
           milestone: str | None = None) -> Context:
    repo = repo.resolve()
    config, _ = trusted(repo, control_root)
    if milestone:
        since, until = _milestone_range(repo, control_root, config, milestone)
        label = milestone
    else:
        label = f"{since or 'start'}..{until}"
    until_sha = git(repo, "rev-parse", "--verify", f"{until}^{{commit}}").stdout.strip()
    since_sha = git(repo, "rev-parse", "--verify", f"{since}^{{commit}}").stdout.strip() if since else None
    span = f"{since_sha}..{until_sha}" if since_sha else until_sha
    log = git(repo, "log", "--format=%H%x1f%s", span).stdout.splitlines()
    commits = [dict(zip(("sha", "subject"), line.split("\x1f", 1))) for line in log if "\x1f" in line]
    if not commits:
        raise ContextError(f"no commits in {span}")
    files = set(git(repo, "ls-tree", "-r", "--name-only", until_sha).stdout.splitlines())
    changed = set(git(repo, "diff", "--name-only", f"{since_sha}..{until_sha}").stdout.splitlines()) if since_sha else files
    docs = sorted(path for path in files if path.endswith(".md") and (path.startswith("docs/") or "/" not in path)
                  and not path.startswith("docs/planning/"))
    handoffs = {path: show(repo, until_sha, path) or "" for path in sorted(changed)
                if path.startswith("docs/HANDOFF_PHASE_") and path in files}
    store = StateStore(control_root)
    state = store.load() if store.path.exists() else None
    findings = [item for item in (state.findings.values() if state else [])
                if item.get("status", "open") not in NON_BLOCKING_FINDING_STATUSES]
    changelog = next((show(repo, until_sha, name) for name in ("CHANGELOG.md", "CHANGES.md") if name in files), None)
    return Context(repo=repo, control_root=control_root, since=since_sha, until=until_sha, label=label,
                   commits=commits, files=files, changelog=changelog, readme=show(repo, until_sha, "README.md"),
                   docs=docs, handoffs=handoffs, findings=findings,
                   product_voice=show(repo, until_sha, PRODUCT_VOICE),
                   operator_voice=OPERATOR_VOICE.read_text() if OPERATOR_VOICE.is_file() else None,
                   config=config)


def clone_at(repo: Path, commit: str, destination: Path) -> Path:
    """A disposable read-only copy of the milestone's commit, with no remote, for the writer to read."""
    git(repo, "clone", "--quiet", "--no-local", "--no-hardlinks", "--no-checkout", str(repo), str(destination))
    git(destination, "checkout", "--quiet", "--detach", commit)
    git(destination, "remote", "remove", "origin")
    return destination


def to_json(context: Context) -> str:
    return json.dumps(context.catalog(), indent=2, sort_keys=True)
