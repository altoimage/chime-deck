"""The speech resolver: choose an engine, apply settings, cache the clip.

This is the implementation behind :class:`chime_deck.press.SpeechSource`.
The press path hands over a button and the effective engine name; this module
walks the fallback chain (design 04 §7), reads the chosen engine's settings,
hashes the render into the cache and returns one playable
:class:`~chime_deck.press.SpeechClip`.

Fallback has two reasons, and both are bounded. An engine whose ``available()``
says no is skipped and the next candidate is tried. A **transient** render
failure — a transport fault, a timeout or a server-side 5xx — is now retried
**once** on the next available engine, but only when the press budget that
remains can pay for it. An auth, bad-request or not-found failure is never
retried: only a changed setting, token or name can fix those, and a second
attempt would fail the same way.

This supersedes HSD-p50k's **no render-failure fallback** ruling, whose stated
reason was the absence of exactly this deadline: a second attempt could not fit
the 10 s seam, and a cancelled press used to leave no press row at all. HSD-96k6
supplies the press-wide deadline, so the retry is allowed only where it can be
bounded. ``piper`` still latches itself unavailable after repeated timeouts, so
the chain stops paying for a wedged worker on every press.

The default engine list is ``remote_tts`` first (another doorbell in renderer mode
synthesises the speech, D24), then ``ha_tts`` (Home Assistant does the synthesis,
so the user gets whatever voice engines they already run), then the local
``piper`` (optional extra, D14) and finally ``espeak_ng``, so an install with no
Home Assistant TTS entity still speaks. ``http_tts`` (the D25 provider engine) is
registered too, but it is deliberately **not** in :data:`FALLBACK_CHAIN`: an
``http_tts`` request tries it first and then falls back to the local chain, while
no other engine may fall back *into* a paid provider, because that would bill on
every press. ``remote_tts`` is unavailable until
``tts.remote_tts.url`` and ``tts.remote_tts.token`` are both set, ``piper`` is
unavailable without the extra, without the RAM of D22 and without a downloaded
voice model, and both fall through to the next engine in every one of those
cases. Settings are read per press by each engine, so a changed
``tts.ha.engine_id`` or ``tts.default_engine`` applies on the next press; the
health snapshot only changes when a press records a new status.

The disabled state (``tts.default_engine: none``, D27) constructs no engine, no
cache, no probe and no warmup. Its health status is set at startup and never by
:meth:`TtsSpeechSource.resolve`, so the websocket snapshot grows a ``tts``
component only when the user deliberately switched speech off.

A templated phrase is rendered **at press time** (design 04 §6 step 4, with
§2.2: a dynamic phrase is never baked) by
:class:`~chime_deck.tts.templating.PhraseTemplating`, after the engine is
chosen and before the cache key is computed, so the cache key is derived from
the *rendered* text. The render and the engine probe are sequential, never
parallel.

**The press-wide deadline (HSD-96k6).** A press now carries one absolute,
monotonic budget (:class:`~chime_deck.press.PressDeadline`) rather than one
budget per engine. Every step that can block — each availability probe, the
template render, the engine render — is bounded by the time that is left, so a
chain that probes several unavailable engines before one renders can no longer
outlive the 10 s MQTT seam. When the budget runs out the press fails with a
recorded row instead of being cancelled without one, which is the failure this
module used to leave unexplained. ``deadline=None`` is the bake path and every
direct construction: it keeps each engine's own budget exactly as it was and
rules out the retry, because a retry that cannot be bounded must not be started.
One templated ``ha_tts`` press is still scoped per engine as a 1.0 s probe plus
:data:`~chime_deck.tts.templating.TEMPLATE_TIMEOUT_S` (0.5 s) plus the 7.5 s
render plus the 0.5 s publish margin — 9.5 s — and ``tests/test_tts_engines.py``
asserts that the 9.0 s deadline binds *before* that sum, so the press fails with
an honest row rather than being cancelled. The probe-failure-then-espeak press
that used to break this arithmetic stays bounded as well (HSD-4y6l):
:data:`~chime_deck.tts.engines._TIMEOUT_S` (6.0 s) plus
:data:`~chime_deck.tts.engines._KILL_GRACE_S` (1.0 s) keep it at 9.0 s, and
the same test asserts both sums.

The cache is never bypassed. The template is attempted on *every* press, so a
press whose render failed re-attempts it and re-emits the warning next time;
the literal text is served from the cache under
``cache_key(engine, options, literal)`` if it was rendered before, and the cache
stays the sole owner of a file's lifetime.

A **static** phrase (no Jinja syntax) is not resolved through the cache at all
once :class:`~chime_deck.tts.bake.BakeStore` is wired: it plays a local file
from ``<data_dir>/tts/baked/<button_id>/`` with **no TTS call at all** when its
artefact is current (design 04 §2.3), and is synthesised once into that
directory when it is not. Baking is a cost optimisation, never a correctness
requirement: a bake that cannot be produced falls back to the ephemeral cache
and the press still speaks.

The returned clip carries ``synthesis_ms``: the milliseconds the engine measured
while producing **this** press's audio. It is ``None`` whenever nothing was
synthesised for the press — a cache hit, a bake hit, or an engine that keeps no
measurement — which is a success state, never "zero" and never a degradation.
It is deliberately separate from ``detail``: a non-``None`` ``detail`` is exactly
what flips the ``tts`` health component to DEGRADED, so a timing figure must
never travel there. A retried press reports the synthesis time of the render
that actually produced its audio, never a sum and never the failed attempt's.

Owning ticket: HSD-bw8h; the default ``ha_tts`` engine was added by HSD-npt0, the
press-time template renderer by HSD-bmqp, the local ``piper`` engine by
HSD-p50k, the ``remote_tts`` client with the settings-to-options table moved
to :func:`options_for` by HSD-5de8, the durable bake branch by HSD-dzek, the
press-wide deadline with the bounded single retry by HSD-96k6 and the
``http_tts`` provider engine with the cost channel by HSD-s856.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Collection, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

import httpx

from ..health import HealthRegistry, HealthStatus
from ..press import PressDeadline, SpeechClip, SpeechSource, SpeechUnavailable
from ..store import SETTINGS, Button
from .bake import (
    BakeArtefact,
    BakeBatch,
    BakeFailure,
    BakeStore,
    BakeUsage,
    baked_root,
    directory_usage,
    expected_bake_sha,
    is_static,
)
from .cache import (
    CACHE_MAX_BYTES,
    CACHE_MAX_FILES,
    TtsCache,
    cache_key,
    suffix_for_format,
)
from .engines import (
    Availability,
    EspeakNgEngine,
    NullTtsEngine,
    SynthesisTiming,
    TtsEngine,
    TtsOptions,
)
from .errors import BakeNotApplicable, TtsRenderError
from .ha_tts import HaTtsEngine
from .http_tts import HttpTtsEngine
from .piper import PiperEngine, PiperProbe
from .presets import (
    DEFAULT_PRESETS,
    SHAPE_KEYS,
    Preset,
    PresetError,
    ProviderShape,
    shape_from_overrides,
)
from .providers import (
    ProviderRecord,
    ProviderSource,
    SynthesisCosting,
    TtsCost,
    has_template_syntax,
    read_providers,
)
from .remote_tts import RemoteTtsEngine
from .templating import TEMPLATE_TIMEOUT_S, HaTemplateRenderer, PhraseTemplating

__all__ = [
    "FALLBACK_CHAIN",
    "MIN_RETRY_RENDER_S",
    "PROBE_TIMEOUT_S",
    "RETRY_MIN_BUDGET_S",
    "BakeRecorder",
    "ClosableEngine",
    "EngineStatus",
    "SettingsReader",
    "TtsSpeechSource",
    "build_speech_source",
    "options_for",
]

_LOGGER = logging.getLogger(__name__)

FALLBACK_CHAIN: Final = ("remote_tts", "ha_tts", "piper", "espeak_ng")
"""Engines tried in order after the requested one (design 04 §7, D24).

