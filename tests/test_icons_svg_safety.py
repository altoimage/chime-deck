"""Serve-time SVG safety: every refusal and every allowed shape.

These are unit tests of :func:`chime_deck.icons.svg_safety.check_svg`; the
HTTP behaviour that consumes the answer lives in ``tests/test_api_custom_icons.py``.
Each payload is a complete file, never a fragment, so a case states what a user
could actually drop into ``data/icons``.

Owning ticket: HSD-i7pw.
"""

from __future__ import annotations

import pytest

from chime_deck.icons.svg_safety import FORBIDDEN_ELEMENTS, check_svg

_DTD_REASON = "the SVG declares a document type or an entity, which a custom icon never needs"
_NOT_XML_REASON = "the SVG is not well-formed XML"
_NOT_UTF8_REASON = "the SVG is not UTF-8 text, so its content cannot be checked"
_EXTERNAL_HREF_REASON = (
    "the SVG's href attribute points outside the file, which a custom icon may not do"
)
_XLINK = 'xmlns:xlink="http://www.w3.org/1999/xlink"'

_BAD_HREFS = [
    "https://evil.example/x.png",
    "http://evil.example/x.png",
    "//evil.example/x.png",
    "file:///etc/passwd",
    "relative.png",
    "javascript:alert(1)",
    "data:image/svg+xml;base64,PHN2Zy8+",
]
"""References that leave the file or embed another document; all are refused."""

_BILLION_LAUGHS = (
    b'<?xml version="1.0"?>\n'
    b"<!DOCTYPE lolz [\n"
    b'  <!ENTITY lol "lol">\n'
    + b"".join(
        f'  <!ENTITY lol{level} "{("&lol" + str(level - 1) + ";") * 10}">\n'.encode()
        for level in range(1, 10)
    )
    + b"]>\n<lolz>&lol9;</lolz>\n"
)
"""The classic entity-expansion bomb: if it were parsed, lol9 would be ~1 GB of text."""


def _svg(body: str) -> bytes:
    """Return a complete SVG document with *body* inside its root element.

    :param body: markup placed inside ``<svg>``.
    """
    return f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>'.encode()


def test_minimal_svg_is_safe() -> None:
    """A plain path-only icon is exactly what the sanitisation must let through."""
    assert check_svg(_svg('<path d="M0 0h24v24H0z"/>')) is None


def test_style_css_is_safe() -> None:
    """``<style>`` is not a scripting element and stays allowed."""
    payload = _svg("<style>.a { fill: red; }</style><rect class='a' width='1' height='1'/>")

    assert check_svg(payload) is None


@pytest.mark.parametrize("spelling", ["href", "xlink:href"])
def test_fragment_reference_is_safe(spelling: str) -> None:
    """``<use href="#shape">`` stays inside the document and is allowed.

    :param spelling: plain ``href`` or the ``xlink:`` spelling.
    """
    payload = f'<svg xmlns="http://www.w3.org/2000/svg" {_XLINK}><use {spelling}="#shape"/></svg>'

    assert check_svg(payload.encode()) is None


@pytest.mark.parametrize("spelling", ["href", "xlink:href"])
def test_inline_raster_data_uri_is_safe(spelling: str) -> None:
    """An inline base64 PNG is data, not a document, and is allowed.

    :param spelling: plain ``href`` or the ``xlink:`` spelling.
    """
    href = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    payload = f'<svg xmlns="http://www.w3.org/2000/svg" {_XLINK}><image {spelling}="{href}"/></svg>'

    assert check_svg(payload.encode()) is None


