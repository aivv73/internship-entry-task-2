import asyncio
import logging
import math
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from payment_service.dispatcher import (
    ClaimedDispatch,
    DispatchPolicy,
    DispatchWorker,
    retry_delay,
)
from payment_service.dispatcher import (
    logger as dispatch_logger,
)
from payment_service.observability import PaymentMetrics
from payment_service.provider import PaymentRequest


@pytest.mark.parametrize(
    ("attempt_count", "random_fraction", "expected_delay"),
    [
        (1, 0.0, 1.5),
        (1, 1.0, 2.0),
        (3, 0.5, 7.0),
        (8, 0.0, 7.5),
        (8, 1.0, 10.0),
    ],
)
def test_retry_delay_is_exponential_jittered_and_bounded(
    attempt_count: int,
    random_fraction: float,
    expected_delay: float,
) -> None:
    policy = DispatchPolicy(
        poll_interval=0.25,
        retry_base_delay=2,
        retry_max_delay=10,
        retry_jitter_ratio=0.25,
        claim_timeout=30,
    )

    assert retry_delay(policy, attempt_count, random_fraction) == expected_delay


def test_retry_delay_continues_exponential_growth_beyond_attempt_63() -> None:
    policy = DispatchPolicy(
        poll_interval=0.25,
        retry_base_delay=1e-20,
        retry_max_delay=86_400,
        retry_jitter_ratio=0,
        claim_timeout=30,
    )

    assert retry_delay(policy, attempt_count=100, random_fraction=0.5) == 86_400


def test_dispatch_policy_rejects_non_finite_internal_override() -> None:
    with pytest.raises(ValueError, match="finite"):
        DispatchPolicy(
            poll_interval=math.inf,
            retry_base_delay=1,
            retry_max_delay=10,
            retry_jitter_ratio=0,
            claim_timeout=30,
        )


async def test_cancellation_during_retry_persistence_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock()
    provider.create_payment = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    worker = DispatchWorker(
        Mock(),
        provider,
        policy=DispatchPolicy(
            poll_interval=0.25,
            retry_base_delay=1,
            retry_max_delay=10,
            retry_jitter_ratio=0,
            claim_timeout=30,
        ),
        metrics=PaymentMetrics(),
    )
    claimed = ClaimedDispatch(
        operation_id="operation-cancelled-retry",
        payment=PaymentRequest(
            operation_id="operation-cancelled-retry",
            amount=Decimal("100.00"),
            currency="RUB",
        ),
        attempt_count=1,
        claimed_at=datetime.now(UTC),
        provider_payment_id=None,
    )
    retry_persistence_started = asyncio.Event()
    keep_retry_persistence_open = asyncio.Event()
    interrupted_claim_released = asyncio.Event()

    async def claim_intent() -> ClaimedDispatch:
        return claimed

    async def hold_retry_persistence(_: ClaimedDispatch) -> None:
        retry_persistence_started.set()
        await keep_retry_persistence_open.wait()

    async def release_interrupted_claim(_: ClaimedDispatch) -> None:
        interrupted_claim_released.set()

    monkeypatch.setattr(worker, "_claim_intent", claim_intent)
    monkeypatch.setattr(worker, "_schedule_retry", hold_retry_persistence)
    monkeypatch.setattr(worker, "_release_interrupted_claim", release_interrupted_claim)

    dispatch = asyncio.create_task(worker._dispatch_one())
    await asyncio.wait_for(retry_persistence_started.wait(), timeout=1)
    dispatch.cancel()

    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert interrupted_claim_released.is_set()


async def test_persistence_failure_logs_returned_provider_payment_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock()
    provider.create_payment = AsyncMock(return_value="provider-returned-conflict")
    worker = DispatchWorker(
        Mock(),
        provider,
        policy=DispatchPolicy(
            poll_interval=0.25,
            retry_base_delay=1,
            retry_max_delay=10,
            retry_jitter_ratio=0,
            claim_timeout=30,
        ),
        metrics=PaymentMetrics(),
    )
    claimed = ClaimedDispatch(
        operation_id="operation-linkage-conflict",
        payment=PaymentRequest(
            operation_id="operation-linkage-conflict",
            amount=Decimal("100.00"),
            currency="RUB",
        ),
        attempt_count=2,
        claimed_at=datetime.now(UTC),
        provider_payment_id=None,
    )
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    monkeypatch.setattr(
        worker,
        "_record_acceptance",
        AsyncMock(side_effect=RuntimeError("provider linkage conflict")),
    )
    monkeypatch.setattr(worker, "_schedule_retry", AsyncMock())
    capture_handler = CaptureHandler()
    previous_disabled = dispatch_logger.disabled
    previous_level = dispatch_logger.level
    dispatch_logger.disabled = False
    dispatch_logger.setLevel(logging.INFO)
    dispatch_logger.addHandler(capture_handler)
    try:
        await worker._deliver_claimed(claimed)
    finally:
        dispatch_logger.removeHandler(capture_handler)
        dispatch_logger.disabled = previous_disabled
        dispatch_logger.setLevel(previous_level)

    failure = next(
        record for record in records if record.getMessage() == "provider dispatch failed"
    )
    assert failure.operationId == "operation-linkage-conflict"
    assert failure.providerPaymentId == "provider-returned-conflict"
    assert failure.attempt == 2
    assert failure.outcome == "error"