**Erratum 13.** Design 04 §6.5 documents the order as ``remote_tts -> ha_tts ->
espeak_ng`` and omits ``piper``, which postdates it (HSD-p50k). The implemented
order keeps ``piper`` at its design 04 §7 position, because a superset must
never *remove* a local fallback: a device whose renderer is unreachable and
whose Home Assistant has no TTS entity still speaks.

Prepending ``remote_tts`` changes nothing for an existing install:
:meth:`TtsSpeechSource._chain_for` slices the chain from the requested engine's
own index, and ``press.py`` passes ``button.tts_engine or tts.default_engine``
(default ``ha_tts``), so ``remote_tts`` is never even probed on a default press.

``http_tts`` (the D25 provider engine) is deliberately **not** in this chain.
:meth:`TtsSpeechSource._chain_for` still tries it first for an ``http_tts``
request and then falls back to the local chain, because a doorbell must not go
silent when a provider is down; but no other engine may fall back **into** a
paid provider, because that would bill the user on every press.
"""

ENGINE_STATUS_TIMEOUT_S: Final = 2.0
"""Seconds one engine may take to answer an availability question on the status page."""

PROBE_TIMEOUT_S: Final = 1.5
"""Seconds one `available()` probe may take on the press path before it is written off.

This is a **backstop**, deliberately *above* every engine's own probe budget:
:data:`~chime_deck.tts.ha_tts.AVAILABILITY_TIMEOUT_S` and
:data:`~chime_deck.tts.remote_tts.REMOTE_AVAILABILITY_TIMEOUT_S` both bound
themselves at 1.0 s, so this can never truncate a working probe. It exists for
the engine that does not bound itself — ``piper.available()`` documents that it
relies on the resolver.
"""

MIN_RETRY_RENDER_S: Final = 1.0
"""Least render time that makes a second attempt worth starting."""

RETRY_MIN_BUDGET_S: Final = PROBE_TIMEOUT_S + MIN_RETRY_RENDER_S
"""Shortest remaining press budget that can pay for one bounded second attempt.

A retry has to probe the *next* candidate (:data:`PROBE_TIMEOUT_S`) and then
render it, so anything less than :data:`MIN_RETRY_RENDER_S` of render time would
guarantee the retry itself is cut off by the press deadline.
"""


def _bounded(
    deadline: PressDeadline | None, cap: float | None = None
) -> AbstractAsyncContextManager[object]:
    """Bound one step by the press deadline and by its own allowance, whichever is sooner.

    With no press deadline the step keeps today's per-engine budget — that is the
    bake path, which has no seam to fit. The element type is ``object`` because
    :func:`asyncio.timeout` yields its own handle while :func:`contextlib.nullcontext`
    yields ``None``; callers only ever enter the context, never read the value.

    :param deadline: whole-press budget, or ``None`` when the caller has none.
    :param cap: the step's own allowance in seconds, or ``None`` for no cap.
    """
    if deadline is None:
        return nullcontext()
    remaining = deadline.remaining()
    return asyncio.timeout(remaining if cap is None else min(cap, remaining))


class _RenderFailure(SpeechUnavailable):
    """A render that produced no audio, still carrying the engine's own classification.

    It *is* a :class:`~chime_deck.press.SpeechUnavailable` — that is what the
    press log shows — and the private type exists only so :meth:`resolve` can
    decide about one bounded second attempt without re-deriving anything from
    prose. It never escapes a public call in a way a caller must know about.
    """

    def __init__(self, engine: str, reason: str, *, transient: bool) -> None:
        """Record the engine, the sentence to show and whether a retry could succeed.

        :param engine: name of the engine whose render failed.
        :param reason: whole actionable sentence, as the press log will show it.
        :param transient: whether repeating the render on another engine could succeed.
        """
        self.engine: str = engine
        self.transient: bool = transient
        super().__init__(reason)


class SettingsReader(Protocol):
    """The narrow settings port, satisfied structurally by ``ConfigStore``."""

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or its registry default.

        :param key: registered setting key.
        """
        ...


@runtime_checkable
class ClosableEngine(Protocol):
    """An engine that owns a worker and can release it at shutdown."""

    def close(self) -> None:
        """Stop the engine's worker without waiting for it."""
        ...


@runtime_checkable
class BakeRecorder(Protocol):
    """The bake-state port, satisfied structurally by ``ConfigStore``.

    Both methods are checked with ``isinstance`` at wiring time, the way
    :class:`ClosableEngine` and
    :class:`~chime_deck.tts.engines.SynthesisTiming` are, so a store that
    cannot record bake state simply disables the reporting instead of failing a
    press. The return value is deliberately ``object``: the resolver wants the
    side effect, not the stored row.
    """

    async def set_bake_state(
        self,
        button_id: int,
        sha: str,
        baked_at: str | None = None,
        *,
        fmt: str | None = None,
    ) -> object:
        """Record a successful bake and clear the previous error.

        :param button_id: database id of the button.
        :param sha: digest of the text, engine and options that were baked.
        :param baked_at: bake timestamp; ``None`` means now.
        :param fmt: real container of the artefact, for example ``"mp3"``.
        """
        ...

    async def record_bake_failure(self, button_id: int, error: str) -> object:
        """Record a bake failure, leaving the button playable.

        :param button_id: database id of the button.
        :param error: whole sentence shown as "Pre-render failed: …".
        """
        ...


@dataclass(frozen=True, slots=True)
class EngineStatus:
    """One row of the speech engine status list.

    This is a read-only report: nothing here writes health, so a status page
    can be polled without changing the snapshot a client already has.
    """

    name: str
    availability: Availability
    last_synthesis_ms: int | None = None


def _speech_conformance(source: TtsSpeechSource) -> SpeechSource:
    """Return *source* typed as the press port, pinning conformance under mypy.

    :param source: the concrete resolver.
    """
    return source


@dataclass(frozen=True, slots=True)
class _RenderedClip:
    """A resolved clip path plus what producing it measured and cost.

    ``synthesis_ms`` is ``None`` when nothing was synthesised for this press — a
    cache hit, a bake hit, or an engine that keeps no measurement — which is a
    success state, not a missing one. ``cost`` is what this press actually paid;
    ``None`` when nothing was synthesised, because a hit bills nothing and
    saying otherwise would be a lie in the press history.
    """

    path: Path
    synthesis_ms: int | None = None
    cost: TtsCost | None = None


