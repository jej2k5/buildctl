"""An MCP stdio server on the standard library: newline-delimited JSON-RPC 2.0.

Stdout carries protocol messages only. The speaker's identity is pinned at launch; operator
tools are not even listed for an owner. Every mutation re-renders the proposal."""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from importlib import resources
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__
from .check import check
from .record import CONFIDENCE, Identity, RecordError, Session
from .render import digest, read_proposal, render, write_proposal
from .review import request_review

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

_LIST_MODE = {"type": "object", "minProperties": 1, "maxProperties": 1,
              "properties": {"add": {"type": "array", "items": {"type": "string"}},
                             "set": {"type": "array", "items": {"type": "string"}}}}
_ARGV = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_COMMANDS_MODE = {"type": "object", "minProperties": 1, "maxProperties": 1,
                  "properties": {"add": {"type": "array", "items": _ARGV}, "set": {"type": "array", "items": _ARGV}}}
ACCEPTANCE = {
    "type": "object",
    "description": "Each list is {\"add\": [...]} to extend the project defaults or {\"set\": [...]} to replace "
                   "them. Example: {\"commands\": {\"add\": [[\"{python}\", \"-m\", \"unittest\", "
                   "\"tests.test_retry\"]]}, \"allowed_paths\": {\"add\": [\"src/retry/\"]}}",
    "properties": {"required_files": _LIST_MODE, "commands": _COMMANDS_MODE, "allowed_paths": _LIST_MODE,
                   "invariants": _LIST_MODE, "merge_policy": {"type": "string", "enum": ["auto", "human"]}},
    "additionalProperties": False}


def _schema(properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


_TEXT = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}


