"""HMAC-SHA256 validation and time-bounded replay protection."""

import hashlib
import hmac
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .exceptions import (
    InvalidSignatureError,
    InvalidWebhookHeaderError,
    ReplayAttackError,
    StaleTimestampError,
)

DEFAULT_TOLERANCE_SECONDS = 300
_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-fA-F]{64})$")


class ReplayStore(Protocol):
    """Atomic store for one-time webhook event identifiers."""

    def claim(
        self, event_id: str, token: str, expires_at: float, now: float
    ) -> bool:
        """Claim an event identifier unless an unexpired claim already exists."""

    def release(self, event_id: str, token: str) -> None:
        """Release only the claim identified by the given token."""


class InMemoryReplayStore:
    """Thread-safe replay store for a single-process development deployment."""

    def __init__(self) -> None:
        self._claims: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def claim(
        self, event_id: str, token: str, expires_at: float, now: float
    ) -> bool:
        with self._lock:
            expired = [
                claimed_id
                for claimed_id, (_, expiry) in self._claims.items()
                if expiry <= now
            ]
            for claimed_id in expired:
                del self._claims[claimed_id]
            if event_id in self._claims:
                return False
            self._claims[event_id] = (token, expires_at)
            return True

    def release(self, event_id: str, token: str) -> None:
        with self._lock:
            claim = self._claims.get(event_id)
            if claim is not None and claim[0] == token:
                del self._claims[event_id]


class SQLiteReplayStore:
    """Persistent replay store shared by app processes using the same database."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)
        path = Path(self._database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_replay_claims (
                    event_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def claim(
        self, event_id: str, token: str, expires_at: float, now: float
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM webhook_replay_claims WHERE expires_at <= ?",
                (now,),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO webhook_replay_claims (event_id, token, expires_at)
                    VALUES (?, ?, ?)
                    """,
                    (event_id, token, expires_at),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
            return True
        finally:
            connection.close()

    def release(self, event_id: str, token: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM webhook_replay_claims WHERE event_id = ? AND token = ?",
                (event_id, token),
            )
            connection.commit()
        finally:
            connection.close()


class WebhookAuthenticator:
    """Validate signed raw request bodies and manage replay claims."""

    def __init__(
        self,
        secret: str | bytes,
        replay_store: ReplayStore,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not secret_bytes:
            raise ValueError("Webhook secret must not be empty")
        if (
            isinstance(tolerance_seconds, bool)
            or not isinstance(tolerance_seconds, int)
            or tolerance_seconds <= 0
        ):
            raise ValueError("Timestamp tolerance must be positive")
        self._secret = secret_bytes
        self._replay_store = replay_store
        self._tolerance_seconds = tolerance_seconds
        self._clock = clock

    def verify_signature(
        self,
        timestamp_header: str,
        event_id: str,
        signature_header: str,
        body: bytes,
    ) -> int:
        """Validate headers, freshness, and HMAC; return the signed timestamp."""
        if (
            not timestamp_header.isascii()
            or not timestamp_header.isdecimal()
            or len(timestamp_header) > 12
        ):
            raise InvalidWebhookHeaderError("Invalid webhook timestamp")
        if (
            not event_id
            or len(event_id) > 128
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in event_id
            )
        ):
            raise InvalidWebhookHeaderError("Invalid webhook event identifier")

        match = _SIGNATURE_PATTERN.fullmatch(signature_header)
        if match is None:
            raise InvalidSignatureError("Malformed webhook signature")

        timestamp = int(timestamp_header)
        now = self._clock()
        if abs(int(now) - timestamp) > self._tolerance_seconds:
            raise StaleTimestampError("Webhook timestamp is outside the allowed window")

        expected = create_signature(
            self._secret, timestamp_header, event_id, body
        ).partition("=")[2]
        supplied = match.group(1).lower()
        if not hmac.compare_digest(expected, supplied):
            raise InvalidSignatureError("Webhook signature does not match")
        return timestamp

    def claim_event(
        self, event_id: str, signed_timestamp: int | None = None
    ) -> str:
        """Claim a verified event ID and return its lease token."""
        now = self._clock()
        token = uuid.uuid4().hex
        signed_time = now if signed_timestamp is None else float(signed_timestamp)
        latest_valid_time = max(now, signed_time)
        expires_at = latest_valid_time + self._tolerance_seconds
        if not self._replay_store.claim(event_id, token, expires_at, now):
            raise ReplayAttackError("Webhook event has already been received")
        return token

    def release_event(self, event_id: str, token: str) -> None:
        """Release a claim after a failed queue operation."""
        self._replay_store.release(event_id, token)


def create_signature(
    secret: str | bytes,
    timestamp: str | int,
    event_id: str,
    body: bytes,
) -> str:
    """Create the `sha256=` signature for `timestamp.event_id.body`."""
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    timestamp_text = str(timestamp)
    signed_content = (
        timestamp_text.encode("ascii")
        + b"."
        + event_id.encode("ascii")
        + b"."
        + body
    )
    digest = hmac.new(secret_bytes, signed_content, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str | bytes,
    timestamp: str,
    event_id: str,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> int:
    """Verify an HMAC signature and timestamp without claiming the event ID.

    The event ID must be signed alongside the timestamp and exact raw body.
    Replay protection is completed by `WebhookAuthenticator.claim_event`.
    """
    authenticator = WebhookAuthenticator(
        secret=secret,
        replay_store=InMemoryReplayStore(),
        tolerance_seconds=tolerance_seconds,
        clock=(lambda: time.time()) if now is None else (lambda: now),
    )
    return authenticator.verify_signature(timestamp, event_id, signature, body)
