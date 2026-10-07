"""The deterministic gate. A proposal reaches the operator only when every rule passes."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from buildctl.supervisor.governance import GOVERNANCE_PATHS, is_governance_path
from buildctl.supervisor.phases import ConfigurationError, load_config_text, merged_metadata
from buildctl.supervisor.safety import ensure_safe_relative

from .record import SEVERITIES, State
from .render import DECISIONS, config, contributed, digest, render, review_digest

BLOCKING = SEVERITIES[:2]


def referenced_governed_files(cfg: dict[str, Any]) -> list[str]:
    roles = cfg.get("roles", {})
    paths = [*cfg.get("governance_documents", []), *roles.get("reviewer", {}).get("context_documents", [])]
    for role, key in (("reviewer", "review_prompt"), ("implementer", "remediation_prompt")):
        if key in roles.get(role, {}):
            paths.append(roles[role][key])
    return [path for path in paths if isinstance(path, str)]


def validate(repo: Path, files: dict[str, bytes]) -> str | None:
    """`buildctl validate` on the proposal, in an overlay of the rendered files on top of the
    repository's own governed inputs that the base references."""
    try:
        cfg = json.loads(files["build.yaml"])
    except (KeyError, json.JSONDecodeError) as exc:
        return f"build.yaml did not render: {exc}"
    with tempfile.TemporaryDirectory(prefix="buildctl-discover-") as name:
        root = Path(name)
        for path in referenced_governed_files(cfg):
            if path in files:
                continue
            try:
                source = ensure_safe_relative(repo, path)
            except ValueError:
                continue  # validate_config reports the unsafe path
            if source.is_file():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        for path, content in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        try:
            load_config_text(files["build.yaml"].decode(), root)
        except (ConfigurationError, KeyError, TypeError, ValueError) as exc:
            return str(exc) or type(exc).__name__
    return None


def _covers_governed(path: str, documents: list[str]) -> str | None:
    if path in ("", ".", "./") or path.startswith("/"):
        return "the whole repository"
    if is_governance_path(path, documents):
        return path
    if path.endswith("/"):
        for governed in (*GOVERNANCE_PATHS, *documents):
            if governed.startswith(path):
                return governed
    return None


def check(state: State, repo: Path, on_disk: dict[str, bytes]) -> dict[str, Any]:
    files = render(state)
    cfg = config(state)
    failures: list[dict[str, Any]] = []

    def fail(rule: str, message: str, *targets: str) -> None:
        failures.append({"rule": rule, "message": message, "targets": list(targets)})

    if not state.thesis:
        fail("thesis", "No product thesis yet. Call set_thesis.")
    if not state.phases:
        fail("phases", "No phases yet. Call propose_phase.")
    if state.milestones is None:
        fail("milestones", "No MVP cut or checkpoints yet. Call set_milestones.")

    problem = validate(repo, files)
    if problem:
        fail("validate", f"buildctl validate refuses the proposed build.yaml: {problem}")

    implemented = {ident for item in state.phases for ident in item["implements"]}
    for decision in state.current_decisions():
        if decision["id"] not in implemented and not decision["deferred"]:
            fail("coverage", f"{decision['id']} is implemented by no phase. Cite it from a phase, or defer it "
                 "with a reason.", decision["id"])

    for number, item in enumerate(state.phases):
        if not item["implements"]:
            fail("citation", f"Phase {number} ({item['id']}) cites no decision.", item["id"])
        for ident in item["implements"]:
            decision = state.decisions.get(ident)
            if decision is None:
                fail("citation", f"Phase {number} ({item['id']}) cites unknown {ident}.", item["id"])
            elif decision["superseded_by"]:
                fail("citation", f"Phase {number} ({item['id']}) cites {ident}, which {decision['superseded_by']} "
                     "superseded.", item["id"], ident)
        if not contributed(item["acceptance"]):
            fail("acceptance", f"Phase {number} ({item['id']}) relies on the default acceptance alone. Add at least "
                 "one command or required file that proves this phase's own work.", item["id"])

    documents = list(cfg.get("governance_documents", []))
    defaults = {"required_files": [], "allowed_paths": [], "commands": [], "invariants": [],
                "merge_policy": "auto", **cfg.get("phase_defaults", {})}
    for number, (item, entry) in enumerate(zip(state.phases, cfg["phases"])):
        for path in merged_metadata(defaults, entry)["allowed_paths"]:
            covered = _covers_governed(path, documents)
            if covered:
                fail("allowed_paths", f"Phase {number} ({item['id']}) may change `{path}`, which covers governed "
                     f"`{covered}`. A phase that touches a governed file fails.", item["id"])

    for question in state.questions.values():
        if question["status"] == "open":
            fail("open_question", f"{question['id']} is still open: {question['text']} Resolve it, or the "
                 "operator accepts it as a risk.", question["id"])

    if DECISIONS not in documents or DECISIONS not in cfg["roles"]["reviewer"].get("context_documents", []):
        fail("governed_decisions", f"`{DECISIONS}` must be a governance document and reviewer context.")

    if not on_disk:
        fail("rendered", "The proposal has not been rendered.")
    elif on_disk != files:
        changed = sorted(set(on_disk) ^ set(files) | {path for path in files if on_disk.get(path) != files[path]})
        fail("rendered", "Proposal files differ from a fresh render of the record. Files are rendered, never "
             f"edited: {', '.join(changed)}")

    current = review_digest(files)
    if not state.reviews:
        fail("review", "No roadmap review yet. Call request_review.")
    else:
        latest = state.reviews[-1]
        if latest["digest"] != current:
            fail("review", f"The latest review ({latest['id']}) covers an earlier proposal. Call request_review "
                 "again.", latest["id"])
        else:
            for finding in latest["findings"]:
                if finding["severity"] in BLOCKING and finding["id"] not in state.accepted_risks:
                    fail("review", f"{finding['id']} ({finding['severity']}, {finding['target']}): "
                         f"{finding['summary']}", finding["id"], finding["target"])

    return {"passed": not failures, "digest": digest(files), "review_digest": current, "failures": failures}
