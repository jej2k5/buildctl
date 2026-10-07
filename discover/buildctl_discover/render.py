"""Render a proposal from the record. Same record, same bytes: nothing here reads the clock,
the environment or the repository."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from buildctl.supervisor.phases import merged_metadata

from .record import LIST_FIELDS, State, slug

THESIS = "docs/PRODUCT_THESIS.md"
DECISIONS = "docs/DISCOVERY_DECISIONS.md"
PRINCIPLES = "docs/ARCHITECTURE_PRINCIPLES.md"
ROADMAP = "docs/ROADMAP.md"
# Accepted risks live apart from the roadmap so accepting one does not stale the review.
RISKS = "docs/DISCOVERY_RISKS.md"


def prompt_path(number: int, name: str) -> str:
    return f"prompts/phases/phase-{number:02d}-{slug(name)}.md"


def _dedupe(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def phase_acceptance(defaults: dict[str, Any], number: int, acceptance: dict[str, Any]) -> dict[str, Any]:
    """Turn the record's add/set intent into what buildctl reads: a named key replaces the default."""
    handoffs = [item for item in defaults.get("required_files", []) if "{phase}" in item]
    result: dict[str, Any] = {}
    for name in LIST_FIELDS:
        if name not in acceptance:
            continue
        mode, items = next(iter(acceptance[name].items()))
        if mode == "add":
            full = _dedupe([*defaults.get(name, []), *items])
        else:
            full = list(items)
            if name == "required_files":
                keep = handoffs
            elif name == "allowed_paths":
                keep = [item for item in handoffs if item in defaults.get("allowed_paths", [])]
            else:
                keep = []
            for item in reversed(keep):
                if item not in full and item.format(phase=number) not in full:
                    full.insert(0, item)
        result[name] = full
    if "merge_policy" in acceptance:
        result["merge_policy"] = acceptance["merge_policy"]
    return result


def contributed(acceptance: dict[str, Any]) -> bool:
    """Did the phase add a command or a required file of its own, beyond the defaults?"""
    for name in ("commands", "required_files"):
        if name in acceptance and next(iter(acceptance[name].values())):
            return True
    return False


def generated_documents(state: State) -> list[str]:
    result = []
    if state.thesis:
        result.append(THESIS)
    if state.principles:
        result.append(PRINCIPLES)
    return [*result, DECISIONS, RISKS, ROADMAP]


def config(state: State) -> dict[str, Any]:
    result = copy.deepcopy(state.base)
    generated = generated_documents(state)
    result["governance_documents"] = _dedupe([*result.get("governance_documents", []), *generated])
    reviewer = result.setdefault("roles", {}).setdefault("reviewer", {})
    context = [item for item in generated if item not in (ROADMAP, RISKS)]
    reviewer["context_documents"] = _dedupe([*reviewer.get("context_documents", []), *context])
    numbers = {item["id"]: number for number, item in enumerate(state.phases)}
    phases = []
    for number, item in enumerate(state.phases):
        entry: dict[str, Any] = {"number": number, "name": item["name"], "prompt": prompt_path(number, item["name"])}
        acceptance = phase_acceptance(result.get("phase_defaults", {}), number, item["acceptance"])
        if acceptance:
            entry["acceptance"] = acceptance
        phases.append(entry)
    marks = state.milestones
    if marks:
        result["milestones"] = {name: numbers.get(ident) for name, ident in marks["milestones"].items()}
        result["checkpoints"] = {"before_phases": sorted(numbers.get(ident) for ident in marks["checkpoints_before"]),
                                 "mvp_completion": numbers.get(marks["mvp_completion"])}
    else:
        result["milestones"] = {}
        result["checkpoints"] = {"before_phases": [], "mvp_completion": None}
    result["phases"] = phases
    return result


def _who(by: dict[str, str] | None) -> str:
    return f"{by['name']}, {by['role']}" if by else "unknown"


def _join(argv: list[str]) -> str:
    # Placeholders such as {python} are resolved by buildctl, so they are shown bare.
    return " ".join(arg if re.fullmatch(r"\{[a-z_]+\}", arg) else shlex.quote(arg) for arg in argv)


