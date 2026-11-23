"""The device's own volume control: resolution, reading, writing, failing soft.

Every test injects :class:`~audio_hardware_fakes.RecordingSpawner`, so the suite
never executes ``amixer`` or ``pactl`` (AGENTS.md HARD RULE 4); the fake mixer
below answers the same shapes a real one prints. The load-bearing test is
:func:`test_applied_comes_from_the_read_back_not_from_the_request`: the panel
must show what the device says, so ``applied`` is taken from a second read and
never from the percentage the app asked for. Its Pulse twin,
:func:`test_a_pulse_write_reports_the_level_the_card_reached`, pins the same rule
for a sink whose backing card quantises the level the sink repeated.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from audio_hardware_fakes import (
    FakeCommand,
    FakeControl,
    FakeMixer,
    FakePulse,
    RecordingSpawner,
    pulse_with_card,
)

from chime_deck.audio import (
    HARDWARE_NOT_STARTED_TEXT,
    AudioBackendNotStarted,
    AudioDevice,
    HardwareVolume,
)
from chime_deck.audio.hardware import (
    NO_HARDWARE_CONTROL_TEXT,
    alsa_card_candidates,
    card_token,
    choose_control,
    has_playback_volume,
    parse_amixer_muted,
    parse_amixer_percent,
    parse_control_names,
    parse_pactl_mute,
    parse_pactl_percent,
    parse_sink_properties,
    pulse_sink_name,
)
from chime_deck.store import ConfigStore

pytestmark = pytest.mark.asyncio

_USB_DEVICE = "alsa/hw:CARD=Device,DEV=0"
_PULSE_DEVICE = "pulse/alsa_output.usb-card"


class _Backend:
    """An :class:`~chime_deck.audio.AudioBackend` that only lists devices."""

    def __init__(
        self,
        devices: Sequence[AudioDevice] | None = None,
        *,
        list_error: Exception | None = None,
    ) -> None:
        """Create a backend reporting *devices*.

        :param devices: outputs to report; ``None`` means one USB DAC.
        :param list_error: raised by :meth:`list_devices` when a test sets it.
        """
        self.devices: list[AudioDevice] = (
            [AudioDevice(name=_USB_DEVICE, hostapi="alsa")] if devices is None else list(devices)
        )
        self.list_error = list_error
        self.hints: list[str] = []

    @property
    def playing(self) -> bool:
        """Return ``False``: this fake plays nothing."""
        return False

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Do nothing; there is no player behind this fake.

        :param timeout_s: budget the caller offered, ignored.
        """

    async def stop(self) -> None:
        """Do nothing; there is no player behind this fake."""

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Refuse to play: a mixer test never makes a sound.

        :param path: file that would have been played.
        :param volume: volume that would have been used.
        :param lead_in_ms: lead-in that would have been asked for.
        :param when_done: completion callback that is never run.
        :raises AudioBackendNotStarted: always.
        """
        raise AudioBackendNotStarted("the fake backend never plays")

    async def stop_playback(self) -> None:
        """Do nothing; nothing is playing."""

    async def set_device(self, hint: str) -> None:
        """Record the requested device.

        :param hint: device hint a real player would resolve.
        """
        self.hints.append(hint)

    async def list_devices(self) -> list[AudioDevice]:
        """Return the configured outputs, or raise the configured error.

        :raises Exception: the configured ``list_error``.
        """
        if self.list_error is not None:
            raise self.list_error
        return list(self.devices)


class _UnreadableStore:
    """A store whose ``get_setting`` always fails, for the fail-soft store path."""

    async def get_setting(self, key: str) -> object:
        """Fail every read.

        :param key: setting that was asked for.
        :raises RuntimeError: always, naming the key.
        """
        raise RuntimeError(f"database is locked, cannot read {key}")


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


def _service(
    store: ConfigStore,
    spawner: RecordingSpawner,
    backend: _Backend | None = None,
) -> HardwareVolume:
    """Build the service under test around a fake backend and spawner.

    :param store: configuration store the service reads.
    :param spawner: fake mixer commands.
    :param backend: speaker accessor result; ``None`` means a USB DAC backend.
    """
    chosen = _Backend() if backend is None else backend
    return HardwareVolume(store=store, backend=lambda: chosen, spawner=spawner)


def _spawner_for(
    mixer: Mapping[str, FakeControl] | FakeMixer | FakePulse,
) -> RecordingSpawner:
    """Return a spawner that answers from *mixer*.

    :param mixer: callable answering one argv, or the mapping a fake ``amixer``
        should be built from.
    """
    if isinstance(mixer, Mapping):
        return RecordingSpawner(FakeMixer(dict(mixer)))
    return RecordingSpawner(mixer)


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (_USB_DEVICE, "Device"),
        ("alsa/hw:2,0", "2"),
        ("alsa/plughw:CARD=USB,DEV=0", "USB"),
        ("alsa/default", None),
        ("alsa/plughw:0,0", "0"),
        (_PULSE_DEVICE, None),
    ],
)
async def test_the_card_token_comes_from_the_device_name(device: str, expected: str | None) -> None:
    """A device name either names its card or it does not; nothing is guessed.

    :param device: mpv device name under test.
    :param expected: card token to pass after ``-c``, or ``None``.
    """
    assert card_token(device) == expected


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (_PULSE_DEVICE, "alsa_output.usb-card"),
        ("pipewire/my-sink", "my-sink"),
        ("alsa/hw:2,0", None),
        ("auto", None),
        ("pipewire/", None),
    ],
)
async def test_only_pulse_and_pipewire_names_resolve_to_a_sink(
    device: str, expected: str | None
) -> None:
    """``pactl`` is only right for the host APIs that are actually Pulse sinks.

    :param device: mpv device name under test.
    :param expected: sink name, or ``None`` for an ALSA device.
    """
    assert pulse_sink_name(device) == expected


async def test_the_preference_order_picks_speaker_over_mic_and_auto_gain_control() -> None:
    """A USB DAC's output control wins over the ones meant for recording."""
    controls = ("Mic", "Auto Gain Control", "Speaker")

    assert choose_control(controls, "") == "Speaker"


