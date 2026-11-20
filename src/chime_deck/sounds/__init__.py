"""The sound library: folder indexing, URL downloads and duration probing.

This package — not :class:`~chime_deck.store.ConfigStore` methods — is the
deliberate seam for design 01 §4's ``rescan_sounds()`` and
``add_sound_from_url()``. The store stays a pure SQL repository: all SQL still
flows through it, it owns no files on disk and it never imports ``ffprobe`` or
``httpx``. The API layer calls :meth:`~SoundLibrary.scan` and
:meth:`~SoundDownloader.download` instead.

Dependencies point one way: :mod:`chime_deck.sounds.library` never imports
:mod:`chime_deck.sounds.download`, so the scanner can be used without an
HTTP client and the downloader can reuse the scanner's format list and lock.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

from .download import (
    DOWNLOAD_TIMEOUT_S,
    MAX_DOWNLOAD_BYTES,
    MAX_NAME_STEM,
    DownloadResult,
    SoundDownloader,
)
from .errors import SoundDownloadError, SoundErrorCode
from .library import (
    SCAN_BUDGET_S,
    SUPPORTED_FORMATS,
    DurationProbe,
    ScanReport,
    SoundLibrary,
    bundled_demo_sounds_dir,
    copy_bundled_demo_sounds,
    sound_file_path,
)
from .probe import probe_duration_ms

__all__ = [
    "DOWNLOAD_TIMEOUT_S",
    "MAX_DOWNLOAD_BYTES",
    "MAX_NAME_STEM",
    "SCAN_BUDGET_S",
    "SUPPORTED_FORMATS",
    "DownloadResult",
    "DurationProbe",
    "ScanReport",
    "SoundDownloadError",
    "SoundDownloader",
    "SoundErrorCode",
    "SoundLibrary",
    "bundled_demo_sounds_dir",
    "copy_bundled_demo_sounds",
    "probe_duration_ms",
    "sound_file_path",
]
