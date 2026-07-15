from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from payment_service.config import Settings, get_settings
from payment_service.database import Database, SqlAlchemyDatabase
from payment_service.dispatcher import DispatchWorker
from payment_service.operations import router as operations_router
from payment_service.provider import ProviderClient
from payment_service.receipts import router as receipts_router


def create_app(
    *,
    database: Database | None = None,
    settings: Settings | None = None,
    provider_transport: httpx.AsyncBaseTransport | None = None,
    worker_poll_interval: float = 0.25,
) -> FastAPI:
    service_settings = settings or get_settings()
    service_database = database or SqlAlchemyDatabase.from_url(str(service_settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service_provider = ProviderClient(
            httpx.AsyncClient(
                base_url=str(service_settings.provider_url).rstrip("/"),
                timeout=10,
                transport=provider_transport,
                trust_env=False,
            )
        )
        dispatch_worker = DispatchWorker(
            service_database, service_provider, poll_interval=worker_poll_interval
        )
        dispatch_worker.start()
        try:
            yield
        finally:
            await dispatch_worker.stop()
            await service_provider.close()
            await service_database.close()

    app = FastAPI(title="Payment Service", lifespan=lifespan)
    app.state.database = service_database
    app.state.settings = service_settings
    app.include_router(operations_router)
    app.include_router(receipts_router)

    @app.get("/health")
    async def health() -> JSONResponse:
        if await service_database.is_ready():
            return JSONResponse(status_code=200, content={"status": "ok"})
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    return app


app = create_app()