class _TimedRenderer:
    """Render one phrase and capture the engine's measurement for *this* render.

    :attr:`~chime_deck.tts.engines.SynthesisTiming.last_synthesis_ms` is the
    last *completion* (last-writer-wins), and
    :data:`~chime_deck.tts.piper.MAX_CONCURRENT_RENDERS` is 2, so reading it
    after a render this object did not perform would report a stale number from
    an earlier press. The capture therefore lives inside the renderer the cache
    and the bake store call: a cache or bake **hit** never calls the renderer, so
    a hit stays ``None`` — "nothing was synthesised for this press".

    A residual, benign race remains: with two renders in flight, a press can be
    handed the other in-flight press's number, which is still a real synthesis
    time. A press can never report a number for a render it did not perform.
    Closing the race would need a change to the :class:`TtsEngine` protocol,
    which is out of scope.
    """

    measured_ms: int | None = None
    """Milliseconds this object's own render measured, or ``None``."""

    def __init__(self, engine: TtsEngine, text: str, options: TtsOptions) -> None:
        """Create the renderer for one phrase.

        :param engine: engine that will speak.
        :param text: phrase to speak.
        :param options: the engine's options for this render.
        """
        self._engine = engine
        self._text = text
        self._options = options

    async def __call__(self, destination: Path) -> None:
        """Render into *destination* and read the measurement back at once.

        The read happens immediately after the ``await`` returns, before any
        other ``await`` in this coroutine, which is the tightest window
        available without changing the engine protocol.

        :param destination: exact output path the cache or bake store wants.
        :raises TtsRenderError: if the engine could not produce the audio.
        """
        await self._engine.render(self._text, self._options, destination=destination)
        if isinstance(self._engine, SynthesisTiming):
            self.measured_ms = self._engine.last_synthesis_ms


