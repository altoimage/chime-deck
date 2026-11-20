"""The ``http_tts`` provider engine: presets, auth, extraction, secrecy and cost.

Fully offline. Every request is answered by ``httpx.MockTransport``, so no
socket is opened, no provider is contacted and no API key leaves the process.
The provider registry reads a dict-backed stub standing in for ``ConfigStore``
(the same shape ``tests/test_tts_providers.py`` uses), so nothing here reads
``data/doorbell.db``.

The error ladder is covered failure shape by failure shape, and each file
failure also asserts the same two facts: the ``destination`` was never created
and the directory the engine was writing into is empty, so a hidden ``.tmp``
sibling can never survive a rejected render. The headline test is
:func:`test_a_provider_defined_only_by_preset_and_credentials_synthesises`:
the acceptance criterion for HSD-s856 is that a provider made of a preset plus
an address, a voice and a key — and nothing else — speaks.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from chime_deck.tts import (
    HttpTtsEngine,
    SynthesisCosting,
    TtsCost,
    TtsOptions,
    TtsRenderError,
    sniff_container,
)
from chime_deck.tts.http_tts import (
    HTTP_TTS_RENDER_BUDGET_S,
    HTTP_TTS_TEXT_MAX_CHARS,
)

pytestmark = pytest.mark.asyncio

ID: Final = "0123456789ab"
"""Provider id the records under test use; twelve lower-case hex characters."""

KEY: Final = "sk-test-provider-key-abc123"
"""The API key the stub store holds. It must never appear in a message or a log."""

BASE: Final = "http://provider.test"
"""The provider address the stub records point at; never resolved by a test."""

VOICE: Final = "alloy"
"""The voice the stub record configures."""

LANGUAGE: Final = "en_GB"
"""The language the stub record configures."""

TEXT: Final = "Someone is at the door"
"""The phrase every render test speaks."""

_SLOW_S: Final = 0.2
"""Sleep of the deliberately slow handler; far longer than the budget under test."""

_HTTP_TIMEOUT_S: Final = 5.0
"""Bound for one mock-transport request."""

_Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
"""One scripted transport answer: a coroutine function awaiting the request it answers."""


class _Source:
    """A dict-backed stand-in for ``ConfigStore``'s provider-facing reads."""

    def __init__(
        self,
        values: Mapping[str, object] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        """Create the stub with no providers and nothing selected.

        :param values: setting values to override.
        :param secrets: provider id to stored API key.
        """
        self.values: dict[str, object] = {"tts.providers": [], "tts.http_tts.provider": ""}
        if values is not None:
            self.values.update(values)
        self.secrets: dict[str, str] = dict(secrets or {})

    async def get_setting(self, key: str) -> object:
        """Return the configured value for *key*, or ``None``.

        :param key: setting key.
        """
        return self.values.get(key)

    async def get_provider_secret(self, provider_id: str) -> str:
        """Return the stored API key for *provider_id*, or ``""``.

        :param provider_id: 12-character hexadecimal provider id.
        """
        return self.secrets.get(provider_id, "")


class _BrokenSource:
    """A source whose every read raises, to prove ``available()`` never raises."""

    async def get_setting(self, key: str) -> object:
        """Raise, as a store that cannot be read would.

        :param key: setting key that is never answered.
        """
        raise RuntimeError("the store is gone")

    async def get_provider_secret(self, provider_id: str) -> str:
        """Raise, as a store that cannot be read would.

        :param provider_id: provider id that is never answered.
        """
        raise RuntimeError("the store is gone")


class _Recorder:
    """Answer every request through one handler and record each request."""

    def __init__(self, handler: _Handler) -> None:
        """Create the recorder.

        :param handler: the transport answer every request is given.
        """
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it.

        :param request: the request httpx is about to send.
        """
        self.requests.append(request)
        return await self._handler(request)


def _mp3(payload: bytes = b"") -> bytes:
    """Build bytes that sniff as MP3 and are large enough to count as audio.

    A real ``ID3`` tag followed by padding: the engine only sniffs the leading
    bytes and checks a plausible size for a non-WAV container, so this is all a
    unit test needs.

    :param payload: extra bytes appended after the tag.
    """
    return b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\xff\xfb\x90\x00" * 40 + payload


def _wav(frames: int = 800, rate: int = 8000) -> bytes:
    """Build a silent mono 16-bit WAV with *frames* sample frames.

    :param frames: number of sample frames, which decide the duration.
    :param rate: sample rate in hertz.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _audio(body: bytes | None = None, content_type: str = "audio/mpeg") -> httpx.Response:
    """Build an audio answer.

    :param body: response body; ``None`` uses an MP3.
    :param content_type: media type the provider declares.
    """
    return httpx.Response(
        200, headers={"content-type": content_type}, content=_mp3() if body is None else body
    )


def _replies(build: Callable[[], httpx.Response]) -> _Handler:
    """Return a handler that builds a fresh answer for every request.

    A single :class:`httpx.Response` can only be streamed once, so any route a
    test hits more than once is built per request.

    :param build: constructs the answer for one request.
    """

    async def respond(request: httpx.Request) -> httpx.Response:
        """Answer *request* with a newly built response.

        :param request: the request being answered.
        """
        return build()

    return respond


def _raises(error: Exception) -> _Handler:
    """Return a handler that raises *error* for every request.

    This is how a transport failure is modelled: ``httpx.MockTransport`` sees a
    raising handler exactly as the client sees a refused connection.

    :param error: exception the transport raises.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        """Raise the configured transport error.

        :param request: the request being answered.
        """
        raise error

    return answer


async def _slow(request: httpx.Request) -> httpx.Response:
    """Answer only after far longer than any budget under test.

    :param request: the request being answered.
    """
    await asyncio.sleep(_SLOW_S)
    return _audio()


def _record(preset: str = "openai", **changes: object) -> dict[str, object]:
    """Return one valid provider record, with *changes* applied over it.

    :param preset: preset id the record names.
    :param changes: fields to replace.
    """
    raw: dict[str, object] = {
        "id": ID,
        "name": "Kitchen voice",
        "preset": preset,
        "base_url": BASE,
        "voice": VOICE,
        "language": LANGUAGE,
        "paid": True,
        "cost_per_1k_chars_usd": None,
        "overrides": {},
    }
    raw.update(changes)
    return raw


def _engine(
    handler: _Handler,
    *,
    records: object = None,
    selected: str = ID,
    secrets: Mapping[str, str] | None = None,
    render_budget_s: float = HTTP_TTS_RENDER_BUDGET_S,
) -> tuple[HttpTtsEngine, _Recorder]:
    """Build an engine over a mock-transport client and return it and the recorder.

    :param handler: the transport answer every request is given.
    :param records: value of ``tts.providers``; one valid record when omitted.
    :param selected: value of ``tts.http_tts.provider``.
    :param secrets: provider id to stored API key; the test key when omitted.
    :param render_budget_s: seconds one render attempt may take.
    """
    stored = [_record()] if records is None else records
    keys = {ID: KEY} if secrets is None else secrets
    source = _Source({"tts.providers": stored, "tts.http_tts.provider": selected}, keys)
    recorder = _Recorder(handler)
    raw = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder), timeout=httpx.Timeout(_HTTP_TIMEOUT_S)
    )
    return (
        HttpTtsEngine(source, client=raw, render_budget_s=render_budget_s),
        recorder,
    )


def _json_body(request: httpx.Request) -> dict[str, object]:
    """Return a sent request's body decoded as a JSON object.

    :param request: request the engine sent.
    """
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


async def _refusal(engine: HttpTtsEngine, destination: Path, text: str = TEXT) -> TtsRenderError:
    """Render *text* and return the refusal the engine raised.

    :param engine: engine under test.
    :param destination: exact output path the render was asked for.
    :param text: phrase to speak.
    """
    with pytest.raises(TtsRenderError) as caught:
        await engine.render(text, TtsOptions(), destination=destination)
    return caught.value


def _no_leftovers(directory: Path, destination: Path) -> None:
    """Assert the failed render left neither the destination nor a temp file.

    :param directory: directory the engine was writing into.
    :param destination: exact output path the render was asked for.
    """
    assert not destination.exists()
    assert list(directory.iterdir()) == []


# ------------------------------------------------------------------- synthesis


async def test_a_provider_defined_only_by_preset_and_credentials_synthesises(
    tmp_path: Path,
) -> None:
    """The headline: a preset plus an address, a voice and a key is enough.

    Nothing about the request is hand-written in this test — no overrides, no
    path, no body — because the acceptance criterion for HSD-s856 is that the
    bundled ``openai`` preset alone produces the documented request: a bearer
    token, the phrase and the voice in a JSON body, and real MP3 bytes back.

    :param tmp_path: pytest temporary directory.
    """
    destination = tmp_path / "out.mp3"
    engine, recorder = _engine(_replies(_audio))

    assert await engine.render(TEXT, TtsOptions(), destination=destination) == destination

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/v1/audio/speech"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    body = _json_body(request)
    assert body["input"] == TEXT
    assert body["voice"] == VOICE
    assert destination.is_file()
    assert sniff_container(destination.read_bytes()[:12]) == "mp3"


async def test_elevenlabs_puts_the_voice_in_the_path_and_sends_its_header(
    tmp_path: Path,
) -> None:
    """A path placeholder is percent-encoded, and the key travels in its own header.

    :param tmp_path: pytest temporary directory.
    """
    voice = "21m00Tcm4TlvDq8ikWAM/with space"
    engine, recorder = _engine(_replies(_audio), records=[_record("elevenlabs", voice=voice)])

    await engine.render(TEXT, TtsOptions(), destination=tmp_path / "out.mp3")

    request = recorder.requests[0]
    assert str(request.url).startswith(
        f"{BASE}/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM%2Fwith%20space"
    )
    assert request.headers["xi-api-key"] == KEY
    assert "authorization" not in request.headers


async def test_google_decodes_base64_audio_into_the_file(tmp_path: Path) -> None:
    """A ``json:audioContent`` reply is decoded, written and validated.

    :param tmp_path: pytest temporary directory.
    """
    audio = _mp3(b"tail")
    reply = {"audioContent": base64.b64encode(audio).decode("ascii")}
    engine, recorder = _engine(
        _replies(lambda: httpx.Response(200, json=reply)),
        records=[_record("google", voice="en-GB-Standard-A")],
    )

    destination = tmp_path / "out.mp3"

    assert await engine.render(TEXT, TtsOptions(), destination=destination) == destination
    assert destination.read_bytes() == audio
    assert sniff_container(destination.read_bytes()[:12]) == "mp3"
    assert len(recorder.requests) == 1


async def test_a_json_reply_naming_an_audio_url_is_fetched_once(tmp_path: Path) -> None:
    """A ``json:<field>`` value that is an http(s) URL is downloaded, not decoded.

    :param tmp_path: pytest temporary directory.
    """
    audio_url = f"{BASE}/files/speech.mp3"

    async def answer(request: httpx.Request) -> httpx.Response:
        """Answer the synthesis with a URL and that URL with the audio.

        :param request: the request being answered.
        """
        if str(request.url) == audio_url:
            return _audio()
        return httpx.Response(200, json={"audio": {"url": audio_url}})

    overrides: dict[str, object] = {
        "method": "POST",
        "path": "/speak",
        "headers": {"Content-Type": "application/json"},
        "auth": {"style": "none"},
        "body_kind": "json",
        "body": {"text": "{text}"},
        "response": "json:audio.url",
        "format": "mp3",
    }
    engine, recorder = _engine(answer, records=[_record("custom", overrides=overrides)])

    destination = tmp_path / "out.mp3"

    assert await engine.render(TEXT, TtsOptions(), destination=destination) == destination
    assert len(recorder.requests) == 2
    assert str(recorder.requests[1].url) == audio_url
    assert recorder.requests[1].method == "GET"
    assert sniff_container(destination.read_bytes()[:12]) == "mp3"


async def test_azure_sends_ssml_with_the_phrase_xml_escaped(tmp_path: Path) -> None:
    """A ``text`` body is SSML: the phrase is escaped as text, the voice as an attribute.

    :param tmp_path: pytest temporary directory.
    """
    phrase = 'Tom & Jerry <3 "quoted"'
    voice = "en-GB-SoniaNeural"
    engine, recorder = _engine(_replies(_audio), records=[_record("azure", voice=voice)])

    await engine.render(phrase, TtsOptions(), destination=tmp_path / "out.mp3")

    request = recorder.requests[0]
    assert request.headers["content-type"] == "application/ssml+xml"
    assert request.headers["ocp-apim-subscription-key"] == KEY
    body = request.content.decode("utf-8")
    assert f'<voice name="{voice}">' in body
    assert "Tom &amp; Jerry &lt;3 &quot;quoted&quot;" in body


# ------------------------------------------------------------------- rejection


async def test_an_html_200_body_is_refused_and_never_published(tmp_path: Path) -> None:
    """A web page dressed as audio is refused, named, and leaves no file behind.

    :param tmp_path: pytest temporary directory.
    """
    page = b"<!doctype html><html><body>Not found</body></html>"
    engine, _recorder = _engine(_replies(lambda: httpx.Response(200, html=page.decode())))
    destination = tmp_path / "out.mp3"

    error = await _refusal(engine, destination)

    message = str(error)
    assert "did not return audio" in message
    assert "html" in message
    assert "text/html" in message
    assert error.transient is False
    _no_leftovers(tmp_path, destination)


async def test_a_json_200_error_body_is_refused_with_the_providers_message(
    tmp_path: Path,
) -> None:
    """A JSON envelope instead of audio is refused with the provider's own words.

    :param tmp_path: pytest temporary directory.
    """
    envelope = {"error": {"message": "invalid voice: alloy"}}
    engine, _recorder = _engine(_replies(lambda: httpx.Response(200, json=envelope)))
    destination = tmp_path / "out.mp3"

    error = await _refusal(engine, destination)

    message = str(error)
    assert "did not return audio" in message
    assert "json" in message
    assert "invalid voice: alloy" in message
    assert error.transient is False
    _no_leftovers(tmp_path, destination)


async def test_a_format_mismatch_names_both_containers(tmp_path: Path) -> None:
    """WAV bytes for a provider declared as mp3 are refused, naming both.

    :param tmp_path: pytest temporary directory.
    """
    engine, _recorder = _engine(_replies(lambda: _audio(body=_wav(), content_type="audio/wav")))
    destination = tmp_path / "out.mp3"

    error = await _refusal(engine, destination)

    message = str(error)
    assert "WAV" in message
    assert "mp3" in message
    assert error.transient is False
    _no_leftovers(tmp_path, destination)


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_a_server_side_failure_is_transient(tmp_path: Path, status: int) -> None:
    """408, 429 and 5xx could succeed if the same render were repeated.

    :param tmp_path: pytest temporary directory.
    :param status: HTTP status the provider answers with.
    """
    engine, _recorder = _engine(_replies(lambda: httpx.Response(status, json={"error": "busy"})))

    error = await _refusal(engine, tmp_path / "out.mp3")

    assert error.transient is True
    assert f"HTTP {status}" in str(error) or str(status) in str(error)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_a_refusal_is_permanent(tmp_path: Path, status: int) -> None:
    """A refused request needs a setting, a key or a phrase to change first.

    :param tmp_path: pytest temporary directory.
    :param status: HTTP status the provider answers with.
    """
    engine, _recorder = _engine(
        _replies(lambda: httpx.Response(status, json={"error": {"message": "no"}}))
    )

    error = await _refusal(engine, tmp_path / "out.mp3")

    assert error.transient is False


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("too slow"),
        httpx.ReadTimeout("too slow"),
    ],
)
async def test_a_transport_failure_is_transient(tmp_path: Path, error: Exception) -> None:
    """A connection that failed or timed out is worth one bounded retry.

    :param tmp_path: pytest temporary directory.
    :param error: transport failure the handler raises.
    """
    engine, _recorder = _engine(_raises(error))

    assert (await _refusal(engine, tmp_path / "out.mp3")).transient is True


async def test_an_engine_side_timeout_is_transient(tmp_path: Path) -> None:
    """A provider that never answers within the render budget is transient.

    :param tmp_path: pytest temporary directory.
    """
    engine, _recorder = _engine(_slow, render_budget_s=0.05)
    destination = tmp_path / "out.mp3"

    error = await _refusal(engine, destination)

    assert error.transient is True
    assert "0.05 s" in str(error)
    _no_leftovers(tmp_path, destination)


async def test_a_bad_base64_payload_is_permanent(tmp_path: Path) -> None:
    """A field that is not base64 is a preset problem, not a transient one.

    :param tmp_path: pytest temporary directory.
    """
    engine, _recorder = _engine(
        _replies(lambda: httpx.Response(200, json={"audioContent": "not base64!!"})),
        records=[_record("google", voice="en-GB-Standard-A")],
    )

    error = await _refusal(engine, tmp_path / "out.mp3")

    assert error.transient is False
    assert "audioContent" in str(error)


async def test_an_unknown_json_field_is_permanent(tmp_path: Path) -> None:
    """A reply without the field the preset promises is a preset problem.

    :param tmp_path: pytest temporary directory.
    """
    engine, _recorder = _engine(
        _replies(lambda: httpx.Response(200, json={"audio": "AAAA"})),
        records=[_record("google", voice="en-GB-Standard-A")],
    )

    error = await _refusal(engine, tmp_path / "out.mp3")

    assert error.transient is False
    assert "audioContent" in str(error)


async def test_a_json_provider_answering_html_is_permanent(tmp_path: Path) -> None:
    """A reply the preset promised would be JSON, but is a web page, is refused.

    :param tmp_path: pytest temporary directory.
    """
    engine, _recorder = _engine(
        _replies(lambda: httpx.Response(200, text="<html>nope</html>")),
        records=[_record("google", voice="en-GB-Standard-A")],
    )
    destination = tmp_path / "out.mp3"

    error = await _refusal(engine, destination)

    assert error.transient is False
    assert "html" in str(error)
    _no_leftovers(tmp_path, destination)


# --------------------------------------------------------------------- secrecy


async def test_the_api_key_never_appears_in_a_sentence_or_a_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that echoes the rejected key cannot leak it anywhere.

    The key travels in a header only, so it cannot reach the log through the
    URL; the one route left is the provider's own error body, which is exactly
    what this 401 does.

    :param tmp_path: pytest temporary directory.
    :param caplog: log capture used to inspect every record the engine emitted.
    """
    echoed = {"error": {"message": f"Incorrect API key provided: {KEY}"}}
    engine, _recorder = _engine(_replies(lambda: httpx.Response(401, json=echoed)))

    with caplog.at_level(logging.DEBUG):
        error = await _refusal(engine, tmp_path / "out.mp3")

    assert KEY not in str(error)
    assert "***" in str(error)
    assert KEY not in caplog.text
    assert all(KEY not in record.getMessage() for record in caplog.records)


async def test_a_missing_api_key_is_refused_before_the_request_is_sent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No key stored means no request: the sentence names the provider and the fix.

    :param tmp_path: pytest temporary directory.
    :param caplog: log capture used to inspect every record the engine emitted.
    """
    engine, recorder = _engine(_replies(_audio), secrets={})

    with caplog.at_level(logging.DEBUG):
        error = await _refusal(engine, tmp_path / "out.mp3")

    assert recorder.requests == []
    assert "no API key stored" in str(error)
    assert "Settings → Speech" in str(error)
    assert error.transient is False
    assert KEY not in caplog.text


# ------------------------------------------------------------------ availability


async def test_available_reports_the_four_honest_states() -> None:
    """Every unconfigured state is one whole sentence, and none of them probes.

    The stub transport is never handed to ``available()``: it answers from the
    configuration alone, which is why a paid endpoint is never pinged per press.
    """
    unconfigured, _ = _engine(_replies(_audio), records=[])
    not_selected, _ = _engine(_replies(_audio), selected="")
    gone, _ = _engine(_replies(_audio), selected="fedcba987654")
    no_key, _ = _engine(_replies(_audio), secrets={})
    ready, recorder = _engine(_replies(_audio))

    assert (await unconfigured.available()).ok is False
    assert "no TTS provider is configured" in str((await unconfigured.available()).reason)
    assert (await not_selected.available()).ok is False
    assert "is selected for the http_tts engine" in str((await not_selected.available()).reason)
    assert (await gone.available()).ok is False
    assert "no longer configured" in str((await gone.available()).reason)
    assert (await no_key.available()).ok is False
    assert "no API key stored" in str((await no_key.available()).reason)
    assert (await ready.available()).ok is True
    assert recorder.requests == []


async def test_available_never_raises_when_the_source_does() -> None:
    """A store that cannot be read becomes a sentence, never a traceback."""
    engine = HttpTtsEngine(_BrokenSource())

    verdict = await engine.available()

    assert verdict.ok is False
    assert "failed unexpectedly" in str(verdict.reason)


async def test_construction_and_warmup_are_inert() -> None:
    """Building the engine touches nothing, and warmup opens nothing."""
    engine = HttpTtsEngine(_BrokenSource())

    await engine.warmup()

    assert isinstance(engine, SynthesisCosting)


# ------------------------------------------------------------------ text hygiene


@pytest.mark.parametrize("text", ["", "   ", "\x00\x01\x7f"])
async def test_blank_text_is_refused_with_a_sentence(tmp_path: Path, text: str) -> None:
    """Whitespace or control characters only cannot be spoken.

    :param tmp_path: pytest temporary directory.
    :param text: phrase the caller hands over.
    """
    engine, recorder = _engine(_replies(_audio))

    error = await _refusal(engine, tmp_path / "out.mp3", text=text)

    assert "no text to speak" in str(error)
    assert recorder.requests == []


async def test_control_characters_are_stripped_before_the_phrase_is_sent(
    tmp_path: Path,
) -> None:
    """A control character becomes a space and the run of spaces is collapsed.

    :param tmp_path: pytest temporary directory.
    """
    engine, recorder = _engine(_replies(_audio))

    await engine.render("Hello\x00world\n\tagain  ", TtsOptions(), destination=tmp_path / "o.mp3")

    assert _json_body(recorder.requests[0])["input"] == "Hello world again"


async def test_an_over_long_phrase_is_refused_naming_the_ceiling(tmp_path: Path) -> None:
    """A phrase past the sanity ceiling is refused rather than truncated.

    :param tmp_path: pytest temporary directory.
    """
    engine, recorder = _engine(_replies(_audio))

    error = await _refusal(engine, tmp_path / "out.mp3", text="a" * (HTTP_TTS_TEXT_MAX_CHARS + 1))

    assert str(HTTP_TTS_TEXT_MAX_CHARS) in str(error)
    assert recorder.requests == []


# ------------------------------------------------------------------------ cost


async def test_estimate_cost_counts_the_sanitised_text_at_the_records_price() -> None:
    """The estimate counts what is sent and uses the record's own price."""
    engine, _recorder = _engine(
        _replies(_audio), records=[_record(cost_per_1k_chars_usd=0.02, paid=True)]
    )

    cost = await engine.estimate_cost("  Hello \n world  ")

    assert cost == TtsCost(chars=len("Hello world"), usd=len("Hello world") * 0.02 / 1000.0)
    assert cost is not None
    assert cost.note() == f"{len('Hello world')} characters billed, estimated $0.0002"


async def test_estimate_cost_is_unknown_without_a_selection_or_a_price() -> None:
    """No provider, or a provider with no price, answers ``None`` — never zero."""
    unselected, _ = _engine(_replies(_audio), selected="")
    unpriced, _ = _engine(
        _replies(_audio),
        records=[_record("custom", paid=False, cost_per_1k_chars_usd=None)],
    )

    assert await unselected.estimate_cost(TEXT) is None
    cost = await unpriced.estimate_cost(TEXT)
    assert cost == TtsCost(chars=len(TEXT), usd=None)
    assert cost is not None
    assert "cost unknown" in cost.note()


async def test_a_broken_store_is_a_classified_render_failure(tmp_path: Path) -> None:
    """A store that cannot be read produces a sentence, never a traceback.

    :param tmp_path: pytest temporary directory.
    """
    engine = HttpTtsEngine(_BrokenSource())

    error = await _refusal(engine, tmp_path / "out.mp3")

    assert error.transient is False
    assert "provider settings could not be read" in str(error)
    _no_leftovers(tmp_path, tmp_path / "out.mp3")


async def test_estimate_cost_never_raises() -> None:
    """A broken store makes the estimate unanswerable, not fatal."""
    engine = HttpTtsEngine(_BrokenSource())

    assert await engine.estimate_cost(TEXT) is None


async def test_the_default_registry_reads_the_source_the_engine_was_given() -> None:
    """With no registry injected, the engine reads ``tts.providers`` from the store."""
    source = _Source({"tts.providers": [_record()], "tts.http_tts.provider": ID}, {ID: KEY})
    engine = HttpTtsEngine(source)

    assert engine.name == "http_tts"
    assert (await engine.available()).ok is True
