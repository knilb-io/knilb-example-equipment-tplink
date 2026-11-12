"""``/v1/cloud/session``: log in (including MFA), inspect and log out."""

from typing import Annotated

from fastapi import APIRouter, Body, Request

from equipment_tplink.core.models import CloudLoginRequest, CloudSession, Error
from equipment_tplink.drivers.cloud_session import CloudSessionManager
from equipment_tplink.kit.errors import error_responses
from equipment_tplink.kit.request_log import annotate


def cloud_session_router() -> APIRouter:
    """Build the cloud session router."""
    router = APIRouter(prefix="/v1/cloud", tags=["cloud"])

    def session(request: Request) -> CloudSessionManager:
        manager: CloudSessionManager = request.app.state.cloud_session
        annotate(driver="cloud")
        return manager

    @router.get(
        "/session",
        operation_id="getCloudSession",
        response_model=CloudSession,
        responses=error_responses(Error, 500),
        summary="Cloud login state",
    )
    async def get_session(request: Request) -> CloudSession:
        return session(request).snapshot()

    @router.post(
        "/session",
        operation_id="loginCloud",
        response_model=CloudSession,
        responses=error_responses(Error, 409, 422, 500, 502, 503, 504),
        summary="Log in, or supply the MFA code a pending login is waiting for",
    )
    async def login(
        request: Request,
        body: Annotated[CloudLoginRequest, Body(default_factory=CloudLoginRequest)],
    ) -> CloudSession:
        timeout = request.app.state.settings.cloud_timeout_s
        return await session(request).login(body.mfa_code, timeout=timeout)

    @router.delete(
        "/session",
        operation_id="logoutCloud",
        response_model=CloudSession,
        responses=error_responses(Error, 500),
        summary="Forget the cloud login",
    )
    async def logout(request: Request) -> CloudSession:
        return await session(request).logout()

    return router
