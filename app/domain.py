"""Pure policies. No database, process-local state or transport dependencies."""
import hashlib
import json
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    TECHNICAL = "technical"
    MODERATION = "moderation"
    PAYLOAD = "payload"


class DomainError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


def next_leg(legs: list[dict], after: int, refs: int, error: str | None) -> int | None:
    """Moderation skip applies to candidates after the immediately previous actual attempt."""
    if error == ErrorKind.PAYLOAD:
        return None
    for index in range(after + 1, len(legs)):
        leg = legs[index]
        if refs > leg["max_ref_images"]:
            continue
        if error == ErrorKind.MODERATION and leg["skip_on_moderation"]:
            continue
        return index
    return None


def fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_current(task, attempt) -> bool:
    return (task["status"] == "active" and task["current_index"] == attempt["leg_index"]
            and attempt["status"] in ("queued", "sending", "waiting"))
