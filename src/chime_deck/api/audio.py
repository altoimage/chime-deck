"""Audio API: list the output devices, resolve the stored hint and read the device level.

``GET /api/audio/devices`` asks the started
:class:`~chime_deck.audio.AudioBackend` for its device list and reports what
the stored ``audio.device_hint`` resolves to. It is what the settings page needs
to tell the user "the hint you typed matches nothing, mpv will use the system
default" before a chime is ever pressed.

``GET /api/audio/hardware`` reports the resolved output's own volume control —
the second loudness stage cd-qul9 made the app own — together with the level the
settings ask for (``configured``) and the level the last apply really wrote
(``applied``). Showing both in one response is the point: the original bug was a
UI that said 100% while the DAC said 43%.

The route owns no device logic: :meth:`AudioBackend.list_devices` is the same
call the playback worker's backend uses, and
:func:`~chime_deck.audio.resolve_device` is the same matcher the backend
applies when it selects a device, so the list and the playback cannot disagree
about which output a hint names.

Six behaviours are deliberate and must not be "fixed":

* **HTTP 200 carries the verdict.** A discovery that ran and failed answers 200
  with ``ok: false`` and prose saying so, exactly like
  ``POST /api/settings/test/*``. A degraded audio state is never a 4xx and never
  a 500: a 500 would tell the user less than a sentence.
* **An empty device list is a success.** Discovery ran and the host has nothing
  to offer: ``ok: true``, ``devices: []``, ``resolved_device: null``. Only a
  discovery that failed is ``ok: false``.
* **The stored hint is echoed and resolved without judgement.** A hint that
  matches nothing is not an error here — mpv falls back to the system default,
  and the UI is what warns. ``resolved_device`` is where that becomes visible.
* **``resolved_device`` is always a real device NAME** (or ``null``), never a
  description or a friendly label: the name is the value written back to the
  player.
* **The hardware route is read-only.** A ``GET`` that changed a system mixer
  would be a surprise, so it calls ``read_hardware()`` and never ``apply()``;
  startup, a settings change and a device change do the writing.
* **``GET /api/audio/hardware`` carries its verdict on 200 too.** An output with
  no hardware volume control (an HDMI ``IEC958`` switch, for instance) answers
  ``ok: true, available: false`` with its reason: that is an honest answer about
  the host, not a failed request. Only a genuine failure to ask at all — the
  audio engine not started, a store read that failed — is ``ok: false``.

Every string that enters the response is scrubbed with
:data:`~chime_deck.config.SECRET_SCRUBBER`, so a host that echoes a
credential into a device name or an error cannot leak it through this route.

Owning ticket: HSD-wpm0.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from pydantic import BaseModel

from ..audio import (
    NO_HARDWARE_CONTROL_TEXT,
    SYSTEM_DEFAULT_DEVICE,
    AudioError,
    HardwareVolumeStatus,
    resolve_device,
)
from ..config import SECRET_SCRUBBER

if TYPE_CHECKING:
    from ..app import AppState

__all__ = [
    "AUDIO_NOT_STARTED_TEXT",
    "AudioDeviceModel",
    "AudioDevicesResponse",
    "HardwareVolumeResponse",
    "build_audio_router",
]

AUDIO_NOT_STARTED_TEXT: Final = (
    "the audio engine has not started yet, so no test tone can play — check the audio health "
    "status and the application log, then test again"
)
"""The one sentence for "no backend is running", shared with the audio probe."""

_TIMEOUT_TEXT: Final = (
    "audio device discovery did not answer within {seconds:g} s — check the audio health status "
    "and the application log, then try again"
)
_DISCOVERY_FAILED_TEXT: Final = "audio device discovery failed: {detail}"
_HARDWARE_READ_FAILED_TEXT: Final = "the device's own volume control could not be read: {detail}"

_HARDWARE_ANSWERS: Final = (NO_HARDWARE_CONTROL_TEXT, "no control matching")
"""Reason prefixes that are an answer about the host rather than a failure to ask.

