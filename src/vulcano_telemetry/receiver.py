"""FastAPI webhook endpoint and framework-independent event receiver."""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request

from .core_sqs import SqsClient
from .exceptions import (
    InvalidPayloadError,
    InvalidSignatureError,
    InvalidWebhookHeaderError,
    PayloadTooLargeError,
    ReplayAttackError,
    SqsError,
    StaleTimestampError,
    WebhookError,
)
from .sanitizer import sanitize_payload
from .webhook_crypto import WebhookAuthenticator

MAX_BODY_BYTES = 1_048_576


@dataclass(frozen=True)
class WebhookResponse:
    """Successful webhook acceptance details."""

    status_code: int
    event_id: str
    message_id: str


class WebhookReceiver:
    """Authenticate, sanitize, and forward payment events to SQS."""

    def __init__(
        self,
        authenticator: WebhookAuthenticator,
        sqs_client: SqsClient,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("Maximum body size must be positive")
        self._authenticator = authenticator
        self._sqs_client = sqs_client
        self.max_body_bytes = max_body_bytes

    def receive(
        self, headers: Mapping[str, str], body: bytes
    ) -> WebhookResponse:
        """Validate one signed request and enqueue its sanitized payload."""
        if len(body) > self.max_body_bytes:
            raise PayloadTooLargeError("Webhook body exceeds the configured limit")

        normalized_headers = self._normalize_headers(headers)
        timestamp = self._required_header(
            normalized_headers, "x-vulcano-timestamp"
        )
        event_id = self._required_header(normalized_headers, "x-vulcano-event-id")
        signature = self._required_header(
            normalized_headers, "x-vulcano-signature"
        )
        signed_timestamp = self._authenticator.verify_signature(
            timestamp, event_id, signature, body
        )

        payload = self._parse_payload(body)
        payload_event_id = payload.get("event_id")
        if payload_event_id is not None and payload_event_id != event_id:
            raise InvalidPayloadError(
                "Payload event_id does not match the signed event identifier"
            )
        try:
            sanitized = sanitize_payload(payload)
        except (TypeError, ValueError, RecursionError) as error:
            raise InvalidPayloadError(
                "Webhook contains unsupported JSON values"
            ) from error

        token = self._authenticator.claim_event(event_id, signed_timestamp)
        try:
            message_id = self._sqs_client.send_event(event_id, sanitized)
        except SqsError:
            self._authenticator.release_event(event_id, token)
            raise
        return WebhookResponse(202, event_id, message_id)

    @staticmethod
    def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in headers.items():
            lowered_key = key.casefold()
            if lowered_key in normalized:
                raise InvalidWebhookHeaderError(
                    "Webhook contains duplicate case-insensitive headers"
                )
            if not isinstance(value, str):
                raise InvalidWebhookHeaderError("Webhook header values must be text")
            normalized[lowered_key] = value.strip()
        return normalized

    @staticmethod
    def _required_header(headers: Mapping[str, str], name: str) -> str:
        value = headers.get(name)
        if value is None or not value:
            raise InvalidWebhookHeaderError(f"Missing required header: {name}")
        return value

    @staticmethod
    def _parse_payload(body: bytes) -> dict[str, object]:
        try:
            decoded = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise InvalidPayloadError(
                "Webhook body must be valid UTF-8 JSON"
            ) from error
        if not isinstance(decoded, dict) or any(
            not isinstance(key, str) for key in decoded
        ):
            raise InvalidPayloadError("Webhook JSON body must be an object")
        return decoded


def create_app(receiver: WebhookReceiver) -> FastAPI:
    """Build the FastAPI application around a configured receiver."""
    app = FastAPI(title="Vulcano Pay Webhook Receiver", version="1.0.0")

    @app.post("/webhook", status_code=202)
    async def receive_webhook(request: Request) -> dict[str, str]:
        body_parts: list[bytes] = []
        body_size = 0
        async for part in request.stream():
            body_size += len(part)
            if body_size > receiver.max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Webhook body exceeds the configured limit",
                )
            body_parts.append(part)
        body = b"".join(body_parts)

        try:
            result = receiver.receive(request.headers, body)
        except ReplayAttackError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InvalidSignatureError, StaleTimestampError) as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except PayloadTooLargeError as error:
            raise HTTPException(status_code=413, detail=str(error)) from error
        except WebhookError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except SqsError as error:
            raise HTTPException(status_code=503, detail="SQS is unavailable") from error

        return {
            "status": "accepted",
            "event_id": result.event_id,
            "message_id": result.message_id,
        }

    return app
