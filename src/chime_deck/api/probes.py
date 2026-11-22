"""Connection probes behind the settings page's "test" buttons.

The settings API asks these for a verdict; it never talks to Home Assistant, an
MQTT broker or the sound card itself. All three probes are live. The Home
Assistant probe asks the REST client for the entity list and reports what came
back. The MQTT probe opens one short paho connection, reads the CONNACK and closes
it again, so the verdict is the broker's own answer. The audio probe plays the
generated verification tone through the started backend and reports the device it
resolved and, when the measurement can back it, the measured start latency.

The MQTT probe does its blocking paho work (DNS, TCP, the TLS handshake and the
wait for a CONNACK) on a daemon thread of its own, :class:`_ProbeWorker`, so the
event loop is never blocked by it. That attempt has its own budget,
:data:`MQTT_PROBE_TIMEOUT_S`, deliberately below the route's outer
:func:`asyncio.timeout` so the probe's typed sentence wins over an outer
cancellation.

The ``error`` field of every result is user-facing text, shown verbatim in the
settings page, so it must be a whole sentence the user can act on.

A probe must answer within :data:`PROBE_TIMEOUT_S`; the route enforces that with
:func:`asyncio.timeout`, so an implementation may block for as long as it likes
and will still be cancelled.

Owning ticket: HSD-91z1; the real Home Assistant probe landed in HSD-qr3k, the
real audio probe in HSD-wpm0 and the real MQTT probe in HSD-8qf2.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import InvalidStateError
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import httpx

from ..audio import (
    SYSTEM_DEFAULT_DEVICE,
    TONE_DURATION_S,
    AudioBackend,
    AudioError,
    resolve_device,
    write_test_tone,
)
from ..ha import HaError, HomeAssistantClient
from ..mqtt import (
    DEFAULT_KEEPALIVE_S,
    ClientFactory,
    MqttConfigurationError,
    MqttSettings,
    default_client_factory,
    load_mqtt_settings,
)
from .audio import AUDIO_NOT_STARTED_TEXT

if TYPE_CHECKING:
    from paho.mqtt.reasoncodes import ReasonCode

    from ..store import ConfigStore

__all__ = [
    "HA_PROBE_TIMEOUT_S",
    "MQTT_PROBE_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "AudioProbeResult",
    "ConnectionProbes",
    "HaProbeCandidate",
    "HaProbeResult",
    "LiveProbes",
    "MqttProbeResult",
]

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT_S: Final = 5.0
"""Seconds a single connection test may take before the route reports a timeout."""

HA_PROBE_TIMEOUT_S: Final = 4.0
"""Seconds the ``GET /api/states`` call itself may take, retry included.

Deliberately below :data:`PROBE_TIMEOUT_S` so the client's own typed timeout
message wins over the route's outer :func:`asyncio.timeout` one — the same
rationale as :data:`~chime_deck.api.ha.TTS_ENGINES_INNER_TIMEOUT_S`.
"""

MQTT_PROBE_TIMEOUT_S: Final = 4.0
"""Seconds one MQTT connection attempt may take, connect and CONNACK included.

Deliberately below :data:`PROBE_TIMEOUT_S` so the probe's own typed timeout
sentence wins over the route's outer :func:`asyncio.timeout` one — the same
rationale as :data:`HA_PROBE_TIMEOUT_S`.
"""

_PROBE_POLL_S: Final = 0.01
"""Seconds between checks of the probe worker's future; small so tests do not crawl."""

_MQTT_LOOP_SLICE_S: Final = 0.05
"""Seconds one ``client.loop()`` call may block, so the deadline is re-checked often."""

_BAD_CREDENTIAL_CODES: Final[tuple[int, ...]] = (0x86, 0x87)
"""CONNACK reason codes for a bad username or password (0x86) and ``not authorized`` (0x87).

Re-derived here rather than importing the bridge's private
``_AUTH_FAILURE_CODES``: those two integers are the whole contract the probe
needs, and a public name for them would belong to the bridge package.
"""

