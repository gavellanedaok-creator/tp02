"""SQS queue lifecycle and event operations for LocalStack or moto_server."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from botocore.exceptions import BotoCoreError, ClientError

from .exceptions import SqsOperationError
from .sanitizer import validate_queue_name, validate_wait_time


class SqsClientProtocol(Protocol):
    """The subset of a boto3 SQS client used by this package."""

    def create_queue(self, **kwargs: object) -> Mapping[str, object]:
        """Create a queue and return its URL."""

    def send_message(self, **kwargs: object) -> Mapping[str, object]:
        """Send a message to a queue."""

    def receive_message(self, **kwargs: object) -> Mapping[str, object]:
        """Receive messages from a queue."""

    def delete_message(self, **kwargs: object) -> Mapping[str, object]:
        """Delete a previously received message."""


@dataclass(frozen=True)
class SqsEvent:
    """A received SQS event and its receipt handle."""

    message_id: str
    receipt_handle: str
    body: str


class SqsClient:
    """Validated SQS operations with a LocalStack-compatible endpoint default."""

    def __init__(
        self,
        queue_name: str,
        endpoint_url: str = "http://localhost:4566",
        region_name: str = "us-east-1",
        client: SqsClientProtocol | None = None,
    ) -> None:
        self.queue_name = validate_queue_name(queue_name)
        if not endpoint_url:
            raise ValueError("endpoint_url must not be empty")
        self._client = (
            client
            if client is not None
            else self._create_client(endpoint_url, region_name)
        )
        self._queue_url: str | None = None

    @staticmethod
    def _create_client(
        endpoint_url: str, region_name: str
    ) -> SqsClientProtocol:
        import boto3

        return boto3.client(
            "sqs",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    @property
    def queue_url(self) -> str:
        """Return the queue URL after `create_queue` has succeeded."""
        if self._queue_url is None:
            raise SqsOperationError("Queue has not been created in this process")
        return self._queue_url

    def create_queue(self) -> str:
        """Create the configured queue and remember its URL."""
        try:
            response = self._client.create_queue(QueueName=self.queue_name)
        except (BotoCoreError, ClientError) as error:
            raise SqsOperationError(
                f"Unable to create SQS queue {self.queue_name}"
            ) from error
        queue_url = response.get("QueueUrl")
        if not isinstance(queue_url, str) or not queue_url:
            raise SqsOperationError("SQS did not return a queue URL")
        self._queue_url = queue_url
        return queue_url

    def send_event(self, event_id: str, payload: Mapping[str, object]) -> str:
        """Serialize and send one event; return its SQS message ID."""
        message_body = json.dumps(
            {"event_id": event_id, "payload": payload},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        try:
            response = self._client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body,
                MessageAttributes={
                    "event_id": {
                        "DataType": "String",
                        "StringValue": event_id,
                    }
                },
            )
        except (BotoCoreError, ClientError) as error:
            raise SqsOperationError("Unable to send event to SQS") from error
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id:
            raise SqsOperationError("SQS did not return a message ID")
        return message_id

    def receive_events(
        self, max_messages: int = 10, wait_time: int = 20
    ) -> list[SqsEvent]:
        """Receive events using SQS long-polling (1-20 seconds)."""
        validate_wait_time(wait_time)
        if (
            isinstance(max_messages, bool)
            or not isinstance(max_messages, int)
            or not 1 <= max_messages <= 10
        ):
            raise ValueError("max_messages must be an integer from 1 to 10")
        try:
            response = self._client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time,
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            )
        except (BotoCoreError, ClientError) as error:
            raise SqsOperationError("Unable to receive events from SQS") from error

        raw_messages = response.get("Messages", [])
        if not isinstance(raw_messages, list):
            raise SqsOperationError("SQS returned an invalid Messages value")
        messages: list[SqsEvent] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, Mapping):
                raise SqsOperationError("SQS returned an invalid message")
            message_id = raw_message.get("MessageId")
            receipt_handle = raw_message.get("ReceiptHandle")
            body = raw_message.get("Body")
            if not all(
                isinstance(value, str) and value
                for value in (message_id, receipt_handle, body)
            ):
                raise SqsOperationError("SQS message is missing required fields")
            messages.append(SqsEvent(message_id, receipt_handle, body))
        return messages

    def delete_event(self, receipt_handle: str) -> None:
        """Delete a successfully processed event from the queue."""
        if not receipt_handle:
            raise ValueError("receipt_handle must not be empty")
        try:
            self._client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
            )
        except (BotoCoreError, ClientError) as error:
            raise SqsOperationError("Unable to delete event from SQS") from error
