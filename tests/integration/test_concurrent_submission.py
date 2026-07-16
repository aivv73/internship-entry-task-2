import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from payment_service.config import Settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("scenario", range(5))
async def test_concurrent_submission_has_one_intent_and_one_provider_effect(
    scenario: int,
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = f"operation-concurrent-{scenario}"
    provider_payment_id = f"provider-payment-concurrent-{scenario}"
    provider_app = FastAPI()
    request_received = asyncio.Event()
    release_response = asyncio.Event()
    provider_requests: list[dict[str, object]] = []

    @provider_app.post("/payments")
    async def create_provider_payment(request: Request) -> JSONResponse:
        provider_requests.append(
            {
                "body": await request.json(),
                "idempotency_key": request.headers.get("Idempotency-Key"),
                "correlation_id": request.headers.get("X-Correlation-ID"),
            }
        )
        request_received.set()
        await release_response.wait()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": provider_payment_id,
                "status": "ACCEPTED",
            },
        )

    settings = Settings(database_url=postgres_url, provider_url="http://provider")
    first_database = SqlAlchemyDatabase.from_url(postgres_url)
    second_database = SqlAlchemyDatabase.from_url(postgres_url)
    first_app = create_app(
        database=first_database,
        settings=settings,
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    second_app = create_app(
        database=second_database,
        settings=settings,
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(first_app.router.lifespan_context(first_app))
        await stack.enter_async_context(second_app.router.lifespan_context(second_app))
        first_client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first_app),
                base_url="http://first-service",
            )
        )
        second_client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="http://second-service",
            )
        )
        create_response = await first_client.post(
            "/operations",
            json={
                "operationId": operation_id,
                "amount": "100.00",
                "currency": "RUB",
                "description": "Concurrent submission",
            },
        )
        assert create_response.status_code == 201

        clients = (first_client, second_client)
        submit_responses = await asyncio.gather(
            *(
                clients[index % len(clients)].post(f"/operations/{operation_id}/submit")
                for index in range(20)
            )
        )

        assert [response.status_code for response in submit_responses].count(202) == 1
        assert [response.status_code for response in submit_responses].count(200) == 19
        assert {response.json()["status"] for response in submit_responses} == {"PROCESSING"}

        await asyncio.wait_for(request_received.wait(), timeout=2)
        claimed_state = await read_intent_state(postgres_url, operation_id)
        processing_events = await first_client.get(f"/operations/{operation_id}/events")

        assert claimed_state == {"count": 1, "attempt_count": 1, "dispatched": False}
        assert [event["type"] for event in processing_events.json()] == [
            "CREATED",
            "PROCESSING",
        ]
        assert provider_requests == [
            {
                "body": {
                    "operationId": operation_id,
                    "amount": "100.00",
                    "currency": "RUB",
                },
                "idempotency_key": operation_id,
                "correlation_id": operation_id,
            }
        ]

        receipt_response = await asyncio.wait_for(
            second_client.post(
                "/receipts",
                json={
                    "providerPaymentId": provider_payment_id,
                    "operationId": operation_id,
                    "result": "COMPLETED",
                    "message": "Payment completed",
                    "occurredAt": "2026-07-15T12:00:00Z",
                },
            ),
            timeout=2,
        )
        finalized_before_response = await first_client.get(f"/operations/{operation_id}")

        assert receipt_response.status_code == 204
        assert finalized_before_response.json()["status"] == "COMPLETED"

        release_response.set()
        await wait_for_dispatched_intent(postgres_url, operation_id)

        finalized_after_response = await second_client.get(f"/operations/{operation_id}")
        final_events = await second_client.get(f"/operations/{operation_id}/events")

    assert finalized_after_response.json()["status"] == "COMPLETED"
    assert finalized_after_response.json()["providerPaymentId"] == provider_payment_id
    assert [event["type"] for event in final_events.json()] == [
        "CREATED",
        "PROCESSING",
        "COMPLETED",
    ]
    assert len(provider_requests) == 1


async def read_intent_state(postgres_url: str, operation_id: str) -> dict[str, object]:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*), max(attempt_count), "
                        "bool_or(dispatched_at IS NOT NULL) "
                        "FROM dispatch_intents WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            ).one()
    finally:
        await database.close()
    return {"count": row[0], "attempt_count": row[1], "dispatched": row[2]}


async def wait_for_dispatched_intent(postgres_url: str, operation_id: str) -> None:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        for _ in range(100):
            async with database.sessions() as session:
                dispatched = await session.scalar(
                    text(
                        "SELECT dispatched_at IS NOT NULL FROM dispatch_intents "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            if dispatched:
                return
            await asyncio.sleep(0.01)
        pytest.fail("provider acceptance was not recorded")
    finally:
        await database.close()
