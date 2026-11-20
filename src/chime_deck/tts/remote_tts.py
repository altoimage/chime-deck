"""The ``remote_tts`` engine: speech synthesised by another doorbell.

D24 lets one doorbell speak for another. The serving side is
``POST /api/tts/render`` in renderer mode, implemented by
:mod:`chime_deck.api.render` and protected by a mandatory bearer token;
this module is the client. :class:`RemoteTtsEngine` implements the same
:class:`~chime_deck.tts.engines.TtsEngine` port as ``ha_tts``, ``piper`` and
``espeak_ng``, so the resolver, the clip cache and the bake path cannot tell
the difference: the bytes land at the ``destination`` path the resolver asked
for, after the same validation every local engine applies.

**The probe is a health check, not a synthesis.** :meth:`RemoteTtsEngine.available`
asks the renderer's token-gated ``GET /api/health``, never a real render: it is
cheap, burns no CPU on the renderer and no minutes anywhere, and it
distinguishes the two failures a user must be told apart — a **rejected token**
(``401``, fix ``tts.renderer.token``) from an **unreachable host** (connect
error, fix the address or the machine). A synthesis probe would cost the whole
render budget on every press and would still not say which of the two is wrong.

**The press seam is arithmetic.** :data:`REMOTE_RENDER_BUDGET_S` is the part of
one render this engine may spend, and :data:`REMOTE_AVAILABILITY_TIMEOUT_S` the
part the probe may spend; together with the template budget and the publish
margin they fill the 10 s press seam exactly. :mod:`chime_deck.mqtt.bridge`
is deliberately **not** imported here — the ``tts`` package must stay free of
the MQTT dependency — so a test pins that arithmetic instead.

**The token never travels in a URL.** It goes in the ``Authorization`` header
only, it is hashed (never stored) in the availability memo, and any renderer
text that could echo it is redacted before it enters a message or a log line.
The memo key is ``sha256(url\\0token)`` on purpose: a changed URL *or* a changed
token must re-probe, because either one changes the verdict.

**A failed render records a negative verdict.** A timeout, a transport failure
or any non-2xx answer means the renderer just proved unusable, so the *next*
press reports unavailable and falls back to ``ha_tts`` instead of paying the
same budget again — piper's latch, in memo form. A render failure is not retried
inside the same press (HSD-p50k).

**Transient is classified at this seam (HSD-96k6).** A transport failure, a
timeout and an answer of ``408``/``429``/``5xx`` are marked
:attr:`~chime_deck.tts.errors.TtsRenderError.transient`, because repeating
the same render could succeed; a refusal — ``401``/``403`` (the token must
change), ``422`` (the phrase must change), ``404`` (the address is wrong) and a
non-audio answer — is not. That distinction lets the resolver spend one bounded
second attempt on the transient half, and it is decided here rather than
derived from prose later. :class:`_RendererUnusable` still means "record a
negative availability verdict", and now carries ``transient`` through to its
base class as well.

**Not a paid provider.** The renderer is another doorbell on the LAN, not a
billed API, so :data:`~chime_deck.tts.providers.NO_PAID_PROVIDERS` stays the
honest default and D25's gate stays dormant; ``remote_tts`` must never be listed
as paid.

**No shared client.** :func:`~chime_deck.tts.speech.build_speech_source`
does **not** hand this engine the shared ``ha_client``: the per-render client
this engine owns (or borrows in a test) is what keeps the ``ha_tts`` request
sequence clean, because a streaming render holds a connection for its whole
budget.

Owning ticket: HSD-5de8.
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
from typing import Final
from uuid import uuid4

import httpx

from ..ha import SettingsReader
from ..store import SETTINGS
from .engines import Availability, TtsOptions, sniff_container, validate_rendered_audio
from .errors import TtsRenderError, clean_detail
from .ha_tts import CLOSE_GRACE_S, MAX_AUDIO_BYTES, MAX_DETAIL_BYTES

__all__ = [
    "REMOTE_AVAILABILITY_NEGATIVE_TTL_S",
    "REMOTE_AVAILABILITY_POSITIVE_TTL_S",
    "REMOTE_AVAILABILITY_TIMEOUT_S",
    "REMOTE_CHUNK_BYTES",
    "REMOTE_CLOSE_GRACE_S",
    "REMOTE_HEALTH_PATH",
    "REMOTE_RENDER_BUDGET_S",
    "REMOTE_RENDER_ENGINE",
    "REMOTE_RENDER_FORMAT",
    "REMOTE_RENDER_PATH",
    "RemoteTtsEngine",
]

_LOGGER = logging.getLogger(__name__)

REMOTE_AVAILABILITY_TIMEOUT_S: Final = 1.0
"""Seconds the per-press availability probe may take.

