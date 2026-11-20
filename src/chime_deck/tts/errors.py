"""Typed errors raised by the speech engines.

The press path must never see a raw :mod:`subprocess`, :mod:`wave` or engine
failure: every render that produced no audio is reported as one of these
classes, so the press history can say *why* a button stayed silent instead of
showing a traceback. Engine failures become explainable ``press_log`` reasons.

Every message is a whole sentence the user can act on.

:class:`VoiceError` extends the same rule to local Piper voice downloads: it
carries a stable :class:`VoiceErrorCode` and the machine-readable facts beside
an actionable sentence, so the settings page never has to parse prose.

Owning ticket: HSD-bw8h; :func:`clean_detail` was added by HSD-npt0, the
voice download errors by HSD-p50k, :class:`BakeNotApplicable` by HSD-dzek and
:attr:`TtsRenderError.transient` by HSD-96k6.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..config import SECRET_SCRUBBER

__all__ = [
    "BakeNotApplicable",
    "TtsError",
    "TtsRenderError",
    "VoiceError",
    "VoiceErrorCode",
    "clean_detail",
]

_DETAIL_CLIP: Final = 200
"""Characters of engine or Home Assistant text kept in one error message."""


def clean_detail(text: str) -> str:
    """Collapse whitespace, scrub registered secrets and clip a detail line.

    Text taken from an engine, a response body or an exception goes through here
    before it enters a message a user can see, so a credential that a backend
    echoed cannot travel any further.

    Three deliberate near-copies of this rule exist today and are **not** yet
    consolidated: :func:`chime_deck.ha.client._clean` (200-character body
    clip), :func:`chime_deck.ha.websocket._clean` and
    :func:`chime_deck.mqtt.bridge._clean`. Both of the latter replace their
    own configured secret with a literal before scrubbing, because the scrubber
    may not have registered it yet. Merging them is future work: each has a
    different clip and its own literal secret, so the consolidating helper has
    to take both as arguments. A fourth near-copy,
    :func:`chime_deck.tts.templating.sanitise_render`, collapses whitespace,
    strips, drops control and format characters, and caps at 500 — but it does
    **not** use :data:`~chime_deck.config.SECRET_SCRUBBER`: the caller
    scrubs Home Assistant-derived fragments through this function before they
    enter a message, so the scrub already happened by then.

    :param text: text from an engine, a response body or an exception.
    """
    return SECRET_SCRUBBER.scrub(" ".join(text.split()))[:_DETAIL_CLIP]


class TtsError(Exception):
    """Base class for every error raised by the speech package."""


class TtsRenderError(TtsError):
    """Raised when an engine could not turn text into an audio file.

    The message names the engine, what it did wrong and, where the engine said
    something useful, the clipped stderr tail. Callers translate this into a
    :class:`~chime_deck.press.SpeechUnavailable` so the press log gets a
    readable reason rather than an exception.

    :attr:`transient` says whether repeating the same render could succeed — a
    transport failure, a timeout or a server-side fault (HTTP 408/429/5xx) — as
    opposed to a failure that needs a setting, a token or a name to change first
    (auth, bad request, not found, a bad address, a local disk fault). It is
    decided **at the engine seam**, where the failure is classified, and carried
    here so the resolver can decide about one bounded second attempt without
    re-deriving it from a :class:`~chime_deck.ha.HaError`.
    """

    def __init__(self, sentence: str, *, transient: bool = False) -> None:
        """Record the sentence to show and whether a retry could succeed.

        :param sentence: whole actionable sentence safe to show to a user.
        :param transient: whether repeating the same render could succeed.
        """
        self.transient: bool = transient
        super().__init__(sentence)


class BakeNotApplicable(TtsError):
    """Raised when a phrase cannot be pre-rendered at all (HSD-dzek).

    A templated phrase is dynamic by definition (design 04 §2.2), and a non-TTS
    or blank-text button has nothing to speak, so neither is a synthesis that
    failed: it is a request that does not apply. That distinction is why this is
    a refusal and not a failure, and why it is never written to ``baked_error``
    — a button that cannot be baked has not earned a warning in the UI.
    """


class VoiceErrorCode(StrEnum):
    """Why a piper voice download or install was refused."""

    INVALID_VOICE = "invalid_voice"  # malformed, or could name a path outside voices/
    VOICE_NOT_SUGGESTED = "voice_not_suggested"  # well-formed but not in the curated table
    VOICE_UNAVAILABLE = "voice_unavailable"  # D22 RAM gate, missing extra, speech disabled
    VOICE_TOO_LARGE = "voice_too_large"  # over the per-file byte cap
    VOICE_CORRUPT = "voice_corrupt"  # truncated, checksum mismatch, unparsable config
    DOWNLOAD_FAILED = "download_failed"  # transport-level failure, no usable answer


class VoiceError(TtsError):
    """A refused or failed voice download, with a stable code and detail fields.

    Invariant: the message is one whole sentence a user can act on, and
    :attr:`detail` carries the machine-readable facts (expected/actual digest,
    byte counts, the voice id) so the UI and the job record can show the
    diagnosis without parsing prose.
    """

    def __init__(
        self,
        code: VoiceErrorCode,
        sentence: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Record the reason, the sentence to show and any structured detail.

        :param code: stable reason for programmatic handling.
        :param sentence: whole actionable sentence safe to show to a user.
        :param detail: optional structured context; never echoed automatically.
        """
        self.code: VoiceErrorCode = code
        self.detail: Mapping[str, object] = MappingProxyType({} if detail is None else dict(detail))
        super().__init__(sentence)
