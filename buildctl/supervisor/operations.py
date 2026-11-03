from __future__ import annotations

from enum import Enum


class PaidOperation(str, Enum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    REMEDIATION = "remediation"
    DEBATE = "debate"
    DEBATE_TURN = "debate_turn"


PAID_OPERATION_IDS = {item.value for item in PaidOperation}
