from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from payment_service.models import Operation, OperationEvent, OperationStatus
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
