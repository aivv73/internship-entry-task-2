import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from payment_service.config import Settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app

pytestmark = pytest.mark.integration


async def test_provider_503_schedules_durable_retry_with_same_request(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()
    first_response_sent = asyncio.Event()
    second_request_received = asyncio.Event()
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
        if len(provider_requests) == 1:
            first_response_sent.set()
            return JSONResponse(status_code=503, content={"status": "UNAVAILABLE"})
        second_request_received.set()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-retried",
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=recovery_settings(postgres_url, retry_delay=10),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await create_submitted_operation(client, "operation-retry-503")

            await asyncio.wait_for(first_response_sent.wait(), timeout=2)
            scheduled_state = await wait_for_scheduled_retry(postgres_url, "operation-retry-503")
            processing_operation = await client.get("/operations/operation-retry-503")

            assert processing_operation.json()["status"] == "PROCESSING"
            assert scheduled_state["attempt_count"] == 1
            assert scheduled_state["claimed"] is False
            assert scheduled_state["dispatched"] is False
            assert scheduled_state["next_attempt_at"] > datetime.now(UTC)

            await make_retry_due(postgres_url, "operation-retry-503")
            await asyncio.wait_for(second_request_received.wait(), timeout=2)
            operation = await wait_for_provider_payment_id(client, "operation-retry-503")

    retry_state = await read_retry_state(postgres_url, "operation-retry-503")
    expected_request = {
        "body": {
            "operationId": "operation-retry-503",
            "amount": "100.00",
            "currency": "RUB",
        },
        "idempotency_key": "operation-retry-503",
        "correlation_id": "operation-retry-503",
    }
    assert provider_requests == [expected_request, expected_request]
    assert operation["status"] == "PROCESSING"
    assert operation["providerPaymentId"] == "provider-payment-retried"
    assert retry_state["attempt_count"] == 2
    assert retry_state["next_attempt_at"] is not None
    assert retry_state["dispatched"] is True


@pytest.mark.parametrize("failure_kind", ["connection", "timeout"])
async def test_transport_failure_retries_without_fabricating_final_state(
    failure_kind: str,
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = f"operation-retry-{failure_kind}"
    provider_app = FastAPI()
    second_request_received = asyncio.Event()
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
        if len(provider_requests) == 1:
            failed_request = httpx.Request("POST", "http://provider/payments")
            if failure_kind == "connection":
                raise httpx.ConnectError("provider unavailable", request=failed_request)
            raise httpx.ReadTimeout("provider response timed out", request=failed_request)
        second_request_received.set()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": f"provider-payment-{failure_kind}",
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=recovery_settings(postgres_url),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await create_submitted_operation(client, operation_id)

            await asyncio.wait_for(second_request_received.wait(), timeout=2)
            operation = await wait_for_provider_payment_id(client, operation_id)

    expected_request = {
        "body": {
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
        },
        "idempotency_key": operation_id,
        "correlation_id": operation_id,
    }
    retry_state = await read_retry_state(postgres_url, operation_id)
    assert provider_requests == [expected_request, expected_request]
    assert operation["status"] == "PROCESSING"
    assert retry_state["attempt_count"] == 2
    assert retry_state["dispatched"] is True


async def test_lost_response_retries_without_another_provider_effect(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = "operation-lost-response"
    provider_payment_id = "provider-payment-lost-response"
    provider_app = FastAPI()
    second_request_received = asyncio.Event()
    provider_requests: list[dict[str, object]] = []
    provider_effects: dict[str, str] = {}

    @provider_app.post("/payments")
    async def create_provider_payment(request: Request) -> JSONResponse:
        body = await request.json()
        idempotency_key = request.headers["Idempotency-Key"]
        provider_requests.append(
            {
                "body": body,
                "idempotency_key": idempotency_key,
                "correlation_id": request.headers.get("X-Correlation-ID"),
            }
        )
        provider_effects.setdefault(idempotency_key, provider_payment_id)
        if len(provider_requests) == 1:
            raise httpx.ReadTimeout(
                "response lost after acceptance",
                request=httpx.Request("POST", "http://provider/payments"),
            )
        second_request_received.set()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": provider_effects[idempotency_key],
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=recovery_settings(postgres_url),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await create_submitted_operation(client, operation_id)

            await asyncio.wait_for(second_request_received.wait(), timeout=2)
            operation = await wait_for_provider_payment_id(client, operation_id)

    expected_request = {
        "body": {
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
        },
        "idempotency_key": operation_id,
        "correlation_id": operation_id,
    }
    assert provider_requests == [expected_request, expected_request]
    assert provider_effects == {operation_id: provider_payment_id}
    assert operation["status"] == "PROCESSING"
    assert operation["providerPaymentId"] == provider_payment_id


async def test_graceful_shutdown_releases_in_flight_work_for_restart(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    operation_id = "operation-graceful-restart"
    provider_payment_id = "provider-payment-graceful-restart"
    first_provider_app = FastAPI()
    first_request_received = asyncio.Event()
    hold_first_response = asyncio.Event()
    provider_requests: list[dict[str, object]] = []

    @first_provider_app.post("/payments")
    async def hold_provider_response(request: Request) -> JSONResponse:
        provider_requests.append(await captured_provider_request(request))
        first_request_received.set()
        await hold_first_response.wait()
        raise AssertionError("the interrupted provider response must not be released")

    first_database = SqlAlchemyDatabase.from_url(postgres_url)
    first_app = create_app(
        database=first_database,
        settings=recovery_settings(postgres_url, retry_delay=10),
        provider_transport=httpx.ASGITransport(app=first_provider_app),
        worker_poll_interval=0.01,
    )
    first_transport = httpx.ASGITransport(app=first_app)
    async with first_app.router.lifespan_context(first_app):
        async with httpx.AsyncClient(
            transport=first_transport, base_url="http://first-service"
        ) as first_client:
            await create_submitted_operation(first_client, operation_id)
            await asyncio.wait_for(first_request_received.wait(), timeout=2)

    interrupted_state = await read_retry_state(postgres_url, operation_id)
    assert interrupted_state["attempt_count"] == 1
    assert interrupted_state["claimed"] is False
    assert interrupted_state["dispatched"] is False

    second_provider_app = FastAPI()
    second_request_received = asyncio.Event()

    @second_provider_app.post("/payments")
    async def accept_recovered_payment(request: Request) -> JSONResponse:
        provider_requests.append(await captured_provider_request(request))
        second_request_received.set()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": provider_payment_id,
                "status": "ACCEPTED",
            },
        )

    second_database = SqlAlchemyDatabase.from_url(postgres_url)
    second_app = create_app(
        database=second_database,
        settings=recovery_settings(postgres_url),
        provider_transport=httpx.ASGITransport(app=second_provider_app),
        worker_poll_interval=0.01,
    )
    second_transport = httpx.ASGITransport(app=second_app)
    async with second_app.router.lifespan_context(second_app):
        async with httpx.AsyncClient(
            transport=second_transport, base_url="http://second-service"
        ) as second_client:
            await asyncio.wait_for(second_request_received.wait(), timeout=2)
            operation = await wait_for_provider_payment_id(second_client, operation_id)

    expected_request = {
        "body": {
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
        },
        "idempotency_key": operation_id,
        "correlation_id": operation_id,
    }
    recovered_state = await read_retry_state(postgres_url, operation_id)
    assert provider_requests == [expected_request, expected_request]
    assert operation["status"] == "PROCESSING"
    assert operation["providerPaymentId"] == provider_payment_id
    assert recovered_state["attempt_count"] == 2
    assert recovered_state["dispatched"] is True


async def test_startup_reclaims_stale_interrupted_claim(
    postgres_client: httpx.AsyncClient,
    postgres_url: str,
) -> None:
    operation_id = "operation-stale-claim"
    await create_submitted_operation(postgres_client, operation_id)
    await make_claim_stale(postgres_url, operation_id)
    provider_app = FastAPI()

    @provider_app.post("/payments")
    async def accept_reclaimed_payment() -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-stale-claim",
                "status": "ACCEPTED",
            },
        )

    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(
        database=database,
        settings=recovery_settings(postgres_url),
        provider_transport=httpx.ASGITransport(app=provider_app),
        worker_poll_interval=0.01,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            operation = await wait_for_provider_payment_id(client, operation_id)

    retry_state = await read_retry_state(postgres_url, operation_id)
    assert operation["status"] == "PROCESSING"
    assert operation["providerPaymentId"] == "provider-payment-stale-claim"
    assert retry_state["attempt_count"] == 2
    assert retry_state["dispatched"] is True


def recovery_settings(postgres_url: str, *, retry_delay: float = 0.01) -> Settings:
    return Settings(
        database_url=postgres_url,
        provider_url="http://provider",
        provider_timeout_seconds=0.02,
        dispatch_retry_base_delay_seconds=retry_delay,
        dispatch_retry_max_delay_seconds=retry_delay,
        dispatch_retry_jitter_ratio=0,
        dispatch_claim_timeout_seconds=0.05,
    )


async def create_submitted_operation(client: httpx.AsyncClient, operation_id: str) -> None:
    create_response = await client.post(
        "/operations",
        json={
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
            "description": "Dispatch recovery test",
        },
    )
    submit_response = await client.post(f"/operations/{operation_id}/submit")
    assert create_response.status_code == 201
    assert submit_response.status_code == 202


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


async def read_retry_state(postgres_url: str, operation_id: str) -> dict[str, object]:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT attempt_count, next_attempt_at, "
                        "claimed_at IS NOT NULL, dispatched_at IS NOT NULL "
                        "FROM dispatch_intents "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            ).one()
    finally:
        await database.close()
    return {
        "attempt_count": row[0],
        "next_attempt_at": row[1],
        "claimed": row[2],
        "dispatched": row[3],
    }


async def wait_for_scheduled_retry(postgres_url: str, operation_id: str) -> dict[str, object]:
    for _ in range(100):
        state = await read_retry_state(postgres_url, operation_id)
        if state["attempt_count"] == 1 and state["claimed"] is False:
            return state
        await asyncio.sleep(0.01)
    pytest.fail("retry schedule was not persisted")


async def make_retry_due(postgres_url: str, operation_id: str) -> None:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE dispatch_intents SET next_attempt_at = now() "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
    finally:
        await database.close()


async def make_claim_stale(postgres_url: str, operation_id: str) -> None:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE dispatch_intents SET attempt_count = 1, "
                    "claimed_at = now() - interval '1 hour', "
                    "next_attempt_at = now() - interval '1 hour' "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
    finally:
        await database.close()


async def captured_provider_request(request: Request) -> dict[str, object]:
    return {
        "body": await request.json(),
        "idempotency_key": request.headers.get("Idempotency-Key"),
        "correlation_id": request.headers.get("X-Correlation-ID"),
    }
