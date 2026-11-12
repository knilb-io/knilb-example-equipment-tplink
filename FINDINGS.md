# Findings

Where the TP-Link libraries or devices differ from their documentation or from reasonable expectations. Each entry is a candidate Knilb test case. Status values:

- **open**: an upstream bug, not yet fixed.
- **fixed upstream**: fixed in a library release that the project now requires.
- **limitation**: the service lives with it and surfaces it (README "Known library limitations").
- **handled**: an integration choice made through the library's public API.

The project never patches or works around the libraries (SPEC §2).

Add entries as `F<n>`. Never renumber them: SPEC.md and PRs refer to them by number.

## F1: python-kasa selects the wrong KLAP hash for IOT plugs on login version 2

- **Library / device:** python-kasa 0.10.2 (fixed in 0.11.0.1) · KP115(US) hw 1.0, `IOT.SMARTPLUGSWITCH`, KLAP, login version 2
- **Expected:** a plug that advertises login version 2 is authenticated with the v2 hash, `sha256(sha1(user) + sha1(pass))`.
- **Observed:** `get_protocol()` maps every `IOT.KLAP` device to `KlapTransport`, which uses the v1 hash `md5(md5(user) + md5(pass))`. Authentication fails with "Device response did not match our challenge … check that your e-mail and password (both case-sensitive) are correct". The message points at the credentials, not the transport, and upstream reports show users factory-resetting hardware because of it.
- **Evidence:** reproduced on the bench plug on 2026-09-22, with credentials passed as `KASA_USERNAME`/`KASA_PASSWORD` and as `--username`/`--password`. Upstream: python-kasa#1648, #1740 (a KP115; forcing `KlapTransportV2` authenticates), #1754.
- **Status:** fixed upstream in python-kasa 0.11.0.1 by python-kasa#1731, which makes the device factory pick the v2 transport for IOT devices on login version 2. The project requires python-kasa ≥ 0.12.0. Confirmed on the bench plug (firmware 1.1.1 Build 250908) on 2026-11-10 with python-kasa 0.12.0: discovery, reads, relay commands, a settings round trip and reboot recovery all authenticate over KLAP login version 2, with no transport forced. On 0.10.2 the service reported `device_auth_failed` together with `encrypt_type` and `login_version` (F22).

## F2: identical discovery flags, different auth hash

- **Library / device:** python-kasa 0.10.2 · HS103(US) hw 5.0 (reported upstream)
- **Expected:** devices that advertise the same `new_klap` and `lv` values use the same auth scheme.
- **Observed (reported):** two HS103s from the same box, both advertising `new_klap: 1, lv: 2`, needed different hashes depending on when each was paired. Re-pairing the older one in the Kasa app aligned them. Discovery exposes nothing that tells them apart.
- **Evidence:** a comment on the python-kasa#1731 thread. Not yet verified on our bench.
- **Status:** open. Not seen on the bench KP115, which authenticates with python-kasa 0.12.0 (F1). A candidate factory-reset test: record the discovery flags and the auth outcome before the reset and after re-pairing.

## F3: `kasa` CLI leaks its HTTP session after an auth failure

- **Library / device:** python-kasa 0.10.2 CLI
- **Expected:** a failed command exits cleanly.
- **Observed:** `kasa --host … on` logs "Unclosed client session" and "Unclosed connector" after the auth error.
- **Evidence:** bench terminal, 2026-09-22.
- **Status:** open, not reported. Cosmetic.

## F4: tplink-cloud-api blocks the event loop

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** an async API doesn't block the event loop.
- **Observed:** `TPLinkDeviceManager.login` and `get_devices` make synchronous `requests` calls (15 s timeout). `get_devices` is `async`, but it blocks inside.
- **Evidence:** `tplinkcloud/client.py`, `tplinkcloud/device_manager.py`.
- **Status:** handled. Both calls run in a worker thread. A timed-out thread finishes in the background (SPEC §7.3).

