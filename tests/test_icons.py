"""D16 icon resolution: the chain, reference validation and the ported tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from chime_deck.entity_info import EntityContext
from chime_deck.icons import (
    D16_DEFAULT_ICON,
    DEVICE_CLASS_ICONS,
    FALLBACK_DOMAIN_ICONS,
    IconChoice,
    IconSource,
    domain_of,
    fallback_icon,
    resolve_icon,
    validate_icon_reference,
)

_AC_DOMAINS = (
    "binary_sensor",
    "sensor",
    "light",
    "switch",
    "lock",
    "cover",
    "climate",
    "person",
    "media_player",
    "weather",
)

_DEVICE_CLASS_CASES = (
    ("binary_sensor", "door"),
    ("binary_sensor", "window"),
    ("binary_sensor", "motion"),
    ("binary_sensor", "occupancy"),
    ("binary_sensor", "presence"),
    ("binary_sensor", "smoke"),
    ("binary_sensor", "carbon_monoxide"),
    ("binary_sensor", "connectivity"),
    ("binary_sensor", "moisture"),
    ("binary_sensor", "battery"),
    ("binary_sensor", "vibration"),
    ("binary_sensor", "opening"),
    ("binary_sensor", "running"),
    ("binary_sensor", "safety"),
    ("binary_sensor", "tamper"),
    ("binary_sensor", "heat"),
    ("binary_sensor", "cold"),
    ("binary_sensor", "gas"),
    ("binary_sensor", "problem"),
    ("sensor", "battery"),
    ("sensor", "humidity"),
    ("sensor", "temperature"),
    ("sensor", "power"),
    ("sensor", "energy"),
    ("sensor", "illuminance"),
    ("sensor", "pressure"),
    ("sensor", "carbon_dioxide"),
    ("sensor", "pm25"),
    ("sensor", "voltage"),
    ("sensor", "current"),
    ("sensor", "signal_strength"),
    ("sensor", "moisture"),
    ("sensor", "distance"),
    ("sensor", "speed"),
    ("sensor", "timestamp"),
    ("sensor", "duration"),
    ("cover", "garage"),
    ("cover", "blind"),
    ("cover", "door"),
    ("cover", "shade"),
    ("cover", "shutter"),
    ("cover", "curtain"),
    ("cover", "window"),
    ("cover", "damper"),
    ("cover", "gate"),
)


@pytest.fixture
def icons_dir(tmp_path: Path) -> Path:
    """Create a custom icon directory with files, a directory and an escaping symlink.

    :param tmp_path: pytest temporary directory.
    """
    directory = tmp_path / "icons"
    directory.mkdir()
    (directory / "cat.svg").write_text("<svg/>", encoding="utf-8")
    (directory / "dot.png").write_bytes(b"\x89PNG")
    (directory / "dir.svg").mkdir()
    outside = tmp_path / "outside.svg"
    outside.write_text("<svg/>", encoding="utf-8")
    (directory / "escape.svg").symlink_to(outside)
    return directory


@pytest.mark.parametrize(
    ("override", "entity_id", "context", "expected_icon", "expected_source"),
    [
        ("mdi:door", "sensor.rear", None, "mdi:door", IconSource.USER),
        (
            "custom:cat.svg",
            "sensor.rear",
            None,
            "custom:cat.svg",
            IconSource.USER,
        ),
        (
            "custom:missing.svg",
            "cover.garage",
            EntityContext(
                entity_id="cover.garage",
                found=True,
                attributes={"icon": "mdi:window-shutter"},
            ),
            "mdi:window-shutter",
            IconSource.HA_ATTRIBUTE,
        ),
        (
            None,
            "light.kitchen",
            EntityContext(
                entity_id="light.kitchen",
                found=True,
                attributes={"icon": "mdi:lamp"},
            ),
            "mdi:lamp",
            IconSource.HA_ATTRIBUTE,
        ),
        (
            None,
            "light.kitchen",
            EntityContext(
                entity_id="light.kitchen",
                found=True,
                registry_icon="mdi:ceiling-light",
            ),
            "mdi:ceiling-light",
            IconSource.HA_REGISTRY,
        ),
        (
            None,
            "cover.garage",
            EntityContext(entity_id="cover.garage", found=True, device_class="garage"),
            "mdi:garage-open",
            IconSource.FALLBACK,
        ),
        (None, "lock.front_door", None, "mdi:lock", IconSource.FALLBACK),
        (None, "no-dot-here", None, D16_DEFAULT_ICON, IconSource.DEFAULT),
        (None, "foobar.thing", None, D16_DEFAULT_ICON, IconSource.DEFAULT),
        (
            None,
            "foobar.thing",
            EntityContext(entity_id="foobar.thing", found=True, attributes={"icon": 5}),
            D16_DEFAULT_ICON,
            IconSource.DEFAULT,
        ),
        (
            None,
            "foobar.thing",
            EntityContext(entity_id="foobar.thing", found=True, attributes={"icon": ""}),
            D16_DEFAULT_ICON,
            IconSource.DEFAULT,
        ),
        (
            None,
            "foobar.thing",
            EntityContext(
                entity_id="foobar.thing",
                found=True,
                registry_icon="https://example.invalid/icon.svg",
            ),
            D16_DEFAULT_ICON,
            IconSource.DEFAULT,
        ),
        (
            None,
            "sensor.kitchen",
            EntityContext(
                entity_id="sensor.kitchen",
                found=True,
                attributes={"device_class": "temperature"},
                device_class="humidity",
            ),
            "mdi:water-percent",
            IconSource.FALLBACK,
        ),
    ],
)
def test_resolve_icon_chain(
    override: str | None,
    entity_id: str,
    context: EntityContext | None,
    expected_icon: str,
    expected_source: IconSource,
    icons_dir: Path,
) -> None:
    """The first matching D16 step wins, and invalid hits fall through.

    :param override: stored icon override under test.
    :param entity_id: entity id being rendered.
    :param context: live entity data, or ``None``.
    :param expected_icon: icon the chain must choose.
    :param expected_source: step that must produce the icon.
    :param icons_dir: custom icon directory holding the valid fixtures.
    """
    choice = resolve_icon(override, entity_id, context, icons_dir=icons_dir)

    assert choice == IconChoice(expected_icon, expected_source)


@pytest.mark.parametrize(
    ("icon", "accepted"),
    [
        ("mdi:door", True),
        ("mdi:door-open", True),
        ("mdi:doorbell-1", True),
        ("custom:cat.svg", True),
        ("custom:dot.png", True),
        ("", False),
        ("  ", False),
        ("mdi:", False),
        ("mdi: front door", False),
        ("mdi:door bell", False),
        ("mdi:door!", False),
        ("Door", False),
        ("MDI:door", False),
        ("custom:", False),
        ("custom:../x.svg", False),
        ("custom:sub/x.svg", False),
        ("custom:a\\b.svg", False),
        ("custom:.", False),
        ("custom:..", False),
        ("custom:cat.gif", False),
        ("custom:missing.svg", False),
        ("custom:dir.svg", False),
        ("custom:escape.svg", False),
        ("http://x", False),
    ],
)
def test_validate_icon_reference(icon: str, accepted: bool, icons_dir: Path) -> None:
    """Valid references pass and every malformed or unusable one is rejected.

    :param icon: candidate reference.
    :param accepted: whether the reference must be accepted.
    :param icons_dir: custom icon directory holding the valid fixtures.
    """
    result = validate_icon_reference(icon, icons_dir)

    assert (result is None) is accepted


def test_custom_reference_needs_a_configured_directory() -> None:
    """A valid file name is still rejected when no icon directory is configured."""
    assert validate_icon_reference("custom:cat.svg", None) is not None


def test_missing_icons_directory_is_a_rejection(tmp_path: Path) -> None:
    """An absent configured directory rejects instead of raising.

    :param tmp_path: pytest temporary directory.
    """
    assert validate_icon_reference("custom:cat.svg", tmp_path / "absent") is not None


def test_directory_named_like_an_icon_is_rejected(tmp_path: Path) -> None:
    """A directory whose name looks like an icon file is not a usable reference.

    :param tmp_path: pytest temporary directory.
    """
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "cat.svg").mkdir()

    assert validate_icon_reference("custom:cat.svg", icons) is not None


@pytest.mark.parametrize("domain", _AC_DOMAINS)
def test_acceptance_criteria_domains_resolve(domain: str) -> None:
    """Every acceptance-criteria domain has a table icon and reaches it without live data.

    :param domain: entity domain from the D16 acceptance criteria.
    """
    icon = fallback_icon(domain, None)

    assert icon is not None
    assert icon.startswith("mdi:")
    assert len(icon) > len("mdi:")
    assert icon != D16_DEFAULT_ICON
    assert resolve_icon(None, f"{domain}.x", None, icons_dir=None) == IconChoice(
        icon, IconSource.FALLBACK
    )


@pytest.mark.parametrize(("domain", "device_class"), _DEVICE_CLASS_CASES)
def test_device_class_tables_resolve(domain: str, device_class: str) -> None:
    """Each required device class is present and wins over its domain default.

    :param domain: entity domain.
    :param device_class: device class from the state attributes.
    """
    expected = DEVICE_CLASS_ICONS[domain][device_class]

    icon = fallback_icon(domain, device_class)

    assert icon == expected
    assert icon.startswith("mdi:")


def test_all_ported_icons_are_mdi_names() -> None:
    """Every value in both tables starts with ``mdi:`` and is longer than the prefix."""
    domain_icons = FALLBACK_DOMAIN_ICONS.values()
    class_icons = [
        icon for class_icons in DEVICE_CLASS_ICONS.values() for icon in class_icons.values()
    ]

    for icon in [*domain_icons, *class_icons]:
        assert icon.startswith("mdi:")
        assert len(icon) > 4


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("light.kitchen", "light"),
        ("no-dot-here", ""),
        (".light", ""),
        ("light.", ""),
        ("", ""),
        ("a.b.c", "a"),
    ],
)
def test_domain_of_is_total(entity_id: str, expected: str) -> None:
    """Malformed entity ids yield an empty domain instead of raising.

    :param entity_id: entity id under test.
    :param expected: domain the helper must return.
    """
    assert domain_of(entity_id) == expected
