"""The ``ha_tts`` engine: Home Assistant's own voice engines, spoken locally.

The work is split in two. :class:`~chime_deck.ha.HomeAssistantClient` owns
``POST /api/tts_get_url``: the bearer token, the retry rule and the mapping of
every HTTP failure onto a typed :class:`~chime_deck.ha.HaError`. This engine
owns everything after that answer — the *unauthenticated* ``GET`` of the
rendered file (``/api/tts_proxy/...`` takes no token, design 04 §3), the checks
that the answer really is audio, and the atomic write into the cache — so the
already-tested client keeps its single responsibility and this module never
builds an ``Authorization`` header itself.

**Timeout arithmetic.** One render is bounded by three nested budgets:
:data:`TTS_GET_URL_TIMEOUT_S` (4.5 s) for ``tts_get_url``,
:data:`TTS_DOWNLOAD_TIMEOUT_S` (2.5 s) for the download, both inside
:data:`TTS_RENDER_TOTAL_TIMEOUT_S` (7.5 s), which is itself the bound the press
path needs. An availability probe costs at most
:data:`AVAILABILITY_TIMEOUT_S` (1.0 s). The worst case for one press of a
**non-templated** phrase is therefore ``1.0 + 7.5 + 0.5 = 9.0`` s, which is
below the 10 s seam budget of
:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S` (``mqtt/bridge.py``)
with :data:`PRESS_MARGIN_S` (0.5 s) to spare. A **templated** phrase adds
:data:`~chime_deck.tts.templating.TEMPLATE_TIMEOUT_S` (0.5 s) before the
engine runs, for 9.5 s; that figure is scoped to one ``ha_tts`` press, not to a
whole press. The per-engine numbers and their arithmetic are unchanged and
still describe the budget *inside* one engine — but the ruling that once stood
on them is deliberately reversed: HSD-p50k ruled there is no render-failure
fallback because a second attempt could not fit the seam, and **HSD-96k6**
supersedes that ruling by giving the press path an absolute press-wide monotonic
deadline (``press.PressDeadline``), bounding every step of a
press by the budget that remains, and retrying the next engine only when the
remaining budget can pay for one bounded second attempt. Whether such an attempt
is worth making is decided at the engine seam, where each failure is classified
as :attr:`~chime_deck.tts.errors.TtsRenderError.transient`.

**The disabled case (D27).** With ``tts.default_engine: none`` this engine is
never constructed: :func:`~chime_deck.tts.speech.build_speech_source` returns
the disabled resolver first, so nothing here ever opens a socket or reads a
setting in that state.

**Format honesty.** The cache path uses the *requested* format from
:attr:`~chime_deck.tts.engines.TtsOptions.preferred_format`, because that is
what was asked for, but Home Assistant's cloud engines may answer MP3 for a
``wav`` request. The bytes are published unchanged and the real container is
logged as a warning: libmpv sniffs the container when it plays the file, so the
audio is still correct, and HSD-dzek must record this real format when it bakes
clips. Renaming the file to the sniffed format would break the cache key, which
is derived from the requested format.

Owning ticket: HSD-npt0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Final, TypeGuard
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..ha import (
    HaError,
    HaNotFoundError,
    HaTtsUrl,
    HomeAssistantClient,
    SettingsReader,
)
from .engines import Availability, TtsOptions, sniff_container, validate_rendered_audio
from .errors import TtsRenderError, clean_detail

__all__ = [
    "AVAILABILITY_NEGATIVE_TTL_S",
    "AVAILABILITY_POSITIVE_TTL_S",
    "AVAILABILITY_TIMEOUT_S",
    "CLOSE_GRACE_S",
    "MAX_AUDIO_BYTES",
    "MAX_DETAIL_BYTES",
    "PRESS_MARGIN_S",
    "TTS_DOWNLOAD_CHUNK_BYTES",
    "TTS_DOWNLOAD_CONNECT_TIMEOUT_S",
    "TTS_DOWNLOAD_POOL_TIMEOUT_S",
    "TTS_DOWNLOAD_READ_TIMEOUT_S",
    "TTS_DOWNLOAD_TIMEOUT_S",
    "TTS_GET_URL_TIMEOUT_S",
    "TTS_RENDER_TOTAL_TIMEOUT_S",
    "HaTtsEngine",
]

_LOGGER = logging.getLogger(__name__)

AVAILABILITY_TIMEOUT_S: Final = 1.0
"""Seconds an availability probe may take before the engine reports unavailable."""

TTS_GET_URL_TIMEOUT_S: Final = 4.5
"""Seconds allowed for ``POST /api/tts_get_url``; Home Assistant synthesises here."""

TTS_DOWNLOAD_TIMEOUT_S: Final = 2.5
"""Seconds allowed for the whole download of the rendered audio."""

TTS_DOWNLOAD_CONNECT_TIMEOUT_S: Final = 1.0
"""Seconds the download may spend opening its connection."""

TTS_DOWNLOAD_READ_TIMEOUT_S: Final = 1.5
"""Seconds the download may wait for the next chunk of audio."""

TTS_DOWNLOAD_POOL_TIMEOUT_S: Final = 1.0
"""Seconds the download may wait for a free connection from the pool."""

TTS_RENDER_TOTAL_TIMEOUT_S: Final = 7.5
"""Seconds one render may take end to end, URL request and download included."""

PRESS_MARGIN_S: Final = 0.5
"""Seconds left for publishing the file inside the press-wide 10 s seam budget."""

MAX_AUDIO_BYTES: Final = 10 * 1024 * 1024
"""Largest speech file accepted; a larger answer is not a doorbell phrase."""

MAX_DETAIL_BYTES: Final = 4096
"""Largest slice of an error answer or file read back for an error message."""

TTS_DOWNLOAD_CHUNK_BYTES: Final = 64 * 1024
"""Bytes read from the download per chunk."""

CLOSE_GRACE_S: Final = 2.0
"""Seconds allowed for closing a response or a client before it is abandoned."""

AVAILABILITY_POSITIVE_TTL_S: Final = 5.0
"""Seconds an "available" verdict is trusted before it is probed again."""

AVAILABILITY_NEGATIVE_TTL_S: Final = 5.0
"""Seconds an "unavailable" verdict is trusted before it is probed again."""

_REJECTED_CONTENT_TYPES: Final = frozenset(
    {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}
)
"""Content types that mean an error page was served instead of audio."""

_BAD_URL_REASON: Final = (
    "Home Assistant returned an address for the speech file that cannot be downloaded — check "
    "ha.base_url and tts.ha.engine_id in Settings"
)
"""Reason used when the answer names an address this engine refuses to fetch."""

_TRANSPORT_REASON: Final = (
    "cannot reach Home Assistant to render the speech — check that ha.base_url is reachable "
    "and that Home Assistant is healthy"
)
"""Reason used when the connection to Home Assistant failed at the transport level."""


class HaTtsEngine:
    """Speak through Home Assistant's own TTS engines.

    Construction is inert: no client is built, no setting is read and nothing
    raises, so a misconfigured install still starts. The settings are read once
    per :meth:`available` and per :meth:`render`, which is what makes a runtime
    change of ``tts.ha.engine_id`` apply on the next press without a restart.
    """

    name: Final = "ha_tts"

    def __init__(
        self,
        settings: SettingsReader,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        max_bytes: int = MAX_AUDIO_BYTES,
        total_timeout_s: float = TTS_RENDER_TOTAL_TIMEOUT_S,
        get_url_timeout_s: float = TTS_GET_URL_TIMEOUT_S,
        download_timeout_s: float = TTS_DOWNLOAD_TIMEOUT_S,
        download_connect_timeout_s: float = TTS_DOWNLOAD_CONNECT_TIMEOUT_S,
        download_read_timeout_s: float = TTS_DOWNLOAD_READ_TIMEOUT_S,
        download_pool_timeout_s: float = TTS_DOWNLOAD_POOL_TIMEOUT_S,
        availability_timeout_s: float = AVAILABILITY_TIMEOUT_S,
        positive_ttl_s: float = AVAILABILITY_POSITIVE_TTL_S,
        negative_ttl_s: float = AVAILABILITY_NEGATIVE_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Store the settings port and the budgets; build and connect nothing.

        :param settings: settings reader, normally ``ConfigStore``.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests. It is never
            closed by this engine.
        :param client_factory: factory for a client this engine owns and closes;
            ``None`` builds a default client per render.
        :param max_bytes: largest speech file accepted, in bytes.
        :param total_timeout_s: seconds one render may take end to end.
        :param get_url_timeout_s: seconds allowed for ``POST /api/tts_get_url``.
        :param download_timeout_s: seconds allowed for the whole download.
        :param download_connect_timeout_s: seconds the download may spend connecting.
        :param download_read_timeout_s: seconds the download may wait per chunk.
        :param download_pool_timeout_s: seconds the download may wait for the pool.
        :param availability_timeout_s: seconds an availability probe may take.
        :param positive_ttl_s: seconds an "available" verdict stays fresh.
        :param negative_ttl_s: seconds an "unavailable" verdict stays fresh.
        :param clock: monotonic clock used for the memo; injectable for tests.
        """
        self._settings = settings
        self._client = client
        self._client_factory = client_factory
        self._max_bytes = max_bytes
        self._total_timeout_s = total_timeout_s
        self._get_url_timeout_s = get_url_timeout_s
        self._download_timeout_s = download_timeout_s
        self._download_connect_timeout_s = download_connect_timeout_s
        self._download_read_timeout_s = download_read_timeout_s
        self._download_pool_timeout_s = download_pool_timeout_s
        self._availability_timeout_s = availability_timeout_s
        self._positive_ttl_s = positive_ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._clock = clock if clock is not None else time.monotonic
        # Keyed by a hash of the three settings, so the token is never kept here.
        self._memo: tuple[str, float, Availability] | None = None

    async def warmup(self) -> None:
        """Do nothing; there is no socket to open and no health to record."""
        return None

    async def available(self) -> Availability:
        """Report whether ``tts.ha.engine_id`` exists in Home Assistant right now.

        Never raises: a check that fails in an unforeseen way is reported as
        unavailable with a whole sentence, because this runs once per press and
        the press path must not see a traceback. An empty token or engine id is
        answered without opening a socket. A verdict is memoised for
        :data:`AVAILABILITY_POSITIVE_TTL_S` or :data:`AVAILABILITY_NEGATIVE_TTL_S`,
        so a burst of presses costs one probe.
        """
        try:
            return await self._availability()
        except Exception as exc:
            _LOGGER.warning("the ha_tts availability check failed unexpectedly", exc_info=True)
            return Availability(
                False,
                f"the Home Assistant TTS check failed unexpectedly ({type(exc).__name__}) — "
                "check the doorbell log",
            )

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Ask Home Assistant for speech and publish it at exactly *destination*.

        The bytes are written to a hidden sibling and moved into place only after
        they were proven to be audio, so a reader never sees a partial file and a
        failed render never removes the destination. Every failure becomes one
        :class:`~chime_deck.tts.errors.TtsRenderError` carrying a whole
        sentence; a cancellation is not swallowed.

        :param text: phrase to speak.
        :param options: sub-engine id, language and preferred format.
        :param destination: exact output path, including its extension.
        :raises TtsRenderError: if no audible file was produced.
        :raises OSError: if the validated file could not be published; the
            resolver catches this next to :class:`TtsRenderError`.
        """
        if not text.strip():
            raise TtsRenderError("no text to speak")
        if not options.engine_id.strip():
            raise TtsRenderError(
                "no Home Assistant TTS engine is selected — set tts.ha.engine_id in Settings"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        try:
            async with self._transport() as raw:
                content_type = await self._fetch(raw, text, options, tmp)
            self._validate_and_publish(tmp, destination, options, content_type=content_type)
        except BaseException:
            # A failed render must be re-probed: a memoised "available" verdict
            # would otherwise keep hiding a Home Assistant that just broke.
            self._memo = None
            raise
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()
        return destination

    async def _availability(self) -> Availability:
        """Probe the configured TTS entity, memoising and never raising.

        :raises Exception: only for a failure the explicit branches below do not
            name; :meth:`available` turns that into a verdict.
        """
        token = await self._str_setting("ha.token")
        if not token:
            return Availability(
                False,
                "no Home Assistant token is configured — add a long-lived access token to "
                "ha.token in Settings",
            )
        engine_id = await self._str_setting("tts.ha.engine_id")
        if not engine_id:
            return Availability(
                False,
                "no Home Assistant TTS engine is selected — set tts.ha.engine_id in Settings "
                "(the settings page lists the engines Home Assistant reports)",
            )
        base_url = await self._str_setting("ha.base_url")
        key = hashlib.sha256("\0".join((base_url, token, engine_id)).encode()).hexdigest()
        now = self._clock()
        memo = self._memo
        if memo is not None and memo[0] == key and now < memo[1]:
            return memo[2]
        verdict = await self._probe(engine_id)
        ttl = self._positive_ttl_s if verdict.ok else self._negative_ttl_s
        self._memo = (key, now + ttl, verdict)
        return verdict

    async def _probe(self, engine_id: str) -> Availability:
        """Ask Home Assistant for one entity state and classify the answer.

        :param engine_id: the ``tts.*`` entity the settings name.
        """
        try:
            async with asyncio.timeout(self._availability_timeout_s):
                async with self._transport() as raw:
                    ha = await HomeAssistantClient.from_store(self._settings, client=raw)
                    try:
                        await ha.state(engine_id, timeout_s=self._availability_timeout_s * 2)
                    finally:
                        with suppress(Exception):
                            await ha.aclose()
        except HaNotFoundError:
            return Availability(
                False,
                f"Home Assistant has no tts entity {clean_detail(engine_id)!r} — the engine id "
                "may have been renamed; choose one from the settings page",
            )
        except HaError as exc:
            return Availability(False, str(exc))
        except TimeoutError:
            return Availability(
                False,
                f"Home Assistant did not answer within {self._availability_timeout_s:g} s — "
                "check that ha.base_url is reachable and that the tts entity still exists",
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            return Availability(
                False,
                f"the Home Assistant TTS check failed ({type(exc).__name__}) — check that "
                "ha.base_url is reachable",
            )
        except Exception as exc:
            _LOGGER.warning("the ha_tts availability probe failed unexpectedly", exc_info=True)
            return Availability(
                False,
                f"the Home Assistant TTS check failed unexpectedly ({type(exc).__name__}) — "
                "check the doorbell log",
            )
        return Availability(True)

    async def _str_setting(self, key: str) -> str:
        """Read one setting, treating a non-string value as unset.

        :param key: registered setting key.
        """
        value = await self._settings.get_setting(key)
        return value if isinstance(value, str) else ""

    async def _fetch(
        self, raw: httpx.AsyncClient, text: str, options: TtsOptions, tmp: Path
    ) -> str:
        """Run the bounded render and translate every failure into one error.

        The ladder is outside :meth:`_request_audio`, so the ``TimeoutError`` the
        :func:`asyncio.timeout` context manager raises while leaving its body is
        caught here too.

        :param raw: the client one request set runs on.
        :param text: phrase to speak.
        :param options: sub-engine id, language and preferred format.
        :param tmp: temporary file that receives the audio bytes.
        :raises TtsRenderError: for every failure, with a whole sentence.
        """
        ha: HomeAssistantClient | None = None
        try:
            ha = await HomeAssistantClient.from_store(self._settings, client=raw)
            return await self._request_audio(ha, raw, text, options, tmp)
        except TtsRenderError:
            # Already classified at the seam it came from, ``transient`` included.
            raise
        except ExceptionGroup as group:
            failure = _classify_group(group)
            if failure is None:
                names = ", ".join(
                    sorted({type(leaf).__name__ for leaf in _flatten_group(group)}) or {"none"}
                )
                _LOGGER.warning(
                    "the ha_tts render failed with an unrecognised exception group of %s",
                    names,
                    exc_info=True,
                )
                # An unknown fault is never retried, so the default ``False`` stands.
                raise TtsRenderError(
                    f"the speech render failed unexpectedly ({names}) — check the doorbell log"
                ) from None
            raise failure from None
        except (httpx.InvalidURL, ValueError, OverflowError) as exc:
            # The answer named a bad address: it has to change before a retry can work.
            _LOGGER.debug("ha_tts rejected the answer: %s", type(exc).__name__)
            raise TtsRenderError(_BAD_URL_REASON) from None
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TtsRenderError(
                f"cannot connect to Home Assistant to render the speech ({type(exc).__name__}) "
                "— check that ha.base_url is reachable and that Home Assistant is healthy",
                transient=True,
            ) from None
        except httpx.PoolTimeout as exc:
            raise TtsRenderError(
                "the speech download could not get a free connection in time — check that "
                "ha.base_url is reachable and that Home Assistant is not overloaded "
                f"({type(exc).__name__})",
                transient=True,
            ) from None
        except TimeoutError:
            raise TtsRenderError(
                f"Home Assistant TTS did not finish the speech within {self._total_timeout_s:g} "
                "s — check that ha.base_url is reachable and that tts.ha.engine_id names a "
                "working TTS engine in Home Assistant",
                transient=True,
            ) from None
        except HaError as exc:
            # The transient rule is read here, at the seam, and never re-derived:
            # ha/errors.py already says connection, timeout, 408, 429 and 5xx are
            # retryable while auth, bad request and not-found are not.
            raise TtsRenderError(str(exc), transient=exc.transient) from None
        except httpx.TimeoutException as exc:
            raise TtsRenderError(
                "Home Assistant stopped sending the speech audio within "
                f"{self._download_read_timeout_s:g} s ({type(exc).__name__}) — check that "
                "ha.base_url is reachable and that Home Assistant is healthy",
                transient=True,
            ) from None
        except OSError as exc:
            # The local disk failed: a second attempt would hit the same wall.
            raise TtsRenderError(
                f"the speech file could not be written to {tmp.parent} — check that the data "
                f"directory is writable ({clean_detail(str(exc)) or type(exc).__name__})"
            ) from None
        except httpx.HTTPError as exc:
            raise TtsRenderError(
                f"the speech download from Home Assistant failed "
                f"({clean_detail(str(exc)) or type(exc).__name__}) — check that ha.base_url "
                "is reachable",
                transient=True,
            ) from None
        except Exception as exc:
            # An unknown fault is never retried: nothing says a second attempt
            # would behave differently, and a bounded retry is a scarce resource.
            _LOGGER.warning("ha_tts render failed unexpectedly", exc_info=True)
            raise TtsRenderError(
                f"the speech render failed unexpectedly ({type(exc).__name__}) — check the "
                "doorbell log"
            ) from None
        finally:
            if ha is not None:
                with suppress(Exception):
                    await ha.aclose()

    async def _request_audio(
        self,
        ha: HomeAssistantClient,
        raw: httpx.AsyncClient,
        text: str,
        options: TtsOptions,
        tmp: Path,
    ) -> str:
        """Ask for the URL and download the audio, all inside one deadline.

        :param ha: the client that asks Home Assistant to synthesise the speech.
        :param raw: the client that downloads the unauthenticated proxy URL.
        :param text: phrase to speak.
        :param options: sub-engine id, language and preferred format.
        :param tmp: temporary file that receives the audio bytes.
        """
        async with asyncio.timeout(self._total_timeout_s):
            audio_url, _base = await self._resolve_url(ha, text, options)
            return await self._download(raw, audio_url, tmp, options)

    async def _resolve_url(
        self, ha: HomeAssistantClient, text: str, options: TtsOptions
    ) -> tuple[str, str]:
        """Ask Home Assistant to synthesise *text* and return the URL to fetch.

        The relative ``path`` is preferred and joined onto the configured base
        URL, because the absolute ``url`` may be Home Assistant's external
        address, which can be slower or unreachable from this device.

        :param ha: client used for ``POST /api/tts_get_url``.
        :param text: phrase to speak.
        :param options: sub-engine id, language and preferred format.
        :raises TtsRenderError: if the answer names no usable address.
        """
        answer = await ha.tts_get_url(
            engine_id=options.engine_id,
            message=text,
            language=options.language or None,
            options={"preferred_format": options.preferred_format},
            timeout_s=self._get_url_timeout_s,
        )
        base = ha.base_url
        if _valid_path(answer.path):
            return f"{base}{answer.path}", base
        return _absolute_url(answer, base), base

    async def _download(
        self, raw: httpx.AsyncClient, url: str, tmp: Path, options: TtsOptions
    ) -> str:
        """Download *url* into *tmp* under the download deadline.

        The request carries its own phased timeout instead of a client-wide one,
        so a slow connect cannot consume the read budget. Redirects are refused
        rather than followed: the audio must come from the host the user
        configured, and the configured host is what the availability probe
        proved is reachable.

        :param raw: client that sends the unauthenticated GET.
        :param url: absolute address of the rendered audio.
        :param tmp: temporary file that receives the bytes.
        :param options: sub-engine id, used in messages.
        :returns: the answer's content type, lowercased and stripped.
        :raises TtsRenderError: if the answer is a redirect, an error or not audio.
        """
        try:
            async with asyncio.timeout(self._download_timeout_s):
                return await self._download_request(raw, url, tmp, options)
        except TimeoutError:
            raise TtsRenderError(
                f"Home Assistant did not send the speech audio within "
                f"{self._download_timeout_s:g} s — check that ha.base_url is reachable and "
                "that Home Assistant is healthy"
            ) from None

    async def _download_request(
        self, raw: httpx.AsyncClient, url: str, tmp: Path, options: TtsOptions
    ) -> str:
        """Send the unauthenticated GET and stream its answer into *tmp*.

        :param raw: client that sends the request.
        :param url: absolute address of the rendered audio.
        :param tmp: temporary file that receives the bytes.
        :param options: sub-engine id, used in messages.
        :returns: the answer's content type, lowercased and stripped.
        """
        request = httpx.Request(
            "GET",
            url,
            headers={"Accept": "audio/*, */*"},
            extensions={
                "timeout": {
                    "connect": self._download_connect_timeout_s,
                    "read": self._download_read_timeout_s,
                    "write": self._download_read_timeout_s,
                    "pool": self._download_pool_timeout_s,
                }
            },
        )
        response = await raw.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            return await self._stream_audio(response, tmp, options)
        finally:
            try:
                await asyncio.wait_for(response.aclose(), CLOSE_GRACE_S)
            except (TimeoutError, OSError, RuntimeError) as exc:
                _LOGGER.debug("could not close the ha_tts download response: %s", exc)

    async def _stream_audio(self, response: httpx.Response, tmp: Path, options: TtsOptions) -> str:
        """Validate the answer's shape and stream its body into *tmp*.

        :param response: the streamed download answer.
        :param tmp: temporary file that receives the bytes.
        :param options: sub-engine id, used in messages.
        :returns: the answer's content type, lowercased and stripped.
        :raises TtsRenderError: if the answer is a redirect, an error page or too large.
        """
        status = response.status_code
        if 300 <= status <= 399:
            location = response.headers.get("location", "")
            raise TtsRenderError(
                f"Home Assistant redirected the speech download to {_location_host(location)} — "
                "set ha.base_url to that address; a redirect is not followed because the audio "
                "would come from somewhere else"
            )
        if status >= 400:
            detail = await _read_detail(response)
            http = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
            raise TtsRenderError(
                f"Home Assistant answered the speech download with {http} — check that "
                f"tts.ha.engine_id ({clean_detail(options.engine_id)!r}) names a working TTS "
                "engine in Home Assistant"
            )
        content_type = _content_type(response)
        if content_type in _REJECTED_CONTENT_TYPES:
            detail = await _read_detail(response)
            http = f" ({detail})" if detail else ""
            raise TtsRenderError(
                f"Home Assistant returned {content_type} instead of audio for the speech file"
                f"{http} — check that tts.ha.engine_id names a working TTS engine in Home "
                "Assistant"
            )
        declared = _content_length(response.headers.get("content-length"))
        if declared is not None and declared > self._max_bytes:
            raise TtsRenderError(self._too_large_message(options))
        total = 0
        with _open_temp(tmp) as handle:
            async for chunk in response.aiter_bytes(TTS_DOWNLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > self._max_bytes:
                    raise TtsRenderError(self._too_large_message(options))
                handle.write(chunk)
        return content_type

    def _validate_and_publish(
        self, tmp: Path, destination: Path, options: TtsOptions, *, content_type: str = ""
    ) -> None:
        """Prove *tmp* is audible audio, then publish it at *destination*.

        The requested format and the real container are compared but never
        reconciled: the bytes are published unchanged, because the cache key is
        derived from the requested format and libmpv sniffs the container.

        :param tmp: temporary file that received the audio bytes.
        :param destination: exact output path, including its extension.
        :param options: sub-engine id, used in messages and in the warning.
        :param content_type: the answer's content type, used when nothing is audio.
        :raises TtsRenderError: if the file is empty or is not audio at all.
        """
        if tmp.stat().st_size == 0:
            raise TtsRenderError(
                f"Home Assistant ({clean_detail(options.engine_id)}) returned an empty speech "
                "file — check that tts.ha.engine_id names a working TTS engine in Home Assistant"
            )
        with tmp.open("rb") as handle:
            container = sniff_container(handle.read(12))
        if container is None:
            detail = _detail_from_file(tmp)
            shown = f": {detail}" if detail else ""
            raise TtsRenderError(
                f"Home Assistant returned {content_type or 'no content type'} instead of audio "
                "for the speech file — check that tts.ha.engine_id names a working TTS engine in "
                f"Home Assistant{shown}"
            )
        validate_rendered_audio(
            tmp,
            container=container,
            label=f"Home Assistant ({clean_detail(options.engine_id)})",
        )
        requested = destination.suffix.strip().lower().lstrip(".")
        if container != requested:
            _LOGGER.warning(
                "Home Assistant returned %s audio for the %s cache path; the bytes are written "
                "unchanged because libmpv sniffs the container and HSD-dzek must record the real "
                "format (engine %s)",
                container,
                requested,
                clean_detail(options.engine_id),
            )
        _publish(tmp, destination)

    def _too_large_message(self, options: TtsOptions) -> str:
        """Build the sentence for an answer larger than ``max_bytes``.

        :param options: sub-engine id, named so the user knows what to check.
        """
        megabytes = self._max_bytes / (1024 * 1024)
        return (
            f"Home Assistant sent more than {megabytes:g} MB of speech — the download was "
            f"abandoned; check that tts.ha.engine_id ({clean_detail(options.engine_id)!r}) names "
            "a speech engine rather than a media player"
        )

    def _default_client(self) -> httpx.AsyncClient:
        """Build the client this engine owns when no factory was supplied."""
        return httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(self._total_timeout_s)
        )

    @asynccontextmanager
    async def _transport(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the HTTP client for one request set, closing only an owned one.

        The close is bounded and its failure is logged at debug level instead of
        raised: a broken socket must not turn a successful render into a failure,
        and it must not hang a render either.
        """
        if self._client is not None:
            yield self._client
            return
        factory = self._client_factory
        raw = self._default_client() if factory is None else factory()
        try:
            yield raw
        finally:
            try:
                await asyncio.wait_for(raw.aclose(), CLOSE_GRACE_S)
            except (TimeoutError, OSError, RuntimeError) as exc:
                _LOGGER.debug("could not close the ha_tts HTTP client: %s", exc)


