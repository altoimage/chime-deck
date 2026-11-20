"""URL downloads: streaming limits, content-type policy, naming and dedupe.

Every test uses ``httpx.MockTransport`` through an injected ``AsyncClient``, so
no socket is opened. The duration probe is always a fake; the suite-wide
``no_real_ffprobe`` fixture is the second line of defence.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio

from chime_deck.sounds import (
    DOWNLOAD_TIMEOUT_S,
    MAX_DOWNLOAD_BYTES,
    SoundDownloader,
    SoundDownloadError,
    SoundErrorCode,
    SoundLibrary,
)
from chime_deck.store import ConfigEvent, ConfigStore, SoundIn

pytestmark = pytest.mark.asyncio

Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
)

_AUDIO_TYPE = {"content-type": "audio/mpeg"}


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


@pytest.fixture
def sounds(tmp_path: Path) -> Path:
    """Return the (not yet created) library directory for this test.

    :param tmp_path: pytest temporary directory.
    """
    return tmp_path / "sounds"


@pytest_asyncio.fixture
async def clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    """Yield a list that tracks borrowed clients and closes them at teardown."""
    created: list[httpx.AsyncClient] = []
    yield created
    for client in created:
        await client.aclose()


class FixedProbe:
    """A duration probe that records calls and returns a fixed value."""

    def __init__(self, duration_ms: int | None = 500) -> None:
        """Create the probe.

        :param duration_ms: value every call returns.
        """
        self.duration_ms = duration_ms
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> int | None:
        """Record the path and return the configured duration.

        :param path: file being probed.
        """
        self.calls.append(path)
        return self.duration_ms


def _make_client(handler: Handler, sink: list[httpx.AsyncClient]) -> httpx.AsyncClient:
    """Create a mock-transport client and remember it for teardown.

    :param handler: MockTransport handler.
    :param sink: fixture list that closes clients after the test.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink.append(client)
    return client


