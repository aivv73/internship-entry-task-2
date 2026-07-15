import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select

from payment_service.database import Database
from payment_service.models import DispatchIntent, Operation
from payment_service.provider import PaymentRequest, ProviderClient

logger = logging.getLogger(__name__)


class DispatchWorker:
    def __init__(
        self,
        database: Database,
        provider: ProviderClient,
        *,
        poll_interval: float,
    ) -> None:
        self._database = database
        self._provider = provider
        self._poll_interval = poll_interval
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
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    async def _dispatch_one(self) -> bool:
        claimed = await self._claim_intent()
        if claimed is None:
            return False
        operation_id, payment = claimed
        try:
            provider_payment_id = await self._provider.create_payment(payment)
            await self._record_acceptance(operation_id, provider_payment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("provider dispatch failed", extra={"operationId": operation_id})
        return True

    async def _claim_intent(self) -> tuple[str, PaymentRequest] | None:
        async with self._database.sessions.begin() as session:
            intent = await session.scalar(
                select(DispatchIntent)
                .where(
                    DispatchIntent.claimed_at.is_(None),
                    DispatchIntent.dispatched_at.is_(None),
                )
                .order_by(DispatchIntent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if intent is None:
                return None
            operation = await session.get(Operation, intent.operation_id)
            if operation is None:
                raise RuntimeError("dispatch intent has no operation")
            intent.claimed_at = datetime.now(UTC)
            intent.attempt_count += 1
            payment = PaymentRequest(
                operation_id=operation.operation_id,
                amount=operation.amount,
                currency=operation.currency,
            )
            return operation.operation_id, payment

    async def _record_acceptance(self, operation_id: str, provider_payment_id: str) -> None:
        async with self._database.sessions.begin() as session:
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
            intent.dispatched_at = datetime.now(UTC)
