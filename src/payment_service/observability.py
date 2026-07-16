import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

PAYMENT_LOGGER_NAME = "payment_service"
STRUCTURED_FIELDS = (
    "operationId",
    "providerPaymentId",
    "attempt",
    "outcome",
    "result",
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_payment_logging() -> None:
    payment_logger = logging.getLogger(PAYMENT_LOGGER_NAME)
    payment_logger.disabled = False
    for name, candidate in logging.Logger.manager.loggerDict.items():
        if name.startswith(f"{PAYMENT_LOGGER_NAME}.") and isinstance(candidate, logging.Logger):
            candidate.disabled = False
    payment_logger.setLevel(logging.INFO)
    if not any(getattr(handler, "payment_json", False) for handler in payment_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        handler.payment_json = True  # type: ignore[attr-defined]
        payment_logger.addHandler(handler)
    payment_logger.propagate = False


def safely_log(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    try:
        logger.log(level, message, extra=fields, exc_info=exc_info)
    except Exception:
        pass


class PaymentMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.unfinished_operations = Gauge(
            "payment_unfinished_operations",
            "Current unfinished payment operations",
            ("status",),
            registry=self.registry,
        )
        self.provider_attempts = Counter(
            "payment_provider_attempts",
            "Provider dispatch attempts",
            registry=self.registry,
        )
        self.provider_retries = Counter(
            "payment_provider_retries",
            "Provider dispatch attempts after the first",
            registry=self.registry,
        )
        self.dispatch_outcomes = Counter(
            "payment_dispatch_outcomes",
            "Provider dispatch outcomes",
            ("outcome",),
            registry=self.registry,
        )
        self.receipt_outcomes = Counter(
            "payment_receipt_outcomes",
            "Receipt processing outcomes",
            ("outcome",),
            registry=self.registry,
        )
        for status in ("CREATED", "PROCESSING"):
            self.unfinished_operations.labels(status=status).set(0)
        for outcome in ("accepted", "unavailable", "transport_error", "error", "cancelled"):
            self.dispatch_outcomes.labels(outcome=outcome)
        for outcome in (
            "finalized",
            "duplicate",
            "ignored_opposite",
            "provider_id_conflict",
            "state_conflict",
            "unknown_operation",
        ):
            self.receipt_outcomes.labels(outcome=outcome)

    def render(self) -> bytes:
        return generate_latest(self.registry)


def safely_observe(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:
        pass