class Tool:
    def __init__(self, name: str, description: str, schema: dict[str, Any], handler: Callable[..., Any], *,
                 operator: bool = False, mutates: bool = True):
        self.name, self.description, self.schema = name, description, schema
        self.handler, self.operator, self.mutates = handler, operator, mutates

    def listing(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


class Server:
    def __init__(self, session: Session, repo: Path, identity: Identity, *, runner: Callable[..., Any] = subprocess.run):
        self.session, self.repo, self.identity, self.runner = session, repo, identity, runner
        self.client: str | None = None
        self.tools = {tool.name: tool for tool in self._tools()
                      if not tool.operator or identity.role == "operator"}

    def _tools(self) -> list[Tool]:
        s, me = self.session, self.identity
        return [
            Tool("get_session", "The whole record: thesis, decisions, open questions, phases with their numbers, "
                 "milestones, reviews and accepted risks, plus the proposal digest. Call this first to resume.",
                 _schema(), lambda: self._session_view(), mutates=False),
            Tool("set_thesis", "Who the product is for and what success means, in a sentence a test could check.",
                 _schema({"text": _TEXT}, ("text",)), lambda text: s.set_thesis(text, me)),
            Tool("add_principle", "An invariant every phase must keep. Returns its id (P-n).",
                 _schema({"text": _TEXT}, ("text",)), lambda text: s.add_principle(text, me)),
            Tool("record_decision", "Record a decision in the speaker's words, as soon as it is made. Returns its id "
                 "(D-n). To change a decision, record a new one with `supersedes`.",
                 _schema({"text": _TEXT, "section": {"type": "string", "description": "Group, e.g. Scope, Security"},
                          "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                          "supersedes": {"type": "string", "description": "The D-n this replaces"}}, ("text",)),
                 lambda text, section="General", confidence="high", supersedes=None:
                     s.record_decision(text, me, section=section, confidence=confidence, supersedes=supersedes)),
            Tool("defer_decision", "Mark a decision as out of this roadmap, with the reason.",
                 _schema({"decision_id": _TEXT, "reason": _TEXT}, ("decision_id", "reason")),
                 lambda decision_id, reason: s.defer_decision(decision_id, reason, me)),
            Tool("add_open_question", "Something that cannot be decided yet. Returns its id (Q-n).",
                 _schema({"text": _TEXT}, ("text",)), lambda text: s.add_open_question(text, me)),
            Tool("resolve_open_question", "Close an open question, naming the decisions that answered it.",
                 _schema({"question_id": _TEXT, "resolution": _TEXT, "decisions": _IDS}, ("question_id", "resolution")),
                 lambda question_id, resolution, decisions=(): s.resolve_open_question(
                     question_id, resolution, me, decisions=decisions)),
            Tool("propose_phase", "Add a phase. It must cite the decisions it implements and carry acceptance that "
                 "proves its own work. Returns its id (PH-n); phase numbers follow order.",
                 _schema({"name": _TEXT, "objective": _TEXT, "implements": _IDS, "acceptance": ACCEPTANCE,
                          "details": {"type": "string", "description": "Extra Markdown for the phase prompt"},
                          "position": {"type": "integer", "minimum": 0, "description": "Insert here instead of "
                                       "appending"}}, ("name", "objective", "implements", "acceptance")),
                 lambda name, objective, implements, acceptance, details="", position=None: s.propose_phase(
                     name, objective, me, implements=implements, acceptance=acceptance, details=details,
                     position=position)),
            Tool("revise_phase", "Change a phase's name, objective, details, implements or acceptance. Acceptance "
                 "is replaced whole.",
                 _schema({"phase_id": _TEXT, "changes": {"type": "object", "properties": {
                     "name": _TEXT, "objective": _TEXT, "details": _TEXT, "implements": _IDS,
                     "acceptance": ACCEPTANCE}, "additionalProperties": False}}, ("phase_id", "changes")),
                 lambda phase_id, changes: s.revise_phase(phase_id, changes, me)),
            Tool("withdraw_phase", "Remove a phase, with the reason. Its id is never reused.",
                 _schema({"phase_id": _TEXT, "reason": _TEXT}, ("phase_id", "reason")),
                 lambda phase_id, reason: s.withdraw_phase(phase_id, reason, me)),
            Tool("reorder_phases", "Set the build order. List every current phase id exactly once.",
                 _schema({"order": _IDS}, ("order",)), lambda order: s.reorder_phases(order, me)),
            Tool("set_milestones", "The MVP cut, named milestones and the phases before which the build stops for "
                 "operator approval. All by phase id.",
                 _schema({"milestones": {"type": "object", "additionalProperties": {"type": "string"},
                                         "description": "e.g. {\"mvp\": \"PH-9\"}"},
                          "checkpoints_before": _IDS, "mvp_completion": _TEXT},
                         ("milestones", "checkpoints_before", "mvp_completion")),
                 lambda milestones, checkpoints_before, mvp_completion: s.set_milestones(
                     milestones, checkpoints_before, mvp_completion, me)),
            Tool("check", "Run the deterministic gate: buildctl validate plus discovery's rules. Returns every "
                 "failure with the record entries to fix.", _schema(), lambda: self._check(), mutates=False),
            Tool("request_review", "Have the project's reviewer command review the current proposal under "
                 "buildctl-roadmap-review-v1. Takes minutes. Findings land in the record.",
                 _schema(), lambda: request_review(s, me, runner=self.runner, client=self.client)),
            Tool("accept_risk", "Operator only. Accept an open question (Q-n) or a review finding (F-n) as a "
                 "risk instead of resolving it, with the reason.",
                 _schema({"target": _TEXT, "reason": _TEXT}, ("target", "reason")),
                 lambda target, reason: s.accept_risk(target, reason, me), operator=True),
        ]

    def _session_view(self) -> dict[str, Any]:
        state = self.session.state()
        return dict(state.as_dict(), proposal_digest=digest(render(state)), proposal_dir=str(self.session.proposal_dir),
                    you=self.identity.as_dict())

    def _check(self) -> dict[str, Any]:
        return check(self.session.state(), self.repo, read_proposal(self.session.proposal_dir))

    def refresh(self) -> str:
        # Under the session lock, so an owner and an operator server cannot interleave renders.
        with self.session.lock():
            files = render(self.session.state())
            write_proposal(self.session.proposal_dir, files)
        return digest(files)

    # JSON-RPC

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return self._error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST,
                               "not a JSON-RPC 2.0 request")
        ident, method, params = message.get("id"), message["method"], message.get("params") or {}
        if "id" not in message:
            return None  # notifications, including notifications/initialized
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.client = (params.get("clientInfo") or {}).get("name")
            return self._result(ident, {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": "buildctl-discover", "version": __version__},
                "instructions": "Load the discovery_interview prompt before you start, and call get_session to "
                                "resume. Write nothing into the proposal yourself; use the tools."})
        if method == "ping":
            return self._result(ident, {})
        if method == "tools/list":
            return self._result(ident, {"tools": [tool.listing() for tool in self.tools.values()]})
        if method == "tools/call":
            return self._call(ident, params)
        if method == "prompts/list":
            return self._result(ident, {"prompts": [{"name": "discovery_interview",
                                                     "description": "How to run a discovery interview"}]})
        if method == "prompts/get":
            if params.get("name") != "discovery_interview":
                return self._error(ident, INVALID_PARAMS, f"unknown prompt: {params.get('name')}")
            guide = resources.files("buildctl_discover").joinpath("protocol/interview.md").read_text()
            return self._result(ident, {"description": "How to run a discovery interview",
                                        "messages": [{"role": "user", "content": {"type": "text", "text": guide}}]})
        return self._error(ident, METHOD_NOT_FOUND, f"unknown method: {method}")

    def _call(self, ident: Any, params: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(params.get("name"))
        if tool is None:
            return self._error(ident, INVALID_PARAMS, f"unknown tool: {params.get('name')}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(ident, INVALID_PARAMS, "arguments must be an object")
        unknown = set(arguments) - set(tool.schema["properties"])
        missing = [name for name in tool.schema["required"] if name not in arguments]
        if unknown or missing:
            problem = "; ".join([*(f"unknown argument: {name}" for name in sorted(unknown)),
                                 *(f"missing argument: {name}" for name in missing)])
            return self._result(ident, {"content": [{"type": "text", "text": problem}], "isError": True})
        try:
            value = tool.handler(**arguments)
            payload: dict[str, Any] = value if isinstance(value, dict) else {"ok": True, **(
                {"id": value} if value is not None else {})}
            if tool.mutates:
                payload = dict(payload, proposal_digest=self.refresh())
            return self._result(ident, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                                        "isError": False})
        except RecordError as exc:
            return self._result(ident, {"content": [{"type": "text", "text": str(exc)}], "isError": True})

    @staticmethod
    def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    @staticmethod
    def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}

    def serve(self, reader: TextIO = sys.stdin, writer: TextIO = sys.stdout, log: TextIO = sys.stderr) -> None:
        for line in reader:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                message, response = None, self._error(None, PARSE_ERROR, "invalid JSON")
            else:
                try:
                    response = self.handle(message)
                except Exception as exc:  # one bad request must not end the session
                    traceback.print_exc(file=log)
                    ident = message.get("id") if isinstance(message, dict) else None
                    response = self._error(ident, -32603, f"internal error: {type(exc).__name__}: {exc}")
            if response is not None:
                writer.write(json.dumps(response) + "\n")
                writer.flush()