def _downloader(
    store: ConfigStore,
    directory: Path,
    client: httpx.AsyncClient,
    *,
    probe: FixedProbe | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> SoundDownloader:
    """Create a downloader sharing one fresh lock and the given client.

    :param store: connected store.
    :param directory: library directory.
    :param client: borrowed mock client.
    :param probe: fake duration probe.
    :param timeout_s: whole-request deadline.
    :param max_bytes: streamed-byte cap.
    """
    return SoundDownloader(
        store,
        directory,
        lock=asyncio.Lock(),
        client=client,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        probe=probe if probe is not None else FixedProbe(),
    )


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every event currently queued.

    :param queue: queue subscribed to the store's event bus.
    """
    events: list[ConfigEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _digest(payload: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of *payload*.

    :param payload: bytes to hash.
    """
    return hashlib.sha256(payload).hexdigest()


async def test_success_creates_row_and_file(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A normal download stores the bytes, metadata and URL exactly once."""
    payload = b"ID3-a-tiny-mp3"
    probe = FixedProbe(650)

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a small audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients), probe=probe)
    result = await downloader.download("https://example.test/ding.mp3")

    assert result.created is True
    row = result.sound
    assert row.name == "ding"
    assert row.source_type == "url"
    assert row.path == "ding.mp3"
    assert row.url == "https://example.test/ding.mp3"
    assert row.sha256 == _digest(payload)
    assert row.format == "mp3"
    assert row.bytes == len(payload)
    assert row.duration_ms == 650
    assert row.missing is False
    assert (sounds / "ding.mp3").read_bytes() == payload
    assert probe.calls == [sounds / "ding.mp3"]
    assert len(await store.list_sounds()) == 1


async def test_identical_redownload_is_a_true_no_op(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """The same URL with identical bytes rewrites nothing and emits no event."""
    payload = b"ID3-unchanged"
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the same audio response every time."""
        calls["count"] += 1
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    first = await downloader.download("https://example.test/ding.mp3")
    queue = store.events.subscribe_queue()
    path = sounds / cast(str, first.sound.path)
    before = path.stat().st_mtime_ns
    before_sha = _digest(path.read_bytes())
    _drain(queue)
    # Let the filesystem tick so an accidental rewrite would be visible.
    await asyncio.sleep(0.01)

    second = await downloader.download("https://example.test/ding.mp3")

    assert second.created is False
    assert second.sound.id == first.sound.id
    assert len(await store.list_sounds()) == 1
    assert [entry.name for entry in sounds.iterdir()] == ["ding.mp3"]
    assert path.stat().st_mtime_ns == before
    assert _digest(path.read_bytes()) == before_sha
    assert _drain(queue) == []
    assert calls["count"] == 2


async def test_changed_bytes_replace_the_same_row_and_file(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A changed payload keeps the row id and path but replaces the content."""
    payloads = [b"ID3-first", b"ID3-second-and-longer"]
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the next payload."""
        payload = payloads[min(calls["count"], len(payloads) - 1)]
        calls["count"] += 1
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    first = await downloader.download("https://example.test/ding.mp3")
    second = await downloader.download("https://example.test/ding.mp3")

    assert second.created is False
    assert second.sound.id == first.sound.id
    assert second.sound.path == first.sound.path == "ding.mp3"
    assert second.sound.sha256 == _digest(payloads[1])
    assert second.sound.bytes == len(payloads[1])
    assert (sounds / "ding.mp3").read_bytes() == payloads[1]
    assert len(await store.list_sounds()) == 1
    assert [entry.name for entry in sounds.iterdir()] == ["ding.mp3"]


async def test_identical_bytes_at_another_url_reuse_the_url_row(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A second URL with the same bytes returns the first URL row, uncopied."""
    payload = b"ID3-shared-bytes"

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the same audio response for every URL."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    first = await downloader.download("https://one.test/a.mp3")
    second = await downloader.download("https://two.test/b.mp3")

    assert second.created is False
    assert second.sound.id == first.sound.id
    assert second.sound.url == "https://one.test/a.mp3"
    assert len(await store.list_sounds()) == 1
    assert [entry.name for entry in sounds.iterdir()] == ["a.mp3"]


async def test_a_scanned_file_row_is_never_reused(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """Hash reuse is restricted to URL rows; a local file stays its own row."""
    payload = b"ID3-local-bytes"
    sounds.mkdir()
    (sounds / "local.mp3").write_bytes(payload)
    await store.create_sound(
        SoundIn(
            name="local",
            source_type="file",
            path="local.mp3",
            format="mp3",
            bytes=len(payload),
            sha256=_digest(payload),
        )
    )
    library = SoundLibrary(store, sounds, probe=FixedProbe())
    await library.scan()

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return bytes identical to the scanned local file."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    result = await downloader.download("https://example.test/remote.mp3")

    assert result.created is True
    assert result.sound.source_type == "url"
    assert result.sound.sha256 == _digest(payload)
    assert len(await store.list_sounds()) == 2


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/html", SoundErrorCode.SOUND_NOT_AUDIO),
        ("application/json", SoundErrorCode.SOUND_NOT_AUDIO),
        ("audio/opus", SoundErrorCode.SOUND_FORMAT_UNSUPPORTED),
    ],
)
async def test_non_audio_and_unsupported_types_leave_nothing_behind(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    content_type: str,
    expected: SoundErrorCode,
) -> None:
    """A rejected media type creates no row, file or temp file."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the configured media type."""
        return httpx.Response(200, headers={"content-type": content_type}, content=b"payload")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/clip.mp3")

    assert caught.value.code is expected
    assert "wav" in caught.value.message
    assert "mp3" in caught.value.message
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_text_html_message_names_the_problem_and_the_formats(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """The user is told the server sent a web page and to link an audio file."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return an HTML page."""
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>"
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/page")

    assert caught.value.code is SoundErrorCode.SOUND_NOT_AUDIO
    message = caught.value.message
    assert "text/html" in message
    assert "not audio" in message
    assert "wav" in message and "flac" in message


async def test_octet_stream_uses_the_url_extension(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A neutral content type falls back to the URL's suffix."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a neutral content type."""
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"ID3"
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    result = await downloader.download("https://example.test/clip.mp3")

    assert result.sound.format == "mp3"
    assert result.sound.path == "clip.mp3"


async def test_octet_stream_without_any_suffix_is_unsupported(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """With no extension anywhere the format cannot be known."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a neutral content type and no useful URL."""
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"data"
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/download")

    assert caught.value.code is SoundErrorCode.SOUND_FORMAT_UNSUPPORTED
    assert "name ending" in caught.value.message


async def test_audio_mpeg_at_a_txt_url_is_stored_as_mp3(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """The content type beats a misleading URL extension."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return MPEG audio with a ``.txt`` URL."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    result = await downloader.download("https://example.test/foo.txt")

    assert result.sound.format == "mp3"
    assert result.sound.path == "foo.mp3"
    assert (sounds / "foo.mp3").is_file()


async def test_oversized_stream_is_rejected_and_cleaned_up(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """The running byte count caps a stream larger than the limit."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a body larger than the configured limit."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"x" * 2048)

    downloader = _downloader(store, sounds, _make_client(handler, clients), max_bytes=1024)

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/big.mp3")

    assert caught.value.code is SoundErrorCode.SOUND_TOO_LARGE
    assert "limit" in caught.value.message
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_a_lying_content_length_cannot_bypass_the_cap(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A small declared length does not disable the streamed-byte cap."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Claim 100 bytes and send 2048."""
        return httpx.Response(
            200,
            headers={**_AUDIO_TYPE, "content-length": "100"},
            content=b"x" * 2048,
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients), max_bytes=1024)

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/big.mp3")

    assert caught.value.code is SoundErrorCode.SOUND_TOO_LARGE
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_declared_content_length_over_the_cap_is_an_early_reject(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A clearly oversized Content-Length is rejected before the body is read."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Declare a body far over the limit and send none of it."""
        return httpx.Response(
            200,
            headers={**_AUDIO_TYPE, "content-length": "9999"},
            content=b"",
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients), max_bytes=1024)

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/big.mp3")

    assert caught.value.code is SoundErrorCode.SOUND_TOO_LARGE
    assert list(sounds.iterdir()) == []


async def test_redirects_are_not_followed(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A redirect is rejected so the stored URL is the real one."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Answer with a redirect."""
        return httpx.Response(302, headers={"location": "https://elsewhere.test/x.mp3"})

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/moved.mp3")

    assert caught.value.code is SoundErrorCode.INVALID_URL
    assert "redirect" in caught.value.message


async def test_server_error_is_a_download_failure(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """An HTTP error status names the status and nothing private."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Answer with a server error."""
        return httpx.Response(500)

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/ding.mp3")

    assert caught.value.code is SoundErrorCode.DOWNLOAD_FAILED
    assert "500" in caught.value.message
    assert "https://example.test" not in caught.value.message


async def test_empty_response_is_a_download_failure(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A zero-byte body is not a sound."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Answer 200 with no body."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/empty.mp3")

    assert caught.value.code is SoundErrorCode.DOWNLOAD_FAILED
    assert "empty" in caught.value.message


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/y.mp3",
        "http://user:pass@example.test/a.mp3",
        "not-a-url",
    ],
)
async def test_hostile_urls_are_rejected_before_any_request(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    url: str,
) -> None:
    """Non-http schemes, credentials and nonsense never reach the transport."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any request that should never happen."""
        calls.append(str(request.url))
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download(url)

    assert caught.value.code is SoundErrorCode.INVALID_URL
    assert calls == []
    assert await store.list_sounds() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test:%zz/a.mp3",
        "http://example.test/a\x00.mp3",
    ],
)
async def test_urls_httpx_cannot_build_are_invalid_urls(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    url: str,
) -> None:
    """A bad port or non-printable character is a 422, never a 500."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any request that should never happen."""
        calls.append(str(request.url))
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download(url)

    assert caught.value.code is SoundErrorCode.INVALID_URL
    assert "well-formed" in caught.value.message
    assert calls == []
    assert await store.list_sounds() == []
    assert not sounds.exists() or list(sounds.iterdir()) == []


async def test_surrounding_whitespace_is_stripped(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A pasted URL with surrounding spaces is used, not rejected."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    result = await downloader.download("  https://example.test/a.mp3  ")

    assert result.created is True
    assert result.sound.url == "https://example.test/a.mp3"


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "../x.wav", ".hidden", "a/b.wav", "x" * 300],
)
async def test_hostile_names_cannot_escape_the_library(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    name: str,
) -> None:
    """Every derived name stays a single file inside the library directory."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))
    result = await downloader.download("https://example.test/original.mp3", name=name)

    relative = cast(str, result.sound.path)
    target = (sounds / relative).resolve()
    assert target.is_relative_to(sounds.resolve())
    assert target.is_file()
    assert "/" not in relative
    assert len(Path(relative).stem) <= 80


@pytest.mark.parametrize("name", ["", "..."])
async def test_unusable_names_are_rejected(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    name: str,
) -> None:
    """An empty or all-punctuation name has no safe stem."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/original.mp3", name=name)

    assert caught.value.code is SoundErrorCode.INVALID_NAME
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_an_exception_group_with_an_overflow_is_an_invalid_url(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A connect-time OverflowError (port outside 0-65535) is a 422-class error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Raise the shape anyio produces for an out-of-range port."""
        raise ExceptionGroup("connect failed", [OverflowError("port must be 0-65535")])

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("http://example.test/a.mp3")

    assert caught.value.code is SoundErrorCode.INVALID_URL
    assert "well-formed" in caught.value.message
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_an_exception_group_with_a_connect_error_is_a_download_failure(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A refused or unreachable host remains a transfer failure."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Raise the shape anyio produces for a refused connection."""
        raise ExceptionGroup("connect failed", [httpx.ConnectError("connection refused")])

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/a.mp3")

    assert caught.value.code is SoundErrorCode.DOWNLOAD_FAILED
    assert await store.list_sounds() == []


async def test_a_mixed_group_prefers_the_domain_error(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A teardown error grouped with our own error must not hide the real reason."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Group a domain error with a socket teardown error."""
        raise ExceptionGroup(
            "transfer failed",
            [
                SoundDownloadError(SoundErrorCode.SOUND_TOO_LARGE, "too big"),
                ConnectionResetError("reset"),
            ],
        )

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/a.mp3")

    assert caught.value.code is SoundErrorCode.SOUND_TOO_LARGE
    assert caught.value.message == "too big"
    assert list(sounds.iterdir()) == []


async def test_mid_stream_failure_leaves_no_temp_or_row(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A body that fails halfway is a download failure with full cleanup."""

    async def broken_body() -> AsyncIterator[bytes]:
        """Yield one chunk and then fail like a dropped connection."""
        yield b"ID3"
        raise httpx.ReadError("connection lost")

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a response whose body fails while streaming."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=broken_body())

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/ding.mp3")

    assert caught.value.code is SoundErrorCode.DOWNLOAD_FAILED
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_whole_request_timeout_is_enforced(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """A slow server is abandoned at the request deadline and cleaned up."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        """Wait longer than the configured deadline."""
        await asyncio.sleep(0.2)
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients), timeout_s=0.05)

    with pytest.raises(SoundDownloadError) as caught:
        await downloader.download("https://example.test/slow.mp3")

    assert caught.value.code is SoundErrorCode.DOWNLOAD_FAILED
    assert await store.list_sounds() == []
    assert list(sounds.iterdir()) == []


async def test_borrowed_client_is_never_closed(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """The downloader closes only clients it created itself."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    client = _make_client(handler, clients)
    downloader = _downloader(store, sounds, client)

    await downloader.download("https://example.test/ding.mp3")

    assert not client.is_closed


async def test_owned_client_is_closed(
    store: ConfigStore, sounds: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client created for one download is closed before it returns."""
    created: list[httpx.AsyncClient] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=b"ID3")

    real_client = httpx.AsyncClient

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        """Build a mock-transport client in place of the real constructor."""
        client = real_client(transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    monkeypatch.setattr("chime_deck.sounds.download.httpx.AsyncClient", factory)
    downloader = SoundDownloader(
        store,
        sounds,
        lock=asyncio.Lock(),
        probe=FixedProbe(),
    )

    result = await downloader.download("https://example.test/ding.mp3")

    assert result.created is True
    assert len(created) == 1
    assert created[0].is_closed


async def test_concurrent_same_url_downloads_publish_one_row(
    store: ConfigStore, sounds: Path, clients: list[httpx.AsyncClient]
) -> None:
    """Two simultaneous downloads of one URL create one row and one file."""
    payload = b"ID3-concurrent"

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the same audio response for both requests."""
        return httpx.Response(200, headers=_AUDIO_TYPE, content=payload)

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    first, second = await asyncio.gather(
        downloader.download("https://example.test/ding.mp3"),
        downloader.download("https://example.test/ding.mp3"),
    )

    assert {first.created, second.created} == {True, False}
    assert len(await store.list_sounds()) == 1
    assert [entry.name for entry in sounds.iterdir()] == ["ding.mp3"]


@pytest.mark.parametrize(
    ("name", "content_type", "expected"),
    [
        ("ding.mp3", "audio/mpeg", "ding.mp3"),
        ("DING.WAV", "audio/x-wav", "DING.wav"),
        ("chime.ogg", "audio/ogg", "chime.ogg"),
        ("alert.flac", "audio/flac", "alert.flac"),
    ],
)
async def test_supported_name_suffix_is_not_duplicated(
    store: ConfigStore,
    sounds: Path,
    clients: list[httpx.AsyncClient],
    name: str,
    content_type: str,
    expected: str,
) -> None:
    """A user name ending in a supported suffix is stored with one extension."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a tiny audio response of the requested type."""
        return httpx.Response(200, headers={"content-type": content_type}, content=b"ID3")

    downloader = _downloader(store, sounds, _make_client(handler, clients))

    result = await downloader.download("https://example.test/original", name=name)

    assert result.sound.path == expected
    assert result.sound.name == Path(expected).stem
    assert [entry.name for entry in sounds.iterdir()] == [expected]