## F5: tplink-cloud-api completes MFA only through a callback inside one blocking login

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a server can start a login, receive an MFA challenge, and complete it in a later request.
- **Observed:** MFA is supported only by `mfa_callback`, which is called synchronously in the middle of `login()`. The only way to complete a pending challenge later is the private `_verify_mfa`.
- **Evidence:** `tplinkcloud/client.py`.
- **Status:** handled with the public callback. The login runs in a worker thread, and its callback waits (up to 300 s) for the code to arrive in `POST /v1/cloud/session` (SPEC §7.6).

## F6: tplink-cloud-api uses a new terminal ID on every process start

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a stable client identity across restarts.
- **Observed:** `term_id` defaults to a new `uuid4` per instance.
- **Evidence:** `tplinkcloud/client.py`.
- **Status:** handled. The service passes `term_id`, a uuid5 derived from the host name and username, through the public constructor parameter.

## F7: tplink-cloud-api enables Tapo by default and hides its failures

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** optional clouds are opt-in, and their failures are visible.
- **Observed:** `include_tapo` defaults to `True`, and a failed Tapo login is swallowed.
- **Evidence:** `tplinkcloud/device_manager.py`.
- **Status:** handled. The service first passed `include_tapo=False`. Since F23 it passes `include_tapo=True`, because the bench account lists its KP115 only in the Tapo cloud. It checks the public `get_token()` and `get_tapo_token()` so a swallowed failure can't pass for a login.

## F8: tplink-cloud-api's device cache never expires

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a device cache expires.
- **Observed:** with `cache_devices=True`, the first device list is kept for the life of the manager.
- **Evidence:** `tplinkcloud/device_manager.py`.
- **Status:** handled. The service passes `cache_devices=False` and keeps its own 60 s cache.

## F9: tplink-cloud-api device calls can wait 10 minutes

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** device calls are bounded by a reasonable timeout.
- **Observed:** passthrough requests use `aiohttp.ClientTimeout(total=600)`.
- **Evidence:** `tplinkcloud/device_client.py`.
- **Status:** handled. Every call runs under the service's own timeout.

## F10: tplink-cloud-api has no public reboot, factory reset or rename

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** the operations the Kasa app offers through the cloud.
- **Observed:** device classes expose power, sysinfo, LED, schedules and usage, but not reboot, reset or alias. These are reachable only through the private `_pass_through_request`.
- **Evidence:** `tplinkcloud/device.py`.
- **Status:** limitation. There are no cloud reboot or factory-reset operations, and a cloud settings update with `alias` returns `501 unsupported`.

## F11: tplink-cloud-api discards cloud error codes on device calls

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a failed device call reports why (device offline, token expired, …).
- **Observed:** `TPLinkDeviceClient.pass_through_request` returns `None` on any non-zero `error_code` and drops the code. `TPLinkDeviceOfflineError` is defined but never raised. Device methods then return `None`, and `KP115.get_sys_info` also prints to stdout.
- **Evidence:** `tplinkcloud/device_client.py:99-107`, `tplinkcloud/exceptions.py`.
- **Status:** limitation. A `None` result becomes `device_offline` if the account list shows the plug offline, otherwise `cloud_error`. Neither carries TP-Link's code.

## F12: tplink-cloud-api reports a failed device list as an empty account

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a failed device-list call raises.
- **Observed:** `get_device_info_list` returns `[]` for any non-zero `error_code` except `-20651`.
- **Evidence:** `tplinkcloud/client.py:364-382`.
- **Status:** limitation. A cloud failure can surface as `404 plug_not_found` or as an empty `listCloudPlugs`.

## F13: tplink-cloud-api refreshes tokens only while listing devices

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** an expired token is refreshed on whichever call hits it.
- **Observed:** the refresh happens only in `get_devices`. Device objects keep the token they were created with, and an expired token on a device call just returns `None` (F11).
- **Evidence:** `tplinkcloud/device_manager.py:150-184`, `tplinkcloud/device_client.py`.
- **Status:** limitation. After the token expires, cloud device calls fail with `cloud_error` until the next device-list refresh (at most 60 s later, or immediately with `listCloudPlugs?refresh=true`).

