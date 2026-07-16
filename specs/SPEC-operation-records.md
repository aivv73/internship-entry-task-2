# SPEC-operation-records: Operation creation and inspection

This specification refines the operation-record obligations in
[REQ-service-foundation](REQ-service-foundation.md).

## Create an operation

`POST /operations` accepts camel-case JSON fields:

- `operationId`: required string; surrounding whitespace is removed and the result must be non-empty.
- `amount`: required positive base-10 string containing digits and optionally one decimal point with
  one or two following digits. Numeric JSON values, signs, exponent notation, and more than two
  fractional digits are invalid.
- `currency`: required literal `RUB`.
- `description`: optional string or null.

Validation occurs before persistence. Valid values are stored using PostgreSQL decimal data. The
operation and its initial event are inserted in one transaction:

- operation status: `CREATED`
- provider payment ID: null
- event ID: 1
- event type: `CREATED`
- prior status: null
- resulting status: `CREATED`
- message: `Operation created`
- occurrence time: database-generated timezone-aware timestamp

Success returns `201 Created` and the public operation representation. The amount is serialized as
a decimal string. A uniqueness conflict for `operationId` rolls back the complete transaction and
returns `409 Conflict`.

## Read an operation

`GET /operations/{id}` returns `200 OK` and the persisted operation representation containing
`operationId`, amount, currency, description, status, and `providerPaymentId`. An unknown ID returns
`404 Not Found`.

## Read event history

`GET /operations/{id}/events` returns `200 OK` and a JSON array ordered by ascending `eventId`.
Events expose `eventId`, `type`, `fromStatus`, `toStatus`, `message`, and `occurredAt`. An unknown
operation returns `404 Not Found` rather than an empty history.

Event identity is the pair of operation ID and event ID. The first event is 1; later state changes
must allocate increasing IDs while preserving transactionality. Submission, provider dispatch,
callback finalization, and ignored-receipt audits are specified by
[SPEC-durable-dispatch](SPEC-durable-dispatch.md).

The persistence and HTTP boundaries are described by
[ARCH-payment-service](ARCH-payment-service.md), and the selected platform is recorded in
[DESIGN-async-python-postgresql](DESIGN-async-python-postgresql.md).
