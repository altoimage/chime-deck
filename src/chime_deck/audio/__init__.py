"""The audio engine: one replaceable speaker behind a narrow async interface.

Import the public API from this package rather than from individual modules: the
module layout is an implementation detail. ``MpvBackend`` is the default
implementation; ``AudioBackend`` is the protocol the playback worker (HSD-kfnd)
depends on. :func:`tone_wav_bytes` generates the verification tone the settings
page's "Play test tone" plays (HSD-wpm0).

Owning ticket: HSD-h13q; the verification tone was added by HSD-wpm0.
"""

from __future__ import annotations

from .backend import AudioBackend
from .devices import SYSTEM_DEFAULT_DEVICE, AudioDevice, hostapi_of, resolve_device
from .errors import (
    AudioBackendNotStarted,
    AudioBackendUnavailable,
    AudioError,
    AudioOperationError,
    AudioPermissionError,
    MissingAudioFileError,
    NoAudioDeviceError,
    PlaybackError,
    PlaybackInProgressError,
    UnsupportedFormatError,
)
from .mpv_backend import (
    DEFAULT_START_TIMEOUT_S,
    MpvBackend,
    MpvEvent,
    MpvEventData,
    MpvEventHandler,
    MpvPlayer,
    warm_library,
)
from .tone import (
    TONE_AMPLITUDE,
    TONE_DURATION_S,
    TONE_FADE_S,
    TONE_FILENAME,
    TONE_FREQUENCY_HZ,
    TONE_SAMPLE_RATE,
    tone_wav_bytes,
    write_test_tone,
)

__all__ = [
    "DEFAULT_START_TIMEOUT_S",
    "SYSTEM_DEFAULT_DEVICE",
    "TONE_AMPLITUDE",
    "TONE_DURATION_S",
    "TONE_FADE_S",
    "TONE_FILENAME",
    "TONE_FREQUENCY_HZ",
    "TONE_SAMPLE_RATE",
    "AudioBackend",
    "AudioBackendNotStarted",
    "AudioBackendUnavailable",
    "AudioDevice",
    "AudioError",
    "AudioOperationError",
    "AudioPermissionError",
    "MissingAudioFileError",
    "MpvBackend",
    "MpvEvent",
    "MpvEventData",
    "MpvEventHandler",
    "MpvPlayer",
    "NoAudioDeviceError",
    "PlaybackError",
    "PlaybackInProgressError",
    "UnsupportedFormatError",
    "hostapi_of",
    "resolve_device",
    "tone_wav_bytes",
    "warm_library",
    "write_test_tone",
]
