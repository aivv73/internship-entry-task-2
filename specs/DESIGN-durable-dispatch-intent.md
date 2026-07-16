# DESIGN-durable-dispatch-intent: Database-backed payment dispatch

Status: confirmed, 2026-07-15, aivv73

Submitting an operation writes a unique PostgreSQL dispatch intent in the same transaction as the
`PROCESSING` state and event. A background worker consumes intents and performs provider HTTP only
after that transaction commits.

## Rationale

An in-memory task could be lost between accepting submit and contacting the provider. Calling the
provider inside the submit transaction would keep locks across unbounded network I/O and could block
callbacks or duplicate submissions. The durable intent separates authoritative scheduling from
transport while PostgreSQL uniqueness guarantees one local intent per operation.

The provider request is reconstructed from immutable operation data. Every attempt uses
`operationId` as both `Idempotency-Key` and `X-Correlation-ID`, so the external provider can collapse
ambiguous repeated deliveries into one payment.

## Tradeoffs

Polling adds latency and worker lifecycle complexity. Claim and outcome metadata become persistent
coordination state that later retry and recovery behavior must maintain. PostgreSQL and provider
idempotency together prevent duplicate effects; neither an in-memory queue nor a long transaction is
treated as authoritative. Workers atomically select due intents with row locks and skip intents
already locked by another worker, then commit the claim before provider I/O.

The worker boundary is described by [ARCH-payment-service](ARCH-payment-service.md), and current
behavior is specified by [SPEC-durable-dispatch](SPEC-durable-dispatch.md).
