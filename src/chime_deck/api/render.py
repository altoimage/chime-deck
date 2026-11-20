"""The renderer's HTTP surface: one token-gated render endpoint and a health probe.

Two routes live here, both under ``/api`` and both belonging to the **renderer
role** (:mod:`chime_deck.renderer`), never to the doorbell UI:

* ``GET /api/health`` — answers "ok" to a caller that already holds the token;
* ``POST /api/tts/render`` — turns a phrase into a WAV, bounded in size, rate,
  queue depth and time.

The status contract is fixed, and every row leaves in the project's standard
error envelope (:mod:`chime_deck.api.errors`):

=================================  ======  ======================  ================
condition                          status  envelope code           extra header
=================================  ======  ======================  ================
rendered                           200     —                       ``X-HSD-Engine``
missing or wrong bearer token      401     ``unauthorized``        ``WWW-Authenticate``
malformed, non-UTF-8 or bad body   422     ``validation_error``    —
unsupported engine or option       422     ``validation_error``    —
too many requests from one address 429     ``rate_limited``        ``Retry-After``
render queue full                  429     ``queue_full``          ``Retry-After``
waited too long for the lock       429     ``queue_full``          ``Retry-After``
engine cannot speak right now      503     ``engine_unavailable``  —
render timed out, failed, too big  503     ``engine_unavailable``  —
=================================  ======  ======================  ================

**Why there is no Pydantic body model.** Two rules are only provable when the
body is read by hand: the bearer-token check must run **before** any body
validation, so an unauthenticated flood is refused before anything is buffered,
and a body that is not valid UTF-8 must be a ``422`` rather than a ``500`` out
of the JSON path. A plain ``Request`` parameter plus explicit helpers give both,
plus one place that can bound the number of bytes read.

**The text validator is the last line, not a nicety.** This is the only endpoint
in the project that accepts arbitrary text from the network and burns CPU on it,
so the phrase is checked for length and for control characters, and the options
are checked before they can reach an engine's argv. Where the checks meet the
client's own (:mod:`chime_deck.tts.remote_tts`), they are deliberately
repeated: a renderer that disagrees with its caller about what is legal produces
a confusing ``422`` on one side and a silent success on the other.

Owning ticket: HSD-5de8.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import unicodedata
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..auth import bearer_token
from ..renderer import RENDER_MAX_BODY_BYTES, RenderQueueFull
from ..tts import MAX_AUDIO_BYTES, TtsOptions, TtsRenderError, clean_detail, voice_id_problem
from ..tts.engines import TtsEngine
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..renderer import RendererState

__all__ = ["PRESERVED_ORTHOGRAPHIC_CONTROLS", "build_render_router"]

_LOGGER = logging.getLogger(__name__)

PRESERVED_ORTHOGRAPHIC_CONTROLS: Final[frozenset[str]] = frozenset({"\u200c", "\u200d"})
"""The only format characters a phrase may contain.

