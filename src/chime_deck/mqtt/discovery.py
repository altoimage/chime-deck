"""Home Assistant MQTT discovery: the payloads, their lifecycle and press routing.

This module owns the *content* the bridge carries (HSD-kslf owns the connection):
the discovery payload of design 02 §2, the non-retained publish and the empty
payload that removes an entity (design 02 §4), the button lifecycle of design 02
§5, and the command-topic press routing of design 02 §6.

**Two errata are worked around here.**

1. Design 02 §2's literal ``configuration_url`` example
   (``http://127.0.0.1:8080/settings/buttons``) contradicts the note directly
   below it ("when bound to localhost only … omit this field"). The field is
   built from the *resolved* ``ServerConfig.host``/``.port`` injected as
   ``server_host``/``server_port`` — not from the stored ``server.host`` /
   ``server.port`` settings, which :mod:`chime_deck.config` documents as
   deliberately not what the server binds. It is omitted entirely for a loopback
   bind, and its path is ``/settings``: design 05 §2.2 makes Buttons a section
   *inside* ``/settings``, so the SPA fallback would otherwise land the user on
   the SPA's own not-found view.
2. Design 02 §6's ``on_message`` snippet handles ``homeassistant/status``. It
   never reaches this module: :class:`~chime_deck.mqtt.bridge.MqttBridge`
   consumes its own status topic before the message seam and owns the
   jitter/birth/availability ordering. The bridge's tests assert that ordering;
   duplicating it here would republish twice per birth.

**One constant stands in for a missing setting.** There is no ``mqtt.qos``
setting anywhere in the registry, so the acceptance wording "subscribed with the
configured QoS" can only mean the design-pinned constant :data:`PRESS_QOS`
(design 02 §2's payload ``"qos": 0``), which drives *both* the payload field and
the command subscription. :data:`DISCOVERY_PUBLISH_QOS` is a separate decision
for our own non-retained ``/config`` publish: nothing is retained, so an
at-most-once publish lost after CONNECT is unrecoverable until the next connect
or Home Assistant birth, and one extra packet per button per republish is cheap.

**A known, accepted limitation of the non-retained design (§4).** A delete that
happens while this process is not running cannot be retracted: we never
published that entity in this process and discovery is not retained, so clearing
the stale entity *requires* a Home Assistant restart — a long-lived HA session
keeps showing the ghost until it restarts. That is a consequence of §4, not a
bug here.

**The selection switches (design 02 §9, D29 as amended by D34).** One writable
``switch`` per displayed entity: a non-retained discovery config, a **retained**
state on ``chime_deck/{instance}/selection/{slug}`` where ``on`` means the tile
is selected right now, and a ``command_topic`` on
``chime_deck/{instance}/selection/{slug}/set`` that Home Assistant publishes
``on``/``off`` to. They share the button's ``device`` block, so the two entity
types can never disagree about the device page.

**Both sides write, through one implementation (D34).** A panel tap flips the
value through :meth:`~chime_deck.selection.SelectionService.toggle` and an
inbound command sets it through
:meth:`~chime_deck.selection.SelectionService.set`; there is no second path.
The state topic stays authoritative and is republished (retained) after every
change, so any disagreement resolves to the last writer and Home Assistant's
optimistic switch is corrected within one round trip. **Refusals are corrected,
never swallowed**: a *retained* command (a replay of an old intent, not a live
one), a command for a tile that is not displayed, and a malformed payload are
all answered with a republish of the current state, so the switch snaps back
instead of lying. **The pre-D34 read-only ``binary_sensor`` configs are purged**
with retained empty payloads *before* the switch config is published, so no dead
read-only entity is left beside the switch. **Volatility stands** (D29 §4): every
restart starts with nothing selected and every selection is republished on every
connect, so after a restart the value is ``off`` for everything.

**The selection slug comes from the tile's display name, not its entity id
(HSD-jw0x).** §9 writes ``switch.chime_deck_<slug>``, and the slug
is now the name: :func:`chime_deck.store.derive_slug` derives the one dash
slug of this project (HSD-po1g) and :func:`selection_slug` maps that into Home
Assistant's *object id* grammar, which forbids a dash (Home Assistant silently
slugifies an invalid ``default_entity_id``, so the payload, the state topic and
the unique id would otherwise disagree with the entity id an automation has to
reference). "Dryer" is therefore ``dryer`` and "Washing Machine"
``washing_machine``, and the entity id an automation sees is
``switch.chime_deck_dryer``. Names that slug to the same base are
disambiguated deterministically by :func:`assign_selection_slugs` — lowest
``entity_id`` keeps the bare slug, the others take ``_2``, ``_3``, … — and a tile
with no usable display name falls back to its entity id.

That pre-HSD-jw0x entity-id slug (:func:`selection_entity_id_slug`) is retained
for exactly two jobs: that fallback, and computing the stale topic an upgrade
must purge. It is what is already on the owner's broker, so an upgrade renames
every existing selection entity (a pre-D34 build also changes its entity *type*,
from a read-only ``binary_sensor`` to a writable ``switch``): the old discovery
and retained state topics are purged and the new ones published. Retained state
is why :meth:`MqttDiscovery.on_birth` republishes every selection value on every
CONNACK: one stale retained ``on`` would outlive the truth forever.

**Loop safety.** Every coroutine in this module is only ever invoked from the
bridge's seams (``on_birth`` and ``on_message``), and the bridge already hops off
paho's network thread before calling them, so nothing here may be called from a
paho callback directly. The synchronous members —
:meth:`MqttDiscovery.on_selection_changed`, which the toggle route calls, and
:meth:`MqttDiscovery._handle_selection_command` with the two helpers it uses,
which :meth:`MqttDiscovery.on_message` calls — are therefore always on the event
loop too, never on a paho callback, which is exactly what the bridge's publish
contract requires.

**A selection command is a write, not a press.** The two halves share
:meth:`MqttDiscovery.on_message` and nothing else: a press is asynchronous and
reaches the shared press service, while a command is a synchronous write to
:class:`~chime_deck.selection.SelectionService` whose only output is the one
``on_change`` the service already had. Selection never presses a button (D29 §1,
D34 §4) — the policy that acts on a selected tile lives in Home Assistant.

**The pre-rename purge (HSD-l84l).** The project was renamed from *Super
Doorbell* to *ChimeDeck*, which renamed every identifier this module publishes.
A pre-rename build left a discovery entity and, for a selection tile, a
*retained* state on the broker; discovery is not retained, so nothing here can
learn about that entity from the broker and a fresh process that only retracted
what it had published would leave both behind forever. Containerised redeploys
are exactly the case: every restart is a fresh store-less process. The purge is
how the upgrade stays clean — every identifier this module can construct today
is translated to its pre-rename form by :mod:`chime_deck.mqtt.legacy` and
blanked, on every connect, so the ghosts never outlive the first CONNACK.

**The identity root is the instance id; the device name is a label (HSD-vwdy).**
Every identifier this module publishes is built from ``settings.instance_id``:

| identifier / artefact | rule | ``device.name`` | ``mqtt.instance_id`` |
|---|---|---|---|
| discovery config topic | instance | stable | changes |
| button ``unique_id`` ``chime_deck_{instance}_{slug}`` | instance | stable | changes |
| selection ``unique_id`` ``..._{slug}_selected`` | instance | stable | changes |
| selection discovery topic | instance | stable | changes |
| device ``identifiers`` ``chime_deck_{instance}`` | instance | stable | changes |
| availability topic ``chime_deck/{instance}/availability`` | instance | stable | changes |
| press topic ``chime_deck/{instance}/button/{slug}/set`` | instance | stable | changes |
| selection state topic ``chime_deck/{instance}/selection/{slug}`` | instance | stable | changes |
| selection command topic ``.../selection/{slug}/set`` | instance | stable | changes |
| selection command filter ``.../selection/+/set`` | instance | stable | changes |
| MQTT client id ``chime-deck-{instance}`` | instance | stable | changes |
| ``device`` block ``name`` (HA device page) | ``device.name`` | changes | stable |
| browser title | ``device.name`` | changes | stable |
| ``default_entity_id`` ``switch.chime_deck_{slug}`` | slug only | stable | stable |
| manufacturer / model / sw_version / configuration_url | constants | stable | stable |

So ``mqtt.instance_id`` is the identity root and ``device.name`` only renames the
Home Assistant device page (and the browser title). The instance id is pinned
the first time the unit is named — :mod:`chime_deck.api.settings` derives it from
the name in the same transaction (see
:func:`chime_deck.store.derive_instance_id`) — which is why a later rename never
moves an entity. A *changed* instance id does move them, and that is handled
here: the reload path retracts every identifier of the old generation through
:meth:`MqttDiscovery.retract_all` before the new generation publishes, so the old
entities are removed rather than orphaned. The pre-rename purge in
:mod:`chime_deck.mqtt.legacy` is the same mechanism for an old *build*'s grammar.

**Why the payload cache exists despite design §5's idempotence.** Bake traffic
is the reason: ``ConfigStore.set_bake_state``/``record_bake_failure``/
``clear_bake`` emit ``button_event(slugs=...)`` whose discovery payload is
provably identical to the last one. The cache is a *publish diff only*: it may
skip an explicit event-driven republish whose payload did not change, and it
must NEVER gate a :meth:`MqttDiscovery.retract_all`/republish — ``bridge.publish``
only buffers while disconnected, so a cache-skipped republish could never reach
the wire.

Owning ticket: HSD-c3si; the selection switches were added by HSD-fro0, made
writable by D34 (HSD-8a5f), the slugs they are named by moved to the tile
display name in HSD-jw0x, the pre-rename purge was added by HSD-l84l, and the
device name became a setting with the instance id as the identity root in
HSD-vwdy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from .. import __version__
from ..config import is_loopback
from ..selection import NO_SELECTION, SelectionWriter
from .bridge import AVAILABILITY_OFFLINE, AVAILABILITY_ONLINE, DEFAULT_DEVICE_NAME
from .errors import MqttConfigurationError
from .legacy import legacy_topic

if TYPE_CHECKING:
    from ..store.events import ConfigEvent, EventHandler
    from ..store.models import Button, Entity
    from .bridge import MqttSettings

__all__ = [
    "CONFIGURATION_PATH",
    "CONFIGURATION_URL_TEMPLATE",
    "DEFAULT_BUTTON_ICON",
    "DEFAULT_READ_TIMEOUT_S",
    "DEVICE_ID_TEMPLATE",
    "DEVICE_KEY_ORDER",
    "DEVICE_MANUFACTURER",
    "DEVICE_MODEL",
    "DEVICE_NAME",
    "DISCOVERY_KEY_ORDER",
    "DISCOVERY_PUBLISH_QOS",
    "DISCOVERY_TOPIC_TEMPLATE",
    "ENTITY_CATEGORY",
    "MQTT_PRESS_SOURCE",
    "PRESS_BYTES",
    "PRESS_PAYLOAD",
    "PRESS_QOS",
    "PRESS_TOPIC_TEMPLATE",
    "SELECTION_COMMAND_FILTER_TEMPLATE",
    "SELECTION_COMMAND_QOS",
    "SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE",
    "SELECTION_COMMAND_TOPIC_TEMPLATE",
    "SELECTION_DEFAULT_ENTITY_ID_TEMPLATE",
    "SELECTION_DISCOVERY_TOPIC_TEMPLATE",
    "SELECTION_ICON",
    "SELECTION_KEY_ORDER",
    "SELECTION_PAYLOAD_OFF",
    "SELECTION_PAYLOAD_ON",
    "SELECTION_READ_ONLY_DISCOVERY_TOPIC_TEMPLATE",
    "SELECTION_STATE_QOS",
    "SELECTION_STATE_TOPIC_TEMPLATE",
    "SELECTION_UNIQUE_ID_TEMPLATE",
    "UNIQUE_ID_TEMPLATE",
    "ButtonReader",
    "DiscoveryBridge",
    "EntityReader",
    "EventBusPort",
    "LegacyPurgeReport",
    "MqttDiscovery",
    "PressHandler",
    "RepublishReport",
    "assign_selection_slugs",
    "build_discovery_payload",
    "build_selection_payload",
    "button_icon",
    "command_topic",
    "configuration_url",
    "discovery_topic",
    "encode_payload",
    "is_mqtt_safe_icon",
    "is_mqtt_safe_slug",
    "selection_command_filter",
    "selection_command_topic",
    "selection_default_entity_id",
    "selection_discovery_topic",
    "selection_entity_id_slug",
    "selection_read_only_discovery_topic",
    "selection_slug",
    "selection_state_topic",
    "selection_unique_id",
    "unique_id",
]

_LOGGER = logging.getLogger(__name__)

PRESS_PAYLOAD: Final = "PRESS"
"""The only payload that presses a button (design 02 §2's ``payload_press``)."""

PRESS_BYTES: Final = PRESS_PAYLOAD.encode()
"""The same payload as bytes, compared with strict equality against the wire."""

PRESS_QOS: Final = 0
"""QoS pinned by design 02 §2 (``"qos": 0``); used for the payload and subscribe."""

DISCOVERY_PUBLISH_QOS: Final = 1
"""QoS of our own non-retained discovery publish; one cheap packet is worth it."""

PRESS_TOPIC_TEMPLATE: Final = "chime_deck/{instance}/button/{slug}/set"
"""One command topic per button; the instance id and slug are substituted."""

UNIQUE_ID_TEMPLATE: Final = "chime_deck_{instance}_{slug}"
"""Stable Home Assistant unique id, derived from the instance id and the slug."""

DEVICE_ID_TEMPLATE: Final = "chime_deck_{instance}"
"""Identifier grouping every button under one Home Assistant device."""

DISCOVERY_TOPIC_TEMPLATE: Final = "{prefix}/button/{unique_id}/config"
"""Home Assistant discovery topic for one button entity."""

ENTITY_CATEGORY: Final = "config"
"""Keeps the chime buttons off the main dashboard (design 02 §2)."""

DEVICE_NAME: Final = DEFAULT_DEVICE_NAME
"""The *default* device name; the effective one is ``settings.device_name``.

Re-exported from :mod:`chime_deck.mqtt.bridge` rather than repeated, so the
registry default, the bridge default and this constant cannot drift apart. It is
used only by an unnamed unit: :func:`_device_block` publishes the setting.
"""

DEVICE_MANUFACTURER: Final = "ChimeDeck"
"""Device manufacturer shown on the Home Assistant device page."""

DEVICE_MODEL: Final = "Chime & Status Panel"
"""Device model shown on the Home Assistant device page."""

DEFAULT_BUTTON_ICON: Final = "mdi:doorbell"
"""Icon published when a button has none, or one Home Assistant would reject."""

CONFIGURATION_PATH: Final = "/settings"
"""Path of the UI, a section inside ``/settings`` rather than a page of its own."""

CONFIGURATION_URL_TEMPLATE: Final = "http://{host}:{port}{path}"
"""Device ``configuration_url``; only usable for a non-loopback HTTP bind."""

MQTT_PRESS_SOURCE: Final = "mqtt"
"""Press-source tag. Duplicated from :mod:`chime_deck.press` on purpose.

Importing ``chime_deck.press`` would drag in the audio, hub and store
packages just to read one string; a test asserts this value equals
``chime_deck.press.SOURCE_MQTT`` so the duplication cannot drift.
"""

DEFAULT_READ_TIMEOUT_S: Final = 2.0
"""Bound for one discovery store read.

Deliberately far below :data:`chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S`
(10 s): the bridge bounds the whole seam callback, so our own read bound has to
fire first to leave room for the republish itself.
"""

DISCOVERY_KEY_ORDER: Final[tuple[str, ...]] = (
    "name",
    "unique_id",
    "command_topic",
    "payload_press",
    "icon",
    "availability_topic",
    "payload_available",
    "payload_not_available",
    "qos",
    "retain",
    "entity_category",
    "device",
)
"""Design 02 §2's top-level keys in the order the payload must be serialised."""

DEVICE_KEY_ORDER: Final[tuple[str, ...]] = (
    "identifiers",
    "name",
    "manufacturer",
    "model",
    "sw_version",
    "configuration_url",
)
"""Device keys in order; ``configuration_url`` is last and optional."""

SELECTION_PAYLOAD_ON: Final = "on"
"""State payload of a selected tile (design 02 §9's ``payload_on``)."""

SELECTION_PAYLOAD_OFF: Final = "off"
"""State payload of an unselected tile; also the state of every tile after a restart."""

SELECTION_ICON: Final = "mdi:gesture-tap-button"
"""Icon of the selection switch: a tap, because a tile is selected by tapping it."""

SELECTION_STATE_QOS: Final = 1
"""QoS of the *retained* selection state, which deserves at-least-once.

Deliberately separate from :data:`DISCOVERY_PUBLISH_QOS`; the payload itself
carries no ``qos`` key, because design 02 §9's example has none.
"""

SELECTION_STATE_TOPIC_TEMPLATE: Final = "chime_deck/{instance}/selection/{slug}"
"""Retained state topic of one tile's selection switch."""

SELECTION_UNIQUE_ID_TEMPLATE: Final = "chime_deck_{instance}_{slug}_selected"
"""Stable Home Assistant unique id of one tile's selection switch."""

SELECTION_DISCOVERY_TOPIC_TEMPLATE: Final = "{prefix}/switch/chime_deck_{instance}_{slug}/config"
"""Home Assistant discovery topic of one tile's writable ``switch`` (D34)."""

SELECTION_DEFAULT_ENTITY_ID_TEMPLATE: Final = "switch.chime_deck_{slug}"
"""Preferred entity id of one tile's selection switch (design 02 §9, D34)."""

SELECTION_READ_ONLY_DISCOVERY_TOPIC_TEMPLATE: Final = (
    "{prefix}/binary_sensor/chime_deck_{instance}_{slug}/config"
)
"""Discovery topic of the PRE-D34 read-only selection ``binary_sensor`` this release purges.

These configs are blanked with a **retained** empty payload: an empty payload is
Home Assistant's only removal signal for a discovery topic, and the retained form
is what also clears the retained config a pre-D34 build left on the broker. They
are purged BEFORE the switch config is published, so an upgraded install never
shows a dead read-only entity beside the switch — and a broker that was down at
upgrade time converges on the next republish, because the blank is unconditional.
"""

SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE: Final = "chime_deck/{instance}/selection/"
"""Common prefix of one tile's command topic and of the filter that subscribes to them.

Spelled once and derived from below, so the command grammar cannot drift between
the topic Home Assistant publishes to, the filter this service subscribes and the
parser that routes an incoming topic back to a slug.
"""

SELECTION_COMMAND_TOPIC_TEMPLATE: Final = SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE + "{slug}/set"
"""Command topic of one tile's selection switch: where Home Assistant writes the value."""

SELECTION_COMMAND_FILTER_TEMPLATE: Final = SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE + "+/set"
"""The one wildcard subscription covering every tile's command topic."""

SELECTION_COMMAND_QOS: Final = 1
"""QoS of the selection command subscription.

At-least-once, unlike the button presses: a missed ``off`` leaves a tile armed,
which is the one failure D29 §4 exists to prevent. The payload itself carries no
``qos`` key, because design 02 §9's example has none — the switch's delivery
semantics come from our subscription, not from the discovery payload.
"""

SELECTION_KEY_ORDER: Final[tuple[str, ...]] = (
    "name",
    "unique_id",
    "default_entity_id",
    "state_topic",
    "command_topic",
    "state_on",
    "state_off",
    "payload_on",
    "payload_off",
    "icon",
    "availability_topic",
    "payload_available",
    "payload_not_available",
    "device",
)
"""Design 02 §9's ``switch`` keys, in the order the payload is serialised.

D34 makes the entity *writable*: ``state_on``/``state_off`` say how the value on
the state topic is interpreted, while ``payload_on``/``payload_off`` are what Home
Assistant sends on the command topic and what we publish as the state. There is
deliberately no ``device_class`` (none is suitable), and no
``qos``/``retain``/``entity_category`` (§9's example carries none of them).
"""

_SELECTION_SLUG_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")
"""Runs of characters a Home Assistant *object id* cannot contain.

Applied by :func:`selection_slug` to the dash slug
:func:`chime_deck.store.derive_slug` derives from a tile's display name, and
by :func:`selection_entity_id_slug` to a raw Home Assistant entity id. Both use
``_`` rather than the ``-`` ``derive_slug`` keeps: dashes are forbidden in a Home
Assistant entity id.
"""

_SLUG_PATTERN: Final = re.compile(r"[a-z0-9_-]{1,128}")
"""Our slug grammar: lowercase only, and a 128-character bound that is ours.

Home Assistant's ``TOPIC_MATCHER`` object-id grammar allows ``[a-zA-Z0-9_-]+``;
the length cap and the lowercase rule are this project's, so a slug that could
never be derived by the store is refused rather than published.
"""

_OBJECT_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""Home Assistant's object-id grammar, applied to the composed unique-id prefix.

Uppercase is allowed because ``MqttSettings.instance_id`` legitimately accepts
``[A-Za-z0-9][A-Za-z0-9._-]*``. A dot in the instance id silently never matches
Home Assistant's matcher, which is why the construction-time check exists — and
why it only *warns*: the command-topic grammar is unrestricted, so presses still
work even when discovery is invisible.
"""

_ICON_PATTERN: Final = re.compile(r"[A-Za-z0-9]+:[A-Za-z0-9_-]+")
"""A deliberately strict subset of Home Assistant's icon rule.

Home Assistant rejects the *entire* discovery payload for an invalid icon, so an
icon that is not obviously well-formed is replaced by the known-good default.
"""

_BIND_ALL_HOSTS: Final[frozenset[str]] = frozenset({"0.0.0.0", "::", "[::]", "*"})
"""Bind addresses that are not an address a browser can reach the UI on."""

_UNUSABLE_HOST_CHARACTERS: Final = ("/", "+", "#", ":")
"""Characters that cannot appear in the host of a well-formed HTTP URL."""


def unique_id(settings: MqttSettings, slug: str) -> str:
    """Return the stable unique id of one button entity.

    :param settings: MQTT settings holding the instance id.
    :param slug: button slug.
    """
    return UNIQUE_ID_TEMPLATE.format(instance=settings.instance_id, slug=slug)


def command_topic(settings: MqttSettings, slug: str) -> str:
    """Return the command topic Home Assistant presses for one button.

    :param settings: MQTT settings holding the instance id.
    :param slug: button slug.
    """
    return PRESS_TOPIC_TEMPLATE.format(instance=settings.instance_id, slug=slug)


def discovery_topic(settings: MqttSettings, slug: str) -> str:
    """Return the discovery topic of one button entity.

    :param settings: MQTT settings holding the discovery prefix and instance id.
    :param slug: button slug.
    """
    return DISCOVERY_TOPIC_TEMPLATE.format(
        prefix=settings.discovery_prefix,
        unique_id=unique_id(settings, slug),
    )


def selection_slug(entity: Entity) -> str:
    """Return the selection slug of one tile, derived from its display name.

    The display name is the naming source (HSD-jw0x). It goes through
    :func:`chime_deck.store.derive_slug` — the one derivation this project
    has (HSD-po1g), which lowercases, collapses each run of non-alphanumerics
    into a ``-`` and strips the ends — and the dash slug is then mapped into Home
    Assistant's *object id* grammar, which forbids a dash
    (``_OBJECT_ID = r"(?!_)[\\da-z_]+(?<!_)"`` in ``homeassistant/core.py``).
    Home Assistant silently slugifies an invalid ``default_entity_id``, so a dash
    slug would publish ``switch.chime_deck_washing-machine`` while
    registering ``switch.chime_deck_washing_machine``, and the
    payload, the state topic and the unique id would all disagree with the entity
    id an automation has to reference. "Dryer" is therefore ``dryer`` and
    "Washing Machine" is ``washing_machine``.

    A tile whose display name is unset, blank, or has no usable character (a
    :class:`~chime_deck.store.SlugDerivationError`, e.g. ``"!!!"``) falls back
    to :func:`selection_entity_id_slug`, the pre-HSD-jw0x entity-id rule, so the
    slug of a tile with a usable entity id is never empty.

    The result is a *base* slug: two tiles can derive the same one, which is what
    :func:`assign_selection_slugs` disambiguates.

    :param entity: the displayed entity (tile) to name.
    :returns: its base selection slug, ``""`` only when neither the display name
        nor the entity id has a usable character.
    """
    name = (entity.display_name or "").strip()
    if name:
        try:
            # Function-local on purpose: this module declares no module-level
            # ``..store`` import (see MqttDiscovery.on_config_changed).
            from ..store import SlugDerivationError, derive_slug

            return _SELECTION_SLUG_SEPARATORS.sub("_", derive_slug(name))
        except SlugDerivationError:
            _LOGGER.debug(
                "display name %r of %s has no usable character; falling back to its entity id",
                entity.display_name,
                entity.entity_id,
            )
    return selection_entity_id_slug(entity.entity_id)


def selection_entity_id_slug(entity_id: str) -> str:
    """Return the PRE-HSD-jw0x selection slug of one tile, from its entity id.

    ``lock.front_door`` becomes ``lock_front_door``: lowercase, each run of
    characters outside ``[a-z0-9]`` replaced by ``_``, and no leading or trailing
    ``_``. That is Home Assistant's *object id* grammar, which forbids dashes
    (``_OBJECT_ID = r"(?!_)[\\da-z_]+(?<!_)"`` in ``homeassistant/core.py``).

    It survives HSD-jw0x for exactly two jobs: it is the fallback of
    :func:`selection_slug` (a tile with no usable display name), and it computes
    the *stale* topic an upgrade must purge
    (:meth:`MqttDiscovery._retract_legacy_selection`). **It must never be changed
    casually**: it is bug-compatible with what is already on the owner's broker,
    so a change would leave a ghost entity nothing can ever retract.

    :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
    :returns: the slug, or ``""`` when the entity id has no usable character.
    """
    return _SELECTION_SLUG_SEPARATORS.sub("_", entity_id.lower()).strip("_")


def selection_unique_id(settings: MqttSettings, slug: str) -> str:
    """Return the stable unique id of one tile's selection switch.

    :param settings: MQTT settings holding the instance id.
    :param slug: selection slug derived by :func:`selection_slug`.
    """
    return SELECTION_UNIQUE_ID_TEMPLATE.format(instance=settings.instance_id, slug=slug)


def selection_state_topic(settings: MqttSettings, slug: str) -> str:
    """Return the retained state topic of one tile's selection switch.

    :param settings: MQTT settings holding the instance id.
    :param slug: selection slug derived by :func:`selection_slug`.
    """
    return SELECTION_STATE_TOPIC_TEMPLATE.format(instance=settings.instance_id, slug=slug)


def selection_command_topic(settings: MqttSettings, slug: str) -> str:
    """Return the command topic Home Assistant writes one tile's value to.

    :param settings: MQTT settings holding the instance id.
    :param slug: selection slug derived by :func:`selection_slug`.
    """
    return SELECTION_COMMAND_TOPIC_TEMPLATE.format(instance=settings.instance_id, slug=slug)


def selection_command_filter(settings: MqttSettings) -> str:
    """Return the one wildcard filter covering every tile's selection command topic.

    One subscription for every tile rather than one per tile: the filter is
    derived from the same prefix as :func:`selection_command_topic`, so a slug
    this process has never heard of still arrives and can be *answered* rather
    than silently ignored (:meth:`MqttDiscovery._selection_slug_from_command_topic`
    is what turns a matching topic back into a slug).

    :param settings: MQTT settings holding the instance id.
    """
    return SELECTION_COMMAND_FILTER_TEMPLATE.format(instance=settings.instance_id)


def selection_discovery_topic(settings: MqttSettings, slug: str) -> str:
    """Return the discovery topic of one tile's writable ``switch``.

    :param settings: MQTT settings holding the discovery prefix and instance id.
    :param slug: selection slug derived by :func:`selection_slug`.
    """
    return SELECTION_DISCOVERY_TOPIC_TEMPLATE.format(
        prefix=settings.discovery_prefix,
        instance=settings.instance_id,
        slug=slug,
    )


def selection_read_only_discovery_topic(settings: MqttSettings, slug: str) -> str:
    """Return the discovery topic of the PRE-D34 read-only selection entity.

    Only ever used to blank the entity: this build announces a ``switch``, and a
    pre-D34 build's ``binary_sensor`` must not survive beside it (D34 §6).

    :param settings: MQTT settings holding the discovery prefix and instance id.
    :param slug: selection slug the read-only entity was published under.
    """
    return SELECTION_READ_ONLY_DISCOVERY_TOPIC_TEMPLATE.format(
        prefix=settings.discovery_prefix,
        instance=settings.instance_id,
        slug=slug,
    )


def selection_default_entity_id(slug: str) -> str:
    """Return the preferred Home Assistant entity id of one selection switch.

    :param slug: selection slug derived by :func:`selection_slug`.
    """
    return SELECTION_DEFAULT_ENTITY_ID_TEMPLATE.format(slug=slug)


def is_mqtt_safe_slug(slug: str) -> bool:
    """Return whether *slug* can be used in the command and discovery topics.

    :param slug: button slug to check.
    """
    return _SLUG_PATTERN.fullmatch(slug) is not None


def assign_selection_slugs(entities: Iterable[Entity]) -> dict[str, Entity]:
    """Assign one collision-free selection slug to every displayed tile.

    A tile's *base* slug is :func:`selection_slug` of its display name (or of its
    entity id when the name is unusable). Tiles whose base slugs collide are
    disambiguated by **ascending entity id**: inside each collision group the tile
    with the lowest ``entity_id`` keeps the bare base slug, and every other tile
    takes the first free ``<base>_<n>`` for ``n`` = 2, 3, 4, …, one that no other
    tile already holds. Two tiles named "Dryer" are therefore ``dryer`` (the
    lower entity id) and ``dryer_2``; tiles "Dryer", "Dryer", "Dryer 2" are
    ``dryer`` (the lower-entity-id Dryer), ``dryer_3`` (the other Dryer) and
    ``dryer_2`` (the naturally named tile, which claims its bare slug in the
    first pass and so pushes the duplicate to ``_3``).

    The mapping is a function of the *set* of ``(entity_id, base_slug)`` pairs
    only, and therefore independent of listing order, of ``sort_order`` (reordering
    tiles in the UI cannot rename them), of set and dict iteration order and of
    process restarts: ``entity_id`` is UNIQUE in the ``entities`` table, so the
    ``(base_slug, entity_id)`` sort key is total and no tie can ever fall back to
    listing order.

    A tile whose base slug is not :func:`is_mqtt_safe_slug` is skipped with a
    warning. The 128-character bound of that grammar is the only reason a suffix
    can be refused, so the suffix search *breaks* on a refusal instead of trying a
    longer one: a longer suffix cannot recover.

    :param entities: every displayed tile, in any order.
    :returns: ``slug -> entity`` sorted by slug, so the publish order is
        deterministic too.
    """
    candidates: list[tuple[str, Entity]] = []
    for entity in entities:
        slug = selection_slug(entity)
        if not is_mqtt_safe_slug(slug):
            _LOGGER.warning(
                "entity %r has no usable selection slug; skipping its MQTT discovery payload",
                entity.entity_id,
            )
            continue
        candidates.append((slug, entity))
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1].entity_id))

    assigned: dict[str, Entity] = {}
    duplicates: list[tuple[str, Entity]] = []
    for base, entity in candidates:
        if base in assigned:
            duplicates.append((base, entity))
            continue
        assigned[base] = entity

    for base, entity in duplicates:
        placed = False
        for n in range(2, len(candidates) + 2):
            candidate = f"{base}_{n}"
            if candidate in assigned:
                continue
            if not is_mqtt_safe_slug(candidate):
                break
            assigned[candidate] = entity
            placed = True
            break
        if not placed:
            _LOGGER.warning(
                "entity %r (%s) has no free selection slug; skipping its MQTT discovery payload",
                entity.display_name or entity.entity_id,
                entity.entity_id,
            )
    return dict(sorted(assigned.items()))


def is_mqtt_safe_icon(icon: str | None) -> bool:
    """Return whether *icon* is one Home Assistant will accept on a button.

    ``None`` and a blank or whitespace-only value answer ``True``: an absent
    icon is a supported state, published as :data:`DEFAULT_BUTTON_ICON`, not an
    error. Otherwise the stripped value has to match the same pattern
    :func:`button_icon` publishes by, so a save-time refusal and the
    publish-time substitution can never disagree (HSD-po1g). ``mdi:<name>`` is
    the only form Home Assistant accepts inside a discovery payload.

    :param icon: icon reference to check, or ``None`` when there is none.
    """
    if icon is None:
        return True
    stripped = icon.strip()
    if not stripped:
        return True
    return _ICON_PATTERN.fullmatch(stripped) is not None


def button_icon(button: Button) -> str:
    """Return the icon to publish, never an icon Home Assistant would reject.

    An unset or blank icon falls back quietly at DEBUG; a non-empty value that is
    not well-formed is reported at WARNING, naming the button and the value,
    because the user asked for it and it cannot be honoured.

    :param button: button whose icon is wanted.
    """
    icon = (button.icon or "").strip()
    if not icon:
        _LOGGER.debug("button %s has no icon; publishing %s", button.slug, DEFAULT_BUTTON_ICON)
        return DEFAULT_BUTTON_ICON
    if not is_mqtt_safe_icon(icon):
        _LOGGER.warning(
            "button %s has an unusable icon %r; publishing %s instead",
            button.slug,
            button.icon,
            DEFAULT_BUTTON_ICON,
        )
        return DEFAULT_BUTTON_ICON
    return icon


def configuration_url(host: str, port: int) -> str | None:
    """Return the device ``configuration_url``, or ``None`` when it is unusable.

    The URL is only published when a browser on the network can plausibly reach
    it: a loopback bind, a "bind everything" address, a port-less configuration,
    a malformed port and a host that would produce a malformed URL all yield
    ``None``. A colon means IPv6, which is out of scope for this field, so it is
    omitted rather than emitted malformed.

    :param host: resolved bind host, as :class:`chime_deck.config.ServerConfig`
        holds it.
    :param port: resolved HTTP port.
    """
    normalised = host.strip().lower()
    if not normalised or is_loopback(normalised) or normalised in _BIND_ALL_HOSTS:
        return None
    if not 1 <= port <= 65535:
        return None
    if any(character in normalised for character in _UNUSABLE_HOST_CHARACTERS):
        return None
    if any(character.isspace() for character in normalised):
        return None
    return CONFIGURATION_URL_TEMPLATE.format(host=normalised, port=port, path=CONFIGURATION_PATH)


def encode_payload(payload: Mapping[str, object]) -> str:
    """Serialise one payload without reordering it.

    ``sort_keys`` is deliberately off: the order produced by
    :func:`build_discovery_payload` is pinned by the golden files and by
    :data:`DISCOVERY_KEY_ORDER`.

    :param payload: payload mapping to serialise.
    """
    return json.dumps(payload)


def _device_block(
    settings: MqttSettings, configuration_url: str | None, sw_version: str
) -> dict[str, object]:
    """Build the ``device`` block shared by every entity this service publishes.

    One builder for both entity types is what makes "every entity groups under
    one Home Assistant device" true by construction: a button and a selection
    switch cannot drift apart on the identifiers, the name or the version.

    :param settings: MQTT settings holding the instance id and the device name.
    :param configuration_url: device configuration URL, or ``None`` to omit it.
    :param sw_version: software version string for the device block.
    """
    device: dict[str, object] = {
        "identifiers": [DEVICE_ID_TEMPLATE.format(instance=settings.instance_id)],
        "name": settings.device_name,
        "manufacturer": DEVICE_MANUFACTURER,
        "model": DEVICE_MODEL,
        "sw_version": sw_version,
    }
    if configuration_url is not None:
        device["configuration_url"] = configuration_url
    return device


def build_discovery_payload(
    button: Button,
    *,
    settings: MqttSettings,
    availability_topic: str,
    configuration_url: str | None = None,
    sw_version: str = __version__,
) -> dict[str, object]:
    """Build one design 02 §2 discovery payload.

    The availability payloads come from :mod:`chime_deck.mqtt.bridge` so
    there is exactly one source of truth for them. ``sw_version`` is injectable
    so the golden files do not churn on a version bump.

    :param button: button to describe.
    :param settings: MQTT settings holding the discovery prefix and instance id.
    :param availability_topic: the bridge's retained availability topic.
    :param configuration_url: device configuration URL, or ``None`` to omit it.
    :param sw_version: software version string for the device block.
    """
    return {
        "name": button.name,
        "unique_id": unique_id(settings, button.slug),
        "command_topic": command_topic(settings, button.slug),
        "payload_press": PRESS_PAYLOAD,
        "icon": button_icon(button),
        "availability_topic": availability_topic,
        "payload_available": AVAILABILITY_ONLINE,
        "payload_not_available": AVAILABILITY_OFFLINE,
        "qos": PRESS_QOS,
        "retain": False,
        "entity_category": ENTITY_CATEGORY,
        "device": _device_block(settings, configuration_url, sw_version),
    }


def build_selection_payload(
    entity: Entity,
    *,
    slug: str,
    settings: MqttSettings,
    availability_topic: str,
    configuration_url: str | None = None,
    sw_version: str = __version__,
) -> dict[str, object]:
    """Build one design 02 §9 selection ``switch`` payload (D34).

    Exactly the fourteen keys of :data:`SELECTION_KEY_ORDER`, with the same
    ``device`` block :func:`build_discovery_payload` uses. D34 makes the entity
    writable: it carries a ``command_topic`` beside the ``state_topic``, with
    ``state_on``/``state_off`` reusing :data:`SELECTION_PAYLOAD_ON`/
    :data:`SELECTION_PAYLOAD_OFF` (one source of truth for ``on``/``off``) and
    ``payload_on``/``payload_off`` being what Home Assistant sends. ``retain`` is
    absent because it is a *publisher* decision, not a payload field; the state
    publish is the retained one.

    The slug is a required argument rather than something derived here: a
    disambiguated tile's slug depends on the *whole* tile set, so only the caller
    (through :func:`assign_selection_slugs`) knows it. The CALLER validates it
    with :func:`is_mqtt_safe_slug`, exactly like ``build_discovery_payload``,
    which does not validate its button either — a builder never refuses to build.

    :param entity: the displayed entity (tile) to describe.
    :param slug: its resolved selection slug.
    :param settings: MQTT settings holding the discovery prefix and instance id.
    :param availability_topic: the bridge's retained availability topic.
    :param configuration_url: device configuration URL, or ``None`` to omit it.
    :param sw_version: software version string for the device block.
    """
    return {
        "name": f"{entity.display_name or entity.entity_id} selected",
        "unique_id": selection_unique_id(settings, slug),
        "default_entity_id": selection_default_entity_id(slug),
        "state_topic": selection_state_topic(settings, slug),
        "command_topic": selection_command_topic(settings, slug),
        "state_on": SELECTION_PAYLOAD_ON,
        "state_off": SELECTION_PAYLOAD_OFF,
        "payload_on": SELECTION_PAYLOAD_ON,
        "payload_off": SELECTION_PAYLOAD_OFF,
        "icon": SELECTION_ICON,
        "availability_topic": availability_topic,
        "payload_available": AVAILABILITY_ONLINE,
        "payload_not_available": AVAILABILITY_OFFLINE,
        "device": _device_block(settings, configuration_url, sw_version),
    }


@dataclass(frozen=True, slots=True)
class RepublishReport:
    """What one :meth:`MqttDiscovery.republish_all` actually managed to publish.

    ``announced`` counts the discovery payloads handed to the bridge without an
    exception, which is the honest number the settings page shows. ``failed``
    names the slugs whose publish attempt raised, in publication order.

    An empty report is not necessarily a failure: the store read may have timed
    out (then nothing at all was published) or there may simply be no enabled
    button to announce.
    """

    announced: int = 0
    failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyPurgeReport:
    """What one :meth:`MqttDiscovery.purge_legacy_entities` managed to blank.

    ``purged`` names the PRE-RENAME topics whose empty payload the bridge
    accepted, sorted. ``failed`` names the topics that were planned for blanking
    but whose translation or publish raised, in planning order: an untranslatable
    topic has no pre-rename form to report, so it is named by the topic this
    build plans today.

    An empty report is not necessarily a failure: a store read may have timed out
    (then only the half that did read was purged), or the build may have no
    entities to reconstruct. A purge over a broker that never held a pre-rename
    entity is harmless — blanking a topic nothing published is a no-op.
    """

    purged: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class DiscoveryBridge(Protocol):
    """The narrow bridge port this service needs, satisfied by ``MqttBridge``.

    Declared as a shape rather than imported as a class so the discovery module
    depends on five methods instead of on the whole connection owner. The real
    :class:`chime_deck.mqtt.bridge.MqttBridge` satisfies it structurally.
    """

    @property
    def settings(self) -> MqttSettings:
        """Return the validated MQTT settings this bridge was built from."""
        ...

    @property
    def availability_topic(self) -> str:
        """Return the retained availability topic this bridge publishes to."""
        ...

    def publish(
        self, topic: str, payload: str | bytes, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Publish one payload, buffering it while the broker is unreachable.

        :param topic: topic to publish to.
        :param payload: payload to publish.
        :param qos: delivery guarantee for the publish.
        :param retain: whether the broker keeps the payload.
        """
        ...

    def subscribe(self, topic: str, qos: int = 0) -> None:
        """Record a subscription and make it now when connected.

        :param topic: topic to subscribe to.
        :param qos: delivery guarantee for the subscription.
        """
        ...

    def unsubscribe(self, topic: str) -> None:
        """Forget a recorded subscription and remove it from the live client.

        :param topic: topic filter to stop receiving from.
        """
        ...