## F14: tplink-cloud-api `set_led_state` payload looks mis-nested

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** the Kasa IOT command `{"system": {"set_led_off": {"off": n}}}`.
- **Observed:** it sends `{"set_led_off": {"off": n}}` without the `system` wrapper.
- **Evidence:** `tplinkcloud/device.py:131-134`. The library then reads `response["set_led_off"]["off"]`, which no Kasa reply contains, so the call returns `None` even if a device were to accept the command. `tests/conformance/test_cloud_library.py::test_f14_led_change_returns_no_result` pins this against the real library code. Confirmed on the bench KP115 on 2026-09-23: the call returned `502 cloud_error` and the LED state didn't change (`tests/hardware/test_cloud_hardware.py::test_f14_led_change_through_the_library`).
- **Status:** open, library bug. `updateCloudPlugSettings {led_on}` always returns `502 cloud_error`.

## F15: tplink-cloud-api doesn't convert units for legacy emeter fields

- **Library / device:** tplink-cloud-api 5.2.1 · HS110 hw v1
- **Expected:** `CurrentPower.power_mw` is always in milliwatts.
- **Observed:** when the plug reports legacy keys (`power` in W, `voltage` in V, `current` in A, `total` in kWh), the library copies them into the `*_mw`/`*_mv`/`*_ma`/`*_wh` fields without converting.
- **Evidence:** `tplinkcloud/emeter_device.py:7-27`.
- **Status:** limitation. Cloud energy readings from HS110 v1 plugs are 1000× too small. KP115, KP125 and HS110 v2 report the newer keys and are unaffected.

## F16: tplink-cloud-api has no public way to restore a session

- **Library / device:** tplink-cloud-api 5.2.1
- **Expected:** a saved session can be restored after a restart.
- **Observed:** tokens have public getters and setters, but the regional server URL chosen at login lives only on the private `_kasa_api.host`.
- **Evidence:** `tplinkcloud/device_manager.py`, `tplinkcloud/client.py:256`.
- **Status:** limitation. Nothing is persisted. Each start logs in again, and an account with MFA needs a new code after every restart.

## F17: python-kasa hides IOT error codes on commands

- **Library / device:** python-kasa 0.10.2
- **Expected:** a device error carries the device's `err_code`.
- **Observed:** high-level commands (`turn_on`, `set_alias`, `reboot`, …) wrap every failure as `KasaException("Communication error on …")`, with the original error in `__cause__`. A non-zero IOT `err_code` appears only in the message text. `DeviceError.error_code` is set only on the SMART/AES paths.
- **Evidence:** `kasa/iot/iotdevice.py:246-284` (0.10.2). Rechecked on 0.12.0 on 2026-11-10: unchanged.
- **Status:** limitation. The service classifies failures by walking the exception chain and reports `device_error` without a code.

## F18: python-kasa retries transport errors internally

- **Library / device:** python-kasa 0.10.2
- **Expected:** a failed request surfaces promptly.
- **Observed:** `IotProtocol.query` retries connection, retryable and timeout errors up to 3 times, with a 1 s sleep after a timeout. There is no public setting for this on high-level calls.
- **Evidence:** `kasa/protocols/iotprotocol.py:85-137` (0.10.2). Rechecked on 0.12.0 on 2026-11-10: unchanged.
- **Status:** limitation. Local failures and latencies include the library's retries, within the service's per-call timeout. The service adds no retries of its own.

## F19: python-kasa discovery aborts if a callback raises

- **Library / device:** python-kasa 0.10.2
- **Expected:** one failing device doesn't hide the others.
- **Observed:** if an `on_discovered` callback raises a `KasaException` (e.g. an `AuthenticationError` from `update()`), `Discover.discover` closes every device and re-raises, returning nothing.
- **Evidence:** `kasa/discover.py:282-301, 502-505`.
- **Status:** handled. The service doesn't use `on_discovered`; it probes each returned device itself.

## F20: python-kasa schedule rules omit sunrise and sunset offsets

