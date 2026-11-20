"""Component health registry shared by the HTTP API and the websocket hub.

The registry is the single place that records whether a backend component
(Home Assistant, MQTT, audio) is usable. Later tickets call :meth:`set` from
the client, bridge and audio engine; nothing else hardcodes a status literal.
It imports no HTTP library on purpose.

Owning ticket: HSD-8gft.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Final

__all__ = ["COMPONENTS", "HealthRegistry", "HealthStatus"]


class HealthStatus(StrEnum):
    """The states a component can report.

    :attr:`NOT_CONFIGURED` and :attr:`NO_ENTITIES` are deliberately distinct:
    the first means no address or credentials are stored, so the integration
    cannot connect at all; the second means Home Assistant is configured and its
    client is running, but no entity ids are selected to watch.
    """

    NOT_CONFIGURED = "not_configured"
    NO_ENTITIES = "no_entities"
    DISABLED = "disabled"
    CONNECTED = "connected"
    READY = "ready"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    ERROR = "error"


COMPONENTS: Final = ("ha", "mqtt", "audio")
"""Components that always appear in a health payload, in display order."""

_DEFAULT_STATUS: Final[dict[str, HealthStatus]] = {
    "ha": HealthStatus.NOT_CONFIGURED,
    "mqtt": HealthStatus.NOT_CONFIGURED,
    "audio": HealthStatus.DISABLED,
}


class HealthRegistry:
    """Track the last known status of every component.

    Statuses for :data:`COMPONENTS` always exist; extra components may be added
    later by calling :meth:`set` with a new name. An optional ``on_change``
    callback lets the websocket hub push a ``health`` message without the
    components knowing that HTTP exists.
    """

    def __init__(self, *, on_change: Callable[[], None] | None = None) -> None:
        """Create a registry with every component at its initial status.

        :param on_change: called after every successful :meth:`set`.
        """
        self._statuses: dict[str, HealthStatus] = dict(_DEFAULT_STATUS)
        self._last_detail: str | None = None
        self._on_change = on_change

    def set_on_change(self, on_change: Callable[[], None] | None) -> None:
        """Install the change callback after construction.

        :param on_change: called after every successful :meth:`set`.
        """
        self._on_change = on_change

    def set(self, component: str, status: HealthStatus, detail: str | None = None) -> None:
        """Record the status of one component and notify the change callback.

        :param component: component name, normally one of :data:`COMPONENTS`.
        :param status: new status.
        :param detail: optional human-readable reason, shown with the header dots.
        """
        self._statuses[component] = status
        if detail is not None:
            self._last_detail = detail
        if self._on_change is not None:
            self._on_change()

    def get(self, component: str) -> HealthStatus:
        """Return the last known status of *component*.

        Unknown components read as :attr:`HealthStatus.NOT_CONFIGURED`.

        :param component: component name.
        """
        return self._statuses.get(component, HealthStatus.NOT_CONFIGURED)

    def snapshot(self) -> dict[str, str]:
        """Return every status, without detail, for the websocket snapshot."""
        statuses = {component: self.get(component).value for component in COMPONENTS}
        for component, status in self._statuses.items():
            statuses.setdefault(component, status.value)
        return statuses

    def message(self) -> dict[str, str]:
        """Return every status plus the last detail, for a ``health`` message."""
        payload = self.snapshot()
        if self._last_detail is not None:
            payload["detail"] = self._last_detail
        return payload