class ButtonReader(Protocol):
    """The narrow store port this service needs, satisfied by ``ConfigStore``."""

    async def list_buttons(self) -> list[Button]:
        """Return every button, ordered the way the settings screen shows them."""
        ...

    async def get_button_by_slug(self, slug: str) -> Button | None:
        """Return one button by slug, or ``None`` when it no longer exists.

        :param slug: button slug.
        """
        ...


class EntityReader(Protocol):
    """The narrow store port the selection switches need, satisfied by ``ConfigStore``.

    Only the *list* read is needed: the selection switches are resynchronised from
    a full listing rather than from per-slug diffs (see
    :meth:`MqttDiscovery.on_entities_changed`).
    """

    async def list_entities(self) -> list[Entity]:
        """Return every displayed entity, ordered the way the tiles are shown."""
        ...


class EventBusPort(Protocol):
    """The narrow event-bus port this service needs, satisfied by ``EventBus``."""

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register a handler and return a callable that removes it.

        :param handler: sync or async callable receiving each event.
        """
        ...


class PressHandler(Protocol):
    """The narrow press port this service needs, satisfied by ``PressService``.

    :meth:`press` returns ``object`` rather than ``PressResult`` so a real
    service whose return type is ``PressResult`` matches structurally: a
    protocol method that promises ``object`` accepts any return type, the same
    reasoning :class:`~chime_deck.mqtt.bridge.PublishResult` documents.
    """

    async def press(self, slug: str, *, source: str = ...) -> object:
        """Run one press and report what it did.

        :param slug: the button slug that was pressed.
        :param source: press-source tag recorded in the press log.
        """
        ...


class _ReadTimeout(Exception):
    """A discovery store read did not finish inside :data:`DEFAULT_READ_TIMEOUT_S`.

    Private, and raised only by :meth:`MqttDiscovery._load`, so a caller can tell
    "the store stalled, do nothing" from "the row is gone, retract the entity".
    """


def _string_tuple(value: object) -> tuple[str, ...]:
    """Narrow one event payload value to a tuple of strings.

    Typed to take ``object`` on purpose: the event payload is an untyped mapping,
    and narrowing it with a helper keeps mypy's ``warn_unreachable`` happy.

    :param value: whatever the event payload held under a key.
    """
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _rename_pairs(value: object) -> tuple[tuple[str, str], ...]:
    """Narrow one event payload value to ``(old_slug, new_slug)`` pairs.

    :param value: whatever the event payload held under ``renamed``.
    """
    if not isinstance(value, (tuple, list)):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        old, new = item
        if isinstance(old, str) and isinstance(new, str):
            pairs.append((old, new))
    return tuple(pairs)


class MqttDiscovery:
    """Publish discovery, keep it in step with the store, route presses and selection commands.

    Wire it exactly like this (HSD-dtpo owns the wiring)::

        bridge = MqttBridge(settings=settings, health=health)
        discovery = MqttDiscovery(bridge=bridge, buttons=store, press=press_service,
                                  bus=store.events, server_host=config.host,
                                  server_port=config.port)
        bridge.set_on_birth(discovery.on_birth)
        bridge.set_on_message(discovery.on_message)
        await bridge.start()
        await discovery.start()

    Construct exactly ONE instance per bridge/settings generation and inject the
    SAME :class:`PressService` across generations: a per-generation service would
    reset its dedupe window. On a settings-generation change, call
    ``await discovery.retract_all()`` BEFORE stopping the old bridge, because the
    empty payloads travel through that bridge.

    The service is idempotent and safe to start after the bridge has connected:
    :meth:`start` republishes, so a late service still reaches Home Assistant.
    """

    def __init__(
        self,
        *,
        bridge: DiscoveryBridge,
        buttons: ButtonReader,
        press: PressHandler,
        bus: EventBusPort,
        server_host: str,
        server_port: int,
        sw_version: str = __version__,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        entities: EntityReader | None = None,
        selection: SelectionWriter = NO_SELECTION,
    ) -> None:
        """Prepare the service; nothing is published until :meth:`start`.

        The ``configuration_url`` and the object-id warning are computed once
        here, because both derive from values that cannot change without a new
        settings generation.

        :param bridge: the MQTT bridge this service publishes through.
        :param buttons: the store's button reads.
        :param press: the shared press service that presses route to.
        :param bus: the store's event bus, subscribed on :meth:`start`.
        :param server_host: the RESOLVED bind host, not the stored setting.
        :param server_port: the RESOLVED HTTP port, not the stored setting.
        :param sw_version: software version for the device block.
        :param read_timeout_s: bound for one discovery store read.
        :param entities: the store's entity reads; a build with no reader
            publishes no selection switches, mirroring
            ``StateHub(selection=NO_SELECTION)``.
        :param selection: the volatile selection (D29 as amended by D34): a state
            publish *reads* it and an incoming Home Assistant command *writes* it,
            both through the same service, so there is one source of truth.
        :raises MqttConfigurationError: if ``read_timeout_s`` is not positive.
        """
        if read_timeout_s <= 0:
            raise MqttConfigurationError(
                f"read_timeout_s must be greater than zero; got {read_timeout_s:g}"
            )
        self._bridge = bridge
        self._buttons = buttons
        self._press = press
        self._bus = bus
        self._entities = entities
        self._selection = selection
        self._sw_version = sw_version
        self._read_timeout_s = read_timeout_s
        self._settings = bridge.settings
        self._availability_topic = bridge.availability_topic
        self._configuration_url = configuration_url(server_host, server_port)
        self._topic_slugs: dict[str, str] = {}
        self._published: dict[str, str | None] = {}
        # slug -> entity id, an ordered set used only to know what to retract,
        # the same role ``_topic_slugs`` plays for buttons. There is deliberately
        # no publish-diff cache here: a selection state must never be suppressed,
        # and the config payload is one extra packet per tile per republish.
        self._selection_slugs: dict[str, str] = {}
        # Entity id -> slug: the synchronous lookup the toggle path and the command
        # handler need, because reading the entity list to re-derive a slug is async
        # and ``on_selection_changed`` is not.
        self._selection_slug_by_entity: dict[str, str] = {}
        # Subscribed once, from the announce path, exactly like the press topics:
        # one wildcard filter covers every tile, so a SETSUBSCRIBE per republish
        # would be pure churn.
        self._selection_commands_subscribed = False
        self._unsubscribe_bus: Callable[[], None] | None = None
        self._logger = logging.getLogger(__name__)
        composed_prefix = f"{DEVICE_ID_TEMPLATE.format(instance=self._settings.instance_id)}_"
        if _OBJECT_ID_PATTERN.fullmatch(composed_prefix) is None:
            self._logger.warning(
                "mqtt.instance_id %r makes the discovery unique ids contain characters Home "
                "Assistant refuses; presses still work but the entities may not appear — use "
                "only letters, digits, underscores and dashes",
                self._settings.instance_id,
            )

    @property
    def published(self) -> Mapping[str, str | None]:
        """Return a read-only snapshot of the last payload published per slug.

        A ``None`` value means the entity was retracted. This is the publish diff
        only and is never consulted to decide whether to retract.
        """
        return MappingProxyType(dict(self._published))

    @property
    def command_topics(self) -> Mapping[str, str]:
        """Return a read-only snapshot of the command-topic to slug router."""
        return MappingProxyType(dict(self._topic_slugs))

    @property
    def configuration_url(self) -> str | None:
        """Return the device configuration URL computed at construction."""
        return self._configuration_url

    @property
    def discovery_prefix(self) -> str:
        """Return the Home Assistant discovery prefix in use."""
        return self._settings.discovery_prefix

    @property
    def instance_id(self) -> str:
        """Return the instance id in use."""
        return self._settings.instance_id

    async def start(self) -> None:
        """Subscribe to the store's events and publish every discovery payload.

        Idempotent: a second call does not subscribe a second handler. The
        republish is unconditional, so a service started after the bridge already
        connected still reaches Home Assistant. One subscriber handles both
        entity types: :meth:`on_config_changed` dispatches
        ``config.buttons_changed`` and ``config.entities_changed``.

        The PRE-rename entity purge runs last, once this build's own entities are
        announced, so an upgrade never leaves the old entities behind even when
        the service starts before the bridge connects.
        """
        if self._unsubscribe_bus is None:
            self._unsubscribe_bus = self._bus.subscribe(self.on_config_changed)
        await self.republish_all()
        await self.republish_selections()
        try:
            await self.purge_legacy_entities()
        except Exception:
            self._logger.exception("purging the pre-rename MQTT entities failed")

    async def stop(self) -> None:
        """Unsubscribe from the bus, every press topic and the selection commands.

        Design 02 §5: a clean shutdown only publishes the availability
        ``offline``, which the bridge owns. The press topics come from the
        router, never re-derived, so an orphaned filter cannot survive; the one
        selection command filter is dropped only when this service subscribed it,
        and its flag is cleared with the rest of the bookkeeping. Safe to call
        twice.
        """
        unsubscribe = self._unsubscribe_bus
        self._unsubscribe_bus = None
        if unsubscribe is not None:
            unsubscribe()
        for topic in tuple(self._topic_slugs):
            self._bridge.unsubscribe(topic)
        self._topic_slugs.clear()
        if self._selection_commands_subscribed:
            self._bridge.unsubscribe(selection_command_filter(self._settings))
            self._selection_commands_subscribed = False
        self._selection_slugs.clear()
        self._selection_slug_by_entity.clear()
        self._published.clear()

    async def retract_all(self) -> None:
        """Retract every entity this service believes it published.

        Public for the settings-generation handoff: call it BEFORE stopping the
        old bridge, because the empty payloads travel through it. Never raises.
        Both entity types are retracted, each in its own ``try``: a failure on
        one tile must not leave a button announced.
        """
        for slug in tuple(self._published):
            try:
                self._retract(slug)
            except Exception:
                self._logger.exception("retracting the discovery payload for %s failed", slug)
        for slug in tuple(self._selection_slugs):
            try:
                self._retract_selection(slug)
            except Exception:
                self._logger.exception("retracting the selection switch for %s failed", slug)

    async def on_birth(self) -> None:
        """Republish every discovery payload after a connect or an HA birth.

        This is the same seam for the first CONNACK, every reconnect and the
        jittered Home Assistant birth: the bridge ends ``_handle_connect`` with
        this callback unconditionally. That is why the full selection state is
        republished here rather than only at ``start()`` — the states are
        retained, so a selected tile that reconnects with a toggle-only publish
        path would leave a stale retained ``off`` (or ``on``) on the broker
        forever.

        The PRE-rename entity purge runs last here too, and on *every* connect is
        the point: a broker restart can resurrect retained state, so each CONNACK
        re-purges rather than only the first one.

        Never raises: the bridge already bounds this seam with ``seam_timeout_s``
        and swallows its failures, and a second failure path here would only log
        twice.
        """
        try:
            await self.republish_all()
        except Exception:
            self._logger.exception("republishing the MQTT discovery payloads failed")
        try:
            await self.republish_selections()
        except Exception:
            self._logger.exception("republishing the selection switches failed")
        try:
            await self.purge_legacy_entities()
        except Exception:
            self._logger.exception("purging the pre-rename MQTT entities failed")

    async def republish_all(self) -> RepublishReport:
        """Publish every enabled button unconditionally, then prune the rest.

        The reads are bounded by one :func:`asyncio.wait_for`; a stalled store
        returns *without publishing anything*, because a partial blanking is worse
        than a stale entity. That path and a failed read both return an empty
        :class:`RepublishReport`: nothing was announced and nothing raised.

        Every desired button is published even when the cache
        holds an identical payload: the cache must never suppress a publish,
        because a create-while-down publishes through the bridge's buffer and a
        cache-skipped republish after the flush would never reach the wire.

        :return: how many payloads were handed to the bridge and which slugs raised.
        """
        try:
            buttons = await asyncio.wait_for(
                self._buttons.list_buttons(), timeout=self._read_timeout_s
            )
        except TimeoutError:
            self._logger.warning(
                "reading the buttons for MQTT discovery timed out after %g s; nothing was "
                "republished",
                self._read_timeout_s,
            )
            return RepublishReport()
        except Exception:
            self._logger.exception(
                "reading the buttons for MQTT discovery failed; nothing was republished"
            )
            return RepublishReport()
        desired: dict[str, Button] = {}
        for button in buttons:
            if not button.enabled:
                continue
            if not is_mqtt_safe_slug(button.slug):
                self._logger.warning(
                    "button %r has an unusable slug; skipping its MQTT discovery payload",
                    button.slug,
                )
                continue
            desired[button.slug] = button
        announced = 0
        failed: list[str] = []
        for button in desired.values():
            try:
                self._publish(button)
            except Exception:
                failed.append(button.slug)
                self._logger.exception(
                    "publishing the MQTT discovery payload for %s failed", button.slug
                )
            else:
                announced += 1
        for slug in tuple(topic_slug for topic_slug in self._topic_slugs.values()):
            if slug in desired:
                continue
            try:
                self._retract(slug)
            except Exception:
                self._logger.exception("retracting the MQTT discovery payload for %s failed", slug)
        return RepublishReport(announced=announced, failed=tuple(failed))

    async def republish_selections(self) -> None:
        """Announce every displayed tile as a selection ``switch``, then prune.

        Runs at :meth:`start`, on every connect and Home Assistant birth (through
        :meth:`on_birth`) and on every ``config.entities_changed``. The read is
        bounded by one :func:`asyncio.wait_for`; a stalled store returns *without
        publishing anything*, because a partial blanking is worse than a stale
        entity — the same rule :meth:`republish_all` follows.

        One tile's announcement (:meth:`_publish_selection`) also blanks that
        tile's pre-D34 read-only ``binary_sensor`` config and subscribes the one
        selection command filter, so this method is what makes the writable side
        work as well as what makes the entity visible.

        A build with no entity reader publishes no selection switches at all.
        Never raises: each tile's publish and each retraction has its own
        ``except``, so one bad tile cannot swallow the rest.

        The naming source is the tile's display name (:func:`selection_slug`) and
        the collision rule is :func:`assign_selection_slugs`: every tile of a
        colliding group is now announced under its own ``_<n>`` slug, where the
        old entity-id rule announced the first tile and silently skipped the
        second. The final slug (base plus any suffix) must fit
        :func:`is_mqtt_safe_slug`'s 128-character bound, which is why a suffix is
        never truncated to fit.

        The legacy purge is the upgrade path: the pre-HSD-jw0x slug is computable
        from the entity id even when this fresh process has no bookkeeping for it
        (the redeploy case), so an upgraded deployment's old discovery and
        retained state topics are blanked on the first republish after the
        upgrade. A legacy slug some current tile claims is skipped, and so is one
        the prune loop owns — see the comment on the loop.

        There is deliberately no publish-diff cache here, unlike the buttons: a
        selection state must never be suppressed, and re-announcing the config is
        one extra small packet per tile per republish.
        """
        reader = self._entities
        if reader is None:
            self._logger.debug(
                "no entity reader is wired into the MQTT discovery service; no selection "
                "switches will be published"
            )
            return
        try:
            listed = await asyncio.wait_for(reader.list_entities(), timeout=self._read_timeout_s)
        except TimeoutError:
            self._logger.warning(
                "reading the entities for MQTT discovery timed out after %g s; nothing was "
                "republished",
                self._read_timeout_s,
            )
            return
        except Exception:
            self._logger.exception(
                "reading the entities for MQTT discovery failed; nothing was republished"
            )
            return
        desired = assign_selection_slugs(listed)
        for slug, entity in desired.items():
            try:
                self._publish_selection(entity, slug)
            except Exception:
                self._logger.exception(
                    "publishing the selection switch for %s failed", entity.entity_id
                )
        # The legacy purge runs BEFORE the prune loop by design: the prune loop
        # retracts every slug this process has bookkeeping for and the legacy loop
        # skips exactly those, so no topic is ever blanked twice.
        for entity in listed:
            legacy = selection_entity_id_slug(entity.entity_id)
            if not legacy or legacy in desired or legacy in self._selection_slugs:
                continue
            try:
                self._retract_legacy_selection(legacy)
            except Exception:
                self._logger.exception(
                    "retracting the pre-HSD-jw0x selection switch %s failed", legacy
                )
        for slug in tuple(self._selection_slugs):
            if slug in desired:
                continue
            try:
                self._retract_selection(slug)
            except Exception:
                self._logger.exception("retracting the selection switch for %s failed", slug)

    async def purge_legacy_entities(self) -> LegacyPurgeReport:
        """Purge every PRE-RENAME entity this build can reconstruct. Never raises.

        The sibling of :meth:`_retract_legacy_selection`, one era later: that
        method busts the ghosts of an intra-release naming change (HSD-jw0x),
        this one busts the ghosts of the product rename itself (HSD-l84l). An
        upgraded app announces ``chime_deck``-grammar identifiers, and everything
        the old build announced — plus its retained selection state and its
        retained availability — would otherwise live on the broker forever,
        because discovery is not retained and a freshly redeployed process holds
        no bookkeeping for the old entities.

        Every topic is reconstructed from what this build would publish *now*,
        translated by :func:`chime_deck.mqtt.legacy.legacy_topic`, so the purge is
        deliberately NOT gated on ``self._published``/``self._selection_slugs``:
        that bookkeeping is exactly what a redeploy lacks. The plan covers every
        button with a usable slug **including disabled ones** (a button that was
        enabled in the old build left an entity that is still a ghost, and
        blanking a topic nothing published is a harmless no-op), every current
        selection slug and every pre-HSD-jw0x entity-id slug (so a deployment that
        skipped the jw0x release is covered too) **in both discovery families**
        (the pre-D34 ``binary_sensor`` and the ``switch`` this build announces),
        and the availability topic.

        The empty *retained* publishes are purges for the reason
        :meth:`_retract_selection` documents: a stale retained ``on`` (or a stale
        ``online``) on a topic nothing will ever write again would outlive the
        truth forever (D29/§9). Old **command** topics are deliberately not
        blanked: they are never retained, so there is nothing stale on them to
        purge, and a subscription is not a broker artefact that survives a
        process.

        Idempotent and without bookkeeping, exactly as the jw0x purge is: a purge
        leaves nothing behind to remember, so a second run republishes the same
        blanks and is harmless. Per topic, the translation and the publish each
        have their own ``except``, so one failure never abandons the rest; a store
        read that times out or fails leaves that half of the plan empty rather
        than aborting the purge.

        :returns: what was blanked and what failed.
        """
        buttons: list[Button] = []
        try:
            buttons = await asyncio.wait_for(
                self._buttons.list_buttons(), timeout=self._read_timeout_s
            )
        except TimeoutError:
            self._logger.warning(
                "reading the buttons for the pre-rename MQTT purge timed out after %g s; "
                "their pre-rename entities are left alone",
                self._read_timeout_s,
            )
        except Exception:
            self._logger.exception(
                "reading the buttons for the pre-rename MQTT purge failed; their pre-rename "
                "entities are left alone"
            )
        listed: list[Entity] = []
        reader = self._entities
        if reader is not None:
            try:
                listed = await asyncio.wait_for(
                    reader.list_entities(), timeout=self._read_timeout_s
                )
            except TimeoutError:
                self._logger.warning(
                    "reading the entities for the pre-rename MQTT purge timed out after %g s; "
                    "their pre-rename entities are left alone",
                    self._read_timeout_s,
                )
            except Exception:
                self._logger.exception(
                    "reading the entities for the pre-rename MQTT purge failed; their "
                    "pre-rename entities are left alone"
                )
        discovery_topics: set[str] = set()
        state_topics: set[str] = set()
        for button in buttons:
            if not is_mqtt_safe_slug(button.slug):
                self._logger.debug(
                    "button %r has an unusable slug; nothing pre-rename to purge for it",
                    button.slug,
                )
                continue
            discovery_topics.add(discovery_topic(self._settings, button.slug))
        # Both slug families: what this build names the tile now, and what the
        # pre-HSD-jw0x rule named it, so a deployment that skipped that release
        # has its pre-rename entity purged as well.
        selection_slugs = set(assign_selection_slugs(listed))
        for entity in listed:
            selection_slugs.add(selection_entity_id_slug(entity.entity_id))
        for slug in selection_slugs:
            if not slug:
                continue
            # Both families, because ``chime_deck.mqtt.legacy.legacy_topic``
            # substitutes project TOKENS only and does not touch the platform
            # segment (design 02 §1's discovery path is
            # ``<prefix>/<platform>/...``). The ``binary_sensor`` family is what
            # the pre-rename build actually published, so dropping it would
            # silently stop purging those ghosts; the ``switch`` family is what a
            # *future* rename will need.
            discovery_topics.add(selection_discovery_topic(self._settings, slug))
            discovery_topics.add(selection_read_only_discovery_topic(self._settings, slug))
            state_topics.add(selection_state_topic(self._settings, slug))
        state_topics.add(self._availability_topic)
        purged: list[str] = []
        failed: list[str] = []
        blanked_discovery = 0
        for topic in sorted(discovery_topics):
            legacy = self._blank_legacy(topic, retain=False, qos=DISCOVERY_PUBLISH_QOS)
            if legacy is None:
                failed.append(topic)
                continue
            purged.append(legacy)
            blanked_discovery += 1
        blanked_states = 0
        for topic in sorted(state_topics):
            legacy = self._blank_legacy(topic, retain=True, qos=SELECTION_STATE_QOS)
            if legacy is None:
                failed.append(topic)
                continue
            purged.append(legacy)
            blanked_states += 1
        self._logger.info(
            "purged the pre-rename MQTT topics of instance %s: %d blanked "
            "(%d discovery configs, %d retained states), %d failed",
            self._settings.instance_id,
            len(purged),
            blanked_discovery,
            blanked_states,
            len(failed),
        )
        return LegacyPurgeReport(purged=tuple(sorted(purged)), failed=tuple(failed))

    def on_selection_changed(self, entity_id: str, selected: bool) -> None:
        """Publish the retained state of one tile after a change. Never raises.

        This publishes the *state only*: the discovery config is published by
        :meth:`republish_selections`, which runs at :meth:`start`, on every
        connect/birth and on every ``config.entities_changed``, so a change that
        outruns a config publish cannot leave Home Assistant without an entity
        for longer than the next event.

        It is the single ``on_change`` the selection service calls, which is why a
        panel tap *and* an accepted Home Assistant command both land here exactly
        once: the hub frame and this retained publish come from one point.

        The slug comes from ``self._selection_slug_by_entity``, the map
        :meth:`republish_selections` fills, because a disambiguated tile's slug
        depends on the whole tile set and re-deriving it here would need an async
        store read this synchronous seam cannot do. When the map has no entry yet
        the fallback is :func:`selection_entity_id_slug` — the pre-HSD-jw0x
        entity-id slug — **deliberately**: it is the topic an entity announced
        under the old rule is still listening on, so a toggle that arrives before
        the first republish still updates the entity the operator can see.

        It deliberately does not consult ``self._selection``: the caller (the
        selection service's change callback) already holds the new value, and
        re-reading it would only let a second change in the same tick overwrite
        this one's payload with its own.

        Synchronous because the bridge's ``publish`` contract is "call it from the
        event loop", and the toggle route and the command handler already run
        there; the broker call is buffered while disconnected.

        :param entity_id: Home Assistant entity id whose selection changed.
        :param selected: the new value, ``True`` when the tile is selected.
        """
        slug = self._selection_slug_by_entity.get(entity_id) or selection_entity_id_slug(entity_id)
        if not is_mqtt_safe_slug(slug):
            self._logger.warning(
                "entity %r has no usable selection slug; cannot publish its selection state",
                entity_id,
            )
            return
        self._publish_selection_state(
            slug, SELECTION_PAYLOAD_ON if selected else SELECTION_PAYLOAD_OFF
        )

    def _publish_selection_state(self, slug: str, payload: str) -> None:
        """Publish one tile's retained selection state. Never raises.

        The one place a selection state reaches the broker, so the publish, its
        QoS and its ``retain`` flag cannot drift between its three callers: the
        change seam (:meth:`on_selection_changed`), the announce path
        (:meth:`_publish_selection`) and the corrective answer
        (:meth:`_answer_selection_command`).

        :param slug: the tile's validated selection slug.
        :param payload: :data:`SELECTION_PAYLOAD_ON` or :data:`SELECTION_PAYLOAD_OFF`.
        """
        try:
            self._bridge.publish(
                selection_state_topic(self._settings, slug),
                payload,
                qos=SELECTION_STATE_QOS,
                retain=True,
            )
        except Exception as exc:
            self._logger.warning(
                "publishing the selection state of %s failed: %s: %s",
                slug,
                type(exc).__name__,
                exc,
            )

    async def on_entities_changed(self, event: ConfigEvent) -> None:
        """Resynchronise the selection switches after a committed entity change.

        Every other event name is ignored. The resync is a *full listing* rather
        than the button-style per-slug diff on purpose: ``config.entities_changed``
        carries only numeric ``ids``/``removed_ids`` (its ``slugs`` is documented
        empty for entities), so a deleted row's slug cannot be recovered from the
        event. The announced-slug bookkeeping plus a full list is what makes the
        empty-payload retraction possible at all.

        A consequence is that editing one tile re-announces every tile: that is
        idempotent and cheap (a handful of small config packets plus one state
        packet each), and the alternative is a deleted tile's retained ``on``
        surviving forever.

        :param event: the committed configuration event.
        """
        from ..store.events import ConfigEventName as ConfigEvents

        if event.name != ConfigEvents.ENTITIES_CHANGED:
            return
        try:
            await self.republish_selections()
        except Exception:
            self._logger.exception("handling an entities-changed event failed")

    async def on_config_changed(self, event: ConfigEvent) -> None:
        """The single bus subscriber: dispatch one committed configuration event.

        ``config.buttons_changed`` keeps its per-slug lifecycle
        (:meth:`on_buttons_changed`), ``config.entities_changed`` resynchronises
        the selection switches (:meth:`on_entities_changed`), and every other name
        is ignored. One handler means one subscription and one unsubscribe, which
        is what :meth:`start` and :meth:`stop` maintain.

        The function-local import costs one ``sys.modules`` lookup per event and
        keeps this module free of a module-level ``..store`` import (see
        :meth:`on_buttons_changed`).

        :param event: the committed configuration event.
        """
        from ..store.events import ConfigEventName as ConfigEvents

        if event.name == ConfigEvents.BUTTONS_CHANGED:
            await self.on_buttons_changed(event)
        elif event.name == ConfigEvents.ENTITIES_CHANGED:
            await self.on_entities_changed(event)

    async def on_buttons_changed(self, event: ConfigEvent) -> None:
        """Apply one committed ``config.buttons_changed`` event.

        Every other event name is ignored: a TTS-engine change emits a button
        event with ids but no slugs, and that must never become a full republish.
        This module declares no module-level ``..store`` import, so the one-way
        layering rule holds for ``discovery.py`` itself; the function-local import
        is only a per-event ``sys.modules`` dict lookup, costing microseconds on a
        configuration event. It is not an isolation device: importing
        :mod:`chime_deck.mqtt` already loads the store package transitively
        through ``mqtt.bridge`` → ``config`` → ``store``.

        :param event: the committed configuration event.
        """
        from ..store.events import ConfigEventName as ButtonsChanged

        if event.name != ButtonsChanged.BUTTONS_CHANGED:
            return
        try:
            await self._handle_event(event.payload)
        except Exception:
            self._logger.exception("handling a buttons-changed event failed")

    async def on_message(self, topic: str, payload: bytes, retain: bool = False) -> None:
        """Route one incoming message: a press, else a selection command.

        The bridge hands over the broker's RETAIN flag for *this delivery* — a
        replay marker, not the publisher's intent — because a retained selection
        command is a replay of an old intent and must be refused
        (:meth:`_handle_selection_command`). ``False`` is the default so a caller
        that has no flag (a test, a backfill) still reads as "live".

        The press half is unchanged: the payload comparison is strict bytes
        equality, so ``b""``, ``b"press"``, ``b"PRESS\\n"``, ``b"PRESS "`` and
        binary garbage are all ignored, with no decode and no strip. There is no
        timeout around ``press`` on purpose: the bridge already bounds the whole
        callback with ``seam_timeout_s``, so a second, shorter deadline here would
        only pre-empt the one owner of the budget — and a cancel between
        ``PressService.press`` arming its dedupe window and writing its
        ``press_log`` row would lose the row and suppress the retry.

        The command half accepts one well-formed slug on a topic this build can
        parse (:meth:`_selection_slug_from_command_topic`) and never raises: a
        malformed, unknown or replayed command is answered with a republish of the
        current state rather than an exception, because Home Assistant's switch is
        *optimistic* and would otherwise sit on a value no one confirmed.

        A ``homeassistant/status`` message never arrives here: the bridge consumes
        its own status topic before the message seam and owns the
        jitter/birth/availability ordering (design 02 §6 deviation, erratum 2).

        :param topic: topic the message arrived on.
        :param payload: raw payload bytes.
        :param retain: the broker's RETAIN flag for this delivery; ``True`` means
            the broker is replaying a stored message, not delivering a live one.
        """
        slug = self._topic_slugs.get(topic)
        if slug is not None:
            if payload != PRESS_BYTES:
                self._logger.debug(
                    "ignoring the payload %r on %s: only %r presses the button",
                    payload,
                    topic,
                    PRESS_PAYLOAD,
                )
                return
            await self._press.press(slug, source=MQTT_PRESS_SOURCE)
            return
        command_slug = self._selection_slug_from_command_topic(topic)
        if command_slug is None:
            self._logger.debug("ignoring an MQTT message on the unknown topic %s", topic)
            return
        self._handle_selection_command(command_slug, payload, retain=retain)

    def _selection_slug_from_command_topic(self, topic: str) -> str | None:
        """Return the selection slug a command topic names, or ``None``.

        Parsed by slicing, never by a regex: the prefix, the ``/set`` suffix and a
        single well-formed slug in between are the whole grammar, and
        :data:`SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE` is the one place it is
        spelled — the same template :func:`selection_command_filter` subscribes
        with, so the filter and the parser cannot disagree.

        This is why the ``+/set`` wildcard is safe. A stale switch whose tile was
        deleted still publishes to a *knowable* slug (Home Assistant keeps trying
        its last known command topic), so the command lands on a slug this process
        can look up and gets a corrective answer instead of silence. Anything that
        is not exactly one well-formed slug — an empty middle, a nested slash, a
        slug :func:`is_mqtt_safe_slug` refuses — is not a selection command at all.

        :param topic: topic an incoming message arrived on.
        :returns: the slug the topic addresses, or ``None`` when it is not one of
            this instance's selection command topics.
        """
        prefix = SELECTION_COMMAND_TOPIC_PREFIX_TEMPLATE.format(instance=self._settings.instance_id)
        suffix = "/set"
        if not topic.startswith(prefix) or not topic.endswith(suffix):
            return None
        middle = topic[len(prefix) : -len(suffix)]
        if not middle or "/" in middle:
            return None
        if not is_mqtt_safe_slug(middle):
            return None
        return middle

    def _handle_selection_command(self, slug: str, payload: bytes, *, retain: bool) -> None:
        """Apply one inbound Home Assistant command to a tile's selection.

        Synchronous, like :meth:`on_selection_changed`, because
        :meth:`~chime_deck.selection.SelectionService.set` and
        ``bridge.publish`` are both called from the event loop and the bridge's
        publish contract is "call me from the event loop"; the broker call is
        buffered while disconnected.

        The value is *set*, never flipped: a command names the value it wants, so
        a repeated ``on`` is an idempotent confirm, not a toggle. Every refusal is
        answered with a republish of the current state — never swallowed, because
        Home Assistant's switch is optimistic and would keep showing a value no
        one accepted:

        1. a **retained** command is refused: it is a replay of an old intent, and
           acting on it would re-arm the tile on every reconnect;
        2. a payload that is neither ``b"on"`` nor ``b"off"`` is refused, and the
           decision is never made on a decoded string;
        3. a slug no *displayed* tile has is refused;
        4. a command that repeats the current value is a confirm: it answers once
           and produces no ``on_change``, so no hub frame and no second publish;
        5. an accepted command goes through the selection service's single write,
           and a writer that did not take the value (e.g. :data:`NO_SELECTION`) is
           answered rather than left to make HA's optimism a lie.

        :param slug: selection slug the command topic addressed.
        :param payload: raw payload bytes.
        :param retain: whether the broker replayed this message.
        """
        if retain:
            self._logger.info(
                "refused the retained command %r on %s: a retained command is a replay of an old "
                "intent, not a live one, and processing it would re-arm the tile on every "
                "reconnect; republished its state",
                payload,
                selection_command_topic(self._settings, slug),
            )
            self._answer_selection_command(slug)
            return
        if payload == SELECTION_PAYLOAD_ON.encode():
            selected = True
        elif payload == SELECTION_PAYLOAD_OFF.encode():
            selected = False
        else:
            self._logger.info(
                "ignored the malformed command %r on %s: only b'on' and b'off' set a tile; "
                "republished its state",
                payload,
                selection_command_topic(self._settings, slug),
            )
            self._answer_selection_command(slug)
            return
        entity_id = self._selection_slugs.get(slug)
        if entity_id is None:
            self._logger.info(
                "refused the command %r on %s: no displayed tile has that selection slug; "
                "republished its state",
                payload,
                selection_command_topic(self._settings, slug),
            )
            self._answer_selection_command(slug)
            return
        if self._selection.is_selected(entity_id) == selected:
            self._logger.debug(
                "the command on %s already matches the current state; republished it without churn",
                selection_command_topic(self._settings, slug),
            )
            self._answer_selection_command(slug)
            return
        self._selection.set(entity_id, selected)
        if self._selection.is_selected(entity_id) != selected:
            self._logger.info(
                "the selection service did not take %r on %s; republished its state",
                selected,
                selection_command_topic(self._settings, slug),
            )
            self._answer_selection_command(slug)

    def _answer_selection_command(self, slug: str) -> None:
        """Republish one tile's state so Home Assistant's optimistic switch snaps back.

        The corrective answer every refusal ends with. A slug this process does not
        know is answered ``off``: an unknown id is unselected by contract, so a
        tile that is not displayed (a deleted one, whose switch HA has not dropped
        yet) is told the truth instead of being left on.

        The publish itself is guarded by :meth:`_publish_selection_state`, so a
        broker problem is logged there once rather than twice.

        :param slug: selection slug the refused command addressed.
        """
        entity_id = self._selection_slugs.get(slug)
        selected = entity_id is not None and self._selection.is_selected(entity_id)
        self._publish_selection_state(
            slug, SELECTION_PAYLOAD_ON if selected else SELECTION_PAYLOAD_OFF
        )

    async def _handle_event(self, payload: Mapping[str, object]) -> None:
        """Apply the three lifecycle phases of one buttons-changed payload.

        Renames first (retract the old entity, then publish the new one), then
        deletions, then the created/updated slugs. Every item has its own
        ``except``: the bus catches per handler only, so one bad slug must not
        swallow the rest of a committed event.

        :param payload: the event's frozen payload mapping.
        """
        renamed = _rename_pairs(payload.get("renamed"))
        removed = _string_tuple(payload.get("removed_slugs"))
        slugs = _string_tuple(payload.get("slugs"))
        handled: set[str] = set()
        deferred: set[str] = set()

        for old, new in renamed:
            try:
                if new in handled:
                    continue
                if not is_mqtt_safe_slug(new):
                    self._logger.warning(
                        "button slug %r is not usable in an MQTT topic; retracting %r and "
                        "publishing nothing for the new slug",
                        new,
                        old,
                    )
                    self._retract(old)
                    handled.update((old, new))
                    continue
                try:
                    button = await self._load(new)
                except _ReadTimeout:
                    # A transient stall must never blank a live entity: leave both
                    # the old and the new topic exactly as they are.
                    deferred.add(new)
                    continue
                self._retract(old)
                handled.update((old, new))
                if button is not None and button.enabled:
                    self._publish(button)
            except Exception:
                self._logger.exception("handling the rename %r -> %r failed", old, new)

        for slug in removed:
            try:
                if slug in handled:
                    continue
                self._retract(slug)
                handled.add(slug)
            except Exception:
                self._logger.exception("retracting the discovery payload for %r failed", slug)

        ordered: dict[str, None] = {}
        for _, new in renamed:
            ordered.setdefault(new, None)
        for slug in slugs:
            ordered.setdefault(slug, None)
        for slug in ordered:
            try:
                if slug in handled or slug in deferred:
                    continue
                if not is_mqtt_safe_slug(slug):
                    self._logger.warning(
                        "button slug %r is not usable in an MQTT topic; skipping its discovery "
                        "payload",
                        slug,
                    )
                    handled.add(slug)
                    continue
                try:
                    await self._sync(slug)
                except _ReadTimeout:
                    self._logger.debug(
                        "leaving the discovery payload for %s alone: its row read stalled", slug
                    )
                    continue
                handled.add(slug)
            except Exception:
                self._logger.exception("synchronising the discovery payload for %r failed", slug)

    async def _load(self, slug: str) -> Button | None:
        """Read one button, bounded, distinguishing a stall from a missing row.

        :param slug: button slug to read.
        :raises _ReadTimeout: if the read did not finish inside the bound.
        """
        try:
            return await asyncio.wait_for(
                self._buttons.get_button_by_slug(slug), timeout=self._read_timeout_s
            )
        except TimeoutError as exc:
            self._logger.warning(
                "reading button %r for MQTT discovery timed out after %g s; leaving its "
                "entity untouched",
                slug,
                self._read_timeout_s,
            )
            raise _ReadTimeout(slug) from exc

    async def _sync(self, slug: str) -> None:
        """Bring one slug's entity in step with the store.

        A missing or disabled row, and a slug that turned unsafe, retract the
        entity. An identical payload is a quiet no-op; anything else publishes.

        :param slug: button slug to synchronise.
        """
        button = await self._load(slug)
        if button is None or not button.enabled:
            self._retract(slug)
            return
        if not is_mqtt_safe_slug(button.slug):
            self._logger.warning(
                "button %r has an unusable slug; retracting its discovery payload", button.slug
            )
            self._retract(slug)
            return
        if self._published.get(slug) == self._serialise(button):
            self._logger.debug("the MQTT discovery payload for %s is unchanged", slug)
            return
        self._publish(button)

    def _publish(self, button: Button) -> None:
        """Publish one discovery payload and start routing its command topic.

        The command subscription is added only when it is not already routed, so
        an updated or republished button does not re-SUBSCRIBE on every event.

        :param button: button whose payload should be published.
        """
        topic = command_topic(self._settings, button.slug)
        payload = self._serialise(button)
        self._bridge.publish(
            discovery_topic(self._settings, button.slug),
            payload,
            qos=DISCOVERY_PUBLISH_QOS,
            retain=False,
        )
        if topic not in self._topic_slugs:
            self._bridge.subscribe(topic, PRESS_QOS)
        self._topic_slugs[topic] = button.slug
        self._published[button.slug] = payload

    def _retract(self, slug: str) -> None:
        """Remove one entity from Home Assistant and stop routing its presses.

        An empty payload is the only removal signal Home Assistant has for a
        non-retained discovery topic. When the command topic was never routed
        there is nothing published to remove and no filter to drop, so this is a
        quiet no-op.

        :param slug: button slug whose entity should be removed.
        """
        topic = command_topic(self._settings, slug)
        if topic not in self._topic_slugs:
            self._logger.debug("nothing to retract for %s: its command topic is not routed", slug)
            return
        self._bridge.publish(
            discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=False,
        )
        self._bridge.unsubscribe(topic)
        self._topic_slugs.pop(topic, None)
        self._published[slug] = None

    def _serialise(self, button: Button) -> str:
        """Return the serialised discovery payload of one button.

        :param button: button to serialise.
        """
        return encode_payload(
            build_discovery_payload(
                button,
                settings=self._settings,
                availability_topic=self._availability_topic,
                configuration_url=self._configuration_url,
                sw_version=self._sw_version,
            )
        )

    def _publish_selection(self, entity: Entity, slug: str) -> None:
        """Announce one tile's writable switch and its retained state.

        Four steps, in this order:

        1. Blank the PRE-D34 read-only ``binary_sensor`` config with a **retained**
           empty payload. It is unconditional on every republish, not gated on
           ``self._selection_slugs``: the blank is idempotent and leaves nothing to
           remember, so an install whose Home Assistant was down or restarting when
           it upgraded still converges on the next republish. Retained *here*,
           while the switch config below is not, because this payload is a
           *removal*: Home Assistant's empty-config signal and the retained config a
           pre-D34 build may have left on the broker are the same message, and only
           the retained form clears the broker's copy too.
        2. Subscribe the one selection command filter, once, on the first announce
           — the same "subscribe once, in the announce path" rule
           :meth:`_publish` follows for the press topics, so a republish never
           re-SUBSCRIBEs, not even for a slug deleted and re-created in between.
        3. Publish the switch config, non-retained like a button's, **before** any
           state: Home Assistant has to know the entity when the value arrives.
        4. Publish the retained state at :data:`SELECTION_STATE_QOS`.

        :param entity: the displayed entity to announce.
        :param slug: its already-validated selection slug.
        """
        self._bridge.publish(
            selection_read_only_discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=True,
        )
        if not self._selection_commands_subscribed:
            self._bridge.subscribe(selection_command_filter(self._settings), SELECTION_COMMAND_QOS)
            self._selection_commands_subscribed = True
        self._bridge.publish(
            selection_discovery_topic(self._settings, slug),
            self._serialise_selection(entity, slug),
            qos=DISCOVERY_PUBLISH_QOS,
            retain=False,
        )
        self._publish_selection_state(slug, self._selection_state(entity.entity_id))
        self._selection_slugs[slug] = entity.entity_id
        self._selection_slug_by_entity[entity.entity_id] = slug

    def _retract_selection(self, slug: str) -> None:
        """Remove one tile's switch, including its retained state value.

        Three blanks, because a delete must leave nothing behind: the PRE-D34
        read-only ``binary_sensor`` config (a retained empty payload, so a broker
        that still holds that config from an earlier build drops it as well), the
        switch config (an empty *non-retained* payload is the only removal signal
        Home Assistant has for it) and the empty **retained** state, without which
        a deleted tile's retained ``on`` would outlive it forever on a topic
        nothing will ever write again — the exact stale-retained hazard D29/§9
        exists to avoid.

        A slug that was never announced is a quiet no-op, mirroring
        :meth:`_retract`.

        :param slug: selection slug whose switch should be removed.
        """
        if slug not in self._selection_slugs:
            self._logger.debug(
                "nothing to retract for the selection switch %s: it was never announced", slug
            )
            return
        self._bridge.publish(
            selection_read_only_discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=True,
        )
        self._bridge.publish(
            selection_discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=False,
        )
        self._bridge.publish(
            selection_state_topic(self._settings, slug),
            "",
            qos=SELECTION_STATE_QOS,
            retain=True,
        )
        self._selection_slugs.pop(slug, None)
        # Drop the reverse entries by VALUE, not by entity id: the rename path
        # publishes the new slug before pruning the old one, so the entity's entry
        # already points at the new slug here and must survive. Popping by entity id
        # unconditionally would delete that mapping and send every later toggle of
        # the tile to the dead topic.
        for stale_entity_id in tuple(self._selection_slug_by_entity):
            if self._selection_slug_by_entity[stale_entity_id] == slug:
                del self._selection_slug_by_entity[stale_entity_id]

    def _retract_legacy_selection(self, slug: str) -> None:
        """Purge one PRE-HSD-jw0x selection entity from the broker.

        The upgrade ghost-buster, and it now covers **both eras**: a pre-jw0x build
        published a ``binary_sensor`` under an entity-id slug, a jw0x build
        published a ``binary_sensor`` under the display-name slug, and this build
        publishes a ``switch``. So the blank covers the read-only config of either
        old naming rule, the switch config of the new one, and the retained state
        either of them wrote.

        The old slug is still *computable* from the entity id
        (:func:`selection_entity_id_slug`), which is what makes this possible for a
        freshly restarted process that holds no bookkeeping for the old entity at
        all.

        It is deliberately NOT gated on ``self._selection_slugs`` membership — that
        gate is exactly the knowledge the redeploy case lacks — and it does no
        bookkeeping, because a purge leaves nothing behind to remember. The
        retained blanks are purges for the reason :meth:`_retract_selection`
        documents: a retained ``on``, and a retained pre-D34 config, on a topic
        nothing will ever write again would outlive the truth forever (D29/§9).

        :param slug: legacy selection slug to purge.
        """
        self._bridge.publish(
            selection_read_only_discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=True,
        )
        self._bridge.publish(
            selection_discovery_topic(self._settings, slug),
            "",
            qos=DISCOVERY_PUBLISH_QOS,
            retain=False,
        )
        self._bridge.publish(
            selection_state_topic(self._settings, slug),
            "",
            qos=SELECTION_STATE_QOS,
            retain=True,
        )
        self._logger.debug("purged the pre-HSD-jw0x selection topics for %s", slug)

    def _blank_legacy(self, topic: str, *, retain: bool, qos: int) -> str | None:
        """Translate one current topic to its pre-rename form and blank it.

        The one place the purge touches the wire, so the translation and the
        empty payload can never drift apart. Each step has its own ``except``:
        a topic this build cannot translate, and a publish the bridge refuses,
        are both logged and both leave the remaining topics untouched.

        Blanking a pre-rename topic nothing ever published is a deliberate
        no-op, not an error: the plan is built from today's configuration, so it
        cannot tell "the old build published this" from "it did not".

        :param topic: the topic this build would publish today.
        :param retain: whether the blank must be retained to purge a stale value.
        :param qos: delivery guarantee for the blank.
        :returns: the blanked pre-rename topic, or ``None`` when the translation
            had nothing to translate or the publish raised.
        """
        try:
            legacy = legacy_topic(topic)
        except ValueError:
            self._logger.exception(
                "the topic %s carries no current project token; there is nothing to purge", topic
            )
            return None
        try:
            self._bridge.publish(legacy, "", qos=qos, retain=retain)
        except Exception:
            self._logger.exception("blanking the pre-rename topic %s failed", legacy)
            return None
        self._logger.debug("purged the pre-rename topic %s (retain=%s)", legacy, retain)
        return legacy

    def _serialise_selection(self, entity: Entity, slug: str) -> str:
        """Return the serialised discovery payload of one tile's selection switch.

        :param entity: displayed entity to serialise.
        :param slug: its resolved selection slug.
        """
        return encode_payload(
            build_selection_payload(
                entity,
                slug=slug,
                settings=self._settings,
                availability_topic=self._availability_topic,
                configuration_url=self._configuration_url,
                sw_version=self._sw_version,
            )
        )

    def _selection_state(self, entity_id: str) -> str:
        """Return the state payload the selection seam reports for one entity.

        :param entity_id: Home Assistant entity id to ask the selection seam about.
        """
        if self._selection.is_selected(entity_id):
            return SELECTION_PAYLOAD_ON
        return SELECTION_PAYLOAD_OFF
