"""Shared fixtures: a fake clock and world, the fake-backed app, a validating client."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from equipment_tplink.app import create_app
from equipment_tplink.config import Settings
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock

MAC1 = "00:00:5E:00:53:01"
MAC2 = "00:00:5E:00:53:02"
DOC_URI = "urn:equipment-tplink:openapi"


class OpenApiValidator:
    """Validate response bodies against the generated OpenAPI document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.registry: Registry = Registry().with_resource(
            DOC_URI, Resource.from_contents(document, default_specification=DRAFT202012)
        )
        self.routes: list[tuple[str, re.Pattern[str], str]] = []
        for path in document["paths"]:
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", path)
            for method in document["paths"][path]:
                self.routes.append((method.upper(), re.compile(f"^{pattern}$"), path))

    def pointer_for(self, method: str, path: str, status: int) -> str | None:
        """JSON pointer of the response schema, or None for undocumented routes."""
        for route_method, pattern, template in self.routes:
            if route_method == method and pattern.match(path):
                responses = self.document["paths"][template][method.lower()][
                    "responses"
                ]
                assert str(status) in responses, (
                    f"{method} {template} returned undocumented status {status}"
                )
                return "/".join(
                    [
                        "#",
                        "paths",
                        _escape(template),
                        method.lower(),
                        "responses",
                        str(status),
                        "content",
                        "application~1json",
                        "schema",
                    ]
                )
        return None

    def validate(self, method: str, path: str, status: int, body: Any) -> None:
        """Raise if ``body`` does not match the documented schema."""
        pointer = self.pointer_for(method, path, status)
        if pointer is None:
            pointer = "#/components/schemas/Error" if status >= 400 else None
        if pointer is None:
            return
        self.validate_pointer(pointer, body)

    def validate_pointer(self, pointer: str, body: Any) -> None:
        """Validate ``body`` against the schema at ``pointer``."""
        validator = Draft202012Validator(
            {"$ref": DOC_URI + pointer},
            registry=self.registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        validator.validate(body)


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


class ValidatingClient:
    """A TestClient whose JSON responses are validated against the contract."""

    def __init__(self, client: TestClient, validator: OpenApiValidator) -> None:
        self.client = client
        self.validator = validator

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response: Any = self.client.request(method, url, **kwargs)
        if response.headers.get("content-type", "").startswith("application/json"):
            path = url.split("?", 1)[0]
            self.validator.validate(
                method.upper(), path, response.status_code, response.json()
            )
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(clock: FakeClock) -> FakeWorld:
    return FakeWorld(clock)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def app(settings: Settings, clock: FakeClock, world: FakeWorld) -> FastAPI:
    return create_app(settings, fake=True, clock=clock, world=world, mfa_wait_s=5)


@pytest.fixture(scope="session")
def document() -> dict[str, Any]:
    return create_app(Settings(), fake=True).openapi()


@pytest.fixture(scope="session")
def validator(document: dict[str, Any]) -> OpenApiValidator:
    return OpenApiValidator(document)


@pytest.fixture
def client(app: FastAPI, validator: OpenApiValidator) -> Iterator[ValidatingClient]:
    with TestClient(app) as test_client:
        yield ValidatingClient(test_client, validator)
