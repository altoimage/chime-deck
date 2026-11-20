"""Smoke tests: the package is installed and the console script is wired up."""

import asyncio
import logging
import re
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from chime_deck import __version__
from chime_deck.cli import main


def test_version_looks_like_a_release() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__) is not None


def test_console_script_is_registered() -> None:
    scripts = {
        entry_point.name: entry_point.value for entry_point in entry_points(group="console_scripts")
    }
    assert scripts["chime-deck"] == "chime_deck.cli:main"


def test_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "usage: chime-deck" in capsys.readouterr().out


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"chime-deck {__version__}"


def test_unknown_option_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--nope"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_bare_invocation_starts_the_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded: list[uvicorn.Config] = []

    def fake_serve(configs: list[uvicorn.Config]) -> int:
        recorded.extend(configs)
        return 0

    monkeypatch.setattr("chime_deck.serving.serve", fake_serve)
    monkeypatch.setenv("CHIMEDECK_DATA_DIR", str(tmp_path))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    try:
        assert main([]) == 0
    finally:
        root_logger.setLevel(previous_level)

    # Two listeners by default: the doorbell on loopback, and the
    # configuration-only admin listener next to it.
    assert len(recorded) == 2
    doorbell, admin = recorded
    assert isinstance(doorbell.app, FastAPI)
    assert doorbell.host == "127.0.0.1"
    assert doorbell.port == 8080
    assert doorbell.lifespan == "auto"
    assert admin.lifespan == "off"


@pytest.mark.asyncio
async def test_asyncio_plugin_is_active() -> None:
    assert asyncio.get_running_loop().is_running()