async def test_the_preference_order_falls_back_through_its_list() -> None:
    """With no Speaker, Headphone, then Master are tried in that order."""
    assert choose_control(("Mic", "Master"), "") == "Master"
    assert choose_control(("Headphone", "Mic", "Master"), "") == "Headphone"


async def test_an_explicit_control_wins_and_is_matched_case_insensitively() -> None:
    """The stored name beats the preference order, exact match first."""
    controls = ("Mic", "Auto Gain Control", "Speaker")

    assert choose_control(controls, "auto gain control") == "Auto Gain Control"
    assert choose_control(controls, "speaker") == "Speaker"


async def test_an_explicit_control_may_match_as_a_substring() -> None:
    """ALSA often suffixes a control, so a substring match is accepted."""
    assert choose_control(("Speaker Playback Volume",), "Speaker") == "Speaker Playback Volume"


async def test_an_unmatched_explicit_control_resolves_to_nothing() -> None:
    """A wrong manual setting is reported by the caller, not silently ignored."""
    assert choose_control(("Speaker", "Mic"), "NoSuchControl") is None


async def test_control_names_are_read_out_of_scontrols_output() -> None:
    """The names are the quoted text amixer prints, in its own order."""
    output = "Simple mixer control 'Speaker',0\nSimple mixer control 'Mic',0\n"

    assert parse_control_names(output) == ("Speaker", "Mic")


async def test_a_volume_line_is_recognised_and_a_switch_only_line_is_not() -> None:
    """The HDMI-case discriminator: ``Playback`` with a percentage or without."""
    volume = "  Front Left: Playback 43 [43%] [-20.00dB] [on]\n"
    switch_only = (
        "  Capabilities: pswitch pswitch-joined\n"
        "  Playback channels: Mono\n"
        "  Mono: Playback [off]\n"
    )

    assert has_playback_volume(volume) is True
    assert has_playback_volume(switch_only) is False


async def test_the_amixer_percentage_and_switch_are_parsed() -> None:
    """``[43%]`` is the value and ``[off]`` is the mute marker."""
    text = "  Front Left: Playback 43 [43%] [-20.00dB] [off]\n"

    assert parse_amixer_percent(text) == 43
    assert parse_amixer_muted(text) is True
    assert parse_amixer_muted(text.replace("[off]", "[on]")) is False
    assert parse_amixer_percent("  Mono: Playback [on]\n") is None


async def test_the_pactl_percentage_and_mute_are_parsed() -> None:
    """A pactl sink reads as a percentage and a ``Mute: yes`` line."""
    volume = "Volume: front-left: 26214 / 40% / -23.81 dB, front-right: 26214 / 40% / -23.81 dB\n"

    assert parse_pactl_percent(volume) == 40
    assert parse_pactl_mute("Mute: yes\n") is True
    assert parse_pactl_mute("Mute: no\n") is False


