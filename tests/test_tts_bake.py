"""Durable pre-rendered speech: the bake directory, its sidecars and the sweep.

Fully offline: the store is a real temporary SQLite database, the bake directory
a real directory under ``tmp_path``, and every engine a fake from
``tests/test_tts_speech.py`` that writes a RIFF stub instead of synthesising. No
module resolves a name, opens a socket or runs a real engine.

Owning ticket: HSD-dzek.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import random
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from test_tts_speech import FakeEngine

from chime_deck.press import SpeechUnavailable
from chime_deck.store import Button, ButtonIn, ConfigStore
from chime_deck.tts import (
    JITTER_MAX_S,
    JITTER_MIN_S,
    BakeFailure,
    BakeNotApplicable,
    BakeStore,
    BakeSweeper,
    PhraseTemplating,
    TtsCache,
    TtsOptions,
    TtsRenderError,
    TtsSpeechSource,
    baked_root,
    build_speech_source,
    expected_bake_sha,
    is_static,
    options_for,
)

pytestmark = pytest.mark.asyncio

_TEXT = "Someone is at the door"
_RENDERED = "Someone is at the front door"
_TEMPLATE = "Someone is at the {{ 'front door' }}"


class Mp3Engine(FakeEngine):
    """A fake engine that answers with MP3 bytes whatever format was requested."""

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Record the render, then write an ID3 header and padding.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders.append((text, options, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ID3" + b"\x00" * 200)
        return destination


class FlakyEngine(FakeEngine):
    """A fake engine whose first render fails and whose second succeeds.

    The first call is always the bake, the second always the cache fallback a
    failed bake degrades to.
    """

    def __init__(self, name: str) -> None:
        """Create the engine.

        :param name: engine name registered with the resolver.
        """
        super().__init__(name)
        self.calls = 0

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Fail the first render, then delegate to :class:`FakeEngine`.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.calls += 1
        if self.calls == 1:
            self.renders.append((text, options, destination))
            raise TtsRenderError("the voice model is not downloaded")
        return await super().render(text, options, destination=destination)


class FixedRenderer:
    """A template renderer that answers with a fixed phrase and counts calls."""

    def __init__(self, answer: str) -> None:
        """Create the renderer.

        :param answer: phrase every render returns.
        """
        self._answer = answer
        self.calls = 0

    async def render(self, template: str) -> object:
        """Return the fixed answer, counting the render.

        :param template: the phrase as stored on the button.
        """
        self.calls += 1
        return self._answer


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


async def _seed_tts(
    store: ConfigStore,
    *,
    name: str = "Front Door",
    text: str = _TEXT,
    tts_engine: str | None = None,
) -> Button:
    """Create a TTS button and return it.

    :param store: connected configuration store.
    :param name: display name, which also decides the slug.
    :param text: phrase the button speaks.
    :param tts_engine: per-button engine override, or ``None`` for the default.
    """
    return await store.upsert_button(
        ButtonIn(name=name, action_type="tts", tts_text=text, tts_engine=tts_engine)
    )


async def _source(
    store: ConfigStore, data_dir: Path, engines: list[FakeEngine], *, bakes: BakeStore | None = None
) -> TtsSpeechSource:
    """Build the resolver the way the application does.

    :param store: connected configuration store.
    :param data_dir: the install's data directory.
    :param engines: fake engines to register.
    :param bakes: bake store to wire, or ``None`` for the default under *data_dir*.
    """
    return await build_speech_source(store, data_dir=data_dir, engines=engines, bakes=bakes)


async def test_ten_presses_of_a_static_phrase_render_once(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Ten presses call the engine once and play the identical file byte-for-byte.

    Acceptance: a static phrase is synthesised once, then served from
    ``<data>/tts/baked/<button id>/<sha>.wav`` with a sidecar that records the
    engine, the options and the real container.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    engine = FakeEngine("espeak_ng")
    source = await _source(store, data_dir, [engine])
    button = await _seed_tts(store)

    clips = [await source.resolve(button=button, engine="espeak_ng") for _ in range(10)]

    assert len(engine.renders) == 1
    paths = {clip.path for clip in clips}
    assert len(paths) == 1
    artefact = clips[0].path
    assert all(path.read_bytes() == artefact.read_bytes() for path in paths)

    directory = data_dir / "tts" / "baked" / str(button.id)
    options = await options_for("espeak_ng", store)
    sha = expected_bake_sha("espeak_ng", options, _TEXT)
    assert artefact == directory / f"{sha}.wav"
    sidecar = json.loads((directory / f"{sha}.json").read_text(encoding="utf-8"))
    assert sidecar["sha256"] == sha
    assert sidecar["format"] == "wav"
    assert sidecar["text"] == _TEXT
    assert sidecar["engine"] == "espeak_ng"
    assert sidecar["engine_options"] == dataclasses.asdict(options)
    assert sidecar["bytes"] == artefact.stat().st_size


async def test_expected_bake_sha_is_deterministic_across_restarts(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Two independently built sources bake the same phrase to the same digest.

    Acceptance: "deterministic across restarts" — the digest is
    :func:`~chime_deck.tts.cache.cache_key`'s, so a second process with the
    same settings replays the artefact instead of paying for a new synthesis.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    first = await _source(store, tmp_path / "one", [FakeEngine("espeak_ng")])
    second = await _source(store, tmp_path / "two", [FakeEngine("espeak_ng")])

    clip_a = await first.resolve(button=button, engine="espeak_ng")
    clip_b = await second.resolve(button=button, engine="espeak_ng")

    options = await options_for("espeak_ng", store)
    same_values = TtsOptions(voice=options.voice, speed=options.speed, preferred_format="wav")
    sha = expected_bake_sha("espeak_ng", same_values, _TEXT)
    assert clip_a.path.name == f"{sha}.wav"
    assert clip_b.path.name == f"{sha}.wav"


async def test_an_mp3_engine_is_stored_as_mp3_and_found_again(
    tmp_path: Path, store: ConfigStore
) -> None:
    """The artefact's suffix comes from its bytes, never from the requested format.

    Acceptance (D25): external providers answer in MP3, so the bake is stored as
    ``<sha>.mp3`` even though the request asked for ``.wav``, and it is never
    transcoded. :meth:`BakeStore.path_for` finds it afterwards.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    engine = Mp3Engine("espeak_ng")
    bakes = BakeStore(baked_root(data_dir))
    source = TtsSpeechSource(store, [engine], TtsCache(data_dir / "tts-cache"), bakes=bakes)
    button = await _seed_tts(store)

    clip = await source.resolve(button=button, engine="espeak_ng")

    assert clip.path.suffix == ".mp3"
    assert clip.path.read_bytes().startswith(b"ID3")
    assert bakes.path_for(button.id, clip.path.stem) == clip.path
    sidecar = json.loads(clip.path.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["format"] == "mp3"

    again = await source.resolve(button=button, engine="espeak_ng")

    assert again.path == clip.path
    assert len(engine.renders) == 1


async def test_a_templated_phrase_is_never_baked(tmp_path: Path, store: ConfigStore) -> None:
    """A dynamic phrase has no bake directory and reuses its cache entry.

    Acceptance (design 04 §2.2): ``{{ … }}`` is rendered on every press, so it
    must never be baked; an unchanged render hits one cache entry.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    engine = FakeEngine("ha_tts")
    renderer = FixedRenderer(_RENDERED)
    source = TtsSpeechSource(
        store,
        [engine],
        TtsCache(data_dir / "tts-cache"),
        templating=PhraseTemplating(renderer),
        bakes=BakeStore(baked_root(data_dir)),
    )
    button = await _seed_tts(store, text=_TEMPLATE)

    first = await source.resolve(button=button, engine="ha_tts")
    second = await source.resolve(button=button, engine="ha_tts")

    assert not is_static(_TEMPLATE)
    assert first.path.parent == data_dir / "tts-cache"
    assert second.path == first.path
    assert renderer.calls == 2
    assert len(engine.renders) == 1
    assert not (data_dir / "tts" / "baked").exists()


async def test_changing_the_text_engine_or_an_option_replaces_the_bake(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A changed phrase, engine or option deletes the old artefact and bakes a new one.

    Acceptance: the three inputs of :func:`expected_bake_sha` each move the
    digest, and the previous artefact is pruned in the same write.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    espeak = FakeEngine("espeak_ng")
    ha = FakeEngine("ha_tts")
    source = await _source(store, data_dir, [espeak, ha])
    button = await _seed_tts(store, text="one", tts_engine="espeak_ng")

    first = await source.bake_button(button=button)
    assert first.path == data_dir / "tts" / "baked" / str(button.id) / f"{first.sha}.wav"

    second = await source.bake_button(button=dataclasses.replace(button, tts_text="two"))
    assert second.sha != first.sha
    assert not first.path.exists()
    assert second.path.exists()

    third = await source.bake_button(button=dataclasses.replace(button, tts_engine="ha_tts"))
    assert third.sha not in (first.sha, second.sha)
    assert not second.path.exists()
    assert third.path.exists()

    await store.set_setting("tts.espeak_voice", "en-us")
    fourth = await source.bake_button(button=button)
    assert fourth.sha not in (first.sha, second.sha, third.sha)
    assert not third.path.exists()

    directory = data_dir / "tts" / "baked" / str(button.id)
    assert sorted(path.suffix for path in directory.iterdir()) == [".json", ".wav"]


async def test_deleting_the_artefact_regenerates_it_on_the_next_press(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A missing bake self-heals: the next press rebuilds exactly the same path.

    Acceptance: deleting a bake regenerates it on the next press.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    engine = FakeEngine("espeak_ng")
    source = await _source(store, tmp_path / "data", [engine])
    button = await _seed_tts(store)

    first = await source.resolve(button=button, engine="espeak_ng")
    first.path.unlink()
    second = await source.resolve(button=button, engine="espeak_ng")

    assert len(engine.renders) == 2
    assert second.path == first.path
    assert second.path.is_file()


async def test_a_failed_bake_still_speaks_and_does_not_stop_other_buttons(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A bake failure sets ``baked_error``, plays from the cache, and blocks nobody.

    Acceptance: a bake is a cost optimisation, never a correctness requirement
    (design 04 §2.3). The flaky engine fails the bake and succeeds on the cache
    fallback, and a second button on a working engine still bakes.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    flaky = FlakyEngine("espeak_ng")
    working = FakeEngine("ha_tts")
    source = await _source(store, data_dir, [flaky, working])
    button = await _seed_tts(store, text="hello")
    other = await _seed_tts(store, name="Back Door", text="goodbye")

    clip = await source.resolve(button=button, engine="espeak_ng")

    assert clip.path.is_file()
    assert clip.path.parent == data_dir / "tts-cache"
    assert flaky.calls == 2
    stored = await store.get_button_by_id(button.id)
    assert stored is not None
    assert stored.baked_error is not None
    assert "the voice model is not downloaded" in stored.baked_error
    assert stored.baked_sha is None

    played = await source.resolve(button=other, engine="ha_tts")

    assert played.path.parent == data_dir / "tts" / "baked" / str(other.id)


async def test_bake_button_refuses_what_can_never_be_prerendered(
    tmp_path: Path, store: ConfigStore
) -> None:
    """A sound button, blank text and a template are refusals, not failures.

    None of them may probe an engine: refusal happens before :meth:`_choose`, so
    a button that has nothing to bake costs nothing.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    engine = FakeEngine("espeak_ng")
    source = await _source(store, tmp_path / "data", [engine])
    button = await _seed_tts(store)

    with pytest.raises(BakeNotApplicable, match="is a sound button"):
        await source.bake_button(button=dataclasses.replace(button, action_type="sound"))
    with pytest.raises(BakeNotApplicable, match="has no text to pre-render"):
        await source.bake_button(button=dataclasses.replace(button, tts_text="   "))
    with pytest.raises(BakeNotApplicable, match="Home Assistant template"):
        await source.bake_button(button=dataclasses.replace(button, tts_text=_TEMPLATE))

    assert engine.available_calls == 0
    assert engine.renders == []


async def test_bake_button_is_unavailable_when_speech_is_switched_off(
    tmp_path: Path, store: ConfigStore
) -> None:
    """Both ``none`` switches refuse without walking the fallback chain (D27).

    Acceptance: an explicitly off engine is a configuration, not an unavailable
    engine, so ``available()`` is never called and no other engine speaks.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    await store.set_setting("tts.default_engine", "none")
    engine = FakeEngine("espeak_ng")
    source = TtsSpeechSource(
        store, [engine], TtsCache(tmp_path / "tts-cache"), bakes=BakeStore(tmp_path / "baked")
    )
    button = await _seed_tts(store)

    with pytest.raises(SpeechUnavailable, match=r"tts\.default_engine is 'none'"):
        await source.bake_button(button=button)
    assert engine.available_calls == 0

    await store.set_setting("tts.default_engine", "espeak_ng")
    with pytest.raises(SpeechUnavailable, match="tts_engine is 'none'"):
        await source.bake_button(button=dataclasses.replace(button, tts_engine="none"))
    assert engine.available_calls == 0
    assert not (tmp_path / "baked").exists()


async def test_bake_all_reports_baked_skipped_and_failed(
    tmp_path: Path, store: ConfigStore
) -> None:
    """One bake, one dynamic skip, one current skip, one recorded failure.

    Acceptance: a broken engine records ``baked_error`` on its own button and
    stops nobody else; a second run reports the survivor as current.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    working = FakeEngine("espeak_ng")
    broken = FakeEngine("piper", error=TtsRenderError("the voice model is not downloaded"))
    source = await _source(store, tmp_path / "data", [working, broken])
    static = await _seed_tts(store, name="Static", tts_engine="espeak_ng")
    dynamic = await _seed_tts(store, name="Dynamic", text=_TEMPLATE)
    failed_button = await _seed_tts(store, name="Broken", tts_engine="piper")

    first = await source.bake_all(await store.list_buttons())

    assert first.baked == (static.id,)
    assert first.skipped_dynamic == (dynamic.id,)
    assert first.skipped_current == ()
    assert first.failed == (
        BakeFailure(
            button_id=failed_button.id,
            error="piper could not pre-render this phrase: the voice model is not downloaded",
        ),
    )

    second = await source.bake_all(await store.list_buttons())

    assert second.baked == ()
    assert second.skipped_current == (static.id,)
    assert second.skipped_dynamic == (dynamic.id,)
    assert second.failed == first.failed
    stored = await store.get_button_by_id(failed_button.id)
    assert stored is not None
    assert stored.baked_error is not None
    assert first.failed[0].error.endswith(stored.baked_error)


async def _render_stub(destination: Path) -> None:
    """Write a RIFF stub at *destination*.

    :param destination: path the bake store asks the renderer to fill.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"RIFF....WAVEfmt ")


async def test_the_bake_store_sweeps_discards_and_counts(tmp_path: Path) -> None:
    """The store owns the directory rules, independent of the resolver.

    Acceptance: the startup sweep removes orphan directories and keeps live
    ones, deleting a button's bake is idempotent, a digest that is not a digest
    is refused, and usage counts both trees.

    :param tmp_path: pytest temporary directory.
    """
    root = baked_root(tmp_path / "data")
    bakes = BakeStore(root, clock=lambda: "2026-01-01T00:00:00Z")
    sha = "a" * 64

    artefact = await bakes.write(
        button_id=7,
        sha=sha,
        render=_render_stub,
        engine="espeak_ng",
        options=TtsOptions(voice="en-gb", speed=160, preferred_format="wav"),
        text="hello",
    )

    assert artefact.path == root / "7" / f"{sha}.wav"
    assert bakes.path_for(7, sha) == artefact.path
    sidecar = json.loads((root / "7" / f"{sha}.json").read_text(encoding="utf-8"))
    assert sidecar["created_at"] == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="invalid bake digest"):
        bakes.path_for(7, "../../etc/passwd")
    assert bakes.path_for(8, sha) is None

    orphan = root / "9"
    orphan.mkdir()
    (orphan / "z.wav").write_bytes(b"z")
    (root / "not-a-button").mkdir()

    assert await bakes.sweep([7]) == 1
    assert (root / "7").is_dir()
    assert not orphan.exists()
    assert (root / "not-a-button").is_dir()

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "clip.wav").write_bytes(b"1234")
    usage = bakes.usage(cache_root=cache)
    assert usage.baked_files == 2
    assert usage.baked_bytes > 0
    assert usage.cache_files == 1
    assert usage.cache_bytes == 4

    assert await bakes.discard(7) == 2
    assert not (root / "7").exists()
    assert await bakes.discard(7) == 0


async def test_the_sweeper_sweeps_orphans_then_retries_missing_bakes(
    tmp_path: Path, store: ConfigStore
) -> None:
    """One call removes an orphan directory and bakes a missing phrase.

    Acceptance: the startup sweep removes orphans and retries missing bakes; a
    second call reports the phrase as current instead of re-rendering it.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    engine = FakeEngine("espeak_ng")
    bakes = BakeStore(baked_root(data_dir))
    source = TtsSpeechSource(store, [engine], TtsCache(data_dir / "tts-cache"), bakes=bakes)
    button = await _seed_tts(store, tts_engine="espeak_ng")
    orphan = baked_root(data_dir) / "99"
    orphan.mkdir(parents=True)
    sweeper = BakeSweeper(store=store, speech=source, bakes=bakes)

    report = await sweeper.run_once()

    assert report.orphans_removed == 1
    assert report.batch.baked == (button.id,)
    assert not orphan.exists()
    assert len(engine.renders) == 1

    again = await sweeper.run_once()

    assert again.orphans_removed == 0
    assert again.batch.baked == ()
    assert again.batch.skipped_current == (button.id,)
    assert len(engine.renders) == 1


async def test_start_waits_one_jittered_delay_and_sweeps_once(
    tmp_path: Path, store: ConfigStore
) -> None:
    """The background task sleeps once, inside the jitter window, then sweeps.

    Acceptance: the retry runs exactly once per boot (design 04 §2.4), so a
    fleet rebooting together does not stampede a cloud endpoint. The delay is
    recorded rather than waited, which is what keeps the test instant.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    data_dir = tmp_path / "data"
    engine = FakeEngine("espeak_ng")
    bakes = BakeStore(baked_root(data_dir))
    source = TtsSpeechSource(store, [engine], TtsCache(data_dir / "tts-cache"), bakes=bakes)
    button = await _seed_tts(store, tts_engine="espeak_ng")
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        """Record the jitter and return without waiting.

        :param delay: seconds the sweeper asked to wait.
        """
        delays.append(delay)

    sweeper = BakeSweeper(
        store=store, speech=source, bakes=bakes, sleep=record_sleep, rng=random.Random(4)
    )

    await sweeper.start()
    await sweeper.start()
    for _ in range(200):
        if len(engine.renders) == 1:
            break
        await asyncio.sleep(0.01)
    await sweeper.stop()

    assert len(delays) == 1
    assert JITTER_MIN_S <= delays[0] <= JITTER_MAX_S
    assert len(engine.renders) == 1
    assert (await source.bake_usage()).baked_files == 2
    sha = expected_bake_sha("espeak_ng", await options_for("espeak_ng", store), _TEXT)
    assert bakes.path_for(button.id, sha) is not None

    never_started = BakeSweeper(store=store, speech=source, bakes=bakes)
    await never_started.stop()
