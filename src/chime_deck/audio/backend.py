"""The narrow speaker interface the playback worker (HSD-kfnd) talks to.

Design 03 §1.1 defines this protocol so a second implementation (for example a
``sounddevice`` backend) can be dropped in without touching callers. Playback is
always local — the device that owns the speaker runs the app (D24) — so there is
no remote or browser-audio variant.

Owning ticket: HSD-h13q.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .devices import AudioDevice

__all__ = ["AudioBackend"]


class AudioBackend(Protocol):
    """One replaceable speaker: start it, play files through it, stop it.

    Only one file plays at a time. ``play()`` returns when the file has really
    finished (the libmpv end-file event), so the caller's queue never overlaps
    two chimes; queueing and overlap policy are the caller's job (HSD-kfnd), not
    the backend's.
    """

    @property
    def playing(self) -> bool:
        """Whether a file is currently playing."""
        ...

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Create the player and select the configured output device.

        Idempotent. Must be called before any other method except :meth:`stop`.

        :param timeout_s: budget for this start in seconds, because starting a
            player is legitimately slower than running one command; ``None``
            uses the implementation's own default.
        :raises AudioError: if the backend cannot be constructed.
        """
        ...

    async def stop(self) -> None:
        """Release the player and every resource behind it.

        Idempotent, and a no-op when the backend was never started. An in-flight
        :meth:`play` returns normally.
        """
        ...

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Play one file and return when it has finished.

        ``when_done`` is a compatible superset of design 03 §1.1's required
        parameter: it is optional here, and when supplied it runs exactly once on
        the event-loop thread — on success and on failure — because the caller
        uses it to release its queue slot. Exceptions from it are logged, never
        raised, so one broken callback cannot break playback.

        ``lead_in_ms`` is how many milliseconds of silence the backend should
        prepend, so a sleeping output wakes up on silence instead of on the first
        syllable of the chime. The caller decides the value, because only it
        knows whether the speaker has been idle long enough to have slept: the
        backend cannot tell an output that is asleep from one that is awake.

        :param path: sound file to play; it must exist and be readable.
        :param volume: playback volume 0-100; values outside are clamped and logged.
        :param lead_in_ms: milliseconds of silence to prepend so a sleeping
            output is awake before the sound starts; ``0`` asks for none.
        :param when_done: optional callback run once when playback ends.
        :returns: the milliseconds of silence actually prepended — ``0`` when
            ``lead_in_ms`` was ``0`` or the platform could not honour it — so a
            caller can record the truth in the press log instead of the intent.
        :raises AudioError: if the file is missing, unreadable, undecodable, or
            another file is still playing.
        """
        ...

    async def stop_playback(self) -> None:
        """Stop the current file and stay usable for the next one.

        Used when the audio device changes or on shutdown. A no-op when nothing
        is playing; the interrupted :meth:`play` returns normally.
        """
        ...

    async def set_device(self, hint: str) -> None:
        """Resolve a device hint and select it, falling back to the system default.

        :param hint: name substring from ``audio.device_hint``; blank or unmatched
            falls back to :data:`~chime_deck.audio.devices.SYSTEM_DEFAULT_DEVICE`.
        :raises AudioError: if the host reports no audio output device at all.
        """
        ...

    async def list_devices(self) -> list[AudioDevice]:
        """Return the audio outputs the backend can see.

        :return: devices in the backend's order, exactly one flagged default.
        """
        ...