def _command(argv: list[str]) -> str:
    if any("\n" in arg for arg in argv):
        return f"`{_join(argv[:2])} ...` (multi-line; see `build.yaml`)"
    return f"`{_join(argv)}`"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def phase_prompt(state: State, cfg: dict[str, Any], number: int) -> str:
    item, entry = state.phases[number], cfg["phases"][number]
    # Tolerate an incomplete base here; `check` reports it through `buildctl validate`.
    defaults = {"required_files": [], "allowed_paths": [], "commands": [], "invariants": [],
                "merge_policy": "auto", **cfg.get("phase_defaults", {})}
    meta = merged_metadata(defaults, entry)
    docs = [path for path in generated_documents(state) if path not in (ROADMAP, RISKS)]
    reads = _and([f"`{path}`" for path in docs])
    lines = [f"# Phase {number}: {item['name']}", "", "## Objective", "", item["objective"], ""]
    if item["details"]:
        lines += [item["details"], ""]
    lines += ["## Implements", ""]
    for ident in item["implements"]:
        decision = state.decisions.get(ident)
        lines.append(f"- **{ident}**: {decision['text']}" if decision else f"- **{ident}**: (unknown decision)")
    if not item["implements"]:
        lines.append("- (no decisions cited)")
    if number:
        reads = _and([*[f"`{path}`" for path in docs], "the previous phase handoff"])
    lines += ["", "## Constraints", "", f"Read {reads} first. Keep every invariant in `build.yaml`. "
              "Change only these paths:", ""]
    lines += [f"- `{path}`" for path in meta["allowed_paths"]]
    lines += ["", "## Acceptance", ""]
    lines += [f"- `{path}` exists." for path in meta["required_files"]]
    lines += [f"- {_command(argv)} exits 0, sandboxed with no network." for argv in meta["commands"]]
    if meta["invariants"]:
        lines.append(f"- The reviewer checks these invariants: {', '.join(meta['invariants'])}.")
    if meta["merge_policy"] == "human":
        lines.append("- The operator approves this phase before it is merged.")
    handoff = [path for path in meta["required_files"] if "HANDOFF" in path]
    if handoff:
        lines.append(f"- `{handoff[0]}` records the summary, tests, decisions, known debt and the next action.")
    return "\n".join(lines) + "\n"


def decisions_document(state: State) -> str:
    lines = ["# Discovery decisions", "",
             "These decisions were made during discovery. Agents must treat them as current requirements. "
             "They change only through discovery: a new decision supersedes the old one, then the operator "
             "commits the change and runs `trust init`.", ""]
    current = [item for item in state.current_decisions() if not item["deferred"]]
    sections: list[str] = _dedupe([item["section"] for item in current])
    for section in sections:
        lines += [f"## {section}", ""]
        for item in current:
            if item["section"] == section:
                lines.append(f"- **{item['id']}** ({item['confidence']} confidence; {_who(item['by'])}, "
                             f"{item['at'][:10]}): {item['text']}")
        lines.append("")
    deferred = [item for item in state.current_decisions() if item["deferred"]]
    if deferred:
        lines += ["## Deferred", ""]
        lines += [f"- **{item['id']}**: {item['text']} Deferred by {_who(item['deferred']['by'])}: "
                  f"{item['deferred']['reason']}" for item in deferred]
        lines.append("")
    superseded = [item for item in state.decisions.values() if item["superseded_by"]]
    if superseded:
        lines += ["## Superseded", ""]
        lines += [f"- **{item['id']}**, superseded by {item['superseded_by']}: {item['text']}" for item in superseded]
        lines.append("")
    return "\n".join(lines)


def risks_document(state: State) -> str:
    lines = ["# Accepted risks", "",
             "Open questions and roadmap-review findings the operator accepted instead of resolving.", ""]
    risks = list(state.accepted_risks.values())
    for risk in risks:
        question = state.questions.get(risk["target"])
        finding = state.finding(risk["target"])
        subject = question["text"] if question else f"{finding['severity']} finding: {finding['summary']}"
        lines.append(f"- **{risk['target']}**: {subject} Accepted by {_who(risk['by'])}: {risk['reason']}")
    if not risks:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def roadmap_document(state: State, cfg: dict[str, Any]) -> str:
    lines = ["# Roadmap", ""]
    lines += [f"{number}. {item['name']}" for number, item in enumerate(state.phases)]
    lines.append("")
    checkpoints = cfg["checkpoints"]
    if checkpoints["mvp_completion"] is not None:
        lines.append(f"The MVP is complete after phase {checkpoints['mvp_completion']}.")
    if checkpoints["before_phases"]:
        lines.append("The build stops for operator approval before phases "
                     f"{', '.join(map(str, checkpoints['before_phases']))}.")
    for name, number in cfg["milestones"].items():
        if name != "mvp":
            lines.append(f"The {name} milestone is phase {number}.")
    return "\n".join(lines) + "\n"


def render(state: State) -> dict[str, bytes]:
    cfg = config(state)
    files = {"build.yaml": json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"}
    if state.thesis:
        files[THESIS] = f"# Product thesis\n\n{state.thesis['text']}\n"
    if state.principles:
        files[PRINCIPLES] = "# Architecture principles\n\n" + "".join(
            f"- **{item['id']}**: {item['text']}\n" for item in state.principles)
    files[DECISIONS] = decisions_document(state)
    files[RISKS] = risks_document(state)
    files[ROADMAP] = roadmap_document(state, cfg)
    for number in range(len(state.phases)):
        files[cfg["phases"][number]["prompt"]] = phase_prompt(state, cfg, number)
    return {path: text.encode() for path, text in sorted(files.items())}


def digest(files: dict[str, bytes]) -> str:
    listing = [[path, hashlib.sha256(content).hexdigest()] for path, content in sorted(files.items())]
    return hashlib.sha256(json.dumps(listing, separators=(",", ":")).encode()).hexdigest()


def review_digest(files: dict[str, bytes]) -> str:
    """What a roadmap review covers: every file except the accepted-risk list."""
    return digest({path: content for path, content in files.items() if path != RISKS})


def write_proposal(directory: Path, files: dict[str, bytes]) -> None:
    staging = directory.with_name(directory.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    for path, content in files.items():
        target = staging / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if directory.exists():
        shutil.rmtree(directory)
    staging.rename(directory)


def read_proposal(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}
