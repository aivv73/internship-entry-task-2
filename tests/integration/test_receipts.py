import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from payment_service.config import Settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app

pytestmark = pytest.mark.integration


async def test_rejected_receipt_establishes_provider_link_and_finalizes_operation(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-rejected")

    receipt_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-rejected",
            provider_payment_id="provider-payment-rejected",
            result="REJECTED",
            message="Payment rejected",
        ),
    )

    operation_response = await postgres_client.get("/operations/operation-rejected")
    events_response = await postgres_client.get("/operations/operation-rejected/events")

    assert receipt_response.status_code == 204
    assert receipt_response.content == b""
    assert operation_response.json()["status"] == "REJECTED"
    assert operation_response.json()["providerPaymentId"] == "provider-payment-rejected"
    assert [event["type"] for event in events_response.json()] == [
        "CREATED",
        "PROCESSING",
        "REJECTED",
    ]
    assert events_response.json()[-1] == {
        "eventId": 3,
        "type": "REJECTED",
        "fromStatus": "PROCESSING",
        "toStatus": "REJECTED",
        "message": "Payment rejected",
        "occurredAt": "2026-07-15T12:00:00Z",
    }


async def test_equivalent_receipt_is_idempotent(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-duplicate-receipt")
    first_receipt = receipt_body(
        operation_id="operation-duplicate-receipt",
        provider_payment_id="provider-payment-duplicate",
        result="COMPLETED",
        message="Payment completed",
    )
    first_response = await postgres_client.post("/receipts", json=first_receipt)

    repeated_response = await postgres_client.post(
        "/receipts",
        json={
            **first_receipt,
            "message": "Repeated delivery with different metadata",
            "occurredAt": "2026-07-15T12:01:00Z",
        },
    )

    events_response = await postgres_client.get("/operations/operation-duplicate-receipt/events")

    assert first_response.status_code == 204
    assert repeated_response.status_code == 204
    assert [event["type"] for event in events_response.json()] == [
        "CREATED",
        "PROCESSING",
        "COMPLETED",
    ]


async def test_opposite_receipt_is_audited_without_changing_first_final_result(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-opposite-receipt")
    await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-opposite-receipt",
            provider_payment_id="provider-payment-opposite",
            result="COMPLETED",
            message="Payment completed",
        ),
    )

    opposite_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-opposite-receipt",
            provider_payment_id="provider-payment-opposite",
            result="REJECTED",
            message="Provider later reported rejection",
        ),
    )
    repeated_opposite_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-opposite-receipt",
            provider_payment_id="provider-payment-opposite",
            result="REJECTED",
            message="Repeated opposite receipt",
        ),
    )

    operation_response = await postgres_client.get("/operations/operation-opposite-receipt")
    events_response = await postgres_client.get("/operations/operation-opposite-receipt/events")

    assert opposite_response.status_code == 204
    assert repeated_opposite_response.status_code == 204
    assert operation_response.json()["status"] == "COMPLETED"
    assert len(events_response.json()) == 4
    assert events_response.json()[-1] == {
        "eventId": 4,
        "type": "RECEIPT_IGNORED",
        "fromStatus": "COMPLETED",
        "toStatus": "COMPLETED",
        "message": "Provider later reported rejection",
        "occurredAt": "2026-07-15T12:00:00Z",
    }


async def test_mismatched_provider_id_is_rejected_without_changes(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-mismatched-provider")
    await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-mismatched-provider",
            provider_payment_id="provider-payment-established",
            result="COMPLETED",
            message="Payment completed",
        ),
    )

    mismatch_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-mismatched-provider",
            provider_payment_id="provider-payment-conflicting",
            result="REJECTED",
            message="Conflicting provider receipt",
        ),
    )

    operation_response = await postgres_client.get("/operations/operation-mismatched-provider")
    events_response = await postgres_client.get("/operations/operation-mismatched-provider/events")
    assert mismatch_response.status_code == 409
    assert operation_response.json()["status"] == "COMPLETED"
    assert operation_response.json()["providerPaymentId"] == "provider-payment-established"
    assert [event["type"] for event in events_response.json()] == [
        "CREATED",
        "PROCESSING",
        "COMPLETED",
    ]


async def test_provider_payment_id_cannot_link_two_operations(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-provider-owner")
    await create_submitted_operation(postgres_client, "operation-provider-conflict")
    await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-provider-owner",
            provider_payment_id="provider-payment-shared",
            result="COMPLETED",
            message="First operation completed",
        ),
    )

    conflict_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-provider-conflict",
            provider_payment_id="provider-payment-shared",
            result="REJECTED",
            message="Conflicting operation rejected",
        ),
    )

    conflicting_operation = await postgres_client.get("/operations/operation-provider-conflict")
    conflicting_events = await postgres_client.get("/operations/operation-provider-conflict/events")
    assert conflict_response.status_code == 409
    assert conflicting_operation.json()["status"] == "PROCESSING"
    assert conflicting_operation.json()["providerPaymentId"] is None
    assert [event["type"] for event in conflicting_events.json()] == ["CREATED", "PROCESSING"]


