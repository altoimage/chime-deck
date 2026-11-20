"""HTTP(S) downloads into the sound library, with honest limits.

A download is streamed to a hidden temporary file, hashed as it arrives and
published under the library's write lock. Nothing is read into memory: the
running byte count is authoritative, so a server that lies about
``Content-Length`` still cannot fill the disk or the device's RAM. The publish
phase is the only part that takes the lock, and it is milliseconds of work; the
store's own per-transaction lock is separate and shorter still.

Temporary files use the same hidden sibling shape as
:mod:`chime_deck.tts.cache`, so a crash leaves nothing a scan could mistake
for a sound. A temp left by a killed process is skipped by the scanner forever
and is **not** pruned by this ticket (known nit, harmless while it is tiny).

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..store import UNSET, ConfigStore, Sound, SoundIn, SoundUpdate, StoreConflict
from .errors import SoundDownloadError, SoundErrorCode
from .library import SUPPORTED_FORMATS, DurationProbe, _normalise_stored_path
from .probe import probe_duration_ms

__all__ = [
    "CHUNK_BYTES",
    "CONTENT_TYPE_FORMATS",
    "DOWNLOAD_CONNECT_TIMEOUT_S",
    "DOWNLOAD_READ_TIMEOUT_S",
    "DOWNLOAD_TIMEOUT_S",
    "MAX_DOWNLOAD_BYTES",
    "MAX_NAME_ATTEMPTS",
    "MAX_NAME_STEM",
    "MAX_PUBLISH_ATTEMPTS",
    "OCTET_STREAM_TYPES",
    "DownloadResult",
    "SoundDownloader",
]

_LOGGER = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES: Final = 20 * 1024 * 1024
"""Hard cap on a download, enforced on the running streamed byte count.

A ``Content-Length`` header is only a hint: checking it early rejects an
obviously large file, but it is never trusted because a lying server could
otherwise make the device write gigabytes to disk and exhaust its RAM while
buffering.
"""

DOWNLOAD_TIMEOUT_S: Final = 30.0
"""Whole-request deadline; a download that takes longer is abandoned."""

DOWNLOAD_READ_TIMEOUT_S: Final = 10.0
"""Longest gap allowed between two reads from the server."""

DOWNLOAD_CONNECT_TIMEOUT_S: Final = 5.0
"""Longest allowed time to connect or to wait for a pool slot."""

CHUNK_BYTES: Final = 64 * 1024
"""Bytes requested per stream iteration."""

MAX_NAME_STEM: Final = 80
"""Longest derived file-name stem; the suffix and ``-999`` may extend it."""

MAX_NAME_ATTEMPTS: Final = 999
"""How many ``stem-N`` variants are tried before the name is rejected."""

MAX_PUBLISH_ATTEMPTS: Final = 5
"""How many times a name taken between listing and insert is retried."""

_MAX_MB: Final = MAX_DOWNLOAD_BYTES // (1024 * 1024)
"""The limit in whole megabytes, for user-facing messages."""

_CONTENT_TYPE_FORMATS: Final[dict[str, str]] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}

CONTENT_TYPE_FORMATS: Final[Mapping[str, str]] = MappingProxyType(_CONTENT_TYPE_FORMATS)
"""Audio content types the doorbell can store, mapped to their extension."""

OCTET_STREAM_TYPES: Final[frozenset[str]] = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)
"""Neutral content types that fall back to a name or URL extension."""

_UNSAFE_NAME_RUNS: Final = re.compile(r"[^A-Za-z0-9._-]+")

_DOWNLOAD_FAILED_MESSAGE: Final = (
    "the download failed — check that the URL is reachable and points at an audio file"
)

_MALFORMED_URL_MESSAGE: Final = (
    "that URL cannot be downloaded — check it is a complete, well-formed http(s) address"
)

_NO_FREE_NAME: Final = (
    "the sound could not be saved because its file name is already in use — send a different name"
)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One download's outcome.

    ``created=False`` means no *new* row was created. For a first download
    whose bytes were already present under another URL (or whose URL's own row
    was refreshed because the bytes changed), the existing row is returned.
    Software that needs to know whether the bytes on disk changed must compare
    ``sha256``/``bytes`` with the value it held before the call.
    """

    sound: Sound
    created: bool


