# ARCH-payment-service: Payment service architecture

The payment service is a single Python application that owns payment-operation state and exposes
HTTP interfaces for readiness, operation creation, current-state reads, and event-history reads.
PostgreSQL is the authoritative durable store. The current system does not derive authoritative
state from process memory.

The application is packaged as `candidate-service` and listens on port 8080. Docker Compose places
it beside PostgreSQL and supplies `DATABASE_URL` and `PROVIDER_URL`. The provider address is part of
runtime configuration, but the current code has no outbound provider adapter or receipt endpoint.
Those capabilities remain tracked outside Linked Specs until they become current behavior.

## Components and boundaries

- **FastAPI application:** owns process lifespan, HTTP routing, dependency wiring, and readiness.
- **Operation API:** validates the public JSON contract and coordinates transactional creation and
  read-only queries.
- **Persistence model:** represents operations and their ordered state-transition events.
- **Database adapter:** owns the async SQLAlchemy engine, session factory, readiness probe, and
  shutdown disposal.
- **Alembic migrations:** are the reproducible authority for PostgreSQL schema evolution.
- **PostgreSQL:** owns committed operation data, uniqueness constraints, event ordering keys, and
  persistence across application instances.

Dependencies point inward from HTTP and migration adapters toward the operation and persistence
model. The application reaches PostgreSQL through async SQLAlchemy sessions; tests exercise the
same public HTTP boundary with PostgreSQL rather than substituting an in-memory store.

## Current flows

Creating an operation validates the request before opening a transaction. One database transaction
inserts the operation in `CREATED` and its initial event. A uniqueness failure rolls back both
records and becomes `409 Conflict`.

Reading an operation or its events opens a short-lived session. Event history is ordered by the
per-operation `eventId`. Readiness executes a database query and cannot report ready when
PostgreSQL is unavailable.

## Architectural invariants

- PostgreSQL is the sole durable source of truth.
- An operation and the event describing its creation commit or roll back together.
- `operationId` is globally unique within the service.
- Event identity is unique within an operation and histories are returned in ascending `eventId`
  order.
- Money is validated and stored as decimal data, never binary floating point.
- Database migrations run before the container starts serving traffic.

The platform choice and rationale are recorded in
[DESIGN-async-python-postgresql](DESIGN-async-python-postgresql.md). External obligations for the
currently implemented surface are captured by
[REQ-service-foundation](REQ-service-foundation.md). Behavioral details are refined by
[SPEC-readiness](SPEC-readiness.md) and
[SPEC-operation-records](SPEC-operation-records.md).
