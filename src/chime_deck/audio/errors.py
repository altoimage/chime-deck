"""Typed errors raised by the audio engine.

Callers of :mod:`chime_deck.audio` never see a raw :mod:`ctypes`, libmpv or
:mod:`mpv` error. Every failure mode the backend can produce is represented by
one of these classes, so the queue worker (HSD-kfnd), the press history and the
settings page can turn a chime that did not ring into an explainable reason
without parsing driver text (design 03 §7: silence must always be explainable).

Every message is a whole sentence the user can act on.

Owning ticket: HSD-h13q.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "AudioBackendNotStarted",
    "AudioBackendUnavailable",
    "AudioError",
    "AudioOperationError",
    "AudioPermissionError",
    "MissingAudioFileError",
    "NoAudioDeviceError",
    "PlaybackError",
    "PlaybackInProgressError",
    "UnsupportedFormatError",
]


class AudioError(Exception):
    """Base class for every error raised by the audio engine."""


class AudioBackendUnavailable(AudioError):
    """Raised when python-mpv/libmpv is missing or the player cannot be constructed."""


class AudioBackendNotStarted(AudioError):
    """Raised when the backend is used before :meth:`start` or after :meth:`stop`."""


class PlaybackInProgressError(AudioError):
    """Raised when ``play()`` is called while another file is still playing.

    Queueing and overlap policy belong to the caller (HSD-kfnd), so the backend
    refuses rather than silently starting a second file; that keeps the failure
    visible in the press history instead of truncating the current chime.
    """


class AudioOperationError(AudioError):
    """Raised when one bounded worker call failed or timed out.

    This is not a playback failure: it means the audio engine could not get an
    answer from libmpv at all, so the caller can tell "the file is bad" apart
    from "the sound card stopped talking to us".
    """

    def __init__(self, operation: str, detail: str) -> None:
        """Record which operation failed and why.

        :param operation: short name of the worker call, for example ``"play"``.
        :param detail: technical explanation, appended to a user-facing sentence.
        """
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation} failed: {detail}")


class NoAudioDeviceError(AudioError):
    """Raised when an audio output is required but the backend reports none."""


class PlaybackError(AudioError):
    """Raised when mpv reports that a file could not be played."""

    def __init__(self, detail: str, code: int | None = None) -> None:
        """Record the user-facing sentence and the mpv error code.

        :param detail: whole sentence shown to the user, naming the file.
        :param code: numeric mpv/libmpv error code, when one was reported.
        """
        self.detail = detail
        self.code = code
        super().__init__(detail)


class MissingAudioFileError(PlaybackError):
    """Raised when the file to play is not on disk."""

    def __init__(self, path: Path) -> None:
        """Record the missing path.

        :param path: sound file that does not exist.
        """
        self.path = path
        super().__init__(f"audio file not found: {path}")


class UnsupportedFormatError(PlaybackError):
    """Raised when mpv cannot decode the file (observed codes ``-17`` and ``-18``)."""

    def __init__(self, path: Path, detail: str) -> None:
        """Record the file and what mpv said about it.

        :param path: sound file mpv refused to decode.
        :param detail: mpv's own description of the failure.
        """
        self.path = path
        super().__init__(
            f"cannot play {path}: {detail} — convert the file to WAV or MP3, "
            "or install the codec it needs"
        )


class AudioPermissionError(PlaybackError):
    """Raised when the sound card or the sound file cannot be accessed.

    Two different situations share this class and supply two different
    ``detail`` texts, because the advice the user needs is not the same: an
    unreadable file is about the file's own permissions, while ``-14``
    (``AO_INIT_FAILED``, assumed) is about the sound server and the ``audio``
    group membership of the service user. The ``audio``-group hint therefore
    never attaches to a file that simply could not be opened.
    """

    def __init__(self, detail: str) -> None:
        """Record the user-facing explanation.

        :param detail: whole sentence describing the permission problem and its fix.
        """
        super().__init__(detail)
