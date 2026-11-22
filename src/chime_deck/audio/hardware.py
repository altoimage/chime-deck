"""The device's own volume control: the second loudness stage the app owns.

Audible loudness is the product of three stages: mpv's software gain
(``audio.volume``), a Pulse/PipeWire sink volume, and the ALSA hardware playback
control. This module owns the last two — whichever the resolved output actually
has — so the settings page's 100% means what the user hears instead of only what
mpv was told. It exists because a USB DAC's ``Speaker`` control defaulted to 43%
while the UI said 100%: the app was raising the gain into a half-closed tap.

Every step is fail-soft. A mixer that is missing, wedged, exits non-zero or
prints something unparseable becomes a sentence in
:attr:`HardwareVolumeStatus.reason`, never an exception and never an audio-health
failure — a cosmetic mixer problem must not make the doorbell look dead. The
distinction the status keeps is the one that matters: ``available=False`` with
"no hardware volume control" is an honest answer about an output that has none
(HDMI's ``IEC958`` control is a switch, not a volume); "the amixer utility is not
installed" is a failure to ask at all.

No subprocess is started except through :mod:`chime_deck.audio.commands`, whose
spawner is the test seam, and nothing here blocks the event loop.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from .backend import AudioBackend
from .commands import COMMAND_TIMEOUT_S, CommandResult, CommandSpawner, run_command
from .devices import AudioDevice, resolve_device

if TYPE_CHECKING:
    from ..store import ConfigStore

__all__ = [
    "CONTROL_PREFERENCE",
    "HARDWARE_NOT_STARTED_TEXT",
    "NO_HARDWARE_CONTROL_TEXT",
    "HardwareVolume",
    "HardwareVolumeStatus",
    "card_token",
    "choose_control",
    "has_playback_volume",
    "parse_amixer_muted",
    "parse_amixer_percent",
    "parse_control_names",
    "parse_pactl_mute",
    "parse_pactl_percent",
    "pulse_sink_name",
]

NO_HARDWARE_CONTROL_TEXT: Final = "no hardware volume control"
"""The honest answer for an output whose only control is a switch (the HDMI case)."""

HARDWARE_NOT_STARTED_TEXT: Final = (
    "the audio engine has not started yet, so the device's own volume cannot be read or set"
)
"""The sentence for a pipeline that has not built its backend yet."""

CONTROL_PREFERENCE: Final = ("speaker", "headphone", "master", "pcm", "front", "digital")
"""Substrings tried in order when ``audio.hardware_control`` is empty.

