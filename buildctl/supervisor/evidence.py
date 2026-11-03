"""What each agent did during a phase: recorded as it happens, sealed as evidence afterward.

Most of a record is the agent's own account, written by the process under supervision. So every
row says where it came from, and nothing here turns a reported row into a fact. See
docs/planning/agent-evidence.md for what this proves and, as importantly, what it does not.

Two layers. The full record stays in the control root until the operator prunes it. The manifest
on the evidence branch carries the actions and the record's hash, and travels with the repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .safety import matches_secret_pattern, redact

EVIDENCE_BRANCH = "buildctl/evidence"
EVIDENCE_REF = f"refs/heads/{EVIDENCE_BRANCH}"
REMOTE_EVIDENCE_REF = f"refs/remotes/origin/{EVIDENCE_BRANCH}"
MANIFEST_SCHEMA = 1
MAX_LINE_BYTES = 64 * 1024 * 1024
TARGET_LIMIT = 500
FEED_LIMIT = 400
NOT_PROVED = ("That an agent's report is true or complete. Only observed rows and flags go beyond its word.",
              "That this receipt was not rewritten before you received it. It is not signed.",
              "Why an agent did anything, or what happened inside an interactive debate. A debate "
              "buildctl ran is listed turn by turn; its arguments stay in the full records.")
NO_RECEIPT = ("No receipt for this commit. Recording may have been off for this phase, or the receipt "
              "may be missing; this check cannot tell which, so a missing receipt proves nothing.")
# Agent text reaches a console holding approval levers. Control characters go, escape codes first.
CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Beyond the shapes in safety.py: what shows up on a command line and nowhere else.
COMMAND_SECRETS = (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
                   re.compile(r"(://[^/\s:@]+:)[^/\s@]+(?=@)"),
                   re.compile(r"(\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)=)\S+"),
                   re.compile(r"(?i)((?:--)?(?:password|passwd|token|secret|api[-_]?key)[= ])\S+"))
HEADER_FIELDS = ("operation", "phase", "attempt", "role", "cli", "binary_sha256", "prompt_sha256", "command",
                 "workspace", "started_at")


class RecordIncomplete(RuntimeError):
    """Part of a phase went unrecorded. That footage cannot be recovered, so the build stops."""


class EvidenceError(RuntimeError):
    pass


def recording_enabled(config: dict[str, Any]) -> bool:
    return config.get("evidence", {}).get("recording", "on") == "on"


def stream_flags_missing(command: list[str]) -> list[str]:
    """The event flags this CLI needs before its work can be recorded. Others record raw lines."""
    name = Path(command[0]).name
    if name == "codex":
        return [] if "--json" in command else ["--json"]
    if name == "claude":
        pair = any(a == "--output-format" and b == "stream-json" for a, b in zip(command, command[1:]))
        return ([] if pair else ["--output-format stream-json"]) + ([] if "--verbose" in command else ["--verbose"])
    return []


def validate_evidence_config(config: dict[str, Any]) -> list[str]:
    """Problems with the evidence settings, so a build is refused before it runs, not during."""
    settings = config.get("evidence", {})
    if not isinstance(settings, dict) or settings.get("recording", "on") not in {"on", "off"}:
        return ['evidence.recording must be "on" or "off"']
    if settings.get("recording", "on") == "off":
        return []
    problems = []
    for name, role in config.get("roles", {}).items():
        if role.get("mode") in {"interactive", "debate"}:
            continue  # No command of its own runs: a human debates, or the other two roles do.
        missing = stream_flags_missing(role.get("command", [""]))
        if missing:
            problems.append(f"role {name} command needs {' and '.join(missing)} because recording is on; "
                            'add them, or set evidence.recording to "off"')
    return problems


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_path(path: Path) -> str:
    return digest(path.read_bytes())


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _map_strings(value: Any, function: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return function(value)
    if isinstance(value, dict):
        return {key: _map_strings(item, function) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, function) for item in value]
    return value


def known_secret(text: str, labelled: dict[str, str]) -> str | None:
    """The kind of credential buildctl was given, if this text holds one. Never its value."""
    return next((kind for kind, value in labelled.items() if value and value in text), None)


def scrub(text: str, secrets: list[str] | None = None) -> tuple[str, bool]:
    """Best-effort redaction for what leaves the machine. It lowers the risk; it cannot remove it."""
    cleaned = redact(text, secrets, limit=len(text))
    for pattern in COMMAND_SECRETS:
        cleaned = pattern.sub(lambda m: m.group(1) + "[REDACTED]", cleaned)
    return cleaned, cleaned != text


def _printable(value: Any, limit: int = TARGET_LIMIT) -> str:
    return CONTROL.sub("", str(value if value is not None else ""))[:limit]


class StreamAdapter:
    """Turn one CLI's event stream into action rows. Knows nothing about what they mean."""
    name = "raw"
    has_final_event = False

    def __init__(self) -> None:
        self.final_seen, self.failed = False, False
        self.result_text: str | None = None
        self.error_text: str | None = None
        self.usage: dict[str, Any] = {}

    def feed(self, line: str) -> tuple[list[dict[str, Any]], bool]:
        """Actions in this stdout line, and whether the line was unrecognized."""
        return [], False

    @staticmethod
    def _decode(line: str) -> Any:
        try:
            return json.loads(line)
        except ValueError:
            return None


