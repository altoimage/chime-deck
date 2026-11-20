"""Offline tests for the libmpv audio backend and its device helpers.

Every libmpv call is faked: ``MpvBackend`` receives a ``player_factory`` whose
player records the name and thread id of each call, so the tests can prove the
single-thread invariant, drive the start-file/end-file events by hand, and check
that a wedged command cannot kill the worker thread. No test here needs libmpv,
a sound card or a network.

All awaits are bounded by :func:`_run` (2 s), and the one test that does load
libmpv is both ``integration``-marked and skipped unless an operator opts in.

Owning ticket: HSD-h13q.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import threading
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TypeVar

import pytest

from chime_deck.audio import (
    SYSTEM_DEFAULT_DEVICE,
    AudioBackend,
    AudioBackendNotStarted,
    AudioBackendUnavailable,
    AudioDevice,
    AudioOperationError,
    AudioPermissionError,
    MissingAudioFileError,
    MpvBackend,
    MpvEvent,
    MpvEventHandler,
    MpvPlayer,
    NoAudioDeviceError,
    PlaybackError,
    PlaybackInProgressError,
    UnsupportedFormatError,
    hostapi_of,
    resolve_device,
    warm_library,
)
from chime_deck.audio.mpv_backend import (
    _BASE_MPV_OPTIONS,
    _DEFAULT_COMMAND_TIMEOUT_S,
    _DEFAULT_PLAY_TIMEOUT_S,
    _END_FILE_ABORTED,
    _END_FILE_EOF,
    _END_FILE_ERROR,
    _END_FILE_QUIT,
    _END_FILE_REDIRECT,
    _END_FILE_RESTARTED,
    _FORBIDDEN_MPV_OPTIONS,
    _MAX_LEAD_IN_MS,
    _clamp_lead_in,
    _lead_in_filter,
)

pytestmark = pytest.mark.asyncio

# mypy-strict conformance pin: MpvBackend must keep satisfying the design 03 §1.1
# protocol HSD-kfnd will program against, so signature drift fails here first.
_CONFORMANCE: AudioBackend = MpvBackend()

_T = TypeVar("_T")

_AWAIT_TIMEOUT_S = 2.0
"""Bound for every ``await`` in this file."""

_USB_NAME = "pipewire/alsa_output.usb-Generic_USB_Audio-00.analog-stereo"
_BUILTIN_NAME = "alsa/hw:CARD=PCH,DEV=0"
_AUTO_NAME = SYSTEM_DEFAULT_DEVICE

_USB_DEVICE: Mapping[str, object] = {"name": _USB_NAME, "description": "USB Audio Device"}
_BUILTIN_DEVICE: Mapping[str, object] = {
    "name": _BUILTIN_NAME,
    "description": "Built-in Audio Analog",
}
_AUTO_DEVICE: Mapping[str, object] = {"name": _AUTO_NAME, "description": "Autoselect device"}


async def _run(awaitable: Awaitable[_T]) -> _T:
    """Await *awaitable* under the file's fixed bound.

    :param awaitable: coroutine or future to await.
    :return: whatever *awaitable* produced.
    """
    return await asyncio.wait_for(awaitable, timeout=_AWAIT_TIMEOUT_S)


def _playing(backend: MpvBackend) -> bool:
    """Return the backend's playing flag.

    Reading it through a call keeps mypy from narrowing the property type and
    then calling the rest of a test unreachable.

    :param backend: backend under test.
    :return: the backend's current ``playing`` value.
    """
    return backend.playing


async def _settle() -> None:
    """Let the loop run the thread-safe callbacks the backend posted."""
    await asyncio.sleep(0.02)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Poll *predicate* until it is true, with a fixed attempt cap.

    :param predicate: condition that may be satisfied by another thread.
    """
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within 2 s")


@asynccontextmanager
async def _running(backend: MpvBackend) -> AsyncIterator[MpvBackend]:
    """Start *backend* for a test body and always stop it again.

    :param backend: backend under test.
    :yields: the started backend.
    """
    await _run(backend.start())
    try:
        yield backend
    finally:
        await _run(backend.stop())


@dataclass(frozen=True, slots=True)
class _FakeEventData:
    """Minimal stand-in for python-mpv's event payload."""

    playlist_entry_id: int = 0
    reason: int = _END_FILE_EOF
    error: int = 0


@dataclass(frozen=True, slots=True)
class _FakeEvent:
    """Minimal stand-in for a python-mpv event."""

    data: _FakeEventData | None = None


class _FakeHandler:
    """Callable mpv event handler carrying python-mpv's unregister hook."""

    def __init__(self, handler: MpvEventHandler, on_unregister: Callable[[], None]) -> None:
        """Wrap *handler* and record an unregister call.

        :param handler: backend handler this stands in for.
        :param on_unregister: recorder run when python-mpv unregisters us.
        """
        self._handler = handler
        self.unregister_mpv_events: Callable[[], None] = on_unregister

    def __call__(self, event: MpvEvent) -> None:
        """Forward the event to the wrapped handler.

        :param event: event python-mpv delivered.
        """
        self._handler(event)


