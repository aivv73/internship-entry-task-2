from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


@dataclass(frozen=True)
class PaymentRequest:
    operation_id: str
    amount: Decimal
    currency: str


class ProviderAccepted(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    provider_payment_id: str
    status: str


class ProviderClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def create_payment(self, payment: PaymentRequest) -> str:
        response = await self._http_client.post(
            "/payments",
            headers={
                "Idempotency-Key": payment.operation_id,
                "X-Correlation-ID": payment.operation_id,
            },
            json={
                "operationId": payment.operation_id,
                "amount": format(payment.amount, "f"),
                "currency": payment.currency,
            },
        )
        if response.status_code != httpx.codes.ACCEPTED:
            raise httpx.HTTPStatusError(
                "provider did not accept payment", request=response.request, response=response
            )
        accepted = ProviderAccepted.model_validate(response.json())
        if accepted.status != "ACCEPTED":
            raise ValueError("provider response status must be ACCEPTED")
        return accepted.provider_payment_id

    async def close(self) -> None:
        await self._http_client.aclose()
