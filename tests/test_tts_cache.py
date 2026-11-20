"""The speech cache: canonical keys, safe paths, atomic writes and pruning.

Fully offline and deterministic: the clock is injected wherever aging matters,
and every renderer is a local async function.

Owning ticket: HSD-bw8h.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from chime_deck.tts import TtsCache, TtsOptions, cache_key, suffix_for_format

_BASE_OPTIONS = TtsOptions(
    engine_id="tts.piper",
    language="en_GB",
    voice="en_GB-alan-low",
    speed=160,
    preferred_format="wav",
)

_KEY_A = "a" * 64
_KEY_B = "b" * 64
_KEY_C = "c" * 64


class _Clock:
    """A wall clock the test advances by hand."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        """Start the clock at *now*.

        :param now: initial POSIX timestamp.
        """
        self.now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


class _CountingCache(TtsCache):
    """A cache that counts how often it pruned."""

    calls = 0

    def prune(self) -> int:
        """Count the call and prune for real."""
        self.calls += 1
        return super().prune()


def _hidden(directory: Path) -> list[str]:
    """Return the names of hidden entries in *directory*.

    :param directory: directory to inspect.
    """
    return sorted(path.name for path in directory.iterdir() if path.name.startswith("."))


async def _write_clip(destination: Path) -> None:
    """Write a small, valid-looking payload at *destination*.

    :param destination: path the cache asks the renderer to fill.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"RIFF....WAVEfmt ")


def test_cache_key_is_stable_for_identical_inputs() -> None:
    """The same engine, options and text always produce the same key."""
    assert cache_key("espeak_ng", _BASE_OPTIONS, "hello") == cache_key(
        "espeak_ng", _BASE_OPTIONS, "hello"
    )
    assert len(cache_key("espeak_ng", _BASE_OPTIONS, "hello")) == 64


def test_cache_key_changes_with_engine_and_text() -> None:
    """The engine name and the text are both part of the key."""
    base = cache_key("espeak_ng", _BASE_OPTIONS, "hello")

    assert cache_key("piper", _BASE_OPTIONS, "hello") != base
    assert cache_key("espeak_ng", _BASE_OPTIONS, "goodbye") != base


@pytest.mark.parametrize(
    "options",
    [
        TtsOptions(
            engine_id="tts.cloud",
            language="en_GB",
            voice="en_GB-alan-low",
            speed=160,
            preferred_format="wav",
        ),
        TtsOptions(
            engine_id="tts.piper",
            language="en_US",
            voice="en_GB-alan-low",
            speed=160,
            preferred_format="wav",
        ),
        TtsOptions(
            engine_id="tts.piper",
            language="en_GB",
            voice="en_GB-susan-low",
            speed=160,
            preferred_format="wav",
        ),
        TtsOptions(
            engine_id="tts.piper",
            language="en_GB",
            voice="en_GB-alan-low",
            speed=200,
            preferred_format="wav",
        ),
        TtsOptions(
            engine_id="tts.piper",
            language="en_GB",
            voice="en_GB-alan-low",
            speed=160,
            preferred_format="mp3",
        ),
    ],
)
def test_cache_key_changes_with_each_option(options: TtsOptions) -> None:
    """Changing any single option produces a different key.

    :param options: base options with exactly one field changed.
    """
    assert cache_key("espeak_ng", options, "hello") != cache_key(
        "espeak_ng", _BASE_OPTIONS, "hello"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("wav", ".wav"), ("MP3", ".mp3"), ("opus", ".opus"), ("", ".wav"), ("  ", ".wav")],
)
def test_suffix_for_format_normalises_the_token(value: str, expected: str) -> None:
    """Format tokens are trimmed, lowercased and dotted.

    :param value: configured format token.
    :param expected: dotted path suffix.
    """
    assert suffix_for_format(value) == expected


@pytest.mark.parametrize("value", ["../x", "a/b", ".wav!", "waytoolongformat"])
def test_suffix_for_format_rejects_traversal(value: str) -> None:
    """Only short alphanumeric tokens can become a path suffix.

    :param value: hostile format token.
    """
    with pytest.raises(ValueError, match="unsupported audio format"):
        suffix_for_format(value)


@pytest.mark.parametrize("key", ["", "not-a-key", "g" * 64, "a" * 63, "a" * 65, "A" * 64])
def test_path_for_rejects_a_malformed_key(tmp_path: Path, key: str) -> None:
    """A key must be exactly 64 lowercase hex characters.

    :param tmp_path: pytest temporary directory.
    :param key: malformed cache key.
    """
    cache = TtsCache(tmp_path / "cache")

    with pytest.raises(ValueError, match="invalid speech cache key"):
        cache.path_for(key, ".wav")


@pytest.mark.parametrize("suffix", ["../x", ".wav/../x", ".toolongformat", ".w v"])
def test_path_for_rejects_a_traversal_suffix(tmp_path: Path, suffix: str) -> None:
    """The suffix cannot escape the cache directory.

    :param tmp_path: pytest temporary directory.
    :param suffix: hostile suffix.
    """
    cache = TtsCache(tmp_path / "cache")

    with pytest.raises(ValueError, match="unsupported audio suffix"):
        cache.path_for(_KEY_A, suffix)


@pytest.mark.asyncio
async def test_miss_then_hit_renders_once(tmp_path: Path) -> None:
    """A cold cache renders; the second read is served from disk.

    :param tmp_path: pytest temporary directory.
    """
    cache = TtsCache(tmp_path / "cache")
    calls = 0

    async def render(destination: Path) -> None:
        """Record the render and write a payload.

        :param destination: temporary path to fill.
        """
        nonlocal calls
        calls += 1
        await _write_clip(destination)

    first = await cache.get_or_render(_KEY_A, ".wav", render)

    assert first == cache.root / f"{_KEY_A}.wav"
    assert first.read_bytes() == b"RIFF....WAVEfmt "
    assert calls == 1

    second = await cache.get_or_render(_KEY_A, ".wav", render)

    assert second == first
    assert calls == 1
    assert _hidden(cache.root) == []


