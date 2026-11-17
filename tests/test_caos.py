"""Pytest coverage for webhook integrity, replay prevention, and sanitization."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vulcano_telemetry.exceptions import (
    InvalidQueueNameError,
    InvalidSignatureError,
    InvalidWaitTimeError,
    ReplayAttackError,
    StaleTimestampError,
)
from vulcano_telemetry.core_sqs import SqsClient
from vulcano_telemetry.receiver import WebhookReceiver
from vulcano_telemetry.sanitizer import (
    sanitize_payload,
    validate_queue_name,
    validate_wait_time,
)
from vulcano_telemetry.webhook_crypto import (
    SQLiteReplayStore,
    WebhookAuthenticator,
    create_signature,
    verify_signature,
)

NOW = 1_800_000_000.0
SECRET = "local-test-secret"


class FakeSqsClient:
    """Small deterministic SQS double for receiver tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def send_event(self, event_id: str, payload: Mapping[str, object]) -> str:
        self.events.append((event_id, payload))
        return f"message-{len(self.events)}"


class FakeAwsClient:
    """Capture boto3-style calls without requiring a running SQS service."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def create_queue(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create_queue", kwargs))
        return {"QueueUrl": "http://localhost:4566/000000000000/vulcano-payments-q"}

    def send_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("send_message", kwargs))
        return {"MessageId": "message-1"}

    def receive_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("receive_message", kwargs))
        return {
            "Messages": [
                {
                    "MessageId": "message-1",
                    "ReceiptHandle": "receipt-1",
                    "Body": '{"event_id":"evt-1"}',
                }
            ]
        }

    def delete_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete_message", kwargs))
        return {}


def signed_request(
    event_id: str = "evt-001",
    body: bytes | None = None,
    timestamp: int = int(NOW),
) -> tuple[dict[str, str], bytes]:
    request_body = body or json.dumps(
        {
            "event_id": event_id,
            "type": "payment.settled",
            "customer": {"email": "buyer@example.com"},
            "payment": {"card_number": "4111111111111111"},
            "amount": 2500,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = create_signature(SECRET, timestamp, event_id, request_body)
    return (
        {
            "X-Vulcano-Timestamp": str(timestamp),
            "X-Vulcano-Event-Id": event_id,
            "X-Vulcano-Signature": signature,
        },
        request_body,
    )


def make_receiver(tmp_path: Path, sqs: FakeSqsClient) -> WebhookReceiver:
    authenticator = WebhookAuthenticator(
        SECRET,
        SQLiteReplayStore(tmp_path / "replay.sqlite3"),
        clock=lambda: NOW,
    )
    return WebhookReceiver(authenticator, sqs)  # type: ignore[arg-type]


def test_valid_signature_and_receiver_enqueue(tmp_path: Path) -> None:
    headers, body = signed_request()

    assert verify_signature(
        SECRET,
        headers["X-Vulcano-Timestamp"],
        headers["X-Vulcano-Event-Id"],
        body,
        headers["X-Vulcano-Signature"],
        now=NOW,
    ) == int(NOW)

    sqs = FakeSqsClient()
    result = make_receiver(tmp_path, sqs).receive(headers, body)

    assert result.status_code == 202
    assert result.event_id == "evt-001"
    assert len(sqs.events) == 1


def test_invalid_signature_is_rejected() -> None:
    headers, body = signed_request()
    headers["X-Vulcano-Signature"] = "sha256=" + "0" * 64

    with pytest.raises(InvalidSignatureError):
        verify_signature(
            SECRET,
            headers["X-Vulcano-Timestamp"],
            headers["X-Vulcano-Event-Id"],
            body,
            headers["X-Vulcano-Signature"],
            now=NOW,
        )


def test_expired_timestamp_is_rejected() -> None:
    expired_timestamp = int(NOW) - 301
    headers, body = signed_request(timestamp=expired_timestamp)

    with pytest.raises(StaleTimestampError):
        verify_signature(
            SECRET,
            headers["X-Vulcano-Timestamp"],
            headers["X-Vulcano-Event-Id"],
            body,
            headers["X-Vulcano-Signature"],
            now=NOW,
        )


def test_duplicate_event_is_rejected(tmp_path: Path) -> None:
    headers, body = signed_request()
    sqs = FakeSqsClient()
    receiver = make_receiver(tmp_path, sqs)
    receiver.receive(headers, body)

    with pytest.raises(ReplayAttackError):
        receiver.receive(headers, body)

    assert len(sqs.events) == 1


def test_future_timestamp_does_not_allow_replay_at_window_boundary(
    tmp_path: Path,
) -> None:
    current_time = [NOW]
    authenticator = WebhookAuthenticator(
        SECRET,
        SQLiteReplayStore(tmp_path / "future-replay.sqlite3"),
        clock=lambda: current_time[0],
    )
    sqs = FakeSqsClient()
    receiver = WebhookReceiver(authenticator, sqs)  # type: ignore[arg-type]
    headers, body = signed_request(timestamp=int(NOW) + 300)
    receiver.receive(headers, body)

    current_time[0] = NOW + 300
    with pytest.raises(ReplayAttackError):
        receiver.receive(headers, body)

    assert len(sqs.events) == 1


def test_sanitization_and_queue_parameter_validation() -> None:
    result = sanitize_payload(
        {
            "event_id": "evt-1",
            "customer": {"email": "person@example.com"},
            "card_number": "4111111111111111",
        }
    )
    assert result == {
        "event_id": "evt-1",
        "customer": {"email": "[REDACTED]"},
        "card_number": "[REDACTED]",
    }

    assert validate_queue_name("vulcano-payments-q") == "vulcano-payments-q"
    assert validate_wait_time(20) == 20
    with pytest.raises(InvalidQueueNameError):
        validate_queue_name("payments")
    with pytest.raises(InvalidWaitTimeError):
        validate_wait_time(0)
    with pytest.raises(InvalidWaitTimeError):
        validate_wait_time(21)


def test_sqlite_replay_claim_is_atomic(tmp_path: Path) -> None:
    store = SQLiteReplayStore(tmp_path / "concurrent.sqlite3")
    barrier = threading.Barrier(8)

    def try_claim(index: int) -> bool:
        barrier.wait()
        return store.claim("evt-concurrent", f"token-{index}", 1000.0, 1.0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(try_claim, range(8)))

    assert sum(claims) == 1


def test_sqs_client_creates_sends_receives_and_deletes() -> None:
    aws_client = FakeAwsClient()
    sqs = SqsClient(
        "vulcano-payments-q",
        client=aws_client,  # type: ignore[arg-type]
    )

    queue_url = sqs.create_queue()
    message_id = sqs.send_event("evt-1", {"type": "payment.settled"})
    messages = sqs.receive_events(max_messages=3, wait_time=7)
    sqs.delete_event(messages[0].receipt_handle)

    assert queue_url.endswith("/vulcano-payments-q")
    assert message_id == "message-1"
    assert messages[0].message_id == "message-1"
    assert [name for name, _ in aws_client.calls] == [
        "create_queue",
        "send_message",
        "receive_message",
        "delete_message",
    ]
    receive_options = aws_client.calls[2][1]
    assert receive_options["WaitTimeSeconds"] == 7
    assert receive_options["MaxNumberOfMessages"] == 3