class SoundDownloader:
    """Streams HTTP(S) audio into the library and the store."""

    def __init__(
        self,
        store: ConfigStore,
        directory: Path,
        *,
        lock: asyncio.Lock,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DOWNLOAD_TIMEOUT_S,
        read_timeout_s: float = DOWNLOAD_READ_TIMEOUT_S,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        probe: DurationProbe | None = None,
    ) -> None:
        """Create a downloader.

        :param store: connected configuration store.
        :param directory: library directory downloads are published into.
        :param lock: the library's write lock, shared with :meth:`SoundLibrary.scan`.
        :param client: borrowed HTTP client; ``None`` creates and closes one per
            download. A borrowed client is never closed here.
        :param timeout_s: whole-request deadline.
        :param read_timeout_s: per-read deadline for an owned client.
        :param max_bytes: hard streamed-byte cap.
        :param probe: duration probe; ``None`` uses :func:`probe_duration_ms`.
        """
        self._store = store
        self._directory = directory
        self._lock = lock
        self._client = client
        self._timeout_s = timeout_s
        self._read_timeout_s = read_timeout_s
        self._max_bytes = max_bytes
        self._probe_file: DurationProbe = probe if probe is not None else probe_duration_ms

    async def download(self, url: str, *, name: str | None = None) -> DownloadResult:
        """Download *url* and publish it into the library.

        :param url: plain http(s) URL; redirects are not followed.
        :param name: optional file-name stem supplied by the user.
        :raises SoundDownloadError: for a rejected URL, name, format, size or
            failed transfer.
        """
        url = url.strip()
        _validate_url(url)
        self._directory.mkdir(parents=True, exist_ok=True)
        temp = self._directory / f".{uuid4().hex}.tmp"
        try:
            try:
                async with asyncio.timeout(self._timeout_s):
                    if self._client is None:
                        timeout = httpx.Timeout(
                            connect=DOWNLOAD_CONNECT_TIMEOUT_S,
                            read=self._read_timeout_s,
                            write=self._read_timeout_s,
                            pool=DOWNLOAD_CONNECT_TIMEOUT_S,
                        )
                        async with httpx.AsyncClient(
                            follow_redirects=False, timeout=timeout
                        ) as client:
                            digest, total, fmt = await self._stream(url, name, temp, client)
                    else:
                        digest, total, fmt = await self._stream(url, name, temp, self._client)
            except SoundDownloadError:
                raise
            except BaseExceptionGroup as group:
                # anyio wraps a connect-time failure (for example a numeric port
                # outside 0-65535, which raises OverflowError deep inside its
                # task group) in an ExceptionGroup. Classify the leaves so a
                # user error stays a 422/502 and never becomes a 500; anything
                # unrecognised is re-raised for the global handler.
                failure = _classify_transfer_group(group)
                if failure is None:
                    raise
                raise failure from None
            except (httpx.InvalidURL, ValueError, OverflowError):
                # httpx rejects a malformed port, a non-printable character or a
                # bad IPv6 literal itself, after our own split-based check has
                # passed. That is a submitted-value problem, not a server error.
                raise SoundDownloadError(
                    SoundErrorCode.INVALID_URL, _MALFORMED_URL_MESSAGE
                ) from None
            except (httpx.HTTPError, TimeoutError, OSError):
                raise SoundDownloadError(
                    SoundErrorCode.DOWNLOAD_FAILED, _DOWNLOAD_FAILED_MESSAGE
                ) from None
            return await self._publish(
                url=url,
                name=name,
                temp=temp,
                digest=digest,
                total=total,
                fmt=fmt,
            )
        finally:
            with suppress(OSError):
                temp.unlink()

    async def _stream(
        self,
        url: str,
        name: str | None,
        temp: Path,
        client: httpx.AsyncClient,
    ) -> tuple[str, int, str]:
        """Stream one response into *temp* and return its hash, size and format.

        :param url: URL to request.
        :param name: optional user name, used to infer a neutral content type.
        :param temp: temporary destination file.
        :param client: client to send the request with.
        """
        digest = hashlib.sha256()
        total = 0
        async with client.stream("GET", url, follow_redirects=False) as response:
            status = response.status_code
            if 300 <= status <= 399:
                raise SoundDownloadError(
                    SoundErrorCode.INVALID_URL,
                    "the server redirects this URL; use the final address instead",
                )
            if status >= 400:
                raise SoundDownloadError(
                    SoundErrorCode.DOWNLOAD_FAILED,
                    f"the server answered HTTP {status} — check that the URL points at a "
                    "sound file",
                )

            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            fmt = _format_for(content_type, name, url)

            declared = _content_length(response.headers.get("content-length"))
            if declared is not None and declared > self._max_bytes:
                raise SoundDownloadError(
                    SoundErrorCode.SOUND_TOO_LARGE,
                    f"the server says the file is larger than the {_MAX_MB} MB limit — "
                    "use a shorter sound file or add it to data/sounds/ manually",
                )

            with temp.open("wb") as handle:
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise SoundDownloadError(
                            SoundErrorCode.SOUND_TOO_LARGE,
                            f"the download exceeded the {_MAX_MB} MB limit — use a shorter "
                            "sound file or add it to data/sounds/ manually",
                        )
                    digest.update(chunk)
                    handle.write(chunk)

        if total == 0:
            raise SoundDownloadError(
                SoundErrorCode.DOWNLOAD_FAILED, "the server returned an empty response"
            )
        return digest.hexdigest(), total, fmt

    async def _publish(
        self,
        *,
        url: str,
        name: str | None,
        temp: Path,
        digest: str,
        total: int,
        fmt: str,
    ) -> DownloadResult:
        """Move the downloaded file into place and create or update its row.

        :param url: the requested URL, preserved on the row.
        :param name: optional user-supplied name stem.
        :param temp: downloaded temporary file.
        :param digest: SHA-256 of the downloaded bytes.
        :param total: size of the downloaded bytes.
        :param fmt: audio format inferred from content type or name.
        """
        async with self._lock:
            rows = await self._store.list_sounds()
            same_url = next(
                (row for row in rows if row.source_type == "url" and row.url == url),
                None,
            )
            if same_url is not None:
                return await self._publish_same_url(
                    row=same_url,
                    url=url,
                    name=name,
                    temp=temp,
                    digest=digest,
                    total=total,
                    fmt=fmt,
                    rows=rows,
                )

            for row in rows:
                relative = None if row.path is None else _normalise_stored_path(row.path)
                if (
                    row.source_type == "url"
                    and row.sha256 == digest
                    and not row.missing
                    and relative is not None
                    and (self._directory / relative).is_file()
                ):
                    with suppress(OSError):
                        temp.unlink()
                    return DownloadResult(row, False)

            return await self._publish_new(
                url=url,
                name=name,
                temp=temp,
                digest=digest,
                total=total,
                fmt=fmt,
                rows=rows,
            )

    async def _publish_same_url(
        self,
        *,
        row: Sound,
        url: str,
        name: str | None,
        temp: Path,
        digest: str,
        total: int,
        fmt: str,
        rows: list[Sound],
    ) -> DownloadResult:
        """Publish a download whose URL already has a row.

        :param row: first row with this URL, ordered by id.
        :param url: requested URL.
        :param name: optional user-supplied name stem.
        :param temp: downloaded temporary file.
        :param digest: SHA-256 of the downloaded bytes.
        :param total: size of the downloaded bytes.
        :param fmt: inferred format.
        :param rows: all rows, for unique-name selection.
        """
        existing_relative = None if row.path is None else _normalise_stored_path(row.path)
        if (
            row.sha256 == digest
            and existing_relative is not None
            and (self._directory / existing_relative).is_file()
        ):
            with suppress(OSError):
                temp.unlink()
            return DownloadResult(row, False)

        stem = _derive_stem(name, url)
        if existing_relative is not None:
            relative = existing_relative
        else:
            relative = self._unique_relative(stem, fmt, rows)
        target = self._directory / relative
        os.replace(temp, target)
        duration = await self._probe(target)
        desired_duration = duration if duration is not None else row.duration_ms
        updated = await self._store.update_sound(
            row.id,
            SoundUpdate(
                path=relative if existing_relative is None else UNSET,
                sha256=digest if digest != row.sha256 else UNSET,
                bytes=total if total != row.bytes else UNSET,
                format=fmt if fmt != row.format else UNSET,
                duration_ms=desired_duration if desired_duration != row.duration_ms else UNSET,
                missing=False if row.missing else UNSET,
            ),
        )
        return DownloadResult(updated, False)

    async def _publish_new(
        self,
        *,
        url: str,
        name: str | None,
        temp: Path,
        digest: str,
        total: int,
        fmt: str,
        rows: list[Sound],
    ) -> DownloadResult:
        """Publish a download that no row covers, retrying name conflicts.

        :param url: requested URL.
        :param name: optional user-supplied name stem.
        :param temp: downloaded temporary file.
        :param digest: SHA-256 of the downloaded bytes.
        :param total: size of the downloaded bytes.
        :param fmt: inferred format.
        :param rows: all rows at the start of the publish phase.
        """
        stem = _derive_stem(name, url)
        relative = self._unique_relative(stem, fmt, rows)
        for _attempt in range(MAX_PUBLISH_ATTEMPTS):
            target = self._directory / relative
            os.replace(temp, target)
            duration = await self._probe(target)
            try:
                sound = await self._store.create_sound(
                    SoundIn(
                        name=stem,
                        source_type="url",
                        path=relative,
                        url=url,
                        sha256=digest,
                        format=fmt,
                        bytes=total,
                        duration_ms=duration,
                    )
                )
            except StoreConflict:
                _LOGGER.info("sound download: %s was taken while publishing; retrying", relative)
                with suppress(OSError):
                    os.replace(target, temp)
                rows = await self._store.list_sounds()
                relative = self._unique_relative(stem, fmt, rows)
                continue
            return DownloadResult(sound, True)
        raise SoundDownloadError(
            SoundErrorCode.INVALID_NAME,
            _NO_FREE_NAME,
            {"reason": "name_exhausted"},
        )

    def _unique_relative(self, stem: str, fmt: str, rows: list[Sound]) -> str:
        """Return a file name not used by a row or an existing file.

        :param stem: sanitised name stem.
        :param fmt: inferred format extension.
        :param rows: all rows at the start of the publish phase.
        :raises SoundDownloadError: if every ``stem-N`` variant is taken.
        """
        used_rows = {row.path for row in rows if row.path is not None}
        used_files = {entry.name for entry in self._directory.iterdir() if entry.is_file()}
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            relative = f"{stem}.{fmt}" if attempt == 1 else f"{stem}-{attempt}.{fmt}"
            if relative not in used_rows and relative not in used_files:
                return relative
        raise SoundDownloadError(
            SoundErrorCode.INVALID_NAME,
            f"no free file name could be made from {stem!r} — send a different name",
            {"reason": "name_exhausted"},
        )

    async def _probe(self, path: Path) -> int | None:
        """Probe a published file without blocking the event loop.

        :param path: final file to probe.
        """
        return await asyncio.to_thread(self._probe_file, path)


