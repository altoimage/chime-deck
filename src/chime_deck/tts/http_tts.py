"""The ``http_tts`` engine: speech from a configured HTTP text-to-speech API.

D25's provider engine. A *preset* (``tts/presets/*.json``, loaded by
:mod:`chime_deck.tts.presets`) is one HTTP request shape — method and path,
auth style, a JSON or SSML body and how to read the audio back out — and a
*provider record* (``tts.providers``, resolved by
:mod:`chime_deck.tts.providers`) binds one preset to an address, a voice and
an API key. :class:`HttpTtsEngine` implements the same
:class:`~chime_deck.tts.engines.TtsEngine` port as ``ha_tts``, ``piper``,
``espeak_ng`` and ``remote_tts``, so the resolver, the clip cache and the bake
path cannot tell the difference: the bytes land at the ``destination`` path the
resolver asked for, after the same validation every other engine applies.

**Construction is inert.** No client is built, no setting is read and nothing
raises, so an install with no provider configured still starts, reports
"unavailable" in one honest sentence and never opens a socket. The provider
registry is read once per :meth:`HttpTtsEngine.available`, once per
:meth:`HttpTtsEngine.render` and once per :meth:`HttpTtsEngine.estimate_cost`,
so a provider added, edited or deleted in Settings applies on the next press
without a restart.

**There is no availability probe, and that is deliberate.** A paid endpoint must
not be pinged on every press, and a probe inside the 9 s press deadline would
eat the render's own budget for an answer that is only a guess. So
:meth:`HttpTtsEngine.available` answers from the configuration alone (is a
provider selected, is a key stored) and **never raises**. A real failure — a
refused key, an unreachable server, a reply that is not audio — surfaces at
render time as a *classified* :class:`~chime_deck.tts.errors.TtsRenderError`,
which is what the resolver needs to spend at most one bounded second attempt
(HSD-96k6) and what the press log then shows.

**The request is built from data, never from a format string.** The body of a
``json`` preset has every string leaf substituted and is then handed to
:func:`json.dumps`, so a quote or a backslash in the phrase cannot break the
request; the URL and an SSML body substitute through one single pass over the
three placeholders, so a phrase that happens to contain ``{voice}`` cannot
inject a second substitution. Every value that travels in a *path* or an
attribute is percent-encoded or XML-escaped first.

**Transient is classified at this seam (HSD-96k6), exactly as ``remote_tts``
classifies it.** A transport failure, a timeout and an answer of
``408``/``429``/``5xx`` are marked
:attr:`~chime_deck.tts.errors.TtsRenderError.transient`, because repeating
the same request could succeed; a refusal — ``400``/``401``/``403``/``404``/
``409``/``422`` — a bad base URL, a body that is not audio and a reply the
preset cannot read are not, because a setting, a key or a phrase must change
first.

**The bytes are validated by magic number, never by Content-Type.** A provider
that answers ``200`` with an HTML error page or a JSON envelope is a
misconfigured address, not audio, and the engine says so in one sentence that
names what the reply looked like. A container that disagrees with the preset
(``WAV`` bytes for a provider declared as ``mp3``) is refused naming both, and
the same :func:`~chime_deck.tts.engines.validate_rendered_audio` bar every
local engine applies is applied to the file. The answer is written to a hidden
sibling of ``destination`` and moved into place only after all of that, so a
reader never sees a partial file and a failed render leaves nothing behind.

**The API key never leaves in a message.** Every borrowed text — the provider's
own error message, an exception's ``str``, a provider name — passes through
:func:`~chime_deck.tts.errors.clean_detail` and then through
:func:`_redact`, which replaces the key with ``***``. Every composed sentence
passes through :func:`_redact` again before it is raised, so a provider that
echoes the key in its ``401`` body cannot leak it into the log, the press row or
the UI. Headers and bodies are never logged, and nothing here logs above
``warning``.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import httpx

from .engines import Availability, TtsOptions, sniff_container, validate_rendered_audio
from .errors import TtsRenderError, clean_detail
from .presets import ProviderShape
from .providers import ConfiguredProviders, ProviderSource, ResolvedProvider, TtsCost

__all__ = [
    "HTTP_TTS_CHUNK_BYTES",
    "HTTP_TTS_CLOSE_GRACE_S",
    "HTTP_TTS_MAX_AUDIO_BYTES",
    "HTTP_TTS_MAX_REPLY_BYTES",
    "HTTP_TTS_RENDER_BUDGET_S",
    "HTTP_TTS_TEXT_MAX_CHARS",
    "HttpTtsEngine",
    "ProviderProbe",
]

_LOGGER = logging.getLogger(__name__)

HTTP_TTS_RENDER_BUDGET_S: Final = 7.5
"""Seconds one provider render may take — the same ceiling ``remote_tts`` clamps to.