Mirrors :data:`~chime_deck.tts.ha_tts.AVAILABILITY_TIMEOUT_S`: the probe is
one part of the press seam, not a place to spend time.
"""

REMOTE_RENDER_BUDGET_S: Final = 7.5
"""Seconds one remote render attempt may take — what a press can afford.

The seam is arithmetic: 1.0 s probe (:data:`REMOTE_AVAILABILITY_TIMEOUT_S`) +
0.5 s template (:data:`~chime_deck.tts.templating.TEMPLATE_TIMEOUT_S`) +
7.5 s render (this value) + 0.5 s publish margin = 9.5 s, below
``DEFAULT_SEAM_TIMEOUT_S`` (10.0 s) in :mod:`chime_deck.mqtt.bridge`.
Mirrors :data:`~chime_deck.tts.ha_tts.TTS_RENDER_TOTAL_TIMEOUT_S`, so the
remote engine is never slower than the local Home Assistant one. The MQTT module
is not imported here, so a test pins the arithmetic instead.
"""

REMOTE_AVAILABILITY_POSITIVE_TTL_S: Final = 5.0
"""Seconds an "available" verdict is trusted before the renderer is probed again."""

REMOTE_AVAILABILITY_NEGATIVE_TTL_S: Final = 5.0
"""Seconds an "unavailable" verdict is trusted before the renderer is probed again."""

REMOTE_RENDER_ENGINE: Final = "piper"
"""Engine name this client asks the renderer for; the v1 wire value."""

REMOTE_RENDER_FORMAT: Final = "wav"
"""Format this client asks the renderer for; the v1 wire value."""

REMOTE_RENDER_PATH: Final = "/api/tts/render"
"""Path of the renderer's render endpoint, appended to ``tts.remote_tts.url``."""

REMOTE_HEALTH_PATH: Final = "/api/health"
"""Path of the renderer's token-gated health endpoint, used by the probe."""

REMOTE_CHUNK_BYTES: Final = 64 * 1024
"""Bytes read from the render answer per chunk."""

REMOTE_CLOSE_GRACE_S: Final = CLOSE_GRACE_S
"""Seconds allowed for closing a response or a client before it is abandoned.

Shares :data:`~chime_deck.tts.ha_tts.CLOSE_GRACE_S` rather than minting a
second number: the grace is a property of ``httpx`` here, not of the engine.
"""

_REMOTE_URL_SETTING: Final = "tts.remote_tts.url"
"""Setting that names the renderer, without the endpoint path."""

_REMOTE_TOKEN_SETTING: Final = "tts.remote_tts.token"
"""Setting holding the bearer token the renderer's ``tts.renderer.token`` must match."""

_REMOTE_TIMEOUT_SETTING: Final = "tts.remote_tts.timeout_s"
"""Setting holding the configured transport ceiling, clamped to the press budget."""

_NO_URL_REASON: Final = "no TTS renderer address is configured — set tts.remote_tts.url in Settings"
"""Sentence for an install that never configured a renderer."""

_NO_TOKEN_REASON: Final = (
    "no TTS renderer token is configured — set tts.remote_tts.token to the renderer's "
    "tts.renderer.token"
)
"""Sentence for an install that has an address but no token."""


