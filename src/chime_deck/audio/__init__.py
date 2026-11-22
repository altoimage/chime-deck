"""The audio engine: one replaceable speaker behind a narrow async interface.

Import the public API from this package rather than from individual modules: the
module layout is an implementation detail. ``MpvBackend`` is the default
implementation; ``AudioBackend`` is the protocol the playback worker (HSD-kfnd)
depends on. :func:`tone_wav_bytes` generates the verification tone the settings
page's "Play test tone" plays (HSD-wpm0).

Owning ticket: HSD-h13q; the verification tone was added by HSD-wpm0 and the
device's own volume control by cd-qul9.
"""

from __future__ import annotations

from .backend import AudioBackend
from .commands import (
    COMMAND_TIMEOUT_S,
    DEFAULT_SPAWN,
    CommandResult,
    CommandSpawner,
    SpawnedProcess,
    run_command,
)
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
from .hardware import (
    HARDWARE_NOT_STARTED_TEXT,
    NO_HARDWARE_CONTROL_TEXT,
    HardwareVolume,
    HardwareVolumeStatus,
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
    "COMMAND_TIMEOUT_S",
    "DEFAULT_SPAWN",
    "DEFAULT_START_TIMEOUT_S",
    "HARDWARE_NOT_STARTED_TEXT",
    "NO_HARDWARE_CONTROL_TEXT",
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
    "CommandResult",
    "CommandSpawner",
    "HardwareVolume",
    "HardwareVolumeStatus",
    "MissingAudioFileError",
    "MpvBackend",
    "MpvEvent",
    "MpvEventData",
    "MpvEventHandler",
    "MpvPlayer",
    "NoAudioDeviceError",
    "PlaybackError",
    "PlaybackInProgressError",
    "SpawnedProcess",
    "UnsupportedFormatError",
    "hostapi_of",
    "resolve_device",
    "run_command",
    "tone_wav_bytes",
    "warm_library",
    "write_test_tone",
]
