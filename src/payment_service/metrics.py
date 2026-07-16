from fastapi import APIRouter, Request
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import func, select
from starlette.responses import Response

from payment_service.models import Operation, OperationStatus
from payment_service.observability import PaymentMetrics, safely_observe

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    payment_metrics: PaymentMetrics = request.app.state.payment_metrics
    async with request.app.state.database.sessions() as session:
        rows = await session.execute(
            select(Operation.status, func.count())
            .where(Operation.status.in_((OperationStatus.CREATED, OperationStatus.PROCESSING)))
            .group_by(Operation.status)
        )
    counts = dict(rows.tuples().all())
    for status in (OperationStatus.CREATED, OperationStatus.PROCESSING):
        safely_observe(
            lambda status=status: payment_metrics.unfinished_operations.labels(status=status).set(
                counts.get(status, 0)
            )
        )
    return Response(
        content=payment_metrics.render(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