The press seam is arithmetic: 1.0 s probe + 0.5 s template + 7.5 s render
(this value) + 0.5 s publish margin = 9.5 s, and the press-wide deadline
(:data:`~chime_deck.press.PRESS_DEADLINE_S`, 9.0 s) binds first, so a stuck
provider fails the press with an honest row instead of being cancelled at the
MQTT seam.
"""

HTTP_TTS_MAX_AUDIO_BYTES: Final = 10 * 1024 * 1024
"""Bytes of audio one render accepts; a doorbell phrase is never larger."""

HTTP_TTS_MAX_REPLY_BYTES: Final = 16 * 1024 * 1024
"""Bytes of JSON one reply may carry — a base64 body is about 1.4x its audio."""

HTTP_TTS_CHUNK_BYTES: Final = 64 * 1024
"""Bytes read from a reply per chunk."""

HTTP_TTS_CLOSE_GRACE_S: Final = 2.0
"""Seconds allowed for closing a response or a client before it is abandoned.

Shares the intent of :data:`~chime_deck.tts.remote_tts.REMOTE_CLOSE_GRACE_S`:
the grace is a property of ``httpx`` here, not of the engine, and a broken socket
must never hang a press or turn a good render into a failure.
"""

HTTP_TTS_TEXT_MAX_CHARS: Final = 5000
"""Longest phrase this engine will send; longer is refused with a sentence.