_HA_UNCONFIGURED: Final = (
    "no Home Assistant connection is configured yet — set ha.base_url and ha.token in "
    "Settings, then test again"
)
_MQTT_UNCONFIGURED: Final = (
    "no MQTT broker is configured yet — set mqtt.host and mqtt.port in Settings, then test again"
)
_AUDIO_MUTED: Final = (
    "audio.volume is 0%, so a test tone would be silent — raise the master volume above 0% on "
    "the Audio settings page and test again"
)
# Checked before ``_AUDIO_MUTED`` because that sentence's advice ("raise the
# master volume") is wrong while the device is muted: raising it changes nothing.
_MUTE_ON: Final = (
    "the doorbell is muted (audio.muted is on), so a test tone would be silent — unmute the "
    "device and test again"
)


@dataclass(frozen=True, slots=True)
class HaProbeCandidate:
    """Credentials to probe once, without saving them.

    A blank or missing field means "use the stored value", because the browser
    never receives a stored secret and so cannot resend one.
    """

    base_url: str | None = None
    token: str | None = None


@dataclass(frozen=True, slots=True)
class HaProbeResult:
    """Outcome of a Home Assistant connection test."""

    ok: bool
    ha_version: str | None = None
    entity_count: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MqttProbeResult:
    """Outcome of an MQTT broker connection test.

    ``broker_version`` legitimately stays ``None`` on a successful test, because
    CONNACK carries no standards-based broker version: MQTT 3.1.1's CONNACK has no
    properties at all, and MQTT 5's properties are broker-defined rather than
    required. Any value here would be a guess at what the broker calls itself, so
    fabricating one is not acceptable. The field stays for a broker that answers
    with a version this probe can trust.

    ``auth_failed`` is the one machine-readable classification the UI branches on,
    so a screen never string-matches the English sentence in ``error``; ``error``
    stays the whole human sentence and is still what gets rendered verbatim.
    """

    ok: bool
    broker_version: str | None = None
    error: str | None = None
    auth_failed: bool = False


@dataclass(frozen=True, slots=True)
class AudioProbeResult:
    """Outcome of an audio device test."""

    ok: bool
    device: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class ConnectionProbes(Protocol):
    """The three connection tests the settings page offers."""

    async def test_ha(self, candidate: HaProbeCandidate | None = None) -> HaProbeResult:
        """Check the configured Home Assistant instance."""
        ...

    async def test_mqtt(self) -> MqttProbeResult:
        """Check the configured MQTT broker."""
        ...

    async def test_audio(self) -> AudioProbeResult:
        """Check the configured audio output device."""
        ...


def _supplied(value: str | None) -> str | None:
    """Return a probe candidate's field when it supplies a non-blank value.

    :param value: the candidate field, or ``None`` when the request omitted it.
    """
    return value if value is not None and value.strip() != "" else None


def _refused_text(settings: MqttSettings) -> str:
    """Return the sentence for a broker that refused the TCP connection.

    :param settings: the broker the attempt targeted.
    """
    return (
        f"the MQTT broker at {settings.host}:{settings.port} refused the connection — check "
        "that the broker is running and that mqtt.port is the port it listens on"
    )


def _unresolved_text(settings: MqttSettings) -> str:
    """Return the sentence for a host name the resolver could not resolve.

    :param settings: the broker the attempt targeted.
    """
    return (
        f"the MQTT broker address {settings.host} could not be resolved — check mqtt.host in "
        "Settings"
    )


def _credentials_text(settings: MqttSettings, reason: str) -> str:
    """Return the sentence for a CONNACK that rejected the username or password.

    :param settings: the broker the attempt targeted.
    :param reason: the broker's CONNACK reason text.
    """
    return (
        f"the MQTT broker at {settings.host}:{settings.port} rejected the credentials "
        f"({reason}) — check mqtt.username and mqtt.password in Settings"
    )


def _rejected_text(settings: MqttSettings, reason: str) -> str:
    """Return the sentence for any other failed CONNACK.

    :param settings: the broker the attempt targeted.
    :param reason: the broker's CONNACK reason text.
    """
    return (
        f"the MQTT broker at {settings.host}:{settings.port} rejected the connection "
        f"({reason}) — check mqtt.instance_id and the broker configuration"
    )


