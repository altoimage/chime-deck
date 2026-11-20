"""Typed failures from the sound library's network and naming rules.

The API layer (HSD-w9eq and the sounds routes) maps :class:`SoundErrorCode` to
an HTTP status and shows :attr:`SoundDownloadError.message` verbatim, so every
message is a whole actionable sentence. Raw exception text and the submitted
URL are deliberately absent: the former is unhelpful to a user, the latter may
carry credentials or fragments the caller did not intend to echo.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SoundDownloadError", "SoundErrorCode"]


class SoundErrorCode(StrEnum):
    """Stable machine-readable reason a sound operation was rejected."""

    INVALID_URL = "invalid_url"
    INVALID_NAME = "invalid_name"
    SOUND_TOO_LARGE = "sound_too_large"
    SOUND_NOT_AUDIO = "sound_not_audio"
    SOUND_FORMAT_UNSUPPORTED = "sound_format_unsupported"
    DOWNLOAD_FAILED = "download_failed"


class SoundDownloadError(Exception):
    """One rejected download or name, with a safe user-facing message."""

    def __init__(
        self,
        code: SoundErrorCode,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Record the reason, the message to show and any structured detail.

        :param code: stable reason for programmatic handling.
        :param message: whole actionable sentence safe to show to a user.
        :param detail: optional structured context; never echoed automatically.
        """
        self.code: SoundErrorCode = code
        self.message: str = message
        self.detail: dict[str, object] = {} if detail is None else detail
        super().__init__(message)
