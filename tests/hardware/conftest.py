"""Opt-in tests against a real plug and TP-Link account.

Run with ``make hardware`` (``pytest -m hardware --no-cov``). Settings come from the
environment and ``./.env``; a test skips unless everything it needs is set:

- cloud: ``TPLINK_USERNAME``, ``TPLINK_PASSWORD``, ``TEST_PLUG_MAC``
- local: ``TEST_PLUG_MAC`` and ``TEST_LOCAL=1``

A lamp (or any load above a few watts) must be plugged into the test plug.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from equipment_tplink.app import create_app
from equipment_tplink.config import load_settings
from equipment_tplink.core.identity import normalize_mac
from tests.conftest import OpenApiValidator, ValidatingClient


def env(name: str) -> str | None:
    """A setting from the environment, falling back to ./.env."""
    return os.environ.get(name) or dotenv_values(".env").get(name) or None


@pytest.fixture(scope="module")
def plug_mac() -> str:
    mac = normalize_mac(env("TEST_PLUG_MAC"))
    if mac is None:
        pytest.skip("set TEST_PLUG_MAC to the test plug's MAC")
    return mac


@pytest.fixture(scope="module")
def live_client(validator: OpenApiValidator) -> Iterator[ValidatingClient]:
    """The real service (real libraries, real clock) behind a validating client."""
    with TestClient(create_app(load_settings())) as test_client:
        yield ValidatingClient(test_client, validator)


def expect(response: Any, status: int = 200) -> dict[str, Any]:
    """Assert the HTTP status, showing the service's error body when it differs."""
    body: dict[str, Any] = response.json()
    assert response.status_code == status, f"HTTP {response.status_code}: {body}"
    return body


def settled_energy(
    client: ValidatingClient, base: str, *, loaded: bool, timeout_s: float = 15.0
) -> dict[str, Any]:
    """Poll the energy reading until power and current both match the relay.

    The plug's power figure lags the relay by several seconds and its fields update
    independently (FINDINGS F24), so a single reading right after a switch can be
    stale. Returns the last reading, settled or not, for the caller to assert on.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        reading = expect(client.get(f"{base}/energy"))
        power, current = reading["power_w"], reading["current_a"] or 0.0
        if loaded:
            settled = power > 1.0 and current > 0.005
        else:
            settled = power < 1.0 and current < 0.005
        if settled or time.monotonic() >= deadline:
            return reading
        time.sleep(1)
