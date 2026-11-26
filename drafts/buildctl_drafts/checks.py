"""The checks every draft passes before an operator sees it: its contract, its claims and its voice.

These are plain code, not a model's judgement. A claim counts as backed when it cites a source that
exists in the milestone: a commit in the range, a quoted passage of a file (at a real line) at the
milestone's commit, a quoted changelog line, or an open finding. The checks do not prove the source
supports the claim, or that every statement in the body is in the ledger: that stays the operator's
read, with the ledger beside the draft. A voice's rules are read from its own file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .context import Context

# The written drafts, in the spec's build order. Demo recording waits on loopback isolation in the kernel.
TYPES: dict[str, dict[str, Any]] = {
    "release-notes": {"voice": "product", "max_words": None, "title": "Release notes"},
    "positioning": {"voice": "product", "max_words": 120, "title": "Positioning"},
    "launch-post": {"voice": "product", "max_words": 650, "title": "Launch post"},
    "demo-script": {"voice": "product", "max_words": 900, "title": "Demo script"},
    "social": {"voice": "operator", "max_words": 250, "title": "Social post"},
}
SOURCE_KINDS = ("commit", "file", "changelog", "finding")
DEFAULT_BANNED = ("blazing", "seamless", "revolutionary", "game-changing", "world-class", "cutting-edge",
                  "best-in-class", "next-generation")
DESCRIPTION_LIMIT = 350          # GitHub's repository description limit
TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
TOPIC_LIMIT = 20


@dataclass
class Voice:
    name: str
    text: str = ""
    banned: list[str] = field(default_factory=list)
    no_em_dashes: bool = False
    max_words: dict[str, int] = field(default_factory=dict)


def parse_voice(name: str, text: str | None, extra_banned: list[str] | None = None) -> Voice:
    """Machine rules from a voice file's `## Rules` bullets; the whole file also goes to the writer.

    Understood bullets: `Banned: a, b`, `No em dashes`, `Max words: N`, `Max words for social: N`.
    Anything else in the file is guidance for the writer only."""
    voice = Voice(name, text or "", banned=list(extra_banned or []))
    if name == "product":
        voice.banned += [phrase for phrase in DEFAULT_BANNED if phrase not in voice.banned]
    in_rules = False
    for line in (text or "").splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            in_rules = heading.group(1).strip().lower().startswith("rules")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if not in_rules or not bullet:
            continue
        rule = bullet.group(1).strip()
        lowered = rule.lower()
        if lowered.startswith(("banned:", "banned phrases:", "never use:")):
            voice.banned += [item.strip().strip("\"'`") for item in rule.split(":", 1)[1].split(",") if item.strip()]
        elif "em dash" in lowered and lowered.startswith(("no ", "never")):
            voice.no_em_dashes = True
        else:
            words = re.match(r"max words(?: for ([a-z-]+))?:\s*(\d+)", lowered)
            if words:
                voice.max_words[words.group(1) or "*"] = int(words.group(2))
    return voice


def validate_contract(kind: str, value: dict[str, Any]) -> list[str]:
    problems = []
    required = {"title", "body", "claims", "known_limits", "fields"}
    if not isinstance(value, dict) or set(value) != required:
        return [f"the draft must have exactly these keys: {', '.join(sorted(required))}"]
    if not isinstance(value["title"], str) or not isinstance(value["body"], str) or not value["body"].strip():
        problems.append("title and body must be text, and the body not empty")
    if not isinstance(value["known_limits"], list) or not all(isinstance(item, str) for item in value["known_limits"]):
        problems.append("known_limits must be a list of text")
    if not isinstance(value["fields"], dict):
        problems.append("fields must be an object")
    if not isinstance(value["claims"], list):
        problems.append("claims must be a list")
        return problems
    if not value["claims"]:
        problems.append("the draft cites no claims; every factual statement needs one")
    for number, claim in enumerate(value["claims"], 1):
        source = claim.get("source") if isinstance(claim, dict) else None
        if not isinstance(claim, dict) or set(claim) != {"text", "source"} or not isinstance(claim["text"], str) \
                or not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS \
                or not isinstance(source.get("ref"), str):
            problems.append(f"claim {number} must be {{text, source: {{kind, ref, line, quote}}}} with kind one of "
                            f"{', '.join(SOURCE_KINDS)}")
    if kind == "positioning" and isinstance(value["fields"], dict):
        fields = value["fields"]
        if not isinstance(fields.get("description"), str) or not isinstance(fields.get("topics"), list):
            problems.append("positioning fields need a description and a list of topics")
    return problems


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def check_claim(claim: dict[str, Any], context: Context, read: Any) -> str | None:
    """None when the claim's source exists in the milestone; otherwise why not.

    `read(path)` returns a file's text at the milestone's commit, or None."""
    source = claim["source"]
    kind, ref = source["kind"], source["ref"].strip()
    quote = source.get("quote")
    if kind == "commit":
        if len(ref) < 7 or not re.fullmatch(r"[0-9a-f]+", ref):
            return f"commit {ref!r} is not a commit id"
        if not any(item["sha"].startswith(ref) for item in context.commits):
            return f"commit {ref[:12]} is not in the milestone's range"
        return None
    if kind == "finding":
        return None if any(item.get("claim_key") == ref for item in context.findings) else \
            f"no open finding has claim_key {ref!r}"
    if kind == "changelog":
        if not context.changelog:
            return "the milestone has no changelog"
        if not isinstance(quote, str) or not quote.strip():
            return "a changelog claim must quote the changelog line"
        return None if _normalized(quote) in _normalized(context.changelog) else "the quoted text is not in the changelog"
    # file
    if ref not in context.files:
        return f"{ref} does not exist at the milestone's commit"
    text = read(ref)
    if text is None:
        return f"{ref} is not a text file"
    if not isinstance(quote, str) or not quote.strip():
        return f"a file claim must quote the text in {ref} that backs it"
    line = source.get("line")
    if line is not None:
        if not isinstance(line, int) or not 1 <= line <= len(text.splitlines()):
            return f"{ref} has no line {line}"
    if _normalized(quote) not in _normalized(text):
        return f"the quoted text is not in {ref}"
    return None


def lint(kind: str, value: dict[str, Any], voice: Voice) -> list[str]:
    """The voice's rules and the draft type's limits."""
    problems = []
    text = "\n".join([value.get("title", ""), value.get("body", ""),
                      *(str(item) for item in (value.get("fields") or {}).values() if isinstance(item, str))])
    lowered = text.lower()
    for phrase in voice.banned:
        if phrase and phrase.lower() in lowered:
            problems.append(f"uses a banned phrase: \"{phrase}\"")
    if voice.no_em_dashes and "—" in text:
        problems.append("uses an em dash, which this voice bans")
    limit = voice.max_words.get(kind) or voice.max_words.get("*") or TYPES[kind]["max_words"]
    words = len(re.findall(r"\b[\w'-]+\b", value.get("body", "")))
    if limit and words > limit:
        problems.append(f"the body is {words} words; the limit is {limit}")
    if kind == "positioning":
        fields = value.get("fields") or {}
        description = fields.get("description") or ""
        if len(description) > DESCRIPTION_LIMIT:
            problems.append(f"the repository description is {len(description)} characters; GitHub allows {DESCRIPTION_LIMIT}")
        topics = fields.get("topics") or []
        bad = [topic for topic in topics if not isinstance(topic, str) or not TOPIC.match(topic)]
        if bad:
            problems.append(f"topics must be lowercase words joined by hyphens: {', '.join(map(str, bad))}")
        if len(topics) > TOPIC_LIMIT:
            problems.append(f"{len(topics)} topics; GitHub allows {TOPIC_LIMIT}")
    return problems


def review(kind: str, value: dict[str, Any], context: Context, voice: Voice, read: Any) -> dict[str, Any]:
    """Every check, as a report stored with the draft."""
    contract = validate_contract(kind, value)
    if contract:
        return {"passed": False, "contract": contract, "unbacked": [], "lint": [], "claims": 0}
    unbacked = []
    for number, claim in enumerate(value["claims"], 1):
        why = check_claim(claim, context, read)
        if why:
            unbacked.append({"claim": number, "text": claim["text"], "why": why})
    style = lint(kind, value, voice)
    return {"passed": not unbacked and not style, "contract": [], "unbacked": unbacked, "lint": style,
            "claims": len(value["claims"])}