class TtsSpeechSource:
    """Resolve a TTS button to a cached audio file.

    Engines are registered by name; unknown names are not an error, they simply
    have no implementation and fall back. The source keeps no per-press state,
    so one instance is shared by every press.
    """

    def __init__(
        self,
        settings: SettingsReader,
        engines: Sequence[TtsEngine],
        cache: TtsCache | None,
        *,
        health: HealthRegistry | None = None,
        disabled: bool = False,
        fallback_chain: Sequence[str] = FALLBACK_CHAIN,
        templating: PhraseTemplating | None = None,
        bakes: BakeStore | None = None,
        recorder: BakeRecorder | None = None,
    ) -> None:
        """Create the resolver.

        ``templating=None`` pins the "not wired" state: the phrase is spoken
        literally with no Home Assistant call, no note and no health flip. That
        is a test-only shape — :func:`build_speech_source` always wires one.
        ``bakes=None`` disables baking entirely, so every press goes through the
        ephemeral cache; that is the shape every direct-construction caller
        keeps, and it is what makes a resolver without a data directory
        sensible.

        :param settings: settings reader used for the chosen engine's options.
        :param engines: implemented engines, registered by their ``name``.
        :param cache: clip cache, or ``None`` when speech cannot be rendered.
        :param health: registry that records the ``tts`` component.
        :param disabled: whether speech was deliberately switched off (D27).
        :param fallback_chain: ordered engine names tried after the requested one.
        :param templating: press-time template renderer, or ``None`` when it is
            deliberately not wired (literal pass-through, zero HA calls).
        :param bakes: durable pre-rendered clips, or ``None`` to disable baking.
        :param recorder: sink for per-button bake state, or ``None`` when no
            store can record it.
        """
        self._settings = settings
        self._engines = {engine.name: engine for engine in engines}
        self._cache = cache
        self._health = health
        self._disabled = disabled
        self._fallback_chain = tuple(fallback_chain)
        self._templating = templating
        self._bakes = bakes
        self._recorder = recorder
        self._logger = logging.getLogger(__name__)

    @property
    def is_disabled(self) -> bool:
        """Whether speech was deliberately switched off (D27); read-only.

        Reading this writes no health: the disabled status is recorded once, at
        startup, never by a caller that merely looks. The bare name ``disabled``
        belongs to the classmethod factory below, so this accessor cannot use it.
        """
        return self._disabled

    async def engine_status(self) -> list[EngineStatus]:
        """Return one status row per registered engine, in fallback-chain order.

        Engines named by the chain come first, in chain order, because that is
        the order a press tries them; any engine registered under another name
        follows. The probes run **concurrently**, which is what lets every probe
        take up to :data:`ENGINE_STATUS_TIMEOUT_S` and the whole list still fit
        inside the route's 5 s budget. Each probe is bounded, and a timeout
        becomes an unavailable row rather than an exception. Nothing here writes
        health.
        """
        names = list(self._fallback_chain)
        names.extend(name for name in self._engines if name not in self._fallback_chain)
        engines = [self._engines[name] for name in names if name in self._engines]
        registered = [engine.name for engine in engines]
        availabilities = await asyncio.gather(*(self._status_of(engine) for engine in engines))
        return [
            EngineStatus(
                name=name,
                availability=availability,
                last_synthesis_ms=(
                    engine.last_synthesis_ms if isinstance(engine, SynthesisTiming) else None
                ),
            )
            for name, engine, availability in zip(registered, engines, availabilities, strict=True)
        ]

    async def _status_of(self, engine: TtsEngine) -> Availability:
        """Return one engine's availability, bounded and never raising.

        A failure is answered as a row rather than an exception, so one broken
        engine cannot blank the whole list: ``available()`` promises not to
        raise, but a third-party engine that breaks that promise must lose its
        own row, not every row.

        :param engine: engine to ask.
        """
        try:
            async with asyncio.timeout(ENGINE_STATUS_TIMEOUT_S):
                return await engine.available()
        except TimeoutError:
            return Availability(
                False,
                f"{engine.name} did not answer within {ENGINE_STATUS_TIMEOUT_S:g} s — check the "
                "doorbell log",
            )
        except Exception as exc:
            self._logger.warning("the %s availability check failed", engine.name, exc_info=True)
            return Availability(
                False,
                f"the {engine.name} availability check failed ({type(exc).__name__}) — check "
                "the doorbell log",
            )

    async def aclose(self) -> None:
        """Close every registered engine that owns a worker, logging any failure.

        This is what keeps the single-worker ``piper`` threads from outliving a
        shutdown: a :class:`~concurrent.futures.thread.ThreadPoolExecutor` joins
        its worker threads at interpreter exit **without a timeout**, so a worker
        left running can hang the process — the same hazard ``app.py`` documents
        for aiosqlite (HSD-cwvc). An engine without ``close()`` (the disabled
        source's ``NullTtsEngine``, ``ha_tts`` and ``espeak_ng``) is skipped. A
        failure is logged, never raised, because shutdown must reach the store.
        """
        for engine in self._engines.values():
            if not isinstance(engine, ClosableEngine):
                continue
            try:
                engine.close()
            except Exception as exc:
                self._logger.warning(
                    "could not close the %s speech engine: %s: %s",
                    engine.name,
                    type(exc).__name__,
                    exc,
                )

    @classmethod
    def disabled(
        cls,
        settings: SettingsReader,
        *,
        health: HealthRegistry | None = None,
    ) -> TtsSpeechSource:
        """Build the deliberate off state: one null engine, no cache, no probe.

        No template renderer is constructed either: the off state is inert, so
        nothing can open a socket or read a Home Assistant setting.

        :param settings: settings reader; kept only so the port is satisfied.
        :param health: registry that already records ``tts`` as disabled.
        """
        return cls(settings, (NullTtsEngine(),), None, health=health, disabled=True)

    async def resolve(
        self, *, button: Button, engine: str, deadline: PressDeadline | None = None
    ) -> SpeechClip:
        """Turn *button* into a playable clip using *engine* or a fallback.

        Engine errors never escape raw: a render failure becomes
        :class:`~chime_deck.press.SpeechUnavailable` carrying the engine's
        sentence, because the press path logs a traceback for anything else and
        the press log would lose its shape.

        The order is fixed: the disabled/``none`` check, the blank-text check,
        the cache check (moved up, so a doomed press spends nothing), the press
        budget check, the engine choice, the engine's options, then the bake
        branch for a static phrase or the press-time template render plus the
        cache key for a dynamic one, and finally the render with at most one
        bounded retry. Every step that can block is bounded by what remains of
        ``deadline``, so the press cannot outlive the MQTT seam that wraps it.
        The fallback sentence is composed first, so the clip's ``detail`` reads
        ``fell back from …`` and then any template warning; a truthy detail that
        names an *availability condition* is exactly what makes the health
        component DEGRADED rather than READY. That is why a correct-but-non-admin
        token — the ``state()`` probe succeeds while ``/api/template`` returns
        ``401`` — still shows DEGRADED: the composed detail carries the reason. A
        retry note names one press rather than a lasting condition, so it goes
        into ``detail`` only and leaves health READY.

        A dynamic phrase is rendered here, at press time (design 04 §6 step 4,
        with §2.2), because its output depends on live state and it must never
        be baked. A static phrase plays a local file with **no TTS call at all**
        when its artefact is current (design 04 §2.3).

        The returned clip's ``synthesis_ms`` is the engine's measurement for this
        press's own render, or ``None`` when this press synthesised nothing (a
        cache or bake hit, or an engine that keeps no measurement). It never
        reaches ``detail``, because a non-``None`` ``detail`` means degraded.
        ``cost`` is what this press actually paid, ``None`` when nothing was
        synthesised: a hit is free, and the history must not claim otherwise.

        :param button: button being pressed; carries ``tts_text``.
        :param engine: effective engine name; never ``"none"`` in practice.
        :param deadline: whole-press time budget, or ``None`` when the caller has
            none (the bake paths and direct constructions).
        :raises SpeechUnavailable: if no engine can speak or the render failed.
        """
        if self._disabled or engine.strip() == "none":
            raise SpeechUnavailable("speech is disabled (tts.default_engine is 'none')")
        text = (button.tts_text or "").strip()
        if not text:
            raise SpeechUnavailable(f"button {button.slug!r} has no text to speak")
        if self._cache is None:
            raise SpeechUnavailable("no speech cache is configured")
        self._require_budget(deadline, "any engine could be tried")
        chosen, first_reason = await self._choose(engine, deadline=deadline)
        options = await self._options_for(chosen.name)
        notes: list[str] = []
        degrading: list[str] = []
        if chosen.name != engine:
            fallback = f"fell back from {engine}: {first_reason}"
            notes.append(fallback)
            degrading.append(fallback)
        static = self._bakes is not None and is_static(text)
        if not static and self._templating is not None:
            try:
                async with _bounded(deadline, TEMPLATE_TIMEOUT_S):
                    templated = await self._templating.resolve(text)
            except TimeoutError:
                raise SpeechUnavailable(
                    "the press deadline ran out while rendering this button's phrase — check "
                    "that the doorbell and its Home Assistant connection are healthy"
                ) from None
            text = templated.text
            notes.extend(templated.warnings)
            degrading.extend(templated.warnings)
        chosen, options, rendered, retry_note = await self._render_with_retry(
            button,
            engine=engine,
            chosen=chosen,
            options=options,
            text=text,
            static=static,
            deadline=deadline,
        )
        if retry_note is not None:
            notes.append(retry_note)
        detail = "; ".join(notes) or None
        if self._health is not None:
            if degrading:
                self._health.set("tts", HealthStatus.DEGRADED, "; ".join(degrading))
            else:
                self._health.set("tts", HealthStatus.READY, f"engine {chosen.name}")
        return SpeechClip(
            path=rendered.path,
            engine=chosen.name,
            detail=detail,
            synthesis_ms=rendered.synthesis_ms,
            cost=rendered.cost,
        )

    def _require_budget(self, deadline: PressDeadline | None, step: str) -> None:
        """Refuse to start *step* when the press has no time left for it.

        Without this an exhausted budget would surface as an instant engine
        timeout, which reads as a broken engine instead of an expired deadline.

        :param deadline: whole-press budget, or ``None`` when the caller has none.
        :param step: what was about to start, named in the refusal.
        :raises SpeechUnavailable: if the press budget is already spent.
        """
        if deadline is not None and deadline.remaining() <= 0.0:
            raise SpeechUnavailable(
                f"the press deadline ran out before {step} — check that the doorbell and its "
                "speech engines are healthy"
            )

    async def _render_with_retry(
        self,
        button: Button,
        *,
        engine: str,
        chosen: TtsEngine,
        options: TtsOptions,
        text: str,
        static: bool,
        deadline: PressDeadline | None,
    ) -> tuple[TtsEngine, TtsOptions, _RenderedClip, str | None]:
        """Render once, and retry on the next engine only for a bounded transient failure.

        There is no loop: exactly one retry, structurally. A non-transient failure
        (auth, bad request, not found, a disk fault) is never retried, because only
        a changed setting, token or name could fix it; a press with no deadline is
        never retried either, because a retry that cannot be bounded must not be
        started; and a transient failure is retried only when the remaining budget
        can pay for the probe of the next candidate plus a real render.

        :param button: button being pressed.
        :param engine: engine the press asked for, which the retry chain starts at.
        :param chosen: engine that was picked first.
        :param options: the first engine's options for this render.
        :param text: phrase to speak, already rendered when it was templated.
        :param static: whether the phrase plays the durable bake branch.
        :param deadline: whole-press budget, or ``None`` when the caller has none.
        :returns: the engine that spoke, its options, its clip and a retry note
            (``None`` when no retry happened).
        :raises SpeechUnavailable: if the render failed and no bounded retry was
            allowed or possible.
        """
        try:
            rendered = await self._render_once(
                button, chosen, options, text, static=static, deadline=deadline
            )
        except _RenderFailure as failure:
            if not failure.transient:
                raise
            if deadline is None:
                raise
            if not deadline.allows(RETRY_MIN_BUDGET_S):
                raise SpeechUnavailable(
                    f"{failure} — no second engine was tried: {deadline.remaining():.1f} s of "
                    f"the press budget remained and a bounded retry needs "
                    f"{RETRY_MIN_BUDGET_S:g} s"
                ) from None
            try:
                retry, _ = await self._choose(engine, deadline=deadline, skip={chosen.name})
            except SpeechUnavailable:
                # The render failure is the honest reason: the search failing says
                # nothing the user can act on that the render's sentence did not.
                raise failure from None
            retry_options = await self._options_for(retry.name)
            rendered = await self._render_once(
                button, retry, retry_options, text, static=static, deadline=deadline
            )
            note = f"retried on {retry.name} after {chosen.name} failed transiently: {failure}"
            return retry, retry_options, rendered, note
        return chosen, options, rendered, None

    async def _render_once(
        self,
        button: Button,
        chosen: TtsEngine,
        options: TtsOptions,
        text: str,
        *,
        static: bool,
        deadline: PressDeadline | None,
    ) -> _RenderedClip:
        """Render *text* with *chosen*, inside what is left of the press budget.

        A timeout here is deliberately **not** transient: there is no budget left
        to retry, so it is reported as the expired deadline it is.

        :param button: button being pressed, for the bake branch.
        :param chosen: engine that will speak.
        :param options: the engine's options for this render.
        :param text: phrase to speak, already rendered when it was templated.
        :param static: whether the phrase plays the durable bake branch.
        :param deadline: whole-press budget, or ``None`` when the caller has none.
        :raises SpeechUnavailable: if the render failed or outlived the press budget.
        """
        self._require_budget(deadline, f"{chosen.name} could render the speech")
        try:
            async with _bounded(deadline):
                if static:
                    return await self._baked_clip(button, chosen, options, text)
                return await self._cached_clip(chosen, options, text)
        except TimeoutError:
            raise SpeechUnavailable(
                f"{chosen.name} was still rendering when the press deadline ran out — check the "
                "doorbell log"
            ) from None

    async def bake_button(self, *, button: Button) -> BakeArtefact:
        """Pre-render *button*'s phrase into a durable artefact.

        Every refusal happens before an engine is probed, so a phrase that has
        nothing to bake costs nothing. The fallback chain is **not** walked when
        speech is deliberately off (D27): ``none`` is a configuration rather than
        an unavailable engine, so there is nothing to fall back to and the answer
        is the same sentence a press earns.

        :param button: button whose phrase should be pre-rendered.
        :raises BakeNotApplicable: if the button has no phrase that could ever be
            pre-rendered (a sound button, blank text or a Home Assistant
            template).
        :raises SpeechUnavailable: if speech is off or no engine could produce
            the clip.
        """
        if self._disabled:
            raise SpeechUnavailable(
                "speech is disabled (tts.default_engine is 'none'), "
                "so there is nothing to pre-render"
            )
        if button.action_type != "tts":
            raise BakeNotApplicable(
                f"button {button.slug!r} is a sound button, so it has no phrase to pre-render"
            )
        text = (button.tts_text or "").strip()
        if not text:
            raise BakeNotApplicable(f"button {button.slug!r} has no text to pre-render")
        if has_template_syntax(text):
            raise BakeNotApplicable(
                f"button {button.slug!r} speaks a Home Assistant template, which is rendered "
                "on every press and cannot be pre-rendered"
            )
        configured = (button.tts_engine or "").strip()
        engine = await self._effective_engine(button)
        if engine == "none":
            source = "this button's tts_engine" if configured == "none" else "tts.default_engine"
            raise SpeechUnavailable(
                f"speech is disabled ({source} is 'none'), so there is nothing to pre-render"
            )
        bakes = self._bakes
        if bakes is None:
            raise SpeechUnavailable("no pre-rendered clip directory is configured")
        try:
            chosen, _reason = await self._choose(engine)
        except SpeechUnavailable as exc:
            await self._record_failure(button.id, str(exc))
            raise
        options = await self._options_for(chosen.name)
        sha = expected_bake_sha(chosen.name, options, text)
        try:
            artefact = await bakes.write(
                button_id=button.id,
                sha=sha,
                render=_TimedRenderer(chosen, text, options),
                engine=chosen.name,
                options=options,
                text=text,
            )
        except (TtsRenderError, OSError) as exc:
            await self._record_failure(button.id, str(exc))
            raise SpeechUnavailable(
                f"{chosen.name} could not pre-render this phrase: {exc}"
            ) from None
        await self._record_success(button.id, artefact)
        return artefact

    async def bake_all(self, buttons: Sequence[Button]) -> BakeBatch:
        """Pre-render every static phrase among *buttons*, one report per button.

        Buttons that are not TTS actions are skipped entirely: they have no
        phrase and no bake state to report on. A dynamic phrase is reported as
        skipped rather than failed, because it is unbakeable by design. A phrase
        whose artefact is already current is reported as skipped too, and its
        button row is refreshed on the way past.

        A :class:`~chime_deck.press.SpeechUnavailable` that belongs to the
        whole run (speech switched off) propagates; one that belongs to a single
        button is recorded in :attr:`BakeBatch.failed` instead, so one broken
        engine cannot stop the rest.

        :param buttons: buttons to consider, in any order.
        :raises SpeechUnavailable: if speech is deliberately switched off (D27).
        """
        if self._disabled:
            raise SpeechUnavailable(
                "speech is disabled (tts.default_engine is 'none'), "
                "so there is nothing to pre-render"
            )
        baked: list[int] = []
        skipped_dynamic: list[int] = []
        skipped_current: list[int] = []
        failed: list[BakeFailure] = []
        for button in buttons:
            if button.action_type != "tts":
                continue
            text = (button.tts_text or "").strip()
            if has_template_syntax(text):
                skipped_dynamic.append(button.id)
                continue
            if await self._current_artefact(button, text) is not None:
                skipped_current.append(button.id)
                continue
            try:
                await self.bake_button(button=button)
            except (BakeNotApplicable, SpeechUnavailable) as exc:
                failed.append(BakeFailure(button_id=button.id, error=str(exc)))
            else:
                baked.append(button.id)
        return BakeBatch(
            baked=tuple(baked),
            skipped_dynamic=tuple(skipped_dynamic),
            skipped_current=tuple(skipped_current),
            failed=tuple(failed),
        )

    async def discard_bake(self, button_id: int) -> int:
        """Delete one button's bake directory.

        :param button_id: database id of the button.
        :returns: number of files removed; ``0`` when baking is not wired.
        """
        if self._bakes is None:
            return 0
        return await self._bakes.discard(button_id)

    async def sweep_bakes(self, live_ids: Iterable[int]) -> int:
        """Delete the bake directories of buttons that no longer exist.

        :param live_ids: ids of the buttons that still exist.
        :returns: number of directories removed; ``0`` when baking is not wired.
        """
        if self._bakes is None:
            return 0
        return await self._bakes.sweep(live_ids)

    async def bake_usage(self) -> BakeUsage:
        """Return the disk footprint of the bakes and of the ephemeral cache.

        This is a read-only report for the settings page; it writes nothing, so
        it can be polled. When baking is not wired the bake figures are zero and
        the cache figures are still real, because the cache exists either way.
        """
        cache_root = self._cache.root if self._cache is not None else None
        if self._bakes is not None:
            return self._bakes.usage(cache_root=cache_root)
        cache_files, cache_bytes = (0, 0) if cache_root is None else directory_usage(cache_root)
        return BakeUsage(
            baked_files=0,
            baked_bytes=0,
            cache_files=cache_files,
            cache_bytes=cache_bytes,
        )

    async def _effective_engine(self, button: Button) -> str:
        """Return the engine *button* would speak through, as ``press.py`` decides it.

        A non-blank per-button ``tts_engine`` wins, otherwise
        ``tts.default_engine`` applies. ``"none"`` is returned verbatim rather
        than translated: it is a deliberate switch-off (D27), not a missing
        engine, and the caller has to tell the two apart.

        :param button: button whose phrase is being baked.
        """
        configured = (button.tts_engine or "").strip()
        if configured:
            return configured
        default = await _str_setting(self._settings, "tts.default_engine")
        return default.strip() or "ha_tts"

    async def _cached_clip(
        self, chosen: TtsEngine, options: TtsOptions, text: str
    ) -> _RenderedClip:
        """Return the ephemeral-cache clip for *text*, rendering it on a miss.

        A hit returns no measurement and no cost: nothing was synthesised for
        this press, so ``synthesis_ms`` stays ``None`` rather than repeating an
        earlier press's number, and ``cost`` stays ``None`` because a cache hit
        bills nothing.

        :param chosen: engine that will speak.
        :param options: the engine's options for this render.
        :param text: phrase to speak, already rendered when it was templated.
        :raises SpeechUnavailable: if the engine could not render the phrase.
        """
        cache = self._cache
        if cache is None:
            raise SpeechUnavailable("no speech cache is configured")
        key = cache_key(chosen.name, options, text)
        suffix = suffix_for_format(options.preferred_format)
        hit = cache.lookup(key, suffix)
        if hit is not None:
            return _RenderedClip(hit)

        renderer = _TimedRenderer(chosen, text, options)
        try:
            path = await cache.get_or_render(key, suffix, renderer)
        except (TtsRenderError, OSError) as exc:
            raise _RenderFailure(
                chosen.name,
                f"{chosen.name} could not render speech: {exc}",
                transient=isinstance(exc, TtsRenderError) and exc.transient,
            ) from None
        return _RenderedClip(path, renderer.measured_ms, await self._cost_for(chosen, text))

    async def _baked_clip(
        self, button: Button, chosen: TtsEngine, options: TtsOptions, text: str
    ) -> _RenderedClip:
        """Return the durable clip for a static *text*, baking it on a miss.

        A bake is a cost optimisation, never a correctness requirement (design
        04 §2.3): a failure is recorded on the button, logged, and then answered
        from the ephemeral cache so the doorbell still speaks. An existing
        artefact is a hit and carries no measurement and no cost; a fresh bake
        carries the time its own synthesis took and what it paid.

        :param button: button being pressed.
        :param chosen: engine that will speak.
        :param options: the engine's options for this render.
        :param text: static phrase to speak.
        :raises SpeechUnavailable: if neither the bake nor the cache could
            produce the clip.
        """
        bakes = self._bakes
        if bakes is None:
            raise SpeechUnavailable("no pre-rendered clip directory is configured")
        sha = expected_bake_sha(chosen.name, options, text)
        existing = bakes.path_for(button.id, sha)
        if existing is not None:
            await self._mirror_current(button, sha, existing.suffix.lstrip("."))
            return _RenderedClip(existing)
        renderer = _TimedRenderer(chosen, text, options)
        try:
            artefact = await bakes.write(
                button_id=button.id,
                sha=sha,
                render=renderer,
                engine=chosen.name,
                options=options,
                text=text,
            )
        except (TtsRenderError, OSError) as exc:
            await self._record_failure(button.id, str(exc))
            self._logger.warning(
                "could not pre-render %r for button %s, so this press pays for the cache: %s",
                text,
                button.id,
                exc,
            )
            return await self._cached_clip(chosen, options, text)
        await self._record_success(button.id, artefact)
        return _RenderedClip(
            artefact.path, renderer.measured_ms, await self._cost_for(chosen, text)
        )

    async def _cost_for(self, engine: TtsEngine, text: str) -> TtsCost | None:
        """Return what *text* cost through *engine*, or ``None`` when it cannot say.

        Duck-typed through :class:`~chime_deck.tts.providers.SynthesisCosting`,
        the same way :class:`~chime_deck.tts.engines.SynthesisTiming` is read,
        so an engine that has no price to report costs nothing to ask. A cost
        lookup must never fail a press, so every failure is logged and swallowed:
        losing a note in the history is better than losing the chime.

        :param engine: engine that just synthesised the phrase.
        :param text: the phrase that was sent.
        """
        if not isinstance(engine, SynthesisCosting):
            return None
        try:
            return await engine.estimate_cost(text)
        except Exception as exc:
            self._logger.warning(
                "could not read the %s synthesis cost: %s: %s",
                engine.name,
                type(exc).__name__,
                exc,
            )
            return None

    async def _current_artefact(self, button: Button, text: str) -> Path | None:
        """Return the artefact *button* already has, or ``None`` when it is stale.

        The engine is chosen exactly as the press path chooses it, so the digest
        is hashed from the engine that would actually speak: a phrase that falls
        back is reported as current once its fallback's artefact exists, instead
        of being re-baked on every boot. The probe is paid once more by
        :meth:`bake_button` on a miss, which is cheaper than a wrong digest.

        :param button: button to inspect.
        :param text: its already-stripped static phrase.
        """
        bakes = self._bakes
        if bakes is None:
            return None
        engine = await self._effective_engine(button)
        if engine == "none":
            return None
        try:
            chosen, _reason = await self._choose(engine)
        except SpeechUnavailable:
            return None
        options = await self._options_for(chosen.name)
        sha = expected_bake_sha(chosen.name, options, text)
        path = bakes.path_for(button.id, sha)
        if path is None:
            return None
        await self._mirror_current(button, sha, path.suffix.lstrip("."))
        return path

    async def _mirror_current(self, button: Button, sha: str, fmt: str) -> None:
        """Record on *button* that its current artefact is *sha* in *fmt*.

        The press has already resolved a playable file, so this is best-effort:
        a store that cannot be written must not turn a hit into a failure. The
        digest *and* the real container decide whether the record is due, so a
        row that says ``mp3`` while the artefact on disk is ``wav`` is corrected
        on the next hit instead of being left to mislead the panel.

        :param button: button the artefact belongs to.
        :param sha: digest of the artefact on disk.
        :param fmt: container token from the artefact's own name.
        """
        if button.baked_sha == sha and button.baked_format == fmt:
            return
        recorder = self._recorder
        if recorder is None:
            return
        try:
            await recorder.set_bake_state(button.id, sha, fmt=fmt)
        except Exception as exc:
            self._logger.warning(
                "could not record the bake for button %s: %s: %s",
                button.id,
                type(exc).__name__,
                exc,
            )

    async def _record_success(self, button_id: int, artefact: BakeArtefact) -> None:
        """Record a successful bake, never letting the store fail the bake.

        A database hiccup must never break a press that already has its audio, so
        a failure is logged at warning level and otherwise ignored.

        :param button_id: database id of the button.
        :param artefact: what was written to disk.
        """
        recorder = self._recorder
        if recorder is None:
            return
        try:
            await recorder.set_bake_state(button_id, artefact.sha, fmt=artefact.format)
        except Exception as exc:
            self._logger.warning(
                "could not record the bake for button %s: %s: %s",
                button_id,
                type(exc).__name__,
                exc,
            )

    async def _record_failure(self, button_id: int, error: str) -> None:
        """Record a failed bake so the UI can show "Pre-render failed: …".

        Best-effort for the same reason as :meth:`_record_success`: the failure
        is already on its way back to the caller, and the store's own trouble is
        not worth turning into a second one.

        :param button_id: database id of the button.
        :param error: whole sentence describing the failure.
        """
        recorder = self._recorder
        if recorder is None:
            return
        try:
            await recorder.record_bake_failure(button_id, error)
        except Exception as exc:
            self._logger.warning(
                "could not record the bake failure for button %s: %s: %s",
                button_id,
                type(exc).__name__,
                exc,
            )

    async def _choose(
        self,
        engine: str,
        *,
        deadline: PressDeadline | None = None,
        skip: Collection[str] = (),
    ) -> tuple[TtsEngine, str]:
        """Return the first available engine in the chain and the first reason.

        The chain is walked in order: a name in *skip* is passed over without a
        probe (the retry walk must not pick the engine that just failed), and a
        press with no budget left stops the walk instead of probing an engine it
        could no longer wait for.

        :param engine: requested engine name.
        :param deadline: whole-press budget, or ``None`` when the caller has none.
        :param skip: engine names already known to be unusable for this press.
        :raises SpeechUnavailable: if no candidate is available.
        """
        chain = self._chain_for(engine)
        reasons: list[str] = []
        first_reason = ""
        # Availability fallback is the availability walk. A *transient render*
        # failure is now also retried once, but only when the remaining press
        # budget can pay for it: HSD-96k6 supersedes HSD-p50k's no-render-fallback
        # ruling, whose stated reason was the absence of exactly this deadline.
        # piper still latches itself unavailable, so the walk never pays for a
        # wedged worker twice.
        for name in chain:
            if name in skip:
                continue
            if deadline is not None and deadline.remaining() <= 0.0:
                reason = "the press deadline ran out before this candidate could be probed"
                if not reasons:
                    first_reason = reason
                reasons.append(f"{name}: {reason}")
                break
            candidate = self._engines.get(name)
            if candidate is None:
                reason = f"{name} is not available in this build"
            else:
                try:
                    async with _bounded(deadline, PROBE_TIMEOUT_S):
                        availability = await candidate.available()
                except TimeoutError:
                    availability = Availability(
                        False,
                        f"{name} did not answer its availability probe in time — check the "
                        "doorbell log",
                    )
                if availability.ok:
                    return candidate, first_reason
                reason = availability.reason or f"{name} is unavailable"
            if not reasons:
                first_reason = reason
            reasons.append(f"{name}: {reason}")
        message = "no TTS engine available: " + "; ".join(reasons)
        if self._health is not None:
            self._health.set("tts", HealthStatus.ERROR, message)
        raise SpeechUnavailable(message)

    def _chain_for(self, engine: str) -> tuple[str, ...]:
        """Return the candidates for *engine*: itself first, then the fallbacks.

        A requested engine that is part of the chain starts the chain at its own
        position, so a ``piper`` request does not retry ``ha_tts``. Any other
        name — ``"None"``, ``"NONE"``, ``"nope"`` — is tried first and then the
        full chain follows, so an unknown request falls back and records a detail
        instead of staying silent. ``http_tts`` is a known engine, deliberately
        absent from :data:`FALLBACK_CHAIN`: an ``http_tts`` request starts the
        chain here, while no other engine may fall back *into* it, because that
        would bill on every press.

        :param engine: requested engine name.
        """
        if engine in self._fallback_chain:
            return self._fallback_chain[self._fallback_chain.index(engine) :]
        return (engine, *self._fallback_chain)

    async def _options_for(self, name: str) -> TtsOptions:
        """Read the settings that apply to the chosen engine.

        :param name: chosen engine name.
        """
        return await options_for(name, self._settings)