class RawAdapter(StreamAdapter):
    """A CLI with no event format: lines are kept and its stdout stays the result, as today."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []

    def feed(self, line: str) -> tuple[list[dict[str, Any]], bool]:
        self._lines.append(line)
        self.result_text = "\n".join(self._lines)
        return [], False


class CodexAdapter(StreamAdapter):
    name = "codex"
    has_final_event = True
    IGNORED = frozenset({"thread.started", "turn.started", "item.updated"})

    def feed(self, line: str) -> tuple[list[dict[str, Any]], bool]:
        event = self._decode(line)
        if not isinstance(event, dict):
            return [], True
        kind = event.get("type")
        if kind == "turn.completed":
            self.final_seen, self.usage = True, event.get("usage") or {}
            return [], False
        if kind in {"turn.failed", "error"}:
            self.failed = True
            problem = event.get("error") if isinstance(event.get("error"), dict) else event
            self.error_text = str(problem.get("message", ""))[:TARGET_LIMIT]
            return [], False
        if kind in self.IGNORED:
            return [], False
        if kind not in {"item.started", "item.completed"} or not isinstance(event.get("item"), dict):
            return [], True
        return self._item(event["item"], kind == "item.completed")

    def _item(self, item: dict[str, Any], done: bool) -> tuple[list[dict[str, Any]], bool]:
        kind, identity = item.get("type"), item.get("id")
        if kind == "command_execution":
            return [{"id": identity, "kind": "ran", "target": _unwrap_shell(item.get("command", "")),
                     "exit_code": item.get("exit_code")}], False
        if kind == "file_change":
            if not done or item.get("status") == "failed":
                return [], False
            return [{"id": f"{identity}:{index}", "kind": "edit", "target": change.get("path", ""),
                     "detail": change.get("kind")}
                    for index, change in enumerate(item.get("changes") or []) if isinstance(change, dict)], False
        if kind == "agent_message":
            return [{"id": identity, "kind": "message", "target": item.get("text", "")}], False
        if kind == "reasoning":
            return [{"id": identity, "kind": "reasoning", "target": item.get("text", "")}], False
        if kind in {"mcp_tool_call", "web_search"}:
            target = item.get("query") or item.get("tool") or kind
            return [{"id": identity, "kind": "tool", "target": target, "tool": kind}], False
        if kind == "todo_list":
            return [], False
        return [], True


class ClaudeAdapter(StreamAdapter):
    name = "claude"
    has_final_event = True
    IGNORED = frozenset({"system", "user", "rate_limit_event", "stream_event"})
    WRITES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
    READS = frozenset({"Read", "NotebookRead"})
    SEARCHES = frozenset({"Grep", "Glob"})

    def feed(self, line: str) -> tuple[list[dict[str, Any]], bool]:
        event = self._decode(line)
        if not isinstance(event, dict):
            return [], True
        kind = event.get("type")
        if kind == "result":
            self.final_seen = True
            self.result_text = event.get("result") if isinstance(event.get("result"), str) else None
            self.usage = dict(event.get("usage") or {})
            if isinstance(event.get("total_cost_usd"), (int, float)):
                self.usage["reported_cost_usd"] = event["total_cost_usd"]
            if event.get("is_error"):
                self.failed, self.error_text = True, str(event.get("result") or event.get("subtype"))[:TARGET_LIMIT]
            return [], False
        if kind in self.IGNORED:
            return [], False
        content = (event.get("message") or {}).get("content") if kind == "assistant" else None
        if not isinstance(content, list):
            return [], True
        actions = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                actions.append({"id": None, "kind": "message", "target": block.get("text", "")})
            elif block.get("type") == "thinking":
                actions.append({"id": None, "kind": "reasoning", "target": block.get("thinking", "")})
            elif block.get("type") == "tool_use":
                actions.append(self._tool(block))
        return actions, False

    def _tool(self, block: dict[str, Any]) -> dict[str, Any]:
        name, values = str(block.get("name", "")), block.get("input") or {}
        if name in self.READS or name in self.WRITES:
            target = values.get("file_path") or values.get("notebook_path") or ""
            kind = "read" if name in self.READS else "edit"
        elif name in self.SEARCHES:
            kind, target = "search", values.get("pattern", "")
        elif name == "Bash":
            kind, target = "ran", values.get("command", "")
        else:
            kind, target = "tool", name
        return {"id": block.get("id"), "kind": kind, "target": target, "tool": name}


def _unwrap_shell(command: str) -> str:
    """`/bin/zsh -lc 'pytest'` is the agent's harness talking. The command is `pytest`."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and Path(parts[0]).name in {"sh", "bash", "zsh"} and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return command