class _FakeMpvPlayer:
    """Stand-in for ``mpv.MPV`` that records every call and its thread.

    Three event modes: ``auto`` fires start-file then end-file inline from the
    calling (worker) thread, ``manual`` lets the test drive the events, and
    ``never`` produces no event at all so the play deadline can be exercised.
    """

    def __init__(
        self,
        *,
        devices: Sequence[Mapping[str, object]] | None = None,
        mode: str = "auto",
        reason: int = _END_FILE_EOF,
        error: int = 0,
    ) -> None:
        """Configure the fake's reported devices and end-file behaviour.

        :param devices: entries for ``audio_device_list``; defaults to a USB and a
            built-in output.
        :param mode: ``auto``, ``manual`` or ``never``.
        :param reason: end-file reason ``auto`` mode reports.
        :param error: mpv error code ``auto`` mode reports.
        """
        self.calls: list[tuple[str, int]] = []
        self.device_writes: list[str] = []
        self.volume_writes: list[float] = []
        self.af_writes: list[str] = []
        self.played: list[str] = []
        self.terminates = 0
        self.mode = mode
        self.reason = reason
        self.error = error
        self.entry_id = 0
        self.handlers: dict[str, MpvEventHandler] = {}
        self.worker_thread: int | None = None
        self.options: dict[str, object] = {}
        self.block: threading.Event | None = None
        self.failures: dict[str, BaseException] = {}
        self._devices: tuple[Mapping[str, object], ...] = (
            tuple(devices) if devices is not None else (_USB_DEVICE, _BUILTIN_DEVICE)
        )
        self._audio_device = ""
        self._volume = 100.0
        self._af = ""

    def record(self, name: str) -> None:
        """Record one call on the current thread, raising a configured failure.

        :param name: call name used for assertions and failure injection.
        """
        self.calls.append((name, threading.get_ident()))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def names(self) -> list[str]:
        """Return the recorded call names in order."""
        return [name for name, _ in self.calls]

    @property
    def audio_device_list(self) -> Sequence[Mapping[str, object]]:
        """Return the configured device entries."""
        self.record("audio_device_list")
        return self._devices

    @property
    def audio_device(self) -> str:
        """Return the last device name written."""
        return self._audio_device

    @audio_device.setter
    def audio_device(self, name: str) -> None:
        """Record a device name written by the backend."""
        self.record("audio_device=")
        self._audio_device = name
        self.device_writes.append(name)

    @property
    def volume(self) -> float:
        """Return the last volume written."""
        return self._volume

    @volume.setter
    def volume(self, level: float) -> None:
        """Record a volume written by the backend."""
        self.record("volume=")
        self._volume = level
        self.volume_writes.append(level)

    @property
    def af(self) -> str:
        """Return the audio filter chain last written."""
        return self._af

    @af.setter
    def af(self, graph: str) -> None:
        """Record an audio filter chain written by the backend."""
        self.record("af=")
        self._af = graph
        self.af_writes.append(graph)

    def event_callback(self, *event_types: str) -> Callable[[MpvEventHandler], MpvEventHandler]:
        """Return a decorator storing a handler per requested event name."""
        self.record("event_callback")

        def register(handler: MpvEventHandler) -> MpvEventHandler:
            wrapper = _FakeHandler(handler, lambda: self.record("unregister"))
            for event_type in event_types:
                self.handlers[event_type] = wrapper
            return wrapper

        return register

    def play(self, filename: str) -> None:
        """Record the file, optionally block, then fire events per the mode."""
        self.record("play")
        self.played.append(filename)
        if self.block is not None:
            self.block.wait(timeout=_AWAIT_TIMEOUT_S)
        self.entry_id += 1
        if self.mode == "auto":
            self.fire_start_file(self.entry_id)
            self.fire_end_file(self.entry_id, self.reason, self.error)

    def stop(self) -> None:
        """Record the stop request."""
        self.record("stop")

    def terminate(self) -> None:
        """Record the one permitted terminate, refusing to run off the worker."""
        self.record("terminate")
        assert self.worker_thread == threading.get_ident(), "terminate() ran off the mpv thread"
        self.terminates += 1

    def fire_start_file(self, entry_id: int) -> None:
        """Deliver a start-file event for *entry_id*.

        :param entry_id: playlist entry the event belongs to.
        """
        self.handlers["start-file"](_FakeEvent(data=_FakeEventData(playlist_entry_id=entry_id)))

    def fire_end_file(self, entry_id: int, reason: int = _END_FILE_EOF, error: int = 0) -> None:
        """Deliver an end-file event.

        :param entry_id: playlist entry the event belongs to.
        :param reason: end-file reason code.
        :param error: mpv error code.
        """
        self.handlers["end-file"](
            _FakeEvent(data=_FakeEventData(playlist_entry_id=entry_id, reason=reason, error=error))
        )

    def fire_empty_end_file(self) -> None:
        """Deliver an end-file event without a payload."""
        self.handlers["end-file"](_FakeEvent(data=None))


def _factory(fake: _FakeMpvPlayer) -> Callable[[Mapping[str, object]], MpvPlayer]:
    """Return a player factory that records the construction and its thread.

    :param fake: player handed to the backend.
    :return: factory accepted by :class:`MpvBackend`.
    """

    def build(options: Mapping[str, object]) -> MpvPlayer:
        fake.record("MPV()")
        fake.worker_thread = threading.get_ident()
        fake.options = dict(options)
        return fake

    return build