async def test_a_sinks_properties_are_read_from_the_listing_that_names_it() -> None:
    """Properties are per sink: another sink's are not returned, an unlisted sink is ``None``."""
    listing = (
        "Sink #0\n"
        "\tState: SUSPENDED\n"
        "\tName: alsa_output.first\n"
        "\tDescription: First\n"
        "\tProperties:\n"
        '\t\tdevice.api = "alsa"\n'
        '\t\talsa.card = "1"\n'
        "Sink #1\n"
        "\tState: RUNNING\n"
        "\tName: alsa_output.second\n"
        "\tDescription: Second\n"
        "\tProperties:\n"
        '\t\tdevice.api = "alsa"\n'
        '\t\talsa.id = "Device"\n'
        '\t\tapi.alsa.path = "hw:CARD=Device,DEV=0"\n'
        "Sink #2\n"
        "\tState: IDLE\n"
        "\tName: alsa_output.third\n"
        "\tDescription: A sink with nothing to declare\n"
    )

    properties = parse_sink_properties(listing, "alsa_output.second")

    assert properties == {
        "device.api": "alsa",
        "alsa.id": "Device",
        "api.alsa.path": "hw:CARD=Device,DEV=0",
    }
    assert properties is not None
    assert "alsa.card" not in properties
    assert parse_sink_properties(listing, "alsa_output.first") == {
        "device.api": "alsa",
        "alsa.card": "1",
    }
    assert parse_sink_properties(listing, "alsa_output.third") == {}
    assert parse_sink_properties(listing, "alsa_output.absent") is None


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        (
            {"api.alsa.path": "hw:CARD=Device,DEV=0", "alsa.id": "Device", "alsa.card": "1"},
            ("Device", "1"),
        ),
        ({"api.alsa.card.longname": "HDA Intel PCH at 0x0 hw:CARD=PCH,DEV=0"}, ("PCH",)),
        ({"alsa.id": "Device", "alsa.card": "1"}, ("Device", "1")),
        ({"alsa.card": "0"}, ("0",)),
        ({"api.alsa.pcm.card": "2", "api.alsa.card": "2"}, ("2",)),
        ({"api.alsa.card.name": "Fake Card", "alsa.card_name": "Fake Card"}, ()),
        ({}, ()),
    ],
)
async def test_the_card_candidates_prefer_the_id_and_never_a_card_name(
    properties: Mapping[str, str], expected: tuple[str, ...]
) -> None:
    """``amixer -c`` takes an index or a card id, so a card *name* is never offered.

    ``api.alsa.path`` names the ``CARD=`` token most directly, the id cannot
    reorder across boots the way an index can, and a name that only
    ``api.alsa.card.name`` carries would be rejected before amixer attached.

    :param properties: one sink's ``pactl`` properties.
    :param expected: the candidate tokens, in the order they are tried.
    """
    assert alsa_card_candidates(properties) == expected


async def test_an_alsa_device_is_read_with_its_card_argument(store: ConfigStore) -> None:
    """The read drives ``scontrols`` then ``sget`` on the resolved card."""
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(mixer)

    status = await _service(store, spawner).read()

    assert spawner.calls == [
        ("amixer", "-c", "Device", "scontrols"),
        ("amixer", "-c", "Device", "sget", "Speaker"),
    ]
    assert status.available is True
    assert status.device == _USB_DEVICE
    assert status.control == "Speaker"
    assert status.percent == 43
    assert status.muted is False
    assert status.reason == ""
    assert status.applied is None
    assert mixer.writes == []


async def test_a_numbered_card_is_passed_through(store: ConfigStore) -> None:
    """``alsa/hw:2,0`` drives ``amixer -c 2``."""
    backend = _Backend([AudioDevice(name="alsa/hw:2,0", hostapi="alsa", is_default=True)])
    spawner = _spawner_for({"Speaker": FakeControl(percent=10)})

    await _service(store, spawner, backend).read()

    assert spawner.calls[0] == ("amixer", "-c", "2", "scontrols")


async def test_the_alsa_default_device_has_no_card_argument(store: ConfigStore) -> None:
    """``alsa/default`` lets amixer open the ALSA default card itself."""
    backend = _Backend([AudioDevice(name="alsa/default", hostapi="alsa", is_default=True)])
    spawner = _spawner_for({"Master": FakeControl(percent=10)})

    await _service(store, spawner, backend).read()

    assert spawner.calls == [
        ("amixer", "scontrols"),
        ("amixer", "sget", "Master"),
    ]


