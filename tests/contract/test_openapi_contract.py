"""The committed OpenAPI document is the contract Knilb imports (SPEC §9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate

from equipment_tplink.kit.openapi import to_yaml
from tests.conftest import OpenApiValidator

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "equipment-tplink-openapi.yaml"

OPERATION_IDS = {
    "getHealth",
    "getVersion",
    "listPlugs",
    "discoverLocalPlugs",
    "getLocalPlugState",
    "waitLocalPlug",
    "setLocalPlugRelay",
    "getLocalPlugEnergy",
    "getLocalPlugInfo",
    "getLocalPlugSchedule",
    "getLocalPlugSettings",
    "updateLocalPlugSettings",
    "rebootLocalPlug",
    "factoryResetLocalPlug",
    "listCloudPlugs",
    "getCloudPlugState",
    "waitCloudPlug",
    "setCloudPlugRelay",
    "getCloudPlugEnergy",
    "getCloudPlugInfo",
    "getCloudPlugSchedule",
    "getCloudPlugSettings",
    "updateCloudPlugSettings",
    "getCloudSession",
    "loginCloud",
    "logoutCloud",
}


def operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
    ]


def test_document_is_valid_openapi_31(document: dict[str, Any]) -> None:
    assert document["openapi"] == "3.1.0"
    validate(document)
    assert "securitySchemes" not in document.get("components", {})
    assert document["servers"][0]["url"] == "http://{host}:{port}"


def test_operation_ids_match_spec(document: dict[str, Any]) -> None:
    ids = [operation["operationId"] for _, _, operation in operations(document)]
    assert len(ids) == len(set(ids)) == 26
    assert set(ids) == OPERATION_IDS


def test_every_response_has_an_example(document: dict[str, Any]) -> None:
    for path, method, operation in operations(document):
        for status, response in operation["responses"].items():
            media = response["content"]["application/json"]
            assert "example" in media or "examples" in media, (method, path, status)


def test_every_example_matches_its_schema(
    document: dict[str, Any], validator: OpenApiValidator
) -> None:
    for path, method, operation in operations(document):
        for status, response in operation["responses"].items():
            media = response["content"]["application/json"]
            pointer = validator.pointer_for(method.upper(), path, int(status))
            assert pointer is not None
            examples = (
                [media["example"]]
                if "example" in media
                else [item["value"] for item in media["examples"].values()]
            )
            for example in examples:
                validator.validate_pointer(pointer, example)


def test_no_split_input_output_schemas(document: dict[str, Any]) -> None:
    names = document["components"]["schemas"]
    assert not [name for name in names if name.endswith(("-Input", "-Output"))]


def test_plug_parameter_has_no_pattern(document: dict[str, Any]) -> None:
    parameters = document["paths"]["/v1/local/plugs/{plug}"]["get"]["parameters"]
    plug = next(p for p in parameters if p["name"] == "plug")
    assert "pattern" not in plug["schema"]


def test_committed_document_is_current(document: dict[str, Any]) -> None:
    if not COMMITTED.exists():  # pragma: no cover - guards a fresh checkout
        pytest.fail("run `make openapi` to create equipment-tplink-openapi.yaml")
    committed = yaml.safe_load(COMMITTED.read_text(encoding="utf-8"))
    generated = yaml.safe_load(to_yaml(document))
    # python-semantic-release bumps the version after merge; ignore only that field.
    committed["info"].pop("version")
    generated["info"].pop("version")
    assert committed == generated, "run `make openapi` and commit the result"
