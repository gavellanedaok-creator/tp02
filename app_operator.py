"""Vulcano Pay command line: server, webhook emitter, and SQS worker."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vulcano_telemetry.core_sqs import SqsClient
from vulcano_telemetry.exceptions import VulcanoError
from vulcano_telemetry.receiver import WebhookReceiver, create_app
from vulcano_telemetry.webhook_crypto import (
    DEFAULT_TOLERANCE_SECONDS,
    SQLiteReplayStore,
    WebhookAuthenticator,
    create_signature,
)


DEFAULT_QUEUE_NAME = "vulcano-payments-q"
DEFAULT_ENDPOINT_URL = "http://localhost:4566"


def build_receiver(
    queue_name: str,
    endpoint_url: str,
    secret: str,
    replay_database: str,
) -> tuple[WebhookReceiver, SqsClient]:
    """Create the queue and wire the webhook receiver to it."""
    sqs_client = SqsClient(queue_name=queue_name, endpoint_url=endpoint_url)
    sqs_client.create_queue()
    authenticator = WebhookAuthenticator(
        secret=secret,
        replay_store=SQLiteReplayStore(replay_database),
        tolerance_seconds=DEFAULT_TOLERANCE_SECONDS,
    )
    return WebhookReceiver(authenticator, sqs_client), sqs_client


def _add_sqs_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--queue-name",
        default=os.environ.get("VULCANO_SQS_QUEUE_NAME", DEFAULT_QUEUE_NAME),
        help="SQS queue matching vulcano-[a-z0-9]+-q",
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("VULCANO_SQS_ENDPOINT", DEFAULT_ENDPOINT_URL),
        help="Local SQS endpoint (moto_server defaults to http://localhost:4566)",
    )


def create_parser() -> argparse.ArgumentParser:
    """Define the supported command-line interface."""
    parser = argparse.ArgumentParser(description="Vulcano Pay event operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser(
        "server", help="Run the FastAPI webhook API"
    )
    _add_sqs_options(server_parser)
    server_parser.add_argument(
        "--secret", default=os.environ.get("VULCANO_WEBHOOK_SECRET")
    )
    server_parser.add_argument(
        "--replay-database",
        default=os.environ.get("VULCANO_REPLAY_DB", "vulcano_replay.sqlite3"),
    )
    server_parser.add_argument("--host", default="127.0.0.1")
    server_parser.add_argument("--port", type=int, default=8000)

    emit_parser = subparsers.add_parser(
        "emit-webhook", help="Send a signed sample webhook to the API"
    )
    emit_parser.add_argument(
        "--url", default="http://127.0.0.1:8000/webhook"
    )
    emit_parser.add_argument(
        "--secret", default=os.environ.get("VULCANO_WEBHOOK_SECRET")
    )
    emit_parser.add_argument("--event-id", default=None)
    emit_parser.add_argument(
        "--payload",
        default='{"type":"payment.settled","amount":2500,"currency":"ARS"}',
        help="Webhook JSON object",
    )

    worker_parser = subparsers.add_parser(
        "worker", help="Consume and delete SQS events"
    )
    _add_sqs_options(worker_parser)
    worker_parser.add_argument("--wait-time", type=int, default=20)
    worker_parser.add_argument("--max-messages", type=int, default=10)
    worker_parser.add_argument(
        "--once", action="store_true", help="Process one long-poll batch and exit"
    )
    return parser


def _run_server(args: argparse.Namespace) -> int:
    if not args.secret:
        raise ValueError("Set VULCANO_WEBHOOK_SECRET or pass --secret")
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")

    receiver, _ = build_receiver(
        args.queue_name,
        args.endpoint_url,
        args.secret,
        args.replay_database,
    )
    import uvicorn

    uvicorn.run(create_app(receiver), host=args.host, port=args.port)
    return 0


def _emit_webhook(args: argparse.Namespace) -> int:
    if not args.secret:
        raise ValueError("Set VULCANO_WEBHOOK_SECRET or pass --secret")
    payload = json.loads(args.payload)
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) for key in payload
    ):
        raise ValueError("--payload must be a JSON object")

    event_id = args.event_id or uuid.uuid4().hex
    if "event_id" in payload and payload["event_id"] != event_id:
        raise ValueError("Payload event_id must match --event-id")
    payload["event_id"] = event_id
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = create_signature(args.secret, timestamp, event_id, body)
    request = urllib.request.Request(
        args.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Vulcano-Timestamp": timestamp,
            "X-Vulcano-Event-Id": event_id,
            "X-Vulcano-Signature": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8")
            print(f"HTTP {response.status}: {response_body}")
            return 0 if response.status == 202 else 1
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        print(f"HTTP {error.code}: {response_body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"Unable to reach webhook API: {error.reason}", file=sys.stderr)
        return 1


def _run_worker(args: argparse.Namespace) -> int:
    sqs_client = SqsClient(
        queue_name=args.queue_name,
        endpoint_url=args.endpoint_url,
    )
    sqs_client.create_queue()
    while True:
        messages = sqs_client.receive_events(
            max_messages=args.max_messages,
            wait_time=args.wait_time,
        )
        for message in messages:
            print(message.body)
            sqs_client.delete_event(message.receipt_handle)
        if args.once:
            break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch the requested operation."""
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "server":
            return _run_server(args)
        if args.command == "emit-webhook":
            return _emit_webhook(args)
        if args.command == "worker":
            return _run_worker(args)
        parser.error("Unknown command")
    except (VulcanoError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