@pytest.mark.parametrize(
    ("payload", "element"),
    [
        (_svg("<script>alert(1)</script>"), "script"),
        (_svg("<SCRIPT>alert(1)</SCRIPT>"), "SCRIPT"),
        (
            b'<svg xmlns:x="http://www.w3.org/1999/xhtml"><x:script>alert(1)</x:script></svg>',
            "script",
        ),
        (_svg("<g><script>alert(1)</script></g>"), "script"),
        (_svg("<foreignObject><body>hi</body></foreignObject>"), "foreignObject"),
        (_svg("<iframe src='https://evil.example'/>"), "iframe"),
        (_svg("<object data='x'/>"), "object"),
        (_svg("<embed src='x'/>"), "embed"),
        (_svg("<video src='x'/>"), "video"),
        (_svg("<audio src='x'/>"), "audio"),
    ],
)
def test_forbidden_elements_are_refused(payload: bytes, element: str) -> None:
    """A scripting or embedding element is refused, naming it as written.

    :param payload: complete SVG document containing the element.
    :param element: local name the reason must name.
    """
    reason = check_svg(payload)

    assert reason == (
        f"the SVG contains a <{element}> element, which is never allowed in a custom icon"
    )


@pytest.mark.parametrize("attribute", ["onload", "ONCLICK", "onbegin"])
def test_event_handlers_are_refused(attribute: str) -> None:
    """Any ``on*`` attribute is refused, whatever its case.

    :param attribute: event-handler attribute name under test.
    """
    payload = f'<svg xmlns="http://www.w3.org/2000/svg" {attribute}="alert(1)"/>'

    assert check_svg(payload.encode()) == (
        f"the SVG sets the {attribute} attribute, which is never allowed in a custom icon"
    )


@pytest.mark.parametrize("href", _BAD_HREFS)
@pytest.mark.parametrize("spelling", ["href", "xlink:href"])
def test_external_href_is_refused(href: str, spelling: str) -> None:
    """Anything that leaves the file, or embeds another document, is refused.

    :param href: attribute value under test.
    :param spelling: plain ``href`` or the ``xlink:`` spelling.
    """
    payload = f'<svg xmlns="http://www.w3.org/2000/svg" {_XLINK}><image {spelling}="{href}"/></svg>'

    assert check_svg(payload.encode()) == _EXTERNAL_HREF_REASON


def test_empty_href_is_safe() -> None:
    """An empty ``href`` references nothing and is not an escape."""
    payload = f'<svg xmlns="http://www.w3.org/2000/svg" {_XLINK}><image xlink:href=""/></svg>'

    assert check_svg(payload.encode()) is None


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE svg [<!ENTITY x "y">]><svg>&x;</svg>',
        b'<!DOCTYPE svg SYSTEM "http://example.com/svg.dtd"><svg/>',
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg>&xxe;</svg>',
        b"<!doctype svg><svg/>",
        b"<!ENTITY x 'y'><svg/>",
    ],
)
def test_document_type_and_entities_are_refused(payload: bytes) -> None:
    """A DTD or entity declaration is refused with the DTD sentence, never parsed.

    :param payload: document carrying a DTD or an entity declaration.
    """
    assert check_svg(payload) == _DTD_REASON


def test_billion_laughs_is_refused_before_any_parse() -> None:
    """The expansion bomb gets the DTD reason, proving expat never saw the bytes.

    A parse error here (``the SVG is not well-formed XML``) would mean the text
    scan was bypassed and the stdlib parser was handed a payload whose expansion
    is roughly a gigabyte. The exact reason is the proof.
    """
    assert check_svg(_BILLION_LAUGHS) == _DTD_REASON


@pytest.mark.parametrize(
    "payload",
    [
        b"&undefined;",
        b"<svg>&undefined;</svg>",
        b"",
        b"<svg",
        b"<svg><g></svg>",
    ],
)
def test_malformed_xml_is_refused(payload: bytes) -> None:
    """Undefined entities and truncated markup are plain parse failures.

    :param payload: bytes that cannot be parsed as XML.
    """
    assert check_svg(payload) == _NOT_XML_REASON


def test_non_utf8_bytes_are_refused() -> None:
    """A file that is not UTF-8 cannot be checked as text and is refused."""
    assert check_svg(b"<svg>\xff</svg>") == _NOT_UTF8_REASON


def test_forbidden_elements_are_stored_casefolded() -> None:
    """The set is casefolded so the tree walk has one comparison to make."""
    assert sorted(FORBIDDEN_ELEMENTS) == [
        "audio",
        "embed",
        "foreignobject",
        "iframe",
        "object",
        "script",
        "video",
    ]
    assert all(name == name.casefold() for name in FORBIDDEN_ELEMENTS)
