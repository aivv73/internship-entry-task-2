# SPEC-readiness: Database-backed readiness

`GET /health` reports whether the candidate service can currently reach its authoritative
PostgreSQL store.

When a lightweight database query succeeds, the endpoint returns `200 OK` with:

```json
{"status":"ok"}
```

When connecting or executing the query fails because PostgreSQL is unavailable, the endpoint
returns `503 Service Unavailable` with:

```json
{"status":"unavailable"}
```

The probe does not mutate payment state. A running Python process alone is not sufficient for
readiness. The Docker health check calls this endpoint, and application shutdown disposes the async
database engine.

Tests may replace the database boundary for narrow HTTP response tests, but the production probe
must also be exercised against reachable and unreachable PostgreSQL.

This behavior satisfies the readiness portion of
[REQ-service-foundation](REQ-service-foundation.md) and uses the database boundary described by
[ARCH-payment-service](ARCH-payment-service.md).
