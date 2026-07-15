from datetime import datetime

import httpx
import pytest
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app

pytestmark = pytest.mark.integration


async def test_client_can_create_and_read_an_operation(
    postgres_client: httpx.AsyncClient,
) -> None:
    response = await postgres_client.post(
        "/operations",
        json={
            "operationId": "operation-create-read",
            "amount": "1000.00",
            "currency": "RUB",
            "description": "Order payment",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "operationId": "operation-create-read",
        "amount": "1000.00",
        "currency": "RUB",
        "description": "Order payment",
        "status": "CREATED",
        "providerPaymentId": None,
    }

    read_response = await postgres_client.get("/operations/operation-create-read")

    assert read_response.status_code == 200
    assert read_response.json() == response.json()


async def test_creation_records_an_ordered_created_event(
    postgres_client: httpx.AsyncClient,
) -> None:
    await create_operation(postgres_client, operation_id="operation-history")

    response = await postgres_client.get("/operations/operation-history/events")

    assert response.status_code == 200
    events = response.json()
    assert events == [
        {
            "eventId": 1,
            "type": "CREATED",
            "fromStatus": None,
            "toStatus": "CREATED",
            "message": "Operation created",
            "occurredAt": events[0]["occurredAt"],
        }
    ]
    assert datetime.fromisoformat(events[0]["occurredAt"]).tzinfo is not None


async def test_duplicate_operation_is_conflict_without_another_event(
    postgres_client: httpx.AsyncClient,
) -> None:
    first_response = await create_operation(postgres_client, operation_id="operation-duplicate")

    duplicate_response = await create_operation(postgres_client, operation_id="operation-duplicate")

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    events_response = await postgres_client.get("/operations/operation-duplicate/events")
    assert [event["eventId"] for event in events_response.json()] == [1]


@pytest.mark.parametrize(
    ("operation_id", "overrides"),
    [
        ("invalid-blank-id", {"operationId": "   "}),
        ("invalid-zero", {"amount": "0"}),
        ("invalid-negative", {"amount": "-1.00"}),
        ("invalid-scale", {"amount": "1.001"}),
        ("invalid-number", {"amount": 100}),
        ("invalid-currency", {"currency": "USD"}),
    ],
)
async def test_invalid_creation_does_not_persist(
    postgres_client: httpx.AsyncClient,
    operation_id: str,
    overrides: dict[str, object],
) -> None:
    request = operation_request(operation_id=operation_id)
    request.update(overrides)

    response = await postgres_client.post("/operations", json=request)

    assert response.status_code == 422
    read_response = await postgres_client.get(f"/operations/{operation_id}")
    assert read_response.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/operations/missing-operation", "/operations/missing-operation/events"],
)
async def test_unknown_operation_is_not_found(
    postgres_client: httpx.AsyncClient, path: str
) -> None:
    response = await postgres_client.get(path)

    assert response.status_code == 404


async def test_operation_survives_a_new_database_instance(
    postgres_client: httpx.AsyncClient, postgres_url: str
) -> None:
    await create_operation(postgres_client, operation_id="operation-persistent")

    replacement_database = SqlAlchemyDatabase.from_url(postgres_url)
    replacement_app = create_app(database=replacement_database)
    transport = httpx.ASGITransport(app=replacement_app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/operations/operation-persistent")
            events_response = await client.get("/operations/operation-persistent/events")
    finally:
        await replacement_database.close()

    assert response.status_code == 200
    assert response.json()["status"] == "CREATED"
    assert [event["eventId"] for event in events_response.json()] == [1]


async def test_migrations_create_operation_and_event_schema(
    postgres_url: str, migrated_database: None
) -> None:
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(read_operation_schema)
    finally:
        await engine.dispose()

    assert schema == {
        "tables": {
            "alembic_version",
            "dispatch_intents",
            "operations",
            "operation_events",
        },
        "operation_primary_key": ["operation_id"],
        "event_primary_key": ["operation_id", "event_id"],
        "intent_primary_key": ["operation_id"],
    }


def operation_request(*, operation_id: str) -> dict[str, object]:
    return {
        "operationId": operation_id,
        "amount": "1000.00",
        "currency": "RUB",
        "description": "Order payment",
    }


async def create_operation(client: httpx.AsyncClient, *, operation_id: str) -> httpx.Response:
    return await client.post("/operations", json=operation_request(operation_id=operation_id))


def read_operation_schema(connection: Connection) -> dict[str, object]:
    inspector = inspect(connection)
    return {
        "tables": set(inspector.get_table_names()),
        "operation_primary_key": inspector.get_pk_constraint("operations")["constrained_columns"],
        "event_primary_key": inspector.get_pk_constraint("operation_events")["constrained_columns"],
        "intent_primary_key": inspector.get_pk_constraint("dispatch_intents")[
            "constrained_columns"
        ],
    }
