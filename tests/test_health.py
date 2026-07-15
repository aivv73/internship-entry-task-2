from collections.abc import AsyncIterator

import httpx
import pytest

from payment_service.main import create_app


class StubDatabase:
    def __init__(self, *, is_reachable: bool) -> None:
        self.is_reachable = is_reachable

    async def is_ready(self) -> bool:
        return self.is_reachable

    async def close(self) -> None:
        pass


@pytest.fixture
async def client(request: pytest.FixtureRequest) -> AsyncIterator[httpx.AsyncClient]:
    database = StubDatabase(is_reachable=request.param)
    app = create_app(database=database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.mark.parametrize("client", [True], indirect=True)
async def test_health_is_ready_when_database_is_reachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("client", [False], indirect=True)
async def test_health_is_unavailable_when_database_cannot_be_reached(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
