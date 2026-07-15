from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from payment_service.models import DispatchIntent, Operation, OperationEvent, OperationStatus
from payment_service.schemas import (
    CreateOperationRequest,
    OperationEventResponse,
    OperationResponse,
)

router = APIRouter(prefix="/operations", tags=["operations"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.sessions() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]


async def next_event_id(session: AsyncSession, operation_id: str) -> int:
    return await session.scalar(
        select(func.coalesce(func.max(OperationEvent.event_id), 0) + 1).where(
            OperationEvent.operation_id == operation_id
        )
    )


@router.post("", response_model=OperationResponse, status_code=status.HTTP_201_CREATED)
async def create_operation(
    request: CreateOperationRequest,
    session: SessionDependency,
) -> Operation:
    operation = Operation(
        operation_id=request.operation_id,
        amount=Decimal(request.amount),
        currency=request.currency,
        description=request.description,
        status=OperationStatus.CREATED,
    )
    event = OperationEvent(
        operation_id=request.operation_id,
        event_id=1,
        type=OperationStatus.CREATED,
        from_status=None,
        to_status=OperationStatus.CREATED,
        message="Operation created",
    )
    try:
        async with session.begin():
            session.add_all((operation, event))
    except IntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    return operation


@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    session: SessionDependency,
) -> Operation:
    operation = await session.scalar(
        select(Operation).where(Operation.operation_id == operation_id)
    )
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return operation


@router.post(
    "/{operation_id}/submit",
    response_model=OperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_operation(
    operation_id: str,
    response: Response,
    session: SessionDependency,
) -> Operation:
    async with session.begin():
        operation = await session.scalar(
            select(Operation).where(Operation.operation_id == operation_id).with_for_update()
        )
        if operation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if operation.status != OperationStatus.CREATED:
            response.status_code = status.HTTP_200_OK
            return operation

        event_id = await next_event_id(session, operation_id)
        operation.status = OperationStatus.PROCESSING
        session.add(
            DispatchIntent(
                operation_id=operation_id,
            )
        )
        session.add(
            OperationEvent(
                operation_id=operation_id,
                event_id=event_id,
                type=OperationStatus.PROCESSING,
                from_status=OperationStatus.CREATED,
                to_status=OperationStatus.PROCESSING,
                message="Operation submitted",
            )
        )
    return operation


@router.get("/{operation_id}/events", response_model=list[OperationEventResponse])
async def get_operation_events(
    operation_id: str,
    session: SessionDependency,
) -> list[OperationEvent]:
    operation_exists = await session.scalar(
        select(Operation.operation_id).where(Operation.operation_id == operation_id)
    )
    if operation_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    events = await session.scalars(
        select(OperationEvent)
        .where(OperationEvent.operation_id == operation_id)
        .order_by(OperationEvent.event_id)
    )
    return list(events)
