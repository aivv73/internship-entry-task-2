import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _compose_config() -> dict[str, object]:
    environment = os.environ.copy()
    environment.pop("COMPOSE_FILE", None)
    environment.pop("COMPOSE_PROJECT_NAME", None)
    available = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if available.returncode != 0:
        pytest.skip("Docker Compose is required to validate compose.yaml")

    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(ROOT / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(rendered.stdout)


def test_compose_wires_candidate_postgres_and_real_provider() -> None:
    config = _compose_config()
    services = config["services"]

    assert set(services) == {"candidate-service", "postgres", "provider-simulator"}
    candidate = services["candidate-service"]
    provider = services["provider-simulator"]
    postgres = services["postgres"]

    assert candidate["environment"]["PROVIDER_URL"] == "http://provider-simulator:8081"
    assert candidate["ports"][0]["published"] == "8080"
    assert provider["image"] == ("ghcr.io/fintech-dev-lab/internship-provider-simulator:v0.2.0")
    assert provider["environment"]["CALLBACK_URL"] == ("http://candidate-service:8080/receipts")
    assert provider["ports"][0]["published"] == "8081"
    assert postgres["volumes"][0]["type"] == "volume"
    assert postgres["volumes"][0]["source"].endswith("candidate-data")
    assert "-h 127.0.0.1" in postgres["healthcheck"]["test"][-1]
