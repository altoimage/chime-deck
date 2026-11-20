"""Audio API: list the output devices the speaker can see and resolve the stored hint.

``GET /api/audio/devices`` asks the started
:class:`~chime_deck.audio.AudioBackend` for its device list and reports what
the stored ``audio.device_hint`` resolves to. It is what the settings page needs
to tell the user "the hint you typed matches nothing, mpv will use the system
default" before a chime is ever pressed.

The route owns no device logic: :meth:`AudioBackend.list_devices` is the same
call the playback worker's backend uses, and
:func:`~chime_deck.audio.resolve_device` is the same matcher the backend
applies when it selects a device, so the list and the playback cannot disagree
about which output a hint names.

Four behaviours are deliberate and must not be "fixed":

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

from ..audio import SYSTEM_DEFAULT_DEVICE, AudioError, resolve_device
from ..config import SECRET_SCRUBBER

if TYPE_CHECKING:
    from ..app import AppState

__all__ = [
    "AUDIO_NOT_STARTED_TEXT",
    "AudioDeviceModel",
    "AudioDevicesResponse",
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


class AudioDeviceModel(BaseModel):
    """One output device as the settings page lists it.

    ``name`` is mpv's device name (the value a hint matches and the value written
    back to the player), ``hostapi`` is the label derived from its prefix and
    ``is_default`` marks the output mpv uses when no name is written.
    """

    name: str
    hostapi: str
    is_default: bool


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

    return router
