import asyncio
import logging
import math
import random
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, or_, select

from payment_service.database import Database
from payment_service.models import DispatchIntent, Operation
from payment_service.observability import PaymentMetrics, safely_log, safely_observe
from payment_service.provider import PaymentRequest, ProviderClient

logger = logging.getLogger(__name__)
MAX_TIMING_SECONDS = 86_400


@dataclass(frozen=True)
class DispatchPolicy:
    poll_interval: float
    retry_base_delay: float
    retry_max_delay: float
    retry_jitter_ratio: float
    claim_timeout: float

    def __post_init__(self) -> None:
        timing_values = (
            self.poll_interval,
            self.retry_base_delay,
            self.retry_max_delay,
            self.claim_timeout,
        )
        if any(
            not math.isfinite(value) or not 0 < value <= MAX_TIMING_SECONDS
            for value in timing_values
        ):
            raise ValueError("dispatch timing values must be finite and at most one day")
        if self.retry_base_delay > self.retry_max_delay:
            raise ValueError("retry base delay cannot exceed maximum delay")
        if not math.isfinite(self.retry_jitter_ratio) or not 0 <= self.retry_jitter_ratio <= 1:
            raise ValueError("retry jitter ratio must be between zero and one")


@dataclass
class ClaimedDispatch:
    operation_id: str
    payment: PaymentRequest
    attempt_count: int
    claimed_at: datetime
    provider_payment_id: str | None


def retry_delay(policy: DispatchPolicy, attempt_count: int, random_fraction: float) -> float:
    exponent = max(attempt_count - 1, 0)
    try:
        uncapped_delay = math.ldexp(policy.retry_base_delay, exponent)
    except OverflowError:
        uncapped_delay = policy.retry_max_delay
    exponential_delay = min(policy.retry_max_delay, uncapped_delay)
    jitter_multiplier = 1 - policy.retry_jitter_ratio * (1 - random_fraction)
    return exponential_delay * jitter_multiplier


