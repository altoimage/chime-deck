"""The speech resolver: fallback chain, settings mapping, caching and health.

Fully offline: engines are fakes, the store is a real temporary SQLite database,
and the cache renders to ``tmp_path``. No engine probes the host and no audio is
played.

Owning ticket: HSD-bw8h.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio

from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.press import PressDeadline, SpeechSource, SpeechUnavailable
from chime_deck.store import SETTINGS, Button, ButtonIn, ConfigStore
from chime_deck.tts import (
    FALLBACK_CHAIN,
    Availability,
    BakeStore,
    RemoteTtsEngine,
    TtsCache,
    TtsOptions,
    TtsRenderError,
    TtsSpeechSource,
    build_speech_source,
    cache_key,
    options_for,
)

pytestmark = pytest.mark.asyncio

_PAYLOAD = b"RIFF....WAVEfmt "


class _UnreadableSettings:
    """A settings reader that fails the test if any setting is read at all."""

    async def get_setting(self, key: str) -> object:
        """Fail: this engine maps no setting, so a read is a bug.

        :param key: setting key that must not be read.
        """
        raise AssertionError(f"options_for must not read {key!r} for this engine")


class FakeEngine:
    """A TtsEngine whose availability, audio and failure are all configurable."""

    def __init__(
        self,
        name: str,
        availability: Availability | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Create the fake engine.

        :param name: engine name registered with the resolver.
        :param availability: result of :meth:`available`; available by default.
        :param error: exception raised by :meth:`render`, or ``None`` to succeed.
        """
        self.name = name
        self._availability = availability if availability is not None else Availability(True)
        self._error = error
        self.renders: list[tuple[str, TtsOptions, Path]] = []
        self.available_calls = 0
        self.warmups = 0

    async def available(self) -> Availability:
        """Record the probe and report the configured availability."""
        self.available_calls += 1
        return self._availability

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Record the render, then write a payload or raise the configured error.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders.append((text, options, destination))
        if self._error is not None:
            raise self._error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_PAYLOAD)
        return destination

    async def warmup(self) -> None:
        """Count the warmup."""
        self.warmups += 1


class AssertingEngine:
    """An engine that fails the test if the disabled state touches it at all."""

    name = "explode"
    available_calls = 0
    render_calls = 0
    warmup_calls = 0

    async def available(self) -> Availability:
        """Fail: a disabled source must not probe."""
        self.available_calls += 1
        raise AssertionError("a disabled speech source must not probe an engine")

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Fail: a disabled source must not render.

        :param text: phrase that must not be spoken.
        :param options: options that must not be used.
        :param destination: path that must not be written.
        """
        self.render_calls += 1
        raise AssertionError("a disabled speech source must not render")

    async def warmup(self) -> None:
        """Fail: a disabled source must not warm up."""
        self.warmup_calls += 1
        raise AssertionError("a disabled speech source must not warm up")


class _DictSettings:
    """A settings reader backed by a dictionary and the registry defaults."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        """Create the reader.

        :param values: overrides keyed by setting name.
        """
        self._values = values if values is not None else {}

    async def get_setting(self, key: str) -> object:
        """Return the override, or the registered default.

        :param key: registered setting key.
        """
        return self._values.get(key, SETTINGS[key].default)


_SETTINGS = _DictSettings()
"""Settings reader used only to pin the port at import time."""

_SPEECH_CONFORMANCE: SpeechSource = TtsSpeechSource.disabled(_SETTINGS)
"""Pin the speech port conformance under mypy, as tests/test_press.py does."""


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


def _cache(tmp_path: Path) -> TtsCache:
    """Build a cache rooted under the test's temporary directory.

    :param tmp_path: pytest temporary directory.
    """
    return TtsCache(tmp_path / "tts-cache")


async def _seed_tts(store: ConfigStore, *, text: str = "Someone is at the door") -> Button:
    """Create a TTS button and return it.

    :param store: connected configuration store.
    :param text: phrase the button speaks.
    """
    return await store.upsert_button(ButtonIn(name="Front Door", action_type="tts", tts_text=text))


async def test_options_for_remote_tts_reads_no_setting_and_asks_for_wav() -> None:
    """``remote_tts`` maps no setting: its options are the default plus ``wav``.

    The renderer applies its own voice, and the caller derives the cache suffix
    from ``preferred_format``, so reading a setting here would be a mapping
    nothing honours. The stub raises on ``get_setting``, so a read is a failure.
    """
    options = await options_for("remote_tts", _UnreadableSettings())

    assert options.preferred_format == "wav"


async def test_a_direct_engine_is_used_cached_and_ready(tmp_path: Path, store: ConfigStore) -> None:
    """The requested engine speaks once; the second resolve is a cache hit.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts")
    espeak = FakeEngine("espeak_ng")
    health = HealthRegistry()
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path), health=health)

    clip = await source.resolve(button=button, engine="ha_tts")

    assert clip.engine == "ha_tts"
    assert clip.detail is None
    assert clip.path.is_file()
    assert len(ha.renders) == 1
    text, options, _destination = ha.renders[0]
    assert text == "Someone is at the door"
    assert options == TtsOptions(engine_id="tts.piper", language="en_GB", preferred_format="wav")
    assert espeak.available_calls == 0
    assert health.get("tts") is HealthStatus.READY
    assert health.message()["detail"] == "engine ha_tts"

    again = await source.resolve(button=button, engine="ha_tts")

    assert again.path == clip.path
    assert len(ha.renders) == 1


