"""Factory reset (SPEC §7.7). Destructive: run manually, with a person at the plug.

    TEST_LOCAL=1 TEST_ALLOW_FACTORY_RESET=1 pipenv run pytest -m destructive --no-cov -s

After the reset, re-provision the plug with the Kasa app within five minutes.
"""

from __future__ import annotations

import pytest

from tests.conftest import ValidatingClient
from tests.hardware.conftest import env

pytestmark = pytest.mark.destructive


def test_factory_reset_and_reprovision(
    live_client: ValidatingClient, plug_mac: str
) -> None:
    if env("TEST_LOCAL") != "1" or env("TEST_ALLOW_FACTORY_RESET") != "1":
        pytest.skip("set TEST_LOCAL=1 and TEST_ALLOW_FACTORY_RESET=1")
    base = f"/v1/local/plugs/{plug_mac}"
    reset = live_client.post(f"{base}/factory-reset", json={"confirm_mac": plug_mac})
    assert reset.status_code == 202, reset.json()
    assert live_client.get(f"{base}/wait?until=offline&timeout_s=60").json()[
        "satisfied"
    ]
    print("\nRe-provision the plug with the Kasa app now (5 minutes)...")
    online = live_client.get(f"{base}/wait?until=online&timeout_s=300").json()
    assert online["satisfied"], online
    info = live_client.get(f"{base}/info").json()
    print(f"cloud_link after re-provisioning: {info.get('cloud_link')}")