def _absolute_url(answer: HaTtsUrl, base: str) -> str:
    """Return the absolute ``url`` of a TTS answer after checking it is safe to fetch.

    Only an ``http(s)`` URL on the configured host is accepted: the audio must
    come from the same place the availability probe reached, and nothing that
    carries credentials belongs in a log line or a request.

    :param answer: the ``POST /api/tts_get_url`` answer.
    :param base: the configured base URL, used for the host comparison.
    :raises TtsRenderError: if the address is missing, unparseable or off-host.
    """
    url = answer.url.strip()
    if not url:
        raise TtsRenderError(
            "Home Assistant returned no address for the speech file — check that "
            "tts.ha.engine_id names a working TTS engine in Home Assistant"
        )
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        base_host = urlsplit(base).hostname
    except ValueError:
        raise TtsRenderError(_BAD_URL_REASON) from None
    if (
        parts.scheme not in {"http", "https"}
        or host is None
        or parts.username is not None
        or parts.password is not None
        or _has_control(url)
    ):
        raise TtsRenderError(_BAD_URL_REASON)
    if host.lower() != (base_host or "").lower():
        named = _format_host(host, port)
        configured = base_host or "an address it did not name"
        raise TtsRenderError(
            f"Home Assistant pointed the speech file at {named}, which is not {configured} — "
            "fix or clear Home Assistant's external URL (Settings → System → Network) so it "
            "names an address this device can reach, and set ha.base_url to the external "
            "address only if this device really is outside the LAN; the file was not downloaded"
        )
    _LOGGER.info(
        "Home Assistant returned no tts_proxy path; downloading the absolute URL %s",
        _format_host(host, port),
    )
    return url


