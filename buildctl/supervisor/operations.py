from __future__ import annotations

from enum import Enum


class PaidOperation(str, Enum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    REMEDIATION = "remediation"
    DEBATE = "debate"
    DEBATE_TURN = "debate_turn"
    DOCUMENTATION = "documentation"
    DOCUMENTATION_REMEDIATION = "documentation_remediation"


PAID_OPERATION_IDS = {item.value for item in PaidOperation}

# Priced only by a build that turns them on, so builds written before they existed still load.
DEBATE_OPERATION_IDS = {PaidOperation.DEBATE_TURN.value}
DOCUMENTATION_OPERATION_IDS = {PaidOperation.DOCUMENTATION.value, PaidOperation.DOCUMENTATION_REMEDIATION.value}
