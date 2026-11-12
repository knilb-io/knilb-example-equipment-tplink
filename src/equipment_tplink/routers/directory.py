"""``GET /v1/plugs``: every known plug, merged across drivers by MAC."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request

from equipment_tplink.core.models import Error, ErrorSummary, PlugDirectory
from equipment_tplink.core.service import PlugService
from equipment_tplink.drivers.cloud import CloudTPLinkDriver
from equipment_tplink.drivers.local import LocalKasaDriver
from equipment_tplink.kit.errors import ServiceError, error_responses


def directory_router() -> APIRouter:
    """Build the directory router."""
    router = APIRouter(prefix="/v1", tags=["directory"])

    @router.get(
        "/plugs",
        operation_id="listPlugs",
        response_model=PlugDirectory,
        responses=error_responses(Error, 422, 500),
        summary="All known plugs, merged by MAC",
    )
    async def list_plugs(
        request: Request,
        refresh: Annotated[
            bool, Query(description="Run LAN discovery and a cloud list first.")
        ] = False,
    ) -> PlugDirectory:
        state = request.app.state
        local_error = cloud_error = None
        if refresh:
            local_svc: PlugService = state.services["local"]
            cloud_svc: PlugService = state.services["cloud"]
            local_driver = local_svc.driver
            cloud_driver = cloud_svc.driver
            assert isinstance(local_driver, LocalKasaDriver)
            assert isinstance(cloud_driver, CloudTPLinkDriver)
            duration = local_driver.discovery_timeout_s
            results = await asyncio.gather(
                local_svc.bounded(
                    duration + local_svc.call_timeout_s + 1,
                    lambda: local_driver.discover_plugs(timeout_s=duration),
                ),
                cloud_svc.bounded(
                    cloud_svc.call_timeout_s,
                    lambda: cloud_driver.list_plugs(refresh=True),
                ),
                return_exceptions=True,
            )
            local_error = _summary(results[0])
            cloud_error = _summary(results[1])
        return PlugDirectory(
            plugs=state.directory.entries(),
            local_error=local_error,
            cloud_error=cloud_error,
        )

    return router


def _summary(result: object) -> ErrorSummary | None:
    if isinstance(result, ServiceError):
        return ErrorSummary(code=result.code, message=result.message)
    if isinstance(result, BaseException):
        return ErrorSummary(code="internal_error", message=type(result).__name__)
    return None
