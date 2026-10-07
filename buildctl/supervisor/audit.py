from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class AuditIntegrityError(RuntimeError):
    pass


class AuditChain:
    """Lightweight tamper-evident event chain; not a compliance ledger."""

    def __init__(self, control_root: Path):
        self.path = control_root.resolve() / "audit.jsonl"

    @staticmethod
    def _hash(entry: dict[str, Any]) -> str:
        material = {key: value for key, value in entry.items() if key != "event_hash"}
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def verify(self) -> tuple[int, str]:
        previous = "0" * 64
        sequence = 0
        if not self.path.exists():
            return sequence, previous
        for number, line in enumerate(self.path.read_text().splitlines(), 1):
            try:
                entry = json.loads(line)
                if entry["sequence"] != number or entry["previous_event_hash"] != previous:
                    raise ValueError("chain link mismatch")
                if entry["event_hash"] != self._hash(entry):
                    raise ValueError("event hash mismatch")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise AuditIntegrityError(f"audit chain corrupt at event {number}") from exc
            sequence, previous = number, entry["event_hash"]
        return sequence, previous

    def append(self, timestamp: str, event_type: str, payload: dict[str, Any]) -> tuple[int, str]:
        sequence, previous = self.verify()
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        entry = {"sequence": sequence + 1, "timestamp": timestamp, "event_type": event_type,
                 "payload_hash": payload_hash, "payload": payload, "previous_event_hash": previous}
        entry["event_hash"] = self._hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n"); stream.flush(); os.fsync(stream.fileno())
        return entry["sequence"], entry["event_hash"]
