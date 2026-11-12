"""An emulation of TP-Link's Kasa cloud at the HTTP boundary of tplink-cloud-api.

It replaces ``requests.post`` (account API) and ``TPLinkDeviceClient._request_post``
(device passthrough) so the *real* library code runs: signing, regional lookup,
MFA, token refresh, device classes and their response parsing. Plug state comes
from a :class:`FakeWorld`; each plug answers IOT commands the way Kasa firmware does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from tplinkcloud.api_response import TPLinkApiResponse

from equipment_tplink.fakes.world import FakePlug, FakeWorld

REGION_URL = "https://n-use1-wap.tplinkcloud.com"
TAPO_REGION_URL = "https://n-use1-wap-gw.tplinkcloud.com"
TAPO_APP_TYPE = "TP-Link_Tapo_Android"
OFFLINE = -20571
NESTED_MFA_REPLY = {  # the Kasa login reply seen on the bench account (F23)
    "errorCode": "-20677",
    "errorMsg": "MFA feature enabled",
    "MFAProcessId": "4f0c",
    "supportedMFATypes": [2, 1],
    "failedAttempts": 0,
    "remainAttempts": 0,
    "lockedMinutes": 0,
}


@dataclass
class CloudServer:
    """Account and device behaviour of the emulated Kasa and Tapo clouds."""

    world: FakeWorld
    token: str = "token-1"
    tapo_token: str = "tapo-token-1"
    refresh_token: str = "refresh-1"
    token_expired: bool = False
    refresh_expired: bool = False
    list_error_code: int | None = None
    legacy_emeter: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    # -- account API (requests.post in tplinkcloud.client) -------------------------

    def requests_post(
        self, url: str, data: str, params: dict[str, Any], **_: Any
    ) -> Any:
        body = json.loads(data)
        self.requests.append({"url": url, "body": body, "params": params})
        tapo = body.get("appType") == TAPO_APP_TYPE or url.startswith(TAPO_REGION_URL)
        cloud = "tapo" if tapo else "kasa"
        if url.endswith("/api/v2/account/getAccountStatusAndUrl"):
            region = TAPO_REGION_URL if tapo else REGION_URL
            return _http({"error_code": 0, "result": {"appServerUrl": region}})
        if url.endswith("/api/v2/account/login"):
            return self._login(body, tapo)
        if url.endswith("/api/v2/account/checkMFACodeAndLogin"):
            if body.get("code") != self.world.account.mfa_code:
                return _http(
                    {"error_code": -20676, "msg": "Incorrect verification code"}
                )
            return _http({"error_code": 0, "result": self._tokens("kasa")})
        if url.endswith("/api/v2/account/refreshToken"):
            if self.refresh_expired:
                return _http({"error_code": -20655, "msg": "Refresh token expired"})
            self.token, self.tapo_token = "token-2", "tapo-token-2"
            self.token_expired = False
            return _http({"error_code": 0, "result": self._tokens(cloud)})
        if body.get("method") == "getDeviceList":
            if not params.get("token"):  # e.g. after F23: no token at all
                return _http({"error_code": -20104, "msg": "Parameter doesn't exist"})
            if self.token_expired or params.get("token") != self._token(cloud):
                return _http({"error_code": -20651, "msg": "Token expired"})
            if self.list_error_code is not None:
                return _http({"error_code": self.list_error_code, "msg": "Server busy"})
            devices = self._devices(cloud)
            return _http({"error_code": 0, "result": {"deviceList": devices}})
        raise AssertionError(f"unexpected request to {url}: {body}")

    def _login(self, body: dict[str, Any], tapo: bool) -> Any:
        account = self.world.account
        credentials = (body["cloudUserName"], body["cloudPassword"])
        if credentials != (account.username, account.password) or (
            tapo and not account.tapo_login_ok
        ):
            return _http({"error_code": -20601, "msg": "Incorrect email or password"})
        if tapo:
            return _http({"error_code": 0, "result": self._tokens("tapo")})
        if account.kasa_nested_mfa:
            return _http({"error_code": 0, "result": dict(NESTED_MFA_REPLY)})
        if account.mfa_code is not None:
            return _http(
                {
                    "error_code": -20677,
                    "msg": "MFA",
                    "result": {"mfaType": "verifyCodeLogin"},
                }
            )
        return _http({"error_code": 0, "result": self._tokens("kasa")})

    def _token(self, cloud: str) -> str:
        return self.tapo_token if cloud == "tapo" else self.token

    def _tokens(self, cloud: str) -> dict[str, str]:
        return {"token": self._token(cloud), "refreshToken": self.refresh_token}

    def _devices(self, cloud: str) -> list[dict[str, Any]]:
        return [
            {
                "deviceType": "IOT.SMARTPLUGSWITCH",
                "role": 0,
                "fwVer": plug.sw_ver,
                "appServerUrl": TAPO_REGION_URL if cloud == "tapo" else REGION_URL,
                "deviceRegion": "us-east-1",
                "deviceId": plug.device_id,
                "deviceName": "Smart Wi-Fi Plug Mini",
                "deviceHwVer": plug.hw_ver,
                "alias": plug.alias,
                "deviceMac": plug.mac.replace(":", ""),
                "oemId": "FFF22CFF774A0B89F7624BFC6F50D5DE",
                "deviceModel": plug.model,
                "hwId": "60FF6B258734EA6880E186F8C96DDC61",
                "fwId": "00000000000000000000000000000000",
                "isSameRegion": True,
                "status": 1 if self.world.online(plug) else 0,
            }
            for plug in self.world.plugs
            if plug.listed and plug.cloud == cloud
        ]

    # -- device passthrough (TPLinkDeviceClient._request_post) ---------------------

    async def device_post(
        self, client: Any, body: dict[str, Any], url_path: str = "/"
    ) -> Any:
        self.requests.append({"url": url_path, "body": body})
        cloud = client._cloud_type
        if self.token_expired or client._params.get("token") != self._token(cloud):
            return TPLinkApiResponse({"error_code": -20651, "msg": "Token expired"})
        if cloud == "tapo":  # V2 passthrough: flat body on /api/v2/common/passthrough
            assert url_path == "/api/v2/common/passthrough"
            params = body
        else:
            params = body["params"]
        plug = next(p for p in self.world.plugs if p.device_id == params["deviceId"])
        if not self.world.online(plug):
            return TPLinkApiResponse(
                {"error_code": OFFLINE, "msg": "Device is offline"}
            )
        answer = self._iot(plug, json.loads(params["requestData"]))
        return TPLinkApiResponse(
            {"error_code": 0, "result": {"responseData": json.dumps(answer)}}
        )

    def _iot(self, plug: FakePlug, request: dict[str, Any]) -> dict[str, Any]:
        """Answer an IOT request like Kasa firmware (unknown modules → err -1)."""
        answer: dict[str, Any] = {}
        for module, methods in request.items():
            if module not in ("system", "emeter", "schedule"):
                answer[module] = {"err_code": -1, "err_msg": "module not support"}
                continue
            answer[module] = {
                method: self._method(plug, module, method, args)
                for method, args in methods.items()
            }
        return answer

    def _method(
        self, plug: FakePlug, module: str, method: str, args: Any
    ) -> dict[str, Any]:
        world = self.world
        if (module, method) == ("system", "get_sysinfo"):
            return world.sysinfo(plug)
        if (module, method) == ("system", "set_relay_state"):
            world.set_relay(plug, bool(args["state"]), via_cloud=True)
            return {"err_code": 0}
        if (module, method) == ("system", "set_led_off"):
            plug.led_on = not args["off"]
            return {"err_code": 0}
        if (module, method) == ("emeter", "get_realtime"):
            power = world.power_w(plug)
            if self.legacy_emeter:  # HS110 hw v1 reports W, V, A, kWh
                return {
                    "power": power,
                    "voltage": plug.voltage_v,
                    "current": power / plug.voltage_v,
                    "total": plug.total_kwh,
                    "err_code": 0,
                }
            return {
                "power_mw": int(power * 1000),
                "voltage_mv": int(plug.voltage_v * 1000),
                "current_ma": int(power / plug.voltage_v * 1000),
                "total_wh": int(plug.total_kwh * 1000),
                "err_code": 0,
            }
        if (module, method) == ("schedule", "get_rules"):
            return {"rule_list": plug.rules, "enable": 1, "version": 2, "err_code": 0}
        return {"err_code": -2, "err_msg": "member not support"}


def _http(payload: dict[str, Any]) -> Any:
    return SimpleNamespace(
        status_code=200, json=lambda: payload, content=b"", reason="OK"
    )
