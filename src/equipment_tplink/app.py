"""Application factory: wires the kit, the drivers and the routers together."""

from __future__ import annotations

import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from typing import Any

from fastapi import FastAPI
from kasa import Credentials

from equipment_tplink import __version__
from equipment_tplink.config import Settings
from equipment_tplink.core.directory import Directory
from equipment_tplink.core.models import (
    CloudHealth,
    Error,
    Health,
    LocalHealth,
    Version,
)
from equipment_tplink.core.service import PlugService, ServiceTimings
from equipment_tplink.drivers.cloud import CloudTPLinkDriver
from equipment_tplink.drivers.cloud_session import CloudSessionManager, ManagerFactory
from equipment_tplink.drivers.local import DiscoverFn, DiscoverSingleFn, LocalKasaDriver
from equipment_tplink.fakes.cloud import fake_manager_factory
from equipment_tplink.fakes.kasa import fake_discover, fake_discover_single
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import Clock, SystemClock
from equipment_tplink.kit.errors import error_responses, install_error_handlers
from equipment_tplink.kit.openapi import build_openapi, install_openapi
from equipment_tplink.kit.request_log import RequestLogMiddleware
from equipment_tplink.kit.system import system_router
from equipment_tplink.routers.cloud_session import cloud_session_router
from equipment_tplink.routers.directory import directory_router
from equipment_tplink.routers.examples import build_registry
from equipment_tplink.routers.plugs import plug_router

TITLE = "equipment-tplink"
DESCRIPTION = """\
TP-Link Kasa energy plugs as Knilb test equipment and as a device under test.

Two drivers sit behind one contract: `local` (python-kasa over the LAN) and `cloud`
(tplink-cloud-api over the TP-Link account API). Both libraries are used through
their public APIs only; their limitations are surfaced, not worked around (see the
README's "Known library limitations" and FINDINGS.md). Every plug response carries an
`upstream` block with the library calls behind it.

**No authentication.** Run only on a trusted LAN behind Knilb's reachability layer.
"""
TAGS = [
    {"name": "system", "description": "Service health and versions."},
    {
        "name": "directory",
        "description": "Every known plug, merged across drivers by MAC.",
    },
    {"name": "local", "description": "Plugs on the LAN through python-kasa."},
    {
        "name": "cloud",
        "description": "Plugs through the TP-Link account API (tplink-cloud-api), "
        "including the login session.",
    },
]
SERVERS = [
    {
        "url": "http://{host}:{port}",
        "variables": {
            "host": {"default": "localhost"},
            "port": {"default": "8080"},
        },
    }
]
POLL_INTERVAL_S = {"local": 0.1, "cloud": 1.0}


@dataclass
class LibraryHooks:
    """Library entry points; replaced by fakes in ``serve --fake`` and tests."""

    discover: DiscoverFn | None = None
    discover_single: DiscoverSingleFn | None = None
    manager_factory: ManagerFactory | None = None


def create_app(
    settings: Settings,
    *,
    fake: bool = False,
    clock: Clock | None = None,
    world: FakeWorld | None = None,
    hooks: LibraryHooks | None = None,
    mfa_wait_s: float = 300.0,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Validated configuration.
        fake: Back both drivers with an in-memory :class:`FakeWorld`.
        clock: Time source (tests pass a ``FakeClock``).
        world: The fake world to use when ``fake`` is set.
        hooks: Explicit library entry points (override ``fake``).
        mfa_wait_s: How long a cloud login waits for an MFA code.
    """
    clock = clock or SystemClock()
    hooks = hooks or LibraryHooks()
    username, password = settings.tplink_username, settings.tplink_password
    if fake:
        world = world or FakeWorld(clock, cloud_latency_s=0.5)
        hooks = LibraryHooks(
            discover=hooks.discover or fake_discover(world),
            discover_single=hooks.discover_single or fake_discover_single(world),
            manager_factory=hooks.manager_factory or fake_manager_factory(world),
        )
        if not settings.cloud_configured:
            username, password = world.account.username, world.account.password

    directory = Directory(clock)
    credentials = Credentials(username, password) if username and password else None
    local = LocalKasaDriver(
        directory=directory,
        clock=clock,
        credentials=credentials,
        discovery_target=settings.discovery_target,
        discovery_timeout_s=settings.discovery_timeout_s,
        call_timeout_s=settings.local_timeout_s,
        discover=hooks.discover,
        discover_single=hooks.discover_single,
    )
    session = CloudSessionManager(
        username=username,
        password=password,
        clock=clock,
        manager_factory=hooks.manager_factory,
        mfa_wait_s=mfa_wait_s,
    )
    cloud = CloudTPLinkDriver(session=session, directory=directory, clock=clock)
    services = {
        "local": PlugService(
            local,
            clock=clock,
            timings=ServiceTimings(
                settings.local_timeout_s,
                POLL_INTERVAL_S["local"],
                settings.relay_settle_ms,
            ),
        ),
        "cloud": PlugService(
            cloud,
            clock=clock,
            timings=ServiceTimings(
                settings.cloud_timeout_s,
                POLL_INTERVAL_S["cloud"],
                settings.relay_settle_ms,
            ),
        ),
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        session.start()
        try:
            yield
        finally:
            await session.close()
            await local.close()

    app = FastAPI(
        title=TITLE,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.directory = directory
    app.state.services = services
    app.state.cloud_session = session
    app.state.world = world

    install_error_handlers(
        app,
        error_model=Error,
        defaults={"driver": "none", "plug": None, "upstream": None, "detail": None},
    )
    app.add_middleware(RequestLogMiddleware)

    async def health() -> Health:
        return Health(
            status="ok",
            version=__version__,
            local=LocalHealth(
                status="ready",
                discovery_target=settings.discovery_target,
                plugs_known=directory.local_count(),
            ),
            cloud=CloudHealth(status=session.state, last_error=session.last_error),
        )

    app.include_router(
        system_router(
            health=health,
            health_model=Health,
            version=versions,
            version_model=Version,
            responses=error_responses(Error, 500),
        )
    )
    app.include_router(directory_router())
    app.include_router(plug_router("local"))
    app.include_router(plug_router("cloud"))
    app.include_router(cloud_session_router())

    def document() -> dict[str, Any]:
        return build_openapi(
            app,
            title=TITLE,
            version=__version__,
            description=DESCRIPTION,
            tags=TAGS,
            servers=SERVERS,
            examples=build_registry,
        )

    install_openapi(app, document)
    return app


def versions() -> Version:
    """Versions of the service, Python and both libraries."""
    return Version(
        version=__version__,
        python=platform.python_version(),
        python_kasa=distribution_version("python-kasa"),
        tplink_cloud_api=distribution_version("tplink-cloud-api"),
    )
