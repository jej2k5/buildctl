from __future__ import annotations

import json
import re
import subprocess
from typing import Any


def reviewer(findings: list[dict[str, Any]] | None = None, *, digest: str | None = None, returncode: int = 0,
             wrap: bool = False):
    """A reviewer command that answers under the roadmap protocol, echoing the digest it was given."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        prompt = next(arg for arg in argv if "Render digest:" in arg)
        given = re.search(r"Render digest: `([0-9a-f]{64})`", prompt).group(1)
        body = json.dumps({"protocol_version": "buildctl-roadmap-review-v1", "render_digest": digest or given,
                           "findings": findings or []})
        stdout = f"Here is my review.\n```json\n{body}\n```\n" if wrap else body
        return subprocess.CompletedProcess(argv, returncode, stdout, "boom" if returncode else "")
    run.calls = calls
    return run
