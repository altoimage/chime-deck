"""The bundled demo set ships four formats and an honest licence file.

These tests read the package data that will be installed on the device, so they
also prove the files travel inside ``src/chime_deck`` rather than relying on
the repository layout.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

from pathlib import Path

from chime_deck.sounds import SUPPORTED_FORMATS, bundled_demo_sounds_dir

_LICENCE = "LICENSES.md"

_FORBIDDEN_SUFFIXES = frozenset({"m4a", "aac", "wma", "opus", "oga", "aiff", "wmv"})


def _sounds_dir() -> Path:
    """Return the bundled directory, failing the test when it is absent."""
    directory = bundled_demo_sounds_dir()
    assert directory is not None
    return directory


def test_bundled_directory_exists_and_is_package_relative() -> None:
    """The demo directory is found through the installed package."""
    directory = _sounds_dir()

    assert directory.is_dir()
    assert "chime_deck" in directory.parts


def test_every_format_has_a_non_empty_demo() -> None:
    """At least one non-empty file exists for each supported format."""
    directory = _sounds_dir()
    by_suffix: dict[str, list[Path]] = {}

    for path in directory.iterdir():
        if path.is_file():
            by_suffix.setdefault(path.suffix.lower().lstrip("."), []).append(path)

    for fmt in SUPPORTED_FORMATS:
        files = by_suffix.get(fmt, [])
        assert files, f"no bundled demo for {fmt}"
        assert any(path.stat().st_size > 0 for path in files), f"empty {fmt} demo"


def test_no_unsupported_audio_format_is_bundled() -> None:
    """No file uses a format the scanner would silently ignore."""
    directory = _sounds_dir()

    suffixes = {path.suffix.lower().lstrip(".") for path in directory.iterdir() if path.is_file()}
    assert not (suffixes & _FORBIDDEN_SUFFIXES)


def test_every_audio_file_is_named_demo_something() -> None:
    """All bundled audio names start with ``demo_`` so they are never a user name."""
    directory = _sounds_dir()

    audio = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower().lstrip(".") in SUPPORTED_FORMATS
    ]
    assert audio
    for path in audio:
        assert path.stem.startswith("demo_"), path.name


def test_licence_file_is_present_and_complete() -> None:
    """The licence names CC0, the public domain and every bundled audio file."""
    directory = _sounds_dir()
    licence = directory / _LICENCE

    assert licence.is_file()
    text = licence.read_text(encoding="utf-8")
    assert "CC0" in text
    assert "public domain" in text.lower()
    for path in directory.iterdir():
        if path.is_file() and path.name != _LICENCE:
            assert path.name in text, f"{path.name} is missing from {_LICENCE}"
