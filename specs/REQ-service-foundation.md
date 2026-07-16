# REQ-service-foundation: Service readiness and operation records

The internship assignment in the root `README.md` is the external authority for this record. Its API
routes and success statuses are mandatory and may not be renamed. GitHub issues #2 through #8 narrow
the implemented foundation without weakening that authority.

## Readiness obligation

The candidate service must listen on port 8080 and expose `GET /health`. It may return `200 OK` only
when PostgreSQL is reachable. Container packaging must supply runtime database and provider URLs and
must support startup through Docker Compose.

## Operation-record obligations

- `POST /operations` must accept a required, non-empty `operationId`, a positive decimal string with
  no more than two fractional digits, currency `RUB`, and an optional description.
- Successful creation must return `201 Created` with the operation in `CREATED` and a null
  `providerPaymentId`.
- Reusing an `operationId` must return `409 Conflict` and must not create another operation or event.
- Invalid input must produce a validation error without persistent changes.
- `GET /operations/{id}` must return the persisted current representation or `404 Not Found`.
- `GET /operations/{id}/events` must return the operation's committed events in order or `404 Not
  Found` for an unknown operation.
- Creating an operation and its initial `CREATED` event must be one atomic persistent change.
- Each event must expose `eventId`, type, prior and resulting status, message, and occurrence time.
  Event IDs must increase monotonically within an operation.
- Operation state and event history must survive application replacement while PostgreSQL data is
  retained.

## Initial dispatch obligations

- The first `POST /operations/{id}/submit` must atomically store one durable send intent, change
  `CREATED` to `PROCESSING`, append the transition event, and return `202 Accepted`.
- Repeated submit must create no additional intent or transition and return the current state with
  `200 OK`.
- The provider request must use the configured provider URL, immutable operation ID, amount, and
  currency. `Idempotency-Key` and `X-Correlation-ID` must both equal `operationId`.
- Provider HTTP must occur after the intent commits and without holding an operation lock.
- A provider `202 Accepted` may establish `providerPaymentId` but must not finalize the operation.
- A matching `COMPLETED` receipt must atomically finalize `PROCESSING` and append its event, returning
  `204 No Content`.

## Race-safe receipt obligations

- Receipts must accept only `COMPLETED` and `REJECTED`; unknown operations return `404` and invalid
  request bodies cannot change persistent state.
- The first valid receipt may establish a missing provider ID and must commit linkage, final status,
  and its event in one transaction.
- An equivalent repeated receipt returns `204` without another transition or audit record.
- A later opposite result returns `204`, preserves the first final status, and records one ignored
  receipt audit event; repeating it adds no further audit event.
- A receipt with a provider ID inconsistent with established linkage returns `409` without changes,
  and one provider ID may belong to only one operation.
- A callback may finalize before provider HTTP returns. Later transport acceptance may persist a
  consistent linkage and dispatch outcome but cannot change the final state.

## Concurrent dispatch obligations

- A simultaneous submit burst must produce exactly one `202`; remaining requests return `200` with
  the persisted `PROCESSING` state.
- The burst must create exactly one durable intent and one `CREATED` to `PROCESSING` transition.
- Multiple worker loops must claim intents atomically in short transactions. Provider HTTP must occur
  after claim commit, and callbacks must remain able to commit while a provider response is open.
- The provider must observe one payment effect for the operation, with the stable operation ID as
  both idempotency and correlation key and one immutable request body.

## Dispatch recovery obligations

- Provider `503` responses, connection failures, timeouts, and lost responses must leave the
  operation `PROCESSING` and schedule another delivery of the same request identity and body.
- Attempt count and next-attempt time must persist in PostgreSQL. Retry delay must use configurable
  exponential backoff with jitter and a bounded maximum.
- Startup must resume due intents and reclaim expired interrupted claims. Graceful shutdown must stop
  polling and release in-flight work for recovery.
- A late accepted response may persist consistent linkage and dispatch completion but cannot change
  a callback-established final result. No provider transport outcome may fabricate a final status.
- Repeated delivery after a lost response relies on the stable provider idempotency key and must not
  produce another provider payment effect.

## Observability obligations

- Payment logs must be structured and correlate provider attempts and callbacks by `operationId`,
  including available provider linkage and retry attempt where relevant.
- `GET /metrics` must return Prometheus-compatible backlog, provider attempt/retry/outcome, and
  ignored/conflicting receipt metrics.
- Metric labels must have bounded cardinality and may not contain operation or provider identifiers.
- Logging and metrics failures must not change payment state or prevent processing.

The assignment remains the authority for provider submission, receipts, retries, and recovery
requirements that are not duplicated in this initial record set.

[SPEC-readiness](SPEC-readiness.md) refines the readiness behavior.
[SPEC-operation-records](SPEC-operation-records.md) refines operation creation and inspection.
[SPEC-durable-dispatch](SPEC-durable-dispatch.md) refines initial submission, dispatch, and receipt
behavior. [SPEC-payment-observability](SPEC-payment-observability.md) refines logs and metrics. All
run within [ARCH-payment-service](ARCH-payment-service.md).
