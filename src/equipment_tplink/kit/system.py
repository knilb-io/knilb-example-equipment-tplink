"""``/v1/health`` and ``/v1/version`` for any equipment service."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel


def system_router(
    *,
    health: Callable[[], Awaitable[BaseModel]],
    health_model: type[BaseModel],
    version: Callable[[], BaseModel],
    version_model: type[BaseModel],
    responses: dict[int | str, Any],
    prefix: str = "/v1",
) -> APIRouter:
    """Build the system router around the service's own health and version models."""
    router = APIRouter(prefix=prefix, tags=["system"])

    @router.get(
        "/health",
        operation_id="getHealth",
        response_model=health_model,
        responses=responses,
        summary="Service and driver health",
    )
    async def get_health() -> BaseModel:
        return await health()

    @router.get(
        "/version",
        operation_id="getVersion",
        response_model=version_model,
        responses=responses,
        summary="Service and library versions",
    )
    async def get_version() -> BaseModel:
        return version()

    return router
