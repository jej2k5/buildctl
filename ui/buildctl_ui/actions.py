"""Reading the build and pulling levers, shared by the web console and the TUI.

Reads go through the kernel's read model. Every change goes through the `buildctl` CLI, run as
a subprocess, so the kernel's own checks, lock and audit apply exactly as they do in a terminal.
Before a lever runs, the model is read again: the lever must still be enabled, the state must
still be the generation the operator was looking at, and the typed confirmation must match the
evidence the kernel names.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buildctl.buildctl import control_root_for
from buildctl.supervisor.inspect import audit_events, inspect


class Refused(Exception):
    """The lever was not pulled. The message says why, in the operator's terms."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    id: str
    argv: list[str]
    pid: int
    log: str
    started_at: float
    detached: bool
    returncode: int | None = None
    output: str = ""


@dataclass
class Build:
    repo: Path
    control_root: Path
    demo: str | None = None
    jobs: list[Job] = field(default_factory=list)

    @classmethod
    def open(cls, repo: Path, control_root: Path | None = None, demo: str | None = None) -> "Build":
        return cls(repo.resolve(), (control_root or control_root_for(repo)).resolve(), demo)

    def model(self) -> dict[str, Any]:
        value = inspect(self.repo, self.control_root)
        value["demo"] = self.demo
        value["jobs"] = [self._job_view(job) for job in self.jobs[-8:]]
        return value

    def audit(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return audit_events(self.control_root, after, limit)

    def read_artifact(self, path: str, limit: int = 200_000) -> str:
        """An artifact file, only from this build's artifacts directory."""
        root = (self.control_root / "artifacts").resolve()
        target = Path(path).resolve()
        if root not in target.parents:
            raise Refused("only files under the build's artifacts directory can be read", 403)
        return target.read_text(errors="replace")[:limit]

    def trust_diff(self) -> dict[str, Any]:
        """What a trust init at HEAD would change, compared with the commit trusted now."""
        model = inspect(self.repo, self.control_root)
        trusted = model["trust"].get("governance_sha")
        head = model["repo"]["head_sha"]
        if not trusted or not head:
            return {"from": trusted, "to": head, "files": []}
        result = subprocess.run(["git", "-C", str(self.repo), "diff", "--numstat", trusted, head],
                                text=True, capture_output=True)
        files = []
        for line in result.stdout.splitlines():
            added, removed, path = line.split("\t", 2)
            files.append({"path": path, "added": added, "removed": removed})
        return {"from": trusted, "to": head, "files": files}

    def pull(self, action: str, *, generation: int, confirm: str = "", operation: str | None = None) -> Job:
        model = inspect(self.repo, self.control_root)
        candidates = [lever for lever in model["levers"] if lever["action"] == action
                      and (operation is None or operation in lever["argv"])]
        if not candidates:
            raise Refused(f"{action} is not available in the current state")
        lever = candidates[0]
        if model["state"]["generation"] != generation:
            raise Refused("the build changed since you looked; review the new state and try again")
        if not lever["enabled"]:
            blocked = [item["label"] for item in lever["conditions"] if item["held"] is False]
            raise Refused("locked: " + "; ".join(blocked))
        expected = lever["confirm"]
        if expected:
            value = str(expected["value"])
            want = value[:expected["length"]] if expected["length"] else value
            if confirm.strip().lower() != want.lower():
                raise Refused("the confirmation does not match the evidence", 422)
        if self.demo and (lever["spends"] or lever["detached"] or action in {"trust", "isolation", "auth"}):
            raise Refused("demo build: this would run `buildctl " + " ".join(_display(lever["argv"])) + "`", 403)
        return self._run(lever["argv"], detached=lever["detached"])

    def _command(self, argv: list[str]) -> list[str]:
        return [sys.executable, "-m", "buildctl", "--repo", str(self.repo), *argv]

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["BUILDCTL_STATE_DIR"] = str(self.control_root)
        return environment

    def _run(self, argv: list[str], *, detached: bool) -> Job:
        identity = hashlib.sha256(f"{time.time()}{argv}".encode()).hexdigest()[:10]
        log_dir = Path(tempfile.gettempdir()) / "buildctl-ui"
        log_dir.mkdir(exist_ok=True, mode=0o700)
        log = log_dir / f"{identity}.log"
        if detached:
            # A new session: the run outlives the console, and closing the console never signals it.
            with log.open("w") as stream:
                process = subprocess.Popen(self._command(argv), stdout=stream, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=self._environment(), start_new_session=True)
            job = Job(identity, argv, process.pid, str(log), time.time(), True)
        else:
            result = subprocess.run(self._command(argv), text=True, capture_output=True, env=self._environment(),
                                    timeout=600)
            output = (result.stdout + result.stderr).strip()
            log.write_text(output)
            job = Job(identity, argv, 0, str(log), time.time(), False, result.returncode, output)
        self.jobs.append(job)
        return job

    def _job_view(self, job: Job) -> dict[str, Any]:
        if job.detached and job.returncode is None:
            try:
                finished, status = os.waitpid(job.pid, os.WNOHANG)
                if finished:
                    job.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                job.returncode = job.returncode if job.returncode is not None else -1
            try:
                job.output = Path(job.log).read_text(errors="replace")[-4000:]
            except OSError:
                pass
        return {"id": job.id, "command": "buildctl " + " ".join(_display(job.argv)), "pid": job.pid,
                "detached": job.detached, "returncode": job.returncode, "output": job.output[-4000:],
                "started_at": job.started_at}


def _display(argv: list[str]) -> list[str]:
    """The command as the operator would type it, with secrets shortened."""
    shown = []
    for index, item in enumerate(argv):
        previous = argv[index - 1] if index else ""
        shown.append(item[:8] + "…" if previous == "--nonce" and item else item)
    return shown


def dumps(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, default=str).encode()
