from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database(Protocol):
    sessions: async_sessionmaker[AsyncSession]

    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


class SqlAlchemyDatabase:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, database_url: str) -> SqlAlchemyDatabase:
        return cls(create_async_engine(database_url, pool_pre_ping=True))

    async def is_ready(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except OSError, SQLAlchemyError, TimeoutError:
            return False
        return True

    async def close(self) -> None:
        await self._engine.dispose()