def _validate_url(url: str) -> None:
    """Reject anything that is not a plain http(s) URL with a host.

    Private and link-local addresses are deliberately allowed: the doorbell is a
    LAN appliance and downloading from a local NAS is a normal use case.

    :param url: submitted URL.
    :raises SoundDownloadError: if the URL cannot be downloaded.
    """
    try:
        parts = urlsplit(url)
        # Accessing ``port`` is what makes urlsplit parse and range-check it.
        port = parts.port
    except ValueError:
        raise SoundDownloadError(SoundErrorCode.INVALID_URL, _MALFORMED_URL_MESSAGE) from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SoundDownloadError(
            SoundErrorCode.INVALID_URL,
            f"only http and https URLs can be downloaded, not {scheme!r}",
        )
    if port is not None and not 1 <= port <= 65535:
        raise SoundDownloadError(SoundErrorCode.INVALID_URL, _MALFORMED_URL_MESSAGE)
    if parts.username is not None or parts.password is not None:
        raise SoundDownloadError(
            SoundErrorCode.INVALID_URL,
            "URLs with a username or password cannot be downloaded — remove the "
            "credentials and use a plain http(s) URL",
        )
    if not parts.hostname:
        raise SoundDownloadError(
            SoundErrorCode.INVALID_URL,
            "the URL has no host — use a complete http(s) address of a sound file",
        )


