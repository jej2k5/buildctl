from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .operations import PaidOperation


class LimitExceeded(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, control_root: Path, daily_limit: float):
        self.path = control_root / "costs.jsonl"
        self.daily_limit = daily_limit

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        for number, line in enumerate(self.path.read_text().splitlines(), 1):
            try:
                item = json.loads(line)
                if set(item) != {"at", "provider", "phase", "operation_id", "cost_usd"}:
                    raise ValueError("unexpected fields")
                entries.append(item)
            except (ValueError, TypeError) as exc:
                raise LimitExceeded(f"budget ledger is corrupt at line {number}; refusing paid work") from exc
        return entries

    def spent_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(float(item["cost_usd"]) for item in self._entries() if item["at"].startswith(today))

    def reserve(self, provider: str, phase: int, operation_id: str, cost_usd: float) -> None:
        if any(item["operation_id"] == operation_id for item in self._entries()):
            return
        if self.spent_today() + cost_usd > self.daily_limit:
            raise LimitExceeded(f"operation would exceed daily cost limit of ${self.daily_limit:.2f}")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        item = {"at": datetime.now(timezone.utc).isoformat(), "provider": provider,
                "phase": phase, "operation_id": operation_id, "cost_usd": max(0.0, cost_usd)}
        with self.path.open("a") as stream:
            stream.write(json.dumps(item, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def ensure_available(self) -> None:
        if self.spent_today() >= self.daily_limit:
            raise LimitExceeded(f"daily cost limit of ${self.daily_limit:.2f} reached")


def configured_cost(config: dict[str, Any], operation: PaidOperation) -> float:
    costs = config.get("operation_cost_usd", {})
    if operation.value not in costs:
        raise LimitExceeded(f"missing cost configuration for paid operation: {operation.value}")
    value = costs[operation.value]
    if not isinstance(value, (int, float)) or value < 0:
        raise LimitExceeded(f"invalid cost configuration for paid operation: {operation.value}")
    return float(value)
