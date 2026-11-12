"""Plug routes, built once per driver (SPEC §6.1)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic.json_schema import SkipJsonSchema

from equipment_tplink.core.models import (
    ActionAccepted,
    CloudPlugList,
    DeviceInfo,
    Driver,
    EnergyReading,
    Error,
    FactoryResetRequest,
    LocalPlugList,
    PlugState,
    RebootRequest,
    RelayCommand,
    RelayResult,
    Schedule,
    Settings,
    SettingsUpdate,
    Until,
    WaitResult,
)
from equipment_tplink.core.service import PlugService
from equipment_tplink.drivers.cloud import CloudTPLinkDriver
from equipment_tplink.drivers.local import LocalKasaDriver
from equipment_tplink.kit.errors import error_responses
from equipment_tplink.kit.request_log import annotate

PlugParam = Annotated[
    str,
    Path(
        description=(
            "MAC (any separator), IPv4 address or hostname (local), or MAC or "
            "40-hex TP-Link deviceId (cloud). See SPEC §5."
        ),
        examples=["00:00:5E:00:53:01"],
    ),
]
CallTimeout = Annotated[
    float | SkipJsonSchema[None],
    Query(ge=0.5, le=120, description="Per-call timeout in seconds (driver default)."),
]


def plug_statuses(kind: Driver, *extra: int) -> tuple[int, ...]:
    """Error statuses shared by every single-plug operation of ``kind``."""
    base = [400, 404, 422, 500, 501, 502, 504, *extra]
    if kind == "cloud":
        base.append(503)
    return tuple(sorted(set(base)))


def plug_router(kind: Driver) -> APIRouter:
    """Build the routes for one driver; ``kind`` is ``local`` or ``cloud``."""
    name = kind.capitalize()
    router = APIRouter(prefix=f"/v1/{kind}", tags=[kind])

    def service(request: Request) -> PlugService:
        services: dict[str, PlugService] = request.app.state.services
        return services[kind]

    Service = Annotated[PlugService, Depends(service)]
    responses = error_responses(Error, *plug_statuses(kind))

    if kind == "local":

        @router.get(
            "/plugs",
            operation_id="discoverLocalPlugs",
            response_model=LocalPlugList,
            responses=error_responses(Error, 422, 500, 502, 504),
            summary="Discover Kasa devices on the LAN",
        )
        async def discover(
            svc: Service,
            timeout_s: Annotated[
                float | SkipJsonSchema[None],
                Query(ge=1, le=30, description="Discovery duration in seconds."),
            ] = None,
        ) -> LocalPlugList:
            driver = svc.driver
            assert isinstance(driver, LocalKasaDriver)
            annotate(driver="local")
            duration = timeout_s or driver.discovery_timeout_s
            return await svc.bounded(
                duration + svc.call_timeout_s + 1,
                lambda: driver.discover_plugs(timeout_s=duration),
            )

    else:

        @router.get(
            "/plugs",
            operation_id="listCloudPlugs",
            response_model=CloudPlugList,
            responses=error_responses(Error, 422, 500, 502, 503, 504),
            summary="List devices in the TP-Link account",
        )
        async def list_cloud(
            svc: Service,
            refresh: Annotated[
                bool, Query(description="Bypass the 60 s cache.")
            ] = False,
            timeout_s: CallTimeout = None,
        ) -> CloudPlugList:
            driver = svc.driver
            assert isinstance(driver, CloudTPLinkDriver)
            annotate(driver="cloud")
            return await svc.bounded(
                timeout_s or svc.call_timeout_s,
                lambda: driver.list_plugs(refresh=refresh),
            )

    @router.get(
        "/plugs/{plug}",
        operation_id=f"get{name}PlugState",
        response_model=PlugState,
        responses=responses,
        summary="Read the relay state",
    )
    async def get_state(
        plug: PlugParam, svc: Service, timeout_s: CallTimeout = None
    ) -> PlugState:
        return await svc.state(plug, timeout_s=timeout_s)

    @router.get(
        "/plugs/{plug}/wait",
        operation_id=f"wait{name}Plug",
        response_model=WaitResult,
        responses=error_responses(
            Error, *([400, 422, 500, 503] if kind == "cloud" else [400, 422, 500])
        ),
        summary="Wait until the plug is on, off, online or offline",
    )
    async def wait(
        plug: PlugParam,
        svc: Service,
        until: Annotated[Until, Query(description="Condition to wait for.")],
        timeout_s: Annotated[
            float, Query(ge=1, le=300, description="Wait window in seconds.")
        ] = 30,
    ) -> WaitResult:
        return await svc.wait(plug, until, window_s=timeout_s)

    @router.post(
        "/plugs/{plug}/relay",
        operation_id=f"set{name}PlugRelay",
        response_model=RelayResult,
        responses=responses,
        summary="Switch the relay (and verify it)",
    )
    async def set_relay(
        plug: PlugParam,
        command: RelayCommand,
        svc: Service,
        timeout_s: CallTimeout = None,
    ) -> RelayResult:
        return await svc.relay(plug, command, timeout_s=timeout_s)

    @router.get(
        "/plugs/{plug}/energy",
        operation_id=f"get{name}PlugEnergy",
        response_model=EnergyReading,
        responses=responses,
        summary="Read one instantaneous energy value",
    )
    async def get_energy(
        plug: PlugParam, svc: Service, timeout_s: CallTimeout = None
    ) -> EnergyReading:
        return await svc.energy(plug, timeout_s=timeout_s)

    @router.get(
        "/plugs/{plug}/info",
        operation_id=f"get{name}PlugInfo",
        response_model=DeviceInfo,
        responses=responses,
        summary="Read hardware, firmware and cloud-link details",
    )
    async def get_info(
        plug: PlugParam, svc: Service, timeout_s: CallTimeout = None
    ) -> DeviceInfo:
        return await svc.info(plug, timeout_s=timeout_s)

    @router.get(
        "/plugs/{plug}/schedule",
        operation_id=f"get{name}PlugSchedule",
        response_model=Schedule,
        responses=responses,
        summary="Read the plug's schedule rules",
    )
    async def get_schedule(
        plug: PlugParam, svc: Service, timeout_s: CallTimeout = None
    ) -> Schedule:
        return await svc.schedule(plug, timeout_s=timeout_s)

    @router.get(
        "/plugs/{plug}/settings",
        operation_id=f"get{name}PlugSettings",
        response_model=Settings,
        responses=responses,
        summary="Read alias and LED state",
    )
    async def get_settings(
        plug: PlugParam, svc: Service, timeout_s: CallTimeout = None
    ) -> Settings:
        return await svc.settings(plug, timeout_s=timeout_s)

    @router.patch(
        "/plugs/{plug}/settings",
        operation_id=f"update{name}PlugSettings",
        response_model=Settings,
        responses=responses,
        summary="Change alias and/or LED state"
        + (
            " (alias: 501, no public API in tplink-cloud-api)"
            if kind == "cloud"
            else ""
        ),
    )
    async def update_settings(
        plug: PlugParam,
        update: SettingsUpdate,
        svc: Service,
        timeout_s: CallTimeout = None,
    ) -> Settings:
        return await svc.update_settings(plug, update, timeout_s=timeout_s)

    if kind == "local":

        @router.post(
            "/plugs/{plug}/reboot",
            operation_id="rebootLocalPlug",
            response_model=ActionAccepted,
            status_code=202,
            responses=responses,
            summary="Reboot the plug",
        )
        async def reboot(
            plug: PlugParam,
            svc: Service,
            request: Annotated[RebootRequest, Body(default_factory=RebootRequest)],
            timeout_s: CallTimeout = None,
        ) -> ActionAccepted:
            return await svc.reboot(plug, request, timeout_s=timeout_s)

        @router.post(
            "/plugs/{plug}/factory-reset",
            operation_id="factoryResetLocalPlug",
            response_model=ActionAccepted,
            status_code=202,
            responses=error_responses(Error, *plug_statuses(kind, 409)),
            summary="Factory-reset the plug (requires confirm_mac)",
        )
        async def factory_reset(
            plug: PlugParam,
            request: FactoryResetRequest,
            svc: Service,
            timeout_s: CallTimeout = None,
        ) -> ActionAccepted:
            return await svc.factory_reset(plug, request, timeout_s=timeout_s)

    return router
