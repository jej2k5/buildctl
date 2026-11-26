"""The writer: one read-only agent call per draft, inside buildctl's own sandbox.

It runs as the build's configured reviewer command (Claude, tools limited to Read, Grep and Glob) in a
disposable copy of the milestone's commit, through the kernel's RoleRunner and isolation backend. Its
read-only workspace is checked before and after, exactly as for a review. It returns JSON: the draft,
and a claims ledger the checks then verify.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from buildctl.supervisor.agents import RoleRunner, TRUSTED_PREAMBLE, untrusted
from buildctl.supervisor.isolation import IsolationContext, production_backend

from .checks import DESCRIPTION_LIMIT, SOURCE_KINDS, TOPIC_LIMIT, TYPES, Voice
from .context import Context

STREAM_FLAGS = {"--output-format": 1, "--verbose": 0, "--include-partial-messages": 0}

INSTRUCTIONS = {
    "release-notes": "Write release notes for this milestone: what changed for a user or operator, grouped under a "
                     "few plain headings, most important first. Use the changelog and the commits; do not list every "
                     "commit. End with what does not work yet, from the known limits.",
    "positioning": "Write positioning for the repository. `body` is the README's opening paragraph: what the project "
                   "is and who it is for, in at most 120 words, describing only what exists. In `fields`, give "
                   f"`description` (one sentence, at most {DESCRIPTION_LIMIT} characters, for the repository "
                   f"description) and `topics` (at most {TOPIC_LIMIT} lowercase, hyphenated repository topics).",
    "launch-post": "Write a launch post announcing this release, for a technical audience (for example a Show HN or "
                   "a project blog). Open with the problem it solves, show what it does with specifics, say plainly "
                   "what it does not do yet, and say how to try it. No hype.",
    "demo-script": "Write a demo script: a talk track for showing the product live in a few minutes. Number the "
                   "steps; for each, what to show on screen and one or two sentences to say. Use only commands, "
                   "screens and behavior the docs describe.",
    "social": "Write one social post, in the operator's own voice from the operator voice file, announcing this "
              "release under the operator's name. Follow that voice's rules exactly.",
}

PROTOCOL = """You write release material for a software project from the facts supplied below, then account for every factual statement.

Rules:
- Use only the facts in the source catalog and the files of this repository at the milestone's commit (you may Read, Grep and Glob them). Never invent a feature, number, date, user, customer, benchmark or quote.
- Every factual statement in the draft appears in `claims`, with the source that backs it:
  - `commit`: `ref` is a commit id from the catalog's commits.
  - `file`: `ref` is a path that exists at the milestone, with `line` (or null) and `quote`, text copied exactly from that file.
  - `changelog`: `quote` is text copied exactly from the changelog; `ref` is "CHANGELOG.md".
  - `finding`: `ref` is an open finding's claim_key, for a known limit.
- Put the open findings that matter to a user in `known_limits`, in plain words, and say them in the draft where it fits.
- The draft is for the project's users. Never mention how it was built: no phases, handoffs, findings, reviews, agents or buildctl. Say a limit as a fact about the software.
- `body` starts with its first paragraph: no top-level `#` title (that is `title`) and no known-limits section (the limits go in `known_limits`, which is printed after the body).
- Follow the voice file. Describe; do not persuade. Short sentences.
- Return only the JSON object below, with no Markdown fence or commentary.
"""

CONTRACT = {"title": "<title>", "body": "<the draft, in Markdown>",
            "claims": [{"text": "<one factual statement from the draft>",
                        "source": {"kind": "|".join(SOURCE_KINDS), "ref": "<commit id, path, CHANGELOG.md or claim_key>",
                                   "line": None, "quote": "<exact text from the source, or null>"}}],
            "known_limits": ["<a limit a user should know>"],
            "fields": {"description": "<positioning only>", "topics": ["<positioning only>"]}}


def build_prompt(kind: str, context: Context, voice: Voice | None, feedback: dict[str, Any] | None = None) -> str:
    text = TRUSTED_PREAMBLE + "\n" + PROTOCOL + f"\nDRAFT TO WRITE: {TYPES[kind]['title']}\n{INSTRUCTIONS[kind]}\n"
    if kind != "positioning":
        text += "Set `fields` to {}.\n"
    if voice and voice.text:
        text += untrusted(f"{voice.name}_voice", voice.text)
    elif kind != "social":
        text += "\nThe project has no voice file. Write plainly: short declarative sentences, no marketing language.\n"
    text += untrusted("source_catalog", json.dumps(context.catalog(), indent=2, sort_keys=True))
    if feedback:
        text += ("\nYour previous draft failed these checks. Fix every one: back or remove each unbacked claim, and "
                 "follow the voice rules." + untrusted("failed_checks", json.dumps(feedback, indent=2)))
    return text + "\nPINNED OUTPUT CONTRACT\n" + json.dumps(CONTRACT, indent=2)


def writer_config(config: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    """The build's reviewer, reading plain output: the drafts are not recorded as phase evidence."""
    result = copy.deepcopy(config)
    command = list(result["roles"]["reviewer"]["command"])
    plain: list[str] = []
    skip = 0
    for part in command:
        if skip:
            skip -= 1
            continue
        if part in STREAM_FLAGS:
            skip = STREAM_FLAGS[part]
            continue
        plain.append(part)
    result["roles"]["reviewer"]["command"] = plain
    result["_trusted_executables"] = anchor.get("executables", {})
    return result


def call_writer(context: Context, config: dict[str, Any], workspace: Path, prompt: str, output: Path,
                timeout: float) -> dict[str, Any]:
    runner = RoleRunner(config, production_backend(), IsolationContext("reviewer", workspace, context.control_root,
                                                                       context.repo))
    return runner.run("reviewer", prompt, output, timeout)
