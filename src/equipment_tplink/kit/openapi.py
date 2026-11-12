"""Build, decorate and serialize the OpenAPI document.

FastAPI's generator drops ``null`` from examples, so examples are injected after
generation from a registry keyed by ``(operationId, status)``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response

ExampleRegistry = Mapping[tuple[str, int], Mapping[str, Any]]
"""``{(operationId, status): {"example": ...} | {"examples": {name: {...}}}}``."""

ExampleBuilder = Callable[[Mapping[str, Any]], ExampleRegistry]
"""Builds the registry from the generated document (operationIds, tags, statuses)."""


def build_openapi(
    app: FastAPI,
    *,
    title: str,
    version: str,
    description: str,
    tags: Sequence[Mapping[str, str]],
    servers: Sequence[Mapping[str, Any]],
    examples: ExampleBuilder,
) -> dict[str, Any]:
    """Generate the document and attach examples to JSON responses."""
    document = get_openapi(
        title=title,
        version=version,
        description=description,
        routes=app.routes,
        tags=[dict(tag) for tag in tags],
        servers=[dict(server) for server in servers],
    )
    registry = examples(document)
    for path_item in document.get("paths", {}).values():
        for operation in path_item.values():
            operation_id = operation.get("operationId")
            for status, response in operation.get("responses", {}).items():
                media = response.get("content", {}).get("application/json")
                example = registry.get((operation_id, int(status)))
                if media is not None and example is not None:
                    media.update(json.loads(json.dumps(example)))
    return document


def install_openapi(app: FastAPI, builder: Callable[[], dict[str, Any]]) -> None:
    """Serve ``builder()`` at ``/openapi.json`` and ``/openapi.yaml``."""
    cache: dict[str, dict[str, Any]] = {}

    def openapi() -> dict[str, Any]:
        if "doc" not in cache:
            cache["doc"] = builder()
        return cache["doc"]

    app.openapi = openapi  # type: ignore[method-assign]

    @app.get("/openapi.yaml", include_in_schema=False)
    async def openapi_yaml() -> Response:
        return Response(to_yaml(openapi()), media_type="application/yaml")


def to_yaml(document: Mapping[str, Any]) -> str:
    """Serialize deterministically (JSON round-trip avoids YAML anchors)."""
    plain = json.loads(json.dumps(document))
    text: str = yaml.safe_dump(plain, sort_keys=False, allow_unicode=True, width=100)
    return text


def to_json(document: Mapping[str, Any]) -> str:
    """Serialize as indented JSON with a trailing newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
