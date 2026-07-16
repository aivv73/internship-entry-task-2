# SPEC-durable-dispatch: Durable dispatch and race-safe receipts

This specification refines the initial dispatch and race-safe receipt obligations in
[REQ-service-foundation](REQ-service-foundation.md) and follows
[DESIGN-durable-dispatch-intent](DESIGN-durable-dispatch-intent.md).

## Submit

`POST /operations/{id}/submit` locks the operation for a short database transaction. For a
`CREATED` operation it inserts the unique dispatch intent, changes status to `PROCESSING`, and adds
the next per-operation event with message `Operation submitted`. The transaction commits before the
endpoint returns `202 Accepted`.

Submitting an operation already in a non-`CREATED` state returns its current representation with
`200 OK` and creates no new intent or event. An unknown operation returns `404 Not Found`.

Concurrent submissions serialize on the operation row. Exactly one request observes `CREATED`,
commits the intent and transition, and returns `202`; all remaining requests observe the committed
state, return `200`, and add nothing.

## Dispatch

The application lifespan owns one background dispatch worker. The worker claims one unclaimed,
due, undispatched intent in a short transaction, records its claim and incremented attempt count,
and copies the immutable operation ID, decimal amount, and currency. A claim whose timestamp exceeds
the configured lease is also eligible. The worker releases the transaction before calling
`POST {PROVIDER_URL}/payments`.

When worker loops in multiple application instances compete, `FOR UPDATE SKIP LOCKED` makes each
claim atomic and prevents a locked intent from being selected twice. Claim commit precedes provider
HTTP, so the operation row remains available to receipt handling while the response is outstanding.

The request body contains `operationId`, amount, and currency. Both `Idempotency-Key` and
`X-Correlation-ID` equal `operationId`. A response is accepted only when its HTTP status is `202`,
its body status is `ACCEPTED`, and it contains `providerPaymentId`.

Acceptance stores a consistent provider ID and marks the intent dispatched in one short
transaction. It does not change operation status or append an event. The operation remains
`PROCESSING` unless an earlier receipt already finalized it; a late response never restores
`PROCESSING` or otherwise changes a final result.

Any transport exception or response other than an accepted `202` leaves operation status unchanged.
The worker releases its claim and persists `next_attempt_at` using capped exponential backoff. For
attempt number `n`, the pre-jitter delay is `min(maxDelay, baseDelay * 2^(n-1))`; configured jitter
selects a value between `(1-jitterRatio)` and `1` times that delay. Every later attempt reconstructs
the same body and headers from immutable operation data.

The runtime timing settings are `PROVIDER_TIMEOUT_SECONDS`, `DISPATCH_POLL_INTERVAL_SECONDS`,
`DISPATCH_RETRY_BASE_DELAY_SECONDS`, `DISPATCH_RETRY_MAX_DELAY_SECONDS`,
`DISPATCH_RETRY_JITTER_RATIO`, and `DISPATCH_CLAIM_TIMEOUT_SECONDS`. Base delay may not exceed maximum
delay, claim timeout must exceed provider timeout, and every duration must be finite, positive, and
no greater than one day so persisted schedule arithmetic remains representable.

Graceful worker shutdown stops polling, cancels provider I/O, and clears the current claim with an
immediately due schedule. If the process cannot do that cleanup, a later worker reclaims the intent
after lease expiry. Claim, expiry, dispatch, and next-attempt timestamps use PostgreSQL's clock.
Startup requires no reconstruction step beyond polling persisted due work.

## Final receipt

`POST /receipts` accepts result `COMPLETED` or `REJECTED`. The receipt must identify an existing
operation and match its established `providerPaymentId`, or establish that linkage when it is
missing. A first final receipt requires `PROCESSING`; finalized operations accept the duplicate and
opposite-result behavior below. Unknown operations return `404 Not Found`, and other non-final
states or linkage mismatches return `409 Conflict`.

A valid receipt locks the operation and atomically changes it to the supplied final result and
appends the next event. The event uses the receipt message and occurrence time, with `PROCESSING` as
its prior state. The endpoint returns `204 No Content` with an empty body.

Once final, an equivalent receipt for the same operation, provider ID, and result returns `204`
without another event; differences in receipt message or occurrence time do not affect equivalence.
An opposite result with the same linkage also returns `204`, preserves the first final status, and
appends one `RECEIPT_IGNORED` audit event whose prior and resulting states are both the established
status. The audit event preserves the first ignored receipt's message and occurrence time; repeated
delivery of that opposite result adds no further audit event.

Once provider linkage exists, any receipt with a different provider ID returns `409` without state,
linkage, or event changes. PostgreSQL uniqueness permits a provider ID to belong to only one
operation; an attempted cross-operation link also returns `409` and rolls back the complete receipt.
Unknown operations return `404`, and validation rejects results outside the two final statuses before
opening the transaction.

Operation representations and event history remain governed by
[SPEC-operation-records](SPEC-operation-records.md). Dispatch and receipt observability is governed
by [SPEC-payment-observability](SPEC-payment-observability.md).