``no hardware volume control`` is the HDMI-style output; ``no control matching``
is a stored ``audio.hardware_control`` that names nothing on this device. Both
are deliberate outcomes of a read that ran, so both are ``ok: true``.
"""


class AudioDeviceModel(BaseModel):
    """One output device as the settings page lists it.

    ``name`` is mpv's device name (the value a hint matches and the value written
    back to the player), ``hostapi`` is the label derived from its prefix and
    ``is_default`` marks the output mpv uses when no name is written.
    """

    name: str
    hostapi: str
    is_default: bool


class HardwareVolumeResponse(BaseModel):
    """The device's own volume control, what the settings ask for, and what was written.

    ``available`` is true only when a control was found and read, so ``percent``
    is the value actually on the device and ``reason`` is ``""``. ``device`` is
    the resolved output's mpv name and ``control`` the ALSA simple-mixer control
    or the Pulse/PipeWire sink. ``configured`` echoes the stored
    ``audio.hardware_volume`` so the panel can show "you asked for 100, the device
    says 43" in one glance; ``applied`` is the percent the last apply of this
    process actually wrote, or ``None`` when that call wrote nothing. ``ok`` is
    false only when the route could not get an answer at all: an output with no
    hardware volume control is an answer and reports ``available: false`` with its
    reason.
    """

    ok: bool
    available: bool
    device: str | None = None
    control: str | None = None
    percent: int | None = None
    muted: bool = False
    reason: str = ""
    configured: int | None = None
    applied: int | None = None


class AudioDevicesResponse(BaseModel):
    """The audio outputs, the stored hint and what that hint resolves to.

    ``hint`` echoes ``audio.device_hint`` verbatim, ``resolved_device`` is the
    matching device *name* or ``None`` when the hint is blank or unnamed,
    ``system_default`` is the token mpv uses when nothing is selected, and
    ``devices`` is in the backend's own order. ``error`` is the user-facing prose
    when discovery did not run or failed, and ``None`` when it succeeded — even
    when the host has no device at all.
    """

    ok: bool
    hint: str = ""
    resolved_device: str | None = None
    system_default: str = SYSTEM_DEFAULT_DEVICE
    devices: list[AudioDeviceModel] = []
    error: str | None = None


def _scrub(value: str) -> str:
    """Redact any secret-looking text before it enters the response.

    :param value: prose, a device name or an exception message about to be returned.
    """
    return SECRET_SCRUBBER.scrub(value)


def _hardware_answer(status: HardwareVolumeStatus) -> bool:
    """Return whether a hardware-volume status is an answer rather than a failure.

    ``available`` is the easy case. An unavailable status is still an answer when
    the reason is about the output itself (no control, or a stored control name
    that matches nothing); anything else — a missing utility, a timeout, an
    unparseable value, a store failure — means the route could not ask.

    :param status: the status the hardware service returned.
    """
    if status.available:
        return True
    return status.reason.startswith(_HARDWARE_ANSWERS)


def build_audio_router(state: AppState) -> APIRouter:
    """Build the ``/api/audio`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store, the chime pipeline
        and the probe timeout.
    """
    router = APIRouter(prefix="/api/audio", tags=["audio"])

    def _hint_text(value: object) -> str:
        """Return the stored ``audio.device_hint`` as text.

        :param value: whatever the store returned for the setting.
        """
        return value if isinstance(value, str) else ""

    @router.get("/devices")
    async def list_audio_devices() -> AudioDevicesResponse:
        """Report the audio outputs and which one the stored hint names.

        ``ok`` carries the verdict on HTTP 200: a discovery that ran and found
        nothing is a success, and only a discovery that failed is ``ok: false``.
        The backend is read first and answered without touching the store, so a
        pipeline that never started reports the honest sentence instead of a 500.
        """
        backend = state.chime.backend
        if backend is None:
            return AudioDevicesResponse(ok=False, error=_scrub(AUDIO_NOT_STARTED_TEXT))
        hint = _scrub(_hint_text(await state.store.get_setting("audio.device_hint")))
        try:
            async with asyncio.timeout(state.probe_timeout):
                devices = await backend.list_devices()
        except TimeoutError:
            prose = _TIMEOUT_TEXT.format(seconds=state.probe_timeout)
            return AudioDevicesResponse(ok=False, hint=hint, error=_scrub(prose))
        except AudioError as exc:
            # Covers ``AudioBackendUnavailable`` (a backend this build lacks) and
            # ``AudioBackendNotStarted``; both already name the setting and the fix.
            return AudioDevicesResponse(ok=False, hint=hint, error=_scrub(str(exc)))
        except Exception as exc:
            prose = _DISCOVERY_FAILED_TEXT.format(detail=exc)
            return AudioDevicesResponse(ok=False, hint=hint, error=_scrub(prose))
        resolved = resolve_device(devices, hint)
        return AudioDevicesResponse(
            ok=True,
            hint=hint,
            resolved_device=None if resolved is None else _scrub(resolved.name),
            system_default=SYSTEM_DEFAULT_DEVICE,
            devices=[
                AudioDeviceModel(
                    name=_scrub(device.name),
                    hostapi=_scrub(device.hostapi),
                    is_default=device.is_default,
                )
                for device in devices
            ],
            error=None,
        )

    @router.get("/hardware")
    async def read_hardware_volume() -> HardwareVolumeResponse:
        """Report the device's own volume control without writing it.

        Read-only by contract: the route calls ``read_hardware()``, never
        ``apply()``. ``ok`` carries the verdict on HTTP 200 — an output with no
        hardware volume control, or a stored control name that matches nothing,
        is an answer and therefore ``ok: true, available: false`` — while only a
        failure to ask at all is ``ok: false``. A pipeline whose backend has not
        started answers honestly instead of raising, so this route can never 500.
        """
        if state.chime.backend is None:
            return HardwareVolumeResponse(
                ok=False, available=False, reason=_scrub(AUDIO_NOT_STARTED_TEXT)
            )
        try:
            stored = await state.store.get_setting("audio.hardware_volume")
        except Exception as exc:
            prose = _HARDWARE_READ_FAILED_TEXT.format(detail=exc)
            return HardwareVolumeResponse(ok=False, available=False, reason=_scrub(prose))
        status = await state.chime.read_hardware()
        return HardwareVolumeResponse(
            ok=_hardware_answer(status),
            available=status.available,
            device=None if status.device is None else _scrub(status.device),
            control=None if status.control is None else _scrub(status.control),
            percent=status.percent,
            muted=status.muted,
            reason=_scrub(status.reason),
            configured=stored if isinstance(stored, int) and not isinstance(stored, bool) else None,
            applied=status.applied,
        )

    return router
