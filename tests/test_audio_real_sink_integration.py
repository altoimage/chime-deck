"""Real libmpv plays the shipped tone into this host's real sound-server sink.

The test generates the shipped verification tone into ``tmp_path`` with
:func:`~chime_deck.audio.tone.write_test_tone`, plays it through the real
:class:`~chime_deck.audio.MpvBackend` with **no** ``ao`` override — production
passes no ``ao`` either, so libmpv resolves the real device — and asserts two
things:

(a) the end-file event fired. That is exactly what ``MpvBackend.play()``
    returning means: it awaits the pending future the ``end-file`` handler
    resolves, and raises on an ``_END_FILE_ERROR`` payload or on a lost event.
(b) a real sound-server sink was observed in ``RUNNING`` while the tone played.
    This is the observation an ``--ao=null`` regression cannot fake: a null
    output never makes a real sink leave ``IDLE``, so the test fails instead of
    passing silently.

What it does **not** prove: that a human heard anything. A sink in ``RUNNING``
plus a file played to end is not an audition, and "playback did not error" must
never be reported as audio verification (design 07 §3). Human confirmation is
tracked separately in the owner-assisted ticket HSD-fye7.

The level is asserted, not incidental. :data:`TONE_AMPLITUDE` (0.30 peak) is
-10.5 dBFS at unity gain and -18.4 dBFS at the ``_PROBE_VOLUME = 40`` used here
(``20 * log10(0.30 * 0.40)``), which is the level the owner verified as audible.
An earlier probe at amplitude 0.06 and volume 25 (~-36.5 dBFS) was inaudible and
produced a false "the routing is broken" conclusion, so a later edit to
``tone.py`` or to ``_PROBE_VOLUME`` must not silently quieten this probe back
into uselessness.

``MPV_AO`` is ignored on purpose: ``scripts/test-integration.sh`` exports
``MPV_AO=null`` for the audio-format tier, and a test that inherited that would
verify a null sink, not a speaker.

The skip conditions are collection-time, so a normal run stays silent: no tone is
generated, no libmpv is loaded and ``pactl`` is never executed unless the opt-in
variable is set and ``pactl`` is installed.

Owning ticket: HSD-0n9z.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import contextlib
import locale
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from chime_deck.audio import AudioBackendUnavailable, MpvBackend
from chime_deck.audio.tone import TONE_AMPLITUDE, write_test_tone

_HARNESS_HINT: Final = (
    "set CHIMEDECK_TEST_REAL_SINK=1 for the integration suite: "
    "CHIMEDECK_TEST_REAL_SINK=1 ./scripts/test-integration.sh --board "
    "tests/test_audio_real_sink_integration.py, or the same variable with "
    "uv run pytest on a host with a speaker"
)

_SKIP_REASON: Final = f"real-sink playback is opt-in because it is audible; {_HARNESS_HINT}"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("CHIMEDECK_TEST_REAL_SINK") != "1",
        reason=_SKIP_REASON,
    ),
    pytest.mark.skipif(
        shutil.which("pactl") is None,
        reason=f"pactl is needed to observe the sink state; {_HARNESS_HINT}",
    ),
]

_PROBE_VOLUME: Final = 40
"""mpv volume for the probe: the level the owner verified as audible (see module docstring)."""

_SINK_RUNNING: Final = "RUNNING"
"""``pactl list sinks short`` state that means a stream really opened the sink."""

_DBFS_FLOOR: Final = -20.0
"""Lower bound of the asserted probe level, in dBFS."""

_DBFS_CEILING: Final = -16.0
"""Upper bound of the asserted probe level, in dBFS."""

_PLAY_TIMEOUT_S: Final = 5.0
"""Backend end-file backstop: 12x the 0.4 s tone, so a hung sink fails, never hangs."""

_START_AWAIT_S: Final = 8.0
"""Bound for ``start()``; longer than the backend's own 5 s bound so its typed error wins."""

_PLAY_AWAIT_S: Final = 8.0
"""Bound for ``play()``; longer than the backend's own backstop so its typed error wins."""

_STOP_AWAIT_S: Final = 8.0
"""Bound for ``stop()``; longer than the backend's own 2 s bound so its typed error wins."""

_PACTL_TIMEOUT_S: Final = 2.0
"""Bound for one ``pactl`` call; a wedged sound server must not hold the suite."""

_PACTL_JOIN_EXTRA_S: Final = 1.0
"""Slack added to the watcher join, so one bounded poll can finish before it is cancelled."""