@pytest.mark.asyncio
async def test_zero_byte_stub_is_a_miss(tmp_path: Path) -> None:
    """A zero-byte file is treated as absent and replaced by a real render.

    :param tmp_path: pytest temporary directory.
    """
    cache = TtsCache(tmp_path / "cache")
    stub = cache.path_for(_KEY_B, ".wav")
    stub.write_bytes(b"")
    calls = 0

    async def render(destination: Path) -> None:
        """Write a payload for the missing clip.

        :param destination: temporary path to fill.
        """
        nonlocal calls
        calls += 1
        await _write_clip(destination)

    path = await cache.get_or_render(_KEY_B, ".wav", render)

    assert path.read_bytes() == b"RIFF....WAVEfmt "
    assert calls == 1


@pytest.mark.asyncio
async def test_a_failed_render_leaves_no_partial_file(tmp_path: Path) -> None:
    """A renderer that raises after writing is cleaned up completely.

    :param tmp_path: pytest temporary directory.
    """
    cache = TtsCache(tmp_path / "cache")

    async def render(destination: Path) -> None:
        """Write a partial payload, then fail.

        :param destination: temporary path to fill.
        """
        destination.write_bytes(b"partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await cache.get_or_render(_KEY_A, ".wav", render)

    assert not (cache.root / f"{_KEY_A}.wav").exists()
    assert _hidden(cache.root) == []


def test_prune_keeps_the_newest_files(tmp_path: Path) -> None:
    """Pruning by file count deletes the oldest entries first.

    :param tmp_path: pytest temporary directory.
    """
    root = tmp_path / "cache"
    cache = TtsCache(root, max_files=3)
    for index in range(5):
        path = root / f"file{index}.wav"
        path.write_bytes(b"x" * 10)
        os.utime(path, (1_000_000.0 + index, 1_000_000.0 + index))

    removed = cache.prune()

    assert removed == 2
    assert sorted(path.name for path in root.iterdir()) == [
        "file2.wav",
        "file3.wav",
        "file4.wav",
    ]


def test_prune_enforces_the_byte_limit(tmp_path: Path) -> None:
    """Pruning by bytes deletes the oldest entries until the total fits.

    :param tmp_path: pytest temporary directory.
    """
    root = tmp_path / "cache"
    cache = TtsCache(root, max_files=10, max_bytes=25)
    for index in range(4):
        path = root / f"file{index}.wav"
        path.write_bytes(b"x" * 10)
        os.utime(path, (1_000_000.0 + index, 1_000_000.0 + index))

    removed = cache.prune()

    assert removed == 2
    assert len(list(root.iterdir())) == 2


def test_prune_retires_only_stale_temporary_files(tmp_path: Path) -> None:
    """An old hidden temporary file is removed; a fresh hidden one is kept.

    A hidden temporary file is never an LRU entry, so even a zero-file budget
    does not count it.

    :param tmp_path: pytest temporary directory.
    """
    root = tmp_path / "cache"
    cache = TtsCache(root, max_files=100)
    stale = root / ".x.tmp.wav"
    stale.write_bytes(b"x")
    os.utime(stale, (1_000_000.0 - 7200.0, 1_000_000.0 - 7200.0))
    fresh = root / ".fresh.tmp.wav"
    fresh.write_bytes(b"x")

    removed = cache.prune()

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert TtsCache(root, max_files=0).prune() == 0
    assert fresh.exists()


def test_prune_tolerates_a_deleted_root(tmp_path: Path) -> None:
    """A cache directory deleted after construction prunes to nothing.

    :param tmp_path: pytest temporary directory.
    """
    cache = TtsCache(tmp_path / "cache")

    shutil.rmtree(cache.root)

    assert cache.prune() == 0


@pytest.mark.asyncio
async def test_prune_on_a_miss_is_throttled(tmp_path: Path) -> None:
    """Misses prune at most once a minute, so a minting template cannot hammer it.

    :param tmp_path: pytest temporary directory.
    """
    clock = _Clock()
    cache = _CountingCache(tmp_path / "cache", clock=clock)

    async def render(destination: Path) -> None:
        """Write a payload for the missing clip.

        :param destination: temporary path to fill.
        """
        await _write_clip(destination)

    await cache.get_or_render(_KEY_A, ".wav", render)
    await cache.get_or_render(_KEY_B, ".wav", render)

    assert cache.calls == 1

    clock.now += 61.0
    await cache.get_or_render(_KEY_C, ".wav", render)

    assert cache.calls == 2


@pytest.mark.asyncio
async def test_a_hit_never_prunes(tmp_path: Path) -> None:
    """Only a miss prunes; a cache hit leaves the prune counter untouched.

    :param tmp_path: pytest temporary directory.
    """
    clock = _Clock()
    cache = _CountingCache(tmp_path / "cache", clock=clock)

    async def render(destination: Path) -> None:
        """Write a payload for the missing clip.

        :param destination: temporary path to fill.
        """
        await _write_clip(destination)

    await cache.get_or_render(_KEY_A, ".wav", render)

    assert cache.calls == 1

    # Move past the miss-prune throttle so a hit that incorrectly pruned
    # could not hide behind it.
    clock.now += 61.0
    await cache.get_or_render(_KEY_A, ".wav", render)

    assert cache.calls == 1
