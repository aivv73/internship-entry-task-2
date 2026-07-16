import json
import logging

from payment_service.observability import JsonLogFormatter, safely_log, safely_observe


def test_json_log_formatter_emits_payment_correlation_fields() -> None:
    record = logging.LogRecord(
        name="payment_service.dispatcher",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider dispatch accepted",
        args=(),
        exc_info=None,
    )
    record.operationId = "operation-structured-log"
    record.providerPaymentId = "provider-payment-structured-log"
    record.attempt = 2
    record.outcome = "accepted"

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "provider dispatch accepted"
    assert payload["operationId"] == "operation-structured-log"
    assert payload["providerPaymentId"] == "provider-payment-structured-log"
    assert payload["attempt"] == 2
    assert payload["outcome"] == "accepted"


def test_observability_helpers_swallow_backend_failures() -> None:
    class BrokenHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("log backend unavailable")

    logger = logging.getLogger("test-broken-observability")
    handler = BrokenHandler()
    logger.addHandler(handler)
    logger.propagate = False
    try:
        safely_log(logger, logging.INFO, "payment event", operationId="operation-safe")
        safely_observe(lambda: (_ for _ in ()).throw(RuntimeError("metrics unavailable")))
    finally:
        logger.removeHandler(handler)