class DispatchWorker:
    def __init__(
        self,
        database: Database,
        provider: ProviderClient,
        *,
        policy: DispatchPolicy,
        metrics: PaymentMetrics,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        self._database = database
        self._provider = provider
        self._policy = policy
        self._metrics = metrics
        self._random_fraction = random_fraction
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("dispatch worker already started")
        self._task = asyncio.create_task(self._run(), name="payment-dispatch-worker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            found_work = await self._dispatch_one()
            if found_work:
                continue
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._policy.poll_interval)
            except TimeoutError:
                pass

    async def _dispatch_one(self) -> bool:
        claimed = await self._claim_intent()
        if claimed is None:
            return False
        safely_observe(self._metrics.provider_attempts.inc)
        if claimed.attempt_count > 1:
            safely_observe(self._metrics.provider_retries.inc)
        safely_log(
            logger,
            logging.INFO,
            "provider dispatch started",
            operationId=claimed.operation_id,
            providerPaymentId=claimed.provider_payment_id,
            attempt=claimed.attempt_count,
            outcome="started",
        )
        try:
            await self._deliver_claimed(claimed)
        except asyncio.CancelledError:
            safely_observe(
                lambda: self._metrics.dispatch_outcomes.labels(outcome="cancelled").inc()
            )
            safely_log(
                logger,
                logging.INFO,
                "provider dispatch cancelled",
                operationId=claimed.operation_id,
                providerPaymentId=claimed.provider_payment_id,
                attempt=claimed.attempt_count,
                outcome="cancelled",
            )
            try:
                await asyncio.shield(self._release_interrupted_claim(claimed))
            except Exception:
                safely_log(
                    logger,
                    logging.ERROR,
                    "failed to release interrupted dispatch",
                    exc_info=True,
                    operationId=claimed.operation_id,
                    providerPaymentId=claimed.provider_payment_id,
                    attempt=claimed.attempt_count,
                    outcome="error",
                )
            raise
        return True

    async def _deliver_claimed(self, claimed: ClaimedDispatch) -> None:
        try:
            provider_payment_id = await self._provider.create_payment(claimed.payment)
            claimed.provider_payment_id = provider_payment_id
            await self._record_acceptance(claimed.operation_id, provider_payment_id)
            safely_observe(lambda: self._metrics.dispatch_outcomes.labels(outcome="accepted").inc())
            safely_log(
                logger,
                logging.INFO,
                "provider dispatch accepted",
                operationId=claimed.operation_id,
                providerPaymentId=provider_payment_id,
                attempt=claimed.attempt_count,
                outcome="accepted",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            outcome = dispatch_failure_outcome(error)
            safely_observe(lambda: self._metrics.dispatch_outcomes.labels(outcome=outcome).inc())
            safely_log(
                logger,
                logging.ERROR,
                "provider dispatch failed",
                exc_info=True,
                operationId=claimed.operation_id,
                providerPaymentId=claimed.provider_payment_id,
                attempt=claimed.attempt_count,
                outcome=outcome,
            )
            try:
                await self._schedule_retry(claimed)
            except Exception:
                safely_log(
                    logger,
                    logging.ERROR,
                    "failed to schedule dispatch retry",
                    exc_info=True,
                    operationId=claimed.operation_id,
                    providerPaymentId=claimed.provider_payment_id,
                    attempt=claimed.attempt_count,
                    outcome="error",
                )

    async def _claim_intent(self) -> ClaimedDispatch | None:
        async with self._database.sessions.begin() as session:
            now = await session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database did not provide current time")
            stale_before = now - timedelta(seconds=self._policy.claim_timeout)
            intent = await session.scalar(
                select(DispatchIntent)
                .where(
                    DispatchIntent.dispatched_at.is_(None),
                    DispatchIntent.next_attempt_at <= now,
                    or_(
                        DispatchIntent.claimed_at.is_(None),
                        DispatchIntent.claimed_at <= stale_before,
                    ),
                )
                .order_by(DispatchIntent.next_attempt_at, DispatchIntent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if intent is None:
                return None
            operation = await session.get(Operation, intent.operation_id)
            if operation is None:
                raise RuntimeError("dispatch intent has no operation")
            intent.claimed_at = now
            intent.attempt_count += 1
            payment = PaymentRequest(
                operation_id=operation.operation_id,
                amount=operation.amount,
                currency=operation.currency,
            )
            return ClaimedDispatch(
                operation_id=operation.operation_id,
                payment=payment,
                attempt_count=intent.attempt_count,
                claimed_at=now,
                provider_payment_id=operation.provider_payment_id,
            )

    async def _schedule_retry(self, claimed: ClaimedDispatch) -> None:
        delay = retry_delay(self._policy, claimed.attempt_count, self._random_fraction())
        await self._release_claim(claimed, delay_seconds=delay)

    async def _release_interrupted_claim(self, claimed: ClaimedDispatch) -> None:
        await self._release_claim(claimed, delay_seconds=0)

    async def _release_claim(
        self,
        claimed: ClaimedDispatch,
        *,
        delay_seconds: float,
    ) -> None:
        async with self._database.sessions.begin() as session:
            now = await session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database did not provide current time")
            intent = await session.scalar(
                select(DispatchIntent)
                .where(DispatchIntent.operation_id == claimed.operation_id)
                .with_for_update()
            )
            if (
                intent is None
                or intent.dispatched_at is not None
                or intent.claimed_at != claimed.claimed_at
            ):
                return
            intent.claimed_at = None
            intent.next_attempt_at = now + timedelta(seconds=delay_seconds)

    async def _record_acceptance(self, operation_id: str, provider_payment_id: str) -> None:
        async with self._database.sessions.begin() as session:
            now = await session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database did not provide current time")
            operation = await session.scalar(
                select(Operation).where(Operation.operation_id == operation_id).with_for_update()
            )
            intent = await session.scalar(
                select(DispatchIntent)
                .where(DispatchIntent.operation_id == operation_id)
                .with_for_update()
            )
            if operation is None or intent is None:
                raise RuntimeError("accepted dispatch no longer exists")
            if operation.provider_payment_id not in (None, provider_payment_id):
                raise RuntimeError("provider payment ID does not match operation")
            operation.provider_payment_id = provider_payment_id
            intent.dispatched_at = now


def dispatch_failure_outcome(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 503:
        return "unavailable"
    if isinstance(error, httpx.TransportError):
        return "transport_error"
    return "error"
