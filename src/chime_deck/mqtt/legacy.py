"""The one explicit translation layer from this build's identifiers to the pre-rename ones.

The project was renamed from *Super Doorbell* to *ChimeDeck* (HSD-l84l), and the
MQTT grammar moved with it: ``super_doorbell`` became ``chime_deck`` in the
topics, the unique ids and the device identifiers, and ``super-doorbell-`` became
``chime-deck-`` in the client id. Nothing else moved, which is the invariant this
module rests on: **the pre-rename grammar differs from the current one only in
these literals, therefore old = translate(new) for every identifier this build
can construct.**

That invariance is the only thing that makes the legacy purge possible at all.
Home Assistant discovery configs are **not** retained (design 02 §4), so a freshly
redeployed process has no memory of the entities the previous build announced; the
single thing it *can* do is reconstruct the pre-rename topic from the current
configuration it holds now. That reconstruction is quoted nowhere else: every
pre-rename literal lives in this module, so the translation is reviewable in one
place instead of being buried in each publish path. The contract is ticket
HSD-l84l together with ``Project_state/design/02-mqtt-buttons.md``.

**This module, :mod:`chime_deck.env` and :mod:`chime_deck.store.transfer` are the
only modules allowed to spell the old literals**, and the test module
``tests/test_mqtt_legacy_purge.py`` is the only test allowed to. The two other
modules keep their literals for their own one-release compatibilities (the old
environment-variable prefix and the old config-export tag) and do not translate
topics; this module exists so the MQTT translation has exactly one owner. A
grep for ``super_doorbell``/``super-doorbell`` under ``src/`` must never find a
fourth file.

**Known limit.** An entity this build has no current configuration for cannot be
reconstructed, and therefore cannot be purged: a button that the operator deleted
before upgrading, or a selection tile that was removed from the store while the
old build was still announcing it, is already unrepresentable here. That ghost is
the accepted §4 limitation (discovery is not retained, so only a Home Assistant
restart clears it) and this module does not pretend to solve it.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CLIENT_ID_PREFIX_TOKEN",
    "LEGACY_CLIENT_ID_PREFIX_TOKEN",
    "LEGACY_PROJECT_TOKEN",
    "LEGACY_SUBSTITUTIONS",
    "PROJECT_TOKEN",
    "legacy_identifier",
    "legacy_topic",
]

PROJECT_TOKEN: Final = "chime_deck"
"""The current project token: topics, unique ids and device identifiers."""

LEGACY_PROJECT_TOKEN: Final = "super_doorbell"
"""The pre-rename project token, what the same positions spelled before HSD-l84l."""

CLIENT_ID_PREFIX_TOKEN: Final = "chime-deck-"
"""The current MQTT client-id prefix; hyphenated, unlike the topic token."""

LEGACY_CLIENT_ID_PREFIX_TOKEN: Final = "super-doorbell-"
"""The pre-rename MQTT client-id prefix."""

LEGACY_SUBSTITUTIONS: Final[tuple[tuple[str, str], ...]] = (
    (PROJECT_TOKEN, LEGACY_PROJECT_TOKEN),
    (CLIENT_ID_PREFIX_TOKEN, LEGACY_CLIENT_ID_PREFIX_TOKEN),
)
"""The whole pre-rename grammar as one ordered table of ``(current, legacy)``.

Ordered rather than a mapping because a translation is a sequence of textual
substitutions, not a lookup: each entry is applied to the whole identifier, in
this order. The two tokens cannot overlap (one is underscore-separated, one
hyphenated), so the order is documentation rather than a correctness
requirement — and keeping the grammar in one table is what lets a future rename
add one row here instead of hunting literals.
"""


def legacy_identifier(identifier: str) -> str:
    """Return the pre-rename form of one identifier this build can construct.

    Every entry of :data:`LEGACY_SUBSTITUTIONS` is applied in order, so an
    identifier that carries both tokens (a unique id inside a topic, say) is
    translated completely in one call.

    A translation that changes nothing raises instead of returning the input
    unchanged: an identifier with no current project token was not built by this
    build, so it has no pre-rename form, and silently passing it through would
    make the purge *look* like it had blanked something. Refusing the no-op is
    what keeps this layer auditable.

    :param identifier: a topic, unique id or device identifier this build built.
    :returns: the same identifier with every current token replaced by its
        pre-rename form.
    :raises ValueError: if *identifier* carries no current project token.
    """
    translated = identifier
    for current, legacy in LEGACY_SUBSTITUTIONS:
        translated = translated.replace(current, legacy)
    if translated == identifier:
        raise ValueError(
            f"{identifier!r} carries no {PROJECT_TOKEN!r} token: it was not built by this "
            "build, so it has no pre-rename form"
        )
    return translated


def legacy_topic(topic: str) -> str:
    """Return the pre-rename form of one topic, for the legacy entity purge.

    A thin, named entry point for the one caller — the purge — so the intent at
    the call site is "this is the old topic" rather than "this is a string
    substitution".

    The hyphenated :data:`CLIENT_ID_PREFIX_TOKEN` appears only in the MQTT
    **client id**, never in a topic, so in practice this translates
    :data:`PROJECT_TOKEN` alone. The client id is deliberately **not** translated
    by the purge: it is not retained, so an old client id leaves no ghost on the
    broker to blank, and a client id that differs from any still-running old
    process is exactly what a fresh deployment wants — two processes sharing one
    client id would knock each other off the broker.

    :param topic: a topic this build would construct today.
    :returns: the topic the pre-rename build published the same entity on.
    :raises ValueError: if *topic* carries no current project token.
    """
    return legacy_identifier(topic)
