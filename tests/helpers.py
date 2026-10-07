from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def repository() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    git(root, "init", "-q")
    git(root, "config", "user.email", "factory@test.invalid")
    git(root, "config", "user.name", "Factory Test")
    (root / "README.md").write_text("base\n")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "base")
    return temporary, root


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)
