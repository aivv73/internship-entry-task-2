import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[2]
COMPOSE_PROJECT = "payment-service-smoke"
BASE_URL = "http://127.0.0.1:8080"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_COMPOSE_SMOKE") != "1",
        reason="RUN_COMPOSE_SMOKE=1 is required for the real-provider Compose smoke test",
    ),
]


def _compose(
    *arguments: str, check: bool = True, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("COMPOSE_FILE", None)
    environment.pop("COMPOSE_PROJECT_NAME", None)
    environment["SIMULATOR_MODE"] = "success"
    return subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(ROOT / "compose.yaml"),
            "--project-name",
            COMPOSE_PROJECT,
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _request(
    method: str, path: str, body: dict[str, str] | None = None
) -> tuple[int, dict[str, object] | list[object] | None]:
    encoded = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=encoded,
        headers={"Content-Type": "application/json"} if encoded else {},
        method=method,
    )
    try:
        with HTTP.open(request, timeout=10) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        payload = error.read()
        return error.code, json.loads(payload) if payload else None


def _wait_for_status(operation_id: str, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status, operation = _request("GET", f"/operations/{operation_id}")
        except OSError, urllib.error.URLError:
            time.sleep(0.25)
            continue
        if status == 200 and operation["status"] == expected:
            return operation
        time.sleep(0.25)
    pytest.fail(f"operation {operation_id} did not reach {expected}")


@pytest.fixture(scope="module", autouse=True)
def compose_stack(request: pytest.FixtureRequest) -> Iterator[None]:
    if _compose("version", check=False, timeout=30).returncode != 0:
        pytest.skip("Docker Compose is unavailable")

    _compose("down", "--volumes", "--remove-orphans", timeout=120)
    setup_failed = False
    try:
        try:
            _compose(
                "up",
                "--detach",
                "--build",
                "--wait",
                "--wait-timeout",
                "180",
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            setup_failed = True
            try:
                logs = _compose("logs", "--no-color", check=False, timeout=30).stdout
            except subprocess.TimeoutExpired:
                logs = "Compose logs also timed out"
            pytest.fail(f"Compose startup failed:\n{error}\n{logs}")
        yield
    finally:
        try:
            cleanup = _compose(
                "down",
                "--volumes",
                "--remove-orphans",
                check=False,
                timeout=120,
            )
            cleanup_error = (
                f"exit code: {cleanup.returncode}\n"
                f"stdout:\n{cleanup.stdout}\n"
                f"stderr:\n{cleanup.stderr}"
                if cleanup.returncode
                else ""
            )
        except subprocess.TimeoutExpired as error:
            cleanup_error = str(error)
        if cleanup_error:
            message = f"Compose smoke cleanup failed: {cleanup_error}"
            call_failed = getattr(request.node, "_compose_call_failed", False)
            if setup_failed or call_failed:
                warnings.warn(message, RuntimeWarning, stacklevel=1)
            else:
                pytest.fail(message)


def test_real_provider_completes_one_payment_and_candidate_recovers() -> None:
    operation_id = f"compose-smoke-{uuid4()}"
    status, created = _request(
        "POST",
        "/operations",
        {
            "operationId": operation_id,
            "amount": "1000.00",
            "currency": "RUB",
            "description": "Compose smoke payment",
        },
    )
    assert status == 201
    assert created["status"] == "CREATED"

    with ThreadPoolExecutor(max_workers=8) as executor:
        submissions = list(
            executor.map(
                lambda _: _request("POST", f"/operations/{operation_id}/submit"),
                range(8),
            )
        )
    assert [status for status, _ in submissions].count(202) == 1
    assert all(status in {200, 202} for status, _ in submissions)

    completed = _wait_for_status(operation_id, "COMPLETED")
    assert completed["providerPaymentId"]
    status, events = _request("GET", f"/operations/{operation_id}/events")
    assert status == 200
    assert [event["type"] for event in events] == ["CREATED", "PROCESSING", "COMPLETED"]

    audit = _compose("logs", "--no-color", "provider-simulator").stdout
    records = [json.loads(line[line.index("{") :]) for line in audit.splitlines() if "{" in line]
    accepted = [
        record
        for record in records
        if record.get("msg") == "payment accepted"
        and record.get("operationId") == operation_id
        and record.get("replay") is False
    ]
    assert len(accepted) == 1

    _compose("restart", "candidate-service")
    persisted = _wait_for_status(operation_id, "COMPLETED")
    assert persisted["providerPaymentId"] == completed["providerPaymentId"]