def _tls_text(settings: MqttSettings, detail: str) -> str:
    """Return the sentence for a failed TLS setup or handshake.

    :param settings: the broker the attempt targeted.
    :param detail: the TLS library's own message.
    """
    return (
        f"the TLS connection to the MQTT broker at {settings.host}:{settings.port} failed "
        f"({detail}) — check mqtt.tls, the broker certificate and that mqtt.port is the TLS port"
    )


def _timeout_text(settings: MqttSettings, seconds: float) -> str:
    """Return the sentence for an attempt that used up its whole budget.

    :param settings: the broker the attempt targeted.
    :param seconds: the budget that attempt actually used.
    """
    return (
        f"the MQTT broker at {settings.host}:{settings.port} did not answer within "
        f"{seconds:g} s — check that the broker is reachable from this device"
    )


def _generic_text(exc: Exception) -> str:
    """Return the fallback sentence for a failure the probe cannot classify.

    :param exc: whatever escaped the attempt.
    """
    return f"the MQTT connection test failed: {exc}"


class _ProbeWorker:
    """One daemon thread that runs the blocking MQTT attempt for one probe.

    The same rationale :class:`~chime_deck.mqtt.bridge._ShutdownWorker`
    documents: executor threads are joined at interpreter exit, so a wedged DNS
    lookup, TLS handshake or CONNACK wait left running there would hang pytest or
    systemd shutdown. This thread is a daemon and is never joined, so a call that
    never returns can only leak this one thread.
    """

    def __init__(self, attempt: Callable[[], MqttProbeResult]) -> None:
        """Prepare the thread without starting it.

        :param attempt: blocking work that returns the probe's verdict.
        """
        self._attempt = attempt
        self._done: concurrent.futures.Future[MqttProbeResult] = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._run, name="mqtt-probe", daemon=True)

    def start(self) -> concurrent.futures.Future[MqttProbeResult]:
        """Start the thread and return the future it resolves.

        :return: future resolved, or failed, once the attempt returns.
        """
        self._thread.start()
        return self._done

    def _run(self) -> None:
        """Run the attempt once; nothing escapes this thread."""
        try:
            result = self._attempt()
        except Exception as exc:
            _LOGGER.warning("the MQTT probe worker failed", exc_info=True)
            self._resolve_error(exc)
            return
        except BaseException as exc:
            # ``BaseException`` rather than ``Exception``: an interrupt arriving
            # here must not print a thread traceback at exit.
            _LOGGER.warning("the MQTT probe worker was interrupted")
            self._resolve_error(exc)
            return
        with suppress(InvalidStateError):
            self._done.set_result(result)

    def _resolve_error(self, exc: BaseException) -> None:
        """Fail the probe future, tolerating one that already finished.

        :param exc: whatever the attempt raised.
        """
        with suppress(InvalidStateError):
            self._done.set_exception(exc)