A sanity ceiling rather than a provider limit: a doorbell phrase is one or two
sentences, and 5000 characters is already far past anything a panel speaks.
"""

_HEAD_BYTES: Final = 12
"""Leading bytes kept for :func:`~chime_deck.tts.engines.sniff_container`."""

_INSPECT_BYTES: Final = 64 * 1024
"""Leading bytes re-read to describe a reply that turned out not to be audio."""

_PLACEHOLDER: Final = re.compile(r"\{(text|voice|language)\}")
"""The one substitution rule; a single pass, so a value cannot cascade."""

_URL_PREFIXES: Final = ("http://", "https://")
"""What a ``json:<field>`` value must look like to be fetched rather than decoded."""

_NO_TEXT_REASON: Final = "http_tts has no text to speak after removing control characters"
"""Sentence for a phrase that sanitised away to nothing."""

_NO_PROVIDER_REASON: Final = (
    "no TTS provider is selected for the http_tts engine — choose one in Settings → Speech"
)
"""Sentence for a press when the provider selection vanished between the two reads."""


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    """What one ``POST /api/tts/providers/{id}/test`` attempt measured.

    ``latency_ms`` is the **measured** wall time of the round trip, or ``None``
    when no answer was usable, so a failure is never dressed up as a fast
    success. ``format`` names the container the provider declares and ``error``
    is the engine's own already-redacted sentence; nothing here carries a file
    path or a secret.
    """

    ok: bool
    latency_ms: float | None
    format: str | None
    error: str | None


class HttpTtsEngine:
    """Speak through the configured HTTP TTS provider (D25).

    Construction is inert: no client is built, no setting is read and nothing
    raises. The provider registry and the API key are read per call, which is
    what makes a provider edit apply on the next press.
    """

    name: Final = "http_tts"

    def __init__(
        self,
        source: ProviderSource,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        render_budget_s: float = HTTP_TTS_RENDER_BUDGET_S,
        providers: ConfiguredProviders | None = None,
    ) -> None:
        """Store the ports and the budget; build and connect nothing.

        :param source: settings reader owning ``tts.providers`` and the secrets,
            normally ``ConfigStore``. Used only when *providers* is ``None``.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests. It is never
            closed by this engine.
        :param client_factory: factory for a client this engine owns and closes;
            ``None`` builds a default client per request set.
        :param render_budget_s: seconds one render attempt may take.
        :param providers: the provider registry to read; ``None`` builds
            :class:`~chime_deck.tts.providers.ConfiguredProviders` over
            *source*, which reads nothing until it is asked a question.
        """
        self._providers: ConfiguredProviders = (
            providers if providers is not None else ConfiguredProviders(source)
        )
        self._client: httpx.AsyncClient | None = client
        self._client_factory: Callable[[], httpx.AsyncClient] | None = client_factory
        self._render_budget_s: float = render_budget_s

    async def warmup(self) -> None:
        """Do nothing; there is no worker to prime and no socket to open."""
        return None

    async def available(self) -> Availability:
        """Report whether ``http_tts`` can speak, from the configuration alone.

        Never raises and **never probes the provider**: this runs once per press,
        and a paid endpoint must not be pinged on every press — nor may a probe
        inside the 9 s press deadline spend the render's own budget on a guess.
        The four honest states come from
        :meth:`~chime_deck.tts.providers.ConfiguredProviders.availability`:
        no provider configured, none selected, the selected one gone, and a
        missing API key. A real failure surfaces at render time as a classified
        :class:`~chime_deck.tts.errors.TtsRenderError` with at most one
        bounded retry (HSD-96k6).
        """
        try:
            return await self._providers.availability()
        except Exception as exc:
            _LOGGER.warning("the http_tts availability check failed unexpectedly", exc_info=True)
            return Availability(
                False,
                f"the http_tts availability check failed unexpectedly "
                f"({type(exc).__name__}) — check the doorbell log",
            )

    async def render(
        self,
        text: str,
        options: TtsOptions,
        *,
        destination: Path,
        provider: ResolvedProvider | None = None,
    ) -> Path:
        """Ask the selected provider for *text* and publish the audio at *destination*.

        The whole attempt — the request, the reply, and the one extra fetch a
        ``json:<field>`` provider may need — runs inside
        :data:`HTTP_TTS_RENDER_BUDGET_S`. The bytes are streamed into a hidden
        sibling and moved into place only after they were proven to be the
        container the preset declares and to carry audible sound, so a failed,
        rejected or cancelled render leaves nothing behind.

        :param text: phrase to speak, already the templated phrase.
        :param options: the resolver's options; unused for the request itself,
            because the provider record is authoritative for the voice, the
            language and the format. Its ``preferred_format`` already chose the
            ``destination`` suffix on the resolver's side.
        :param destination: exact output path, including its extension.
        :param provider: the provider to speak through; ``None`` (every press)
            uses ``tts.http_tts.provider``. :meth:`probe` passes one explicitly,
            so the test route can ask a provider that is not the selected one.
        :raises TtsRenderError: if no audible audio was produced.
        :raises OSError: if the validated file could not be published; the
            resolver catches this next to :class:`TtsRenderError`.
        """
        if provider is None:
            try:
                provider = await self._providers.selected()
                if provider is None:
                    # The registry's own sentence is the honest one: it names which
                    # of the four states (none configured, none selected, the
                    # selected one gone, no key) made the render impossible.
                    verdict = await self._providers.availability()
                    raise _error(verdict.reason or _NO_PROVIDER_REASON, "")
            except TtsRenderError:
                raise
            except Exception as exc:
                # A store that cannot be read is a classified render failure like any
                # other: the press log must show a sentence, never a traceback.
                _LOGGER.warning("could not read the http_tts provider settings", exc_info=True)
                raise _error(
                    f"the http_tts provider settings could not be read ({type(exc).__name__}) — "
                    "check the doorbell log",
                    "",
                ) from None
        phrase = _sanitise(text)
        if not phrase:
            raise _error(_NO_TEXT_REASON, provider.api_key)
        if len(phrase) > HTTP_TTS_TEXT_MAX_CHARS:
            raise _error(_too_long_reason(len(phrase)), provider.api_key)
        secret = provider.api_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        try:
            # The resolver's ``_bounded(deadline)`` wraps this call, so the
            # remaining press budget is enforced outside; this budget is the
            # ceiling one render may spend on its own.
            async with asyncio.timeout(self._render_budget_s):
                head, content_type = await self._attempt(provider, phrase, tmp)
            self._publish(
                tmp, destination, head, content_type=content_type, provider=provider, secret=secret
            )
        except TimeoutError:
            raise _error(
                _timed_out_reason(provider, self._render_budget_s), secret, transient=True
            ) from None
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()
        _LOGGER.debug(
            "http_tts rendered %d characters through provider %s",
            len(phrase),
            provider.record.id,
        )
        return destination

    async def probe(self, provider: ResolvedProvider, text: str) -> ProviderProbe:
        """Speak *text* through *provider* and measure it, for the test route.

        The test route must be able to ask a provider that is **not** the
        selected one, so the record is passed in rather than read from
        ``tts.http_tts.provider``. The render writes into a fresh temporary
        directory that is always removed, and every failure becomes a
        :class:`ProviderProbe` with ``ok=False`` instead of an exception: the
        route answers ``200`` with a verdict, like the settings page's other
        connection tests.

        :param provider: the provider to speak through, selected or not.
        :param text: the phrase to speak; the test route uses one fixed phrase.
        """
        started = time.monotonic()
        secret = provider.api_key
        with tempfile.TemporaryDirectory(prefix="doorbell-tts-probe-") as raw_dir:
            destination = Path(raw_dir) / f"probe.{provider.shape.format}"
            options = TtsOptions(
                engine_id=provider.record.id,
                language=provider.record.language,
                voice=provider.record.voice,
                preferred_format=provider.shape.format,
            )
            try:
                await self.render(text, options, destination=destination, provider=provider)
            except TtsRenderError as exc:
                return ProviderProbe(ok=False, latency_ms=None, format=None, error=str(exc))
            except (OSError, ValueError) as exc:
                return ProviderProbe(
                    ok=False,
                    latency_ms=None,
                    format=None,
                    error=_redact(
                        f"{_label(provider)} could not be tested because the audio could not "
                        f"be written ({type(exc).__name__}) — check the doorbell's free disk space",
                        secret,
                    ),
                )
            elapsed_ms = (time.monotonic() - started) * 1000.0
        return ProviderProbe(
            ok=True,
            latency_ms=round(elapsed_ms, 1),
            format=provider.shape.format,
            error=None,
        )

    async def estimate_cost(self, text: str) -> TtsCost | None:
        """Return what *text* costs through the selected provider, or ``None``.

        The count is the **sanitised** text — the characters actually sent — so
        the estimate matches the bill. ``None`` means "nothing to estimate":
        no provider is selected, the engine is not set up, or the lookup failed.
        A cost lookup must never fail a press, so every failure is swallowed and
        logged at debug level.

        :param text: the phrase as it will be sent, after templating.
        """
        try:
            return await self._providers.estimate_cost(_sanitise(text))
        except Exception as exc:
            _LOGGER.debug("could not estimate the http_tts cost: %s: %s", type(exc).__name__, exc)
            return None

    async def _attempt(self, provider: ResolvedProvider, text: str, tmp: Path) -> tuple[bytes, str]:
        """Send one request and extract the audio into *tmp*.

        Every transport failure is translated here, inside the caller's deadline,
        so the render's error ladder stays one flat set of sentences.

        :param provider: the selected provider, its shape and its key.
        :param text: sanitised phrase to speak.
        :param tmp: temporary file that receives the audio bytes.
        :returns: the first twelve bytes of the audio and the reply's media type,
            each ``""`` when absent.
        :raises TtsRenderError: for every failure, with a whole sentence.
        """
        secret = provider.api_key
        shape = provider.shape
        try:
            headers = self._headers(provider, text)
            body = _body(shape, provider, text)
            url = _url(provider, text)
            async with self._transport() as raw:
                request = raw.build_request(shape.method, url, content=body, headers=headers)
                response = await raw.send(request, stream=True, follow_redirects=False)
                try:
                    return await self._consume(raw, response, provider, tmp)
                finally:
                    await _close(response)
        except TtsRenderError:
            # Already classified at the seam it came from, ``transient`` included.
            raise
        except TimeoutError:
            # A transport-level timeout: worth one bounded second attempt.
            raise _error(
                _timed_out_reason(provider, self._render_budget_s), secret, transient=True
            ) from None
        except (httpx.InvalidURL, ValueError, OverflowError) as exc:
            # The request cannot be built: the address or a shape field must change.
            raise _error(_bad_request_reason(provider, exc, secret), secret) from None
        except httpx.HTTPError as exc:
            # Refused, reset, timed out: the same request could work next time.
            raise _error(_transport_reason(provider, exc, secret), secret, transient=True) from None

    async def _consume(
        self,
        raw: httpx.AsyncClient,
        response: httpx.Response,
        provider: ResolvedProvider,
        tmp: Path,
    ) -> tuple[bytes, str]:
        """Validate the answer's status and extract its audio into *tmp*.

        :param raw: the client that sent the request, reused for the one extra
            fetch a ``json:<field>`` URL answer needs.
        :param response: the streamed answer.
        :param provider: the provider being asked.
        :param tmp: temporary file that receives the bytes.
        :raises TtsRenderError: if the answer is an error, not audio, not the
            declared container or larger than the caps.
        """
        status = response.status_code
        if status >= 400:
            detail = await _reply_message(response, provider.api_key)
            raise _error(
                _status_reason(provider, status, detail),
                provider.api_key,
                # 408/429/5xx are worth one bounded second attempt; 401/403 need
                # a key, 422 needs a different phrase and 404 needs a different
                # address, so none of those may be retried.
                transient=status in {408, 429} or status >= 500,
            )
        content_type = _header(response, "content-type").split(";")[0].strip().lower()
        field = provider.shape.response_field
        if field is None:
            return await _stream_audio(response, tmp, provider, content_type)
        reply = await _read_reply(response, provider)
        payload = _decode_json(reply, provider, content_type)
        value = _walk(payload, field)
        if not isinstance(value, str) or not value:
            raise _error(
                _missing_field_reason(
                    provider, field, _envelope_message(payload, provider.api_key)
                ),
                provider.api_key,
            )
        if value.startswith(_URL_PREFIXES):
            return await self._fetch_audio(raw, value, provider, tmp)
        audio = _decode_base64_audio(value, field, provider)
        if len(audio) > HTTP_TTS_MAX_AUDIO_BYTES:
            raise _error(_too_large_reason(provider), provider.api_key)
        tmp.write_bytes(audio)
        return audio[:_HEAD_BYTES], ""

    async def _fetch_audio(
        self,
        raw: httpx.AsyncClient,
        url: str,
        provider: ResolvedProvider,
        tmp: Path,
    ) -> tuple[bytes, str]:
        """Fetch the audio a ``json:<field>`` answer pointed at, once.

        :param raw: the client that sent the request, inside the same deadline.
        :param url: absolute ``http(s)`` address the reply named.
        :param provider: the provider being asked.
        :param tmp: temporary file that receives the bytes.
        :raises TtsRenderError: if the download failed or the body is not audio.
        """
        try:
            request = raw.build_request("GET", url)
        except (httpx.InvalidURL, ValueError, OverflowError) as exc:
            raise _error(
                _bad_request_reason(provider, exc, provider.api_key), provider.api_key
            ) from None
        response = await raw.send(request, stream=True, follow_redirects=False)
        try:
            status = response.status_code
            if status >= 400:
                detail = await _reply_message(response, provider.api_key)
                raise _error(
                    _status_reason(provider, status, detail),
                    provider.api_key,
                    transient=status in {408, 429} or status >= 500,
                )
            content_type = _header(response, "content-type").split(";")[0].strip().lower()
            return await _stream_audio(response, tmp, provider, content_type)
        finally:
            await _close(response)

    def _publish(
        self,
        tmp: Path,
        destination: Path,
        head: bytes,
        *,
        content_type: str,
        provider: ResolvedProvider,
        secret: str,
    ) -> None:
        """Prove *tmp* is audible audio of the declared container, then publish it.

        :param tmp: temporary file that received the bytes.
        :param destination: exact output path, including its extension.
        :param head: first twelve bytes of the body.
        :param content_type: the reply's media type, used only to describe a
            reply that turned out not to be audio.
        :param provider: the provider that answered.
        :param secret: the API key, redacted out of every sentence.
        :raises TtsRenderError: if the bytes are not audio, are a different
            container than the preset declares, or carry no audible sound.
        """
        sniffed = sniff_container(head)
        if sniffed is None:
            raise _error(_not_audio_reason(tmp, provider, content_type, secret), secret)
        if sniffed != provider.shape.format:
            raise _error(_format_mismatch_reason(provider, sniffed), secret)
        validate_rendered_audio(tmp, container=sniffed, label=_label(provider))
        os.replace(tmp, destination)

    def _headers(self, provider: ResolvedProvider, text: str) -> dict[str, str]:
        """Build the request headers, with the substituted and authenticated values.

        :param provider: the provider being asked.
        :param text: sanitised phrase to speak.
        :raises TtsRenderError: when the shape authenticates with a key and no
            key is stored — the request is never sent in that case.
        """
        if provider.needs_api_key and not provider.has_api_key:
            raise _error(_no_key_reason(provider), provider.api_key)
        headers = {
            name: _fill(value, _values(provider, text))
            for name, value in provider.shape.headers.items()
        }
        auth = provider.shape.auth
        if auth.style == "bearer":
            headers["Authorization"] = f"Bearer {provider.api_key}"
        elif auth.style == "header":
            headers[auth.name] = f"{auth.prefix}{provider.api_key}"
        return headers

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


def _sanitise(text: str) -> str:
    """Drop control characters, collapse whitespace and strip *text*.

    A control character becomes a space rather than vanishing, so
    ``"door\\nbell"`` reads ``"door bell"`` instead of being glued together; the
    run of whitespace that may leave is collapsed by the final ``split``. This is
    the same rule :func:`chime_deck.tts.templating.sanitise_render` applies
    at the template seam, without its code-point cap — a phrase this long is
    refused by sentence instead of silently truncated.

    :param text: phrase as the resolver handed it over.
    """
    kept = "".join(" " if ch == "\x7f" or (ord(ch) < 0x20 and ch != " ") else ch for ch in text)
    return " ".join(kept.split())


def _values(provider: ResolvedProvider, text: str) -> dict[str, str]:
    """Return the three substitution values of one provider request.

    :param provider: the provider being asked; its record is authoritative for
        the voice and the language.
    :param text: sanitised phrase to speak.
    """
    return {
        "text": text,
        "voice": provider.record.voice,
        "language": provider.record.language,
    }


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace every known placeholder in *template* in one single pass.

    One pass, not three ``str.replace`` calls: a phrase that literally contains
    ``{voice}`` must survive as text instead of being substituted a second time.

    :param template: text carrying ``{text}``, ``{voice}`` or ``{language}``.
    :param values: the replacement for each placeholder name.
    """
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def _substitute(body: object, values: Mapping[str, str]) -> object:
    """Return *body* with every string leaf's placeholders substituted.

    :param body: a preset JSON body: a string, an object or an array.
    :param values: the replacement for each placeholder name.
    """
    if isinstance(body, str):
        return _fill(body, values)
    if isinstance(body, Mapping):
        return {
            key: _substitute(value, values)
            for key, value in cast(Mapping[str, object], body).items()
        }
    if isinstance(body, list):
        return [_substitute(item, values) for item in cast(list[object], body)]
    return body


