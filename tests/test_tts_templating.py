"""Press-time templating: the render, the guards, the fallbacks and the wiring.

Fully offline. The settings registry's default ``ha.base_url`` is
``http://homeassistant.local:8123``, so **every** templated test either injects
an ``httpx.MockTransport`` client or a fake renderer — otherwise it would do a
real DNS lookup, which AGENTS.md HARD RULE 4 forbids. The store is a real
temporary SQLite database, the cache a real :class:`TtsCache` under ``tmp_path``
and the engine a :class:`FakeEngine`, so no module resolves a name, opens a
socket, runs ``espeak-ng`` or bakes a file.

Owning ticket: HSD-bmqp.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

import httpx
import pytest
import pytest_asyncio
from log_isolation import preserve_log_state
from test_press import _Clock, _FakeDispatcher, _seed_tts, _service
from test_tts_speech import AssertingEngine, FakeEngine

import chime_deck.tts.speech as speech_module
from chime_deck.config import SECRET_SCRUBBER
from chime_deck.ha import (
    HaAuthError,
    HaBadRequestError,
    HaConfigurationError,
    HaConnectionError,
    HaNotFoundError,
    HaServerError,
    HaTimeoutError,
)
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.hub import StateHub
from chime_deck.mqtt.bridge import DEFAULT_SEAM_TIMEOUT_S
from chime_deck.press import PressCode, PressOutcome, SpeechUnavailable
from chime_deck.store import ConfigStore
from chime_deck.tts import (
    FALLBACK_CHAIN,
    MAX_TEMPLATE_CHARS,
    TEMPLATE_TIMEOUT_S,
    Availability,
    HaTemplateRenderer,
    PhraseTemplating,
    RenderedPhrase,
    TtsCache,
    TtsOptions,
    TtsSpeechSource,
    build_speech_source,
    cache_key,
    sanitise_render,
)
from chime_deck.tts.ha_tts import (
    AVAILABILITY_TIMEOUT_S,
    PRESS_MARGIN_S,
    TTS_RENDER_TOTAL_TIMEOUT_S,
)
from chime_deck.tts.templating import looks_like_markup

BASE_URL: Final = "http://ha.test:8123"
TOKEN: Final = "secret-long-lived-token-abc123"
TEMPLATE_PATH: Final = "/api/template"

TEMPLATE: Final = "Someone is at the {{ states('sensor.front_door_name') or 'door' }}"
"""A dynamic phrase: it has template syntax and is rendered on every press."""

RENDERED: Final = "Someone is at the front door"
"""What the mocked Home Assistant answers the render with."""

PLAIN: Final = "Someone is at the door"
"""A static phrase: no template syntax, so it never reaches a renderer."""

_HTML: Final = "<!DOCTYPE html><html><body>Sign in</body></html>"
"""What a reverse proxy or a captive portal answers instead of rendered text."""

_HA_OPTIONS: Final = TtsOptions(engine_id="tts.piper", language="en_GB", preferred_format="wav")
"""The options the default ``ha_tts`` settings produce."""

_Item = (
    httpx.Response | Exception | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)
"""What one scripted route can hold."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and forget every registered secret after a test."""
    with preserve_log_state():
        yield


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


@pytest_asyncio.fixture
async def hub(store: ConfigStore) -> AsyncIterator[StateHub]:
    """Yield a real hub that is never started, so only explicit flushes deliver.

    :param store: connected configuration store.
    """
    instance = StateHub(store, HealthRegistry(), flush_interval=3600.0)
    yield instance
    await instance.stop()


async def _seed_ha(store: ConfigStore) -> None:
    """Store the Home Assistant settings the renderer needs.

    :param store: connected configuration store.
    """
    await store.set_setting("ha.base_url", BASE_URL)
    await store.set_setting("ha.token", TOKEN)


