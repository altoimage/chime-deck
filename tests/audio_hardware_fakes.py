"""Offline fakes for the device's own volume commands (``amixer``/``pactl``).

:class:`RecordingSpawner` is the injection point for
:mod:`chime_deck.audio.commands`: it records every argv and answers from a
handler the test supplies, so no test ever forks ``amixer`` or ``pactl``
(AGENTS.md HARD RULE 4). The argless default answers a successful, empty command,
which the hardware service reads as "this output has no hardware volume control"
— harmless, honest and silent.

:class:`FakeMixer` and :class:`FakePulse` simulate the two utilities closely
enough to be useful: they answer the real output shapes, mutate their own state on
a write and record every write, so a test can assert both the command and its
effect without a sound card. :class:`FakePulse` can also list its sink with the
``alsa.*`` properties of the card behind it and answer ``amixer`` for that card,
which is the only seam needed to test what a Pulse write really reached.

:class:`FakeProcess` is deliberately small: ``communicate()`` returns the
configured pipes at once, unless the command is marked ``hang``, in which case it
waits for :meth:`FakeProcess.kill` — which is exactly what makes the timeout and
cancellation paths of :func:`chime_deck.audio.commands.run_command` testable
without a real sleep.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from chime_deck.audio import SpawnedProcess

__all__ = [
    "FakeCommand",
    "FakeControl",
    "FakeMixer",
    "FakeProcess",
    "FakePulse",
    "RecordingSpawner",
    "pulse_with_card",
]


@dataclass(frozen=True, slots=True)
class FakeCommand:
    """What one fake process prints and how it exits."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    hang: bool = False
    """When true, ``communicate()`` never returns until the process is killed."""