async def test_a_pulse_sink_is_driven_with_pactl(store: ConfigStore) -> None:
    """A PipeWire/Pulse output is written through ``pactl`` and read through both tools."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(pulse)

    status = await _service(store, spawner, backend).read()

    assert spawner.calls == [
        ("pactl", "get-sink-volume", "alsa_output.usb-card"),
        ("pactl", "get-sink-mute", "alsa_output.usb-card"),
        ("pactl", "list", "sinks"),
        ("amixer", "-c", "Device", "scontrols"),
        ("amixer", "-c", "Device", "sget", "Speaker"),
    ]
    assert status.available is True
    assert status.control == "alsa_output.usb-card"
    assert status.percent == 43
    assert status.sink_percent == 40


async def test_a_pulse_write_reports_the_level_the_card_reached(store: ConfigStore) -> None:
    """The money case: ``pactl`` takes the request, the card behind the sink answers.

    The stored 60 is written to the sink, but the panel's C-Media card turns it
    into 65. ``percent`` and ``applied`` are that 65 — the level the speaker
    really reached — while ``sink_percent`` keeps the sink's own 60, and the
    command that ran still asked for 60%.
    """
    await store.set_setting("audio.hardware_volume", 60)
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=43, write_percent=65)})
    spawner = RecordingSpawner(pulse)

    status = await _service(store, spawner, backend).apply()

    assert status.available is True
    assert status.percent == 65
    assert status.applied == 65
    assert status.sink_percent == 60
    assert status.muted is False
    assert status.control == "alsa_output.usb-card"
    assert spawner.argv_matching("pactl", "set-sink-volume") == [
        ("pactl", "set-sink-volume", "alsa_output.usb-card", "60%")
    ]
    assert await store.get_setting("audio.hardware_volume") == 60
    assert pulse.mixer is not None
    assert pulse.mixer.writes == []


async def test_a_pulse_sink_whose_card_cannot_be_resolved_never_reports_the_sink_level(
    store: ConfigStore,
) -> None:
    """A sink with no ALSA properties is a sentence, not the sink's own 60% dressed as one."""
    await store.set_setting("audio.hardware_volume", 60)
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    service = _service(store, RecordingSpawner(FakePulse(percent=60)), backend)

    applied_status = await service.apply()
    read_status = await service.read()

    for status in (applied_status, read_status):
        assert status.available is False
        assert status.percent is None
        assert status.applied is None
        assert status.sink_percent == 60
        assert status.percent != 60
        assert "alsa_output.usb-card" in status.reason
        assert "no ALSA card" in status.reason


async def test_an_unresolvable_pulse_sink_is_still_written_but_not_confirmed(
    store: ConfigStore,
) -> None:
    """An unmappable sink is still set: failing to confirm the card is not failing to set the sink.

    The status reports the level as unknown, so a write that did happen is never
    silently dropped just because the deeper ALSA read cannot be confirmed.
    """
    await store.set_setting("audio.hardware_volume", 60)
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = FakePulse(percent=40)
    spawner = RecordingSpawner(pulse)

    status = await _service(store, spawner, backend).apply()

    assert pulse.writes == [
        ("set-sink-volume", "alsa_output.usb-card", "60%"),
        ("set-sink-mute", "alsa_output.usb-card", "0"),
    ]
    assert spawner.argv_matching("pactl", "set-sink-volume") == [
        ("pactl", "set-sink-volume", "alsa_output.usb-card", "60%")
    ]
    assert status.available is False
    assert status.percent is None
    assert status.applied is None
    assert status.sink_percent == 60
    assert "alsa_output.usb-card" in status.reason
    assert await store.get_setting("audio.hardware_volume") == 60


async def test_a_pulse_target_already_at_the_achieved_level_is_not_written_again(
    store: ConfigStore,
) -> None:
    """The sink holds the request, so the second apply writes nothing and invents no ``applied``."""
    await store.set_setting("audio.hardware_volume", 60)
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=43, write_percent=65)})
    service = _service(store, RecordingSpawner(pulse), backend)

    await service.apply()
    second = await service.apply()

    assert pulse.writes == [
        ("set-sink-volume", "alsa_output.usb-card", "60%"),
        ("set-sink-mute", "alsa_output.usb-card", "0"),
    ]
    assert second.percent == 65
    assert second.applied is None