async def test_b_the_chain_falls_back_and_reports_degraded(
    tmp_path: Path, store: ConfigStore
) -> None:
    """An unavailable first engine hands over to the next available one.

    The chain is the D24 order, ``remote_tts`` first (an erratum in
    ``FALLBACK_CHAIN``'s own docstring: design 04 §6.5 omits ``piper``).

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts", Availability(False, "HA is unreachable"))
    espeak = FakeEngine("espeak_ng")
    health = HealthRegistry()
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path), health=health)

    clip = await source.resolve(button=button, engine="ha_tts")

    assert clip.engine == "espeak_ng"
    assert clip.detail == "fell back from ha_tts: HA is unreachable"
    assert health.get("tts") is HealthStatus.DEGRADED
    assert health.message()["detail"] == clip.detail
    assert len(espeak.renders) == 1
    assert FALLBACK_CHAIN == ("remote_tts", "ha_tts", "piper", "espeak_ng")


@pytest.mark.parametrize("requested", ["http_tts", "None", "NONE"])
async def test_an_unknown_engine_falls_back_and_records_the_detail(
    tmp_path: Path, store: ConfigStore, requested: str
) -> None:
    """An unknown engine is tried first, then the full chain follows.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    :param requested: engine name that has no implementation.
    """
    button = await _seed_tts(store)
    source = TtsSpeechSource(store, [FakeEngine("ha_tts")], _cache(tmp_path))

    clip = await source.resolve(button=button, engine=requested)

    assert clip.engine == "ha_tts"
    assert clip.detail is not None
    assert clip.detail.startswith(f"fell back from {requested}:")


async def test_a_real_remote_tts_request_reports_its_own_reason(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A real ``remote_tts`` hands over with its own actionable sentence, not a placeholder.

    The engine is registered, so the first reason must be the client's
    "set tts.remote_tts.url" sentence rather than the resolver's "not available
    in this build" text. The settings stub leaves the URL and token empty so the
    probe answers without opening a socket.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    remote = RemoteTtsEngine(_DictSettings({"tts.remote_tts.url": "", "tts.remote_tts.token": ""}))
    source = TtsSpeechSource(store, [remote, FakeEngine("ha_tts")], _cache(tmp_path))

    clip = await source.resolve(button=button, engine="remote_tts")

    assert clip.engine == "ha_tts"
    assert clip.detail is not None
    assert clip.detail.startswith("fell back from remote_tts:")
    assert "set tts.remote_tts.url" in clip.detail
    assert "not available in this build" not in clip.detail


async def test_the_exact_lowercase_none_short_circuits_an_enabled_source(
    tmp_path: Path, store: ConfigStore
) -> None:
    """``"none"`` is the disabled state even when the source is enabled.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = AssertingEngine()
    source = TtsSpeechSource(store, [engine], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable, match="disabled"):
        await source.resolve(button=button, engine="none")

    assert engine.available_calls == 0
    assert engine.render_calls == 0


