"""Render a TTS phrase as a Home Assistant Jinja template, at press time.

A phrase containing ``{{ … }}`` or ``{% … %}`` is dynamic by definition, so it
must never be baked: design 04 §2.2 sends it through the ephemeral cache, and
design 04 §6 step 4 says the render happens at press time, never at save time.
:class:`PhraseTemplating` is that press-time step. It asks Home Assistant once
(through :class:`HaTemplateRenderer`, a thin adapter over
:meth:`~chime_deck.ha.HomeAssistantClient.render_template`) and hands the
answer through :func:`sanitise_render` before any engine sees it.

**The plain path is deliberately untouched.** A phrase without template syntax
is returned byte-identically, with zero warnings and zero renderer calls — no
HTTP request at all. It is *not* sanitised or length-capped here: a static
phrase belongs to HSD-dzek's bake path, and general outbound sanitisation is
design §6.6's job (HSD-s856/HSD-5de8). Sanitising it here would silently change
every existing non-templated phrase.

**Known limitation (design 04 §6).** ``trigger.*`` variables only exist inside a
Home Assistant automation, so a phrase that uses them renders empty through
``/api/template``. An empty render is never treated as success: it falls back to
the literal phrase with a hedged warning naming ``trigger.*`` as a *common*
cause, so the sentence does not lie for ``{{ states('sensor.gone') }}``.
Silence is the worst failure a doorbell can have, so the literal phrase always
wins over nothing.

**The markup guard.** A ``200`` that is a web page — a reverse proxy, a captive
portal or Home Assistant's onboarding page intercepting ``/api/template`` — is
never spoken (design §6.6: responses are validated, not trusted). The guard is
deliberately conservative: it fires only when the stripped body starts with a
tag and ends with ``>``, so prose such as ``The garage is <open>`` is left
alone. A phrase that is entirely one markup-like token is treated as a proxy
page; the literal fallback plus the press-log warning means nothing is silently
lost.

**Sanitisation classes.** :func:`sanitise_render` removes whole ANSI CSI/OSC
sequences, maps every ``str.isspace()`` character to one space, and then walks
the characters. U+00AD SOFT HYPHEN and U+200B ZERO WIDTH SPACE are deleted
outright: deleting reproduces what the eye already sees, mapping them to a space
would insert one the user never saw, and U+200B is a line-break hint rather than
a word separator. U+200C ZWNJ and U+200D ZWJ are *preserved*: they are
orthographic controls (Persian nim-fasele, Arabic/Indic joins), and deleting or
space-mapping them changes spelling and joins; they cannot hide or reorder text
and TTS ignores them, so they are not treated as hostile. Every other character
whose :func:`unicodedata.category` is ``Cc``, ``Cf`` or ``Cs`` becomes one
space — C0/C1/DEL, the bidi embeddings/overrides U+202A to U+202E (including the
RTL override U+202E), the isolates U+2066 to U+2069, LRM/RLM, U+FEFF and
surrogates. The ``isspace()`` branch runs *before* the category branch, so
U+00A0, U+2028 and U+2029 are handled as whitespace. Runs of spaces then
collapse and the result is stripped. The 500-code-point cap is applied **after**
the collapse and the control removal, by truncation rather than rejection:
rejection here means silence, and design §6.5's reject rule belongs to the
untrusted inbound server boundary (``remote_tts``/``http_tts``), a different
trust boundary from Home Assistant's own answer: :mod:`chime_deck.api.render`
is where that rule is implemented.

**Press-budget arithmetic.** One templated ``ha_tts`` press is scoped to a 1.0 s
availability probe (:data:`~chime_deck.tts.ha_tts.AVAILABILITY_TIMEOUT_S`),
:data:`TEMPLATE_TIMEOUT_S` (0.5 s) for the render and 7.5 s for synthesis
(:data:`~chime_deck.tts.ha_tts.TTS_RENDER_TOTAL_TIMEOUT_S`), plus the 0.5 s
publish margin: 9.5 s in total, below the 10 s press seam. The render and the
engine probe are sequential, never parallel. That is not the press-wide worst
case either: the probe-failure-then-espeak press that used to exceed it now
fits too (HSD-4y6l) — 1.0 + 0.5 + 6.0 + 1.0 + 0.5 = 9.0 s, asserted in
``tests/test_tts_engines.py``. It remains a bound on one engine: a chain that
probes several unavailable engines before one renders is still uncapped, which
is HSD-96k6.

Owning ticket: HSD-bmqp; the pointer to :mod:`chime_deck.api.render` as the
place that implements the reject rule was added by HSD-5de8.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

import httpx

from ..ha import HaError, HaTimeoutError, HomeAssistantClient, SettingsReader
from .errors import clean_detail
from .providers import has_template_syntax

__all__ = [
    "MAX_TEMPLATE_CHARS",
    "TEMPLATE_TIMEOUT_S",
    "HaTemplateRenderer",
    "PhraseTemplating",
    "RenderedPhrase",
    "TemplateRenderer",
    "looks_like_markup",
    "sanitise_render",
]

_LOGGER = logging.getLogger(__name__)

MAX_TEMPLATE_CHARS: Final = 500
"""Longest rendered phrase that reaches an engine, counted in code points."""

TEMPLATE_TIMEOUT_S: Final = 0.5
"""Hard outer bound, in seconds, for one template render."""

_CLIENT_TIMEOUT_S: Final = 0.4
"""Budget handed to ``render_template(timeout_s=...)``.