def _attempt_mqtt(
    *, settings: MqttSettings, factory: ClientFactory, timeout_s: float
) -> MqttProbeResult:
    """Connect once, read the CONNACK and disconnect. Total: it never raises.

    The synchronous paho path is deliberate:
    :meth:`~chime_deck.mqtt.MqttClient.connect` blocks for DNS, TCP, TLS and
    the CONNECT packet and raises the error that actually happened, while
    ``connect_async`` plus ``loop_start`` silently retries a TCP-level failure and
    never calls a callback at all. No paho network thread is started here, so the
    only thread involved is the probe's own.

    ``connect_timeout`` bounds the blocking part with what is left of the budget,
    and the ``loop()`` slices bound the wait for the CONNACK — the CONNACK is only
    ever processed by ``loop()``.

    :param settings: the broker to connect to.
    :param factory: builds the throwaway probe client.
    :param timeout_s: seconds this attempt may take in total.
    :returns: the verdict; ``ok=True`` with ``broker_version=None`` on a CONNACK.
    """
    deadline = time.monotonic() + timeout_s
    connack: list[ReasonCode] = []

    def _on_connect(
        _client: object,
        _userdata: object,
        _flags: object,
        reason_code: ReasonCode,
        _properties: object,
    ) -> None:
        """Record the CONNACK reason code for the attempt to read.

        :param reason_code: the broker's CONNACK reason code.
        """
        connack.append(reason_code)

    def _ignore_disconnect(
        _client: object, _userdata: object, _flags: object, _reason: object, _properties: object
    ) -> None:
        """Discard a disconnect callback; the probe has no reconnect to report."""

    def _ignore_message(_client: object, _userdata: object, _message: object) -> None:
        """Discard a message; the probe subscribes to nothing."""

    try:
        client = factory(
            client_id=f"{settings.client_id}-probe",
            clean_session=True,
            on_connect=_on_connect,
            on_disconnect=_ignore_disconnect,
            on_message=_ignore_message,
        )
    except Exception as exc:
        return MqttProbeResult(ok=False, error=_generic_text(exc))
    try:
        if settings.username:
            # The same rule as the bridge: a password is only ever handed to paho
            # together with a username.
            client.username_pw_set(settings.username, settings.password or None)
        if settings.tls:
            client.tls_set()
        client.connect_timeout = max(0.0, deadline - time.monotonic())
        client.connect(settings.host, settings.port, keepalive=DEFAULT_KEEPALIVE_S)
        while not connack and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            client.loop(timeout=min(_MQTT_LOOP_SLICE_S, remaining))
    except ConnectionRefusedError:
        return MqttProbeResult(ok=False, error=_refused_text(settings))
    except (socket.gaierror, socket.herror):
        return MqttProbeResult(ok=False, error=_unresolved_text(settings))
    except (ssl.SSLError, ssl.CertificateError) as exc:
        return MqttProbeResult(ok=False, error=_tls_text(settings, str(exc)))
    except TimeoutError:
        return MqttProbeResult(ok=False, error=_timeout_text(settings, timeout_s))
    except Exception as exc:
        return MqttProbeResult(ok=False, error=_generic_text(exc))
    finally:
        # In a ``finally`` on purpose: a refusal raised mid-attempt must still
        # close the socket. Suppressed because the verdict is already decided;
        # a failing ``disconnect()`` has none left to change.
        with suppress(Exception):
            client.disconnect()
    if not connack:
        return MqttProbeResult(ok=False, error=_timeout_text(settings, timeout_s))
    reason = connack[0]
    if reason.is_failure or reason.value != 0:
        detail = str(reason)
        if reason.value in _BAD_CREDENTIAL_CODES:
            return MqttProbeResult(
                ok=False, error=_credentials_text(settings, detail), auth_failed=True
            )
        return MqttProbeResult(ok=False, error=_rejected_text(settings, detail))
    return MqttProbeResult(ok=True)


