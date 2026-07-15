# DESIGN-async-python-postgresql: Async Python with PostgreSQL

Status: confirmed, 2026-07-15, aivv73

The service uses Python 3.14 and FastAPI, async SQLAlchemy with asyncpg, Alembic migrations, and
PostgreSQL as its persistent store.

## Rationale

The assignment requires correctness under concurrent requests, ambiguous network outcomes, and
process restarts. PostgreSQL transactions and constraints provide a durable coordination boundary
for operation state and event history. Async database and HTTP-capable application infrastructure
allows later provider calls to avoid blocking the service process while preserving short database
transactions.

Alembic makes the persisted model reproducible in containers and tests. FastAPI and Pydantic keep
the required JSON contract explicit while allowing integration tests to exercise the ASGI boundary
without starting a separate HTTP server.

## Tradeoffs and alternatives

PostgreSQL adds a Compose service and operational setup compared with SQLite, and asynchronous
SQLAlchemy adds lifecycle and testing complexity compared with synchronous persistence. These costs
were accepted in exchange for clearer concurrency semantics and production-representative tests.

Python was selected instead of the assignment's C# option. SQLite was considered for simplicity but
not selected. The decision does not require every module to be asynchronous when no I/O is involved;
it governs service and persistence boundaries.

This choice shapes [ARCH-payment-service](ARCH-payment-service.md) and supports the durability
obligations in [REQ-service-foundation](REQ-service-foundation.md).
