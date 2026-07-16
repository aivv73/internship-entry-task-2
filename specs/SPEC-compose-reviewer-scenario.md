# SPEC-compose-reviewer-scenario: Real-provider Compose verification

This specification refines the packaging and reviewer-verification obligations in
[REQ-service-foundation](REQ-service-foundation.md) within the deployment boundary described by
[ARCH-payment-service](ARCH-payment-service.md).

## Compose stack

The root `compose.yaml` starts `candidate-service`, PostgreSQL 17, and the unchanged
`ghcr.io/fintech-dev-lab/internship-provider-simulator:v0.2.0` image in one Compose network. The
candidate is published on host port 8080 and receives `PROVIDER_URL` equal to
`http://provider-simulator:8081`. The provider is published on host port 8081 and receives
`CALLBACK_URL` equal to `http://candidate-service:8080/receipts`.

PostgreSQL stores its cluster in the named `candidate-data` volume. Ordinary container restart,
replacement, and `docker compose down` preserve that volume; `docker compose down --volumes` is the
documented destructive cleanup. PostgreSQL readiness probes its final TCP listener so initialization
cannot release the migration-dependent candidate against the temporary bootstrap server.

The candidate image applies every Alembic migration before starting its HTTP server. The provider
starts after candidate health succeeds. `SIMULATOR_MODE` defaults to `success` and may be supplied as
`reject` when the provider container is created to demonstrate the other final callback result.

## Reviewer scenarios

The root README provides copyable commands for clean startup, `COMPLETED` and `REJECTED` provider
callbacks, concurrent submission, interruption and candidate restart, volume-preserving container
replacement, tests, ordinary shutdown, and destructive cleanup. Provider structured logs are the
audit surface: a newly created payment has message `payment accepted` and `replay:false`; filtering
those records by operation ID demonstrates one provider payment effect.

The environment-gated Compose smoke test builds a clean isolated project, creates an operation
through the public candidate API, submits it concurrently, waits for a real simulator callback,
checks its ordered history, and requires exactly one non-replay provider audit record. It restarts
the candidate and verifies the completed operation and provider linkage remain readable. The test
removes its isolated volume on completion and runs only when `RUN_COMPOSE_SMOKE=1` because it requires
Docker, Compose, the published image, free assignment ports, and destructive ownership of its test
project.

Payment state and dispatch semantics remain governed by
[SPEC-durable-dispatch](SPEC-durable-dispatch.md); logs and candidate metrics remain governed by
[SPEC-payment-observability](SPEC-payment-observability.md).