async def test_the_card_id_is_tried_before_the_numeric_index(store: ConfigStore) -> None:
    """ALSA indices reorder across boots, so the id is offered first and the index second."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=43)})
    pulse.mixer = FakeMixer({"Speaker": FakeControl(percent=43)}, valid_cards=frozenset({"1"}))
    spawner = RecordingSpawner(pulse)

    status = await _service(store, spawner, backend).read()

    assert spawner.argv_matching("amixer", "scontrols") == [
        ("amixer", "-c", "Device", "scontrols"),
        ("amixer", "-c", "1", "scontrols"),
    ]
    assert status.available is True
    assert status.percent == 43


async def test_a_card_that_reports_no_playback_control_is_an_answer(store: ConfigStore) -> None:
    """The HDMI case behind a sink: switch-only card, ``available: false`` with a reason."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(60, {"IEC958": FakeControl(percent=0, has_volume=False)})

    status = await _service(store, RecordingSpawner(pulse), backend).read()

    assert status.available is False
    assert status.percent is None
    assert status.sink_percent == 60
    assert status.reason.startswith(NO_HARDWARE_CONTROL_TEXT)
    assert "alsa_output.usb-card" in status.reason


async def test_a_card_amixer_cannot_list_is_reported_with_its_own_words(
    store: ConfigStore,
) -> None:
    """A card pactl names but ALSA cannot attach is quoted, not guessed at."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = FakePulse(percent=60, alsa_card="1")

    def _handler(argv: tuple[str, ...]) -> FakeCommand:
        if argv[0] == "amixer":
            return FakeCommand(
                returncode=1, stderr="amixer: Mixer attach hw:1 error: No such device"
            )
        return pulse(argv)

    status = await _service(store, RecordingSpawner(_handler), backend).read()

    assert status.available is False
    assert status.percent is None
    assert status.sink_percent == 60
    assert "amixer scontrols failed" in status.reason
    assert "No such device" in status.reason
    assert "alsa_output.usb-card" in status.reason


async def test_a_stored_control_that_matches_nothing_behind_the_sink_is_reported(
    store: ConfigStore,
) -> None:
    """A wrong manual control name is visible, and says it was the card behind the sink."""
    await store.set_setting("audio.hardware_control", "Nope")
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=43)})

    status = await _service(store, RecordingSpawner(pulse), backend).read()

    assert status.available is False
    assert status.reason.startswith("no control matching 'Nope'")
    assert "alsa_output.usb-card" in status.reason


async def test_a_pulse_sink_that_pactl_does_not_list_is_reported(store: ConfigStore) -> None:
    """A sink pactl will not list has no discoverable card, and the status says so."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = FakePulse(percent=40, listed=False)

    status = await _service(store, RecordingSpawner(pulse), backend).read()

    assert status.available is False
    assert status.percent is None
    assert status.sink_percent == 40
    assert "alsa_output.usb-card" in status.reason
    assert "not listed by pactl" in status.reason


async def test_the_device_mpv_would_default_to_is_used_when_the_hint_is_blank(
    store: ConfigStore,
) -> None:
    """No hint means the default output, so the mixer follows what mpv will play."""
    backend = _Backend(
        [
            AudioDevice(name="alsa/hw:CARD=Other,DEV=0", hostapi="alsa"),
            AudioDevice(name=_USB_DEVICE, hostapi="alsa", is_default=True),
        ]
    )
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})

    status = await _service(store, RecordingSpawner(mixer), backend).read()

    assert status.device == _USB_DEVICE
    assert mixer.writes == []


async def test_the_stored_hint_selects_the_device(store: ConfigStore) -> None:
    """A stored ``audio.device_hint`` is resolved with the same matcher playback uses."""
    backend = _Backend(
        [
            AudioDevice(name="alsa/hw:CARD=Other,DEV=0", hostapi="alsa", is_default=True),
            AudioDevice(name=_USB_DEVICE, hostapi="alsa"),
        ]
    )
    await store.set_setting("audio.device_hint", "Device")
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(mixer)

    status = await _service(store, spawner, backend).read()

    assert status.device == _USB_DEVICE
    assert spawner.calls[0] == ("amixer", "-c", "Device", "scontrols")


async def test_the_stored_control_overrides_the_preference_order(store: ConfigStore) -> None:
    """``audio.hardware_control`` decides when the card exposes several controls."""
    await store.set_setting("audio.hardware_control", "Auto Gain Control")
    mixer = FakeMixer(
        {
            "Speaker": FakeControl(percent=100),
            "Mic": FakeControl(percent=50),
            "Auto Gain Control": FakeControl(percent=30),
        }
    )

    status = await _service(store, RecordingSpawner(mixer)).read()

    assert status.control == "Auto Gain Control"
    assert status.percent == 30


