"""Structurally offline wiring for every application a test builds.

AGENTS.md HARD RULE 4 says no test may touch a real broker, a sound card or a
network. Before this module that rule held only because each test remembered to
inject its fakes; now it holds by construction: :func:`offline_wiring` is
autouse, and it replaces the ONE production seam
(``chime_deck.app.default_wiring``, a factory rather than a ``Final``
constant, which is what makes the replacement legal) with a
:class:`~chime_deck.wiring.Wiring` whose speaker is
:class:`~chime_fakes.RecordingBackend` and whose MQTT clients come from
:class:`~mqtt_fakes.FakeBroker`. A test that builds an app with
``create_app(config)`` therefore cannot open a paho socket or a sound card even
if it forgets everything else.

:func:`fake_broker` and :func:`fake_backend` are ordinary fixtures, so a test
that wants to inspect or drive the fakes simply requests them and receives the
very instances the application under test is using.

:func:`no_real_ffprobe` is autouse too: it makes ``ffprobe`` look absent for
every test, so a unit test that forgets to inject a fake probe still cannot
execute the system binary. :func:`real_ffprobe` opts one integration test back
in by taking :func:`no_real_ffprobe` as an argument (fixture ordering) and
restoring ``shutil.which``.

One honesty note about timing: :class:`~mqtt_fakes.RecordingSleep` records the
requested delay and returns without yielding to the event loop. The production
birth jitter really waits 0-2 s, so ordering that depends on another task
running *during* that wait is slightly optimistic here; no test in this suite
relies on such an ordering.
"""

from __future__ import annotations

import shutil

import pytest
from audio_hardware_fakes import RecordingSpawner
from chime_fakes import RecordingBackend
from mqtt_fakes import FakeBroker, FixedRandom, RecordingSleep

from chime_deck.wiring import Wiring

__all__ = [
    "fake_backend",
    "fake_broker",
    "fake_hardware_spawner",
    "no_real_ffprobe",
    "offline_wiring",
    "real_ffprobe",
]


@pytest.fixture
def fake_broker() -> FakeBroker:
    """Return the in-memory broker every application built by this test uses."""
    return FakeBroker()


@pytest.fixture
def fake_backend() -> RecordingBackend:
    """Return the recording speaker every application built by this test uses."""
    return RecordingBackend()


@pytest.fixture
def fake_hardware_spawner() -> RecordingSpawner:
    """Return the fake ``amixer``/``pactl`` spawner every application uses here."""
    return RecordingSpawner()


@pytest.fixture(autouse=True)
def offline_wiring(
    monkeypatch: pytest.MonkeyPatch,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    fake_hardware_spawner: RecordingSpawner,
) -> Wiring:
    """Make every application this test builds fully offline.

    The production factory is replaced rather than the resolved object, because
    :func:`chime_deck.app.create_app` calls it at construction time: a test
    that wants a different wiring can still pass ``wiring=...`` explicitly.

    :param monkeypatch: patcher that installs the replacement factory.
    :param fake_broker: broker the injected client factory comes from.
    :param fake_backend: speaker the injected audio backend is.
    :param fake_hardware_spawner: fake mixer commands, so startup's hardware
        volume apply can never execute a real ``amixer`` (HARD RULE 4).
    :return: the wiring every application built through ``create_app`` receives.
    """
    wiring = Wiring(
        audio_backend=fake_backend,
        hardware_spawner=fake_hardware_spawner,
        mqtt_client_factory=fake_broker.factory,
        mqtt_sleep=RecordingSleep(),
        mqtt_rng=FixedRandom(0.0),
    )

    def factory() -> Wiring:
        """Return the offline wiring for every application built in this test."""
        return wiring

    monkeypatch.setattr("chime_deck.app.default_wiring", factory)
    return wiring


@pytest.fixture(autouse=True)
def no_real_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``ffprobe`` look absent for every test that does not opt back in.

    Duration probing is metadata, so the production code degrades to ``None``
    when the binary is missing. Patching the one lookup seam here means no unit
    test can execute the real system binary even if it forgets to inject a fake
    probe, which keeps the suite offline and identical on every host.

    :param monkeypatch: patcher that installs the absent lookup.
    """
    monkeypatch.setattr("chime_deck.sounds.probe.DEFAULT_WHICH", lambda _name: None)


@pytest.fixture
def real_ffprobe(no_real_ffprobe: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt one integration test back in to the real ``ffprobe`` lookup.

    Taking :func:`no_real_ffprobe` first forces the autouse patch to run before
    this fixture restores ``shutil.which``, so the ordering is deterministic.
    The test must still skip cleanly when the binary is absent; this fixture
    only removes the deliberate offline override.

    :param no_real_ffprobe: guarantees the offline patch was applied first.
    :param monkeypatch: patcher that restores the real lookup.
    """
    monkeypatch.setattr("chime_deck.sounds.probe.DEFAULT_WHICH", shutil.which)
