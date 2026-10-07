"""The roadmap review: a second model checks the proposal under its own protocol.

`buildctl-review-v2` reviews code against tree hashes and stays untouched. This protocol
reviews a rendered proposal and is pinned to its render digest."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from importlib import resources
from typing import Any, Callable

from buildctl.supervisor.safety import redact

from .record import SEVERITIES, Identity, RecordError, Session
from .render import RISKS, render, review_digest

PROTOCOL = "buildctl-roadmap-review-v1"
# MCP client names that reveal the interviewing agent's vendor.
CLIENT_VENDORS = (("claude", "anthropic"), ("codex", "openai"), ("gemini", "google"))


def template() -> str:
    return resources.files("buildctl_discover").joinpath("protocol/roadmap-review.md").read_text()


def build_prompt(state: Any, files: dict[str, bytes], expected: str) -> str:
    sections = [f"## `{path}`\n\n````\n{content.decode()}````" for path, content in files.items() if path != RISKS]
    ids = ", ".join(f"{item['id']} = phase {number}" for number, item in enumerate(state.phases)) or "none"
    return (template().replace("{digest}", expected).replace("{phase_ids}", ids)
            .replace("{proposal}", "\n\n".join(sections)))


def parse(output: str, expected: str) -> list[dict[str, str]]:
    text = output.strip()
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [text, *reversed(fenced)]
    if "{" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    value = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(value, dict):
        raise RecordError("the reviewer did not return a JSON object")
    if value.get("protocol_version") != PROTOCOL:
        raise RecordError(f"the reviewer did not answer under {PROTOCOL}")
    if value.get("render_digest") != expected:
        raise RecordError("the reviewer reviewed a different proposal (render digest mismatch)")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise RecordError("the reviewer's findings must be a list")
    result = []
    for item in findings:
        if not isinstance(item, dict) or item.get("severity") not in SEVERITIES:
            raise RecordError(f"each finding needs a severity of {', '.join(SEVERITIES)}")
        for key in ("target", "summary"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise RecordError(f"each finding needs a {key}")
        detail = item.get("detail", "")
        if not isinstance(detail, str):
            raise RecordError("a finding's detail must be text")
        result.append({"severity": item["severity"], "target": item["target"].strip(),
                       "summary": redact(item["summary"].strip(), limit=500),
                       "detail": redact(detail.strip(), limit=2000)})
    return result


def vendor_warnings(client: str | None, reviewer: dict[str, Any]) -> list[str]:
    if not client:
        return []
    for marker, vendor in CLIENT_VENDORS:
        if marker in client.lower() and reviewer.get("vendor") == vendor:
            return [f"The interview ran in {client} and the reviewer is also {vendor}. A reviewer from another "
                    "vendor catches more."]
    return []


def request_review(session: Session, by: Identity, *, runner: Callable[..., Any] = subprocess.run,
                   client: str | None = None, timeout: int = 900) -> dict[str, Any]:
    state = session.state()
    files = render(state)
    expected = review_digest(files)
    reviewer = state.base.get("roles", {}).get("reviewer", {})
    command = reviewer.get("command")
    if not isinstance(command, list) or "{prompt}" not in command:
        raise RecordError("the base reviewer command must be an argv list containing {prompt}")
    prompt = build_prompt(state, files, expected)
    argv = [prompt if arg == "{prompt}" else arg for arg in command]
    with tempfile.TemporaryDirectory(prefix="buildctl-discover-review-") as empty:
        try:
            completed = runner(argv, cwd=empty, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RecordError(f"reviewer command not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RecordError(f"the reviewer ran past {timeout}s") from exc
    if completed.returncode != 0:
        raise RecordError(f"the reviewer exited {completed.returncode}: {redact(completed.stderr or '', limit=400)}")
    findings = parse(completed.stdout or "", expected)
    warnings = vendor_warnings(client, reviewer)
    identity = {key: reviewer[key] for key in ("vendor", "model_family") if key in reviewer}
    review_id = session.record_review(expected, dict(identity, protocol=PROTOCOL, command=command[0]),
                                      findings, by, warnings)
    recorded = session.state().reviews[-1]
    return {"review": review_id, "digest": expected, "findings": recorded["findings"], "warnings": warnings}