async def _str_setting(settings: SettingsReader, key: str) -> str:
    """Return a string setting, falling back to its default on a bad value.

    :param settings: settings reader to read from.
    :param key: setting key to read.
    """
    value = await settings.get_setting(key)
    if isinstance(value, str):
        return value
    _LOGGER.warning("setting %r is not a string; using the registry default", key)
    default = SETTINGS[key].default
    if not isinstance(default, str):
        raise TypeError(f"setting {key!r} does not have a string default")
    return default


async def _int_setting(settings: SettingsReader, key: str) -> int:
    """Return an integer setting, falling back to its default on a bad value.

    :param settings: settings reader to read from.
    :param key: setting key to read.
    """
    value = await settings.get_setting(key)
    if isinstance(value, bool) or not isinstance(value, int):
        _LOGGER.warning("setting %r is not an integer; using the registry default", key)
        default = SETTINGS[key].default
        if isinstance(default, bool) or not isinstance(default, int):
            raise TypeError(f"setting {key!r} does not have an integer default")
        return default
    return value


async def options_for(name: str, settings: SettingsReader) -> TtsOptions:
    """Read the settings that apply to one engine (see ``TtsOptions``'s table).

    ``remote_tts`` is an explicit branch rather than a fall-through: the caller
    derives the cache **suffix** from ``options.preferred_format`` through
    :func:`~chime_deck.tts.cache.suffix_for_format`, so a default
    ``TtsOptions()`` that happened to differ would put the clip under the wrong
    extension. The renderer applies its own voice, so no setting is mapped.

    :param name: chosen engine name.
    :param settings: settings reader to read the engine's keys from.
    :returns: the options for that engine; the default object for an engine
        that maps none.
    """
    if name == "espeak_ng":
        return TtsOptions(
            voice=await _str_setting(settings, "tts.espeak_voice"),
            speed=await _int_setting(settings, "tts.espeak_speed"),
            preferred_format="wav",
        )
    if name == "ha_tts":
        return TtsOptions(
            engine_id=await _str_setting(settings, "tts.ha.engine_id"),
            language=await _str_setting(settings, "tts.ha.language"),
            preferred_format="wav",
        )
    if name == "piper":
        return TtsOptions(
            voice=await _str_setting(settings, "tts.piper.voice"),
            preferred_format="wav",
        )
    if name == "remote_tts":
        return TtsOptions(preferred_format="wav")
    if name == "http_tts":
        return await _http_tts_options(settings)
    return TtsOptions()


