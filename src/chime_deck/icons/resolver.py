"""D16's five-step icon resolution chain.

Resolution is deliberately a pure, total function: every entity id the store or
a configuration import accepted must produce an :class:`IconChoice`, even when
it is not shaped like a Home Assistant entity id and even when no live data is
available. The only I/O is checking that a configured ``custom:`` file exists;
failures there degrade to the next step rather than raising.

Owning ticket: HSD-jwd3.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..entity_info import EntityContext
from .fallback import D16_DEFAULT_ICON, fallback_icon

__all__ = [
    "CUSTOM_ICON_EXTENSIONS",
    "CUSTOM_PREFIX",
    "IconChoice",
    "IconSource",
    "custom_icon_path",
    "domain_of",
    "resolve_icon",
    "validate_icon_reference",
]


class IconSource(StrEnum):
    """Which D16 step produced an icon; the vocabulary the UI switches on."""

    USER = "user"
    HA_ATTRIBUTE = "ha-attribute"
    HA_REGISTRY = "ha-registry"
    FALLBACK = "fallback"
    DEFAULT = "default"


CUSTOM_ICON_EXTENSIONS: Final = frozenset({".svg", ".png"})
"""Suffixes a ``custom:`` reference may use; D16 allows SVG and PNG only."""

CUSTOM_PREFIX: Final = "custom:"
"""Prefix of a reference naming a file in the configured custom icon directory."""

_MDI_PREFIX: Final = "mdi:"
_MDI_NAME_PATTERN: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

_NOT_A_REFERENCE: Final = "an icon must be an mdi:<name> or custom:<file> reference"
_BAD_CUSTOM_NAME: Final = "a custom icon must be a single file name"
_BAD_CUSTOM_SUFFIX: Final = "a custom icon must be an .svg or .png file"
_NO_CUSTOM_DIR: Final = "no custom icon directory is configured"
_CUSTOM_ESCAPES: Final = "the custom icon resolves outside the icons directory"
_CUSTOM_MISSING: Final = "the custom icon file does not exist"
_CUSTOM_UNREADABLE: Final = "the custom icon directory could not be read"


@dataclass(frozen=True, slots=True)
class IconChoice:
    """One resolved icon and the step that produced it."""

    icon: str
    source: IconSource


def domain_of(entity_id: str) -> str:
    """Return the domain part of *entity_id*, or ``""`` when it has none.

    Total by design: the store and config import accept historical entity ids that
    are not in Home Assistant's shape, and the snapshot must still render them.

    :param entity_id: entity id such as ``"light.kitchen"``.
    """
    domain, separator, object_id = entity_id.partition(".")
    if not separator or not domain or not object_id:
        return ""
    return domain


def validate_icon_reference(icon: str, icons_dir: Path | None) -> str | None:
    """Return why *icon* is not a storable icon reference, or ``None`` when it is.

    One predicate serves both directions: a save rejects the description with a 422,
    and a read treats it as "not a hit" so the chain falls through. It never raises:
    any OSError (missing dir, unreadable dir, symlink loop) is a rejection, never a
    500.

    ``mdi:`` names are charset-only here, on purpose: the shipped icon manifest
    (:mod:`chime_deck.icons.manifest`) is a search and path-data source, not a
    gate on what may be saved. A name this build's icon set does not ship still
    stores — it may come from a newer set, or be a typo the user is about to
    correct — and it renders the fallback icon until then.

    :param icon: candidate reference, either ``mdi:<name>`` or ``custom:<file>``.
    :param icons_dir: configured custom icon directory, or ``None``.
    :returns: a human-readable rejection reason, or ``None`` when accepted.
    """
    if _as_mdi_icon(icon) is not None:
        return None
    if not icon.startswith(CUSTOM_PREFIX):
        return _NOT_A_REFERENCE
    return _validate_custom_reference(icon.removeprefix(CUSTOM_PREFIX), icons_dir)


def resolve_icon(
    override: str | None,
    entity_id: str,
    context: EntityContext | None = None,
    *,
    icons_dir: Path | None,
) -> IconChoice:
    """Resolve D16's chain for one entity, first hit wins.

    Steps: the stored override (``mdi:`` name or a usable ``custom:`` file), then
    ``attributes.icon``, then the registry ``ic``, then the domain/device-class
    table, then :data:`D16_DEFAULT_ICON`. ``context is None`` means no live data, so
    steps 2 and 3 cannot hit and the domain table still can.

    :param override: stored icon override, or ``None``.
    :param entity_id: entity id being rendered.
    :param context: live entity data, or ``None`` when it could not be fetched.
    :param icons_dir: configured custom icon directory, or ``None``.
    :returns: the winning icon and the step that chose it.
    """
    if override is not None:
        if _as_mdi_icon(override) is not None:
            return IconChoice(override, IconSource.USER)
        if (
            override.startswith(CUSTOM_PREFIX)
            and validate_icon_reference(override, icons_dir) is None
        ):
            return IconChoice(override, IconSource.USER)

    if context is not None:
        attribute_icon = _as_mdi_icon(context.attributes.get("icon"))
        if attribute_icon is not None:
            return IconChoice(attribute_icon, IconSource.HA_ATTRIBUTE)

        registry_icon = _as_mdi_icon(context.registry_icon)
        if registry_icon is not None:
            return IconChoice(registry_icon, IconSource.HA_REGISTRY)

    device_class = _device_class_for(context)
    table_icon = fallback_icon(domain_of(entity_id), device_class)
    if table_icon is not None:
        return IconChoice(table_icon, IconSource.FALLBACK)

    return IconChoice(D16_DEFAULT_ICON, IconSource.DEFAULT)


def _as_mdi_icon(value: object) -> str | None:
    """Return *value* when it is a syntactically valid ``mdi:<name>``, else ``None``.

    :param value: candidate value, usually an override, attribute or registry icon.
    """
    if not isinstance(value, str):
        return None
    if not value.startswith(_MDI_PREFIX):
        return None
    if _MDI_NAME_PATTERN.fullmatch(value.removeprefix(_MDI_PREFIX)) is None:
        return None
    return value


def _device_class_for(context: EntityContext | None) -> str | None:
    """Return the authoritative device class for *context*, if any.

    :param context: live entity data, or ``None``.
    """
    if context is None:
        return None
    authoritative = context.device_class
    if isinstance(authoritative, str) and authoritative.strip():
        return authoritative.strip()
    from_attributes = context.attributes.get("device_class")
    if isinstance(from_attributes, str) and from_attributes.strip():
        return from_attributes.strip()
    return None


def custom_icon_path(name: str, icons_dir: Path | None) -> tuple[Path | None, str | None]:
    """Return the contained file a ``custom:`` file name names, or why it cannot be used.

    The one place the custom-icon path rule lives: a single path component with no
    ``/``, ``\\``, control characters, ``.`` or ``..``; a suffix in
    :data:`CUSTOM_ICON_EXTENSIONS`; both sides resolved and the candidate contained
    under *icons_dir*; and a regular file. :func:`validate_icon_reference` turns the
    rejection into a sentence for a 422, and the custom-icon routes turn the same
    rejection into a 404 while reading exactly the path returned here — so save-time
    and serve-time validation cannot drift apart.

    :param name: candidate file name, the part of a ``custom:`` reference after the prefix.
    :param icons_dir: configured custom icon directory, or ``None``.
    :returns: the resolved file path and ``None``, or ``None`` and a rejection reason.
    """
    if not name or name in {".", ".."}:
        return None, _BAD_CUSTOM_NAME
    if "/" in name or "\\" in name:
        return None, _BAD_CUSTOM_NAME
    if any(unicodedata.category(character) == "Cc" for character in name):
        return None, _BAD_CUSTOM_NAME
    if Path(name).suffix.lower() not in CUSTOM_ICON_EXTENSIONS:
        return None, _BAD_CUSTOM_SUFFIX
    if icons_dir is None:
        return None, _NO_CUSTOM_DIR

    try:
        directory = icons_dir.resolve()
        candidate = (directory / name).resolve()
    except OSError:
        return None, _CUSTOM_UNREADABLE

    if not candidate.is_relative_to(directory):
        return None, _CUSTOM_ESCAPES

    try:
        if not candidate.is_file():
            return None, _CUSTOM_MISSING
    except OSError:
        return None, _CUSTOM_UNREADABLE
    return candidate, None


def _validate_custom_reference(name: str, icons_dir: Path | None) -> str | None:
    """Validate the part of a ``custom:`` reference after the prefix.

    A thin delegate to :func:`custom_icon_path`, which owns the rule; the two
    cannot drift apart because there is only one predicate.

    :param name: candidate file name.
    :param icons_dir: configured custom icon directory, or ``None``.
    :returns: a rejection reason, or ``None`` when the file is usable.
    """
    return custom_icon_path(name, icons_dir)[1]