def _body(shape: ProviderShape, provider: ResolvedProvider, text: str) -> bytes:
    """Build the request body for this provider's shape.

    A ``json`` body is substituted leaf by leaf and serialised with
    :func:`json.dumps`, never string-formatted, so a quote in the phrase cannot
    break the request. A ``text`` body (Azure's SSML) substitutes into the raw
    string with the phrase XML-escaped as text and the voice and language
    escaped as attribute values.

    :param shape: the provider's request shape.
    :param provider: the provider being asked.
    :param text: sanitised phrase to speak.
    :raises TtsRenderError: when the preset's body does not match its kind; that
        cannot happen through the validated preset files, but a caller-built
        shape must fail with a sentence rather than a traceback.
    """
    if shape.body_kind == "text":
        template = shape.body
        if not isinstance(template, str):
            raise _error(_bad_body_reason(provider, "text"), provider.api_key)
        values = {
            "text": html.escape(text),
            "voice": html.escape(provider.record.voice),
            "language": html.escape(provider.record.language),
        }
        return _fill(template, values).encode("utf-8")
    return json.dumps(_substitute(shape.body, _values(provider, text))).encode("utf-8")


def _url(provider: ResolvedProvider, text: str) -> str:
    """Join the preset's path onto the provider's address, with the path quoted.

    :param provider: the provider being asked; ``base_url`` has no trailing ``/``.
    :param text: sanitised phrase to speak.
    """
    quoted = {
        name: urllib.parse.quote(value, safe="") for name, value in _values(provider, text).items()
    }
    return provider.base_url.rstrip("/") + _fill(provider.shape.path, quoted)