async def _http_tts_options(settings: SettingsReader) -> TtsOptions:
    """Map the selected provider onto the options the render and cache key use.

    ``engine_id`` is the selected provider's id plus a fingerprint of the facts
    that decide the request: the preset, the **effective** base URL (the
    record's own, else the preset's default), the voice and the audio format.
    The cache key is ``(engine, options, text)``, so a fingerprint is what stops
    a provider edit — a new address, voice or format — from serving audio that
    was rendered under the old request forever.

    A provider that is not selected, has vanished from ``tts.providers`` or
    names a preset this build does not ship answers the plain default options,
    so no caller has to special-case a broken configuration. Nothing here
    touches the request itself: :class:`~chime_deck.tts.http_tts.HttpTtsEngine`
    reads the record again at render time.

    :param settings: settings reader holding ``tts.providers`` and the selection.
    """
    selected = (await _str_setting(settings, "tts.http_tts.provider")).strip()
    if not selected:
        return TtsOptions(preferred_format="wav")
    records = read_providers(await settings.get_setting("tts.providers"), presets=DEFAULT_PRESETS)
    record = next((item for item in records if item.id == selected), None)
    if record is None:
        return TtsOptions(preferred_format="wav")
    preset = DEFAULT_PRESETS.get(record.preset)
    if preset is None:
        return TtsOptions(preferred_format="wav")
    shape = _shape_of(record, preset)
    base_url = (record.base_url or preset.base_url or "").rstrip("/")
    fingerprint = _provider_fingerprint(record.preset, base_url, record.voice, shape.format)
    return TtsOptions(
        engine_id=f"{record.id}@{fingerprint}",
        voice=record.voice,
        language=record.language,
        preferred_format=shape.format,
    )


