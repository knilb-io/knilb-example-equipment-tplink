"""Configuration precedence (SPEC §4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from equipment_tplink.config import load_settings


def test_defaults_without_any_env(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)
    assert settings.port == 8080
    assert settings.cloud_configured is False
    assert "tplink_password" not in repr(settings)


def test_precedence(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("EQUIPMENT_TPLINK_PORT=1000\nTPLINK_USERNAME=dot\n")
    extra = tmp_path / "extra.env"
    extra.write_text("EQUIPMENT_TPLINK_PORT=2000\nTPLINK_PASSWORD=pw\nUNRELATED=1\n")
    settings = load_settings(
        extra, environ={"EQUIPMENT_TPLINK_PORT": "3000", "HOME": "/x"}, cwd=tmp_path
    )
    assert settings.port == 3000
    assert settings.tplink_username == "dot"
    assert settings.cloud_configured is True
    empty = load_settings(environ={"TPLINK_USERNAME": ""}, cwd=tmp_path)
    assert empty.tplink_username == "dot"


def test_missing_env_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.env", environ={}, cwd=tmp_path)


def test_invalid_value(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_settings(environ={"EQUIPMENT_TPLINK_PORT": "0"}, cwd=tmp_path)