async def test_an_unmatched_stored_control_is_reported_by_name(store: ConfigStore) -> None:
    """A manual name that matches nothing is visible instead of silently ineffective."""
    await store.set_setting("audio.hardware_control", "NoSuchControl")
    mixer = FakeMixer({"Speaker": FakeControl(percent=100)})

    status = await _service(store, RecordingSpawner(mixer)).read()

    assert status.available is False
    assert "no control matching 'NoSuchControl'" in status.reason


async def test_a_switch_only_output_reports_no_hardware_volume_control(
    store: ConfigStore,
) -> None:
    """The HDMI case: an ``IEC958`` switch has no volume, and the app says so."""
    mixer = FakeMixer({"IEC958": FakeControl(percent=0, has_volume=False)})

    status = await _service(store, RecordingSpawner(mixer)).read()

    assert status.available is False
    assert status.percent is None
    assert status.control is None
    assert status.reason == NO_HARDWARE_CONTROL_TEXT


async def test_a_matching_value_is_not_written_again(store: ConfigStore) -> None:
    """An already correct device is left alone, so a boot causes no click or churn."""
    mixer = FakeMixer({"Speaker": FakeControl(percent=100)})
    service = _service(store, RecordingSpawner(mixer))

    status = await service.apply()

    assert mixer.writes == []
    assert status.available is True
    assert status.percent == 100
    assert status.applied is None


async def test_a_different_value_is_written_with_unmute(store: ConfigStore) -> None:
    """The measured bug: a DAC at 43% is brought to the stored 100 in one command."""
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(mixer)

    status = await _service(store, spawner).apply()

    assert ("amixer", "-c", "Device", "sset", "Speaker", "100%", "unmute") in spawner.calls
    assert mixer.writes == [("Speaker", 100)]
    assert status.available is True
    assert status.percent == 100
    assert status.applied == 100