async def test_no_available_engine_reports_every_reason(tmp_path: Path, store: ConfigStore) -> None:
    """When nothing can speak, every candidate's reason is in the message.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts", Availability(False, "HA is unreachable"))
    espeak = FakeEngine(
        "espeak_ng",
        Availability(False, "espeak-ng is not installed; install it with `apt install espeak-ng`"),
    )
    health = HealthRegistry()
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path), health=health)

    with pytest.raises(SpeechUnavailable) as excinfo:
        await source.resolve(button=button, engine="ha_tts")

    message = str(excinfo.value)
    assert "no TTS engine available" in message
    assert "HA is unreachable" in message
    assert "not installed" in message
    assert "piper is not available in this build" in message
    assert health.get("tts") is HealthStatus.ERROR
    assert health.message()["detail"] == message


async def test_a_button_without_text_is_unavailable(tmp_path: Path, store: ConfigStore) -> None:
    """Blank text is rejected before any engine is consulted.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = dataclasses.replace(await _seed_tts(store), tts_text="   ")
    engine = FakeEngine("ha_tts")
    source = TtsSpeechSource(store, [engine], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable, match="has no text to speak"):
        await source.resolve(button=button, engine="ha_tts")

    assert engine.available_calls == 0


async def test_changing_an_espeak_option_produces_a_new_cache_entry(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Voice and speed are part of the cache key, so a change re-renders.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    espeak = FakeEngine("espeak_ng")
    source = TtsSpeechSource(store, [espeak], _cache(tmp_path))

    first = await source.resolve(button=button, engine="espeak_ng")
    assert len(espeak.renders) == 1

    await store.set_setting("tts.espeak_speed", 200)
    second = await source.resolve(button=button, engine="espeak_ng")

    assert len(espeak.renders) == 2
    assert second.path != first.path
    assert espeak.renders[1][1].speed == 200

    await store.set_setting("tts.espeak_voice", "en-us")
    third = await source.resolve(button=button, engine="espeak_ng")

    assert len(espeak.renders) == 3
    assert third.path not in (first.path, second.path)
    assert espeak.renders[2][1].voice == "en-us"


async def test_changing_the_ha_engine_id_produces_a_new_cache_entry(
    tmp_path: Path, store: ConfigStore
) -> None:
    """The HA sub-engine id is part of the cache key too.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts")
    source = TtsSpeechSource(store, [ha], _cache(tmp_path))

    first = await source.resolve(button=button, engine="ha_tts")
    await store.set_setting("tts.ha.engine_id", "tts.cloud")
    second = await source.resolve(button=button, engine="ha_tts")

    assert len(ha.renders) == 2
    assert second.path != first.path
    assert ha.renders[1][1].engine_id == "tts.cloud"


async def test_a_mid_chain_request_never_retries_an_earlier_engine(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A ``piper`` request starts at ``piper``; ``ha_tts`` is not retried.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts")
    piper = FakeEngine("piper", Availability(False, "piper-tts is not installed"))
    espeak = FakeEngine("espeak_ng", Availability(False, "no espeak"))
    source = TtsSpeechSource(store, [ha, piper, espeak], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable) as excinfo:
        await source.resolve(button=button, engine="piper")

    message = str(excinfo.value)
    assert "piper-tts is not installed" in message
    assert "no espeak" in message
    assert "ha_tts" not in message
    assert ha.available_calls == 0


async def test_a_render_failure_is_not_retried_on_the_next_engine(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Only availability falls back; a render failure is reported directly.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    espeak = FakeEngine("espeak_ng", error=TtsRenderError("bad voice"))
    probe = FakeEngine("fallback_probe")
    source = TtsSpeechSource(
        store,
        [espeak, probe],
        _cache(tmp_path),
        fallback_chain=("espeak_ng", "fallback_probe"),
    )

    with pytest.raises(SpeechUnavailable) as excinfo:
        await source.resolve(button=button, engine="espeak_ng")

    message = str(excinfo.value)
    assert "could not render speech" in message
    assert "bad voice" in message
    assert probe.available_calls == 0
    assert probe.renders == []


async def test_the_disabled_factory_probes_nothing_and_reports_disabled(
    tmp_path: Path, store: ConfigStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``none`` setting builds the disabled source with no engine touched.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    :param caplog: captured log records.
    """
    await store.set_setting("tts.default_engine", "none")
    registry = HealthRegistry()
    engine = AssertingEngine()
    caplog.set_level(logging.INFO, logger="chime_deck")

    source = await build_speech_source(
        store, data_dir=tmp_path / "data", health=registry, engines=[engine]
    )

    assert isinstance(source, TtsSpeechSource)
    assert registry.get("tts") is HealthStatus.DISABLED
    assert registry.snapshot()["tts"] == "disabled"
    assert "detail" not in registry.message()
    messages = [
        record.getMessage() for record in caplog.records if record.name == "chime_deck.tts.speech"
    ]
    assert any(message.startswith("TTS disabled") for message in messages)
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name.startswith("chime_deck.tts")
    ]
    assert warnings == []

    button = await _seed_tts(store)
    with pytest.raises(SpeechUnavailable, match="disabled"):
        await source.resolve(button=button, engine="ha_tts")

    assert engine.available_calls == 0
    assert engine.render_calls == 0
    assert engine.warmup_calls == 0


