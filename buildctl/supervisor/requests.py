"""Caller request identity: the contract an orchestrator uses to know what its command did.

Without it a caller that ran `buildctl phase 2` and lost its process could only guess,
from audit events that carry no caller identity, whether its command landed, is still
running or finished, and could not resend safely. With `--request-id <id>`:

1. `request_accepted` is written, under the state lock, before the command does anything.
   No record means the command never took effect, and resending the same id is safe.
2. Every audit event the command causes carries `request_id`.
3. `request_terminal` records the outcome once the command returns: `ok`, `escalated`
   (with the stop code) or `error`. Accepted with no terminal record means in flight, or
   interrupted with an unknown outcome: reconcile from `audit --request`, never replay.
4. The same id with the same arguments never runs twice; it reports the first run's record.
   The same id with different arguments is refused.

The contract is versioned (`CONTRACT_VERSION`) and reported by `inspect`, so a caller can
refuse a buildctl that does not provide it. Request records are retained with the state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from typing import Any

from .safety import redact
from .state import BuildState, StateStore, SupervisorLocked, now, request_scope

CONTRACT_VERSION = 1
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Commands that change state. Everything else is read-only or changes trust, which a
# request id cannot cover; passing --request-id to them is refused rather than ignored.
MUTATING = frozenset({"start", "run", "phase", "pause", "resume", "retry", "approve", "debate",
                      "reconcile", "reopen"})

# Arguments that do not change what the command does.
_NEUTRAL = frozenset({"repo", "dry_run", "request_id"})


class RequestConflict(RuntimeError):
    pass


def contract() -> dict[str, Any]:
    return {"version": CONTRACT_VERSION, "flag": "--request-id",
            "events": ["request_accepted", "request_terminal"],
            "outcomes": ["ok", "escalated", "error"]}


def is_mutating(args: argparse.Namespace) -> bool:
    if args.command == "evidence":
        return args.evidence_command == "prune"
    return args.command in MUTATING


def validate_request_id(value: str) -> str:
    if not REQUEST_ID.match(value):
        raise ValueError("--request-id must be 1-128 characters of letters, digits, '.', '_', ':' or '-', "
                         "starting with a letter or digit")
    return value


def arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {key: value for key, value in sorted(vars(args).items()) if key not in _NEUTRAL}


def digest(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def _public(arguments_: dict[str, Any]) -> dict[str, Any]:
    """Nonces authorize; the record shows a request used one, never what it was."""
    return {key: ("<redacted>" if key == "nonce" and value else value) for key, value in arguments_.items()}


def accept(store: StateStore, request_id: str, command: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Record the request before it acts. Returns None when new, the prior record when a duplicate.

    A duplicate is answered without the lock: the original command may still be running and
    holding it, which is exactly when a caller that lost its process resends. Records are
    never removed, so a lockless read that finds one is final; a new id is re-checked under
    the lock before it is written.
    """
    if not store.path.exists():
        raise RuntimeError("REQUEST_REQUIRES_STATE: run `buildctl bootstrap` before using --request-id")
    args_digest = digest(args)
    prior = _prior(store.load(), request_id, command, args_digest)
    if prior is not None:
        return prior
    with store.lock():
        state = store.load()
        prior = _prior(state, request_id, command, args_digest)
        if prior is not None:
            return prior
        state.requests[request_id] = {
            "request_id": request_id, "contract_version": CONTRACT_VERSION, "command": command,
            "arguments": _public(args), "args_digest": args_digest, "status": "accepted",
            "accepted_at": now(), "generation_before": state.generation,
            "outcome": None, "code": None, "detail": None, "finished_at": None, "generation_after": None}
        with request_scope(request_id):
            store.event(state, "request_accepted", command=command, args_digest=args_digest,
                        generation_before=state.generation, contract_version=CONTRACT_VERSION)
        return None


def _prior(state: BuildState, request_id: str, command: str, args_digest: str) -> dict[str, Any] | None:
    prior = state.requests.get(request_id)
    if prior is None:
        return None
    if prior["args_digest"] != args_digest or prior["command"] != command:
        raise RequestConflict(f"REQUEST_ID_CONFLICT: {request_id} was already used for a different "
                              f"{prior['command']} request")
    return dict(prior)


def finish(store: StateStore, request_id: str, outcome: str, code: str | None, detail: str | None,
           *, wait_seconds: float = 30.0) -> None:
    """Record the outcome, waiting a bounded time for the lock.

    Another process may take the lock between this command releasing it and settling the
    request. Waiting without bound could hang the caller for a whole phase run; giving up
    leaves the request `accepted`, which the contract already reads as "reconcile".
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with store.lock():
                _settle(store, request_id, outcome, code, detail)
            return
        except SupervisorLocked:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _settle(store: StateStore, request_id: str, outcome: str, code: str | None, detail: str | None) -> None:
    state = store.load()
    record = state.requests[request_id]
    record.update(status="terminal", outcome=outcome, code=code,
                  detail=redact(detail, limit=500) if detail else None,
                  finished_at=now(), generation_after=state.generation + 1)
    with request_scope(request_id):
        store.event(state, "request_terminal", outcome=outcome, code=code,
                    state_status=state.status, escalation_code=state.escalation_code)


def lookup(state: BuildState, request_id: str) -> dict[str, Any] | None:
    record = state.requests.get(request_id)
    return dict(record) if record is not None else None


def exit_code(record: dict[str, Any]) -> int:
    """For a duplicate: 0 finished ok, 2 finished otherwise, 3 accepted and not yet terminal."""
    if record["status"] != "terminal":
        return 3
    return 0 if record["outcome"] == "ok" else 2
