import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_serializer, field_validator
from pydantic.alias_generators import to_camel

OperationId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateOperationRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    operation_id: OperationId
    amount: str
    currency: Literal["RUB"]
    description: str | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        if re.fullmatch(r"\d+(?:\.\d{1,2})?", value) is None or Decimal(value) <= 0:
            raise ValueError("amount must be a positive decimal string with at most two decimals")
        return value


class OperationResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, from_attributes=True, populate_by_name=True)

    operation_id: str
    amount: Decimal
    currency: str
    description: str | None
    status: str
    provider_payment_id: str | None

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value, "f")


class OperationEventResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, from_attributes=True, populate_by_name=True)

    event_id: int
    type: str
    from_status: str | None
    to_status: str
    message: str
    occurred_at: datetime