class _RendererUnusable(TtsRenderError):
    """A failure that proves the renderer cannot render right now.

    Private and never raised outside this module: it only marks the cases that
    must also record a negative availability verdict (a transport failure or a
    non-2xx answer), while a rejected *content* answer stays a plain
    :class:`~chime_deck.tts.errors.TtsRenderError`. Callers catch the public
    base class, so the distinction is invisible to them.

    ``transient`` is forwarded to the base class unchanged, so a renderer that
    merely timed out or answered ``503`` is also the case worth one bounded second
    attempt, while a refused token or a refused phrase is not.
    """

    def __init__(self, sentence: str, *, transient: bool = False) -> None:
        """Record the sentence and whether a retry could succeed.

        :param sentence: whole actionable sentence safe to show to a user.
        :param transient: whether repeating the same render could succeed.
        """
        super().__init__(sentence, transient=transient)


class RemoteTtsEngine:
    """Speak through another doorbell's ``POST /api/tts/render``.

    Construction is inert: no client is built, no setting is read and nothing
    raises, so an install with no renderer configured still starts and answers
    "unavailable" without opening a socket. The settings are read once per
    :meth:`available` and once per :meth:`render`, which is what makes a runtime
    change of ``tts.remote_tts.url`` or ``tts.remote_tts.token`` apply on the
    next press without a restart.
    """

    name: Final = "remote_tts"

    def __init__(
        self,
        settings: SettingsReader,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        availability_timeout_s: float = REMOTE_AVAILABILITY_TIMEOUT_S,
        render_budget_s: float = REMOTE_RENDER_BUDGET_S,
        positive_ttl_s: float = REMOTE_AVAILABILITY_POSITIVE_TTL_S,
        negative_ttl_s: float = REMOTE_AVAILABILITY_NEGATIVE_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Store the settings port and the budgets; build and connect nothing.

        :param settings: settings reader, normally ``ConfigStore``.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests. It is never
            closed by this engine.
        :param client_factory: factory for a client this engine owns and closes;
            ``None`` builds a default client per request set.
        :param availability_timeout_s: seconds an availability probe may take.
        :param render_budget_s: seconds one render attempt may take, the press
            ceiling the configured timeout is clamped to.
        :param positive_ttl_s: seconds an "available" verdict stays fresh.
        :param negative_ttl_s: seconds an "unavailable" verdict stays fresh.
        :param clock: monotonic clock used for the memo; injectable for tests.
        """
        self._settings = settings
        self._client = client
        self._client_factory = client_factory
        self._availability_timeout_s = availability_timeout_s
        self._render_budget_s = render_budget_s
        self._positive_ttl_s = positive_ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._clock = clock if clock is not None else time.monotonic
        # Keyed by a hash of url and token, so the token itself is never kept.
        self._memo: tuple[str, float, Availability] | None = None

    async def warmup(self) -> None:
        """Do nothing; there is no socket to open and no health to record."""
        return None

    async def available(self) -> Availability:
        """Report whether the configured renderer accepts this client right now.

        Never raises: this runs once per press and the press path must not see a
        traceback, so a check that fails in an unforeseen way is answered with
        one whole sentence. Reading the settings first means an unconfigured
        renderer is answered without building a client or opening a socket at
        all. The probe is the renderer's token-gated ``GET /api/health``, not a
        real synthesis, because it is cheap, burns no renderer CPU and
        distinguishes a rejected token from an unreachable host. A verdict is
        memoised for :data:`REMOTE_AVAILABILITY_POSITIVE_TTL_S` or
        :data:`REMOTE_AVAILABILITY_NEGATIVE_TTL_S`, so a burst of presses costs
        one probe.
        """
        try:
            return await self._availability()
        except Exception as exc:
            _LOGGER.warning("the remote_tts availability check failed unexpectedly", exc_info=True)
            return Availability(
                False,
                f"the remote_tts availability check failed unexpectedly "
                f"({type(exc).__name__}) — check the doorbell log",
            )

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Ask the renderer for *text* and publish the WAV at exactly *destination*.

        The whole attempt runs inside ``min(tts.remote_tts.timeout_s,
        REMOTE_RENDER_BUDGET_S)``, because one press can afford only the render
        budget even when the user configured a longer transport ceiling. The body
        is streamed into a hidden sibling and moved into place only after it was
        proven to be a WAV, so a reader never sees a partial file and a failed,
        rejected or cancelled render leaves nothing behind.

        A timeout, a transport failure or a non-2xx answer records a negative
        availability verdict for :data:`REMOTE_AVAILABILITY_NEGATIVE_TTL_S`, so
        the *next* press falls back to ``ha_tts`` instead of paying the same
        budget again.

        :param text: phrase to speak.
        :param options: unused today — the renderer applies its own
            ``tts.piper.voice`` and this client always asks for ``wav``.
        :param destination: exact output path, including its extension.
        :raises TtsRenderError: if no audible WAV was produced.
        :raises OSError: if the validated file could not be published; the
            resolver catches this next to :class:`TtsRenderError`.
        """
        if not text.strip():
            raise TtsRenderError("remote_tts has no text to speak")
        url = (await self._str_setting(_REMOTE_URL_SETTING)).rstrip("/")
        if not url:
            raise TtsRenderError(_NO_URL_REASON)
        token = await self._str_setting(_REMOTE_TOKEN_SETTING)
        if not token:
            raise TtsRenderError(_NO_TOKEN_REASON)
        configured_s = await self._int_setting(_REMOTE_TIMEOUT_SETTING)
        budget_s = min(float(configured_s), self._render_budget_s)
        if budget_s < configured_s:
            _LOGGER.info(
                "tts.remote_tts.timeout_s is %d s but one press can afford only %g s; the "
                "render is bounded at %g s (a press cannot afford the configured timeout)",
                configured_s,
                self._render_budget_s,
                budget_s,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        try:
            async with asyncio.timeout(budget_s):
                head, answered_engine, answered_voice = await self._attempt(url, token, text, tmp)
            self._publish(tmp, destination, head)
        except _RendererUnusable as exc:
            self._remember(url, token, Availability(False, str(exc)))
            raise
        except TimeoutError:
            reason = (
                f"the TTS renderer at {url} did not render the speech within {budget_s:g} s — "
                "check that the renderer machine is powered on and that tts.remote_tts.url is "
                "correct"
            )
            self._remember(url, token, Availability(False, reason))
            raise TtsRenderError(reason, transient=True) from None
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()
        _LOGGER.info(
            "remote_tts rendered %d characters at %s%s",
            len(text),
            url,
            _answered_suffix(answered_engine, answered_voice),
        )
        return destination

    async def _availability(self) -> Availability:
        """Read the settings, consult the memo and probe the renderer.

        :raises Exception: only for a failure the branches below do not name;
            :meth:`available` turns that into a verdict.
        """
        url = (await self._str_setting(_REMOTE_URL_SETTING)).rstrip("/")
        if not url:
            return Availability(False, _NO_URL_REASON)
        token = await self._str_setting(_REMOTE_TOKEN_SETTING)
        if not token:
            return Availability(False, _NO_TOKEN_REASON)
        key = _memo_key(url, token)
        now = self._clock()
        memo = self._memo
        if memo is not None and memo[0] == key and now < memo[1]:
            return memo[2]
        verdict = await self._probe(url, token)
        self._remember(url, token, verdict)
        return verdict

    async def _probe(self, url: str, token: str) -> Availability:
        """Ask the renderer's token-gated ``GET /api/health`` and classify the answer.

        :param url: configured renderer address, without the endpoint path.
        :param token: bearer token for the renderer.
        """
        health_url = _endpoint(url, REMOTE_HEALTH_PATH)
        try:
            async with asyncio.timeout(self._availability_timeout_s):
                async with self._transport() as raw:
                    response = await raw.get(
                        health_url,
                        headers={"Authorization": f"Bearer {token}"},
                        follow_redirects=False,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return Availability(False, _reach_reason(url))
        except (TimeoutError, httpx.TimeoutException):
            return Availability(
                False,
                f"the TTS renderer at {url} did not answer within "
                f"{self._availability_timeout_s:g} s — check that the renderer machine is "
                "powered on and that tts.remote_tts.url is correct",
            )
        except httpx.HTTPError as exc:
            return Availability(
                False,
                f"the TTS renderer check at {url} failed ({type(exc).__name__}) — check that "
                "tts.remote_tts.url is correct",
            )
        status = response.status_code
        if status == 200:
            return Availability(True)
        if status in {401, 403}:
            return Availability(False, _rejected_reason(url))
        return Availability(
            False,
            f"the TTS renderer at {url} answered HTTP {status} — check that it is running in "
            "the renderer role (chime-deck --role renderer)",
        )

    async def _attempt(self, url: str, token: str, text: str, tmp: Path) -> tuple[bytes, str, str]:
        """Send one render request and stream its answer into *tmp*.

        Every transport failure is translated here, inside the caller's deadline,
        so the render's error ladder stays one flat set of sentences.

        :param url: configured renderer address, without the endpoint path.
        :param token: bearer token for the renderer.
        :param text: phrase to speak.
        :param tmp: temporary file that receives the audio bytes.
        :returns: the first twelve bytes of the answer, the renderer's echoed
            engine name and its echoed voice name, each ``""`` when absent.
        :raises TtsRenderError: for every failure, with a whole sentence.
        """
        try:
            async with self._transport() as raw:
                request = raw.build_request(
                    "POST",
                    _endpoint(url, REMOTE_RENDER_PATH),
                    json=_payload(text),
                    headers={"Authorization": f"Bearer {token}"},
                )
                response = await raw.send(request, stream=True, follow_redirects=False)
                try:
                    return await self._stream_audio(response, tmp, url, token)
                finally:
                    await _close(response)
        except TtsRenderError:
            # Already classified at the seam it came from, ``transient`` included.
            raise
        except (httpx.InvalidURL, ValueError, OverflowError) as exc:
            # The address cannot be used: it must change before a retry can work.
            raise _RendererUnusable(
                f"the TTS renderer address in tts.remote_tts.url ({clean_detail(url)}) cannot be "
                f"used ({type(exc).__name__}) — check that it is a plain http(s) address"
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _RendererUnusable(
                f"cannot reach the TTS renderer at {url} to render the speech "
                f"({type(exc).__name__}) — check that the renderer machine is powered on and "
                "that tts.remote_tts.url is correct",
                transient=True,
            ) from None
        except httpx.TimeoutException as exc:
            raise _RendererUnusable(
                f"the TTS renderer at {url} stopped sending the speech ({type(exc).__name__}) — "
                "check that the renderer machine is powered on and that tts.remote_tts.url is "
                "correct",
                transient=True,
            ) from None
        except httpx.HTTPError as exc:
            raise _RendererUnusable(
                f"the speech render through the TTS renderer at {url} failed "
                f"({clean_detail(str(exc)) or type(exc).__name__}) — check that "
                "tts.remote_tts.url is correct",
                transient=True,
            ) from None

    async def _stream_audio(
        self, response: httpx.Response, tmp: Path, url: str, token: str
    ) -> tuple[bytes, str, str]:
        """Validate the answer's shape and stream its body into *tmp*.

        :param response: the streamed render answer.
        :param tmp: temporary file that receives the bytes.
        :param url: configured renderer address, used in messages.
        :param token: bearer token, redacted out of any renderer text.
        :returns: the first twelve bytes of the body, the echoed engine name
            and the echoed voice name, each ``""`` when the header is absent.
        :raises TtsRenderError: if the answer is an error, not audio, not a WAV
            or larger than :data:`~chime_deck.tts.ha_tts.MAX_AUDIO_BYTES`.
        """
        status = response.status_code
        if status >= 400:
            detail = await _renderer_message(response, token)
            # 408/429/5xx are worth one bounded second attempt; 401/403 need a
            # token, 422 needs a different phrase and 404 needs a different
            # address, so none of those may be retried.
            raise _RendererUnusable(
                _status_reason(status, url, detail),
                transient=status in {408, 429} or status >= 500,
            )
        content_type = _content_type(response)
        if not content_type.startswith("audio/"):
            raise TtsRenderError(
                f"the renderer answered {content_type or 'without a content type'} instead of "
                "audio — check that tts.remote_tts.url points at the doorbell renderer and not "
                "at a web page"
            )
        declared = _content_length(response.headers.get("content-length"))
        if declared is not None and declared > MAX_AUDIO_BYTES:
            raise TtsRenderError(_too_large_reason())
        total = 0
        head = b""
        with tmp.open("wb") as handle:
            async for chunk in response.aiter_bytes(REMOTE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise TtsRenderError(_too_large_reason())
                if len(head) < 12:
                    head = (head + chunk)[:12]
                handle.write(chunk)
        return (
            head,
            _header(response, "X-HSD-Engine"),
            _header(response, "X-HSD-Voice"),
        )

    def _publish(self, tmp: Path, destination: Path, head: bytes) -> None:
        """Prove *tmp* is an audible WAV, then publish it at *destination*.

        :param tmp: temporary file that received the audio bytes.
        :param destination: exact output path, including its extension.
        :param head: first twelve bytes of the body, from the streamed answer.
        :raises TtsRenderError: if the bytes are not a WAV or carry no audible
            audio.
        """
        if sniff_container(head) != "wav":
            raise TtsRenderError(
                "the renderer answered 200 audio/wav with bytes that are not a WAV file, so the "
                "answer is an error page dressed as audio — check that tts.remote_tts.url "
                "points at the doorbell renderer and not at a web page"
            )
        # The same >=50 ms audibility bar every local engine applies.
        validate_rendered_audio(tmp, container="wav", label="the renderer")
        os.replace(tmp, destination)

    def _remember(self, url: str, token: str, verdict: Availability) -> None:
        """Memoise *verdict* for the TTL that matches it.

        :param url: configured renderer address, without the endpoint path.
        :param token: bearer token; only its hash is kept.
        :param verdict: the verdict to trust for the next few seconds.
        """
        ttl = self._positive_ttl_s if verdict.ok else self._negative_ttl_s
        self._memo = (_memo_key(url, token), self._clock() + ttl, verdict)

    async def _str_setting(self, key: str) -> str:
        """Read one setting, treating a non-string value as unset.

        :param key: registered setting key.
        """
        value = await self._settings.get_setting(key)
        return value if isinstance(value, str) else ""

    async def _int_setting(self, key: str) -> int:
        """Read one integer setting, falling back to its registry default.

        A value of the wrong type (a corrupt row) must never become a
        zero-second deadline, so the registry default is used instead — the same
        rule the resolver's own reader applies.

        :param key: registered setting key.
        """
        value = await self._settings.get_setting(key)
        if isinstance(value, bool) or not isinstance(value, int):
            default = SETTINGS[key].default
            if isinstance(default, bool) or not isinstance(default, int):
                raise TypeError(f"setting {key!r} does not have an integer default")
            return default
        return value

    def _default_client(self) -> httpx.AsyncClient:
        """Build the client this engine owns when no factory was supplied."""
        return httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(self._render_budget_s)
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
            await _close(raw)


def _memo_key(url: str, token: str) -> str:
    """Hash the renderer address and token into the availability memo key.

    A hash, not the value: the token must never be kept in this process any
    longer than the settings read that produced it.

    :param url: configured renderer address, without the endpoint path.
    :param token: bearer token for the renderer.
    """
    return hashlib.sha256("\0".join((url, token)).encode()).hexdigest()


def _endpoint(url: str, path: str) -> str:
    """Join *path* onto *url* without doubling or dropping a slash.

    :param url: configured renderer address, with or without a trailing slash.
    :param path: endpoint path, always starting with ``/``.
    """
    return url.rstrip("/") + path


def _payload(text: str) -> dict[str, object]:
    """Build the render request body for *text*.

    Rule from :mod:`chime_deck.api.render`: a field whose value is ``""`` or
    ``0`` is absent, not an override. So this client sends exactly the three
    fields below and never sends ``engine_id``, a voice or a speed.

    :param text: phrase to speak, already the rendered phrase.
    """
    return {
        "text": text,
        "engine": REMOTE_RENDER_ENGINE,
        "options": {"preferred_format": REMOTE_RENDER_FORMAT},
    }


def _status_reason(status: int, url: str, detail: str) -> str:
    """Build the whole sentence for a render answer that failed.

    :param status: HTTP status the renderer answered with.
    :param url: configured renderer address, named in the generic sentence.
    :param detail: the renderer's own envelope message, already cleaned, or ``""``.
    """
    shown = f": {detail}" if detail else ""
    if status in {401, 403}:
        return _rejected_reason(url)
    if status == 422:
        return (
            f"the renderer rejected the text{shown} — check that the phrase is plain text of at "
            "most 500 characters with no control characters"
        )
    if status == 429:
        return "the renderer is busy (HTTP 429) — press the button again in a moment"
    if status == 503:
        return (
            f"the renderer could not synthesise the speech{shown} — check the renderer's log on "
            "the machine that runs it"
        )
    return (
        f"the renderer at {url} answered HTTP {status} for the speech{shown} — check that "
        "tts.remote_tts.url points at the doorbell renderer and that it is running in the "
        "renderer role"
    )


def _rejected_reason(url: str) -> str:
    """Build the sentence for a renderer that refused the client's token.

    :param url: configured renderer address.
    """
    return (
        f"the renderer at {url} rejected the token — check that tts.remote_tts.token matches "
        "tts.renderer.token on the renderer"
    )


def _reach_reason(url: str) -> str:
    """Build the sentence for a renderer whose host could not be reached.

    :param url: configured renderer address.
    """
    return (
        f"cannot reach the TTS renderer at {url} — check that the renderer machine is powered on "
        "and that tts.remote_tts.url is correct"
    )


def _too_large_reason() -> str:
    """Build the sentence for an answer larger than the shared size cap."""
    megabytes = MAX_AUDIO_BYTES / (1024 * 1024)
    return (
        f"the renderer sent more than {megabytes:g} MB of speech — the download was abandoned; "
        "a doorbell phrase is never that long, so check that tts.remote_tts.url points at the "
        "doorbell renderer"
    )


def _answered_suffix(engine: str, voice: str) -> str:
    """Format the renderer's echoed engine and voice for one log line.

    An absent header simply omits its part, so a renderer that echoes nothing
    still logs one clean sentence.

    :param engine: value of the renderer's ``X-HSD-Engine`` header, or ``""``.
    :param voice: value of the renderer's ``X-HSD-Voice`` header, or ``""``.
    """
    parts: list[str] = []
    if engine:
        parts.append(f"engine {clean_detail(engine)}")
    if voice:
        parts.append(f"voice {clean_detail(voice)}")
    return f" ({', '.join(parts)})" if parts else ""


async def _renderer_message(response: httpx.Response, token: str) -> str:
    """Read the renderer's own envelope message from an error answer.

    The body is streamed, never read whole, and only the envelope's
    ``error.message`` is used: an arbitrary body is not a sentence a user can
    act on, and it may quote anything the renderer chose to echo.

    :param response: the streamed answer to describe.
    :param token: bearer token, redacted out of the message if it appears.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes(MAX_DETAIL_BYTES):
        body.extend(chunk)
        if len(body) >= MAX_DETAIL_BYTES:
            break
    text = bytes(body[:MAX_DETAIL_BYTES]).decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    message = error.get("message")
    if not isinstance(message, str):
        return ""
    return _redact(clean_detail(message), token)


def _redact(text: str, token: str) -> str:
    """Replace every occurrence of *token* in *text* with a placeholder.

    :param text: text about to enter a message a user can see.
    :param token: the secret that must never appear there.
    """
    if not token:
        return text
    return text.replace(token, "***")


def _content_type(response: httpx.Response) -> str:
    """Return the answer's media type, lowercased and without its parameters.

    ``httpx.Headers.get`` is typed ``Any``, so the value is narrowed here instead
    of being trusted.

    :param response: the answer to inspect.
    """
    return _header(response, "content-type").split(";")[0].strip().lower()


def _header(response: httpx.Response, name: str) -> str:
    """Return one response header as text, or ``""`` when it is absent.

    :param response: the answer to inspect.
    :param name: header name, case insensitive.
    """
    raw = response.headers.get(name)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


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


async def _close(target: httpx.Response | httpx.AsyncClient) -> None:
    """Close a response or a client, bounded, logging a failure at debug level.

    :param target: the response or client to close.
    """
    try:
        await asyncio.wait_for(target.aclose(), REMOTE_CLOSE_GRACE_S)
    except (TimeoutError, OSError, RuntimeError) as exc:
        _LOGGER.debug("could not close a remote_tts resource: %s", exc)
