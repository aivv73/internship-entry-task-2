import math

import pytest
from pydantic import ValidationError

from payment_service.config import Settings


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_timeout_seconds": math.inf},
        {"dispatch_poll_interval_seconds": math.inf},
        {"dispatch_retry_base_delay_seconds": 86_401},
        {"dispatch_retry_max_delay_seconds": 1e300},
        {"dispatch_retry_jitter_ratio": math.nan},
        {"dispatch_claim_timeout_seconds": math.inf},
    ],
)
def test_dispatch_timing_rejects_non_finite_or_unrepresentable_values(
    overrides: dict[str, float],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_dispatch_timing_accepts_one_day_retry_bound() -> None:
    settings = Settings(
        dispatch_retry_base_delay_seconds=86_400,
        dispatch_retry_max_delay_seconds=86_400,
    )

    assert settings.dispatch_retry_max_delay_seconds == 86_400