async def test_applied_comes_from_the_read_back_not_from_the_request(
    store: ConfigStore,
) -> None:
    """``applied`` reports the device's own value, even when it is not what was asked.

    The fake answers 37% after a successful ``sset 100%``. The panel must show 37,
    because that is what the device says; reporting the request would recreate
    the lie this ticket exists to remove.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=43, write_percent=37)})

    status = await _service(store, RecordingSpawner(mixer)).apply()

    assert mixer.writes == [("Speaker", 100)]
    assert status.percent == 37
    assert status.applied == 37


async def test_a_quantising_card_reports_the_level_it_reached(store: ConfigStore) -> None:
    """The panel's C-Media card turns 60 into 65, and both numbers are reported honestly.

    ``percent`` and ``applied`` are the device's own answer (65) while the command
    that was run still asked for the stored value (60%): nothing snaps or rounds
    the request on its way to ``amixer``.
    """
    await store.set_setting("audio.hardware_volume", 60)
    mixer = FakeMixer({"Speaker": FakeControl(percent=43, write_percent=65)})
    spawner = RecordingSpawner(mixer)

    status = await _service(store, spawner).apply()

    assert status.available is True
    assert status.percent == 65
    assert status.applied == 65
    assert spawner.argv_matching("amixer", "sset") == [
        ("amixer", "-c", "Device", "sset", "Speaker", "60%", "unmute")
    ]
    assert not any("65%" in call for call in spawner.calls)


async def test_the_stored_request_survives_a_quantised_write(store: ConfigStore) -> None:
    """The read-back never overwrites the stored request, even when it differs."""
    await store.set_setting("audio.hardware_volume", 63)
    mixer = FakeMixer({"Speaker": FakeControl(percent=43, write_percent=65)})

    status = await _service(store, RecordingSpawner(mixer)).apply()

    assert await store.get_setting("audio.hardware_volume") == 63
    assert status.percent == 65
    assert status.applied == 65


async def test_a_repeat_apply_at_the_reached_level_is_not_written_again(
    store: ConfigStore,
) -> None:
    """A coarse card is written once, not on every apply that follows.

    The read-back (65) never equals the request (60), so a guard that compared
    the two alone re-issued ``sset`` on every settings change, device change and
    test tone.
    """
    await store.set_setting("audio.hardware_volume", 60)
    mixer = FakeMixer({"Speaker": FakeControl(percent=43, write_percent=65)})
    service = _service(store, RecordingSpawner(mixer))

    await service.apply()
    second = await service.apply()

    assert mixer.writes == [("Speaker", 60)]
    assert second.percent == 65
    assert second.applied is None


async def test_a_device_that_drifted_off_the_reached_level_is_written_again(
    store: ConfigStore,
) -> None:
    """The other direction: a control that moved on its own is still corrected."""
    await store.set_setting("audio.hardware_volume", 60)
    mixer = FakeMixer({"Speaker": FakeControl(percent=43, write_percent=65)})
    service = _service(store, RecordingSpawner(mixer))

    await service.apply()
    # The OS moved the control behind the service's back; 40 is neither the
    # request nor the level this card reaches for it.
    mixer.controls["Speaker"].percent = 40
    drifted = await service.apply()

    assert mixer.writes == [("Speaker", 60), ("Speaker", 60)]
    assert drifted.percent == 65
    assert drifted.applied == 65


async def test_a_changed_request_is_written_again_after_a_quantised_write(
    store: ConfigStore,
) -> None:
    """Reached levels are remembered per request, so 70 is not mistaken for 60.

    60 lands on 65 and 70 lands on 70. Asking for 60 again while the device sits
    at 70 must write: 70 is not the level 60 reaches. A memo keyed only by target
    would skip that write and leave the device above the request.
    """
    await store.set_setting("audio.hardware_volume", 60)
    control = FakeControl(percent=43, write_percent=65)
    mixer = FakeMixer({"Speaker": control})
    service = _service(store, RecordingSpawner(mixer))

    await service.apply()
    await store.set_setting("audio.hardware_volume", 70)
    control.write_percent = None  # this card can land on 70 exactly
    await service.apply()
    await store.set_setting("audio.hardware_volume", 60)
    control.write_percent = 65
    third = await service.apply()

    assert mixer.writes == [("Speaker", 60), ("Speaker", 70), ("Speaker", 60)]
    assert third.percent == 65


async def test_a_failed_read_back_is_not_reported_as_the_level_before_the_write(
    store: ConfigStore,
) -> None:
    """A successful ``sset`` whose ``sget`` fails fabricates no percentage.

    The pre-write reading is not a fact about the device any more once the write
    landed, so the status has no ``percent`` at all and says why.
    """
    await store.set_setting("audio.hardware_volume", 60)
    written: list[bool] = []

    def _handler(argv: tuple[str, ...]) -> FakeCommand:
        if "scontrols" in argv:
            return FakeCommand(stdout="Simple mixer control 'Speaker',0\n")
        if "sset" in argv:
            written.append(True)
            return FakeCommand()
        if written:
            return FakeCommand(returncode=1, stderr="amixer: Mixer attach default error")
        return FakeCommand(stdout="  Front Left: Playback 43 [43%] [-20.00dB] [on]\n")

    status = await _service(store, RecordingSpawner(_handler)).apply()

    assert status.available is False
    assert status.percent is None
    assert status.applied is None
    assert "amixer sget Speaker failed" in status.reason


async def test_a_failed_read_back_on_a_pulse_sink_is_not_the_pre_write_level(
    store: ConfigStore,
) -> None:
    """The Pulse twin: a ``pactl`` read after the write fails and is reported."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    written: list[bool] = []

    def _handler(argv: tuple[str, ...]) -> FakeCommand:
        command = argv[1]
        if command == "set-sink-volume":
            written.append(True)
            return FakeCommand()
        if command == "set-sink-mute":
            return FakeCommand()
        if command == "get-sink-mute":
            return FakeCommand(stdout="Mute: no\n")
        if written:
            return FakeCommand(returncode=1, stderr="pactl: Connection failure")
        return FakeCommand(stdout="Volume: front-left: 26214 / 40% / -23.81 dB\n")

    status = await _service(store, RecordingSpawner(_handler), backend).apply()

    assert status.available is False
    assert status.percent is None
    assert status.applied is None
    assert "pactl get-sink-volume alsa_output.usb-card failed" in status.reason


async def test_a_muted_control_is_unmuted_by_the_same_write(store: ConfigStore) -> None:
    """Being at the right percentage but muted still needs one ``sset ... unmute``."""
    mixer = FakeMixer({"Speaker": FakeControl(percent=100, muted=True)})

    status = await _service(store, RecordingSpawner(mixer)).apply()

    assert mixer.writes == [("Speaker", 100)]
    assert status.muted is False
    assert status.applied == 100


async def test_mute_targets_zero_and_unmute_restores_the_stored_value(
    store: ConfigStore,
) -> None:
    """Mute silences both stages without destroying the value the user chose."""
    await store.set_setting("audio.hardware_volume", 40)
    mixer = FakeMixer({"Speaker": FakeControl(percent=40)})
    service = _service(store, RecordingSpawner(mixer))

    await store.set_setting("audio.muted", True)
    muted = await service.apply()
    assert mixer.writes == [("Speaker", 0)]
    assert muted.percent == 0

    await store.set_setting("audio.muted", False)
    unmuted = await service.apply()
    assert mixer.writes == [("Speaker", 0), ("Speaker", 40)]
    assert unmuted.percent == 40
    assert unmuted.applied == 40


