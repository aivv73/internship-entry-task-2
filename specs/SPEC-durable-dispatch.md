# SPEC-durable-dispatch: Initial provider dispatch and completion

This specification refines the initial dispatch obligations in
[REQ-service-foundation](REQ-service-foundation.md) and follows
[DESIGN-durable-dispatch-intent](DESIGN-durable-dispatch-intent.md).

## Submit

`POST /operations/{id}/submit` locks the operation for a short database transaction. For a
`CREATED` operation it inserts the unique dispatch intent, changes status to `PROCESSING`, and adds
the next per-operation event with message `Operation submitted`. The transaction commits before the
endpoint returns `202 Accepted`.

Submitting an operation already in a non-`CREATED` state returns its current representation with
`200 OK` and creates no new intent or event. An unknown operation returns `404 Not Found`.

## Dispatch

The application lifespan owns one background dispatch worker. The worker claims one unclaimed,
undispatched intent in a short transaction, records its claim and attempt count, and copies the
immutable operation ID, decimal amount, and currency. It then releases the transaction before
calling `POST {PROVIDER_URL}/payments`.

The request body contains `operationId`, amount, and currency. Both `Idempotency-Key` and
`X-Correlation-ID` equal `operationId`. A response is accepted only when its HTTP status is `202`,
its body status is `ACCEPTED`, and it contains `providerPaymentId`.

Acceptance stores the provider ID and marks the intent dispatched in one short transaction. The
operation remains `PROCESSING`, and no state-transition event is appended for transport acceptance.

## Completion receipt

`POST /receipts` currently accepts result `COMPLETED`. The receipt must identify an existing
`PROCESSING` operation and match its established `providerPaymentId`; unknown operations return
`404 Not Found` and other mismatches return `409 Conflict`.

A valid receipt locks the operation and atomically changes it to `COMPLETED` and appends the next
event. The event uses the receipt message and occurrence time, with `PROCESSING` as its prior state.
The endpoint returns `204 No Content` with an empty body.

## Current exceptions

Provider failures and interrupted claims are logged but are not yet retried or recovered. Early,
duplicate, opposite-result, `REJECTED`, and conflicting receipts are not yet supported beyond basic
conflict rejection. These are known, scoped divergences from the root assignment and remain tracked
by the dependent GitHub tickets for receipt races and dispatch recovery; they are not permission to
treat the behavior as complete.

Operation representations and event history remain governed by
[SPEC-operation-records](SPEC-operation-records.md).