def _shape_of(record: ProviderRecord, preset: Preset) -> ProviderShape:
    """Return the request shape a record actually uses.

    Only the ``custom`` preset accepts overrides; a shape that cannot be built
    falls back to the preset's own, because a hand-broken override must never
    break a press (``read_providers`` already logged it once).

    :param record: the selected provider record.
    :param preset: the preset the record names.
    """
    if record.preset != "custom" or not record.overrides:
        return preset.shape
    overrides = {key: value for key, value in record.overrides.items() if key in SHAPE_KEYS}
    try:
        return shape_from_overrides(preset.shape, overrides, where=f"provider {record.name!r}")
    except PresetError:
        return preset.shape


def _provider_fingerprint(preset: str, base_url: str, voice: str, audio_format: str) -> str:
    """Hash the facts a provider edit changes into eight hex characters.

    :param preset: preset id of the selected provider.
    :param base_url: effective base URL, with no trailing slash.
    :param voice: the provider's configured voice.
    :param audio_format: the audio container the request produces.
    """
    digest = hashlib.sha256("\0".join((preset, base_url, voice, audio_format)).encode())
    return digest.hexdigest()[:8]


async def build_speech_source(
    store: SettingsReader,
    *,
    data_dir: Path,
    health: HealthRegistry | None = None,
    engines: Sequence[TtsEngine] | None = None,
    ha_client: httpx.AsyncClient | None = None,
    voices_dir: Path | None = None,
    piper_probe: PiperProbe | None = None,
    cache_max_files: int = CACHE_MAX_FILES,
    cache_max_bytes: int = CACHE_MAX_BYTES,
    templating: PhraseTemplating | None = None,
    bakes: BakeStore | None = None,
    recorder: BakeRecorder | None = None,
) -> TtsSpeechSource:
    """Build the resolver from the stored settings and the data directory.

    When ``tts.default_engine`` is ``"none"`` the disabled state constructs no
    engine, cache, probe, warmup or template renderer, and records ``tts`` as
    disabled without a detail (the registry's detail is global and sticky, so a
    detail would mislabel ``ha``, ``mqtt`` or ``audio``). The ``tts`` health
    component appears only after :meth:`HealthRegistry.set` is called, so the
    pinned websocket snapshot of a default install is unchanged. A disabled
    source bakes nothing at all: an install with no speech has no artefacts to
    keep, so no bake store is built on that path.

    Otherwise every supplied engine is warmed up (a warmup failure is logged and
    tolerated), the cache is pruned once and the engine list is passed to the
    resolver in order. The default list is ``remote_tts``, then ``ha_tts``, then
    the local ``piper``, ``espeak_ng`` and finally ``http_tts`` (the D25 provider
    engine): a press speaks through the renderer this install points at, falls
    back to Home Assistant when its TTS engine is configured, then to local Piper
    when the optional extra, the RAM of D22 and a downloaded voice are all there,
    then to the local binary; an ``http_tts`` press is tried first by the chain
    and falls back to all four of those. A requested engine with no
    implementation also falls back and records the fallback as the clip's detail.
    This function never returns ``None``.

    The default template renderer is built whether or not *engines* was
    injected: a templated phrase still needs Home Assistant even when the engine
    list is a fake. The bake store is rooted at
    ``<data_dir>/tts/baked`` and records its state through *store*, which is what
    turns a static phrase into one synthesis for the life of the button (D21).

    A runtime change of ``tts.default_engine`` is not re-probed until a restart,
    but each engine reads its own settings per call, so a changed
    ``tts.ha.engine_id`` applies on the next press. The health snapshot does not
    change until a press records a status.

    :param store: settings reader.
    :param data_dir: directory that receives ``tts-cache``.
    :param health: registry that records the ``tts`` component.
    :param engines: implemented engines; ``None`` builds ``remote_tts``,
        ``ha_tts``, ``piper``, ``espeak_ng`` and ``http_tts``.
    :param ha_client: HTTP client borrowed by the default ``ha_tts`` engine
        *and* by the default template renderer, with or without an injected
        engine list. It is never closed here, and it is deliberately **not**
        handed to ``remote_tts``: that engine owns a client per render, which is
        what keeps the ``ha_tts`` request sequence clean.
    :param voices_dir: directory the local Piper voice models live in; ``None``
        uses ``data_dir / "voices"``.
    :param piper_probe: the process-wide environment probe the default local
        ``piper`` engine uses; ``None`` builds one over *store*. Injecting the
        same probe the settings routes use keeps ``PiperEngine.available()``
        and ``GET /api/tts/voices`` reporting the same facts.
    :param cache_max_files: most clips kept on disk.
    :param cache_max_bytes: most bytes the cache may occupy.
    :param templating: press-time template renderer; ``None`` builds the default
        :class:`~chime_deck.tts.templating.PhraseTemplating` over
        :class:`~chime_deck.tts.templating.HaTemplateRenderer` around
        *store* and *ha_client*.
    :param bakes: durable pre-rendered clips; ``None`` builds the default
        :class:`~chime_deck.tts.bake.BakeStore` rooted at
        :func:`~chime_deck.tts.bake.baked_root` of *data_dir*. It creates no
        directory until the first static phrase is baked.
    :param recorder: sink for per-button bake state; ``None`` uses *store* when
        it satisfies :class:`BakeRecorder`, and reports nothing otherwise.
    """
    configured = await store.get_setting("tts.default_engine")
    name = configured if isinstance(configured, str) and configured.strip() else "ha_tts"
    if name.strip() == "none":
        _LOGGER.info(
            "TTS disabled: tts.default_engine is 'none' (D27); speech buttons report tts_disabled"
        )
        if health is not None:
            health.set("tts", HealthStatus.DISABLED)
        return TtsSpeechSource.disabled(store, health=health)
    engine_list: list[TtsEngine]
    if engines is not None:
        engine_list = list(engines)
    else:
        resolved_voices_dir = voices_dir if voices_dir is not None else data_dir / "voices"
        probe = piper_probe if piper_probe is not None else PiperProbe(store)
        engine_list = [
            RemoteTtsEngine(store),
            HaTtsEngine(store, client=ha_client),
            PiperEngine(probe, voices_dir=resolved_voices_dir),
            EspeakNgEngine(),
            # The D25 provider engine: one preset plus a stored key. It is not in
            # FALLBACK_CHAIN, so nothing else can fall back into a billed API.
            # The cast is a type-only widening: this port is the narrow
            # SettingsReader, and every production caller passes a ConfigStore,
            # which satisfies ProviderSource (get_setting + get_provider_secret)
            # structurally. A settings-only stub never reaches this list, because
            # it always injects its own engines.
            HttpTtsEngine(cast(ProviderSource, store)),
        ]
    for engine in engine_list:
        try:
            await engine.warmup()
        except Exception as exc:
            _LOGGER.warning("could not warm up the %s speech engine: %s", engine.name, exc)
    cache = TtsCache(data_dir / "tts-cache", max_files=cache_max_files, max_bytes=cache_max_bytes)
    removed = cache.prune()
    _LOGGER.info(
        "TTS enabled (preferred engine %s, cache %s, %d stale entries pruned)",
        name,
        cache.root,
        removed,
    )
    renderer = templating
    if renderer is None:
        renderer = PhraseTemplating(HaTemplateRenderer(store, client=ha_client))
    store_bakes = bakes if bakes is not None else BakeStore(baked_root(data_dir))
    resolved_recorder = (
        recorder if recorder is not None else (store if isinstance(store, BakeRecorder) else None)
    )
    return TtsSpeechSource(
        store,
        engine_list,
        cache,
        health=health,
        templating=renderer,
        bakes=store_bakes,
        recorder=resolved_recorder,
    )