_SINK_WATCH_DEADLINE_S: Final = 8.0
"""Wall-clock cap on the sink watch, independent of the poll count."""

_MAX_POLLS: Final = 400
"""Attempt cap on the sink watch, so a slow ``pactl`` cannot stretch the deadline."""

_POLL_INTERVAL_S: Final = 0.01
"""Pause between polls: short against the 0.4 s tone, so ``RUNNING`` is not missed."""


async def _run_pactl(*args: str) -> tuple[int, str]:
    """Run ``pactl`` with *args* and return its exit status and standard output.

    The child is always reaped: on the timeout or a cancellation the pipes are
    drained after the kill, because under ``filterwarnings = ["error"]`` a leaked
    subprocess transport is a test failure, not a note.

    :param args: arguments passed verbatim to ``pactl``.
    :return: exit status and decoded standard output (undecodable bytes replaced).
    :raises TimeoutError: if the call outlives :data:`_PACTL_TIMEOUT_S`.
    """
    proc = await asyncio.create_subprocess_exec(
        "pactl",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PACTL_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise
    # communicate() returns only once the child is reaped, so the status is set;
    # a missing one is reported as a failed poll instead of an exception.
    returncode = proc.returncode if proc.returncode is not None else -1
    return returncode, stdout.decode(errors="replace")


def _parse_sink_states(text: str) -> dict[str, str]:
    """Map every sink name in ``pactl list sinks short`` output to its state.

    The short format is one tab-separated line per sink: index, name, driver,
    sample spec and the state in the last field.

    :param text: standard output of ``pactl list sinks short``.
    :return: sink name to upper-cased state; blank and malformed lines are skipped.
    """
    states: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        name = fields[1].strip()
        state = fields[-1].strip().upper()
        if name and state:
            states[name] = state
    return states


async def _default_sink() -> str | None:
    """Return the sound server's default sink name, or ``None`` when it cannot be read.

    Best effort on purpose: the watch prefers the default sink but can still fall
    back to any sink, so a missing answer must not fail the test.

    :return: the default sink name, or ``None`` when the query fails or is empty.
    """
    try:
        returncode, text = await _run_pactl("get-default-sink")
    except (TimeoutError, OSError):
        return None
    if returncode != 0:
        return None
    name = text.strip()
    return name or None


@dataclass(slots=True)
class _SinkWatch:
    """State collected by the sink watcher, and the assertions read from it."""

    default_sink: str | None
    running: dict[str, int] = field(default_factory=dict)
    """Sink name to the number of polls that saw it in ``RUNNING``."""

    last_seen: dict[str, str] = field(default_factory=dict)
    """Sink name to the last state observed, for a diagnostic when nothing ran."""

    polls: int = 0
    """Polls attempted, so the diagnostic can distinguish "never polled" from "no state"."""

    def saw_running(self) -> bool:
        """Whether a sink was ever seen in ``RUNNING``.

        The default sink is preferred, because that is the one a production play
        would use; when it could not be read, any sink counts.

        :return: ``True`` when the watched sink (or any sink) reported ``RUNNING``.
        """
        if self.default_sink is not None:
            return self.default_sink in self.running
        return bool(self.running)

    def describe(self) -> str:
        """Return a one-line diagnostic of what the watcher actually observed.

        :return: default sink, poll count and every sink's last state, sorted.
        """
        states = ", ".join(f"{name}={state}" for name, state in sorted(self.last_seen.items()))
        return (
            f"default sink: {self.default_sink or 'unknown'}; "
            f"polls: {self.polls}; last seen: {states or 'nothing'}"
        )


async def _watch_for_running(watch: _SinkWatch, finished: asyncio.Event) -> None:
    """Poll ``pactl`` until a sink reports ``RUNNING``, the watch ends, or a poll fails.

    Every exit is bounded: by :data:`_MAX_POLLS`, by
    :data:`_SINK_WATCH_DEADLINE_S`, by *finished* being set, or by a failed poll.
    A failure is not raised — it records nothing and stops, so the assertion in
    the test reports what was (not) observed instead of an opaque ``pactl`` error.

    :param watch: state to fill in; read by the test's assertion.
    :param finished: set when playback is over, which ends the watch after at most
        one more bounded poll.
    """
    deadline = time.monotonic() + _SINK_WATCH_DEADLINE_S
    while True:
        if watch.polls >= _MAX_POLLS or time.monotonic() >= deadline:
            return
        if finished.is_set() or watch.saw_running():
            return
        watch.polls += 1
        try:
            returncode, text = await _run_pactl("list", "sinks", "short")
        except (TimeoutError, OSError):
            return
        if returncode != 0:
            return
        states = _parse_sink_states(text)
        if states:
            watch.last_seen.update(states)
        for name, state in states.items():
            if state == _SINK_RUNNING:
                watch.running[name] = watch.running.get(name, 0) + 1
        await asyncio.sleep(_POLL_INTERVAL_S)


async def test_real_sink_reaches_running_while_the_tone_plays(tmp_path: Path) -> None:
    """The shipped tone plays to end through the real device, and a real sink runs.

    :param tmp_path: directory the verification tone is generated into.
    """
    default = await _default_sink()

    # Headless hosts and containers without a sound server are a skip, never a
    # failure: this test observes a sink, so no sink means nothing to observe.
    returncode, text = await _run_pactl("list", "sinks", "short")
    if returncode != 0:
        pytest.skip(f"no sound server is reachable from this process; {_HARNESS_HINT}")
    if not _parse_sink_states(text):
        pytest.skip(f"this host reports no audio output device; {_HARNESS_HINT}")

    # The level is part of the contract: see the module docstring for the earlier
    # inaudible probe that made a working route look broken.
    level_dbfs = 20 * math.log10(TONE_AMPLITUDE * _PROBE_VOLUME / 100)
    assert _DBFS_FLOOR <= level_dbfs <= _DBFS_CEILING, (
        f"the probe plays at {level_dbfs:.1f} dBFS "
        f"(20*log10(TONE_AMPLITUDE * _PROBE_VOLUME / 100) = "
        f"20*log10({TONE_AMPLITUDE} * {_PROBE_VOLUME} / 100)); HSD-0n9z requires the "
        f"~-18 dBFS level the owner verified as audible, so this probe cannot be "
        f"quietened into a test that proves nothing"
    )

    tone = write_test_tone(tmp_path)

    # python-mpv pins the process-wide LC_NUMERIC to C when _default_player_factory
    # imports it inside start(); restore it afterwards (same reason and pattern as
    # tests/test_audio_formats_integration.py).
    saved_numeric_locale = locale.setlocale(locale.LC_NUMERIC)
    # No mpv_options and no ``ao``: production passes none, so libmpv resolves the
    # real device. An ``ao`` here would be exactly the regression this test exists
    # to catch.
    backend = MpvBackend(play_timeout=_PLAY_TIMEOUT_S)
    try:
        try:
            await asyncio.wait_for(backend.start(), timeout=_START_AWAIT_S)
        except AudioBackendUnavailable as exc:
            pytest.skip(f"libmpv is not available on this host ({exc}); {_HARNESS_HINT}")

        recorded: list[str] = []
        finished = asyncio.Event()
        watch = _SinkWatch(default_sink=default)
        watcher = asyncio.create_task(_watch_for_running(watch, finished))
        try:
            # The awaited play() is load-bearing: returning from it means the
            # end-file event fired, and it raises on ``_END_FILE_ERROR``. The
            # ``when_done`` callback fires on failure too, so ``recorded ==
            # ["done"]`` alone would prove nothing.
            await asyncio.wait_for(
                backend.play(
                    tone,
                    volume=_PROBE_VOLUME,
                    when_done=lambda: recorded.append("done"),
                ),
                timeout=_PLAY_AWAIT_S,
            )
            assert backend.playing is False
            assert recorded == ["done"], (
                "the end-file event never reached the playback callback "
                f"(recorded {recorded!r}); play() returned without it"
            )
        finally:
            # No cancel(): the watch notices the event after at most one bounded
            # poll, so no pactl child is orphaned and no cancellation is needed.
            finished.set()
            await asyncio.wait_for(watcher, timeout=_PACTL_TIMEOUT_S + _PACTL_JOIN_EXTRA_S)
    finally:
        await asyncio.wait_for(backend.stop(), timeout=_STOP_AWAIT_S)
        locale.setlocale(locale.LC_NUMERIC, saved_numeric_locale)

    assert watch.saw_running(), (
        f"no sink ever reported {_SINK_RUNNING} while the tone played ({watch.describe()}). "
        "That means one of: an --ao=null (or other non-device) output regression, an "
        "absent or muted output device, or a sound server that never opened the device. "
        "Note this assertion is about the sink, not about anyone hearing the tone "
        "(HSD-fye7)."
    )
