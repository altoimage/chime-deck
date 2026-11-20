"""Component health registry: defaults, snapshots, details and change callbacks."""

from __future__ import annotations

from chime_deck.health import HealthRegistry, HealthStatus


def test_defaults() -> None:
    """Every component starts at the status the design asks for."""
    registry = HealthRegistry()

    assert registry.get("ha") is HealthStatus.NOT_CONFIGURED
    assert registry.get("mqtt") is HealthStatus.NOT_CONFIGURED
    assert registry.get("audio") is HealthStatus.DISABLED
    assert registry.snapshot() == {
        "ha": "not_configured",
        "mqtt": "not_configured",
        "audio": "disabled",
    }


def test_snapshot_has_statuses_only() -> None:
    """The snapshot sent with ``snapshot`` carries no prose."""
    registry = HealthRegistry()
    registry.set("ha", HealthStatus.CONNECTED, detail="HA 2025.1")

    snapshot = registry.snapshot()

    assert snapshot == {"ha": "connected", "mqtt": "not_configured", "audio": "disabled"}
    assert "detail" not in snapshot


def test_message_adds_the_last_detail() -> None:
    """A ``health`` message adds the most recent detail."""
    registry = HealthRegistry()
    registry.set("mqtt", HealthStatus.ERROR, detail="connection refused")

    assert registry.message() == {
        "ha": "not_configured",
        "mqtt": "error",
        "audio": "disabled",
        "detail": "connection refused",
    }


def test_message_omits_detail_when_none_was_ever_set() -> None:
    """Without a detail the key is absent rather than null."""
    registry = HealthRegistry()
    registry.set("audio", HealthStatus.READY)

    message = registry.message()

    assert "detail" not in message
    assert message["audio"] == "ready"


def test_set_fires_on_change_once_per_call() -> None:
    """The callback fires exactly once per status change."""
    calls: list[int] = []
    registry = HealthRegistry(on_change=lambda: calls.append(len(calls)))

    registry.set("ha", HealthStatus.CONNECTED)
    registry.set("mqtt", HealthStatus.RECONNECTING)
    registry.set("audio", HealthStatus.READY)

    assert calls == [0, 1, 2]


def test_set_on_change_rewires_the_callback() -> None:
    """A new callback replaces the old one."""
    first: list[str] = []
    second: list[str] = []
    registry = HealthRegistry(on_change=lambda: first.append("called"))

    registry.set_on_change(lambda: second.append("called"))
    registry.set("ha", HealthStatus.DEGRADED)

    assert first == []
    assert second == ["called"]


def test_set_on_change_accepts_none() -> None:
    """Clearing the callback stops the notifications."""
    calls: list[str] = []
    registry = HealthRegistry(on_change=lambda: calls.append("called"))

    registry.set_on_change(None)
    registry.set("ha", HealthStatus.CONNECTED)

    assert calls == []
    assert registry.get("ha") is HealthStatus.CONNECTED


def test_extra_components_are_tracked() -> None:
    """Components outside the default set appear in both payloads."""
    registry = HealthRegistry()

    registry.set("printer", HealthStatus.CONNECTED)

    assert registry.get("printer") is HealthStatus.CONNECTED
    assert registry.snapshot()["printer"] == "connected"
    assert registry.message()["printer"] == "connected"


def test_unknown_component_reads_as_not_configured() -> None:
    """Reading a component nobody ever reported is not an error."""
    assert HealthRegistry().get("nothing-here") is HealthStatus.NOT_CONFIGURED


def test_get_returns_the_current_status() -> None:
    """``get`` reflects the last ``set``."""
    registry = HealthRegistry()

    registry.set("audio", HealthStatus.ERROR)

    assert registry.get("audio") is HealthStatus.ERROR
