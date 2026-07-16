# ARCH-payment-service: Payment service architecture

The payment service is a single Python application that owns payment-operation state and exposes
HTTP interfaces for readiness, operation creation and submission, receipt callbacks, current-state
reads, event-history reads, and Prometheus metrics.
PostgreSQL is the authoritative durable store. The current system does not derive authoritative
state from process memory.

The application is packaged as `candidate-service` and listens on port 8080. Docker Compose places
it beside PostgreSQL and supplies `DATABASE_URL` and `PROVIDER_URL`. The provider address is part of
runtime configuration and is called by a background dispatch worker. Multiple application instances
coordinate submissions and worker claims through PostgreSQL rather than process-local state.

## Components and boundaries

- **FastAPI application:** owns process lifespan, HTTP routing, dependency wiring, and readiness.
- **Operation API:** validates the public JSON contract and coordinates transactional creation and
  submission plus read-only queries.
- **Receipt API:** applies either provider-confirmed final result or audits an ignored opposite result
  in the same transaction as its event.
- **Dispatch worker:** claims durable send intents, calls the provider outside the claim transaction,
  persists accepted provider identifiers, and durably reschedules ambiguous failures.
- **Provider adapter:** owns the external HTTP request and its idempotency and correlation headers.
- **Observability adapter:** emits structured payment logs and bounded-cardinality metrics without
  participating in authoritative transactions.
- **Persistence model:** represents operations and their ordered transition and receipt-audit events.
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

Submitting a `CREATED` operation locks it for a short transaction that creates one dispatch intent,
changes the operation to `PROCESSING`, and appends the transition event. The worker claims that
intent in a separate transaction, releases database locks, and then calls the provider. A provider
`202 Accepted` stores a consistent `providerPaymentId` but never changes operation status; absent an
earlier receipt, the operation therefore remains `PROCESSING`.

Concurrent submits serialize on the operation row, so exactly one request performs the transition.
Competing workers use row locking with locked-row skipping, so at most one claims an intent while
other workers remain free to claim different work.

Each claim increments a durable attempt count. Transport failures and non-accepted responses release
the claim and set a durable future attempt time without changing operation status. Graceful shutdown
releases an in-flight claim immediately; after an abrupt interruption, the claim lease expires and a
worker can reclaim it. Application startup polls the same persisted due intents, so no separate
in-memory recovery source exists. PostgreSQL time is authoritative for claim leases and retry due
times, avoiding coordination dependence on application-host clock agreement.

A `COMPLETED` or `REJECTED` receipt locks the operation and atomically establishes a missing
provider linkage, changes its state, and appends the final transition event. Provider transport
success alone never establishes a final state. Equivalent receipts are no-ops; an opposite later
result appends an ignored-receipt audit event without changing the first final state.

Reading an operation or its events opens a short-lived session. Event history is ordered by the
per-operation `eventId`. Readiness executes a database query and cannot report ready when
PostgreSQL is unavailable.

Metrics reads derive unfinished-operation counts from PostgreSQL and combine them with process-local
dispatch and receipt counters. Payment logs are JSON objects correlated by operation ID. Metrics and
logging failures are isolated from payment state changes and worker progress.

## Architectural invariants

- PostgreSQL is the sole durable source of truth.
- An operation and the event describing its creation commit or roll back together.
- A send intent, `PROCESSING` transition, and corresponding event commit or roll back together.
- Concurrent submits create one intent and one transition, independent of the serving instance.
- Competing worker loops cannot claim the same intent concurrently.
- Retry attempt and schedule metadata survive process and container replacement.
- Unfinished claims are recoverable through graceful release or lease expiry.
- Provider HTTP occurs only after the send-intent transaction commits and holds no operation lock.
- Only a callback receipt can establish a final operation state.
- The first valid final receipt wins; equivalent delivery is idempotent and opposite delivery is
  audited without changing that state.
- `operationId` is globally unique within the service.
- A non-null `providerPaymentId` belongs to at most one operation.
- Event identity is unique within an operation and histories are returned in ascending `eventId`
  order.
- Money is validated and stored as decimal data, never binary floating point.
- Metric labels never contain operation or provider identifiers.
- Observability is non-authoritative and cannot veto payment processing.
- Database migrations run before the container starts serving traffic.

The platform choice and rationale are recorded in
[DESIGN-async-python-postgresql](DESIGN-async-python-postgresql.md). External obligations for the
currently implemented surface are captured by
[REQ-service-foundation](REQ-service-foundation.md). Behavioral details are refined by
[SPEC-readiness](SPEC-readiness.md) and
[SPEC-operation-records](SPEC-operation-records.md). Durable dispatch is governed by
[DESIGN-durable-dispatch-intent](DESIGN-durable-dispatch-intent.md) and refined by
[SPEC-durable-dispatch](SPEC-durable-dispatch.md). Logs and metrics are specified by
[SPEC-payment-observability](SPEC-payment-observability.md).