A USB DAC exposes several controls (``Speaker``, ``Mic``, ``Auto Gain Control``),
and the one that is the output is not discoverable from their order alone, so a
narrow preference list beats "the first thing amixer printed" and the stored
override beats both.
"""

_CARD_PATTERN: Final = re.compile(r"CARD=([\w.-]+)")
_HW_INDEX_PATTERN: Final = re.compile(r"hw:(\d+)")
_AMIXER_PERCENT_PATTERN: Final = re.compile(r"\[(\d+)%\]")
_PACTL_PERCENT_PATTERN: Final = re.compile(r"(\d+)%")
_CONTROL_NAME_PATTERN: Final = re.compile(r"Simple mixer control '([^']+)'")
_PULSE_PREFIXES: Final = ("pulse", "pipewire")

_AMIXER: Final = "amixer"
_PACTL: Final = "pactl"


@dataclass(frozen=True, slots=True)
class HardwareVolumeStatus:
    """What the device's own control says, and whether the app could reach it.

    ``available`` is true only when a control the app can both read and write was
    found and read; ``percent`` is therefore the value actually read back, never
    the value that was asked for. ``device`` is the resolved output's mpv name,
    so the mixer and the playback cannot disagree about the output, and
    ``control`` is the ALSA simple-mixer control name or the Pulse/PipeWire sink.
    ``reason`` is ``""`` when available and one actionable sentence otherwise.
    ``applied`` remembers the percent the last :meth:`HardwareVolume.apply`
    actually wrote, taken from the read-back that followed the write, so the
    settings page can show "you asked for 100, the device now says 100".
    """

    available: bool
    device: str | None
    control: str | None
    percent: int | None
    muted: bool
    reason: str
    applied: int | None


@dataclass(frozen=True, slots=True)
class _MixerTarget:
    """One resolved control to read and write.

    ``medium`` is ``"alsa"`` (driven by ``amixer``, with ``card`` when the device
    name names one) or ``"pulse"`` (driven by ``pactl`` against a sink name).
    """

    medium: str
    device: str
    control: str
    card: str | None


@dataclass(frozen=True, slots=True)
class _Resolution:
    """The outcome of resolving an output to a control, plus why it failed."""

    target: _MixerTarget | None
    device: str | None
    control: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class _Reading:
    """One read of a control: the percentage, the mute flag and any failure prose."""

    percent: int | None
    muted: bool
    reason: str


def card_token(device_name: str) -> str | None:
    """Derive the ``amixer -c`` card from an mpv device name.

    ``alsa/hw:CARD=Device,DEV=0`` names the card in the string; ``alsa/hw:2,0``
    names it by index. A name that names neither (``alsa/default``,
    ``alsa/plughw:...`` without ``CARD=``) yields ``None``, which means "let
    ``amixer`` open the ALSA default" rather than guessing.

    :param device_name: mpv device name such as ``alsa/hw:CARD=Device,DEV=0``.
    :return: the card token to pass after ``-c``, or ``None``.
    """
    match = _CARD_PATTERN.search(device_name)
    if match is not None:
        return match.group(1)
    index = _HW_INDEX_PATTERN.search(device_name)
    return None if index is None else index.group(1)


def pulse_sink_name(device_name: str) -> str | None:
    """Return the Pulse/PipeWire sink name an mpv device name addresses.

    Only the ``pulse/`` and ``pipewire/`` host APIs are driven through
    ``pactl``; every other name goes down the ALSA path or falls back to the
    ALSA default.

    :param device_name: mpv device name such as ``pipewire/alsa_output.pci-...``.
    :return: the name after the first ``/``, or ``None`` for an ALSA device.
    """
    prefix, separator, rest = device_name.partition("/")
    if separator == "" or prefix.strip().lower() not in _PULSE_PREFIXES:
        return None
    sink = rest.strip()
    return None if sink == "" else sink


def parse_control_names(scontrols_output: str) -> tuple[str, ...]:
    """Extract the simple-mixer control names from ``amixer scontrols`` output.

    :param scontrols_output: stdout of ``amixer scontrols``.
    :return: the names in the order amixer printed them.
    """
    return tuple(_CONTROL_NAME_PATTERN.findall(scontrols_output))


def choose_control(controls: Sequence[str], explicit: str) -> str | None:
    """Pick the control to drive from the probed names and the stored override.

    A non-empty *explicit* name wins, matched case-insensitively — exactly first,
    then as a substring, so ``speaker`` finds ``Speaker`` and ``Speaker`` finds
    ``Speaker Playback Volume``. An explicit name that matches nothing yields
    ``None``: the caller turns that into a reason naming it, because a wrong
    manual setting must be visible rather than silently ineffective. With no
    override the preference order in :data:`CONTROL_PREFERENCE` decides, and an
    output whose controls match none of it is left to the caller's playback-volume
    probe (which is what recognises an HDMI switch-only control).

    :param controls: control names printed by ``amixer scontrols``.
    :param explicit: stored ``audio.hardware_control``; blank means auto-detect.
    :return: the chosen control name, or ``None``.
    """
    wanted = explicit.strip().casefold()
    if wanted != "":
        for control in controls:
            if control.casefold() == wanted:
                return control
        for control in controls:
            if wanted in control.casefold():
                return control
        return None
    for keyword in CONTROL_PREFERENCE:
        for control in controls:
            if keyword in control.casefold():
                return control
    return None


def has_playback_volume(sget_output: str) -> bool:
    """Report whether ``amixer sget`` printed a playback *volume* line.

    A switch-only control (``Mono: Playback [off]``) mentions ``Playback`` but
    carries no percentage, which is how HDMI-class outputs are recognised and
    reported as having no hardware volume control.

    :param sget_output: stdout of ``amixer sget <control>``.
    :return: whether some line names ``Playback`` and shows a percentage.
    """
    for line in sget_output.splitlines():
        if "Playback" in line and _AMIXER_PERCENT_PATTERN.search(line) is not None:
            return True
    return False


def parse_amixer_percent(sget_output: str) -> int | None:
    """Return the first ``[NN%]`` in ``amixer sget`` output.

    :param sget_output: stdout of ``amixer sget <control>``.
    :return: the percentage, or ``None`` when there is no volume line.
    """
    match = _AMIXER_PERCENT_PATTERN.search(sget_output)
    return None if match is None else int(match.group(1))


def parse_amixer_muted(sget_output: str) -> bool:
    """Return the switch state reported by ``amixer sget``.

    ``[off]`` is the only mute marker; a line with neither marker (a control that
    has no switch at all) is treated as unmuted, which is the harmless reading.

    :param sget_output: stdout of ``amixer sget <control>``.
    :return: whether the control reports ``[off]``.
    """
    return "[off]" in sget_output


def parse_pactl_percent(volume_output: str) -> int | None:
    """Return the first ``NN%`` in ``pactl get-sink-volume`` output.

    :param volume_output: stdout of ``pactl get-sink-volume <sink>``.
    :return: the percentage, or ``None`` when the line is missing.
    """
    match = _PACTL_PERCENT_PATTERN.search(volume_output)
    return None if match is None else int(match.group(1))


def parse_pactl_mute(mute_output: str) -> bool:
    """Return whether ``pactl get-sink-mute`` says the sink is muted.

    :param mute_output: stdout of ``pactl get-sink-mute <sink>``.
    :return: whether the line reads ``Mute: yes``.
    """
    return "Mute: yes" in mute_output


def _amixer_argv(card: str | None, *arguments: str) -> tuple[str, ...]:
    """Build one ``amixer`` argv, with ``-c`` only when the card is known.

    :param card: card token from :func:`card_token`, or ``None`` for the default.
    :param arguments: the command and its operands, e.g. ``"sset", "Speaker", "40%"``.
    """
    prefix = (_AMIXER,) if card is None else (_AMIXER, "-c", card)
    return (*prefix, *arguments)


def _pactl_argv(*arguments: str) -> tuple[str, ...]:
    """Build one ``pactl`` argv.

    :param arguments: the command and its operands, e.g. ``"set-sink-mute", sink, "0"``.
    """
    return (_PACTL, *arguments)


def _failure_reason(result: CommandResult, utility: str, action: str) -> str:
    """Turn a failed command into the one sentence the status carries.

    The three outcomes are kept distinct on purpose: a missing utility is a
    deployment fact the user can fix, a timeout means the mixer is wedged, and a
    non-zero exit carries the utility's own words.

    :param result: the failed command.
    :param utility: ``"amixer"`` or ``"pactl"``, for the sentence.
    :param action: what was attempted, e.g. ``"sget Speaker"``.
    """
    if result.returncode is None:
        return f"the {utility} utility did not answer within {COMMAND_TIMEOUT_S:g} s"
    if result.returncode == 127:
        return f"the {utility} utility is not installed"
    detail = result.stderr.strip() or f"exit status {result.returncode}"
    return f"{utility} {action} failed: {detail}"


class HardwareVolume:
    """Read and set the resolved output's own volume control, never raising.

    One instance per pipeline. Construction opens nothing; every call resolves
    the output from the live backend, so a device change is picked up without a
    restart and the mixer cannot point at a different output than the player.
    One :class:`asyncio.Lock` serialises read/write pairs, so a settings change
    and a startup apply cannot interleave their two commands.

    :meth:`read` is the read-only verdict the API route uses: it never changes
    the mixer. :meth:`apply` is what startup, every device change and every
    ``audio.*`` change invokes: it writes only when the read-back differs, so a
    boot whose stored value already matches causes no click and no churn, and a
    muted control is cleared in the same ``sset``.
    """

    def __init__(
        self,
        *,
        store: ConfigStore,
        backend: Callable[[], AudioBackend | None],
        spawner: CommandSpawner | None = None,
    ) -> None:
        """Prepare the service; nothing is read or written until a method is awaited.

        :param store: configuration store read for ``audio.device_hint``,
            ``audio.hardware_control``, ``audio.hardware_volume`` and
            ``audio.muted``.
        :param backend: lazy accessor for the started speaker, so the resolved
            device is the one mpv is really playing through; ``None`` from it
            means "not started yet".
        :param spawner: process factory; ``None`` resolves
            :data:`~chime_deck.audio.commands.DEFAULT_SPAWN` at call time, which
            is what keeps the default offline under the test suite's wiring.
        """
        self._store = store
        self._backend = backend
        self._spawner = spawner
        self._lock = asyncio.Lock()
        self._status: HardwareVolumeStatus | None = None
        self._applied: int | None = None

    @property
    def status(self) -> HardwareVolumeStatus | None:
        """Return the last status read or applied, or ``None`` before either."""
        return self._status

    async def read(self) -> HardwareVolumeStatus:
        """Resolve the output and read its control, without writing anything.

        Total by construction: every failure becomes a status with
        ``available=False`` and a sentence. This is the route's method, because a
        ``GET`` that changes a system mixer is a surprise.
        """
        async with self._lock:
            return await self._read_locked()

    async def apply(self) -> HardwareVolumeStatus:
        """Make the device match the stored settings, then read the result back.

        The target is ``0`` when ``audio.muted`` is true and otherwise
        ``audio.hardware_volume``; the write happens only when the current value
        differs or the control is muted while the target is not, so an already
        correct device is left alone. ``applied`` is taken from a fresh read
        after the write, never from the value that was requested: the panel must
        show what the device says, not what the app hoped.
        """
        async with self._lock:
            return await self._apply_locked()

    async def _read_locked(self) -> HardwareVolumeStatus:
        """Read under the lock, converting even an unexpected error into a status."""
        try:
            resolution, reading = await self._probe()
        except Exception as exc:
            resolution = _Resolution(
                None, None, None, f"the device's own volume could not be read: {exc}"
            )
            reading = _Reading(None, False, resolution.reason)
        status = _status_of(resolution, reading, self._applied)
        self._status = status
        return status

    async def _apply_locked(self) -> HardwareVolumeStatus:
        """Apply under the lock, converting even an unexpected error into a status."""
        self._applied = None
        try:
            resolution, reading = await self._probe()
            status = _status_of(resolution, reading, None)
            if not status.available or resolution.target is None:
                self._status = status
                return status
            target_percent = await self._target_percent()
            if reading.percent == target_percent and not (reading.muted and target_percent > 0):
                # Already right: no write, so no click and no settings churn.
                self._status = status
                return status
            failure = await self._write(resolution.target, target_percent)
            if failure != "":
                status = replace(status, available=False, reason=failure, applied=None)
                self._status = status
                return status
            confirmed = await self._read_target(resolution.target)
            if confirmed.percent is None:
                status = replace(status, available=False, reason=confirmed.reason, applied=None)
                self._status = status
                return status
            self._applied = confirmed.percent
            status = _status_of(resolution, confirmed, self._applied)
            self._status = status
            return status
        except Exception as exc:
            status = HardwareVolumeStatus(
                available=False,
                device=None,
                control=None,
                percent=None,
                muted=False,
                reason=f"the device's own volume could not be set: {exc}",
                applied=None,
            )
            self._status = status
            return status

    async def _probe(self) -> tuple[_Resolution, _Reading]:
        """Resolve the output and read its control once.

        :return: the resolution and the reading; the reading carries the
            resolution's reason when there is no target to read.
        """
        resolution = await self._resolve()
        if resolution.target is None:
            return resolution, _Reading(None, False, resolution.reason)
        return resolution, await self._read_target(resolution.target)

    async def _run(self, argv: Sequence[str]) -> CommandResult:
        """Run one mixer command through the injected seam.

        :param argv: complete argv, never through a shell.
        """
        return await run_command(argv, spawner=self._spawner)

    async def _resolve(self) -> _Resolution:
        """Turn the resolved output into a control the service can drive.

        Failures are data, not exceptions: each returns a :class:`_Resolution`
        with ``target=None`` and the sentence the status will carry.
        """
        backend = self._backend()
        if backend is None:
            return _Resolution(None, None, None, HARDWARE_NOT_STARTED_TEXT)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                devices = await backend.list_devices()
        except TimeoutError:
            return _Resolution(
                None,
                None,
                None,
                f"the audio outputs did not answer within {COMMAND_TIMEOUT_S:g} s",
            )
        except Exception as exc:
            return _Resolution(None, None, None, f"the audio outputs could not be listed: {exc}")
        device = await self._resolved_device(devices)
        if device is None:
            return _Resolution(None, None, None, "the audio engine reported no output devices")
        sink = pulse_sink_name(device.name)
        if sink is not None:
            target = _MixerTarget(medium="pulse", device=device.name, control=sink, card=None)
            return _Resolution(target, device.name, sink, "")
        card = card_token(device.name)
        controls_result = await self._run(_amixer_argv(card, "scontrols"))
        if not controls_result.ok:
            reason = _failure_reason(controls_result, _AMIXER, "scontrols")
            return _Resolution(None, device.name, None, reason)
        controls = parse_control_names(controls_result.stdout)
        try:
            stored_control = await self._store.get_setting("audio.hardware_control")
        except Exception as exc:
            reason = f"audio.hardware_control could not be read: {exc}"
            return _Resolution(None, device.name, None, reason)
        explicit = stored_control if isinstance(stored_control, str) else ""
        control = choose_control(controls, explicit)
        if control is not None:
            target = _MixerTarget(medium="alsa", device=device.name, control=control, card=card)
            return _Resolution(target, device.name, control, "")
        if explicit.strip() != "":
            reason = f"no control matching {explicit.strip()!r} on this device"
            return _Resolution(None, device.name, None, reason)
        for candidate in controls:
            probe = await self._run(_amixer_argv(card, "sget", candidate))
            if probe.ok and has_playback_volume(probe.stdout):
                target = _MixerTarget(
                    medium="alsa", device=device.name, control=candidate, card=card
                )
                return _Resolution(target, device.name, candidate, "")
        return _Resolution(None, device.name, None, NO_HARDWARE_CONTROL_TEXT)

    async def _resolved_device(self, devices: Sequence[AudioDevice]) -> AudioDevice | None:
        """Pick the output mpv is playing through, using the same matcher as playback.

        The stored hint is applied with :func:`~chime_deck.audio.resolve_device`,
        exactly as the backend does, so the mixer and the speaker cannot disagree.
        A blank or unmatched hint falls back to the device mpv uses when nothing
        is selected: the one marked ``is_default``, else the first.

        :param devices: outputs reported by the backend, in its own order.
        :return: the output to drive, or ``None`` when the backend listed none.
        """
        try:
            stored_hint = await self._store.get_setting("audio.device_hint")
        except Exception as exc:
            raise RuntimeError(f"audio.device_hint could not be read: {exc}") from exc
        hint = stored_hint if isinstance(stored_hint, str) else ""
        resolved = resolve_device(devices, hint)
        if resolved is not None:
            return resolved
        for device in devices:
            if device.is_default:
                return device
        return devices[0] if devices else None

    async def _read_target(self, target: _MixerTarget) -> _Reading:
        """Read one resolved control.

        :param target: the control to read.
        """
        if target.medium == "pulse":
            return await self._read_pulse(target.control)
        result = await self._run(_amixer_argv(target.card, "sget", target.control))
        if not result.ok:
            reason = _failure_reason(result, _AMIXER, f"sget {target.control}")
            return _Reading(None, False, reason)
        percent = parse_amixer_percent(result.stdout)
        if percent is None:
            reason = f"the {target.control!r} mixer control reports no volume percentage"
            return _Reading(None, False, reason)
        return _Reading(percent, parse_amixer_muted(result.stdout), "")

    async def _read_pulse(self, sink: str) -> _Reading:
        """Read a Pulse/PipeWire sink's volume and mute flag.

        :param sink: sink name from :func:`pulse_sink_name`.
        """
        volume = await self._run(_pactl_argv("get-sink-volume", sink))
        if not volume.ok:
            reason = _failure_reason(volume, _PACTL, f"get-sink-volume {sink}")
            return _Reading(None, False, reason)
        percent = parse_pactl_percent(volume.stdout)
        if percent is None:
            reason = f"the {sink!r} sink reports no volume percentage"
            return _Reading(None, False, reason)
        mute = await self._run(_pactl_argv("get-sink-mute", sink))
        if not mute.ok:
            reason = _failure_reason(mute, _PACTL, f"get-sink-mute {sink}")
            return _Reading(None, False, reason)
        return _Reading(percent, parse_pactl_mute(mute.stdout), "")

    async def _write(self, target: _MixerTarget, percent: int) -> str:
        """Set one resolved control, clearing its mute in the same step.

        :param target: the control to write.
        :param percent: target percentage, already clamped by the registry.
        :return: ``""`` on success, else the sentence describing the failure.
        """
        if target.medium == "pulse":
            volume = await self._run(_pactl_argv("set-sink-volume", target.control, f"{percent}%"))
            if not volume.ok:
                return _failure_reason(volume, _PACTL, f"set-sink-volume {target.control}")
            unmute = await self._run(_pactl_argv("set-sink-mute", target.control, "0"))
            if not unmute.ok:
                return _failure_reason(unmute, _PACTL, f"set-sink-mute {target.control}")
            return ""
        result = await self._run(
            _amixer_argv(target.card, "sset", target.control, f"{percent}%", "unmute")
        )
        if not result.ok:
            return _failure_reason(result, _AMIXER, f"sset {target.control}")
        return ""

    async def _target_percent(self) -> int:
        """Return the percentage the stored settings ask the device to be at.

        Mute silences both stages (D28), so a muted device targets 0 and unmute
        restores the stored ``audio.hardware_volume``: the value the user chose is
        never destroyed by muting.

        :raises Exception: a store read failure, reported by the caller.
        """
        if await self._store.get_setting("audio.muted") is True:
            return 0
        stored = await self._store.get_setting("audio.hardware_volume")
        if isinstance(stored, bool) or not isinstance(stored, int):
            return 100
        return min(max(stored, 0), 100)


def _status_of(
    resolution: _Resolution, reading: _Reading, applied: int | None
) -> HardwareVolumeStatus:
    """Assemble a status from one resolution and one reading.

    :param resolution: what the output resolved to.
    :param reading: what the control reported.
    :param applied: percent the last :meth:`HardwareVolume.apply` wrote, if any.
    """
    available = resolution.target is not None and reading.percent is not None
    reason = "" if available else (reading.reason or resolution.reason)
    return HardwareVolumeStatus(
        available=available,
        device=resolution.device,
        control=resolution.control,
        percent=reading.percent,
        muted=reading.muted,
        reason=reason,
        applied=applied if available else None,
    )
