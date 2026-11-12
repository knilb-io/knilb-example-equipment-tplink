"""The ``equipment-tplink`` command, run in-process."""

from __future__ import annotations

import io
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from kasa import Discover

from equipment_tplink import __version__
from equipment_tplink.cli import main
from equipment_tplink.fakes.kasa import fake_discover
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("TPLINK_USERNAME", "TPLINK_PASSWORD", "EQUIPMENT_TPLINK_PORT"):
        monkeypatch.delenv(key, raising=False)


def test_version() -> None:
    out = io.StringIO()
    assert main(["version"], out=out) == 0
    assert out.getvalue().strip() == __version__


def test_module_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["equipment-tplink", "version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("equipment_tplink", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_openapi_stdout_and_file(tmp_path: Path) -> None:
    out = io.StringIO()
    assert main(["openapi"], out=out) == 0
    assert yaml.safe_load(out.getvalue())["openapi"] == "3.1.0"
    target = tmp_path / "spec.json"
    assert main(["openapi", "--format", "json", "-o", str(target)]) == 0
    assert target.read_text().startswith("{")


def test_serve_uses_settings_and_overrides(tmp_path: Path) -> None:
    env = tmp_path / "custom.env"
    env.write_text("EQUIPMENT_TPLINK_PORT=9000\n")
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    assert (
        main(
            ["serve", "--env-file", str(env), "--fake", "--host", "127.0.0.1"],
            run_server=fake_run,
        )
        == 0
    )
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9000
    assert calls[0]["access_log"] is False
    assert calls[0]["app"].state.world is not None


def test_bad_env_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["serve", "--env-file", str(tmp_path / "missing.env")]) == 2
    assert "env file not found" in capsys.readouterr().err


def test_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    world = FakeWorld(FakeClock())
    world.accept_local_credentials = False
    world.plugs[1].model = "HS103(US)"
    monkeypatch.setattr(Discover, "discover", fake_discover(world))
    monkeypatch.setenv("TPLINK_USERNAME", "user@example.com")
    monkeypatch.setenv("TPLINK_PASSWORD", "pw")
    out = io.StringIO()
    assert main(["discover", "--timeout", "1", "--target", "192.0.2.255"], out=out) == 0
    text = out.getvalue()
    assert "00:00:5E:00:53:01" in text and "KLAP lv2" in text
    assert "failed" in text and "(unsupported model)" in text
    assert "2 device(s) on 192.0.2.255" in text
    assert "did not match our challenge" in text


def test_openapi_ignores_broken_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUIPMENT_TPLINK_PORT", "not-a-port")
    out = io.StringIO()
    assert main(["openapi"], out=out) == 0
    assert main(["serve"], run_server=lambda *a, **k: None) == 2