- **Library / device:** python-kasa 0.10.2
- **Expected:** rule offsets for sunrise and sunset triggers.
- **Observed:** the `Rule` model has no `soffset`/`eoffset` fields, and unknown keys are dropped.
- **Evidence:** `kasa/iot/modules/rulemodule.py:32-52`. Rechecked on 0.12.0 on 2026-11-10: unchanged.
- **Status:** limitation. On local schedules, `offset_minutes` is `null`; cloud schedules carry it.

## F21: python-kasa `IotDevice.device_id` returns the MAC

- **Library / device:** python-kasa 0.10.2
- **Expected:** the TP-Link `deviceId` (the ID the cloud uses).
- **Observed:** `IotDevice.device_id` returns `self.mac`. The real ID is available in `sys_info["deviceId"]`.
- **Evidence:** `kasa/iot/iotdevice.py:651-659` (0.10.2). Rechecked on 0.12.0 on 2026-11-10: unchanged.
- **Status:** handled. The service reads `sys_info["deviceId"]`.

## F22: python-kasa doesn't expose the transport in use

- **Library / device:** python-kasa 0.10.2
- **Expected:** a public way to see which transport a device connection uses, to diagnose F1.
- **Observed:** only the private `device.protocol._transport` holds it. `device.config.connection_type` is public and shows the encryption type and login version.
- **Evidence:** `kasa/device.py`, `kasa/deviceconfig.py`. Rechecked on 0.12.0 on 2026-11-10: unchanged.
- **Status:** limitation. `upstream.connection` reports `encrypt_type` and `login_version` only. On python-kasa 0.10.2, `KLAP` with login version 2 on an IOT plug identified F1.

## F23: tplink-cloud-api misses a Kasa MFA request nested in a success reply

- **Library / device:** tplink-cloud-api 5.2.1 · the bench TP-Link account (KP115(US))
- **Expected:** when the Kasa cloud wants an MFA code, `login()` calls `mfa_callback` or raises `TPLinkMFARequiredError`.
- **Observed:** the Kasa login reply had a top-level `error_code` of 0, with the MFA request inside `result`: `{"errorCode": "-20677", "errorMsg": "MFA feature enabled", "MFAProcessId": …, "supportedMFATypes": [2, 1], "failedAttempts": 0, …}`. The library only checks the top-level code, so the login "succeeds" without a token. `get_devices()` then returns an empty list (F12). With the same credentials, the Tapo cloud logged in without MFA and listed the KP115 (`IOT.SMARTPLUGSWITCH`).
- **Evidence:** diagnosis against the bench account, 2026-09-23 (no failed attempts counted). Reproduced by `tests/conformance/test_cloud_library.py::test_f23_*`.
- **Status:** limitation. The cloud driver also logs in to the Tapo cloud (public `include_tapo=True`) and reports `cloud_login_required` when neither cloud returns a token. MFA on the Kasa cloud can't be completed through this library for accounts that get the nested reply.

## F24: KP115 energy readings lag the relay by several seconds

- **Library / device:** KP115(US) hw 1.0, read through the TP-Link cloud (tplink-cloud-api 5.2.1 passes `emeter.get_realtime` through unchanged)
- **Expected:** once the relay reads back as switched, `get_realtime` reflects the new load.
- **Observed:** the relay state (`get_sysinfo`) changes at once, but the meter fields catch up later and each on its own schedule. With a 4.2 W LED lamp:
  - After switching off, `current_ma` fell to 0 after 2–3 s, but `power_mw` kept its old value for 5–8 s.
  - After switching on, current appeared after about 3 s and power after about 6 s.
  - In between, one reading can pair the old power with the new current (for example `power_mw` 4172 with `current_ma` 0).
  - `voltage_mv` also changes in steps a few seconds apart.
- **Evidence:** bench measurements on 2026-09-23, polling `GET /v1/cloud/plugs/{mac}/energy` about once a second after `POST …/relay`; the raw replies are in `upstream`. The plug's metering and the cloud relay can't be told apart until local access works (F1).
- **Status:** limitation of the device. A reading taken right after a switch doesn't show the new load. A Knilb step that compares a meter under test against the plug should re-read until power and current agree, or wait about 10 s. The hardware tests poll for this (`settled_energy` in `tests/hardware/conftest.py`).
