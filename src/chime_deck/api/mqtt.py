"""MQTT API: re-announce the chime buttons' Home Assistant discovery payloads.

``POST /api/mqtt/republish`` asks the live MQTT generation to publish every
enabled button's discovery payload again. That is what the settings page's
"re-announce" action needs when Home Assistant never showed the entities: the
payloads are not retained (design 02 §4), so a lost publish is invisible until
something asks for it again.

The route publishes nothing itself. It calls
:meth:`~chime_deck.mqtt.discovery.MqttDiscovery.republish_all`, whose own
store read is bounded, and it bounds the whole call with the same
:data:`~chime_deck.api.probes.PROBE_TIMEOUT_S` budget the connection tests
use, so a wedged broker or a stalled configuration store cannot hold the request
open.

Four behaviours are deliberate and must not be "fixed":

* **HTTP 200 carries the verdict.** A re-announcement that ran and found the
  broker offline answers 200 with ``ok: false`` and prose saying so, exactly like
  ``POST /api/settings/test/*``. Only a malformed request is a 4xx.
* **Nothing raises out of the route.** An unusable stored ``mqtt.*`` value, a
  bridge that never started, a timeout and an unexpected failure each become
  prose plus ``ok: false``, because a 500 tells the user less than a sentence.
* **An unconfigured broker is reported by name**, not as a queued publish: with
  no ``mqtt.host`` there is nothing to reconnect to, so "wait for the connection"
  would be a lie.
* **An empty configuration is a successful no-op.** With no enabled buttons
  there is nothing to announce: ``ok: true``, ``announced: 0`` and no prose. The
  UI shows 0 and the user learns the truth.

Every string that enters the response is scrubbed with
:data:`~chime_deck.config.SECRET_SCRUBBER`, so a broker or a store that
echoes a credential into an error cannot leak it through this route.

Owning ticket: HSD-bq76.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import SECRET_SCRUBBER
from ..mqtt import MqttConfigurationError, load_mqtt_settings

if TYPE_CHECKING:
    from ..app import AppState

__all__ = ["RepublishResponse", "build_mqtt_router"]

_TIMEOUT_TEXT: Final = (
    "the re-announcement did not finish within {seconds:g} s — the MQTT broker or the "
    "configuration store is not answering; check the MQTT connection and try again"
)
_UNCONFIGURED_TEXT: Final = (
    "mqtt.host is empty, so there is no MQTT broker to announce the discovery payloads to — "
    "set mqtt.host to the broker address in Settings and try again"
)
_BRIDGE_NOT_RUNNING_TEXT: Final = (
    "the MQTT bridge is not running, so no discovery payload could be announced — check "
    "mqtt.host and mqtt.port in Settings and restart the service"
)
_QUEUED_TEXT: Final = (
    "the MQTT broker is not connected, so the {count} discovery payload(s) were queued and "
    "will reach Home Assistant when the connection returns"
)
_REJECTED_TEXT: Final = (
    "{failed} discovery payload(s) were not published — check the broker log and "
    "the application log, then try again"
)
_UNPUBLISHED_TEXT: Final = (
    "the discovery payload for {slugs} could not be published — check the application log "
    "and try again"
)


class RepublishResponse(BaseModel):
    """The outcome of one re-announcement of the discovery payloads.

    ``announced`` counts the payloads handed to the bridge, ``queued`` says the
    broker is not connected so the bridge is buffering at least one of them,
    ``failed`` counts the payloads that provably never reached the broker
    (refused by the client, or never built) and ``error`` is the user-facing
    prose, or ``None`` when the run was clean.
    """

    ok: bool
    announced: int = 0
    queued: bool = False
    failed: int = 0
    error: str | None = None


def _scrub(value: str) -> str:
    """Redact any secret-looking text before it enters the response.

    :param value: prose or an exception message about to be returned.
    """
    return SECRET_SCRUBBER.scrub(value)


def _slug_list(slugs: tuple[str, ...]) -> str:
    """Return the slugs of the payloads that failed as readable prose.

    :param slugs: button slugs whose discovery payload raised.
    """
    ordered = sorted(slugs)
    if len(ordered) == 1:
        return ordered[0]
    return ", ".join(ordered)


def build_mqtt_router(state: AppState) -> APIRouter:
    """Build the ``/api/mqtt`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store, the chime pipeline
        and the health registry.
    """
    router = APIRouter(prefix="/api/mqtt", tags=["mqtt"])

    def _health_detail() -> str | None:
        """Return the health registry's last recorded detail, when it has one.

        The pipeline records why a bridge could not start there, so a failed
        start becomes actionable prose instead of a bare "not running".
        """
        detail = state.health.message().get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return None

    @router.post("/republish")
    async def republish() -> RepublishResponse:
        """Re-announce every enabled button's discovery payload.

        ``ok`` carries the verdict on HTTP 200, matching
        ``POST /api/settings/test/*``: only a malformed request is a 4xx. A run
        with no enabled button is a success reporting ``announced: 0``, because
        an empty configuration is an honest no-op rather than a failure.
        """
        try:
            settings = await load_mqtt_settings(state.store)
        except MqttConfigurationError as exc:
            # The message already names the setting and the fix; report it verbatim.
            return RepublishResponse(ok=False, error=_scrub(str(exc)))
        if not settings.is_configured:
            return RepublishResponse(ok=False, error=_scrub(_UNCONFIGURED_TEXT))
        discovery = state.chime.discovery
        if discovery is None:
            detail = _health_detail()
            prose = _BRIDGE_NOT_RUNNING_TEXT
            if detail is not None:
                prose = f"{_BRIDGE_NOT_RUNNING_TEXT} ({detail})"
            return RepublishResponse(ok=False, error=_scrub(prose))
        try:
            bridge = state.chime.bridge
            before = 0 if bridge is None else bridge.publish_failures
            async with asyncio.timeout(state.probe_timeout):
                report = await discovery.republish_all()
        except TimeoutError:
            return RepublishResponse(
                ok=False, error=_scrub(_TIMEOUT_TEXT.format(seconds=state.probe_timeout))
            )
        except Exception as exc:
            return RepublishResponse(ok=False, error=_scrub(str(exc)))
        after = 0 if bridge is None else bridge.publish_failures
        # "Queued" only means something when there was a payload to buffer: with
        # no enabled button the run is a clean no-op even while the broker is
        # down, and "the 0 payload(s) were queued" would be a nonsensical answer.
        queued = report.announced > 0 and (bridge is None or not bridge.connected)
        # The two terms are disjoint failure witnesses. The counter only moves for
        # payloads the paho client refused, and the bridge swallows that exception,
        # so ``republish_all`` still counts those as announced; ``report.failed``
        # names the slugs whose ``_publish`` itself raised before any publish. A
        # run with one of each therefore lost three payloads, out of the
        # ``report.announced + len(report.failed)`` buttons it attempted.
        #
        # The message counts *payloads* and no longer quotes the button count as a
        # denominator: since HSD-bnb0 a republish publishes twice to a button's
        # discovery topic (the blank that lets Home Assistant move the entity id,
        # then the announcement), so the two numbers are no longer comparable and a
        # ratio would read "3 of 2".
        refused = max(0, after - before)
        failed = refused + len(report.failed)
        problems: list[str] = []
        if queued:
            problems.append(_QUEUED_TEXT.format(count=report.announced))
        if failed > 0:
            problems.append(_REJECTED_TEXT.format(failed=failed))
        if report.failed:
            problems.append(_UNPUBLISHED_TEXT.format(slugs=_slug_list(report.failed)))
        if not problems:
            return RepublishResponse(ok=True, announced=report.announced)
        return RepublishResponse(
            ok=False,
            announced=report.announced,
            queued=queued,
            failed=failed,
            error=_scrub(" ".join(problems)),
        )

    return router