@dataclass(frozen=True, slots=True)
class LiveProbes:
    """The three live connection tests: Home Assistant, the MQTT broker and the speaker.

    :meth:`test_ha` asks the REST client for the entity list and reports its
    count, degrading to an ``ok=False`` sentence when it cannot.
    :meth:`test_mqtt` opens one short broker connection, reads the CONNACK and
    closes it again, so the verdict is the broker's own answer rather than a
    guess. :meth:`test_audio` plays the generated verification tone through the
    started backend, so the settings page can prove the speaker makes a sound.

    ``client`` is the borrowed transport from ``create_app(ha_client=…)``. It is
    never closed here (HSD-npt0's contract), so two consecutive tests reuse the
    same connection pool. ``audio_backend`` is the lazy accessor for the started
    speaker — lazy because the chime pipeline constructs it during the lifespan,
    after this probe set exists — and ``clock`` is the monotonic clock the audio
    latency is measured with.

    ``mqtt_client_factory`` builds the throwaway probe client; ``None`` uses
    :func:`~chime_deck.mqtt.default_client_factory`, and a test injects the
    same in-memory fake it injects as ``audio_backend`` and ``client``.
    ``mqtt_timeout_s`` is the inner budget for one MQTT attempt, injectable so a
    test can prove the timeout path in milliseconds — the same reason ``clock``
    is injectable.
    """

    store: ConfigStore
    client: httpx.AsyncClient | None = None
    audio_backend: Callable[[], AudioBackend | None] | None = None
    mqtt_client_factory: ClientFactory | None = None
    mqtt_timeout_s: float = MQTT_PROBE_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic

    async def test_ha(self, candidate: HaProbeCandidate | None = None) -> HaProbeResult:
        """Report whether Home Assistant is configured, then its live entity count.

        A *candidate* carries credentials to try once without saving them: a
        settings form with unsaved typing needs an answer about what is on
        screen, not about what is stored. Each field is resolved here, before the
        client exists, and never written back. A blank candidate field falls back
        to the stored value, because the browser never receives a stored secret
        and so cannot resend one.
        """
        stored_url = await self.store.get_setting("ha.base_url")
        stored_token = await self.store.get_setting("ha.token")
        base_url = _supplied(candidate.base_url if candidate is not None else None)
        if base_url is None and isinstance(stored_url, str) and stored_url.strip() != "":
            base_url = stored_url
        token = _supplied(candidate.token if candidate is not None else None)
        if token is None and isinstance(stored_token, str) and stored_token != "":
            token = stored_token
        if base_url is None or token is None:
            return HaProbeResult(ok=False, error=_HA_UNCONFIGURED)
        try:
            async with HomeAssistantClient(
                base_url=base_url, token=token, client=self.client
            ) as ha:
                states = await ha.states(timeout_s=HA_PROBE_TIMEOUT_S)
        except HaError as exc:
            return HaProbeResult(ok=False, error=str(exc))
        except Exception as exc:
            return HaProbeResult(
                ok=False, error=f"the Home Assistant connection test failed: {exc}"
            )
        # ``ha_version`` stays ``None``: reading it means a second ``/api/config``
        # call, and the settings page only needs the entity list (HSD-8qf6).
        return HaProbeResult(ok=True, entity_count=len(states))

    async def test_mqtt(self) -> MqttProbeResult:
        """Open one short broker connection and report the CONNACK's verdict.

        The blocking attempt runs on :class:`_ProbeWorker`, so this coroutine only
        waits for its future and enforces the inner deadline. A stored setting
        this build cannot use is reported with the loader's own sentence, because
        that is the actionable text the user needs.
        """
        try:
            settings = await load_mqtt_settings(self.store)
        except MqttConfigurationError as exc:
            return MqttProbeResult(ok=False, error=str(exc))
        if not settings.is_configured:
            return MqttProbeResult(ok=False, error=_MQTT_UNCONFIGURED)
        factory = (
            self.mqtt_client_factory
            if self.mqtt_client_factory is not None
            else default_client_factory
        )
        budget = self.mqtt_timeout_s
        future = _ProbeWorker(
            partial(_attempt_mqtt, settings=settings, factory=factory, timeout_s=budget)
        ).start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while not future.done():
            if loop.time() >= deadline:
                # The worker is a daemon and is never joined: an attempt wedged
                # past the deadline can only leak that one thread.
                return MqttProbeResult(ok=False, error=_timeout_text(settings, budget))
            await asyncio.sleep(_PROBE_POLL_S)
        try:
            return future.result()
        except Exception as exc:
            return MqttProbeResult(ok=False, error=_generic_text(exc))

    async def test_audio(self) -> AudioProbeResult:
        """Play the verification tone and report the resolved device and its latency.

        The probe calls the backend **directly**, which bypasses
        :class:`~chime_deck.playback.PlaybackWorker`'s queue on purpose: the
        user asked for an immediate tone, not one queued behind a chime. A chime
        already in flight therefore makes ``play()`` raise
        :class:`~chime_deck.audio.PlaybackInProgressError`, which is reported
        verbatim — that guard is the honesty, and it needs no extra locking here.
        The settings route wraps the whole call in
        ``asyncio.timeout(probe_timeout)``, so the tone cannot hold a request open.

        The device is selected first with ``backend.set_device(hint)`` — the same
        call the settings page's save path makes — so the tone really plays on the
        just-saved device rather than the one the process started with, and a host
        with no output at all raises
        :class:`~chime_deck.audio.NoAudioDeviceError` with its own sentence.
        A **non-empty hint that matches no device** is not an error: the backend
        falls back to the system default, so the probe still reports ``ok=True``
        with ``device="auto"``, and it is ``GET /api/audio/devices``'s
        ``resolved_device: null`` that exposes that to the UI.

        ``AudioBackend.play()`` returns when libmpv reports the end-file event,
        which fires once the tone has been **buffered ahead**, not once it has
        been heard. That boundary is measured, not assumed: the same generated
        0.4 s tone through this repository's real ``MpvBackend`` returned after
        0.232 s on an x86_64 dev host with PipeWire sinks present, and on the ARM
        board (radxa-zero3, real HDMI PipeWire sink, ``python-mpv`` 1.0.8) after
        0.632 s at volume 40, 0.307 s at volume 70, and 0.332 s at volume 70 on
        the default device. An audibly-played run can therefore be SHORTER than
        the tone, in the same range as a discarded one, so no threshold can tell
        "reached the speaker" from "was discarded".

        So ``elapsed`` only backs a latency when it is at least the tone's own
        length: the start delay is then the measured total minus
        :data:`~chime_deck.audio.TONE_DURATION_S`, in milliseconds, rounded to
        one decimal and clamped at 0 so an early clock reading can never report a
        negative latency. A shorter ``elapsed`` means the start delay is below
        what this method can resolve: the probe reports ``ok=True`` with
        ``latency_ms=None`` and still names the resolved ``device``. ``None`` is
        the honest "not measurable this time" — never a fabricated ``0.0``, and
        never a claim that the tone was inaudible.

        ``ok=True`` therefore means "the audio engine played the file without
        error", which is exactly what this probe can attest; it is not a promise
        that a human heard it. Proving audible output belongs to the hardware
        tier (the ARM board and its panel), not to this software probe.

        Mute outranks the tone (D28): a muted device reports that it is muted
        instead of playing silently, so the user is told the real cause.
        """
        backend = None if self.audio_backend is None else self.audio_backend()
        if backend is None:
            return AudioProbeResult(ok=False, error=AUDIO_NOT_STARTED_TEXT)
        if await self.store.get_setting("audio.muted") is True:
            return AudioProbeResult(ok=False, error=_MUTE_ON)
        volume = await self.store.get_setting("audio.volume")
        if not isinstance(volume, int) or volume <= 0:
            return AudioProbeResult(ok=False, error=_AUDIO_MUTED)
        stored_hint = await self.store.get_setting("audio.device_hint")
        hint = stored_hint if isinstance(stored_hint, str) else ""
        try:
            await backend.set_device(hint)
            devices = await backend.list_devices()
            resolved = resolve_device(devices, hint)
            # The tone only has to exist while ``play`` runs; the temporary
            # directory removes it even when the route's timeout cancels mid-play.
            with tempfile.TemporaryDirectory(prefix="hsd-test-tone-") as directory:
                path = write_test_tone(Path(directory))
                started = self.clock()
                await backend.play(path, volume=volume)
                finished = self.clock()
        except AudioError as exc:
            return AudioProbeResult(ok=False, error=str(exc))
        # ``asyncio.CancelledError`` is a ``BaseException`` in 3.11, so a route
        # timeout or a shutdown passes through this handler untouched.
        except Exception as exc:
            return AudioProbeResult(ok=False, error=f"the audio test failed: {exc}")
        device = resolved.name if resolved is not None else SYSTEM_DEFAULT_DEVICE
        elapsed = finished - started
        if elapsed < TONE_DURATION_S:
            # The end-file event fires after buffering, so this run finished
            # before the tone could have been heard: its start delay is below this
            # method's resolution. ``None`` says so; it is not a verdict on audio.
            return AudioProbeResult(ok=True, device=device, latency_ms=None)
        latency_ms = round(max(0.0, elapsed - TONE_DURATION_S) * 1000.0, 1)
        return AudioProbeResult(ok=True, device=device, latency_ms=latency_ms)