class FakeProcess:
    """An :class:`~chime_deck.audio.commands.SpawnedProcess` a test can observe."""

    def __init__(self, command: FakeCommand) -> None:
        """Create a process that will behave like *command*.

        :param command: pipes and exit status to report.
        """
        self.command = command
        self.killed = False
        self._killed = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        """Exit status; a hung process has none until it is killed."""
        if self.command.hang and not self.killed:
            return None
        return self.command.returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Return the configured pipes, or wait to be killed when the command hangs.

        :param input: bytes a real caller would write to stdin; ignored here.
        """
        if self.command.hang:
            await self._killed.wait()
        return self.command.stdout.encode(), self.command.stderr.encode()

    def kill(self) -> None:
        """Record the kill and release a hung ``communicate()``."""
        self.killed = True
        self._killed.set()

    async def wait(self) -> int:
        """Wait until the process is killed, then report SIGKILL."""
        await self._killed.wait()
        return -9


@dataclass
class FakeControl:
    """One simple-mixer control the fake ``amixer`` reports.

    ``write_percent`` is what ``sget`` reports *after* a write when set, which is
    how a device that does not land exactly on the requested value is simulated.
    """

    percent: int
    muted: bool = False
    has_volume: bool = True
    write_percent: int | None = None

    def sget(self, name: str) -> str:
        """Return the ``amixer sget`` text for this control.

        :param name: control name as printed by ``amixer``.
        """
        switch = "[off]" if self.muted else "[on]"
        if not self.has_volume:
            return (
                f"Simple mixer control '{name}',0\n"
                "  Capabilities: pswitch pswitch-joined\n"
                "  Playback channels: Mono\n"
                f"  Mono: Playback {switch}\n"
            )
        return (
            f"Simple mixer control '{name}',0\n"
            "  Capabilities: pvolume pswitch\n"
            "  Playback channels: Front Left - Front Right\n"
            f"  Front Left: Playback {self.percent} [{self.percent}%] [0.00dB] {switch}\n"
            f"  Front Right: Playback {self.percent} [{self.percent}%] [0.00dB] {switch}\n"
        )


@dataclass
class FakeMixer:
    """A fake ``amixer`` driven by a mapping of control name to state.

    Every ``sset`` is recorded in :attr:`writes` and mutates the control, exactly
    like the real tool, so a test can assert both the command and its effect. A
    pulse sink write moves the controls with :meth:`follow`, which records no
    write: the card followed the sink, the app did not ask amixer for anything.
    """

    controls: dict[str, FakeControl] = field(default_factory=dict)
    writes: list[tuple[str, int]] = field(default_factory=list)
    valid_cards: frozenset[str] | None = None
    """Card tokens ``-c`` accepts, or ``None`` to accept any token.

    This models ``amixer -c``'s own option parsing: alsa-lib resolves the token
    with ``snd_card_get_index``, which accepts a numeric index or the card *id*,
    never a card *name*, and rejects anything else with ``Invalid card number``
    before it ever attaches to the card. ``None`` is the permissive default, for
    tests that do not care which card amixer is asked for.
    """

    def follow(self, percent: int) -> None:
        """Move every playback control to the level a pulse sink write drives it to.

        A real card has no choice: raising the sink raises the hardware control
        behind it, which is exactly why a read-back can differ from the request.
        This is not an ``amixer`` write, so nothing is added to :attr:`writes` —
        the app never asked amixer for anything.

        :param percent: the percentage the sink behind this card was set to.
        """
        for control in self.controls.values():
            if control.has_volume:
                control.percent = (
                    percent if control.write_percent is None else control.write_percent
                )

    def __call__(self, argv: tuple[str, ...]) -> FakeCommand:
        """Answer one ``amixer`` argv.

        :param argv: recorded command line.
        """
        assert argv[0] == "amixer", argv
        arguments = list(argv[1:])
        if arguments[:1] == ["-c"]:
            card = arguments[1]
            if self.valid_cards is not None and card not in self.valid_cards:
                return FakeCommand(returncode=1, stderr=f"Invalid card number '{card}'.")
            arguments = arguments[2:]
        command = arguments[0]
        if command == "scontrols":
            return FakeCommand(
                stdout="".join(f"Simple mixer control '{name}',0\n" for name in self.controls)
            )
        name = arguments[1]
        control = self.controls.get(name)
        if control is None:
            return FakeCommand(
                returncode=1, stderr=f"amixer: Unable to find simple control '{name}',0"
            )
        if command == "sget":
            return FakeCommand(stdout=control.sget(name))
        assert command == "sset", command
        percent = int(arguments[2].rstrip("%"))
        self.writes.append((name, percent))
        control.percent = percent if control.write_percent is None else control.write_percent
        control.muted = False
        return FakeCommand(stdout=control.sget(name))


@dataclass
class FakePulse:
    """A fake ``pactl`` with one sink's volume and mute state.

    ``pactl list sinks`` prints no name on its command line, so this fake lists
    the one sink its :attr:`sink` field names, with the ``alsa.*`` properties in
    :attr:`alsa_card`/:attr:`alsa_id`/:attr:`alsa_path` and an ``amixer``
    :attr:`mixer` behind them — that is what lets a test exercise the mapping
    from a sink back to the card that really sets the level.
    """

    percent: int = 40
    muted: bool = False
    writes: list[tuple[str, ...]] = field(default_factory=list)
    sink: str = "alsa_output.usb-card"
    """The sink's own name: ``pactl list sinks`` must list the one being asked about."""
    listed: bool = True
    """When false, ``pactl list sinks`` prints nothing: the sink is not on the list."""
    alsa_card: str | None = None
    """The card's numeric index, advertised as ``alsa.card``; ``None`` omits it."""
    alsa_id: str | None = None
    """The card's id, advertised as ``alsa.id``; ``None`` omits it."""
    alsa_path: str | None = None
    """The PCM path, advertised as ``api.alsa.path``; ``None`` omits it.

    All three unset together means the sink is not ALSA-backed at all, so it
    advertises no ``alsa.*`` properties and can never map back to a card.
    """
    mixer: FakeMixer | None = None
    """The ``amixer`` fake backing the card, or ``None`` for a host without alsa-utils."""

    def __call__(self, argv: tuple[str, ...]) -> FakeCommand:
        """Answer one ``pactl`` (or ``amixer``) argv.

        :param argv: recorded command line.
        """
        if argv[0] == "amixer":
            if self.mixer is None:
                return FakeCommand(returncode=127, stderr="amixer: command not found")
            return self.mixer(argv)
        assert argv[0] == "pactl", argv
        command, sink, *rest = argv[1:]
        if command == "list":
            return FakeCommand(stdout=self.list_output())
        if command == "get-sink-volume":
            return FakeCommand(
                stdout=(
                    f"Volume: front-left: 26214 / {self.percent}% / -23.81 dB,  "
                    f"front-right: 26214 / {self.percent}% / -23.81 dB\n"
                )
            )
        if command == "get-sink-mute":
            return FakeCommand(stdout=f"Mute: {'yes' if self.muted else 'no'}\n")
        if command == "set-sink-volume":
            self.writes.append((command, sink, rest[0]))
            self.percent = int(rest[0].rstrip("%"))
            if self.mixer is not None:
                self.mixer.follow(self.percent)
            return FakeCommand()
        assert command == "set-sink-mute", argv
        self.writes.append((command, sink, rest[0]))
        self.muted = rest[0] != "0"
        return FakeCommand()

    def list_output(self) -> str:
        """Return a realistic ``pactl list sinks`` transcript for this one sink.

        The shape is what the service parses: a ``Sink #N`` header, a ``Name:``
        line, and a tab-indented ``key = "value"`` property block after
        ``Properties:``. An unlisted sink prints nothing at all.
        """
        if not self.listed:
            return ""
        properties = ['\t\tdevice.api = "alsa"']
        if self.alsa_card is not None or self.alsa_id is not None:
            if self.alsa_card is not None:
                properties.append(f'\t\talsa.card = "{self.alsa_card}"')
            properties.append('\t\talsa.device = "0"')
            if self.alsa_id is not None:
                properties.append(f'\t\talsa.id = "{self.alsa_id}"')
            properties.append('\t\tapi.alsa.card.name = "Fake Card"')
        if self.alsa_path is not None:
            properties.append(f'\t\tapi.alsa.path = "{self.alsa_path}"')
        mute = "yes" if self.muted else "no"
        lines = [
            "Sink #0",
            "\tState: RUNNING",
            f"\tName: {self.sink}",
            "\tDescription: Fake sink",
            f"\tMute: {mute}",
            "\tVolume: front-left: 26214 / "
            f"{self.percent}% / -23.81 dB,   front-right: 26214 / {self.percent}% / -23.81 dB",
            "\t        balance 0.00",
            "\tFlags: HARDWARE HW_MUTE_CTL HW_VOLUME_CTL DECIBEL_VOLUME LATENCY",
            "\tProperties:",
            *properties,
            "\tFormats:",
            "\t\tpcm",
        ]
        return "\n".join(lines) + "\n"


