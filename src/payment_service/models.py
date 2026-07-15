from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OperationStatus(StrEnum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_operations_positive_amount"),
        CheckConstraint("currency = 'RUB'", name="ck_operations_rub_currency"),
        CheckConstraint(
            "status IN ('CREATED', 'PROCESSING', 'COMPLETED', 'REJECTED')",
            name="ck_operations_status",
        ),
        UniqueConstraint("provider_payment_id", name="uq_operations_provider_payment_id"),
    )

    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric())
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=OperationStatus.CREATED)
    provider_payment_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list[OperationEvent]] = relationship(
        back_populates="operation", cascade="all, delete-orphan"
    )
    dispatch_intent: Mapped[DispatchIntent | None] = relationship(
        back_populates="operation", cascade="all, delete-orphan"
    )


class OperationEvent(Base):
    __tablename__ = "operation_events"

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    operation: Mapped[Operation] = relationship(back_populates="events")


class DispatchIntent(Base):
    __tablename__ = "dispatch_intents"

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="CASCADE"), primary_key=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    operation: Mapped[Operation] = relationship(back_populates="dispatch_intent")
