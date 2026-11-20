"""Audio output devices, as this project models them.

libmpv reports every output as a ``name`` plus a human ``description``. The
``name`` is the value that has to be written back to ``player.audio_device``, so
it is the only field :func:`resolve_device` matches against; the settings page
still shows the description. libmpv exposes no host-API field, so
:func:`hostapi_of` derives the label from the name prefix (``alsa/hw:...`` ->
``alsa``).

Owning ticket: HSD-h13q.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["SYSTEM_DEFAULT_DEVICE", "AudioDevice", "hostapi_of", "resolve_device"]

SYSTEM_DEFAULT_DEVICE: Final = "auto"
"""Device name that means "let mpv pick"; also a valid ``audio.device_hint``."""


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """One audio output the backend can see.

    ``name`` is mpv's device name — the value a hint matches and the value
    written back to the player — and ``hostapi`` is the label derived from its
    prefix. ``is_default`` marks the output mpv uses when no name is written.
    """

    name: str
    hostapi: str
    is_default: bool = False


def hostapi_of(name: str) -> str:
    """Derive the host API label from an mpv device name.

    :param name: device name such as ``pipewire/alsa_output...`` or ``auto``.
    :return: the text before the first ``/``, stripped and lowercased; the whole
        name when it contains no slash.
    """
    return name.split("/", 1)[0].strip().lower()


def resolve_device(devices: Sequence[AudioDevice], hint: str) -> AudioDevice | None:
    """Pick the first device whose *name* contains *hint*, ignoring case.

    Only :attr:`AudioDevice.name` is matched, never the human-readable
    ``description``: the name is what the resolved device is written back as, so
    matching a description could resolve to a device the hint does not name. The
    first match wins when the hint is ambiguous, and a blank hint matches
    nothing.

    :param devices: devices reported by the backend, in the backend's order.
    :param hint: user-supplied name substring, taken from ``audio.device_hint``.
    :return: the matching device, or ``None`` for a blank or non-matching hint.
    """
    wanted = hint.strip().casefold()
    if wanted == "":
        return None
    for device in devices:
        if wanted in device.name.casefold():
            return device
    return None