async def test_the_enabled_factory_warms_up_and_prunes_without_tts_health(
    tmp_path: Path, store: ConfigStore, caplog: pytest.LogCaptureFixture
) -> None:
    """An enabled factory warms up, prunes and leaves the snapshot untouched.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    :param caplog: captured log records.
    """
    registry = HealthRegistry()
    engine = FakeEngine("espeak_ng")
    root = tmp_path / "data" / "tts-cache"
    root.mkdir(parents=True)
    stale = root / ".x.tmp.wav"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 7200.0, time.time() - 7200.0))
    caplog.set_level(logging.INFO, logger="chime_deck")

    source = await build_speech_source(
        store, data_dir=tmp_path / "data", health=registry, engines=[engine]
    )

    assert engine.warmups == 1
    assert not stale.exists()
    assert registry.get("tts") is HealthStatus.NOT_CONFIGURED
    assert registry.snapshot() == {
        "ha": "not_configured",
        "mqtt": "not_configured",
        "audio": "disabled",
    }
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("TTS enabled") for message in messages)

    button = await _seed_tts(store, text="hello")
    clip = await source.resolve(button=button, engine="espeak_ng")
    assert clip.path.is_file()
    # D21: a static phrase is baked into the durable directory, not the ephemeral cache.
    assert clip.path.parent == tmp_path / "data" / "tts" / "baked" / str(button.id)
    assert len(engine.renders) == 1


async def test_a_disabled_source_without_health_still_refuses_to_speak(
    store: ConfigStore,
) -> None:
    """The disabled source works when no health registry is supplied.

    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    source = TtsSpeechSource.disabled(store)

    with pytest.raises(SpeechUnavailable, match="disabled"):
        await source.resolve(button=button, engine="ha_tts")


class ClosableFakeEngine(FakeEngine):
    """A fake engine that records :meth:`close` and can fail it."""

    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        """Create the engine.

        :param name: engine name registered with the resolver.
        :param error: exception raised by :meth:`close`, or ``None`` to succeed.
        """
        super().__init__(name)
        self.close_calls = 0
        self._close_error = error

    def close(self) -> None:
        """Count the close, then raise the configured error if there is one."""
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


async def test_aclose_closes_the_closable_engines_and_logs_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown closes workers, skips engines without ``close``, and never raises.

    :param caplog: captured log records.
    """
    closable = ClosableFakeEngine("piper")
    failing = ClosableFakeEngine("broken", error=RuntimeError("boom"))
    plain = FakeEngine("espeak_ng")
    source = TtsSpeechSource(_SETTINGS, [closable, failing, plain], None)

    with caplog.at_level(logging.WARNING, logger="chime_deck.tts.speech"):
        await source.aclose()

    assert closable.close_calls == 1
    assert failing.close_calls == 1
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "chime_deck.tts.speech"
    ]
    assert warnings == ["could not close the broken speech engine: RuntimeError: boom"]


