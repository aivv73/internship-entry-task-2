# REQ-service-foundation: Service readiness and operation records

The internship assignment in the root `README.md` is the external authority for this record. Its API
routes and success statuses are mandatory and may not be renamed. GitHub issues #2 and #3 narrow the
implemented foundation without weakening that authority.

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

The assignment remains the authority for provider submission, receipts, retries, and recovery
requirements that are not duplicated in this initial record set.

[SPEC-readiness](SPEC-readiness.md) refines the readiness behavior.
[SPEC-operation-records](SPEC-operation-records.md) refines operation creation and inspection.
Both run within [ARCH-payment-service](ARCH-payment-service.md).
