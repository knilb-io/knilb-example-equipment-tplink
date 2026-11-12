"""Command-line entry point: ``equipment-tplink`` (SPEC §3)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from equipment_tplink import __version__
from equipment_tplink.config import Settings, load_settings
from equipment_tplink.core.directory import Directory
from equipment_tplink.core.models import LocalPlugList
from equipment_tplink.kit.clock import SystemClock
from equipment_tplink.kit.openapi import to_json, to_yaml

Runner = Callable[..., Any]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one sub-command per operation."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file", type=Path, help="read settings from this .env file as well"
    )
    parser = argparse.ArgumentParser(
        prog="equipment-tplink",
        description="TP-Link Kasa plugs as Knilb test equipment.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", parents=[common], help="run the REST service")
    serve.add_argument("--host", help="bind address (EQUIPMENT_TPLINK_HOST)")
    serve.add_argument("--port", type=int, help="bind port (EQUIPMENT_TPLINK_PORT)")
    serve.add_argument("--log-level", help="log level (EQUIPMENT_TPLINK_LOG_LEVEL)")
    serve.add_argument(
        "--fake", action="store_true", help="serve fake plugs (no hardware needed)"
    )

    openapi = commands.add_parser(
        "openapi", parents=[common], help="write the OpenAPI document"
    )
    openapi.add_argument("--format", choices=("yaml", "json"), default="yaml")
    openapi.add_argument("-o", "--output", type=Path, help="file to write (stdout)")

    discover = commands.add_parser(
        "discover", parents=[common], help="list Kasa devices on the LAN"
    )
    discover.add_argument("--timeout", type=float, help="discovery duration (s)")
    discover.add_argument("--target", help="broadcast address")

    commands.add_parser("version", parents=[common], help="print the package version")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run_server: Runner | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the CLI and return a process exit code."""
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    if args.command == "version":
        print(__version__, file=out)
        return 0
    if args.command == "openapi":
        return _openapi(args, out)
    try:
        settings = load_settings(args.env_file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"equipment-tplink: {exc}", file=sys.stderr)
        return 2
    if args.command == "serve":
        return _serve(args, settings, run_server)
    return _discover(args, settings, out)


def _serve(
    args: argparse.Namespace, settings: Settings, run_server: Runner | None
) -> int:
    from equipment_tplink.app import create_app

    overrides = {
        key: value
        for key, value in (
            ("host", args.host),
            ("port", args.port),
            ("log_level", args.log_level),
        )
        if value is not None
    }
    settings = settings.model_copy(update=overrides)
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(message)s", force=True
    )
    app = create_app(settings, fake=args.fake)
    if run_server is None:  # pragma: no cover - exercised by running the service
        import uvicorn

        run_server = uvicorn.run
    run_server(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


def _openapi(args: argparse.Namespace, out: TextIO) -> int:
    """Write the document; it does not depend on settings, so none are loaded."""
    from equipment_tplink.app import create_app

    document = create_app(Settings(), fake=True).openapi()
    text = to_yaml(document) if args.format == "yaml" else to_json(document)
    if args.output is None:
        out.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


def _discover(args: argparse.Namespace, settings: Settings, out: TextIO) -> int:
    from kasa import Credentials

    from equipment_tplink.drivers.local import LocalKasaDriver

    credentials = (
        Credentials(settings.tplink_username or "", settings.tplink_password or "")
        if settings.cloud_configured
        else None
    )
    clock = SystemClock()
    driver = LocalKasaDriver(
        directory=Directory(clock),
        clock=clock,
        credentials=credentials,
        discovery_target=args.target or settings.discovery_target,
        discovery_timeout_s=args.timeout or settings.discovery_timeout_s,
        call_timeout_s=settings.local_timeout_s,
    )

    async def run() -> LocalPlugList:
        try:
            return await driver.discover_plugs(timeout_s=driver.discovery_timeout_s)
        finally:
            await driver.close()

    result = asyncio.run(run())
    print_discovery(result, out)
    return 0


def print_discovery(result: LocalPlugList, out: TextIO) -> None:
    """Print a discovery result as a table."""
    header = ("MAC", "HOST", "MODEL", "ALIAS", "ENCRYPTION", "AUTH")
    rows = [
        (
            plug.plug.mac or "-",
            plug.plug.host or "-",
            plug.plug.model or "-",
            plug.plug.alias or "-",
            _encryption(plug.connection.encrypt_type, plug.connection.login_version),
            plug.auth + ("" if plug.supported else " (unsupported model)"),
        )
        for plug in result.plugs
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print(
            "  ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            ).rstrip(),
            file=out,
        )
    print(
        f"\n{len(rows)} device(s) on {result.target} in {result.duration_ms} ms",
        file=out,
    )
    for plug in result.plugs:
        if plug.auth_error:
            print(f"- {plug.plug.mac or plug.plug.host}: {plug.auth_error}", file=out)


def _encryption(encrypt_type: str | None, login_version: int | None) -> str:
    return f"{encrypt_type or '-'} lv{login_version or '-'}"