async def _stream_audio(
    response: httpx.Response,
    tmp: Path,
    provider: ResolvedProvider,
    content_type: str,
) -> tuple[bytes, str]:
    """Stream an audio answer into *tmp*, counting the bytes as they arrive.

    The running count, not ``Content-Length``, enforces the cap: a server that
    omits the length would otherwise be trusted to be honest about its size.

    :param response: the streamed answer.
    :param tmp: temporary file that receives the bytes.
    :param provider: the provider that answered.
    :param content_type: the reply's media type, returned for the refusal sentence.
    :returns: the first twelve bytes of the body and *content_type*.
    :raises TtsRenderError: if the body exceeds :data:`HTTP_TTS_MAX_AUDIO_BYTES`.
    """
    total = 0
    head = b""
    with tmp.open("wb") as handle:
        async for chunk in response.aiter_bytes(HTTP_TTS_CHUNK_BYTES):
            total += len(chunk)
            if total > HTTP_TTS_MAX_AUDIO_BYTES:
                raise _error(_too_large_reason(provider), provider.api_key)
            if len(head) < _HEAD_BYTES:
                head = (head + chunk)[:_HEAD_BYTES]
            handle.write(chunk)
    return head, content_type


async def _read_reply(response: httpx.Response, provider: ResolvedProvider) -> bytes:
    """Read a JSON answer whole, bounded by :data:`HTTP_TTS_MAX_REPLY_BYTES`.

    :param response: the streamed answer.
    :param provider: the provider that answered.
    :raises TtsRenderError: if the body exceeds the reply cap.
    """
    reply = bytearray()
    async for chunk in response.aiter_bytes(HTTP_TTS_CHUNK_BYTES):
        reply.extend(chunk)
        if len(reply) > HTTP_TTS_MAX_REPLY_BYTES:
            raise _error(_too_large_reply_reason(provider), provider.api_key)
    return bytes(reply)