Both are Unicode ``Cf`` (format) characters, and both are orthographic in
Persian, Arabic and several Indic scripts: deleting or rejecting them would
change how a word is written. Every other ``Cc``, ``Cf`` and ``Cs`` character is
a ``422``. The set is the same as
:data:`chime_deck.tts.templating._PRESERVED_JOINERS`, and a test pins the two
equal so they cannot drift apart.
"""

_NO_TOKEN: Final = (
    "the renderer needs a bearer token — send 'Authorization: Bearer <token>' matching "
    "tts.renderer.token on this renderer"
)
"""The one 401 sentence; it names the fix and never the value."""

_BODY_TOO_LARGE: Final = (
    f"the request body is larger than {RENDER_MAX_BODY_BYTES} bytes — "
    "a doorbell phrase is never that long"
)
"""Sentence for a body over the read cap."""

_OPTION_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
"""What an option string may look like: no leading ``-``, no slash, no spaces."""

_SPEED_MIN: Final = 80
_SPEED_MAX: Final = 450


def _unauthorised() -> ApiError:
    """Build the ``401`` every authentication failure answers with."""
    return ApiError(
        401,
        ErrorCode.UNAUTHORIZED,
        _NO_TOKEN,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorise(state: RendererState, request: Request) -> None:
    """Refuse the request unless it carries the configured bearer token.

    Fails closed when the configured token is empty, so an empty configured token
    can never match an empty presented one. The comparison is constant-time over
    pre-encoded bytes, and the submitted value is never echoed, logged or put in
    an exception.

    :param state: renderer state holding the expected token.
    :param request: incoming request.
    :raises ApiError: ``401`` for a missing, malformed or wrong token.
    """
    if not state.token:
        raise _unauthorised()
    presented = bearer_token(request.headers.get("authorization"))
    if presented is None or not hmac.compare_digest(presented, state.token):
        raise _unauthorised()


def _client_key(request: Request) -> str:
    """Return the request's source address, or ``"unknown"``.

    :param request: incoming request; ``client`` is ``None`` under some ASGI
        servers.
    """
    return request.client.host if request.client is not None else "unknown"


def _rate_limit(state: RendererState, request: Request) -> None:
    """Refuse the request when its source address has spent its window.

    The key is the TCP peer address and never ``X-Forwarded-For``: that header is
    supplied by the client and would let one caller claim a fresh identity per
    request.

    :param state: renderer state holding the limiter.
    :param request: incoming request.
    :raises ApiError: ``429`` carrying the seconds left in the window.
    """
    seconds_left = state.limiter.allow(_client_key(request))
    if seconds_left is None:
        return
    wait_s = max(1, math.ceil(seconds_left))
    raise ApiError(
        429,
        ErrorCode.RATE_LIMITED,
        f"too many render requests from this address — wait {wait_s} s and try again",
        {"reason": "rate_limited"},
        headers={"Retry-After": str(wait_s)},
    )


def _declared_length(request: Request) -> int | None:
    """Return the declared ``Content-Length``, or ``None`` when absent or unusable.

    :param request: incoming request.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing anything larger than the cap.

    Stays **after** the token check on purpose: an unauthenticated flood must not
    be able to make the renderer buffer anything at all. A declared length over
    the cap is refused without reading a byte; a body that lies about its length
    is abandoned as soon as the cap is crossed.

    :param request: incoming request.
    :raises ApiError: ``422`` for a body over the cap.
    """
    declared = _declared_length(request)
    if declared is not None and declared > RENDER_MAX_BODY_BYTES:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BODY_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > RENDER_MAX_BODY_BYTES:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BODY_TOO_LARGE)
    return bytes(body)


def _parse_payload(raw: bytes) -> tuple[str, str, Mapping[str, object]]:
    """Decode and structurally validate one render request.

    Decoding happens here rather than in a model parser so that a body which is
    not UTF-8, or not JSON, is a clean ``422`` instead of an exception out of the
    JSON library.

    :param raw: request body bytes.
    :returns: the phrase, the requested engine name and the raw options object.
    :raises ApiError: ``422`` for anything structurally wrong.
    """
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the request body is not UTF-8 text — send a JSON object with a 'text' field",
        ) from None
    try:
        payload = json.loads(decoded)
    except ValueError:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the request body is not valid JSON — send a JSON object with a 'text' field",
        ) from None
    if not isinstance(payload, dict):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the request body must be a JSON object with a 'text' field",
        )
    body: Mapping[str, object] = payload
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the request body has no 'text' string — send the phrase to speak",
        )
    engine = body.get("engine", "")
    if not isinstance(engine, str):
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR, "the request field 'engine' must be a string"
        )
    options = body.get("options", {})
    if not isinstance(options, dict):
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR, "the request field 'options' must be an object"
        )
    return text, engine, options


def _control_problem(char: str) -> str | None:
    """Describe why *char* is not allowed in a phrase, or ``None`` when it is.

    :param char: one code point from the submitted phrase.
    """
    category = unicodedata.category(char)
    if category in {"Cc", "Cs"}:
        return f"a control character (U+{ord(char):04X})"
    if category == "Cf" and char not in PRESERVED_ORTHOGRAPHIC_CONTROLS:
        return f"a format character (U+{ord(char):04X}) that is not an orthographic joiner"
    return None


def _clean_text(state: RendererState, text: str) -> str:
    """Validate the phrase this renderer is asked to speak.

    :param state: renderer state holding the length cap.
    :param text: submitted phrase.
    :returns: the phrase, unchanged.
    :raises ApiError: ``422`` when it is empty, too long or contains a character
        the engine must never see.
    """
    if not text.strip():
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR, "the text is empty — send the phrase to speak"
        )
    if len(text) > state.max_text_chars:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"the text is {len(text)} characters long and this renderer speaks at most "
            f"{state.max_text_chars} — shorten the phrase",
        )
    for char in text:
        problem = _control_problem(char)
        if problem is not None:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                f"the text contains {problem} — send plain text with no control characters",
            )
    return text


def _option_string(options: Mapping[str, object], key: str) -> str:
    """Read one string option, rejecting anything that is not a plain token.

    An empty string means "absent, not an override", which mirrors the client: it
    sends exactly the fields it means, so a value it did not send must not become
    an override.

    :param options: raw options object.
    :param key: option name.
    :raises ApiError: ``422`` when the value is not a string or not a plain token.
    """
    value = options.get(key, "")
    if not isinstance(value, str):
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR, f"the options field {key!r} must be a string"
        )
    if value == "":
        return ""
    if _OPTION_TOKEN.fullmatch(value) is None:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"the options field {key!r} must be 1-64 characters of letters, digits, '.', '_', "
            "'+' or '-', and must start with a letter or a digit",
        )
    return value


def _option_speed(options: Mapping[str, object]) -> int:
    """Read the ``speed`` option as whole words per minute.

    ``0`` means "absent, not an override", exactly like an empty string. A bool is
    rejected even though Python treats it as an int: ``true`` is not a speed.

    :param options: raw options object.
    :raises ApiError: ``422`` when the value is not an integer in range.
    """
    value = options.get("speed", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the options field 'speed' must be a whole number of words per minute",
        )
    if value == 0:
        return 0
    if not _SPEED_MIN <= value <= _SPEED_MAX:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"the options field 'speed' must be between {_SPEED_MIN} and {_SPEED_MAX} words per "
            f"minute, got {value}",
        )
    return value


def _option_format(options: Mapping[str, object]) -> None:
    """Reject a format request, because this renderer speaks only WAV.

    The field is accepted and ignored while it says ``""`` or ``"wav"``, so a
    client that always sends it keeps working; anything else is a caller error
    rather than a silent substitution.

    :param options: raw options object.
    :raises ApiError: ``422`` for an explicit non-WAV format.
    """
    value = options.get("preferred_format", "")
    if not isinstance(value, str):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the options field 'preferred_format' must be a string",
        )
    if value in {"", "wav"}:
        return
    raise ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        f"this renderer speaks only 'wav', not {clean_detail(value)!r}",
    )


def _parse_options(options: Mapping[str, object], engine_name: str) -> TtsOptions:
    """Turn the raw options object into :class:`~chime_deck.tts.TtsOptions`.

    Unknown keys are ignored, so a newer client cannot break an older renderer. A
    voice for the ``piper`` engine is checked with the engine's own
    :func:`~chime_deck.tts.piper.voice_id_problem`, which is also the argv and
    path-traversal guard: the renderer must not disagree with ``PiperEngine``
    about what names a model file.

    :param options: raw options object; ``{}`` when the client sent none.
    :param engine_name: requested engine name, which decides the voice rule.
    :raises ApiError: ``422`` for any value that is not accepted.
    """
    engine_id = _option_string(options, "engine_id")
    language = _option_string(options, "language")
    voice = _option_string(options, "voice")
    speed = _option_speed(options)
    _option_format(options)
    if engine_name == "piper" and voice:
        problem = voice_id_problem(voice)
        if problem is not None:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, problem)
    return TtsOptions(
        engine_id=engine_id,
        language=language,
        voice=voice,
        speed=speed,
        preferred_format="wav",
    )


def _unknown_engine(state: RendererState, requested: str) -> ApiError:
    """Build the ``422`` for an engine name this renderer does not host.

    An unsupported name is the caller's error, so it is a ``422``; a hosted engine
    that cannot speak right now is the environment's, so that is a ``503``.

    :param state: renderer state naming what is hosted.
    :param requested: engine name from the request, possibly empty.
    """
    if not requested:
        return ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"every render request must name an engine — this renderer hosts {state.hosted}",
        )
    return ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        f"this renderer hosts only {state.hosted}, so {clean_detail(requested)!r} cannot be "
        f"rendered here — ask for {state.hosted}",
    )


def _resolve_engine(state: RendererState, requested: str) -> TtsEngine:
    """Return the engine the request names.

    :param state: renderer state holding the hosted engines.
    :param requested: engine name from the request.
    :raises ApiError: ``422`` when this renderer does not host that name.
    """
    engine = state.engines.get(requested)
    if engine is None:
        raise _unknown_engine(state, requested)
    return engine


def _queue_full(message: str) -> ApiError:
    """Build the ``429`` for a queue that refused the request.

    :param message: sentence naming the limit that was hit.
    """
    return ApiError(
        429,
        ErrorCode.QUEUE_FULL,
        message,
        {"reason": "queue_full"},
        headers={"Retry-After": "1"},
    )


def _lock_wait_expired() -> ApiError:
    """Build the ``429`` for a request that never got the synthesis lock."""
    return ApiError(
        429,
        ErrorCode.QUEUE_FULL,
        "the renderer is busy — nothing was synthesised",
        {"reason": "queue_wait"},
        headers={"Retry-After": "1"},
    )


def _render_failed(reason: str, message: str) -> ApiError:
    """Build a ``503`` for a render that could not produce audio.

    :param reason: machine-readable ``detail.reason`` marker.
    :param message: whole sentence naming what went wrong.
    """
    return ApiError(503, ErrorCode.ENGINE_UNAVAILABLE, message, {"reason": reason})


def _render_timeout(state: RendererState) -> ApiError:
    """Build the ``503`` for a render that outlived its deadline.

    :param state: renderer state holding the deadline.
    """
    return _render_failed(
        "timeout",
        f"the render took longer than {state.synthesis_timeout_s:g} s and was abandoned — check "
        "the renderer log",
    )


def _too_large() -> ApiError:
    """Build the ``503`` for a rendered file over the shared audio size cap."""
    megabytes = MAX_AUDIO_BYTES / (1024 * 1024)
    return _render_failed(
        "too_large",
        f"the rendered speech is larger than {megabytes:g} MB — a doorbell phrase is never that "
        "long",
    )


def _render_error(exc: TtsRenderError | OSError) -> ApiError:
    """Build the ``503`` for a render that failed, keeping the engine's sentence.

    :param exc: error raised by the engine or by the filesystem.
    """
    return _render_failed("engine_failed", clean_detail(str(exc)) or type(exc).__name__)


async def _synthesise(
    state: RendererState,
    engine: TtsEngine,
    text: str,
    options: TtsOptions,
    client: str,
) -> Response:
    """Render one phrase and return it as a WAV response.

    The synthesis lock bounds *waiting* (``429``; nothing was rendered) while the
    render deadline bounds *working* (``503``), because the caller must be able to
    tell "busy, retry" from "broken, look at the log". The spool file is unlinked
    in a ``finally`` on every path, so a failed render leaves nothing behind.

    :param state: renderer state holding the lock, deadlines and spool.
    :param engine: engine that will synthesise the phrase.
    :param text: already validated phrase.
    :param options: already validated render options.
    :param client: source address, used in the one success log line.
    :raises ApiError: ``429`` while waiting for the lock, ``503`` for any render
        failure.
    """
    destination = state.spool_dir / f".render-{uuid4().hex}.wav"
    started = state.clock()
    acquired = False
    try:
        try:
            async with asyncio.timeout(state.queue_wait_s):
                await state.synthesis_lock.acquire()
                acquired = True
        except TimeoutError:
            raise _lock_wait_expired() from None
        try:
            async with asyncio.timeout(state.synthesis_timeout_s):
                await engine.render(text, options, destination=destination)
        except TimeoutError:
            raise _render_timeout(state) from None
        except (TtsRenderError, OSError) as exc:
            raise _render_error(exc) from None
        try:
            size = destination.stat().st_size
            if size > MAX_AUDIO_BYTES:
                raise _too_large()
            content = await asyncio.to_thread(destination.read_bytes)
        except OSError as exc:
            raise _render_error(exc) from None
    finally:
        if acquired:
            state.synthesis_lock.release()
        with suppress(OSError):
            destination.unlink()

    headers = {"Cache-Control": "no-store", "X-HSD-Engine": engine.name}
    if options.voice:
        headers["X-HSD-Voice"] = options.voice
    elapsed_ms = round((state.clock() - started) * 1000)
    _LOGGER.info(
        "renderer: rendered %d characters with %s in %d ms (%d bytes) for %s",
        len(text),
        engine.name,
        elapsed_ms,
        len(content),
        client,
    )
    return Response(content=content, media_type="audio/wav", headers=headers)


def build_render_router(state: RendererState) -> APIRouter:
    """Build the ``/api`` router of the renderer role, bound to *state*.

    Handlers are closures over *state*, following the house style of
    :mod:`chime_deck.app`: no handler annotates the state type, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: renderer state holding the token, the engines and the policy
        objects.
    """
    router = APIRouter(prefix="/api", tags=["renderer"])

    @router.get("/health")
    async def renderer_health(request: Request) -> JSONResponse:
        """Report that this renderer is alive, to a caller that holds the token.

        Gated **on purpose**: the client's ``available()`` probe uses this answer
        to tell a rejected token from an unreachable host, so a supervisor needs
        the token too. It touches no engine and no probe, so it stays cheap enough
        to answer on a busy renderer. The main app's unauthenticated
        ``/api/health`` (D20) is a different route in a different process.
        """
        _authorise(state, request)
        return JSONResponse(
            {
                "status": "ok",
                "role": "renderer",
                "version": state.version,
                "uptime_s": round(max(0.0, state.clock() - state.started_monotonic), 3),
            }
        )

    @router.post("/tts/render")
    async def render_speech(request: Request) -> Response:
        """Render the requested phrase and answer with an ``audio/wav`` body.

        The order is load-bearing and must not be rearranged: token, rate limit,
        body, payload, options — all before an engine object is touched, so a
        refused request never probes or renders. Then the engine lookup, the queue
        slot, the availability probe and finally the bounded render. The lookup
        precedes the slot on purpose: a permanently wrong engine name is the
        caller's error, so it must be told with its own ``422`` even while every
        queue slot is busy, rather than being answered ``429`` as if retrying
        would help.
        """
        _authorise(state, request)
        _rate_limit(state, request)
        raw = await _read_body(request)
        text, engine_name, raw_options = _parse_payload(raw)
        text = _clean_text(state, text)
        options = _parse_options(raw_options, engine_name)
        client = _client_key(request)
        engine = _resolve_engine(state, engine_name)
        try:
            async with state.queue.slot():
                availability = await engine.available()
                if not availability.ok:
                    reason = availability.reason or (
                        f"the {engine.name} engine cannot speak right now — check the renderer log"
                    )
                    raise _render_failed("engine_unavailable", reason)
                return await _synthesise(state, engine, text, options, client)
        except RenderQueueFull as exc:
            raise _queue_full(str(exc)) from None

    return router
