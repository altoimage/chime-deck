"""Speech engines behind one narrow asynchronous interface.

An engine is replaceable: :class:`TtsEngine` is the port the resolver
(:mod:`chime_deck.tts.speech`) depends on, :class:`EspeakNgEngine` (a local
subprocess), :class:`~chime_deck.tts.ha_tts.HaTtsEngine` (Home Assistant's
own voice engines) and :class:`~chime_deck.tts.remote_tts.RemoteTtsEngine`
(another doorbell in renderer mode, D24) are the implementations that exist
today, and :class:`NullTtsEngine` stands in for "speech is switched off" so
callers never special-case ``None``.

The interface is deliberately format-agnostic: an engine writes whatever bytes
its backend produces (16-bit PCM WAV from the piper engine added by HSD-p50k,
WAV from espeak-ng, whatever Home Assistant's engine returns — often MP3 — from
``ha_tts``) at exactly the ``destination`` path. There is no ``formats`` set to
keep in sync; the resolver asks for the format through
:attr:`TtsOptions.preferred_format` and trusts the engine. Two module-level
helpers serve every engine that writes a file itself:
:func:`sniff_container` reads the real container from the leading bytes, and
:func:`validate_rendered_audio` rejects a file that produced no sound.

``SpawnedProcess`` and ``ProcessSpawner`` are the test seam: unit tests inject a
fake spawner and never fork a real process, so the suite stays offline.

Owning ticket: HSD-bw8h; the ``remote_tts`` row was added by HSD-5de8.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import wave
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeAlias, cast, runtime_checkable
from uuid import uuid4

from .errors import TtsRenderError

__all__ = [
    "Availability",
    "EspeakInvocation",
    "EspeakNgEngine",
    "NullTtsEngine",
    "ProcessSpawner",
    "SpawnedProcess",
    "SynthesisTiming",
    "TtsEngine",
    "TtsOptions",
    "sniff_container",
    "validate_rendered_audio",
]

DEFAULT_VOICE: Final = "en-gb"
"""espeak-ng voice used when no voice is configured."""

DEFAULT_SPEED: Final = 160
"""espeak-ng words-per-minute used when no speed is configured."""

_MIN_AUDIBLE_S: Final = 0.05
"""Shortest WAV that counts as speech; espeak emits 8 ms for whitespace-only text."""

_MIN_NON_WAV_BYTES: Final = 128
"""Smallest non-WAV payload that can plausibly be audio rather than a stub."""

_TIMEOUT_S: Final = 6.0
"""Default render deadline; a phrase that takes longer than this is a fault.