def _backend(
    fake: _FakeMpvPlayer,
    *,
    device_hint: str = "",
    mpv_options: Mapping[str, object] | None = None,
    play_timeout: float = _DEFAULT_PLAY_TIMEOUT_S,
    command_timeout: float = _DEFAULT_COMMAND_TIMEOUT_S,
) -> MpvBackend:
    """Build a backend wired to *fake*.

    :param fake: player handed to the backend.
    :param device_hint: name substring from ``audio.device_hint``.
    :param mpv_options: extra mpv options for the factory.
    :param play_timeout: lost end-file backstop, in seconds.
    :param command_timeout: bound for one libmpv command, in seconds.
    :return: a backend that is not started yet.
    """
    return MpvBackend(
        player_factory=_factory(fake),
        device_hint=device_hint,
        mpv_options=mpv_options,
        play_timeout=play_timeout,
        command_timeout=command_timeout,
    )


def _chime(tmp_path: Path) -> Path:
    """Create a small file that satisfies the pre-play checks.

    :param tmp_path: pytest temporary directory.
    :return: path to the created file.
    """
    path = tmp_path / "chime.wav"
    path.write_bytes(b"RIFF0000WAVEfmt ")
    return path


async def test_resolve_device_matches_the_first_name_substring() -> None:
    """A hint picks the first device whose name contains it, case-insensitively."""
    devices = [
        AudioDevice(name=_BUILTIN_NAME, hostapi="alsa"),
        AudioDevice(name=_USB_NAME, hostapi="pipewire"),
        AudioDevice(name="pulse/alsa_output.usb-extra", hostapi="pulse"),
    ]
    assert resolve_device(devices, "usb") is devices[1]
    assert resolve_device(devices, "USB-EXTRA") is devices[2]
    assert resolve_device(devices, "GENERIC") is devices[1]


async def test_resolve_device_rejects_blank_and_unmatched_hints() -> None:
    """A blank hint matches nothing, and so does a hint no name contains."""
    devices = [AudioDevice(name=_USB_NAME, hostapi="pipewire")]
    assert resolve_device(devices, "") is None
    assert resolve_device(devices, "   ") is None
    assert resolve_device(devices, "HDMI") is None
    assert resolve_device([], "usb") is None


async def test_hostapi_is_the_name_prefix_lowercased() -> None:
    """The host API label comes from the text before the first slash."""
    assert hostapi_of("pipewire/alsa_output.pci") == "pipewire"
    assert hostapi_of("pipewire") == "pipewire"
    assert hostapi_of("auto") == "auto"
    assert hostapi_of(" ALSA/hw:1 ") == "alsa"


async def test_list_devices_maps_names_and_flags_the_first_as_default() -> None:
    """Device names are mapped with their host API; the first entry is the default."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        devices = await _run(backend.list_devices())
    assert [(device.name, device.hostapi) for device in devices] == [
        (_USB_NAME, "pipewire"),
        (_BUILTIN_NAME, "alsa"),
    ]
    assert [device.is_default for device in devices] == [True, False]


async def test_list_devices_prefers_the_auto_sentinel_as_default() -> None:
    """When mpv reports an ``auto`` output, that entry is the flagged default."""
    fake = _FakeMpvPlayer(devices=(_BUILTIN_DEVICE, _AUTO_DEVICE))
    backend = _backend(fake)
    async with _running(backend):
        devices = await _run(backend.list_devices())
    assert [device.is_default for device in devices] == [False, True]


async def test_list_devices_skips_entries_without_a_usable_name() -> None:
    """Entries with a blank or non-string name are dropped."""
    fake = _FakeMpvPlayer(
        devices=({"name": ""}, {"description": "no name"}, {"name": _BUILTIN_NAME})
    )
    backend = _backend(fake)
    async with _running(backend):
        devices = await _run(backend.list_devices())
    assert [device.name for device in devices] == [_BUILTIN_NAME]


async def test_set_device_writes_the_resolved_name() -> None:
    """A matching hint is written to the player as mpv's device name."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        await _run(backend.set_device("USB"))
    assert fake.device_writes == [_USB_NAME]
    assert fake.audio_device == _USB_NAME


async def test_set_device_falls_back_to_auto_for_a_blank_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blank hint warns once and selects the system default.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        caplog.clear()
        await _run(backend.set_device("   "))
    assert "no audio.device_hint is set" in caplog.text
    assert fake.device_writes == [SYSTEM_DEFAULT_DEVICE]


async def test_set_device_warns_and_falls_back_when_nothing_matches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unmatched hint warns with the hint and selects the system default.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        caplog.clear()
        await _run(backend.set_device("HDMI"))
    assert "no audio device name matches 'HDMI'" in caplog.text
    assert fake.device_writes == [SYSTEM_DEFAULT_DEVICE]


async def test_set_device_raises_when_the_host_reports_no_device() -> None:
    """The settings-page path surfaces a host without any audio output."""
    fake = _FakeMpvPlayer(devices=())
    backend = _backend(fake)
    async with _running(backend):
        with pytest.raises(NoAudioDeviceError):
            await _run(backend.set_device("USB"))
    assert fake.device_writes == []


