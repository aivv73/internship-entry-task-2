from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from payment_service.models import Operation, OperationEvent, OperationStatus
from payment_service.operations import SessionDependency, next_event_id
from payment_service.schemas import ReceiptRequest

router = APIRouter(tags=["receipts"])


@router.post("/receipts", status_code=status.HTTP_204_NO_CONTENT)
async def receive_receipt(
    receipt: ReceiptRequest,
    session: SessionDependency,
) -> Response:
    async with session.begin():
        operation = await session.scalar(
            select(Operation)
            .where(Operation.operation_id == receipt.operation_id)
            .with_for_update()
        )
        if operation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if operation.status != OperationStatus.PROCESSING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        if operation.provider_payment_id != receipt.provider_payment_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)

        event_id = await next_event_id(session, operation.operation_id)
        operation.status = OperationStatus.COMPLETED
        session.add(
            OperationEvent(
                operation_id=operation.operation_id,
                event_id=event_id,
                type=OperationStatus.COMPLETED,
                from_status=OperationStatus.PROCESSING,
                to_status=OperationStatus.COMPLETED,
                message=receipt.message,
                occurred_at=receipt.occurred_at,
            )
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