async def test_a_pulse_write_sets_the_volume_then_unmutes(store: ConfigStore) -> None:
    """The sink is raised and unmuted by two bounded ``pactl`` commands."""
    backend = _Backend([AudioDevice(name=_PULSE_DEVICE, hostapi="pulse", is_default=True)])
    pulse = pulse_with_card(40, {"Speaker": FakeControl(percent=100)}, muted=True)
    spawner = RecordingSpawner(pulse)

    status = await _service(store, spawner, backend).apply()

    assert pulse.writes == [
        ("set-sink-volume", "alsa_output.usb-card", "100%"),
        ("set-sink-mute", "alsa_output.usb-card", "0"),
    ]
    assert status.percent == 100
    assert status.applied == 100
    assert status.sink_percent == 100


async def test_a_missing_utility_is_reported_as_not_installed(store: ConfigStore) -> None:
    """A host without ``amixer`` says so instead of pretending or raising."""

    def _missing(_argv: tuple[str, ...]) -> FakeCommand:
        raise FileNotFoundError("amixer")

    status = await _service(store, RecordingSpawner(_missing)).read()

    assert status.available is False
    assert status.reason == "the amixer utility is not installed"


async def test_a_mixer_that_does_not_answer_is_reported_as_a_timeout(
    store: ConfigStore,
) -> None:
    """A wedged ``amixer`` is killed and reported with the bound, never raised."""
    spawner = RecordingSpawner(lambda _argv: FakeCommand(hang=True))

    status = await _service(store, spawner).read()

    assert status.available is False
    assert "did not answer within" in status.reason
    assert spawner.processes[0].killed is True


async def test_a_non_zero_exit_carries_the_utilitys_own_words(store: ConfigStore) -> None:
    """A failing ``amixer`` is quoted, so the user can see what it objected to."""

    def _failure(_argv: tuple[str, ...]) -> FakeCommand:
        return FakeCommand(returncode=1, stderr="amixer: Mixer attach default error")

    status = await _service(store, RecordingSpawner(_failure)).read()

    assert status.available is False
    assert "amixer scontrols failed" in status.reason
    assert "Mixer attach default error" in status.reason


async def test_an_unparseable_value_is_reported(store: ConfigStore) -> None:
    """A control that prints no percentage is not guessed at."""

    def _handler(argv: tuple[str, ...]) -> FakeCommand:
        if "scontrols" in argv:
            return FakeCommand(stdout="Simple mixer control 'Speaker',0\n")
        return FakeCommand(stdout="Simple mixer control 'Speaker',0\n  Capabilities: pvolume\n")

    spawner = RecordingSpawner(_handler)

    status = await _service(store, spawner).read()

    assert status.available is False
    assert "reports no volume percentage" in status.reason


async def test_a_backend_that_has_not_started_says_so(store: ConfigStore) -> None:
    """No pipeline backend is an honest sentence, not a silent success."""
    spawner = RecordingSpawner()
    service = HardwareVolume(store=store, backend=lambda: None, spawner=spawner)

    status = await service.read()

    assert status.available is False
    assert status.reason == HARDWARE_NOT_STARTED_TEXT
    assert spawner.calls == []


async def test_a_device_list_that_fails_is_reported(store: ConfigStore) -> None:
    """A backend that cannot list outputs fails the lookup, not the caller."""
    backend = _Backend(list_error=RuntimeError("libmpv is not loaded"))

    status = await _service(store, RecordingSpawner(), backend).read()

    assert status.available is False
    assert "the audio outputs could not be listed" in status.reason
    assert "libmpv is not loaded" in status.reason


async def test_a_store_that_cannot_be_read_fails_soft(store: ConfigStore) -> None:
    """A database outage is a sentence, never an exception out of ``read``."""
    exploding = cast(ConfigStore, _UnreadableStore())

    status = await HardwareVolume(
        store=exploding, backend=lambda: _Backend(), spawner=RecordingSpawner()
    ).read()

    assert status.available is False
    assert "could not be read" in status.reason


async def test_the_status_property_remembers_the_last_verdict(store: ConfigStore) -> None:
    """The API can show the last read without asking the mixer again."""
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    service = _service(store, RecordingSpawner(mixer))
    before = service.status
    assert before is None

    status = await service.read()

    after = service.status
    assert after is not None
    assert after.percent == status.percent