def _flatten_group(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    """Return every non-group leaf of *group*, in order.

    :param group: exception group raised by anyio/httpx.
    """
    leaves: list[BaseException] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            leaves.extend(_flatten_group(exc))
        else:
            leaves.append(exc)
    return leaves


def _classify_transfer_group(
    group: BaseExceptionGroup[BaseException],
) -> SoundDownloadError | None:
    """Map a transfer-time exception group onto the domain error.

    A port outside 0-65535 raises ``OverflowError`` inside anyio's task group;
    a refused connection raises ``httpx.ConnectError``. Both arrive wrapped, so
    the leaves decide whether this was a bad submitted URL or a failed transfer.
    Anything unrecognised returns ``None`` and is re-raised unchanged.

    :param group: exception group caught around the transfer.
    """
    leaves = _flatten_group(group)
    if not leaves:
        return None
    # Prefer our own domain error even if a teardown error (for example a
    # ConnectionResetError while closing the socket) is grouped with it: the
    # user-visible reason is the domain one, not "the transfer failed".
    for exc in leaves:
        if isinstance(exc, SoundDownloadError):
            return exc
    if all(isinstance(exc, (httpx.InvalidURL, ValueError, OverflowError)) for exc in leaves):
        return SoundDownloadError(SoundErrorCode.INVALID_URL, _MALFORMED_URL_MESSAGE)
    if all(isinstance(exc, (httpx.HTTPError, TimeoutError, OSError)) for exc in leaves):
        return SoundDownloadError(SoundErrorCode.DOWNLOAD_FAILED, _DOWNLOAD_FAILED_MESSAGE)
    return None


def _content_length(value: str | None) -> int | None:
    """Parse a ``Content-Length`` header, or return ``None`` when unusable.

    :param value: raw header value.
    """
    if value is None:
        return None
    try:
        declared = int(value)
    except ValueError:
        return None
    return declared if declared >= 0 else None


def _format_for(content_type: str, name: str | None, url: str) -> str:
    """Decide the stored format from the content type, then a name or URL suffix.

    :param content_type: lowercase media type with parameters removed.
    :param name: optional user-supplied name stem.
    :param url: requested URL, whose extension is the last hint.
    :raises SoundDownloadError: when the type is not audio or the format is not
        one the library supports.
    """
    if content_type in CONTENT_TYPE_FORMATS:
        return CONTENT_TYPE_FORMATS[content_type]

    if content_type and content_type not in OCTET_STREAM_TYPES:
        if content_type.startswith("audio/"):
            raise SoundDownloadError(
                SoundErrorCode.SOUND_FORMAT_UNSUPPORTED,
                f"the server returned {content_type!r}, which this doorbell cannot store — "
                "use a wav, mp3, ogg or flac file",
            )
        raise SoundDownloadError(
            SoundErrorCode.SOUND_NOT_AUDIO,
            f"the server returned {content_type!r}, which is not audio — link directly to a "
            "wav, mp3, ogg or flac file instead of a web page",
        )

    hints = [name, PurePosixPath(urlsplit(url).path).name]
    for hint in hints:
        if not hint:
            continue
        token = PurePosixPath(hint).suffix.lower().lstrip(".")
        if token in SUPPORTED_FORMATS:
            return token
    raise SoundDownloadError(
        SoundErrorCode.SOUND_FORMAT_UNSUPPORTED,
        "the download has no file extension to tell its format — use wav, mp3, ogg or "
        "flac, or send a name ending in one of them",
    )


def _derive_stem(name: str | None, url: str) -> str:
    """Return a safe file-name stem from a user name or the URL's file name.

    A URL's last suffix is dropped so ``audio/mpeg`` at ``foo.txt`` is stored as
    ``foo.mp3``. A user-supplied name has only a *supported* suffix dropped, so
    ``name="ding.mp3"`` with ``audio/mpeg`` is stored as ``ding.mp3`` rather
    than ``ding.mp3.mp3``. Every character outside ``[A-Za-z0-9._-]`` becomes a
    dash, and leading and trailing dots and dashes are removed, so no derived
    name can be empty, hidden, absolute or contain a path separator.

    :param name: user-supplied name; a supported audio suffix is stripped first.
    :param url: requested URL.
    :raises SoundDownloadError: if the result has no usable characters.
    """
    if name is not None:
        raw = name
        suffix = PurePosixPath(raw).suffix
        if suffix.lower().lstrip(".") in SUPPORTED_FORMATS:
            raw = raw[: -len(suffix)]
    else:
        raw = PurePosixPath(urlsplit(url).path).name
        suffix = PurePosixPath(raw).suffix
        if suffix:
            raw = raw[: -len(suffix)]
    stem = _UNSAFE_NAME_RUNS.sub("-", raw).strip(".-")
    stem = stem[:MAX_NAME_STEM].strip(".-")
    if not stem:
        raise SoundDownloadError(
            SoundErrorCode.INVALID_NAME,
            "the sound needs a usable name — send a name made of letters, digits, dots or dashes",
        )
    return stem