def pulse_with_card(
    pulse_percent: int, card: Mapping[str, FakeControl], *, muted: bool = False
) -> FakePulse:
    """Return a fake pulse sink that maps back to one ALSA card.

    The card is advertised the way PipeWire prints a USB DAC (``alsa.card = "1"``,
    ``alsa.id = "Device"``) and backed by a :class:`FakeMixer` built from *card*,
    so the service reads the sink first and the card's own control second.

    :param pulse_percent: the sink's own level, the value ``pactl`` reports.
    :param card: control name → state for the backing card's fake ``amixer``.
    :param muted: whether the sink starts muted.
    """
    return FakePulse(
        percent=pulse_percent,
        muted=muted,
        alsa_card="1",
        alsa_id="Device",
        mixer=FakeMixer(dict(card)),
    )


class RecordingSpawner:
    """A :data:`~chime_deck.audio.CommandSpawner` that records and never forks.

    ``handler`` maps one argv to the :class:`FakeCommand` it should behave like;
    raising :class:`FileNotFoundError` from it reproduces a missing binary. The
    default handler answers a successful empty command for every argv.
    """

    def __init__(self, handler: Callable[[tuple[str, ...]], FakeCommand] | None = None) -> None:
        """Create the spawner.

        :param handler: per-argv response; ``None`` answers success with no output.
        """
        self.calls: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []
        self._handler = handler

    async def __call__(self, argv: Sequence[str]) -> SpawnedProcess:
        """Record *argv* and return the fake process it should become.

        :param argv: complete command line the service wanted to run.
        """
        call = tuple(argv)
        self.calls.append(call)
        command = FakeCommand() if self._handler is None else self._handler(call)
        process = FakeProcess(command)
        self.processes.append(process)
        return process

    def argv_matching(self, *parts: str) -> list[tuple[str, ...]]:
        """Return every recorded argv that contains all of *parts*.

        :param parts: substrings that must all appear in the argv.
        """
        return [call for call in self.calls if all(part in call for part in parts)]