Strictly smaller than :data:`TEMPLATE_TIMEOUT_S` on purpose: the client's own
``asyncio.timeout`` always fires first, so its classified
:class:`~chime_deck.ha.HaTimeoutError` is what the warning carries and the
outer bound is only a backstop. A cold TLS handshake to a remote HTTPS Home
Assistant can exceed 0.4 s and will then degrade to the literal phrase; widening
it would break the 10 s press seam, so that is a follow-up, not this ticket.
"""

_CLOSE_GRACE_S: Final = 2.0
"""Seconds allowed for closing an owned client before it is abandoned."""

_ANSI_ESCAPE: Final = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: ESC [ params intermediates final
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC ] ... BEL or ST
)
"""Whole ANSI escape sequences, removed before the per-character walk.

Dropping only the ``ESC`` byte would leave speakable garbage such as ``[0m``
behind, which is why the whole sequence is matched.
"""

_SPACE_RUN: Final = re.compile(r" +")
"""Runs of spaces, collapsed to one after the character walk."""

_MARKUP_PATTERN: Final = re.compile(r"\A\s*<[A-Za-z!/?][^>]*>")
"""An opening tag at the start of the answer; see :func:`looks_like_markup`."""

_NO_SEPARATOR_DELETIONS: Final = frozenset({"\u00ad", "\u200b"})
"""Characters deleted outright: neither is a word separator the user saw."""

_PRESERVED_JOINERS: Final = frozenset({"\u200c", "\u200d"})
"""Orthographic controls kept verbatim: deleting them would change spelling."""

_CONTROL_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})
"""Unicode categories replaced by one space; surrogates included."""

_FALLBACK_WARNING: Final = (
    "Home Assistant could not render the speech template ({detail}); "
    "the literal phrase was spoken instead"
)
"""Warning used when the render failed; ``transient`` errors say "right now"."""

_FALLBACK_TRANSIENT_WARNING: Final = (
    "Home Assistant could not render the speech template right now ({detail}); "
    "the literal phrase was spoken instead"
)
"""Warning used for a transient :class:`~chime_deck.ha.HaError`."""

_MALFORMED_WARNING: Final = (
    "Home Assistant answered the speech template with something that was not text "
    "({type_name}); the literal phrase was spoken instead"
)
"""Warning used when the renderer answered a non-``str`` object."""

_MARKUP_WARNING: Final = (
    "Home Assistant answered the speech template with a web page instead of text — "
    "a reverse proxy, a captive portal or the Home Assistant onboarding page may be "
    "intercepting requests; the literal phrase was spoken instead"
)
"""Warning used when the answer is a web page rather than rendered text."""

_EMPTY_WARNING: Final = (
    "the speech template rendered empty — a common cause is trigger.*, which only "
    "exists inside Home Assistant automations; the literal phrase was spoken instead"
)
"""Warning used when the sanitised render has no speakable character."""

_TRUNCATION_WARNING: Final = (
    "the speech template rendered more than {cap} characters (the original was "
    "{original} characters); the phrase was truncated to {cap} characters"
)
"""Warning used when the render is longer than :data:`MAX_TEMPLATE_CHARS`."""


@dataclass(frozen=True, slots=True)
class RenderedPhrase:
    """One phrase ready to speak, with the warnings it earned.

    ``warnings`` is empty for a phrase that needed no fallback. Each entry is a
    whole sentence the press log can carry; the text itself is already
    sanitised when the templated path produced it.
    """

    text: str
    warnings: tuple[str, ...] = ()


def looks_like_markup(text: str) -> bool:
    """Return whether *text* looks like a whole web page rather than a phrase.

    Deliberately conservative: the stripped answer must start with a tag and end
    with ``>``. A phrase that is entirely one markup-like token is therefore
    treated as a proxy page — the literal fallback plus the press-log warning
    means nothing is silently lost. Design §6.6's rule is that responses are
    validated, not trusted.

    :param text: the answer of ``POST /api/template``.
    """
    stripped = text.strip()
    return _MARKUP_PATTERN.match(text) is not None and stripped.endswith(">")


def sanitise_render(text: str) -> RenderedPhrase:
    """Turn a template answer into one safe, short, speakable phrase.

    The steps are ordered: whole ANSI sequences, whitespace mapping, the
    per-character control/format rule (see the module docstring for the exact
    classes), space collapsing, then the code-point cap. The cap counts
    characters, not bytes, and is applied last so a whitespace-padded answer that
    collapses below the cap earns no truncation warning.

    :param text: Home Assistant's plain-text answer, untrusted.
    """
    without_ansi = _ANSI_ESCAPE.sub("", text)
    kept: list[str] = []
    for char in without_ansi:
        if char in _NO_SEPARATOR_DELETIONS:
            continue
        if char in _PRESERVED_JOINERS:
            kept.append(char)
            continue
        if char.isspace():
            kept.append(" ")
            continue
        if unicodedata.category(char) in _CONTROL_CATEGORIES:
            kept.append(" ")
            continue
        kept.append(char)
    collapsed = _SPACE_RUN.sub(" ", "".join(kept)).strip()
    if len(collapsed) <= MAX_TEMPLATE_CHARS:
        return RenderedPhrase(collapsed)
    warning = _TRUNCATION_WARNING.format(cap=MAX_TEMPLATE_CHARS, original=len(collapsed))
    return RenderedPhrase(collapsed[:MAX_TEMPLATE_CHARS].rstrip(), (warning,))


class TemplateRenderer(Protocol):
    """Render one Jinja template through Home Assistant, and nothing else.

    The return type is ``object`` rather than ``str`` on purpose: a third-party
    or monkeypatched renderer is not trusted at runtime, and the caller has to
    narrow it. ``tests/test_ha_client.py``'s
    ``test_render_template_does_not_parse_numbers_as_json`` is the precedent for
    why a body may not be a string.
    """

    async def render(self, template: str) -> object:
        """Return Home Assistant's plain-text body verbatim.

        :param template: the phrase as stored on the button.
        :raises HaError: if the render failed, already classified.
        """
        ...


class HaTemplateRenderer:
    """Render a phrase with :meth:`HomeAssistantClient.render_template`.

    Construction is completely inert: no setting is read, no client is built and
    no I/O happens until :meth:`render`. This is a thin adapter — the client
    owns the URL, the bearer header, the no-retry rule and the 401/400/404/5xx
    classification, and this class must not hand-roll ``POST /api/template``.

    **Client lifetime.** An injected client is *borrowed* and never closed. With
    no client, each render builds an owned
    :class:`httpx.AsyncClient` and closes it in ``finally`` under
    :data:`_CLOSE_GRACE_S`, swallowing the failure at debug level. Production
    takes the owned path, because ``cli.py`` builds the app with no
    ``ha_client``, so ``app_state.ha_client`` is ``None``; the bounded close is
    load-bearing there, not test-only. Closing a
    :class:`~chime_deck.ha.HomeAssistantClient` built over the borrowed
    transport only marks it closed, so the borrowed socket survives.
    """

    def __init__(
        self,
        settings: SettingsReader,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = TEMPLATE_TIMEOUT_S,
        variables: Mapping[str, object] | None = None,
    ) -> None:
        """Store the settings port and the budgets; build and connect nothing.

        :param settings: settings reader, normally ``ConfigStore``.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests. It is never
            closed here.
        :param timeout_s: hard outer bound for one render, in seconds.
        :param variables: optional template variables, forwarded as-is
            (``None`` stays ``None``, so the client omits the key entirely).
            This is the reserved design §6 seam for later ``{button, now}``; no
            press-path caller sets it.
        """
        self._settings = settings
        self._client = client
        self._timeout_s = timeout_s
        self._variables = variables

    async def render(self, template: str) -> str:
        """Render *template* and return Home Assistant's body verbatim.

        :param template: the phrase as stored on the button.
        :raises HaError: any failure, already classified; an escaped builtin
            :class:`TimeoutError` is converted into
            :class:`~chime_deck.ha.HaTimeoutError` so the caller's warning
            names the budget and the base URL.
        """
        raw = self._client
        owned = raw is None
        if raw is None:
            raw = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(self._timeout_s))
        ha: HomeAssistantClient | None = None
        try:
            async with asyncio.timeout(self._timeout_s):
                ha = await HomeAssistantClient.from_store(self._settings, client=raw)
                return await ha.render_template(
                    template,
                    variables=self._variables,
                    timeout_s=_CLIENT_TIMEOUT_S,
                )
        except TimeoutError as exc:
            base = ha.base_url if ha is not None else "ha.base_url"
            raise HaTimeoutError(
                f"Home Assistant did not answer the speech template within "
                f"{self._timeout_s:g} s — check that {base} is reachable and that "
                "Home Assistant is healthy",
                timeout_s=self._timeout_s,
            ) from exc
        finally:
            if ha is not None:
                await _close_quietly(ha.aclose(), "Home Assistant template client")
            if owned:
                await _close_quietly(raw.aclose(), "template renderer's HTTP client")


class PhraseTemplating:
    """Turn one stored phrase into the phrase that is actually spoken."""

    def __init__(self, renderer: TemplateRenderer) -> None:
        """Create the resolver around *renderer*.

        :param renderer: renders a templated phrase through Home Assistant.
        """
        self._renderer = renderer

    async def resolve(self, phrase: str) -> RenderedPhrase:
        """Return the phrase to speak, rendering it first when it is templated.

        A phrase without template syntax is returned byte-identically and costs
        nothing. A templated phrase is rendered, guarded and sanitised; every
        failure degrades to the literal template text with a whole-sentence
        warning, never to silence and never to
        :class:`~chime_deck.press.SpeechUnavailable`. The ladder shape
        mirrors :meth:`~chime_deck.tts.ha_tts.HaTtsEngine._probe`: only
        :class:`Exception` is caught, so a cancellation still propagates.

        :param phrase: the button's ``tts_text``, already stripped.
        """
        if not has_template_syntax(phrase):
            return RenderedPhrase(phrase)
        try:
            body = await self._renderer.render(phrase)
        except Exception as exc:
            return self._fallback(phrase, _fallback_warning(exc))
        if not isinstance(body, str):
            warning = _MALFORMED_WARNING.format(type_name=type(body).__name__)
            return self._fallback(phrase, warning)
        if looks_like_markup(body):
            return self._fallback(phrase, _MARKUP_WARNING)
        rendered = sanitise_render(body)
        if not _speakable(rendered.text):
            return self._fallback(phrase, _EMPTY_WARNING)
        return rendered

    def _fallback(self, phrase: str, warning: str) -> RenderedPhrase:
        """Return the literal phrase, sanitised, carrying *warning* first.

        The literal template is sanitised too, so a 5 KB literal cannot reach an
        engine uncapped.

        :param phrase: the button's ``tts_text``.
        :param warning: the whole-sentence reason the render was not used.
        """
        _LOGGER.warning("%s", warning)
        literal = sanitise_render(phrase)
        return RenderedPhrase(literal.text, (warning, *literal.warnings))


def _fallback_warning(exc: Exception) -> str:
    """Build the warning for a render that raised *exc*.

    Home Assistant's own text is scrubbed and clipped through
    :func:`~chime_deck.tts.errors.clean_detail`, so a credential it echoed
    cannot travel any further.

    :param exc: the exception the renderer raised.
    """
    detail = clean_detail(str(exc)) or type(exc).__name__
    if isinstance(exc, HaError) and exc.transient:
        return _FALLBACK_TRANSIENT_WARNING.format(detail=detail)
    return _FALLBACK_WARNING.format(detail=detail)


def _speakable(text: str) -> bool:
    """Return whether *text* has a character an engine could speak.

    The preserved joiners alone count as nothing: ``""``, ``"   "``, ``"\\u200b"``
    and ``"\\u200d"`` are all an empty render.

    :param text: an already sanitised phrase.
    """
    return any(char not in _PRESERVED_JOINERS for char in text)


async def _close_quietly(closing: Awaitable[None], label: str) -> None:
    """Await one close coroutine under the grace period, swallowing failures.

    A broken socket must not turn a successful render into a failure, and it must
    not hang a press either, so the failure is logged at debug level. A
    cancellation is deliberately not swallowed.

    :param closing: the coroutine returned by ``aclose()``.
    :param label: what is being closed, for the debug line.
    """
    try:
        await asyncio.wait_for(closing, _CLOSE_GRACE_S)
    except Exception as exc:
        _LOGGER.debug("could not close the %s: %s", label, exc)
