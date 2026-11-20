"""D16's sanitisation consequence, enforced when a custom SVG is served.

A custom icon is a file the user drops into ``data/icons``; on a small panel it
is also a same-origin document, so it must never be able to run anything. The
policy is reject, never strip: re-serialising an SVG with
:mod:`xml.etree.ElementTree` rewrites namespace prefixes, and a picture that
silently changes is worse than a file the picker reports as unusable. PNGs are
not inspected here at all — a PNG has no script surface, and the routes'
``X-Content-Type-Options: nosniff`` plus ``image/png`` is the whole defence.

``<!DOCTYPE`` and ``<!ENTITY`` are refused **before** any parse. Python's
stdlib expat expands internal general entities, so an internal DTD subset is a
billion-laughs amplifier (and external entities are the XXE path); a tile icon
never needs a DTD, and refusing the text outright means the payload is never
handed to the parser at all.

Owning ticket: HSD-i7pw.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from typing import Final

__all__ = ["FORBIDDEN_ELEMENTS", "check_svg"]

FORBIDDEN_ELEMENTS: Final = frozenset(
    {"audio", "embed", "foreignobject", "iframe", "object", "script", "video"}
)
"""Casefolded local element names a custom icon may never contain.

``foreignObject`` is in the set because it embeds an HTML/XHTML subtree, which
is exactly the scripting surface the rest of the list removes; the names are
stored casefolded so the tree walk compares once.
"""

_DTD_REASON: Final = (
    "the SVG declares a document type or an entity, which a custom icon never needs"
)
_NOT_XML_REASON: Final = "the SVG is not well-formed XML"
_NOT_UTF8_REASON: Final = "the SVG is not UTF-8 text, so its content cannot be checked"
_EXTERNAL_HREF_REASON: Final = (
    "the SVG's href attribute points outside the file, which a custom icon may not do"
)

_DATA_IMAGE_PATTERN: Final = re.compile(
    r"data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]*",
    re.IGNORECASE,
)
"""The only ``data:`` URIs an icon may embed: raster images, base64 encoded.

``image/svg+xml`` is deliberately absent — an embedded SVG is another document
with its own script surface, which is what this module exists to keep out.
"""


def check_svg(data: bytes) -> str | None:
    """Return why *data* is not a safe custom SVG, or ``None`` when it is acceptable.

    The checks are ordered so the most dangerous input is refused the cheapest:
    strict UTF-8 decoding, then a textual scan for a DTD before expat ever sees
    the bytes, then a well-formedness parse, then a walk of every element and
    attribute. The answer is always a sentence safe to show a user and never
    contains a filesystem path.

    :param data: the file's bytes as read from disk.
    :returns: a human-readable rejection reason, or ``None`` when the SVG is safe.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _NOT_UTF8_REASON

    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        return _DTD_REASON

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return _NOT_XML_REASON

    return _check_tree(root)


def _check_tree(root: ElementTree.Element) -> str | None:
    """Walk every element and attribute of *root*, returning the first violation.

    Both loops use the local name (the part after the last ``}`` of a
    ``{namespace}name`` tag or attribute key), so ``<x:script>`` and
    ``<script>`` are the same refusal and ``xlink:href`` is checked like a plain
    ``href``.

    :param root: parsed document root.
    :returns: a rejection reason, or ``None`` when nothing is forbidden.
    """
    for element in root.iter():
        local = element.tag.rpartition("}")[2]
        if local.casefold() in FORBIDDEN_ELEMENTS:
            return f"the SVG contains a <{local}> element, which is never allowed in a custom icon"

        for key, value in element.attrib.items():
            attribute = key.rpartition("}")[2]
            folded = attribute.casefold()
            if folded.startswith("on"):
                return (
                    f"the SVG sets the {attribute} attribute, which is never allowed in "
                    "a custom icon"
                )
            if folded == "href" and not _is_local_reference(value):
                return _EXTERNAL_HREF_REASON
    return None


def _is_local_reference(value: str) -> bool:
    """Whether an ``href`` value stays inside the document or embeds a raster image.

    An empty value, a fragment (``#shape``) or an inline base64 PNG/JPEG/GIF/WebP
    is fine; anything else — an absolute URL, a protocol-relative ``//host``, a
    relative file, ``javascript:``, an embedded SVG document — is not.

    :param value: the attribute's raw value.
    """
    stripped = value.strip()
    if not stripped or stripped.startswith("#"):
        return True
    return _DATA_IMAGE_PATTERN.fullmatch(stripped) is not None
