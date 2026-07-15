from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from payment_service.config import Settings, get_settings
from payment_service.database import Database, SqlAlchemyDatabase
from payment_service.operations import router as operations_router


def create_app(*, database: Database | None = None, settings: Settings | None = None) -> FastAPI:
    service_settings = settings or get_settings()
    service_database = database or SqlAlchemyDatabase.from_url(str(service_settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await service_database.close()

    app = FastAPI(title="Payment Service", lifespan=lifespan)
    app.state.database = service_database
    app.state.settings = service_settings
    app.include_router(operations_router)

    @app.get("/health")
    async def health() -> JSONResponse:
        if await service_database.is_ready():
            return JSONResponse(status_code=200, content={"status": "ok"})
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    return app


app = create_app()
