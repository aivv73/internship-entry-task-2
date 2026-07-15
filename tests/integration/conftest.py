import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from payment_service.config import get_settings
from payment_service.database import SqlAlchemyDatabase
from payment_service.main import create_app


@pytest.fixture(scope="session")
def postgres_url() -> str:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return database_url


@pytest.fixture(scope="session")
def migrated_database(postgres_url: str) -> Iterator[None]:
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = postgres_url
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    yield
    if previous_database_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_database_url
    get_settings.cache_clear()


@pytest.fixture
async def clean_operation_tables(postgres_url: str, migrated_database: None) -> None:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    try:
        async with database.sessions.begin() as session:
            await session.execute(text("TRUNCATE operation_events, operations CASCADE"))
    finally:
        await database.close()


@pytest.fixture
async def postgres_client(
    postgres_url: str, clean_operation_tables: None
) -> AsyncIterator[httpx.AsyncClient]:
    database = SqlAlchemyDatabase.from_url(postgres_url)
    app = create_app(database=database)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await database.close()