def _valid_path(path: object) -> TypeGuard[str]:
    """Return whether *path* is a relative ``/api/tts_proxy/...`` path we may join.

    A path that carries its own scheme, a query, a fragment, a backslash, a
    control character, a ``.``/``..`` segment or a ``user@`` authority would
    escape the configured base URL, so it is refused rather than repaired.

    :param path: the ``path`` field of a TTS answer, or ``None``.
    """
    if not isinstance(path, str):
        return False
    # Only the documented proxy prefix is joined: any other same-host path would
    # download from an endpoint Home Assistant never promised to serve audio from.
    if not path.startswith("/api/tts_proxy/"):
        return False
    if "://" in path or "\\" in path or "?" in path or "#" in path or _has_control(path):
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    first_segment = path[1:].split("/", 1)[0]
    return "@" not in first_segment


def _has_control(text: str) -> bool:
    """Return whether *text* contains a control character.

    :param text: text about to enter a URL.
    """
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


async def _read_detail(response: httpx.Response) -> str:
    """Read at most :data:`MAX_DETAIL_BYTES` of a response body as a detail line.

    The body is streamed rather than read whole, so an error answer can never
    buffer an unbounded page. ``response.text`` and ``response.aread()`` are
    deliberately not used.

    :param response: the streamed answer to describe.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes(MAX_DETAIL_BYTES):
        body.extend(chunk)
        if len(body) >= MAX_DETAIL_BYTES:
            break
    return _detail_text(bytes(body[:MAX_DETAIL_BYTES]))


def _detail_from_file(path: Path) -> str:
    """Read at most :data:`MAX_DETAIL_BYTES` of a file as a detail line.

    :param path: temporary file that turned out to be no audio.
    """
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_DETAIL_BYTES)
    except OSError:
        return ""
    return _detail_text(body)


def _detail_text(body: bytes) -> str:
    """Turn error bytes into one cleaned detail line.

    Home Assistant's own ``{"message": ...}`` wins when the body is small JSON,
    because that is the only text the user can act on; otherwise the raw text is
    cleaned and clipped.

    :param body: decoded-pending bytes from an error answer or file.
    """
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return clean_detail(message)
    return clean_detail(text)


def _content_type(response: httpx.Response) -> str:
    """Return the answer's media type, lowercased and without its parameters.

    ``httpx.Headers.get`` is typed ``Any``, so the value is narrowed here instead
    of being trusted.

    :param response: the answer to inspect.
    """
    raw = response.headers.get("content-type", "")
    if not isinstance(raw, str):
        return ""
    return raw.split(";")[0].strip().lower()


def _content_length(raw: str | None) -> int | None:
    """Return a declared body length, or ``None`` when it is absent or unusable.

    :param raw: the ``Content-Length`` header value.
    """
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _location_host(location: str) -> str:
    """Describe where a redirect pointed without echoing a full URL.

    :param location: the raw ``Location`` header value.
    """
    cleaned = clean_detail(location)
    if not cleaned:
        return "an address it did not name"
    try:
        parts = urlsplit(cleaned)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "an address it did not name"
    if host is None:
        return "an address it did not name"
    return _format_host(host, port)


def _format_host(host: str, port: int | None) -> str:
    """Format *host* and *port* the way a URL does, bracketing an IPv6 host.

    :param host: host name or address.
    :param port: port number, or ``None`` when the default applies.
    """
    shown = f"[{host}]" if ":" in host else host
    return shown if port is None else f"{shown}:{port}"


def _open_temp(path: Path) -> BinaryIO:
    """Open *path* for writing, discarding anything a crashed render left behind.

    Module level on purpose: a test replaces it to observe or fail the write.

    :param path: temporary file that receives the audio bytes.
    """
    return path.open("wb")


def _publish(tmp: Path, destination: Path) -> None:
    """Move *tmp* to *destination* atomically, replacing any earlier clip.

    Module level on purpose: a test replaces it to observe the publish.

    :param tmp: the validated temporary file.
    :param destination: exact output path.
    """
    os.replace(tmp, destination)


def _classify_group(group: ExceptionGroup[Exception]) -> TtsRenderError | None:
    """Turn an :class:`ExceptionGroup` into one typed error, or ``None``.

    A group carries no ordering guarantee, so the leaves are flattened and
    classified by precedence: an engine error wins, then a Home Assistant error,
    then a pure transport group, then a pure address group. ``None`` means the
    group is none of those and the caller reports it as unexpected. A
    :class:`BaseExceptionGroup` with a non-``Exception`` leaf — a cancellation —
    is deliberately not caught anywhere here, so it still propagates.

    ``transient`` follows the same rule the ladder in :meth:`HaTtsEngine._fetch`
    applies leaf by leaf: a :class:`~chime_deck.ha.HaError` carries its own
    verdict, a transport group may succeed on a second attempt, a bad address has
    to change first, and anything else is conservatively not retried.

    :param group: the group raised by the render body.
    """
    leaves = _flatten_group(group)
    if not leaves:
        return None
    for leaf in leaves:
        if isinstance(leaf, TtsRenderError):
            return leaf
    for leaf in leaves:
        if isinstance(leaf, HaError):
            return TtsRenderError(str(leaf), transient=leaf.transient)
    if all(
        isinstance(leaf, (httpx.InvalidURL, httpx.HTTPError, TimeoutError, OSError))
        for leaf in leaves
    ):
        return TtsRenderError(_TRANSPORT_REASON, transient=True)
    if all(isinstance(leaf, (ValueError, OverflowError)) for leaf in leaves):
        return TtsRenderError(_BAD_URL_REASON)
    return None


def _flatten_group(group: BaseExceptionGroup[Exception]) -> list[BaseException]:
    """Return the leaves of a nested exception group, in order.

    :param group: any exception group, nested or not.
    """
    leaves: list[BaseException] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            leaves.extend(_flatten_group(exc))
        else:
            leaves.append(exc)
    return leaves
