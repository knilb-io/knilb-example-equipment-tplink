# equipment-tplink

> This repository is an example. It shows one way to build test equipment for Knilb on low-cost hardware. It is not a finished product, it is not maintained on a schedule, and it may change or be archived without notice. Fork it and adapt it to your bench.

A small REST service that makes TP-Link Kasa energy-monitoring plugs (KP115, HS110, KP125) usable from the [Knilb](http://knilb.com) test platform, in two roles:

- **Test equipment.** A switchable, metered load. When an energy meter is under test, the plug's own watt reading is the reference.
- **Device under test.** The plug and TP-Link's cloud are themselves tested: whether cloud commands switch the plug, how the cloud API behaves, and how the firmware handles auth, reboots and resets.

Two drivers sit behind one OpenAPI 3.1 contract ([`equipment-tplink-openapi.yaml`](equipment-tplink-openapi.yaml)):

| Driver | Library | Reaches the plug | Addressed by |
|---|---|---|---|
| `local` | [python-kasa](https://github.com/python-kasa/python-kasa) | over the LAN | MAC, IP or hostname |
| `cloud` | [tplink-cloud-api](https://github.com/piekstra/tplink-cloud-api) | through the TP-Link account | MAC or cloud `deviceId` |

Both libraries are used strictly through their public APIs. Their limitations are reported, never worked around (see [Known library limitations](#known-library-limitations)). Every response includes an `upstream` block listing the library calls it made, with timing, TP-Link error codes and the decoded replies.

## Try it without hardware

```bash
git clone https://github.com/knilb-io/knilb-example-equipment-tplink.git
cd knilb-example-equipment-tplink
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/equipment-tplink serve --fake
```

`--fake` serves two simulated plugs (MACs `00:00:5E:00:53:01` and `00:00:5E:00:53:02`) through the real drivers. It needs no credentials, and a cloud command reaches the simulated plug after 0.5 s. Then, in another terminal:

```bash
curl -s localhost:8080/v1/health
curl -s -X POST localhost:8080/v1/cloud/plugs/00:00:5E:00:53:01/relay -H 'content-type: application/json' -d '{"state":"on","verify":false}'
curl -s "localhost:8080/v1/local/plugs/00:00:5E:00:53:01/wait?until=on&timeout_s=5"
```

Interactive API docs are at <http://localhost:8080/docs>.

## Install on a Raspberry Pi

These steps take a blank SD card to a running service in about ten minutes. You need a Pi 3, 4 or 5 on the same LAN as the plugs.

1. **Flash the SD card.** Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write *Raspberry Pi OS Lite (64-bit)*. In the Imager's settings, set a username, your Wi-Fi (or use Ethernet), and enable SSH. Boot the Pi and connect with `ssh <user>@<pi>.local`.
2. **Install git and venv support:**

   ```bash
   sudo apt update && sudo apt install -y git python3-venv
   ```

3. **Install the service.** 

   ```bash
   git clone https://github.com/knilb-io/knilb-example-equipment-tplink.git ~/equipment-tplink
   python3 -m venv ~/equipment-tplink/.venv
   ~/equipment-tplink/.venv/bin/pip install ~/equipment-tplink
   ```

4. **Configure it.** Copy the example settings and fill in your TP-Link account:

   ```bash
   cp ~/equipment-tplink/.env.example ~/equipment-tplink/.env
   chmod 600 ~/equipment-tplink/.env    # it will hold your TP-Link password
   nano ~/equipment-tplink/.env
   ```

5. **Run it as a service:**

   ```bash
   sudo cp ~/equipment-tplink/deploy/equipment-tplink@.service /etc/systemd/system/
   sudo systemctl enable --now equipment-tplink@$USER
   curl -s localhost:8080/v1/health
   ```

   Logs go to the journal as one JSON line per request: `journalctl -u equipment-tplink@$USER -f`.

To update, run `git -C ~/equipment-tplink pull`, reinstall with `~/equipment-tplink/.venv/bin/pip install ~/equipment-tplink`, then `sudo systemctl restart equipment-tplink@$USER`.

## Install and use on macOS

On a Mac the service runs in a terminal while you need it; there is nothing to flash and no system service to set up. The Mac must be on the same LAN as the plugs.

1. **Check Python.** The service needs Python 3.11 or newer. `python3 --version` shows what you have; `brew install python` installs a current one.
2. **Install the service.** 

   ```bash
   git clone https://github.com/knilb-io/knilb-example-equipment-tplink.git
   cd knilb-example-equipment-tplink
   python3 -m venv .venv
   .venv/bin/pip install .
   ```

3. **Configure it.** Copy the example settings and fill in your TP-Link account:

   ```bash
   cp .env.example .env
   chmod 600 .env    # it will hold your TP-Link password
   open -e .env
   ```

4. **Run it** from the repository folder, so it finds `.env`:

   ```bash
   .venv/bin/equipment-tplink serve
   ```

   Then, in another terminal:

   ```bash
   curl -s localhost:8080/v1/health
   ```

   Logs print in the first terminal as one JSON line per request. Press Ctrl-C to stop the service.

LAN discovery may need the Local Network permission for your terminal (System Settings → Privacy & Security). If no plugs are found, check that first.

To update, run `git pull`, reinstall with `.venv/bin/pip install .`, then start the service again.

## Configuration

Settings are read in this order, later sources overriding earlier ones: defaults, `./.env`, the file given by `--env-file`, then the process environment. Every variable is listed with a comment in [`.env.example`](.env.example). The ones you're most likely to set:

| Variable | Default | Meaning |
|---|---|---|
| `TPLINK_USERNAME`, `TPLINK_PASSWORD` | — | TP-Link account. Needed for the cloud driver, and passed to python-kasa for plugs that require local credentials (KLAP). |
| `EQUIPMENT_TPLINK_PORT` | `8080` | HTTP port. |
| `EQUIPMENT_TPLINK_DISCOVERY_TARGET` | `255.255.255.255` | Broadcast address. Set your subnet's (e.g. `192.168.1.255`) if the host has several network interfaces. |

Without credentials the service still starts: the local driver works, and cloud calls answer `503 cloud_not_configured`.

## Using the API

Everything lives under `/v1`. The contract is [SPEC.md](SPEC.md) §6, and the full schema with an example for every response is in [`equipment-tplink-openapi.yaml`](equipment-tplink-openapi.yaml).

**Identify a plug by its MAC.** Any common spelling of the MAC is accepted, and it works on both drivers. The local driver also accepts an IP address or hostname, and the cloud driver a `deviceId`.

```bash
equipment-tplink discover                                   # plugs on this LAN: MAC, IP, model, auth result
curl -s "localhost:8080/v1/plugs?refresh=true"              # local and cloud views merged by MAC
```

**Plug as test equipment** (a metered load):

```bash
curl -s -X POST localhost:8080/v1/local/plugs/00-00-5E-00-53-01/relay -H 'content-type: application/json' -d '{"state":"on"}'
curl -s localhost:8080/v1/local/plugs/00-00-5E-00-53-01/energy          # power_w, voltage_v, current_a, energy_total_kwh
```

**Timing matters when the plug is a meter's reference.** Each reading is instantaneous. Its `observed_at` is when the service read the plug, not when the plug measured the values. After a relay switch the plug's power figure lags by up to about 8 s, and power, current and voltage update separately (FINDINGS F24), so a reading taken at a switch can mix old and new values. Compare the meter under test with the plug only after the load has been steady for about 10 s, or re-read the plug until its power and current agree. Allow for the meter's own reporting interval too, and take both readings in the same steady period. See [Response times](#response-times).

**Plug as device under test** (the cloud command path):

```bash
curl -s -X POST localhost:8080/v1/cloud/plugs/00-00-5E-00-53-01/relay -H 'content-type: application/json' -d '{"state":"on","verify":false}'   # note sent_at
curl -s "localhost:8080/v1/local/plugs/00-00-5E-00-53-01/wait?until=on&timeout_s=30"                                                           # satisfied_at
```

`wait` polls the plug from the service itself (every 100 ms locally, every 1 s through the cloud), so the latency you measure doesn't depend on Knilb's round trip. `until` can be `on`, `off`, `online` or `offline`, which also measures reboot recovery (`POST …/local/plugs/{plug}/reboot`).

**Cloud login with MFA.** At startup the service logs in by itself, to both the Kasa and the Tapo cloud (accounts moved to TP-Link's newer app list their Kasa plugs in the Tapo cloud). If the account uses two-step verification, TP-Link emails a code and the service waits up to 5 minutes for it:

```bash
curl -s -X POST localhost:8080/v1/cloud/session -H 'content-type: application/json' -d '{}'                     # 503 cloud_mfa_required
curl -s -X POST localhost:8080/v1/cloud/session -H 'content-type: application/json' -d '{"mfa_code":"123456"}'   # 200 authenticated
```

**Errors** all use one `Error` body with a stable `code`, listed in SPEC §7.4. A `confirmed: false` on a relay command or `satisfied: false` on a wait is a result, not an error: both return 200. Knilb test steps assert on them.

## Response times

The service adds no padding of its own. When a call feels slow, the time goes here:

| Source | Typical time | Why | Setting |
|---|---|---|---|
| LAN discovery | 5 s | python-kasa listens for the whole window instead of stopping at the first reply. It runs on `GET /v1/local/plugs` and the first time a MAC is used on `local`; after that the service remembers the plug's IP. | `EQUIPMENT_TPLINK_DISCOVERY_TIMEOUT_S`, or `?timeout_s=` on `GET /v1/local/plugs` |
| Cloud login | about 2 s, at startup | A region lookup and a login over HTTPS, for both the Kasa and the Tapo cloud. | — |
| Cloud device list | about 0.8 s, at most once a minute | Needed to map a MAC to a cloud `deviceId`; cached for 60 s. | `?refresh=true` on `GET /v1/cloud/plugs` forces it |
| Cloud call | 0.5–1 s each | A round trip through TP-Link's servers to the plug and back. | — |
| Relay verify | 0.5 s per command | A deliberate wait before reading the relay back, so a slow plug isn't reported as `confirmed: false`. | `EQUIPMENT_TPLINK_RELAY_SETTLE_MS`, or `settle_ms` or `"verify": false` in the request |
| Energy after a switch | up to about 8 s | The plug's power reading lags the relay, and its fields update separately (FINDINGS F24). Re-read `…/energy` until power and current agree. | — |

`make hardware` takes about 25 s for the same reasons: two discoveries (each test module starts its own service), the cloud login, and the relay tests waiting for the energy reading to settle.

## Known library limitations

The service uses python-kasa and tplink-cloud-api as they are and doesn't patch them, so a few of their gaps show up in the API. They're grouped here by the API you call. Each one is also a candidate test case; the F numbers point to the details and evidence in [FINDINGS.md](FINDINGS.md).

### When you use the local API (`/v1/local/…`, python-kasa)

| When you… | What you will see | What to do | Finding |
|---|---|---|---|
| Read or command a plug that rejects the login | `502 device_auth_failed`, with `encrypt_type` and `login_version` in `upstream.connection`. Plugs with identical discovery flags can need different login hashes, depending on when they were paired (reported upstream, not seen on our bench). | Check `TPLINK_USERNAME` and `TPLINK_PASSWORD`. If they're right, re-pair the plug in the Kasa app. | F2 |
| Diagnose a connection | `upstream.connection` shows the encryption type and login version, but not which transport python-kasa chose. | — | F22 |
| Get an error from a plug | `502 device_error` with no `error_code`. python-kasa wraps the plug's code in a "Communication error…" message. | Read `error_message` in `upstream.calls`. | F17 |
| Call a plug that is slow or unreachable | The error arrives late. python-kasa retries up to 3 times before the service sees the failure. At the latest, the call ends with `504 timeout` when `timeout_s` runs out. | Pass a shorter `?timeout_s=` when a test needs a fast failure. | F18 |
| Read a schedule with sunrise or sunset rules | `offset_minutes` is `null`. | Read the schedule through the cloud API, which has the offsets. | F20 |

### When you use the cloud API (`/v1/cloud/…`, tplink-cloud-api)

| When you… | What you will see | What to do | Finding |
|---|---|---|---|
| Restart the service | It logs in again, because a login can't be saved and restored. With two-step verification on the account, TP-Link emails a new code after every restart. | Send the code with `POST /v1/cloud/session`. | F16 |
| Log in to an account that has two-step verification | The Kasa cloud's request for a code can go unnoticed, and that login ends without a token. The service also logs in to the Tapo cloud, where migrated accounts list their Kasa plugs. If neither cloud gives a token you get `503 cloud_login_required`. | — | F23 |
| List plugs while TP-Link's cloud is failing | The failure looks like an empty account: an empty list from `GET /v1/cloud/plugs`, or `404 plug_not_found` for a plug you know exists. | Try again with `GET /v1/cloud/plugs?refresh=true`, and check `/v1/health`. | F12 |
| Leave the service running past token expiry | Calls to a plug fail with `502 cloud_error` until the next plug list, at most 60 s later. | `GET /v1/cloud/plugs?refresh=true` renews the token at once. | F13 |
| Get an error from a plug call | No reason is given, never TP-Link's error code. You get `502 device_offline` if the account list shows the plug offline, otherwise `502 cloud_error`. | Use the local API to see the plug's own answer. | F11 |
| Change the LED | Always `502 cloud_error`, and the LED doesn't change. The library sends a malformed command. | Use `PATCH /v1/local/plugs/{plug}/settings`. | F14 |
| Rename a plug | `501 unsupported`. | Use `PATCH /v1/local/plugs/{plug}/settings`. | F10 |
| Reboot or factory-reset a plug | There is no cloud operation for either. | Use `POST /v1/local/plugs/{plug}/reboot` or `…/factory-reset`. | F10 |
| Read energy from an HS110 hardware v1 | Readings come back 1000× too small, because the library doesn't convert the older units. KP115, KP125 and HS110 v2 are unaffected. | Read energy through the local API. | F15 |

## Security

The service has **no authentication**. It was built before Knilb could send credentials to equipment services. Knilb can now, so an API key is a possible later addition, but nothing checks one today. The service switches mains power and can factory-reset plugs, so:

- Run it only on a trusted LAN, behind Knilb's reachability layer. Never expose it to the internet.
- Credentials stay in the environment or `.env`, which is gitignored. Responses mask account emails and strip tokens.

## Development

```bash
pip install pipenv   # once
pipenv sync --dev
make check      # ruff, ruff format --check, mypy --strict, pytest (branch coverage >= 90%)
make openapi    # regenerate equipment-tplink-openapi.yaml after changing routes or models
make hardware   # opt-in tests against a real plug; see tests/hardware/conftest.py
```

The tests run the real drivers against look-alikes of both libraries (`src/equipment_tplink/fakes/`). Conformance tests (`tests/conformance/`) then run the drivers against the *real* libraries, with the network emulated, and pin the limitations above.

`src/equipment_tplink/kit/` holds generic FastAPI building blocks (error bodies, OpenAPI with examples, request log, upstream recorder, health) that import nothing TP-Link-specific. Future Knilb equipment services can copy them.

Versions and GitHub releases come from [python-semantic-release](https://python-semantic-release.readthedocs.io/) on `main`, driven by Conventional Commits. The version lives only in `pyproject.toml`.

Project documents: [SPEC.md](SPEC.md) (the contract), [FINDINGS.md](FINDINGS.md) (library behaviour) and [CHANGELOG.md](CHANGELOG.md). This README is the user documentation; `docs/` is reserved for later.

## License

GPL-3.0. See [LICENSE](LICENSE). The service uses `python-kasa` (GPL-3.0-or-later) and `tplink-cloud-api` (GPL-3.0), so it is licensed under the GPL too.