class TimedFakeEngine(FakeEngine):
    """A fake engine that, unlike its base, measures its own syntheses.

    A plain :class:`FakeEngine` deliberately keeps no ``last_synthesis_ms``, so
    it does not satisfy :class:`~chime_deck.tts.engines.SynthesisTiming` and
    must report ``None`` even though it rendered. This subclass satisfies it.
    """

    def __init__(
        self, name: str, *, synthesis_ms: int = 240, error: Exception | None = None
    ) -> None:
        """Create the timed engine with a fixed, positive measurement.

        :param name: engine name registered with the resolver.
        :param synthesis_ms: measurement reported after a successful render.
        :param error: exception raised by :meth:`render`, or ``None`` to succeed.
        """
        super().__init__(name, error=error)
        self.last_synthesis_ms: int | None = None
        self._synthesis_ms = synthesis_ms

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Render like the base class, then record the measurement.

        A failed render raises inside the base call, so the measurement is left
        untouched — exactly like a real engine.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        path = await super().render(text, options, destination=destination)
        self.last_synthesis_ms = self._synthesis_ms
        return path


async def test_a_cache_miss_records_the_positive_synthesis_time(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A rendered press reports the measured time, plays, and stays healthy.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = TimedFakeEngine("ha_tts")
    health = HealthRegistry()
    source = TtsSpeechSource(store, [engine], _cache(tmp_path), health=health)

    clip = await source.resolve(button=button, engine="ha_tts")

    assert len(engine.renders) == 1
    assert clip.synthesis_ms is not None
    assert clip.synthesis_ms > 0
    assert clip.synthesis_ms == 240
    assert clip.path.is_file()
    assert clip.detail is None
    assert health.get("tts") is HealthStatus.READY


async def test_an_identical_second_press_is_a_hit_and_reports_no_synthesis_time(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A cache hit synthesises nothing, so it reports ``None`` and renders no more.

    Reporting the engine's stale ``last_synthesis_ms`` here would attribute an
    earlier press's measurement to this one.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = TimedFakeEngine("ha_tts")
    source = TtsSpeechSource(store, [engine], _cache(tmp_path))

    first = await source.resolve(button=button, engine="ha_tts")
    again = await source.resolve(button=button, engine="ha_tts")

    assert first.synthesis_ms == 240
    assert again.path == first.path
    assert again.synthesis_ms is None
    assert len(engine.renders) == 1


async def test_a_first_press_bakes_and_the_second_hits_the_bake_with_no_time(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A static phrase is measured when baked, then served from the bake unmeasured.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = TimedFakeEngine("espeak_ng")
    source = TtsSpeechSource(
        store,
        [engine],
        _cache(tmp_path),
        bakes=BakeStore(tmp_path / "baked"),
    )

    baked = await source.resolve(button=button, engine="espeak_ng")
    hit = await source.resolve(button=button, engine="espeak_ng")

    assert len(engine.renders) == 1
    assert baked.synthesis_ms == 240
    assert hit.path == baked.path
    assert hit.synthesis_ms is None


async def test_an_engine_without_a_measurement_reports_none_though_it_rendered(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A non-timing engine renders, but there is no number to report.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = FakeEngine("ha_tts")
    source = TtsSpeechSource(store, [engine], _cache(tmp_path))

    clip = await source.resolve(button=button, engine="ha_tts")

    assert len(engine.renders) == 1
    assert not hasattr(engine, "last_synthesis_ms")
    assert clip.synthesis_ms is None


async def test_a_measurement_never_changes_the_health_status(
    tmp_path: Path, store: ConfigStore
) -> None:
    """The trap: a successful timing measurement must not degrade ``tts``.

    ``synthesis_ms`` travels on its own field precisely because a truthy
    ``detail`` is what flips the component to DEGRADED.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = TimedFakeEngine("ha_tts")
    health = HealthRegistry()
    source = TtsSpeechSource(store, [engine], _cache(tmp_path), health=health)

    clip = await source.resolve(button=button, engine="ha_tts")

    assert clip.synthesis_ms is not None
    assert clip.detail is None
    assert health.get("tts") is HealthStatus.READY
    assert health.get("tts") is not HealthStatus.DEGRADED
    assert health.message()["detail"] == "engine ha_tts"


async def test_a_failed_render_records_no_measurement(tmp_path: Path, store: ConfigStore) -> None:
    """A render that raised measured nothing, so nothing is reported.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    engine = TimedFakeEngine("ha_tts", error=TtsRenderError("piper produced no audio"))
    source = TtsSpeechSource(store, [engine], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable, match="could not render speech"):
        await source.resolve(button=button, engine="ha_tts")

    assert engine.last_synthesis_ms is None
    assert len(engine.renders) == 1


_PRESS_BUDGET_S = 8.0
"""Comfortably enough press budget for a probe plus one retried render."""

_TIGHT_BUDGET_S = 0.05
"""A press budget that is spent before any second attempt could finish."""

_UNDER_RETRY_BUDGET_S = 1.0
"""A press budget that is too small for a retry, but only by the rule's own measure.

Decidedly **not** :data:`_TIGHT_BUDGET_S`: this test is about the comparison
against :data:`~chime_deck.tts.speech.RETRY_MIN_BUDGET_S` (2.5 s), not about
a budget running out. One second sits 1.5 s below that threshold and far above
the microseconds a fake engine's render takes, so neither side of the comparison
is a race — a scheduling hiccup on a loaded machine cannot turn the expected
"no second engine was tried" into "the press deadline ran out".
"""

_TIMED_WAIT_S = 0.5
"""Bound on a press that must expire on its own; a stall must fail, not hang."""


class HangingProbeEngine(FakeEngine):
    """A fake engine whose availability probe never answers on its own."""

    async def available(self) -> Availability:
        """Wait on an event nothing sets, so only the resolver's bound ends the probe.

        :returns: never; the wait is cancelled first.
        """
        self.available_calls += 1
        await asyncio.Event().wait()
        raise AssertionError("a never-set event must not return an availability")


class HangingRenderEngine(FakeEngine):
    """A fake engine whose render never finishes on its own."""

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Record the render, then wait on an event nothing sets.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path that is never written.
        :returns: never; the wait is cancelled by the press deadline.
        """
        self.renders.append((text, options, destination))
        await asyncio.Event().wait()
        raise AssertionError("a never-set event must not return a render")


async def test_a_transient_render_failure_is_retried_on_the_next_engine(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A transient failure is retried once, and the next engine speaks.

    The retry note names the retry, which is a fact about one press rather than a
    lasting condition, so the ``tts`` component stays READY because the press did
    speak.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts", error=TtsRenderError("Home Assistant timed out", transient=True))
    espeak = FakeEngine("espeak_ng")
    health = HealthRegistry()
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path), health=health)

    clip = await source.resolve(
        button=button, engine="ha_tts", deadline=PressDeadline.after(_PRESS_BUDGET_S)
    )

    assert clip.engine == "espeak_ng"
    assert clip.detail is not None
    assert "retried on espeak_ng" in clip.detail
    assert "transiently" in clip.detail
    assert len(ha.renders) == 1
    assert len(espeak.renders) == 1
    assert health.get("tts") is HealthStatus.READY
    assert health.message()["detail"] == "engine espeak_ng"


@pytest.mark.parametrize(
    "sentence",
    [
        "ha_tts could not render speech: Home Assistant rejected the token (401)",
        "ha_tts could not render speech: Home Assistant rejected the request (400)",
        "ha_tts could not render speech: Home Assistant has no such entity (404)",
    ],
)
async def test_a_non_transient_render_failure_is_never_retried(
    tmp_path: Path, store: ConfigStore, sentence: str
) -> None:
    """Auth, bad-request and not-found failures buy no second attempt at all.

    This is the test that stops a future change from turning the bounded transient
    retry into a blanket retry: the next engine is never even probed.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    :param sentence: the classified render failure the engine raised.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts", error=TtsRenderError(sentence, transient=False))
    espeak = FakeEngine("espeak_ng")
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable, match="could not render speech"):
        await source.resolve(
            button=button, engine="ha_tts", deadline=PressDeadline.after(_PRESS_BUDGET_S)
        )

    assert espeak.available_calls == 0
    assert espeak.renders == []
    assert len(ha.renders) == 1


async def test_a_retry_is_attempted_at_most_once(tmp_path: Path, store: ConfigStore) -> None:
    """A retried engine's transient failure propagates; the third engine stays untouched.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    remote = FakeEngine(
        "remote_tts", error=TtsRenderError("the renderer timed out", transient=True)
    )
    ha = FakeEngine("ha_tts", error=TtsRenderError("Home Assistant timed out", transient=True))
    espeak = FakeEngine("espeak_ng")
    source = TtsSpeechSource(
        store,
        [remote, ha, espeak],
        _cache(tmp_path),
        fallback_chain=("remote_tts", "ha_tts", "espeak_ng"),
    )

    with pytest.raises(SpeechUnavailable, match="Home Assistant timed out"):
        await source.resolve(
            button=button, engine="remote_tts", deadline=PressDeadline.after(_PRESS_BUDGET_S)
        )

    assert len(remote.renders) == 1
    assert len(ha.renders) == 1
    assert espeak.available_calls == 0
    assert espeak.renders == []


async def test_a_transient_failure_without_enough_budget_is_not_retried(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A retry that could not finish is not started, and the message says so.

    The budget is :data:`_UNDER_RETRY_BUDGET_S` rather than a near-zero one, so
    the press is refused for having too little *budget* while the first render
    still has abundant time to finish.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = FakeEngine("ha_tts", error=TtsRenderError("Home Assistant timed out", transient=True))
    espeak = FakeEngine("espeak_ng")
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path))

    with pytest.raises(SpeechUnavailable, match="no second engine was tried"):
        await source.resolve(
            button=button, engine="ha_tts", deadline=PressDeadline.after(_UNDER_RETRY_BUDGET_S)
        )

    assert len(ha.renders) == 1
    assert espeak.available_calls == 0
    assert espeak.renders == []


async def test_a_chain_of_silent_probes_stops_at_the_press_deadline(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Engines that never answer their probe are written off when the budget ends.

    This is the case the per-engine arithmetic could not bound: before HSD-96k6 a
    chain of silent probes could outlive the MQTT seam and leave no press row.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = HangingProbeEngine("ha_tts")
    espeak = HangingProbeEngine("espeak_ng")
    source = TtsSpeechSource(
        store,
        [ha, espeak],
        _cache(tmp_path),
        fallback_chain=("ha_tts", "espeak_ng"),
    )
    started = time.monotonic()

    with pytest.raises(SpeechUnavailable, match="no TTS engine available"):
        await source.resolve(button=button, engine="ha_tts", deadline=PressDeadline.after(0.1))

    elapsed = time.monotonic() - started
    assert elapsed < _TIMED_WAIT_S
    assert ha.available_calls == 1
    assert espeak.available_calls == 0


async def test_a_render_that_outlives_the_deadline_reports_the_deadline(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A render cut off by the press deadline names the deadline, not the engine.

    A timeout here is not transient: there is no budget left for a second
    attempt, so the engine after it is never probed.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    ha = HangingRenderEngine("ha_tts")
    espeak = FakeEngine("espeak_ng")
    source = TtsSpeechSource(store, [ha, espeak], _cache(tmp_path))
    started = time.monotonic()

    with pytest.raises(SpeechUnavailable, match="press deadline ran out"):
        await source.resolve(
            button=button, engine="ha_tts", deadline=PressDeadline.after(_TIGHT_BUDGET_S)
        )

    elapsed = time.monotonic() - started
    assert elapsed < _TIMED_WAIT_S
    assert len(ha.renders) == 1
    assert espeak.available_calls == 0
    assert espeak.renders == []


async def test_without_a_press_budget_a_transient_failure_is_not_retried(
    tmp_path: Path, store: ConfigStore
) -> None:
    """``deadline=None`` is the bake path: it keeps the no-retry rule.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    espeak = FakeEngine("espeak_ng", error=TtsRenderError("bad voice", transient=True))
    probe = FakeEngine("fallback_probe")
    source = TtsSpeechSource(
        store,
        [espeak, probe],
        _cache(tmp_path),
        fallback_chain=("espeak_ng", "fallback_probe"),
    )

    with pytest.raises(SpeechUnavailable, match="could not render speech"):
        await source.resolve(button=button, engine="espeak_ng")

    assert probe.available_calls == 0
    assert probe.renders == []


# --------------------------------------------------- the http_tts provider engine

_HTTP_ID: Final = "0123456789ab"
"""Provider id used by the ``http_tts`` option tests."""

_HTTP_RECORD: Final = {
    "id": _HTTP_ID,
    "name": "Kitchen voice",
    "preset": "openai",
    "base_url": "",
    "voice": "alloy",
    "language": "en_GB",
    "paid": True,
    "cost_per_1k_chars_usd": None,
    "overrides": {},
}
"""A valid provider record: the openai preset, its own default address."""


class _RecordingSettings(_DictSettings):
    """A settings reader that remembers which keys were read."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        """Create the reader.

        :param values: overrides keyed by setting name.
        """
        super().__init__(values)
        self.read: list[str] = []

    async def get_setting(self, key: str) -> object:
        """Record the read, then answer it.

        :param key: registered setting key.
        """
        self.read.append(key)
        return await super().get_setting(key)


def _http_settings(
    base_url: str = "http://provider.test", selected: str = _HTTP_ID
) -> _DictSettings:
    """Return a settings reader naming one provider and its address.

    :param base_url: the provider's own address.
    :param selected: value of ``tts.http_tts.provider``.
    """
    record = dict(_HTTP_RECORD, base_url=base_url)
    return _DictSettings({"tts.providers": [record], "tts.http_tts.provider": selected})


async def test_options_for_http_tts_maps_the_selected_provider() -> None:
    """``http_tts`` maps the selected record: voice, language, format and a fingerprint."""
    options = await options_for("http_tts", _http_settings())

    assert options.engine_id.startswith(f"{_HTTP_ID}@")
    provider_id, _, fingerprint = options.engine_id.partition("@")
    assert provider_id == _HTTP_ID
    assert len(fingerprint) == 8
    assert all(char in "0123456789abcdef" for char in fingerprint)
    assert options.voice == "alloy"
    assert options.language == "en_GB"
    assert options.preferred_format == "mp3"


async def test_the_http_tts_fingerprint_tracks_a_provider_edit_not_a_phrase() -> None:
    """A moved base URL changes the cache key; a different phrase always does.

    The fingerprint is what stops an edited provider (a new address, voice or
    format) from serving audio cached under the old request, because the cache
    key is ``(engine, options, text)``.
    """
    first = await options_for("http_tts", _http_settings("http://one.test"))
    same = await options_for("http_tts", _http_settings("http://one.test"))
    moved = await options_for("http_tts", _http_settings("http://two.test"))

    assert first == same
    assert first.engine_id != moved.engine_id
    assert cache_key("http_tts", first, "one phrase") != cache_key("http_tts", first, "another")
    assert cache_key("http_tts", first, "one phrase") != cache_key("http_tts", moved, "one phrase")


async def test_options_for_http_tts_with_no_selection_reads_nothing_else() -> None:
    """No selected provider is the plain default, and ``tts.providers`` is not read."""
    settings = _RecordingSettings({"tts.providers": [_HTTP_RECORD], "tts.http_tts.provider": ""})

    options = await options_for("http_tts", settings)

    assert options == TtsOptions(preferred_format="wav")
    assert settings.read == ["tts.http_tts.provider"]


async def test_options_for_http_tts_ignores_a_selection_that_no_longer_exists() -> None:
    """A selection pointing at a deleted provider answers the plain default."""
    options = await options_for("http_tts", _http_settings(selected="fedcba987654"))

    assert options == TtsOptions(preferred_format="wav")


async def test_the_built_source_registers_http_tts_but_keeps_it_out_of_the_chain(
    tmp_path: Path, store: ConfigStore
) -> None:
    """``http_tts`` is a registered engine and never a fallback target.

    A doorbell must not fall back *into* a paid provider, because that would bill
    on every press, so it is absent from :data:`FALLBACK_CHAIN`; an ``http_tts``
    request still tries it first and then falls through to the local chain.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    source = await build_speech_source(store, data_dir=tmp_path / "data")

    assert "http_tts" in source._engines
    assert "http_tts" not in FALLBACK_CHAIN
    assert source._chain_for("http_tts")[0] == "http_tts"
    assert source._chain_for("http_tts")[1:] == FALLBACK_CHAIN
    await source.aclose()
