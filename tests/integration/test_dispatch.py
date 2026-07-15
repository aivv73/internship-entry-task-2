import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from payment_service.config import Settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app

pytestmark = pytest.mark.integration


async def test_submit_atomically_records_one_processing_intent(
    postgres_client: httpx.AsyncClient,
    postgres_url: str,
) -> None:
    await postgres_client.post(
        "/operations",
        json={
            "operationId": "operation-submit",
            "amount": "1000.00",
            "currency": "RUB",
            "description": "Order payment",
        },
    )

    first_response = await postgres_client.post("/operations/operation-submit/submit")
    repeated_response = await postgres_client.post("/operations/operation-submit/submit")

    assert first_response.status_code == 202
    assert first_response.json()["status"] == "PROCESSING"
    assert repeated_response.status_code == 200
    assert repeated_response.json()["status"] == "PROCESSING"

    events_response = await postgres_client.get("/operations/operation-submit/events")
    assert [event["type"] for event in events_response.json()] == ["CREATED", "PROCESSING"]

    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions() as session:
            intent_count = await session.scalar(
                text(
                    "SELECT count(*) FROM dispatch_intents WHERE operation_id = 'operation-submit'"
                )
            )
    finally:
        await database.close()
    assert intent_count == 1


async def test_worker_dispatches_committed_intent_without_holding_operation_lock(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()
    request_received = asyncio.Event()
    release_response = asyncio.Event()
    captured_requests: list[dict[str, object]] = []

    @provider_app.post("/payments")
    async def create_provider_payment(request: Request) -> JSONResponse:
        captured_requests.append(
            {
                "body": await request.json(),
                "url": str(request.url),
                "idempotency_key": request.headers.get("Idempotency-Key"),
                "correlation_id": request.headers.get("X-Correlation-ID"),
            }
        )
        request_received.set()
        await release_response.wait()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-123",
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
            await client.post(
                "/operations",
                json={
                    "operationId": "operation-dispatch",
                    "amount": "1000.00",
                    "currency": "RUB",
                    "description": "Order payment",
                },
            )
            submit_response = await client.post("/operations/operation-dispatch/submit")

            assert submit_response.status_code == 202
            await asyncio.wait_for(request_received.wait(), timeout=1)

            claimed_intent = await read_intent_state(postgres_url, "operation-dispatch")

            repeated_response = await asyncio.wait_for(
                client.post("/operations/operation-dispatch/submit"), timeout=0.5
            )
            processing_response = await client.get("/operations/operation-dispatch")

            assert repeated_response.status_code == 200
            assert processing_response.json()["status"] == "PROCESSING"
            assert processing_response.json()["providerPaymentId"] is None
            assert claimed_intent == {
                "attempt_count": 1,
                "claimed": True,
                "dispatched": False,
            }

            release_response.set()
            accepted_operation = await wait_for_provider_payment_id(client, "operation-dispatch")

    dispatched_intent = await read_intent_state(postgres_url, "operation-dispatch")
    assert accepted_operation["status"] == "PROCESSING"
    assert accepted_operation["providerPaymentId"] == "provider-payment-123"
    assert dispatched_intent == {
        "attempt_count": 1,
        "claimed": True,
        "dispatched": True,
    }
    assert captured_requests == [
        {
            "body": {
                "operationId": "operation-dispatch",
                "amount": "1000.00",
                "currency": "RUB",
            },
            "url": "http://provider/payments",
            "idempotency_key": "operation-dispatch",
            "correlation_id": "operation-dispatch",
        }
    ]


async def test_completed_receipt_is_the_only_finalization_signal(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()

    @provider_app.post("/payments")
    async def create_provider_payment() -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-completed",
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
            await client.post(
                "/operations",
                json={
                    "operationId": "operation-completed",
                    "amount": "20.00",
                    "currency": "RUB",
                    "description": "Completed payment",
                },
            )
            await client.post("/operations/operation-completed/submit")
            accepted_operation = await wait_for_provider_payment_id(client, "operation-completed")

            assert accepted_operation["status"] == "PROCESSING"
            processing_events = await client.get("/operations/operation-completed/events")
            assert [event["type"] for event in processing_events.json()] == [
                "CREATED",
                "PROCESSING",
            ]

            receipt_response = await client.post(
                "/receipts",
                json={
                    "providerPaymentId": "provider-payment-completed",
                    "operationId": "operation-completed",
                    "result": "COMPLETED",
                    "message": "Payment completed",
                    "occurredAt": "2026-07-15T12:00:00Z",
                },
            )
            completed_response = await client.get("/operations/operation-completed")
            events_response = await client.get("/operations/operation-completed/events")

    assert receipt_response.status_code == 204
    assert receipt_response.content == b""
    assert completed_response.json()["status"] == "COMPLETED"
    assert completed_response.json()["providerPaymentId"] == "provider-payment-completed"
    assert [event["type"] for event in events_response.json()] == [
        "CREATED",
        "PROCESSING",
        "COMPLETED",
    ]
    assert events_response.json()[-1] == {
        "eventId": 3,
        "type": "COMPLETED",
        "fromStatus": "PROCESSING",
        "toStatus": "COMPLETED",
        "message": "Payment completed",
        "occurredAt": "2026-07-15T12:00:00Z",
    }


async def test_submit_unknown_operation_is_not_found(
    postgres_client: httpx.AsyncClient,
) -> None:
    response = await postgres_client.post("/operations/missing-operation/submit")

    assert response.status_code == 404


async def wait_for_provider_payment_id(
    client: httpx.AsyncClient, operation_id: str
) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/operations/{operation_id}")
        operation = response.json()
        if operation["providerPaymentId"] is not None:
            return operation
        await asyncio.sleep(0.01)
    pytest.fail("provider payment ID was not stored")


async def read_intent_state(postgres_url: str, operation_id: str) -> dict[str, object]:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT attempt_count, claimed_at IS NOT NULL, "
                        "dispatched_at IS NOT NULL FROM dispatch_intents "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            ).one()
    finally:
        await database.close()
    return {
        "attempt_count": row[0],
        "claimed": row[1],
        "dispatched": row[2],
    }