async def test_start_survives_a_host_with_no_audio_device(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Start logs the missing device and leaves mpv on its own default.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer(devices=())
    backend = _backend(fake)
    async with _running(backend):
        pass
    assert "no audio output device was found" in caplog.text
    assert fake.device_writes == []


async def test_start_without_a_hint_warns_once_and_writes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fresh instance warns about the missing hint but not about a mismatch.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        pass
    assert "no audio.device_hint is set" in caplog.text
    assert "no audio device name matches" not in caplog.text
    assert fake.device_writes == []


async def test_device_hint_is_applied_again_after_a_restart() -> None:
    """A stop/start cycle re-applies the stored hint instead of mpv's default."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake, device_hint="USB")
    await _run(backend.start())
    assert fake.device_writes == [_USB_NAME]
    await _run(backend.stop())
    await _run(backend.start())
    try:
        assert fake.device_writes == [_USB_NAME, _USB_NAME]
    finally:
        await _run(backend.stop())


async def test_play_rejects_a_missing_file_before_touching_mpv(tmp_path: Path) -> None:
    """A missing file fails before any libmpv command is issued.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        fake.calls.clear()
        with pytest.raises(MissingAudioFileError) as raised:
            await _run(backend.play(tmp_path / "nope.wav", volume=50))
        assert "audio file not found" in str(raised.value)
        assert fake.names() == []
    assert _playing(backend) is False


async def test_play_rejects_an_unreadable_file_with_file_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable file is about the file's permissions, not the audio group.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patch helper.
    """
    monkeypatch.setattr("chime_deck.audio.mpv_backend.os.access", lambda path, mode: False)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        with pytest.raises(AudioPermissionError) as raised:
            await _run(backend.play(_chime(tmp_path), volume=50))
    message = str(raised.value)
    assert "permission" in message
    assert "`audio` group" not in message


async def test_play_maps_unsupported_formats(tmp_path: Path) -> None:
    """Codes -17 and -18 report the file and suggest converting it.

    :param tmp_path: pytest temporary directory.
    """
    path = _chime(tmp_path)
    for code in (-17, -18):
        fake = _FakeMpvPlayer(reason=_END_FILE_ERROR, error=code)
        backend = _backend(fake)
        async with _running(backend):
            with pytest.raises(UnsupportedFormatError) as raised:
                await _run(backend.play(path, volume=50))
        message = str(raised.value)
        assert str(path) in message
        assert "convert" in message
        assert _playing(backend) is False


async def test_play_maps_audio_output_init_failure_to_group_advice(tmp_path: Path) -> None:
    """Code -14 talks about the device and the service user's ``audio`` group.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(reason=_END_FILE_ERROR, error=-14)
    backend = _backend(fake)
    async with _running(backend):
        with pytest.raises(AudioPermissionError) as raised:
            await _run(backend.play(_chime(tmp_path), volume=50))
    message = str(raised.value)
    assert "-14" in message
    assert "`audio` group" in message


async def test_play_maps_other_errors_and_carries_the_code(tmp_path: Path) -> None:
    """A generic mpv error is a plain :class:`PlaybackError` with its code.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(reason=_END_FILE_ERROR, error=-13)
    backend = _backend(fake)
    async with _running(backend):
        with pytest.raises(PlaybackError) as raised:
            await _run(backend.play(_chime(tmp_path), volume=50))
    assert type(raised.value) is PlaybackError
    assert raised.value.code == -13
    assert "loading failed" in str(raised.value)


@pytest.mark.parametrize("reason", [_END_FILE_EOF, _END_FILE_ABORTED, _END_FILE_QUIT])
async def test_play_returns_normally_for_successful_reasons(tmp_path: Path, reason: int) -> None:
    """EOF, abort and shutdown all count as "the file has finished".

    :param tmp_path: pytest temporary directory.
    :param reason: end-file reason the fake reports.
    """
    fake = _FakeMpvPlayer(reason=reason)
    backend = _backend(fake)
    calls: list[str] = []
    async with _running(backend):
        await _run(
            backend.play(_chime(tmp_path), volume=50, when_done=lambda: calls.append("done"))
        )
    assert calls == ["done"]
    assert _playing(backend) is False


async def test_play_keeps_waiting_after_restart_and_redirect(tmp_path: Path) -> None:
    """Reasons 1 and 5 do not end playback; the real end-file still does.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    async with _running(backend):
        task = asyncio.create_task(backend.play(_chime(tmp_path), volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        entry = fake.entry_id
        fake.fire_start_file(entry)
        await _settle()
        for reason in (_END_FILE_RESTARTED, _END_FILE_REDIRECT):
            fake.fire_end_file(entry, reason)
            await _settle()
            assert not task.done()
            assert _playing(backend) is True
        fake.fire_end_file(entry, _END_FILE_EOF)
        await _run(task)
    assert _playing(backend) is False


async def test_play_treats_an_unknown_end_file_reason_as_finished(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown reason is logged and treated as the end of the file.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    async with _running(backend):
        task = asyncio.create_task(backend.play(_chime(tmp_path), volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_end_file(fake.entry_id, 99)
        await _run(task)
    assert "unknown end-file reason 99" in caplog.text


async def test_play_ignores_an_end_file_event_without_data(tmp_path: Path) -> None:
    """An end-file event with ``data=None`` is ignored, not raised on.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    async with _running(backend):
        task = asyncio.create_task(backend.play(_chime(tmp_path), volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_empty_end_file()
        await _settle()
        assert not task.done()
        assert _playing(backend) is True
        fake.fire_end_file(fake.entry_id, _END_FILE_EOF)
        await _run(task)


async def test_a_stale_end_file_does_not_truncate_the_next_playback(tmp_path: Path) -> None:
    """A late abort for the previous entry cannot end the file that replaced it.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        first = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_end_file(fake.entry_id, _END_FILE_EOF)
        await _run(first)

        second = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 2)
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_end_file(fake.entry_id - 1, _END_FILE_ABORTED)
        await _settle()
        assert not second.done()
        assert _playing(backend) is True
        fake.fire_end_file(fake.entry_id, _END_FILE_EOF)
        await _run(second)


async def test_a_deferred_abort_cannot_finish_the_next_playback(tmp_path: Path) -> None:
    """A stop's late ABORTED must not resolve a playback claimed before its own start.

    This is the ordering real libmpv produces: the abort of the stopped file is
    delivered after the next ``loadfile`` has already been issued, so the next
    playback is pending but has not seen its own ``start-file`` yet.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        first = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        fake.fire_start_file(1)
        await _settle()
        await _run(backend.stop_playback())
        await _run(first)

        second = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 2)
        fake.fire_end_file(1, _END_FILE_ABORTED)
        await _settle()
        assert not second.done()
        assert _playing(backend) is True

        fake.fire_start_file(2)
        fake.fire_end_file(2, _END_FILE_EOF)
        await _run(second)
    assert _playing(backend) is False


async def test_a_failed_plays_trailing_abort_cannot_finish_the_next_playback(
    tmp_path: Path,
) -> None:
    """The abort mpv emits after a failed file is not the next file's end.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        failing = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        fake.fire_start_file(1)
        await _settle()
        fake.fire_end_file(1, _END_FILE_ERROR, -13)
        with pytest.raises(PlaybackError):
            await _run(failing)

        second = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 2)
        fake.fire_end_file(1, _END_FILE_ABORTED)
        await _settle()
        assert not second.done()

        fake.fire_start_file(2)
        fake.fire_end_file(2, _END_FILE_EOF)
        await _run(second)


async def test_a_lost_end_file_raises_an_operation_error(tmp_path: Path) -> None:
    """A missing end-file event surfaces as an operation error, not a play error.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="never")
    backend = _backend(fake, play_timeout=0.1)
    async with _running(backend):
        with pytest.raises(AudioOperationError) as raised:
            await _run(backend.play(_chime(tmp_path), volume=50))
    assert raised.value.operation == "play"
    assert "no end-file event within 0.1 s" in str(raised.value)
    assert not isinstance(raised.value, PlaybackError)
    assert "stop" in fake.names()
    assert _playing(backend) is False


async def test_a_normal_clip_is_not_truncated(tmp_path: Path) -> None:
    """A clip that ends well inside the deadline completes normally.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake, play_timeout=1.0)
    async with _running(backend):
        await _run(backend.play(_chime(tmp_path), volume=50))
    assert _playing(backend) is False


async def test_cancelling_a_play_stops_the_player_and_clears_state(tmp_path: Path) -> None:
    """Cancellation stops the file and leaves the backend reusable.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="never")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        task = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _run(task)
        await _settle()
        assert "stop" in fake.names()
        assert _playing(backend) is False
        fake.mode = "auto"
        await _run(backend.play(path, volume=50))
    assert _playing(backend) is False


async def test_when_done_runs_once_on_success_and_once_on_failure(tmp_path: Path) -> None:
    """The completion callback fires exactly once either way.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    path = _chime(tmp_path)
    calls: list[str] = []
    async with _running(backend):
        await _run(backend.play(path, volume=50, when_done=lambda: calls.append("ok")))
        assert calls == ["ok"]
        fake.reason = _END_FILE_ERROR
        fake.error = -17
        with pytest.raises(UnsupportedFormatError):
            await _run(backend.play(path, volume=50, when_done=lambda: calls.append("failed")))
    assert calls == ["ok", "failed"]


async def test_when_done_exceptions_are_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising callback is logged and cannot break playback.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)

    def explode() -> None:
        raise RuntimeError("boom")

    async with _running(backend):
        await _run(backend.play(_chime(tmp_path), volume=50, when_done=explode))
    assert "when_done callback raised" in caplog.text


@pytest.mark.parametrize(("requested", "expected"), [(70, 70.0), (150, 100.0), (-5, 0.0)])
async def test_play_writes_mpvs_volume_percentage(
    tmp_path: Path, requested: int, expected: float, caplog: pytest.LogCaptureFixture
) -> None:
    """Volume is mpv's percentage, clamped to 0-100 and logged when changed.

    :param tmp_path: pytest temporary directory.
    :param requested: volume the caller asked for.
    :param expected: volume written to the player.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        await _run(backend.play(_chime(tmp_path), volume=requested))
    assert fake.volume_writes == [expected]
    if requested == expected:
        assert "outside 0-100" not in caplog.text
    else:
        assert "outside 0-100" in caplog.text


async def test_playing_tracks_the_current_file(tmp_path: Path) -> None:
    """``playing`` is true from before the load command until the end-file event.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    async with _running(backend):
        assert _playing(backend) is False
        task = asyncio.create_task(backend.play(_chime(tmp_path), volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        assert _playing(backend) is True
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_end_file(fake.entry_id, _END_FILE_EOF)
        await _run(task)
        assert _playing(backend) is False


async def test_every_libmpv_call_runs_on_one_worker_thread(tmp_path: Path) -> None:
    """Construction, commands and terminate share one thread that is not the loop's.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        await _run(backend.list_devices())
        await _run(backend.set_device("USB"))
        await _run(backend.play(_chime(tmp_path), volume=50))
    threads = {ident for _, ident in fake.calls}
    assert threads == {fake.worker_thread}
    assert fake.worker_thread != threading.get_ident()
    assert fake.terminates == 1
    names = fake.names()
    assert names.count("unregister") == 2
    assert names.index("unregister") < names.index("terminate")


async def test_start_is_idempotent() -> None:
    """A second start does not construct a second player."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        await _run(backend.start())
        assert fake.names().count("MPV()") == 1
        assert fake.names().count("event_callback") == 2


async def test_methods_raise_before_start(tmp_path: Path) -> None:
    """Every command except ``stop`` refuses to run before ``start``.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    path = _chime(tmp_path)
    with pytest.raises(AudioBackendNotStarted):
        await _run(backend.play(path, volume=50))
    with pytest.raises(AudioBackendNotStarted):
        await _run(backend.list_devices())
    with pytest.raises(AudioBackendNotStarted):
        await _run(backend.set_device("USB"))
    with pytest.raises(AudioBackendNotStarted):
        await _run(backend.stop_playback())
    await _run(backend.stop())
    assert fake.calls == []


async def test_play_after_stop_raises_not_started(tmp_path: Path) -> None:
    """A stopped backend rejects playback until it is started again.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    await _run(backend.start())
    await _run(backend.stop())
    with pytest.raises(AudioBackendNotStarted):
        await _run(backend.play(_chime(tmp_path), volume=50))


async def test_overlapping_play_raises_and_does_not_hang(tmp_path: Path) -> None:
    """A second play while one is running is refused immediately.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        first = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        with pytest.raises(PlaybackInProgressError):
            await _run(backend.play(path, volume=50))
        assert fake.names().count("play") == 1
        fake.fire_start_file(fake.entry_id)
        await _settle()
        fake.fire_end_file(fake.entry_id, _END_FILE_EOF)
        await _run(first)


@pytest.mark.parametrize(
    "error",
    [ImportError("No module named 'mpv'"), OSError("libmpv.so.2: cannot open shared object")],
)
async def test_default_factory_reports_a_missing_python_mpv_or_libmpv(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A missing module and a missing shared library both map to one typed error.

    :param monkeypatch: patch helper.
    :param error: exception the import raises.
    """

    def fail_import(name: str, package: str | None = None) -> ModuleType:
        raise error

    monkeypatch.setattr("chime_deck.audio.mpv_backend.importlib.import_module", fail_import)
    backend = MpvBackend()
    with pytest.raises(AudioBackendUnavailable) as raised:
        await _run(backend.start())
    assert "libmpv" in str(raised.value)
    await _run(backend.stop())


@pytest.mark.parametrize(
    "error", [AttributeError("unknown option"), SystemError("mpv was shut down")]
)
async def test_player_construction_failures_map_to_backend_unavailable(error: Exception) -> None:
    """Any factory failure becomes an ``AudioBackendUnavailable``.

    :param error: exception the factory raises.
    """

    def factory(options: Mapping[str, object]) -> MpvPlayer:
        raise error

    backend = MpvBackend(player_factory=factory)
    with pytest.raises(AudioBackendUnavailable) as raised:
        await _run(backend.start())
    assert type(error).__name__ in str(raised.value)
    await _run(backend.stop())


async def test_a_player_that_fails_to_register_is_terminated_on_the_worker() -> None:
    """A half-built player never falls to GC, whose terminate() would block the loop."""
    fake = _FakeMpvPlayer()
    fake.failures["event_callback"] = SystemError("mpv was shut down")
    backend = _backend(fake)
    with pytest.raises(AudioBackendUnavailable):
        await _run(backend.start())
    assert fake.terminates == 1
    assert {ident for _, ident in fake.calls} == {fake.worker_thread}
    await _run(backend.stop())


async def test_a_failing_player_command_becomes_an_operation_error() -> None:
    """A libmpv ``SystemError`` is wrapped, never leaked as-is."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    async with _running(backend):
        fake.failures["audio_device_list"] = SystemError("mpv was shut down")
        with pytest.raises(AudioOperationError) as raised:
            await _run(backend.list_devices())
    assert raised.value.operation == "list audio devices"
    assert "SystemError" in str(raised.value)


async def test_stop_resolves_an_in_flight_play(tmp_path: Path) -> None:
    """Stopping the backend ends the running play normally and terminates once.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    calls: list[str] = []
    await _run(backend.start())
    task = asyncio.create_task(
        backend.play(_chime(tmp_path), volume=50, when_done=lambda: calls.append("done"))
    )
    await _wait_until(lambda: len(fake.played) == 1)
    await _run(backend.stop())
    await _run(task)
    assert calls == ["done"]
    assert _playing(backend) is False
    assert fake.terminates == 1
    await _run(backend.stop())
    assert fake.terminates == 1


async def test_stop_before_start_is_a_no_op() -> None:
    """Stopping an unstarted backend does nothing at all."""
    fake = _FakeMpvPlayer()
    backend = _backend(fake)
    await _run(backend.stop())
    assert fake.calls == []


async def test_stop_playback_ends_the_play_and_stays_usable(tmp_path: Path) -> None:
    """Stopping one file leaves the backend started and ready for the next.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer(mode="manual")
    backend = _backend(fake)
    path = _chime(tmp_path)
    async with _running(backend):
        first = asyncio.create_task(backend.play(path, volume=50))
        await _wait_until(lambda: len(fake.played) == 1)
        await _run(backend.stop_playback())
        await _run(first)
        assert _playing(backend) is False
        assert "stop" in fake.names()
        fake.mode = "auto"
        await _run(backend.play(path, volume=50))
        assert fake.names().count("play") == 2
    assert _playing(backend) is False


@pytest.mark.parametrize("key", sorted(_FORBIDDEN_MPV_OPTIONS))
async def test_forbidden_mpv_options_are_refused(key: str) -> None:
    """Every spelling that would silence the end-file event is rejected up front.

    :param key: forbidden option name from the constant itself.
    """
    with pytest.raises(ValueError) as raised:
        MpvBackend(player_factory=_factory(_FakeMpvPlayer()), mpv_options={key: False})
    assert key in str(raised.value)


async def test_factory_receives_the_base_options_plus_the_callers() -> None:
    """The factory gets the safe defaults with the caller's keys layered on top."""
    fake = _FakeMpvPlayer()
    backend = MpvBackend(player_factory=_factory(fake), mpv_options={"ao": "null"})
    async with _running(backend):
        pass
    assert fake.options == dict(_BASE_MPV_OPTIONS) | {"ao": "null"}


async def test_a_command_that_outlives_the_timeout_leaves_the_worker_alive(
    tmp_path: Path,
) -> None:
    """A timed-out command must not kill the worker thread that runs later ones.

    :param tmp_path: pytest temporary directory.
    """
    release = threading.Event()
    fake = _FakeMpvPlayer()
    fake.block = release
    backend = _backend(fake, command_timeout=0.05)
    async with _running(backend):
        task = asyncio.create_task(backend.play(_chime(tmp_path), volume=50))
        await _wait_until(lambda: "play" in fake.names())
        await asyncio.sleep(0.1)
        release.set()
        with pytest.raises(AudioOperationError) as raised:
            await _run(task)
        assert raised.value.operation == "play"
        fake.block = None
        devices = await _run(backend.list_devices())
        assert devices


async def test_play_prepends_and_then_clears_the_wake_lead_in(tmp_path: Path) -> None:
    """A lead-in is set as an adelay graph before the play, and cleared after it.

    The graph must go on before ``play`` so it applies to the file mpv starts,
    and come off afterwards so the next chime is not silently delayed too.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)

    async with _running(backend):
        applied = await _run(backend.play(_chime(tmp_path), volume=50, lead_in_ms=1000))

    assert applied == 1000
    assert fake.af_writes == ["lavfi=[adelay=delays=1000:all=1]", ""]
    names = fake.names()
    assert names.index("af=") < names.index("play")
    assert names.index("play") < names.index("af=", names.index("play"))


async def test_play_without_a_lead_in_writes_no_audio_filter(tmp_path: Path) -> None:
    """Asking for no lead-in leaves mpv's filter chain untouched.

    :param tmp_path: pytest temporary directory.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)

    async with _running(backend):
        applied = await _run(backend.play(_chime(tmp_path), volume=50, lead_in_ms=0))

    assert applied == 0
    assert fake.af_writes == []
    assert "af=" not in fake.names()


@pytest.mark.parametrize(
    ("requested", "applied"),
    [(0, 0), (1500, 1500), (_MAX_LEAD_IN_MS + 5000, _MAX_LEAD_IN_MS), (-20, 0)],
)
async def test_play_reports_the_lead_in_it_really_applied(
    tmp_path: Path, requested: int, applied: int
) -> None:
    """The return value is what was applied: 0 when none, the clamped length otherwise.

    :param tmp_path: pytest temporary directory.
    :param requested: lead-in the caller asked for, in milliseconds.
    :param applied: lead-in the backend must report applying.
    """
    fake = _FakeMpvPlayer()
    backend = _backend(fake)

    async with _running(backend):
        actual = await _run(backend.play(_chime(tmp_path), volume=50, lead_in_ms=requested))

    assert actual == applied
    if applied:
        assert fake.af_writes[0] == _lead_in_filter(applied)
    else:
        assert fake.af_writes == []


async def test_a_lead_in_that_cannot_be_set_still_plays_the_chime(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected filter graph warns and reports 0; the chime must still play.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)
    fake = _FakeMpvPlayer()
    fake.failures["af="] = RuntimeError("af is not available")
    backend = _backend(fake)

    async with _running(backend):
        applied = await _run(backend.play(_chime(tmp_path), volume=50, lead_in_ms=1000))

    assert applied == 0
    assert fake.played
    assert "the chime plays without it" in caplog.text


async def test_clamp_lead_in_keeps_the_range_and_warns_outside_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A negative lead-in becomes 0 and an over-long one becomes the maximum, both warned.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING)

    assert _clamp_lead_in(1500) == 1500
    assert _clamp_lead_in(0) == 0
    assert _clamp_lead_in(-1) == 0
    assert _clamp_lead_in(_MAX_LEAD_IN_MS) == _MAX_LEAD_IN_MS
    assert _clamp_lead_in(_MAX_LEAD_IN_MS + 1) == _MAX_LEAD_IN_MS

    warnings = [record.getMessage() for record in caplog.records]
    assert any("negative wake lead-in" in message for message in warnings)
    assert any("longer than" in message for message in warnings)
    assert len(warnings) == 2


async def test_lead_in_filter_builds_the_adelay_graph() -> None:
    """The graph is lavfi's adelay on every channel, in milliseconds."""
    assert _lead_in_filter(1500) == "lavfi=[adelay=delays=1500:all=1]"


def _write_test_wav(path: Path) -> Path:
    """Write a 200 ms 440 Hz sine at 25 % amplitude, 8 kHz mono 16-bit.

    :param path: destination file.
    :return: *path*, for convenience.
    """
    rate = 8000
    frames = int(rate * 0.2)
    payload = bytearray()
    for index in range(frames):
        value = int(0.25 * 32767 * math.sin(2 * math.pi * 440 * index / rate))
        payload += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return path


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("CHIMEDECK_TEST_REAL_MPV") != "1",
    reason=("./scripts/test-integration.sh: set CHIMEDECK_TEST_REAL_MPV=1 and provide libmpv"),
)
async def test_real_libmpv_plays_a_generated_chime(tmp_path: Path) -> None:
    """Play a generated tone through the real libmpv with a null audio output.

    Opt-in only: it loads libmpv, which the ordinary suite never does. Run it with
    ``CHIMEDECK_TEST_REAL_MPV=1 timeout 120 uv run pytest -q
    tests/test_audio_backend.py``. Audible output on the real box is a hand check,
    not something this test can assert. Note that importing python-mpv sets
    ``LC_NUMERIC`` to ``C`` for the whole process.

    :param tmp_path: pytest temporary directory.
    """
    path = _write_test_wav(tmp_path / "chime.wav")
    backend = MpvBackend(play_timeout=10.0, mpv_options={"ao": "null"})
    calls: list[str] = []
    try:
        await asyncio.wait_for(backend.start(), timeout=15)
    except AudioBackendUnavailable as exc:
        pytest.skip(f"./scripts/test-integration.sh: libmpv is not available on this host ({exc})")
    try:
        await asyncio.wait_for(
            backend.play(path, volume=70, when_done=lambda: calls.append("done")),
            timeout=15,
        )
        assert _playing(backend) is False
        assert calls == ["done"]
    finally:
        await asyncio.wait_for(backend.stop(), timeout=15)


async def test_a_start_that_times_out_can_be_tried_again() -> None:
    """A start that blew its budget is re-callable, which the recovery ladder relies on.

    ``_FakeMpvPlayer.block`` wedges ``play()`` only, so the wedge here is the
    expensive half of a start — the player factory — parked on the same event.
    The abandoned construction is released and waited for before the second
    start, so the fake is only ever driven by one worker at a time.
    """
    fake = _FakeMpvPlayer()
    fake.block = threading.Event()
    build = _factory(fake)

    def wedged_factory(options: Mapping[str, object]) -> MpvPlayer:
        """Block the player construction until the test releases the block.

        :param options: mpv options the backend passed.
        :return: the fake player, once the wedge is released.
        """
        assert fake.block is not None
        fake.block.wait(timeout=_AWAIT_TIMEOUT_S)
        return build(options)

    backend = MpvBackend(player_factory=wedged_factory)
    with pytest.raises(AudioBackendUnavailable) as raised:
        await _run(backend.start(timeout_s=0.05))
    assert "timed out" in str(raised.value)
    assert "start" in str(raised.value)
    assert _playing(backend) is False

    fake.block.set()
    await _wait_until(lambda: fake.names().count("MPV()") == 1)

    try:
        await _run(backend.start(timeout_s=1.0))
        assert _playing(backend) is False
    finally:
        await _run(backend.stop())
    assert fake.terminates == 1


async def test_warm_library_swallows_a_missing_mpv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm-up that cannot load python-mpv is silent, never a startup failure.

    :param monkeypatch: patcher that removes the lazy import seam.
    """

    def missing() -> ModuleType:
        """Stand in for the lazy import on a host without python-mpv.

        :raises AudioBackendUnavailable: always, as the real seam does.
        """
        raise AudioBackendUnavailable("python-mpv/libmpv is not available on this host")

    monkeypatch.setattr("chime_deck.audio.mpv_backend._load_mpv_module", missing)

    warm_library()


async def test_warm_library_never_raises_on_this_host() -> None:
    """The real warm-up is total: whatever this host has, it never raises."""
    warm_library()
