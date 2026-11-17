"""Typed exceptions for SQS operations and webhook validation."""


class VulcanoError(Exception):
    """Base class for expected Vulcano Pay errors."""


VulcanoTelemetryError = VulcanoError


class SqsError(VulcanoError):
    """Base class for SQS configuration and operation errors."""


class InvalidQueueNameError(SqsError, ValueError):
    """The queue name does not follow the Vulcano queue naming convention."""


class InvalidWaitTimeError(SqsError, ValueError):
    """The SQS long-polling wait time is outside the supported range."""


class SqsOperationError(SqsError):
    """An AWS SDK operation failed."""


class WebhookError(VulcanoError):
    """Base class for malformed or invalid webhook requests."""


WebhookValidationError = WebhookError


class InvalidWebhookHeaderError(WebhookError):
    """A required webhook header is missing or malformed."""


class InvalidSignatureError(WebhookError):
    """The webhook's HMAC signature is invalid."""


class StaleTimestampError(WebhookError):
    """The signed timestamp is outside the accepted time window."""


class InvalidPayloadError(WebhookError):
    """The request body is not a valid JSON object."""


class PayloadTooLargeError(WebhookError):
    """The request body exceeds the configured maximum size."""


class ReplayAttackError(WebhookError):
    """The event identifier has already been accepted."""