def adapter_for(command: list[str]) -> StreamAdapter:
    return {"codex": CodexAdapter, "claude": ClaudeAdapter}.get(Path(command[0]).name, RawAdapter)()


class AgentRecord:
    """One operation's record: append-only JSONL, each line linked to the last by hash."""

    def __init__(self, path: Path, header: dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.previous, self.sequence = path, "0" * 64, -1
        self.unparsed = 0
        self._stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.append("header", "", header=header)

    def append(self, stream: str, text: str, redacted: bool = False, **extra: Any) -> None:
        self.sequence += 1
        row = {"seq": self.sequence, "at": now(), "stream": stream, "text": text, "redacted": redacted,
               "previous_hash": self.previous, **extra}
        row["hash"] = digest(canonical(row))
        self._stream.write(json.dumps(row, sort_keys=True) + "\n")
        self._stream.flush()
        self.previous = row["hash"]

    def close(self, *, exit_code: int | None, terminated: str | None) -> dict[str, Any]:
        self.append("footer", "", footer={"exit_code": exit_code, "terminated": terminated, "ended_at": now(),
                                          "unparsed_events": self.unparsed})
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        return {"file": self.path.name, "sha256": digest(self.path.read_bytes()), "lines": self.sequence + 1,
                "terminated": terminated}

    def abandon(self) -> None:
        try:
            self._stream.close()
        except OSError:
            pass


@dataclass
class EvidenceStore:
    """The records of one build. The supervisor names the operation; the runner records into it."""
    control_root: Path
    operation: tuple[str, int, int] | None = None
    closed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        return self.control_root / "records"

    def path_for(self, operation: str, phase: int) -> Path:
        # An operation id can run twice: `reconcile authorize-retry` reuses it.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", operation)
        directory = self.directory / f"phase-{phase}"
        run = 1
        while (directory / f"{safe}.{run}.jsonl").exists():
            run += 1
        return directory / f"{safe}.{run}.jsonl"

    def open(self, role: str, command: list[str], binary: str, prompt: str, workspace: Path) -> AgentRecord:
        if self.operation is None:
            raise RecordIncomplete("recording is on but no operation was named")
        operation, phase, attempt = self.operation
        try:
            binary_sha = digest(Path(binary).read_bytes())
            record = AgentRecord(self.path_for(operation, phase), {
                "operation": operation, "phase": phase, "attempt": attempt, "role": role,
                "cli": adapter_for(command).name, "binary_sha256": binary_sha,
                "prompt_sha256": digest(prompt.encode()), "command": command,
                "workspace": str(workspace.resolve()), "started_at": now()})
            cleaned = redact(prompt, limit=len(prompt))
            record.append("prompt", cleaned, cleaned != prompt)
            return record
        except OSError as exc:
            raise RecordIncomplete(f"cannot start the record for {operation}: {exc.strerror}") from exc


@dataclass
class StreamResult:
    returncode: int | None
    exposed: str | None = None
    exposed_source: str = "stdout_stderr"
    timed_out: bool = False
    stderr_tail: str = ""
    stdout_tail: str = ""


def _reader(stream: Any, name: str, lines: "queue.Queue[tuple[str, str | None]]") -> None:
    try:
        while True:
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES:
                lines.put((name, "\0OVERSIZE"))
                break
            lines.put((name, raw.decode("utf-8", "replace").rstrip("\r\n")))
    finally:
        lines.put((name, None))


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def _drain_and_close(process: subprocess.Popen, readers: list[threading.Thread], lines: "queue.Queue[Any]") -> None:
    """Release the pipes. A reader still blocked on a descendant's pipe is left to its daemon thread."""
    limit = time.monotonic() + 2
    while any(reader.is_alive() for reader in readers) and time.monotonic() < limit:
        try:
            lines.get(timeout=0.05)  # A reader waiting on a full queue must be able to finish.
        except queue.Empty:
            pass
    for reader, stream in zip(readers, (process.stdout, process.stderr)):
        if not reader.is_alive():
            stream.close()


def _clean_line(stream: str, line: str, labelled: dict[str, str]) -> tuple[str, bool, str | None]:
    """A line made safe to keep: the text, whether it changed, and any known credential in it.

    The raw check alone misses a secret containing quotes or newlines, because JSONL escapes it,
    so each event's decoded strings are checked too. A pattern match only redacts: an agent
    reading a source file holding `token = "abc123..."` has not leaked anything of its own, and
    would otherwise stop the build. The result itself is still checked as strictly as before.
    """
    exposed = known_secret(line, labelled)
    value = None
    if stream == "stdout" and line[:1] in "{[":
        try:
            value = json.loads(line)
        except ValueError:
            value = None
    if value is not None:
        exposed = exposed or next(filter(None, (known_secret(text, labelled) for text in _strings(value))), None)
        if exposed is None and any(matches_secret_pattern(text) for text in _strings(value)):
            return json.dumps(_map_strings(value, lambda text: redact(text, limit=len(text)))), True, None
        return line, False, exposed
    if exposed is None and matches_secret_pattern(line):
        return redact(line, limit=len(line)), True, None
    return line, False, exposed


def stream_process(command: list[str], *, cwd: Path, env: dict[str, str], timeout: float, record: AgentRecord,
                   adapter: StreamAdapter, labelled: dict[str, str]) -> StreamResult:
    """Run an agent, writing each line to its record as it arrives.

    Any failure to record kills the agent: paying for work nobody can later see is the outcome
    the operator asked to rule out. A timeout is different, because the record of a failed
    operation is still complete.
    """
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    lines: "queue.Queue[tuple[str, str | None]]" = queue.Queue(maxsize=4096)
    readers = [threading.Thread(target=_reader, args=(stream, name, lines), daemon=True)
               for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
    for reader in readers:
        reader.start()
    result = StreamResult(None)
    deadline, open_streams = time.monotonic() + timeout, 2
    failure: str | None = None
    try:
        while open_streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not result.timed_out:
                result.timed_out = True
                _kill(process)
                deadline = time.monotonic() + 5  # Let what it already wrote drain into the record.
            elif remaining <= 0:
                break
            try:
                name, line = lines.get(timeout=min(0.2, max(remaining, 0.01)))
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            if line == "\0OVERSIZE":
                failure = f"a {name} line exceeded {MAX_LINE_BYTES} bytes"
                break
            text, redacted, exposed = _clean_line(name, line, labelled)
            if exposed:
                result.exposed = exposed
                record.append(name, f"[REMOVED: credential kind {exposed}]", True)
                break
            record.append(name, text, redacted)
            if name == "stdout":
                record.unparsed += int(adapter.feed(text)[1])
                result.stdout_tail = text[-2000:]
            else:
                result.stderr_tail = (result.stderr_tail + "\n" + text)[-2000:]
    except (OSError, ValueError) as exc:
        failure = f"writing the record failed: {exc}"
    finally:
        if failure or result.exposed or process.poll() is None and (result.timed_out or open_streams):
            _kill(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _drain_and_close(process, readers, lines)
    result.returncode = process.returncode
    if failure:
        record.abandon()
        raise RecordIncomplete(failure)
    terminated = "timeout" if result.timed_out else "secret" if result.exposed else None
    try:
        record.close(exit_code=result.returncode, terminated=terminated)
    except (OSError, ValueError) as exc:
        record.abandon()
        raise RecordIncomplete(f"closing the record failed: {exc}") from exc
    return result


def read_record(path: Path) -> dict[str, Any]:
    """Parse and check one record. A record missing its footer, or with a broken link, says so."""
    rows, previous, problem = [], "0" * 64, None
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        return {"path": path, "rows": [], "header": {}, "footer": None, "problem": str(exc)}
    for number, line in enumerate(lines):
        try:
            row = json.loads(line)
            material = {key: value for key, value in row.items() if key != "hash"}
            if row["seq"] != number or row["previous_hash"] != previous or row["hash"] != digest(canonical(material)):
                raise ValueError("link mismatch")
        except (ValueError, KeyError, TypeError):
            # The last line of a crashed run can be half-written. That is an unclosed record,
            # which the footer check reports. A bad line anywhere else is tampering.
            if number != len(lines) - 1:
                problem = f"line {number + 1} fails the chain"
            break
        rows.append(row)
        previous = row["hash"]
    header = rows[0].get("header", {}) if rows else {}
    footer = rows[-1].get("footer") if rows and rows[-1].get("stream") == "footer" else None
    return {"path": path, "rows": rows, "header": header, "footer": footer, "problem": problem, "head": previous}


def actions_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The agent-reported rows in a record, each carrying the record line it came from."""
    command = record["header"].get("command") or [""]
    adapter = adapter_for(command)
    workspace = record["header"].get("workspace") or ""
    # macOS reaches a temp directory as both /private/var/... and /var/..., and an agent may report either.
    prefixes = [workspace, workspace.removeprefix("/private")] if workspace else []
    actions: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in record["rows"]:
        if row["stream"] != "stdout":
            continue
        for action in adapter.feed(row["text"])[0]:
            target = str(action.get("target", ""))
            for prefix in prefixes:
                target = target.replace(prefix + "/", "")
            action.update(target=target, seq=row["seq"], at=row["at"], source="agent_reported")
            identity = action.get("id")
            if identity and identity in by_id:  # item.completed fills in what item.started began.
                # Point at the completing line: that one carries the command's output.
                by_id[identity].update(exit_code=action.get("exit_code"), target=target, seq=row["seq"])
                continue
            if identity:
                by_id[identity] = action
            actions.append(action)
    return actions


def _record_files(control_root: Path, phase: int) -> list[Path]:
    return sorted((control_root / "records" / f"phase-{phase}").glob("*.jsonl"))


def phase_records(control_root: Path, phase: int) -> list[dict[str, Any]]:
    return [read_record(path) for path in _record_files(control_root, phase)]


def unclosed_records(control_root: Path, phase: int, attempt: int) -> list[str]:
    """Records from this attempt without a clean ending. This attempt's workspace may hold their work."""
    return [record["path"].name for record in phase_records(control_root, phase)
            if record["problem"] or (record["footer"] is None and record["header"].get("attempt", attempt) == attempt)]


def is_debate_turn(operation: Any) -> bool:
    return isinstance(operation, str) and ":debate-" in operation and "-turn-" in operation


def discrepancies(records: list[dict[str, Any]], changed: tuple[str, ...] | list[str],
                  exempt: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Where the agent's account and the observed change disagree. Flags, never stops."""
    # A debate turn runs as the implementer against a read-only workspace. It wrote nothing,
    # so counting its account against the change would flag every debate.
    implementers = [record for record in records if record["header"].get("role") == "implementer"
                    and not is_debate_turn(record["header"].get("operation"))]
    actions = [action for record in implementers for action in actions_of(record)]
    written = {action["target"] for action in actions if action["kind"] == "edit"}
    commands = any(action["kind"] == "ran" for action in actions)
    delta = set(changed) - set(exempt)
    flags = []
    for path in sorted(delta - written):
        flags.append({"flag": "changed_possibly_by_command" if commands else "changed_unreported", "path": path})
    for path in sorted(written - delta):
        flags.append({"flag": "reported_no_net_change", "path": path})
    for record in records:
        footer = record["footer"] or {}
        if footer.get("unparsed_events"):
            flags.append({"flag": "events_unparsed", "operation": record["header"].get("operation"),
                          "count": footer["unparsed_events"]})
        if record["header"].get("role") == "reviewer":
            command = record["header"].get("command") or []
            allowed = set(command[command.index("--tools") + 1].split(",")) if "--tools" in command[:-1] else None
            for tool in sorted({action["tool"] for action in actions_of(record) if action.get("tool")}):
                if allowed is not None and tool not in allowed:
                    flags.append({"flag": "reviewer_tool_outside_allowlist", "tool": tool,
                                  "operation": record["header"].get("operation")})
    return flags


def _artifact_entries(control_root: Path, phase: int, interactive_debate: bool) -> list[dict[str, Any]]:
    entries = []
    for path in sorted((control_root / "artifacts" / f"phase-{phase}").glob("*.json")):
        name = path.name
        kind = next((label for prefix, label in (("GATE_RESULT", "gate"), ("REVIEW_RESULT", "review"),
                                                 ("DEBATE_DECISION", "operator_decision"),
                                                 ("DEBATE_TURN", "debate_turn"), ("DEBATE_", "debate"),
                                                 ("PHASE_RESULT", "implementer_result"))
                     if name.startswith(prefix)), None)
        if kind is None:
            continue
        entry = {"file": name, "kind": kind, "sha256": digest(path.read_bytes()),
                 "source": {"gate": "observed", "operator_decision": "operator",
                            "debate": "human_run" if interactive_debate else "agent_reported"}
                 .get(kind, "agent_reported")}
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            value = {}
        if kind == "gate":
            entry["passed"] = value.get("passed")
        if kind == "review":
            entry["verdict"] = value.get("verdict")
        entries.append(entry)
    return entries


def build_manifest(control_root: Path, config: dict[str, Any], *, phase: int, code_commit: str, tree_hash: str,
                   base_sha: str, governance_sha: str, attempt: int, previous: str | None,
                   flags: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    secrets = [os.environ[name] for role in config.get("roles", {}).values()
               for name in role.get("credential_env", []) if os.environ.get(name)]
    charged = {item["operation_id"]: item["cost_usd"] for item in ledger}
    attempts: dict[int, list[dict[str, Any]]] = {}
    for record in phase_records(control_root, phase):
        header, footer = record["header"], record["footer"]
        if record["problem"]:
            raise RecordIncomplete(f"{record['path'].name}: {record['problem']}")
        if footer is None and header.get("attempt") == attempt:
            raise RecordIncomplete(f"{record['path'].name} never closed")
        actions = []
        for action in actions_of(record):
            if action["kind"] in {"message", "reasoning"}:
                continue
            target, redacted = scrub(str(action["target"])[:TARGET_LIMIT], secrets)
            actions.append({"seq": action["seq"], "at": action["at"], "kind": action["kind"], "target": target,
                            "exit_code": action.get("exit_code"), "source": action["source"], "redacted": redacted})
        attempts.setdefault(header.get("attempt", 0), []).append({
            "operation": header.get("operation"), "role": header.get("role"), "cli": header.get("cli"),
            "binary_sha256": header.get("binary_sha256"), "prompt_sha256": header.get("prompt_sha256"),
            "started_at": header.get("started_at"), "ended_at": (footer or {}).get("ended_at"),
            "exit_code": (footer or {}).get("exit_code"), "terminated": (footer or {}).get("terminated"),
            "record_state": "closed" if footer else "incomplete",
            "record_file": record["path"].name, "record_sha256": digest(record["path"].read_bytes()),
            "record_lines": len(record["rows"]), "unparsed_events": (footer or {}).get("unparsed_events", 0),
            "ledger_usd": charged.get(header.get("operation")), "actions": actions})
    interactive = config.get("roles", {}).get("arbiter", {}).get("mode") == "interactive"
    return {"schema_version": MANIFEST_SCHEMA, "phase": phase, "code_commit": code_commit, "tree_hash": tree_hash,
            "base_sha": base_sha, "governance_sha": governance_sha, "previous_manifest_sha256": previous,
            "attempts": [{"attempt": number, "operations": operations} for number, operations in sorted(attempts.items())],
            "artifacts": _artifact_entries(control_root, phase, interactive), "flags": flags, "signatures": []}


def _trailers(message: str) -> dict[str, str]:
    return dict(match.groups() for match in re.finditer(r"^(Code-Commit|Manifest-Path|Manifest-Sha256): (\S+)$",
                                                        message, re.M))


class EvidenceBranch:
    """The receipts, written without the working tree: `ensure_safe` refuses any uncommitted work."""

    def __init__(self, root: Path, runner: Callable[..., Any] = subprocess.run, ref: str = EVIDENCE_REF):
        self.root, self.runner, self.ref = root, runner, ref

    def _git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> Any:
        base = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        base.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}, **(env or {}))
        result = self.runner(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.root, text=True,
                             capture_output=True, env=base)
        if check and result.returncode:
            raise EvidenceError(result.stderr.strip() or f"git {args[0]} failed")
        return result

    def head(self) -> str | None:
        result = self._git("rev-parse", "--verify", "--quiet", self.ref, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def entries(self) -> list[dict[str, str]]:
        """Every receipt on the branch, oldest first."""
        head = self.head()
        if not head:
            return []
        result = []
        for commit in self._git("rev-list", "--reverse", "--first-parent", head).stdout.split():
            trailers = _trailers(self._git("log", "-1", "--format=%B", commit).stdout)
            result.append({"commit": commit, **trailers})
        return result

    def blob(self, commit: str, path: str) -> str:
        return self._git("cat-file", "blob", f"{commit}:{path}").stdout

    def latest_manifest_sha(self) -> str | None:
        entries = self.entries()
        return entries[-1].get("Manifest-Sha256") if entries else None

    def find(self, code_commit: str) -> dict[str, str] | None:
        return next((item for item in self.entries() if item.get("Code-Commit") == code_commit), None)

    def append(self, manifest: dict[str, Any], scratch: Path) -> str:
        """Add one receipt. Idempotent, so a restart after a crash completes it."""
        existing = self.find(manifest["code_commit"])
        if existing:
            return existing["Manifest-Sha256"]
        data = canonical(manifest)
        path = f"phase-{manifest['phase']}/{manifest['code_commit']}/manifest.json"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            source = Path(directory) / "manifest.json"
            source.write_bytes(data)
            blob = self._git("hash-object", "-w", "--no-filters", str(source)).stdout.strip()
            parent = self.head()
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            if parent:
                self._git("read-tree", parent, env=env)
            self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = self._git("write-tree", env=env).stdout.strip()
            sha = digest(data)
            message = (f"Evidence for phase {manifest['phase']}\n\nCode-Commit: {manifest['code_commit']}\n"
                       f"Manifest-Path: {path}\nManifest-Sha256: {sha}\n")
            commit = self._git("commit-tree", tree, *(("-p", parent) if parent else ()), "-m", message).stdout.strip()
            # The expected old value makes this append-only: a concurrent writer fails here.
            self._git("update-ref", self.ref, commit, parent or "0" * 40)
        return sha


def resolve_branch(root: Path, runner: Callable[..., Any] = subprocess.run) -> EvidenceBranch:
    """The local branch, or origin's: a fresh clone holds only the remote-tracking ref."""
    branch = EvidenceBranch(root, runner)
    return branch if branch.head() else EvidenceBranch(root, runner, REMOTE_EVIDENCE_REF)


def _check_chain(branch: EvidenceBranch) -> tuple[list[dict[str, Any]], str | None]:
    """Each manifest matches its recorded hash, links to the last, and nothing earlier changed."""
    manifests, previous, parent = [], None, None
    for entry in branch.entries():
        path, expected = entry.get("Manifest-Path"), entry.get("Manifest-Sha256")
        if not path or not expected:
            return manifests, f"evidence commit {entry['commit'][:12]} has no manifest trailers"
        text = branch.blob(entry["commit"], path)
        if digest(text.encode()) != expected:
            return manifests, f"{path} does not match its recorded hash"
        value = json.loads(text)
        if canonical(value) != text.encode():
            return manifests, f"{path} is not in canonical form"
        if value.get("previous_manifest_sha256") != previous:
            return manifests, f"{path} does not link to the receipt before it"
        if value.get("code_commit") != entry.get("Code-Commit"):
            return manifests, f"{path} names a different commit than its trailer"
        if parent:
            changes = branch._git("diff-tree", "-r", "--name-status", parent, entry["commit"]).stdout.split("\n")
            if any(line and not line.startswith("A\t") for line in changes):
                return manifests, f"evidence commit {entry['commit'][:12]} changed an earlier receipt"
        manifests.append(value)
        previous, parent = expected, entry["commit"]
    return manifests, None


def _check_records(manifest: dict[str, Any], records_dir: Path | None) -> tuple[list[str], list[str]]:
    """Compare full records against the manifest. Returns (problems, notes)."""
    problems, notes = [], []
    directory = records_dir / f"phase-{manifest['phase']}" if records_dir else None
    for attempt in manifest["attempts"]:
        for operation in attempt["operations"]:
            path = directory / operation["record_file"] if directory else None
            if path is None or not path.is_file():
                notes.append(f"{operation['operation']}: full record not available, so only its hash is known")
                continue
            if digest(path.read_bytes()) != operation["record_sha256"]:
                problems.append(f"{operation['record_file']} does not match the hash in the receipt")
            elif read_record(path)["problem"]:
                problems.append(f"{operation['record_file']} fails its internal chain")
    return problems, notes


def verify(root: Path, commit: str, records_dir: Path | None = None,
           runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Check a commit's receipt. Needs a clone with the branch: no sandbox, trust or control root."""
    branch = resolve_branch(root, runner)
    resolved = branch._git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    report: dict[str, Any] = {"commit": commit, "not_proved": list(NOT_PROVED), "notes": [], "problems": []}
    if resolved.returncode:
        return {**report, "status": "failed", "problems": [f"unknown commit: {commit}"]}
    sha = resolved.stdout.strip()
    report["commit"] = sha
    tree = branch._git("rev-parse", f"{sha}^{{tree}}").stdout.strip()
    try:
        manifests, problem = _check_chain(branch)
    except (ValueError, EvidenceError) as exc:
        return {**report, "status": "failed", "problems": [f"the evidence branch cannot be read: {exc}"]}
    if problem:
        return {**report, "status": "failed", "problems": [problem]}
    manifest = next((item for item in manifests if item["code_commit"] == sha), None)
    by_tree = False
    if manifest is None:
        manifest = next((item for item in manifests if item["tree_hash"] == tree), None)
        by_tree = manifest is not None
    if manifest is None:
        return {**report, "status": "no_receipt", "notes": [NO_RECEIPT]}
    if not by_tree and manifest["tree_hash"] != tree:
        return {**report, "status": "failed", "problems": ["the commit's tree differs from the receipt's"]}
    problems, notes = _check_records(manifest, records_dir)
    report.update(phase=manifest["phase"], manifest_sha256=digest(canonical(manifest)), flags=manifest["flags"],
                  signed=bool(manifest["signatures"]), problems=problems, notes=notes)
    if by_tree:
        report["notes"].append(f"Matched by tree, not commit: the receipt names {manifest['code_commit'][:12]}, "
                               "as happens after a squash merge.")
    if problems:
        report["status"] = "failed"
    else:
        report["status"] = "verified_by_tree" if by_tree else "verified_with_flags" if manifest["flags"] else "verified"
    return report


def show(root: Path, commit: str, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any] | None:
    branch = resolve_branch(root, runner)
    resolved = branch._git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    if resolved.returncode:
        raise EvidenceError(f"unknown commit: {commit}")
    entry = branch.find(resolved.stdout.strip())
    return json.loads(branch.blob(entry["commit"], entry["Manifest-Path"])) if entry else None


def prune_records(control_root: Path, phase: int) -> dict[str, Any]:
    """Delete a phase's full records. Receipts stay; afterward each hash shows only that a record existed."""
    directory = control_root / "records" / f"phase-{phase}"
    files = _record_files(control_root, phase)
    removed = {path.name: digest(path.read_bytes()) for path in files}
    if directory.exists():
        shutil.rmtree(directory)
    return {"phase": phase, "removed": removed}


def activity(control_root: Path, phase: int, operation: str | None = None) -> dict[str, Any]:
    """The live view: a summary per agent call and a feed of rows, each saying who vouches for it."""
    operations, rows = [], []
    for path in _record_files(control_root, phase):
        stat = path.stat()
        record = _read_cached(str(path), stat.st_size, stat.st_mtime_ns)
        header, footer = record["header"], record["footer"]
        identity = path.stem
        actions = actions_of(record)
        adapter = adapter_for(header.get("command") or [""])
        for row in record["rows"]:
            if row["stream"] == "stdout":
                adapter.feed(row["text"])
        operations.append({
            "id": identity, "operation": header.get("operation"), "role": header.get("role"), "cli": header.get("cli"),
            "attempt": header.get("attempt"), "started_at": header.get("started_at"),
            "ended_at": (footer or {}).get("ended_at"), "exit_code": (footer or {}).get("exit_code"),
            "terminated": (footer or {}).get("terminated"), "state": "closed" if footer else "open",
            # Observed: buildctl stamped this when the line arrived. A quiet agent is all it shows.
            "last_output_at": record["rows"][-1]["at"] if record["rows"] else None,
            "files_reported": len({action["target"] for action in actions if action["kind"] == "edit"}),
            "commands_reported": sum(action["kind"] == "ran" for action in actions),
            "reported_usage": adapter.usage, "problem": record["problem"]})
        if operation and identity != operation:
            continue
        common = {"record": identity, "operation": header.get("operation"), "role": header.get("role")}
        if header:
            rows.append({**common, "seq": 0, "at": header.get("started_at"), "kind": "launched",
                         "target": header.get("cli") or header.get("role"), "source": "observed", "exit_code": None})
        for action in actions:
            rows.append({**common, "seq": action["seq"], "at": action["at"], "kind": action["kind"],
                         "target": _printable(action["target"]), "source": action["source"],
                         "exit_code": action.get("exit_code")})
        if footer:
            rows.append({**common, "seq": record["rows"][-1]["seq"], "at": footer.get("ended_at"), "kind": "exited",
                         "target": footer.get("terminated") or "", "source": "observed",
                         "exit_code": footer.get("exit_code")})
    rows.sort(key=lambda item: (str(item["at"]), item["seq"]))
    return {"phase": phase, "operations": operations, "rows": rows[-FEED_LIMIT:], "truncated": len(rows) > FEED_LIMIT}


_cache: dict[tuple[str, int, int], dict[str, Any]] = {}


def _read_cached(path: str, size: int, mtime: int) -> dict[str, Any]:
    key = (path, size, mtime)
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = read_record(Path(path))
    return _cache[key]


def record_lines(control_root: Path, phase: int, record: str, seq: int, context: int = 0) -> list[dict[str, Any]]:
    """The full record around one row. `record` names a file in the phase's directory, nothing else."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", record):
        raise EvidenceError("not a record name")
    path = control_root / "records" / f"phase-{phase}" / f"{record}.jsonl"
    if not path.is_file():
        raise EvidenceError("no such record; it may have been pruned")
    rows = read_record(path)["rows"]
    return [{"seq": row["seq"], "at": row["at"], "stream": row["stream"], "redacted": row["redacted"],
             "text": _printable(row["text"], 200_000)}
            for row in rows if seq - context <= row["seq"] <= seq + context]


def receipts_state(root: Path, runner: Callable[..., Any] = subprocess.run, remote: str = "origin") -> dict[str, Any]:
    """For the console: how many receipts exist, and whether the remote has them all. Runs on every poll."""
    local = EvidenceBranch(root, runner)
    tracking = EvidenceBranch(root, runner, f"refs/remotes/{remote}/{EVIDENCE_BRANCH}")
    try:
        head = local.head()
        count = int(local._git("rev-list", "--count", "--first-parent", head).stdout.strip()) if head else 0
        return {"count": count, "pushed": head is not None and head == tracking.head()}
    except (EvidenceError, ValueError):
        return {"count": 0, "pushed": False}
