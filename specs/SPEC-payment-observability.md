# SPEC-payment-observability: Correlated logs and payment metrics

This specification refines the observability obligations in
[REQ-service-foundation](REQ-service-foundation.md) within the boundaries described by
[ARCH-payment-service](ARCH-payment-service.md).

## Structured logs

Payment-processing logs are newline-delimited JSON objects containing timestamp, level, logger, and
message. Relevant dispatch records include `operationId`, available `providerPaymentId`, `attempt`,
and a bounded outcome, including `started` for an initiated delivery. Once the provider returns an
ID, that value remains available to acceptance-persistence failure and cancellation logs. Receipt
records include `operationId`, supplied `providerPaymentId`, result, and outcome. Operation ID is the
correlation value shared by provider request headers and callback handling.

Logging covers dispatch start, acceptance, failure, cancellation, retry-persistence failure, receipt
finalization, duplicate delivery, ignored opposite results, and receipt conflicts. Logging backend or
formatter exceptions are swallowed at the observability boundary and cannot alter control flow.

## Prometheus endpoint

`GET /metrics` returns the Prometheus text exposition content type and these metric families:

- `payment_unfinished_operations{status}`: gauge derived at scrape time from PostgreSQL for bounded
  statuses `CREATED` and `PROCESSING`.
- `payment_provider_attempts_total`: every claimed provider delivery.
- `payment_provider_retries_total`: claimed deliveries whose durable attempt number exceeds one.
- `payment_dispatch_outcomes_total{outcome}`: bounded outcomes `accepted`, `unavailable`,
  `transport_error`, `error`, and `cancelled`.
- `payment_receipt_outcomes_total{outcome}`: bounded outcomes `finalized`, `duplicate`,
  `ignored_opposite`, `provider_id_conflict`, `state_conflict`, and `unknown_operation`.

Counters are process-local Prometheus observations and may reset with an application instance. The
unfinished gauge is reconstructed from authoritative PostgreSQL state on every scrape. No metric
label contains `operationId`, `providerPaymentId`, or another unbounded identifier.

Metric update failures are swallowed and do not participate in payment transactions. A scrape may
fail if its own database query or exposition backend fails, but that failure cannot change payment
state or stop worker and receipt processing.