def _client(
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` wired to *handler*.

    :param handler: the transport handler that answers every request.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _replies(
    build: Callable[[], httpx.Response],
) -> Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]:
    """Return a handler that builds a fresh answer for every request.

    A single :class:`httpx.Response` can only be read once, so a route answered
    more than once uses this factory.

    :param build: constructs the answer for one request.
    """

    async def respond(request: httpx.Request) -> httpx.Response:
        """Answer *request* with a newly built response.

        :param request: the request being answered; unused.
        """
        return build()

    return respond


def _text(body: str) -> httpx.Response:
    """Build an unattached plain-text answer, as ``/api/template`` sends.

    :param body: the rendered phrase.
    """
    return httpx.Response(200, text=body)


class ScriptedTransport:
    """Answer ``(method, path)`` routes and record every request it receives."""

    def __init__(
        self,
        routes: Mapping[tuple[str, str], _Item] | None = None,
        *,
        default: _Item | None = None,
    ) -> None:
        """Create the handler.

        :param routes: answers keyed by ``(METHOD, url.path)``; an unknown key
            falls back to *default*.
        :param default: answer for an unscripted route; ``None`` answers ``404``.
        """
        self.routes = dict(routes) if routes is not None else {}
        self.default = default
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it with the scripted item for its route.

        :param request: the request httpx is about to send.
        """
        self.requests.append(request)
        item = self.routes.get((request.method.upper(), request.url.path), self.default)
        if item is None:
            return httpx.Response(404, text="unexpected")
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, Exception):
            raise item
        return await item(request)

    def count(self, method: str, path: str) -> int:
        """Return how many recorded requests used *method* and *path*.

        :param method: HTTP method to count.
        :param path: URL path to count.
        """
        return sum(
            1
            for request in self.requests
            if request.method.upper() == method and request.url.path == path
        )


class FakeRenderer:
    """Record every render and answer from a queued script, repeating the last.

    :class:`~chime_deck.tts.templating.TemplateRenderer` is satisfied
    structurally, so no Home Assistant call is ever made with this fake.

    :param results: queued answers; an :class:`Exception` entry is raised, any
        other object is returned verbatim.
    """

    def __init__(self, *results: object) -> None:
        """Create the renderer with its scripted answers.

        :param results: queued answers, consumed in order.
        """
        self._results = list(results)
        self._index = 0
        self.calls: list[str] = []

    async def render(self, template: str) -> object:
        """Record the call and answer from the script.

        :param template: the phrase passed to the renderer.
        """
        self.calls.append(template)
        if not self._results:
            raise AssertionError("the fake renderer has no queued answer")
        answer = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


class OrderedEngine(FakeEngine):
    """A :class:`FakeEngine` that appends its name to a shared probe-order list."""

    def __init__(
        self, name: str, order: list[str], availability: Availability | None = None
    ) -> None:
        """Create the engine.

        :param name: engine name registered with the resolver.
        :param order: shared list that records every ``available()`` call.
        :param availability: result of :meth:`available`; available by default.
        """
        super().__init__(name, availability)
        self._order = order

    async def available(self) -> Availability:
        """Record the probe in the shared order, then report availability."""
        self._order.append(self.name)
        return await super().available()


class ExplodingTemplating(PhraseTemplating):
    """A templating step that fails the test if the press path runs it at all.

    Nothing is constructed: the disabled state must not even build a renderer.
    """

    def __init__(self) -> None:
        """Create the guard without a renderer."""
        self.calls = 0

    async def resolve(self, phrase: str) -> RenderedPhrase:
        """Fail: templating must not run in a disabled state.

        :param phrase: the phrase that must not be rendered.
        """
        self.calls += 1
        raise AssertionError("the disabled state must not run templating")


# ------------------------------------------------------------------ sanitisation


_SANITISE_ROWS: Final = [
    ("Someone is at the door", "Someone is at the door"),
    ("  Someone\n is\tat  the door  ", "Someone is at the door"),
    ("Someone\x00is at the door", "Someone is at the door"),
    ("\x1b[31mALERT\x1b[0m", "ALERT"),
    ("\x1b]0;title\x07ALERT", "ALERT"),
    ("a\u200bb", "ab"),
    ("octo\u00adpus", "octopus"),
    ("abc\u202edef", "abc def"),
    ("\ufeffHello", "Hello"),
    ("a\x85b", "a b"),
    ("a\x7fb", "a b"),
    ("café", "café"),
    ("日本語", "日本語"),
    ("Fire \U0001f514 bell", "Fire \U0001f514 bell"),
    ('"; rm -rf /', '"; rm -rf /'),
    ("$HOME & 100% 'quoted'", "$HOME & 100% 'quoted'"),
    ("می\u200cشود", "می\u200cشود"),
    ("क्\u200dष", "क्\u200dष"),
    ("", ""),
    (" \t\n ", ""),
    ("\u200b", ""),
]
"""One row per sanitisation rule: ``(raw, expected)``."""


@pytest.mark.parametrize(("raw", "expected"), _SANITISE_ROWS)
def test_a_sanitise_render_removes_controls_and_keeps_text(raw: str, expected: str) -> None:
    """Each class is removed or kept exactly as the module docstring promises.

    :param raw: the untrusted answer.
    :param expected: the sanitised text.
    """
    result = sanitise_render(raw)

    assert result.text == expected
    again = sanitise_render(result.text)
    assert again.text == result.text
    assert again.warnings == ()
    assert len(result.text) <= MAX_TEMPLATE_CHARS


def test_b_a_short_render_is_not_truncated() -> None:
    """Exactly 500 code points pass untouched, with no warning."""
    raw = "a" * MAX_TEMPLATE_CHARS

    result = sanitise_render(raw)

    assert len(result.text) == MAX_TEMPLATE_CHARS
    assert result.warnings == ()


def test_b_one_character_over_the_cap_is_truncated_with_a_warning() -> None:
    """501 characters are truncated to 500 and the warning names the original."""
    raw = "a" * (MAX_TEMPLATE_CHARS + 1)

    result = sanitise_render(raw)

    assert len(result.text) == MAX_TEMPLATE_CHARS
    assert len(result.warnings) == 1
    assert "501" in result.warnings[0]
    assert "500" in result.warnings[0]


def test_b_a_long_render_is_capped_by_code_points() -> None:
    """A 5 000-character answer is capped, and a CJK one counts characters not bytes."""
    assert len(sanitise_render("a" * 5_000).text) == MAX_TEMPLATE_CHARS

    result = sanitise_render("日" * 600)

    assert len(result.text) == MAX_TEMPLATE_CHARS
    assert result.warnings


def test_b_the_cap_applies_after_whitespace_collapses() -> None:
    """A padded answer that collapses below the cap earns no truncation warning."""
    raw = "a" * (MAX_TEMPLATE_CHARS // 2) + " " * (MAX_TEMPLATE_CHARS * 2)

    result = sanitise_render(raw)

    assert len(result.text) == MAX_TEMPLATE_CHARS // 2
    assert result.warnings == ()


# ------------------------------------------------------------------------ plain


@pytest.mark.asyncio
async def test_c_plain_text_is_passed_through_without_any_request(store: ConfigStore) -> None:
    """A phrase without template syntax is byte-identical, warning-free and inert.

    :param store: connected configuration store.
    """
    await _seed_ha(store)
    handler = ScriptedTransport(
        default=AssertionError("the plain path must not reach Home Assistant")
    )
    raw = _client(handler)
    try:
        templating = PhraseTemplating(HaTemplateRenderer(store, client=raw))
        result = await templating.resolve(PLAIN)
    finally:
        await raw.aclose()

    assert result.text == PLAIN
    assert result.warnings == ()
    assert handler.requests == []


@pytest.mark.asyncio
async def test_c_a_renderer_whose_render_raises_is_never_called() -> None:
    """Plain text does not touch the renderer, not even one that would raise."""
    renderer = FakeRenderer(AssertionError("the plain path must not render"))

    result = await PhraseTemplating(renderer).resolve(PLAIN)

    assert result.text == PLAIN
    assert renderer.calls == []


# ---------------------------------------------------------------- mocked Home Assistant


@pytest.mark.asyncio
async def test_d_a_templated_press_borrows_the_client(store: ConfigStore, tmp_path: Path) -> None:
    """The renderer borrows the injected client, sends the token and speaks the render.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    await _seed_ha(store)
    button = await _seed_tts(store, text=TEMPLATE)
    handler = ScriptedTransport({("POST", TEMPLATE_PATH): _replies(lambda: _text(RENDERED))})
    raw = _client(handler)
    engine = FakeEngine("ha_tts")
    try:
        source = await build_speech_source(
            store, data_dir=tmp_path / "data", engines=[engine], ha_client=raw
        )
        clip = await source.resolve(button=button, engine="ha_tts")
        assert raw.is_closed is False
    finally:
        await raw.aclose()

    assert handler.count("POST", TEMPLATE_PATH) == 1
    request = handler.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["template"] == TEMPLATE
    assert "variables" not in body
    assert [render[0] for render in engine.renders] == [RENDERED]
    assert clip.engine == "ha_tts"


@pytest.mark.asyncio
async def test_e_variables_are_forwarded_to_the_render(store: ConfigStore) -> None:
    """An injected ``variables`` mapping travels in the request body exactly.

    :param store: connected configuration store.
    """
    await _seed_ha(store)
    handler = ScriptedTransport({("POST", TEMPLATE_PATH): _replies(lambda: _text(RENDERED))})
    raw = _client(handler)
    try:
        renderer = HaTemplateRenderer(store, client=raw, variables={"button": "front-door"})
        body = await renderer.render(TEMPLATE)
    finally:
        await raw.aclose()

    assert body == RENDERED
    request_body = json.loads(handler.requests[0].content)
    assert request_body["variables"] == {"button": "front-door"}


@pytest.mark.asyncio
async def test_f_the_cache_key_uses_the_rendered_text(store: ConfigStore, tmp_path: Path) -> None:
    """The clip is keyed by the rendered phrase, not by the raw template.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    await _seed_ha(store)
    button = await _seed_tts(store, text=TEMPLATE)
    handler = ScriptedTransport({("POST", TEMPLATE_PATH): _replies(lambda: _text(RENDERED))})
    raw = _client(handler)
    data_dir = tmp_path / "data"
    try:
        source = await build_speech_source(
            store, data_dir=data_dir, engines=[FakeEngine("ha_tts")], ha_client=raw
        )
        clip = await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    cache = TtsCache(data_dir / "tts-cache")
    rendered_key = cache_key("ha_tts", _HA_OPTIONS, RENDERED)
    raw_key = cache_key("ha_tts", _HA_OPTIONS, TEMPLATE)
    assert clip.path == cache.path_for(rendered_key, ".wav")
    assert clip.path != cache.path_for(raw_key, ".wav")


@pytest.mark.asyncio
async def test_f_two_different_renders_produce_two_clips(
    store: ConfigStore, tmp_path: Path
) -> None:
    """Live data changes the phrase, so the two presses mint two cache entries.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, text=TEMPLATE)
    engine = FakeEngine("ha_tts")
    renderer = FakeRenderer("The garage is open", "The garage is closed")
    source = TtsSpeechSource(
        store,
        [engine],
        TtsCache(tmp_path / "cache"),
        templating=PhraseTemplating(renderer),
    )

    first = await source.resolve(button=button, engine="ha_tts")
    second = await source.resolve(button=button, engine="ha_tts")

    assert first.path != second.path
    assert [render[0] for render in engine.renders] == [
        "The garage is open",
        "The garage is closed",
    ]


@pytest.mark.asyncio
async def test_g_the_template_is_rerendered_but_the_audio_is_reused(
    store: ConfigStore, tmp_path: Path
) -> None:
    """Every press re-renders the template; only the audio is deduped by the cache.

    That is deliberate: a rendered phrase is dynamic by definition (design 04
    §2.2), so the render must run even when the cache answers the clip.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, text=TEMPLATE)
    engine = FakeEngine("ha_tts")
    renderer = FakeRenderer(RENDERED)
    source = TtsSpeechSource(
        store, [engine], TtsCache(tmp_path / "cache"), templating=PhraseTemplating(renderer)
    )

    first = await source.resolve(button=button, engine="ha_tts")
    second = await source.resolve(button=button, engine="ha_tts")

    assert first.path == second.path
    assert len(engine.renders) == 1
    assert renderer.calls == [TEMPLATE, TEMPLATE]


@pytest.mark.asyncio
async def test_g_a_rendered_value_is_never_rerendered(store: ConfigStore, tmp_path: Path) -> None:
    """A render that looks like a template is spoken literally, not rendered again.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, text=TEMPLATE)
    engine = FakeEngine("ha_tts")
    value = "Value {{ not_a_template }} here"
    renderer = FakeRenderer(value)
    source = TtsSpeechSource(
        store, [engine], TtsCache(tmp_path / "cache"), templating=PhraseTemplating(renderer)
    )

    await source.resolve(button=button, engine="ha_tts")

    assert renderer.calls == [TEMPLATE]
    assert [render[0] for render in engine.renders] == [value]


# --------------------------------------------------------------------- fallbacks


_HA_FAILURES: Final = [
    pytest.param(
        HaConnectionError("Home Assistant could not be reached — check ha.base_url"),
        id="ha-connection",
    ),
    pytest.param(
        HaTimeoutError(
            "Home Assistant did not answer within 0.4 s — check ha.base_url", timeout_s=0.4
        ),
        id="ha-timeout",
    ),
    pytest.param(
        HaAuthError(
            "Home Assistant rejected the token (HTTP 401: Unauthorized) — the template API "
            "needs an admin token; the template API needs an admin token",
            status_code=401,
            detail="Unauthorized",
        ),
        id="ha-auth",
    ),
    pytest.param(
        HaBadRequestError(
            "Home Assistant refused the template (HTTP 400: template error: unexpected dot)",
            status_code=400,
            detail="template error",
        ),
        id="ha-bad-request",
    ),
    pytest.param(
        HaNotFoundError("Home Assistant has no such entity (HTTP 404)", status_code=404, detail=""),
        id="ha-not-found",
    ),
    pytest.param(
        HaServerError("Home Assistant failed (HTTP 500: boom)", status_code=500, detail="boom"),
        id="ha-server",
    ),
    pytest.param(
        HaConfigurationError("no Home Assistant token is configured — add one in Settings"),
        id="ha-configuration",
    ),
    pytest.param(TimeoutError("timed out"), id="builtin-timeout"),
    pytest.param(ValueError("not a template"), id="plain-value-error"),
    pytest.param(123, id="non-text-answer"),
]
"""One row per failed render: an exception or a non-``str`` answer."""


@pytest.mark.parametrize("failure", _HA_FAILURES)
@pytest.mark.asyncio
async def test_h_a_failed_render_speaks_the_literal_phrase(
    failure: object, store: ConfigStore, tmp_path: Path
) -> None:
    """Every failure degrades to the literal phrase with an actionable warning.

    :param failure: the exception or non-text answer the renderer produces.
    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    templating = PhraseTemplating(FakeRenderer(failure))

    result = await templating.resolve(TEMPLATE)

    assert result.text == TEMPLATE
    assert result.warnings
    warning = result.warnings[0]
    assert "Home Assistant" in warning
    assert warning.endswith("the literal phrase was spoken instead")

    button = await _seed_tts(store, text=TEMPLATE)
    engine = FakeEngine("ha_tts")
    source = TtsSpeechSource(store, [engine], TtsCache(tmp_path / "cache"), templating=templating)
    clip = await source.resolve(button=button, engine="ha_tts")

    assert [render[0] for render in engine.renders] == [TEMPLATE]
    assert clip.detail is not None
    assert warning in clip.detail


@pytest.mark.asyncio
async def test_h_a_cancellation_propagates_out_of_resolve() -> None:
    """Only ``Exception`` is caught, so a cancellation is never turned into speech."""
    renderer = FakeRenderer(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await PhraseTemplating(renderer).resolve(TEMPLATE)


# ------------------------------------------------------------------------ markup


def test_i_looks_like_markup_is_conservative() -> None:
    """A page is recognised; prose that merely contains a tag is not."""
    assert looks_like_markup(_HTML) is True
    assert looks_like_markup("<html>") is True
    assert looks_like_markup("The garage is <open>") is False
    assert looks_like_markup(PLAIN) is False


@pytest.mark.asyncio
async def test_i_a_web_page_is_never_spoken(store: ConfigStore, tmp_path: Path) -> None:
    """A proxy page is not text, so the literal phrase wins and the page is not spoken.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    templating = PhraseTemplating(FakeRenderer(_HTML))

    result = await templating.resolve(TEMPLATE)

    assert result.text == TEMPLATE
    warning = result.warnings[0]
    assert "reverse proxy" in warning
    assert "captive portal" in warning

    button = await _seed_tts(store, text=TEMPLATE)
    engine = FakeEngine("ha_tts")
    source = TtsSpeechSource(store, [engine], TtsCache(tmp_path / "cache"), templating=templating)
    await source.resolve(button=button, engine="ha_tts")

    assert [render[0] for render in engine.renders] == [TEMPLATE]
    assert "Sign in" not in engine.renders[0][0]


# ------------------------------------------------------------------------- empty


@pytest.mark.parametrize(
    ("template", "body"),
    [
        (TEMPLATE, ""),
        (TEMPLATE, "   "),
        (TEMPLATE, "\u200b"),
        (TEMPLATE, "\u200d"),
        ("{{ trigger.to_state.name }}", ""),
    ],
)
@pytest.mark.asyncio
async def test_j_an_empty_render_speaks_the_literal_phrase(
    template: str, body: str, store: ConfigStore, tmp_path: Path
) -> None:
    """An empty render never means silence and never raises ``SpeechUnavailable``.

    :param template: the button's phrase.
    :param body: the empty render Home Assistant answered.
    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    health = HealthRegistry()
    templating = PhraseTemplating(FakeRenderer(body))
    button = await _seed_tts(store, text=template)
    engine = FakeEngine("ha_tts")
    source = TtsSpeechSource(
        store,
        [engine],
        TtsCache(tmp_path / "cache"),
        health=health,
        templating=templating,
    )

    clip = await source.resolve(button=button, engine="ha_tts")

    assert [render[0] for render in engine.renders] == [template]
    assert clip.detail is not None
    assert "trigger.*" in clip.detail
    assert health.get("tts") is HealthStatus.DEGRADED


# ------------------------------------------------------------------------ budgets


def test_k_the_templated_press_fits_the_seam_budget() -> None:
    """Probe plus template plus render plus margin stays below the seam."""
    assert (
        AVAILABILITY_TIMEOUT_S + TEMPLATE_TIMEOUT_S + TTS_RENDER_TOTAL_TIMEOUT_S + PRESS_MARGIN_S
        < DEFAULT_SEAM_TIMEOUT_S
    )


# -------------------------------------------------------------- fallback chain


@pytest.mark.asyncio
async def test_l_the_chain_falls_back_in_the_documented_order(
    store: ConfigStore, tmp_path: Path
) -> None:
    """First available wins, in the documented order; later engines are not probed.

    The order is D24's, ``remote_tts`` first; ``FALLBACK_CHAIN``'s own docstring
    carries the erratum that design 04 §6.5 omits ``piper``.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    assert FALLBACK_CHAIN == ("remote_tts", "ha_tts", "piper", "espeak_ng")
    button = await _seed_tts(store, text=PLAIN)
    order: list[str] = []
    ha = OrderedEngine("ha_tts", order, Availability(False, "no engine configured"))
    piper = OrderedEngine("piper", order)
    espeak = OrderedEngine("espeak_ng", order)
    source = TtsSpeechSource(store, [ha, piper, espeak], TtsCache(tmp_path / "cache"))

    clip = await source.resolve(button=button, engine="ha_tts")

    assert clip.engine == "piper"
    assert order == ["ha_tts", "piper"]
    assert espeak.available_calls == 0


# ---------------------------------------------------------------------- press log


@pytest.mark.asyncio
async def test_m_the_press_detail_composes_fallback_and_template_warning(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """The fallback sentence comes first, the templating warning after it.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    await _seed_ha(store)
    button = await _seed_tts(store, text=TEMPLATE, dedupe_seconds=0)
    order: list[str] = []
    ha = OrderedEngine("ha_tts", order, Availability(False, "no Home Assistant TTS engine"))
    espeak = OrderedEngine("espeak_ng", order)
    renderer = FakeRenderer(HaConnectionError("Home Assistant could not be reached"))
    source = TtsSpeechSource(
        store,
        [ha, espeak],
        TtsCache(tmp_path / "cache"),
        templating=PhraseTemplating(renderer),
    )
    service = _service(
        store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=source
    )

    result = await service.press(button.slug)
    rows = await store.recent_presses()

    assert result.outcome is PressOutcome.QUEUED
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail is not None
    assert "engine espeak_ng" in detail
    assert "fell back from ha_tts:" in detail
    assert "could not render the speech template" in detail
    assert detail.index("fell back from ha_tts:") < detail.index(
        "could not render the speech template"
    )


@pytest.mark.asyncio
async def test_m_the_token_never_reaches_the_warning_or_the_press_detail(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """A rejection sentence echoing the token is scrubbed before it is stored.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    await _seed_ha(store)
    button = await _seed_tts(store, text=TEMPLATE, dedupe_seconds=0)
    rejection = HaAuthError(
        f"Home Assistant rejected the token (HTTP 401: {TOKEN}) — use the admin token",
        status_code=401,
        detail=TOKEN,
    )
    templating = PhraseTemplating(FakeRenderer(rejection))
    with preserve_log_state():
        SECRET_SCRUBBER.set_secrets([TOKEN])
        result = await templating.resolve(TEMPLATE)
        engine = FakeEngine("ha_tts")
        source = TtsSpeechSource(
            store, [engine], TtsCache(tmp_path / "cache"), templating=templating
        )
        service = _service(
            store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=source
        )
        pressed = await service.press(button.slug)

    assert pressed.outcome is PressOutcome.QUEUED
    assert result.warnings
    assert TOKEN not in result.warnings[0]
    rows = await store.recent_presses()
    detail = rows[0].detail
    assert detail is not None
    assert TOKEN not in detail
    assert TEMPLATE not in detail


@pytest.mark.asyncio
async def test_n_the_literal_is_cached_but_the_template_is_retried(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """A failed render re-runs next press, re-emits the warning and reuses the audio.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, text=TEMPLATE, dedupe_seconds=0)
    engine = FakeEngine("ha_tts")
    renderer = FakeRenderer(HaConnectionError("Home Assistant could not be reached"))
    source = TtsSpeechSource(
        store, [engine], TtsCache(tmp_path / "cache"), templating=PhraseTemplating(renderer)
    )
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=source)

    await service.press(button.slug)
    first_rows = await store.recent_presses()
    first_path = dispatcher.jobs[0].path
    await service.press(button.slug)
    second_rows = await store.recent_presses()
    second_path = dispatcher.jobs[1].path

    assert first_path == second_path
    assert len(engine.renders) == 1
    assert renderer.calls == [TEMPLATE, TEMPLATE]
    assert first_rows[0].detail is not None
    assert second_rows[0].detail is not None
    assert "could not render the speech template" in second_rows[0].detail


# --------------------------------------------------------------------------- D27


@pytest.mark.asyncio
async def test_o_the_disabled_state_never_runs_templating(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """``tts.default_engine: none`` wins before the renderer or an engine is touched.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    await store.set_setting("tts.default_engine", "none")
    button = await _seed_tts(store, text=TEMPLATE)
    engine = AssertingEngine()
    templating = ExplodingTemplating()
    source = TtsSpeechSource(store, [engine], TtsCache(tmp_path / "cache"), templating=templating)
    service = _service(
        store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=source
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_DISABLED
    assert templating.calls == 0
    assert engine.available_calls == 0
    assert engine.warmup_calls == 0


@pytest.mark.asyncio
async def test_o_build_speech_source_constructs_no_renderer_when_disabled(
    store: ConfigStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disabled build returns a disabled source and never builds a renderer.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest patch helper.
    """
    await store.set_setting("tts.default_engine", "none")
    button = await _seed_tts(store, text=TEMPLATE)

    def _forbidden(*args: object, **kwargs: object) -> object:
        """Fail: the disabled state must not construct a template renderer.

        :param args: positional constructor arguments; unused.
        :param kwargs: keyword constructor arguments; unused.
        """
        raise AssertionError("the disabled state must not construct a template renderer")

    monkeypatch.setattr(speech_module, "HaTemplateRenderer", _forbidden)

    source = await build_speech_source(store, data_dir=tmp_path / "data")

    assert isinstance(source, TtsSpeechSource)
    with pytest.raises(SpeechUnavailable, match="disabled"):
        await source.resolve(button=button, engine="ha_tts")


# --------------------------------------------------------------------- no baking


@pytest.mark.asyncio
async def test_p_a_templated_press_bakes_nothing(store: ConfigStore, tmp_path: Path) -> None:
    """A dynamic phrase is never pre-rendered: no bake state and no bake directory.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    await _seed_ha(store)
    button = await _seed_tts(store, text=TEMPLATE)
    handler = ScriptedTransport({("POST", TEMPLATE_PATH): _replies(lambda: _text(RENDERED))})
    raw = _client(handler)
    data_dir = tmp_path / "data"
    try:
        source = await build_speech_source(
            store, data_dir=data_dir, engines=[FakeEngine("ha_tts")], ha_client=raw
        )
        await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    stored = await store.get_button_by_id(button.id)
    assert stored is not None
    assert stored.baked_sha is None
    assert stored.baked_at is None
    assert stored.baked_error is None
    assert not any(path.name == "baked" for path in data_dir.rglob("*"))
