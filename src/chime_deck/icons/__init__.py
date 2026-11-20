"""D16 icon resolution, presentation hints and the shipped MDI manifest.

:func:`resolve_icon` walks the five sources in order, first hit wins: the stored
user override, the live ``attributes.icon``, the entity registry ``ic``, the
portable domain/device-class tables, and finally ``mdi:help-circle``.
:class:`IconSource` names the source for the UI, and
:func:`presentation_hints` supplies the complementary state wording and
attribute labels. :func:`presentation_payload` is that one shape on the wire:
a snapshot row and an ``entity_state`` frame both spread it, so a tile's word
and unit travel with every change (HSD-7oto). The package's I/O is checking
that a configured ``custom:`` icon file exists, reading a validated custom icon
file for the custom-icon routes, plus reading the committed Material Design
Icons manifest (:mod:`chime_deck.icons.manifest`, generated at development
time from ``@mdi/svg`` and never written at runtime) the icon picker is served
from. The tone vocabulary and its default state→tone table live in
:mod:`chime_deck.icons.tones`.

Owning ticket: HSD-jwd3; the manifest was added by HSD-zyru; the shared custom
icon path rule and its serving were added by HSD-i7pw.
"""

from __future__ import annotations

from .fallback import D16_DEFAULT_ICON, DEVICE_CLASS_ICONS, FALLBACK_DOMAIN_ICONS, fallback_icon
from .hints import (
    AttributeHint,
    PresentationHints,
    humanise_attribute_name,
    presentation_hints,
    presentation_payload,
)
from .manifest import (
    MANIFEST_INDEX_FILENAME,
    MDI_DATA_DIRECTORY,
    MDI_NAME_PATTERN,
    MDI_PREFIX,
    MDI_VIEW_BOX,
    MdiManifest,
    MdiManifestError,
    icon_path,
    load_mdi_manifest,
    normalise_icon_name,
)
from .resolver import (
    CUSTOM_ICON_EXTENSIONS,
    CUSTOM_PREFIX,
    IconChoice,
    IconSource,
    custom_icon_path,
    domain_of,
    resolve_icon,
    validate_icon_reference,
)
from .tones import (
    DEFAULT_STATE_TONES,
    NEVER_MAPPED,
    TONE_ALERT,
    TONE_OFF,
    TONE_OK,
    TONE_WARN,
    TONES,
    is_tone,
    state_tone,
)

__all__ = [
    "CUSTOM_ICON_EXTENSIONS",
    "CUSTOM_PREFIX",
    "D16_DEFAULT_ICON",
    "DEFAULT_STATE_TONES",
    "DEVICE_CLASS_ICONS",
    "FALLBACK_DOMAIN_ICONS",
    "MANIFEST_INDEX_FILENAME",
    "MDI_DATA_DIRECTORY",
    "MDI_NAME_PATTERN",
    "MDI_PREFIX",
    "MDI_VIEW_BOX",
    "NEVER_MAPPED",
    "TONES",
    "TONE_ALERT",
    "TONE_OFF",
    "TONE_OK",
    "TONE_WARN",
    "AttributeHint",
    "IconChoice",
    "IconSource",
    "MdiManifest",
    "MdiManifestError",
    "PresentationHints",
    "custom_icon_path",
    "domain_of",
    "fallback_icon",
    "humanise_attribute_name",
    "icon_path",
    "is_tone",
    "load_mdi_manifest",
    "normalise_icon_name",
    "presentation_hints",
    "presentation_payload",
    "resolve_icon",
    "state_tone",
    "validate_icon_reference",
]
