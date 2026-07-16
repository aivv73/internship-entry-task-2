from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from payment_service.models import Operation, OperationEvent, OperationStatus
from payment_service.operations import SessionDependency, next_event_id
from payment_service.schemas import ReceiptRequest

router = APIRouter(tags=["receipts"])
IGNORED_RECEIPT_EVENT_TYPE = "RECEIPT_IGNORED"


@router.post("/receipts", status_code=status.HTTP_204_NO_CONTENT)
async def receive_receipt(
    receipt: ReceiptRequest,
    session: SessionDependency,
) -> Response:
    try:
        async with session.begin():
            operation = await session.scalar(
                select(Operation)
                .where(Operation.operation_id == receipt.operation_id)
                .with_for_update()
            )
            if operation is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            if operation.provider_payment_id not in (None, receipt.provider_payment_id):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT)
            if operation.status not in (
                OperationStatus.PROCESSING,
                OperationStatus.COMPLETED,
                OperationStatus.REJECTED,
            ):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT)

            operation.provider_payment_id = receipt.provider_payment_id
            if operation.status == OperationStatus.PROCESSING:
                event_id = await next_event_id(session, operation.operation_id)
                operation.status = receipt.result
                session.add(
                    OperationEvent(
                        operation_id=operation.operation_id,
                        event_id=event_id,
                        type=receipt.result,
                        from_status=OperationStatus.PROCESSING,
                        to_status=receipt.result,
                        message=receipt.message,
                        occurred_at=receipt.occurred_at,
                    )
                )
            elif operation.status != receipt.result:
                ignored_event_id = await session.scalar(
                    select(OperationEvent.event_id)
                    .where(
                        OperationEvent.operation_id == operation.operation_id,
                        OperationEvent.type == IGNORED_RECEIPT_EVENT_TYPE,
                    )
                    .limit(1)
                )
                if ignored_event_id is None:
                    event_id = await next_event_id(session, operation.operation_id)
                    session.add(
                        OperationEvent(
                            operation_id=operation.operation_id,
                            event_id=event_id,
                            type=IGNORED_RECEIPT_EVENT_TYPE,
                            from_status=operation.status,
                            to_status=operation.status,
                            message=receipt.message,
                            occurred_at=receipt.occurred_at,
                        )
                    )
    except IntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
