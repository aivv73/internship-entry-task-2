import os

import httpx
import pytest

from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app


async def get_health(database_url: str) -> httpx.Response:
    database = SqlAlchemyDatabase.from_url(database_url)
    app = create_app(database=database)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")
    finally:
        await database.close()


@pytest.mark.integration
async def test_health_uses_reachable_postgres() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL integration test")

    response = await get_health(database_url)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_health_rejects_an_unreachable_postgres() -> None:
    response = await get_health("postgresql+asyncpg://payment:payment@127.0.0.1:1/payments")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
