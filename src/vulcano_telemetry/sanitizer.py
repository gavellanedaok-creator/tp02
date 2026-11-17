"""Removal of sensitive values before payment events are placed on a queue."""

import re
from collections.abc import Mapping
from math import isfinite

from .exceptions import InvalidQueueNameError, InvalidWaitTimeError

QUEUE_NAME_PATTERN = re.compile(r"^vulcano-[a-z0-9]+-q$")
MIN_WAIT_TIME_SECONDS = 1
MAX_WAIT_TIME_SECONDS = 20

SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "authorization",
    "account_number",
    "bank_account",
    "card",
    "cvv",
    "email",
    "iban",
    "password",
    "phone",
    "routing_number",
    "secret",
    "tax_id",
    "token",
)

REDACTED = "[REDACTED]"


def validate_queue_name(queue_name: str) -> str:
    """Validate and return a queue name matching `vulcano-<slug>-q`."""
    if not isinstance(queue_name, str) or not QUEUE_NAME_PATTERN.fullmatch(queue_name):
        raise InvalidQueueNameError(
            "queue_name must match the pattern vulcano-[a-z0-9]+-q"
        )
    return queue_name


def validate_wait_time(wait_time: int) -> int:
    """Validate an SQS long-polling wait time from 1 to 20 seconds."""
    if (
        isinstance(wait_time, bool)
        or not isinstance(wait_time, int)
        or not MIN_WAIT_TIME_SECONDS <= wait_time <= MAX_WAIT_TIME_SECONDS
    ):
        raise InvalidWaitTimeError("wait_time must be an integer from 1 to 20")
    return wait_time


def sanitize_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a JSON-compatible copy of a payload with sensitive values removed.

    Sensitive fields are redacted recursively. The input mapping and nested
    collections are never mutated.
    """
    return _sanitize_mapping(payload)


def _sanitize_mapping(payload: Mapping[object, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise TypeError("JSON object keys must be strings")
        normalized_key = key.casefold().replace("-", "_")
        if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
            sanitized[key] = REDACTED
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Non-finite numbers are not valid event data")
        return value
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")
