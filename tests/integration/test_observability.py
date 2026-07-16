import asyncio
import logging
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from payment_service.config import Settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app
from payment_service.observability import PaymentMetrics

pytestmark = pytest.mark.integration


async def test_metrics_expose_unfinished_operations_without_identifier_labels(
    postgres_client: httpx.AsyncClient,
) -> None:
    operation_id = "operation-metrics-backlog"
    await create_submitted_operation(postgres_client, operation_id)

    response = await postgres_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE payment_unfinished_operations gauge" in response.text
    assert 'payment_unfinished_operations{status="CREATED"} 0.0' in response.text
    assert 'payment_unfinished_operations{status="PROCESSING"} 1.0' in response.text
    assert operation_id not in response.text


async def test_metrics_count_successful_provider_dispatch(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()

    @provider_app.post("/payments")
    async def accept_payment() -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-metrics-success",
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=Settings(database_url=postgres_url, provider_url="http://provider"),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await create_submitted_operation(client, "operation-metrics-success")
            await wait_for_provider_payment_id(client, "operation-metrics-success")

            response = await client.get("/metrics")

    assert "payment_provider_attempts_total 1.0" in response.text
    assert "payment_provider_retries_total 0.0" in response.text
    assert 'payment_dispatch_outcomes_total{outcome="accepted"} 1.0' in response.text


async def test_metrics_count_provider_retry_and_outcomes(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()
    request_count = 0

    @provider_app.post("/payments")
    async def retry_payment() -> JSONResponse:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return JSONResponse(status_code=503, content={"status": "UNAVAILABLE"})
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-metrics-retry",
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=Settings(
            database_url=postgres_url,
            provider_url="http://provider",
            provider_timeout_seconds=0.02,
            dispatch_retry_base_delay_seconds=0.01,
            dispatch_retry_max_delay_seconds=0.01,
            dispatch_retry_jitter_ratio=0,
            dispatch_claim_timeout_seconds=0.05,
        ),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await create_submitted_operation(client, "operation-metrics-retry")
            await wait_for_provider_payment_id(client, "operation-metrics-retry")

            response = await client.get("/metrics")

    assert "payment_provider_attempts_total 2.0" in response.text
    assert "payment_provider_retries_total 1.0" in response.text
    assert 'payment_dispatch_outcomes_total{outcome="unavailable"} 1.0' in response.text
    assert 'payment_dispatch_outcomes_total{outcome="accepted"} 1.0' in response.text


async def test_metrics_count_ignored_and_conflicting_receipts(
    postgres_client: httpx.AsyncClient,
) -> None:
    operation_id = "operation-metrics-receipts"
    provider_payment_id = "provider-payment-metrics-receipts"
    await create_submitted_operation(postgres_client, operation_id)
    completed_receipt = {
        "providerPaymentId": provider_payment_id,
        "operationId": operation_id,
        "result": "COMPLETED",
        "message": "Payment completed",
        "occurredAt": "2026-07-15T12:00:00Z",
    }
    completed_response = await postgres_client.post("/receipts", json=completed_receipt)
    ignored_response = await postgres_client.post(
        "/receipts",
        json={**completed_receipt, "result": "REJECTED", "message": "Late rejection"},
    )
    conflict_response = await postgres_client.post(
        "/receipts",
        json={**completed_receipt, "providerPaymentId": "provider-payment-conflicting"},
    )

    response = await postgres_client.get("/metrics")

    assert completed_response.status_code == 204
    assert ignored_response.status_code == 204
    assert conflict_response.status_code == 409
    assert 'payment_receipt_outcomes_total{outcome="finalized"} 1.0' in response.text
    assert 'payment_receipt_outcomes_total{outcome="ignored_opposite"} 1.0' in response.text
    assert 'payment_receipt_outcomes_total{outcome="provider_id_conflict"} 1.0' in response.text
    assert operation_id not in response.text
    assert provider_payment_id not in response.text


async def test_logs_correlate_retry_and_callback_with_structured_fields(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = "operation-structured-logs"
    provider_payment_id = "provider-payment-structured-logs"
    provider_app = FastAPI()
    request_count = 0
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    @provider_app.post("/payments")
    async def retry_payment() -> JSONResponse:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return JSONResponse(status_code=503, content={"status": "UNAVAILABLE"})
        return JSONResponse(
            status_code=202,
            content={"providerPaymentId": provider_payment_id, "status": "ACCEPTED"},
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=retry_settings(postgres_url),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    payment_logger = logging.getLogger("payment_service")
    capture_handler = CaptureHandler()
    payment_logger.addHandler(capture_handler)
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await create_submitted_operation(client, operation_id)
                await wait_for_provider_payment_id(client, operation_id)
                await wait_for_log_message(records, "provider dispatch accepted")
                conflict_response = await client.post(
                    "/receipts",
                    json={
                        "providerPaymentId": "provider-payment-conflicting-log",
                        "operationId": operation_id,
                        "result": "COMPLETED",
                        "message": "Conflicting receipt",
                        "occurredAt": "2026-07-15T12:00:00Z",
                    },
                )
    finally:
        payment_logger.removeHandler(capture_handler)

    dispatch_starts = [
        record for record in records if record.getMessage() == "provider dispatch started"
    ]
    accepted = next(
        record for record in records if record.getMessage() == "provider dispatch accepted"
    )
    receipt_conflict = next(
        record for record in records if getattr(record, "outcome", None) == "provider_id_conflict"
    )
    assert conflict_response.status_code == 409
    assert [record.attempt for record in dispatch_starts] == [1, 2]
    assert {record.operationId for record in dispatch_starts} == {operation_id}
    assert accepted.operationId == operation_id
    assert accepted.providerPaymentId == provider_payment_id
    assert accepted.attempt == 2
    assert receipt_conflict.operationId == operation_id
    assert receipt_conflict.providerPaymentId == "provider-payment-conflicting-log"


async def test_observability_backend_failures_do_not_block_payment_processing(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = "operation-broken-observability"
    provider_payment_id = "provider-payment-broken-observability"
    provider_app = FastAPI()

    class BrokenMetric:
        def inc(self) -> None:
            raise RuntimeError("metrics backend unavailable")

        def labels(self, **labels: str) -> BrokenMetric:
            raise RuntimeError("metrics backend unavailable")

    class BrokenMetrics:
        provider_attempts = BrokenMetric()
        provider_retries = BrokenMetric()
        dispatch_outcomes = BrokenMetric()
        receipt_outcomes = BrokenMetric()

    class BrokenHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("log backend unavailable")

    @provider_app.post("/payments")
    async def accept_payment() -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={"providerPaymentId": provider_payment_id, "status": "ACCEPTED"},
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=Settings(database_url=postgres_url, provider_url="http://provider"),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
        payment_metrics=cast(PaymentMetrics, BrokenMetrics()),
    )
    payment_logger = logging.getLogger("payment_service")
    broken_handler = BrokenHandler()
    payment_logger.addHandler(broken_handler)
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await create_submitted_operation(client, operation_id)
                await wait_for_provider_payment_id(client, operation_id)
                receipt_response = await client.post(
                    "/receipts",
                    json={
                        "providerPaymentId": provider_payment_id,
                        "operationId": operation_id,
                        "result": "COMPLETED",
                        "message": "Payment completed",
                        "occurredAt": "2026-07-15T12:00:00Z",
                    },
                )
                operation_response = await client.get(f"/operations/{operation_id}")
    finally:
        payment_logger.removeHandler(broken_handler)

    assert receipt_response.status_code == 204
    assert operation_response.json()["status"] == "COMPLETED"
    assert operation_response.json()["providerPaymentId"] == provider_payment_id


async def create_submitted_operation(client: httpx.AsyncClient, operation_id: str) -> None:
    create_response = await client.post(
        "/operations",
        json={
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
            "description": "Observability test",
        },
    )
    submit_response = await client.post(f"/operations/{operation_id}/submit")
    assert create_response.status_code == 201
    assert submit_response.status_code == 202


async def wait_for_provider_payment_id(client: httpx.AsyncClient, operation_id: str) -> None:
    for _ in range(200):
        response = await client.get(f"/operations/{operation_id}")
        if response.json()["providerPaymentId"] is not None:
            return
        await asyncio.sleep(0.01)
    pytest.fail("provider payment ID was not stored")


async def wait_for_log_message(records: list[logging.LogRecord], message: str) -> None:
    for _ in range(100):
        if any(record.getMessage() == message for record in records):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"log message was not emitted: {message}")


def retry_settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        provider_url="http://provider",
        provider_timeout_seconds=0.02,
        dispatch_retry_base_delay_seconds=0.01,
        dispatch_retry_max_delay_seconds=0.01,
        dispatch_retry_jitter_ratio=0,
        dispatch_claim_timeout_seconds=0.05,
    )