Sized so one probe-then-espeak press fits **strictly below** the MQTT press
seam (:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S`, 10 s):
:data:`~chime_deck.tts.ha_tts.AVAILABILITY_TIMEOUT_S` (1.0 s) +
:data:`~chime_deck.tts.templating.TEMPLATE_TIMEOUT_S` (0.5 s) + this render +
:data:`_KILL_GRACE_S` (1.0 s) +
:data:`~chime_deck.tts.ha_tts.PRESS_MARGIN_S` (0.5 s) = 9.0 s. The number is
measured, not round: espeak-ng synthesises at an RTF near 0.01 on the ARM board
(6.2 s of audio in 0.077 s, recorded in HSD-bw8h), so 6 s covers about eight
minutes of speech and is roughly nine times the measured cold render. It must
stay below the seam because a seam expiry cancels the press before the press
log row is written — the silent doorbell of HSD-4y6l.
``tests/test_tts_engines.py`` asserts the sum; the press-wide deadline is
HSD-96k6.
"""

_KILL_GRACE_S: Final = 1.0
"""How long a killed process may take to exit before the render gives up on it.

The process has already been SIGKILLed, which cannot be masked, so this only
bounds reaping; the expiry is swallowed by :func:`contextlib.suppress` and can
never hang the press. One second is about thirteen times the measured whole
render (HSD-bw8h) and is what keeps the probe-then-espeak sum under the press
seam (HSD-4y6l).
"""

_STDERR_CLIP: Final = 200
"""Characters of engine stderr kept in an error message."""

_NO_OUTPUT_WITH_STDERR: Final = "{label} produced no output (stderr: {stderr})"
"""Message for a render that left no file at all."""

_INAUDIBLE: Final = "{label} produced no audible audio for this text"
"""Message for a file that exists but cannot be heard."""


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether an engine can speak right now.

    Invariant: when :attr:`ok` is ``False``, :attr:`reason` is a whole
    actionable sentence naming the fix, in the house style of
    :mod:`chime_deck.audio.errors`. A caller can therefore put the reason
    straight into the press log without adding advice of its own.
    """

    ok: bool
    reason: str | None = None


def _no_output_message(label: str, stderr_tail: str) -> str:
    """Build the message for a render that left no usable file behind.

    :param label: engine label shown to the user, normally the binary name.
    :param stderr_tail: already clipped stderr, ``none`` when the engine was silent.
    """
    return _NO_OUTPUT_WITH_STDERR.format(label=label, stderr=stderr_tail or "none")


def sniff_container(head: bytes) -> str | None:
    """Return the audio container the leading bytes of a file identify.

    Only the four containers this project can play are recognised. The check is
    deliberately a magic-number sniff rather than the requested format: a Home
    Assistant cloud engine may answer an MP3 for a ``.wav`` request, and the
    bytes are facts where the request is only a hope.

    :param head: first bytes of the file; 12 are enough for every signature.
    """
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def validate_rendered_audio(
    path: Path, *, container: str, label: str, stderr_tail: str = ""
) -> None:
    """Reject a render that exited cleanly but produced no audible audio.

    Two failure shapes are checked, both observed in practice: no file at all
    (espeak-ng exits 0 for an unwritable path) and a file that carries no sound
    (espeak-ng exits 0 with 8 ms of silence for whitespace-only text). A WAV is
    measured properly through :mod:`wave`; any other container can only be
    checked for a plausible size, because there is no decoder here.

    :param path: file the engine was asked to write.
    :param container: format token of the emitted bytes, from :func:`sniff_container`
        or the destination suffix.
    :param label: engine label shown to the user, normally the binary name.
    :param stderr_tail: already clipped stderr, used by the no-output message.
    :raises TtsRenderError: if the file is missing, empty, or silent.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise TtsRenderError(_no_output_message(label, stderr_tail)) from None
    if size == 0:
        raise TtsRenderError(_no_output_message(label, stderr_tail))
    if container == "wav":
        try:
            with wave.open(str(path), "rb") as reader:
                frames = reader.getnframes()
                rate = reader.getframerate()
        except (wave.Error, EOFError, OSError):
            raise TtsRenderError(_INAUDIBLE.format(label=label)) from None
        if frames <= 0 or rate <= 0 or frames / rate < _MIN_AUDIBLE_S:
            raise TtsRenderError(_INAUDIBLE.format(label=label))
    elif size < _MIN_NON_WAV_BYTES:
        raise TtsRenderError(_INAUDIBLE.format(label=label))


@dataclass(frozen=True, slots=True)
class TtsOptions:
    """Everything one render needs besides the text.

    The engine *name* is the cache-key ``engine`` argument and is a separate
    field from :attr:`engine_id`. ``engine_id`` is the **sub-engine** identifier
    only — for the Home Assistant engine that is the HA entity such as
    ``tts.piper`` — and is never the engine name.

    Settings are mapped per engine:

    ============== ============================================= ===============================
    engine         options                                       source key
    ``ha_tts``     ``engine_id``, ``language``, voice ``""``     ``tts.ha.engine_id``,
                   speed 0                                       ``tts.ha.language``
    ``piper``      ``voice``                                     ``tts.piper.voice``
    ``remote_tts`` no options                                    —
    ``http_tts``   ``engine_id`` (provider id + fingerprint),     ``tts.providers``,
                   ``voice``, ``language``,                      ``tts.http_tts.provider``
                   ``preferred_format``
    ``espeak_ng``  ``voice``, ``speed``                          ``tts.espeak_voice``,
                                                                 ``tts.espeak_speed``
    ============== ============================================= ===============================

    ``remote_tts`` maps no settings: the renderer applies its own
    ``tts.piper.voice`` and the client always asks for ``wav``, so every field
    stays at its default and the wire request carries only ``preferred_format``.

    ``http_tts`` reads the **selected provider record**: ``voice``, ``language``
    and ``preferred_format`` come from it, and ``engine_id`` is
    ``<provider id>@<fingerprint>`` so that editing a provider (a new base URL,
    voice or format) changes the cache key instead of serving stale audio.

    ``preferred_format`` is ``"wav"`` for every engine today; the field exists
    so the cache's suffix and the engine's output format cannot drift apart.
    """

    engine_id: str = ""
    language: str = ""
    voice: str = ""
    speed: int = 0
    preferred_format: str = "wav"


class TtsEngine(Protocol):
    """One way to turn text into an audio file."""

    @property
    def name(self) -> str:
        """Stable engine name used as the cache key's ``engine`` argument."""
        ...

    async def available(self) -> Availability:
        """Return whether this engine can speak now.

        This is called once per press, so it must never raise and must be time
        bounded; a network probe needs its own timeout inside the engine.
        """
        ...

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Write speech for *text* at exactly *destination* and return the path.

        The engine writes exactly at ``destination`` and nowhere else.
        Implementations may publish atomically through a sibling temporary file
        plus :func:`os.replace`, but the final path is ``destination``. The
        emitted bytes are whatever the backend produces (WAV, MP3, Opus); the
        interface is format-agnostic.

        :param text: phrase to speak.
        :param options: voice, speed and preferred format for this render.
        :param destination: exact output path, including its extension.
        :raises TtsRenderError: if no audible file was produced.
        """
        ...

    async def warmup(self) -> None:
        """Prepare the engine once at startup; a failure here is not fatal."""
        ...


@runtime_checkable
class SynthesisTiming(Protocol):
    """An engine that measures how long its last synthesis took.

    Kept separate from :class:`TtsEngine` so a third-party engine does not have
    to grow a measurement it does not keep, and read through
    :func:`isinstance` rather than ``getattr`` so a typo cannot silently answer
    ``None``.
    """

    @property
    def last_synthesis_ms(self) -> int | None:
        """Milliseconds the last successful synthesis took, or ``None``."""
        ...


class NullTtsEngine:
    """The engine used when speech is deliberately switched off (D27).

    Callers never special-case ``None``: this engine is always registered under
    the name ``none`` and reports why it cannot speak. It never probes anything,
    so the "disabled" state costs no subprocess and no network.
    """

    name: Final = "none"

    async def available(self) -> Availability:
        """Report the deliberate off state without probing anything."""
        return Availability(False, "speech is disabled (tts.default_engine is 'none')")

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Refuse to speak; the resolver should never have chosen this engine.

        :param text: phrase that would have been spoken.
        :param options: options that would have been used.
        :param destination: path that would have received the audio.
        :raises TtsRenderError: always.
        """
        raise TtsRenderError("speech is disabled; no audio was rendered")

    async def warmup(self) -> None:
        """Do nothing; there is nothing to prepare."""
        return None


class SpawnedProcess(Protocol):
    """The subset of :class:`asyncio.subprocess.Process` an engine uses."""

    @property
    def returncode(self) -> int | None:
        """Exit status, or ``None`` while the process is still running."""
        ...

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes | None]:
        """Feed *input* to stdin, close it, drain both pipes and wait.

        :param input: bytes written to stdin before it is closed, or ``None``.
        """
        ...

    async def wait(self) -> int:
        """Wait for the process to exit and return its status."""
        ...

    def kill(self) -> None:
        """Send SIGKILL; safe to call more than once."""
        ...


ProcessSpawner: TypeAlias = Callable[[Sequence[str]], Awaitable[SpawnedProcess]]
"""Starts one process for an argv, without a shell; injectable for tests."""


@dataclass(frozen=True, slots=True)
class EspeakInvocation:
    """One fully built espeak-ng command, ready to run."""

    argv: tuple[str, ...]
    stdin: bytes


class EspeakNgEngine:
    """espeak-ng executed as a subprocess, text only ever on stdin.

    The verified failure matrix makes exit-code checking alone insufficient;
    every row below was observed on espeak-ng 1.51:

    ========================== ==========================================
    input                      behaviour
    ========================== ==========================================
    empty stdin                exit 0, no file written
    whitespace-only text       exit 0, 396-byte file, 8 ms of silence
    unwritable output path     exit 0, stderr ``Can't write to: ...``
    ``-s 100000``              exit 0, 44-byte file, 0 frames
    unknown voice              exit 1, ``The specified espeak-ng voice
                               does not exist.``
    ========================== ==========================================

    A render therefore requires a non-zero exit status, an existing file with a
    plausible duration, and a WAV frame count above :data:`_MIN_AUDIBLE_S`.
    Otherwise the caller would log "queued" for a phrase that never made a
    sound — exactly the silence design 03 §7 forbids.
    """

    name: Final = "espeak_ng"

    def __init__(
        self,
        *,
        binary: str = "espeak-ng",
        timeout_s: float = _TIMEOUT_S,
        which: Callable[[str], str | None] = shutil.which,
        spawner: ProcessSpawner | None = None,
    ) -> None:
        """Create the engine; nothing runs until :meth:`render` is awaited.

        :param binary: executable name or path passed to the spawner.
        :param timeout_s: seconds one render may take before it is killed.
        :param which: lookup used by :meth:`available`; injectable for tests.
        :param spawner: process factory; ``None`` uses the real asyncio spawner.
        """
        self._binary = binary
        self._timeout_s = timeout_s
        self._which = which
        self._spawn: ProcessSpawner = spawner if spawner is not None else _default_spawn

    async def available(self) -> Availability:
        """Report whether the espeak-ng binary is on ``PATH``.

        No subprocess is started, so this is cheap enough to call per press and
        cannot hang. When the binary is missing the reason names the fix.
        """
        if self._which(self._binary) is not None:
            return Availability(True)
        return Availability(
            False, f"{self._binary} is not installed; install it with `apt install espeak-ng`"
        )

    def build_invocation(
        self, text: str, options: TtsOptions, destination: Path
    ) -> EspeakInvocation:
        """Build the argv and stdin for one render, without running anything.

        The text never appears in ``argv``: it is UTF-8 on stdin only, and
        ``--stdin`` is load-bearing because without it espeak reads the text
        line by line and drops everything after the first newline. The speed is
        passed verbatim; the resolver applies the registry default of 160.

        :param text: phrase to speak.
        :param options: voice and speed for this render.
        :param destination: exact output path espeak-ng must write.
        """
        voice = options.voice or DEFAULT_VOICE
        argv = (
            self._binary,
            "-v",
            voice,
            "-s",
            str(options.speed),
            "-w",
            str(destination),
            "--stdin",
        )
        return EspeakInvocation(argv=argv, stdin=text.encode("utf-8"))

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Render *text* to *destination*, publishing the file atomically.

        The file is written to a hidden sibling and moved into place only after
        the engine exited cleanly and the audio passed validation, so a
        concurrent reader never sees a partial file and the destination is never
        removed by a failed render.

        Every way out of the wait kills the subprocess and discards the temporary
        file: a deadline expiry gets the kill grace before the typed error, and a
        cancellation from outside — the press-wide deadline of HSD-96k6 or the
        MQTT seam — re-raises untouched after a kill that needs no grace, because
        SIGKILL cannot be masked.

        :param text: phrase to speak.
        :param options: voice and speed for this render.
        :param destination: exact output path.
        :raises TtsRenderError: if espeak-ng failed, timed out or produced no
            audible audio.
        """
        if not text.strip():
            raise TtsRenderError(f"{self._binary} has no text to speak")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        invocation = self.build_invocation(text, options, tmp)
        process = await self._spawn(invocation.argv)
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(input=invocation.stdin), timeout=self._timeout_s
            )
        except TimeoutError:
            self._kill(process)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), _KILL_GRACE_S)
            self._discard(tmp)
            raise TtsRenderError(
                f"{self._binary} did not finish within {self._timeout_s:g} s; the render was killed"
            ) from None
        except BaseException:
            # A cancellation (the press-wide deadline or the MQTT seam) must not leave
            # the engine subprocess running. SIGKILL cannot be masked, so this needs no
            # grace period; reaping belongs to the event loop's child watcher.
            self._kill(process)
            self._discard(tmp)
            raise
        clipped = _clip_stderr(stderr)
        code = process.returncode
        if code != 0:
            self._discard(tmp)
            raise TtsRenderError(
                f"{self._binary} exited with status {code}: {clipped or 'no error output'}"
            )
        try:
            validate_rendered_audio(
                tmp,
                container=destination.suffix.strip().lower().lstrip("."),
                label=self._binary,
                stderr_tail=clipped,
            )
        except TtsRenderError:
            self._discard(tmp)
            raise
        os.replace(tmp, destination)
        return destination

    async def warmup(self) -> None:
        """Do nothing; espeak-ng needs no preparation."""
        return None

    @staticmethod
    def _kill(process: SpawnedProcess) -> None:
        """Kill a process, tolerating one that already exited.

        :param process: process to kill.
        """
        with suppress(ProcessLookupError):
            process.kill()

    @staticmethod
    def _discard(path: Path) -> None:
        """Remove a temporary file, tolerating one that was never written.

        :param path: temporary file to remove.
        """
        with suppress(FileNotFoundError):
            path.unlink()


def _clip_stderr(stderr: bytes | None) -> str:
    """Decode and clip an engine's stderr for an error message.

    :param stderr: captured stderr bytes, or ``None`` when the pipe was empty.
    """
    if not stderr:
        return ""
    return stderr[:_STDERR_CLIP].decode("utf-8", errors="replace").strip()


async def _default_spawn(argv: Sequence[str]) -> SpawnedProcess:
    """Start one real subprocess for :class:`EspeakNgEngine`.

    :param argv: complete argv; never passed through a shell.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    return cast(SpawnedProcess, process)