async def test_unknown_and_invalid_receipts_do_not_change_operations(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-invalid-receipt")

    unknown_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="missing-operation",
            provider_payment_id="provider-payment-missing",
            result="COMPLETED",
            message="Unknown operation",
        ),
    )
    invalid_response = await postgres_client.post(
        "/receipts",
        json=receipt_body(
            operation_id="operation-invalid-receipt",
            provider_payment_id="provider-payment-invalid",
            result="PENDING",
            message="Invalid result",
        ),
    )

    operation_response = await postgres_client.get("/operations/operation-invalid-receipt")
    events_response = await postgres_client.get("/operations/operation-invalid-receipt/events")
    assert unknown_response.status_code == 404
    assert invalid_response.status_code == 422
    assert operation_response.json()["status"] == "PROCESSING"
    assert operation_response.json()["providerPaymentId"] is None
    assert [event["type"] for event in events_response.json()] == ["CREATED", "PROCESSING"]


async def test_early_receipt_wins_before_late_provider_acceptance(
    postgres_url: str,
    clean_operation_tables: None,
) -> None:
    provider_app = FastAPI()
    request_received = asyncio.Event()
    release_response = asyncio.Event()

    @provider_app.post("/payments")
    async def create_provider_payment() -> JSONResponse:
        request_received.set()
        await release_response.wait()
        return JSONResponse(
            status_code=202,
            content={
                "providerPaymentId": "provider-payment-early",
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
            await create_submitted_operation(client, "operation-early-receipt")
            await asyncio.wait_for(request_received.wait(), timeout=1)

            receipt_response = await client.post(
                "/receipts",
                json=receipt_body(
                    operation_id="operation-early-receipt",
                    provider_payment_id="provider-payment-early",
                    result="REJECTED",
                    message="Provider rejected payment",
                ),
            )
            finalized_before_response = await client.get("/operations/operation-early-receipt")

            release_response.set()
            await wait_for_dispatched_intent(postgres_url, "operation-early-receipt")

            finalized_after_response = await client.get("/operations/operation-early-receipt")
            events_response = await client.get("/operations/operation-early-receipt/events")

    assert receipt_response.status_code == 204
    assert finalized_before_response.json()["status"] == "REJECTED"
    assert finalized_after_response.json()["status"] == "REJECTED"
    assert finalized_after_response.json()["providerPaymentId"] == "provider-payment-early"
    assert [event["type"] for event in events_response.json()] == [
        "CREATED",
        "PROCESSING",
        "REJECTED",
    ]


async def test_simultaneous_opposite_receipts_preserve_one_final_result(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_submitted_operation(postgres_client, "operation-simultaneous-receipts")
    completed_receipt = receipt_body(
        operation_id="operation-simultaneous-receipts",
        provider_payment_id="provider-payment-simultaneous",
        result="COMPLETED",
        message="Payment completed",
    )
    rejected_receipt = receipt_body(
        operation_id="operation-simultaneous-receipts",
        provider_payment_id="provider-payment-simultaneous",
        result="REJECTED",
        message="Payment rejected",
    )

    responses = await asyncio.gather(
        postgres_client.post("/receipts", json=completed_receipt),
        postgres_client.post("/receipts", json=rejected_receipt),
    )

    operation_response = await postgres_client.get("/operations/operation-simultaneous-receipts")
    events_response = await postgres_client.get(
        "/operations/operation-simultaneous-receipts/events"
    )
    final_status = operation_response.json()["status"]
    events = events_response.json()
    assert [response.status_code for response in responses] == [204, 204]
    assert final_status in ("COMPLETED", "REJECTED")
    assert [event["type"] for event in events[:2]] == ["CREATED", "PROCESSING"]
    assert events[2]["type"] == final_status
    assert events[3]["type"] == "RECEIPT_IGNORED"
    assert events[3]["fromStatus"] == final_status
    assert events[3]["toStatus"] == final_status


async def create_submitted_operation(client: httpx.AsyncClient, operation_id: str) -> None:
    create_response = await client.post(
        "/operations",
        json={
            "operationId": operation_id,
            "amount": "100.00",
            "currency": "RUB",
            "description": "Receipt test",
        },
    )
    submit_response = await client.post(f"/operations/{operation_id}/submit")
    assert create_response.status_code == 201
    assert submit_response.status_code == 202


def receipt_body(
    *,
    operation_id: str,
    provider_payment_id: str,
    result: str,
    message: str,
) -> dict[str, str]:
    return {
        "providerPaymentId": provider_payment_id,
        "operationId": operation_id,
        "result": result,
        "message": message,
        "occurredAt": "2026-07-15T12:00:00Z",
    }


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