def _decode_json(reply: bytes, provider: ResolvedProvider, content_type: str) -> object:
    """Decode a JSON answer, refusing a reply the preset promised would be JSON.

    :param reply: the answer's body.
    :param provider: the provider that answered.
    :param content_type: the reply's media type, named in the refusal.
    :raises TtsRenderError: if the body is not JSON.
    """
    try:
        return json.loads(reply)
    except ValueError:
        kind = _reply_kind(reply, content_type)
        raise _error(_not_json_reason(provider, kind, content_type), provider.api_key) from None


def _walk(payload: object, field: str) -> object:
    """Return the value at the dotted *field* of *payload*, or ``None``.

    :param payload: decoded JSON reply.
    :param field: dotted path such as ``audioContent`` or ``audio.b64``.
    """
    current: object = payload
    for part in field.split("."):
        if not isinstance(current, Mapping):
            return None
        current = cast(Mapping[str, object], current).get(part)
    return current


def _decode_base64_audio(value: str, field: str, provider: ResolvedProvider) -> bytes:
    """Decode a base64 audio field, refusing anything that is not valid base64.

    :param value: the reply's text value for *field*.
    :param field: dotted path the value came from, named in the refusal.
    :param provider: the provider that answered.
    :raises TtsRenderError: if the value is not valid base64 (``binascii.Error``
        is a :class:`ValueError`, so both land here).
    """
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise _error(_bad_base64_reason(provider, field, exc), provider.api_key) from None


def _reply_kind(body: bytes, content_type: str) -> str:
    """Describe what a body that is not audio actually looks like.

    :param body: the leading bytes of the body, possibly a truncated JSON reply.
    :param content_type: the reply's media type.
    """
    head = body.lstrip()[:64].lower()
    if content_type in {"text/html", "application/xhtml+xml"} or head.startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html"
    if content_type in {"application/json", "application/problem+json"} or head.startswith(
        (b"{", b"[")
    ):
        return "json"
    return "something else"


def _envelope_message(payload: object, secret: str) -> str:
    """Return a provider's own error message from a JSON envelope, cleaned and redacted.

    Only the envelope's ``error.message`` (or a plain ``{"error": "…"}``) is used:
    an arbitrary body is not a sentence a user can act on, and it may quote
    anything the provider chose to echo.

    :param payload: decoded JSON reply.
    :param secret: the API key, redacted out of the message.
    """
    if not isinstance(payload, Mapping):
        return ""
    data = cast(Mapping[str, object], payload)
    error = data.get("error")
    if isinstance(error, str):
        return _borrowed(error, secret)
    if isinstance(error, Mapping):
        message = cast(Mapping[str, object], error).get("message")
        if isinstance(message, str):
            return _borrowed(message, secret)
    return ""


def _read_prefix(tmp: Path) -> bytes:
    """Return the leading bytes of *tmp*, or ``b""`` when it cannot be read.

    :param tmp: file that received the reply body.
    """
    try:
        with tmp.open("rb") as handle:
            return handle.read(_INSPECT_BYTES)
    except OSError:
        return b""


def _header(response: httpx.Response, name: str) -> str:
    """Return one response header as text, or ``""`` when it is absent.

    ``httpx.Headers.get`` is typed ``Any``, so the value is narrowed here instead
    of being trusted.

    :param response: the answer to inspect.
    :param name: header name, case insensitive.
    """
    raw = response.headers.get(name)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


async def _reply_message(response: httpx.Response, secret: str) -> str:
    """Read a provider's own error message from an error answer.

    The body is streamed, never read whole, and only the envelope's
    ``error.message`` is used. The message is cleaned and the API key redacted
    out of it, because a provider may echo the key it just rejected.

    :param response: the streamed answer to describe.
    :param secret: the API key, redacted out of the message if it appears.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes(HTTP_TTS_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) >= HTTP_TTS_MAX_REPLY_BYTES:
            break
    try:
        payload = json.loads(bytes(body))
    except ValueError:
        return ""
    return _envelope_message(payload, secret)


def _label(provider: ResolvedProvider) -> str:
    """Return the provider's display name for a message, cleaned.

    :param provider: the provider a sentence is about.
    """
    return f"TTS provider {_borrowed(provider.record.name, '')!r}"


def _borrowed(text: str, secret: str) -> str:
    """Clean text taken from a provider, then redact *secret* out of it.

    :param text: provider or exception text that must not leak anything.
    :param secret: the API key that must never appear in a message.
    """
    return _redact(clean_detail(text), secret)


def _redact(text: str, secret: str) -> str:
    """Replace every occurrence of *secret* in *text* with a placeholder.

    :param text: text about to enter a message a user can see.
    :param secret: the API key that must never appear there.
    """
    if not secret:
        return text
    return text.replace(secret, "***")


def _error(sentence: str, secret: str, *, transient: bool = False) -> TtsRenderError:
    """Build a classified render error with *secret* redacted out of its sentence.

    Every raise in this module goes through here, so "the key never appears in a
    message or a log line" is structural rather than a habit.

    :param sentence: whole actionable sentence, safe to show once redacted.
    :param secret: the API key that must never appear in the sentence.
    :param transient: whether repeating the same request could succeed.
    """
    return TtsRenderError(_redact(sentence, secret), transient=transient)


def _no_key_reason(provider: ResolvedProvider) -> str:
    """Build the sentence for a provider that needs a key and has none.

    :param provider: the provider with no stored key.
    """
    return f"{_label(provider)} has no API key stored — add one in Settings → Speech"


def _timed_out_reason(provider: ResolvedProvider, budget_s: float) -> str:
    """Build the sentence for a provider that did not answer within its budget.

    :param provider: the provider being asked.
    :param budget_s: seconds the render was allowed.
    """
    return (
        f"{_label(provider)} did not return the speech within {budget_s:g} s — check that the "
        "provider's server is reachable and that its API key is still valid"
    )


def _transport_reason(provider: ResolvedProvider, exc: httpx.HTTPError, secret: str) -> str:
    """Build the sentence for a request that never reached or never left the provider.

    :param provider: the provider being asked.
    :param exc: the transport failure.
    :param secret: the API key, redacted out of the exception text.
    """
    return (
        f"{_label(provider)} could not be reached "
        f"({_borrowed(str(exc), secret) or type(exc).__name__}) — check the provider's base "
        "URL and that its server is running"
    )


def _bad_request_reason(provider: ResolvedProvider, exc: Exception, secret: str) -> str:
    """Build the sentence for an address or body that cannot make a request.

    :param provider: the provider being asked.
    :param exc: the failure raised while building the request.
    :param secret: the API key, redacted out of the exception text.
    """
    return (
        f"{_label(provider)} cannot be reached at its base URL "
        f"({_borrowed(str(exc), secret) or type(exc).__name__}) — check the provider's base URL "
        "and its preset"
    )


def _bad_body_reason(provider: ResolvedProvider, kind: str) -> str:
    """Build the sentence for a preset body that does not match its kind.

    :param provider: the provider being asked.
    :param kind: the preset's ``body_kind``.
    """
    return (
        f"{_label(provider)} has a {kind} body that is not usable — "
        "check the preset chosen for this provider"
    )


def _status_reason(provider: ResolvedProvider, status: int, detail: str) -> str:
    """Build the whole sentence for an answer that failed.

    :param provider: the provider being asked.
    :param status: HTTP status the provider answered with.
    :param detail: the provider's own envelope message, already cleaned and
        redacted, or ``""``.
    """
    shown = f": {detail}" if detail else ""
    if status == 401:
        return (
            f"{_label(provider)} rejected the API key{shown} — check the key stored for this "
            "provider in Settings → Speech"
        )
    if status == 403:
        return (
            f"{_label(provider)} refused the request{shown} — check what the stored API key is "
            "allowed to do"
        )
    if status == 404:
        return (
            f"{_label(provider)} answered HTTP 404{shown} — check the provider's base URL and "
            "the preset's path"
        )
    if status in {400, 409, 422}:
        return (
            f"{_label(provider)} rejected the request{shown} — check the voice and the language "
            "configured for this provider"
        )
    if status == 429:
        return (
            f"{_label(provider)} is rate-limiting the doorbell (HTTP 429){shown} — "
            "press the button again in a moment"
        )
    if status == 408:
        return (
            f"{_label(provider)} timed out on its own side (HTTP 408){shown} — "
            "press the button again in a moment"
        )
    if status >= 500:
        return (
            f"{_label(provider)} could not synthesise the speech (HTTP {status}){shown} — "
            "check the provider's status page and try again"
        )
    return (
        f"{_label(provider)} answered HTTP {status}{shown} — check the provider's base URL, its "
        "preset and its API key"
    )


def _not_audio_reason(tmp: Path, provider: ResolvedProvider, content_type: str, secret: str) -> str:
    """Build the sentence for a ``200`` answer whose bytes are not audio.

    :param tmp: file that received the reply body.
    :param provider: the provider that answered.
    :param content_type: the reply's media type, named in the sentence.
    :param secret: the API key, redacted out of any echoed message.
    """
    kind = _reply_kind(_read_prefix(tmp), content_type)
    detail = ""
    if kind == "json":
        try:
            detail = _envelope_message(json.loads(_read_prefix(tmp)), secret)
        except ValueError:
            detail = ""
    said = f" — the provider said: {detail}" if detail else ""
    return (
        f"{_label(provider)} did not return audio — the reply looked like {kind} "
        f"({content_type or 'no content type'}){said}; check the base URL, the voice and the "
        "API key"
    )


def _format_mismatch_reason(provider: ResolvedProvider, sniffed: str) -> str:
    """Build the sentence for audio in a different container than the preset says.

    :param provider: the provider that answered.
    :param sniffed: the container the bytes actually are.
    """
    return (
        f"{_label(provider)} answered {sniffed.upper()} audio but this provider is declared as "
        f"{provider.shape.format} — check the preset chosen for this provider"
    )


def _missing_field_reason(provider: ResolvedProvider, field: str, detail: str) -> str:
    """Build the sentence for a JSON reply that does not carry the audio field.

    :param provider: the provider that answered.
    :param field: dotted path the preset expected.
    :param detail: the provider's own envelope message, already cleaned, or ``""``.
    """
    said = f" — the provider said: {detail}" if detail else ""
    return (
        f"{_label(provider)} did not return audio — the reply has no {field!r} field{said}; "
        "check the preset's response field for this provider"
    )


def _not_json_reason(provider: ResolvedProvider, kind: str, content_type: str) -> str:
    """Build the sentence for a reply the preset promised would be JSON.

    :param provider: the provider that answered.
    :param kind: what the reply actually looked like.
    :param content_type: the reply's media type.
    """
    return (
        f"{_label(provider)} did not return audio — the reply looked like {kind} "
        f"({content_type or 'no content type'}) instead of the JSON its preset promises; "
        "check the base URL, the voice and the API key"
    )


def _bad_base64_reason(provider: ResolvedProvider, field: str, exc: Exception) -> str:
    """Build the sentence for an audio field that is not valid base64.

    :param provider: the provider that answered.
    :param field: dotted path the value came from.
    :param exc: the decode failure.
    """
    return (
        f"{_label(provider)} returned a {field!r} value that is not valid base64 audio "
        f"({type(exc).__name__}) — check the preset's response field for this provider"
    )


def _too_large_reason(provider: ResolvedProvider) -> str:
    """Build the sentence for an audio answer larger than the shared size cap.

    :param provider: the provider that answered.
    """
    megabytes = HTTP_TTS_MAX_AUDIO_BYTES / (1024 * 1024)
    return (
        f"{_label(provider)} sent more than {megabytes:g} MB of speech — the download was "
        "abandoned; a doorbell phrase is never that long"
    )


def _too_large_reply_reason(provider: ResolvedProvider) -> str:
    """Build the sentence for a JSON answer larger than the reply cap.

    :param provider: the provider that answered.
    """
    megabytes = HTTP_TTS_MAX_REPLY_BYTES / (1024 * 1024)
    return (
        f"{_label(provider)} sent a reply larger than {megabytes:g} MB — the download was "
        "abandoned; a doorbell phrase never produces a reply that large"
    )


def _too_long_reason(chars: int) -> str:
    """Build the sentence for a phrase longer than this engine will send.

    :param chars: length of the sanitised phrase.
    """
    return (
        f"the phrase is {chars} characters, longer than the {HTTP_TTS_TEXT_MAX_CHARS}-character "
        "ceiling a provider request allows — shorten the button's text"
    )


async def _close(target: httpx.Response | httpx.AsyncClient) -> None:
    """Close a response or a client, bounded, logging a failure at debug level.

    :param target: the response or client to close.
    """
    try:
        await asyncio.wait_for(target.aclose(), HTTP_TTS_CLOSE_GRACE_S)
    except (TimeoutError, OSError, RuntimeError) as exc:
        _LOGGER.debug("could not close an http_tts resource: %s", exc)
