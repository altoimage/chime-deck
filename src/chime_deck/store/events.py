"""Dependency-free in-process configuration change events.

The store never imports FastAPI, websockets or any HTTP library: backend
components subscribe in process and decide what to do with the event. The
web hub (HSD-8gft) forwards events as ``{"type": "config_changed", "data":
{"what": ...}}``; the MQTT bridge uses ``removed_slugs`` and ``renamed`` to
retract discovery topics for buttons that disappeared or changed slug.

Every :class:`ConfigEvent` payload has the same key set so consumers can rely
on the shape:

``ids``
    Ids of rows that were created or updated.
``removed_ids``
    Ids of rows that were deleted.
``slugs``
    Slugs of rows that were created or updated (buttons only; empty tuple
    for entities and sounds).
``removed_slugs``
    Slugs of rows that were deleted, so MQTT discovery can clear the old
    topic. Renames are reported in ``renamed`` rather than here.
``renamed``
    Pairs of ``(old_slug, new_slug)`` for buttons whose slug changed. A
    rename changes the MQTT unique id, so the old discovery topic must be
    removed as well as the new one published.
``keys``
    Setting keys that changed (settings events only).

Payloads are built only by the typed constructors below, which freeze the
mapping and force every value to be a tuple. There is deliberately no
``press_log`` event: the log is high volume and UI-only.

Owning ticket: HSD-2sem.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "ConfigEvent",
    "ConfigEventName",
    "EventBus",
    "EventHandler",
    "button_event",
    "entity_event",
    "settings_event",
    "sound_event",
]


class ConfigEventName(StrEnum):
    """Names of the configuration events published by the store."""

    ENTITIES_CHANGED = "config.entities_changed"
    BUTTONS_CHANGED = "config.buttons_changed"
    SOUNDS_CHANGED = "config.sounds_changed"
    SETTINGS_CHANGED = "config.settings_changed"


_CONFIG_EVENT_NAMES: Mapping[str, ConfigEventName] = MappingProxyType(
    {
        "entities": ConfigEventName.ENTITIES_CHANGED,
        "buttons": ConfigEventName.BUTTONS_CHANGED,
        "sounds": ConfigEventName.SOUNDS_CHANGED,
        "settings": ConfigEventName.SETTINGS_CHANGED,
    }
)


@dataclass(frozen=True, slots=True)
class ConfigEvent:
    """One committed configuration change."""

    name: ConfigEventName
    what: str
    payload: Mapping[str, object]


def _build_event(
    *,
    what: str,
    ids: Iterable[int] = (),
    removed_ids: Iterable[int] = (),
    slugs: Iterable[str] = (),
    removed_slugs: Iterable[str] = (),
    renamed: Iterable[tuple[str, str]] = (),
    keys: Iterable[str] = (),
) -> ConfigEvent:
    """Build an event with a fully populated, immutable payload.

    :param what: one of ``entities``, ``buttons``, ``sounds`` or ``settings``.
    :param ids: ids created or updated.
    :param removed_ids: ids deleted.
    :param slugs: slugs created or updated.
    :param removed_slugs: slugs deleted.
    :param renamed: ``(old_slug, new_slug)`` pairs.
    :param keys: setting keys changed.
    """
    payload: dict[str, object] = {
        "ids": tuple(ids),
        "removed_ids": tuple(removed_ids),
        "slugs": tuple(slugs),
        "removed_slugs": tuple(removed_slugs),
        "renamed": tuple(renamed),
        "keys": tuple(keys),
    }
    return ConfigEvent(
        name=_CONFIG_EVENT_NAMES[what],
        what=what,
        payload=MappingProxyType(payload),
    )


def entity_event(
    *,
    ids: Iterable[int] = (),
    removed_ids: Iterable[int] = (),
    slugs: Iterable[str] = (),
    removed_slugs: Iterable[str] = (),
    renamed: Iterable[tuple[str, str]] = (),
    keys: Iterable[str] = (),
) -> ConfigEvent:
    """Build a ``config.entities_changed`` event.

    :param ids: ids created or updated.
    :param removed_ids: ids deleted.
    :param slugs: slugs created or updated.
    :param removed_slugs: slugs deleted.
    :param renamed: ``(old_slug, new_slug)`` pairs.
    :param keys: setting keys changed.
    """
    return _build_event(
        what="entities",
        ids=ids,
        removed_ids=removed_ids,
        slugs=slugs,
        removed_slugs=removed_slugs,
        renamed=renamed,
        keys=keys,
    )


def button_event(
    *,
    ids: Iterable[int] = (),
    removed_ids: Iterable[int] = (),
    slugs: Iterable[str] = (),
    removed_slugs: Iterable[str] = (),
    renamed: Iterable[tuple[str, str]] = (),
    keys: Iterable[str] = (),
) -> ConfigEvent:
    """Build a ``config.buttons_changed`` event.

    :param ids: ids created or updated.
    :param removed_ids: ids deleted.
    :param slugs: slugs created or updated.
    :param removed_slugs: slugs deleted.
    :param renamed: ``(old_slug, new_slug)`` pairs.
    :param keys: setting keys changed.
    """
    return _build_event(
        what="buttons",
        ids=ids,
        removed_ids=removed_ids,
        slugs=slugs,
        removed_slugs=removed_slugs,
        renamed=renamed,
        keys=keys,
    )


def sound_event(
    *,
    ids: Iterable[int] = (),
    removed_ids: Iterable[int] = (),
    slugs: Iterable[str] = (),
    removed_slugs: Iterable[str] = (),
    renamed: Iterable[tuple[str, str]] = (),
    keys: Iterable[str] = (),
) -> ConfigEvent:
    """Build a ``config.sounds_changed`` event.

    :param ids: ids created or updated.
    :param removed_ids: ids deleted.
    :param slugs: slugs created or updated.
    :param removed_slugs: slugs deleted.
    :param renamed: ``(old_slug, new_slug)`` pairs.
    :param keys: setting keys changed.
    """
    return _build_event(
        what="sounds",
        ids=ids,
        removed_ids=removed_ids,
        slugs=slugs,
        removed_slugs=removed_slugs,
        renamed=renamed,
        keys=keys,
    )


def settings_event(
    *,
    ids: Iterable[int] = (),
    removed_ids: Iterable[int] = (),
    slugs: Iterable[str] = (),
    removed_slugs: Iterable[str] = (),
    renamed: Iterable[tuple[str, str]] = (),
    keys: Iterable[str] = (),
) -> ConfigEvent:
    """Build a ``config.settings_changed`` event.

    :param ids: ids created or updated.
    :param removed_ids: ids deleted.
    :param slugs: slugs created or updated.
    :param removed_slugs: slugs deleted.
    :param renamed: ``(old_slug, new_slug)`` pairs.
    :param keys: setting keys changed.
    """
    return _build_event(
        what="settings",
        ids=ids,
        removed_ids=removed_ids,
        slugs=slugs,
        removed_slugs=removed_slugs,
        renamed=renamed,
        keys=keys,
    )


EventHandler = Callable[[ConfigEvent], Awaitable[None] | None]
"""A sync or async callable that receives published configuration events."""


class EventBus:
    """In-process fan-out for configuration events.

    Handlers are awaited sequentially so a subscriber sees events in commit
    order. Queue subscribers receive events without blocking the writer; a
    full queue drops its oldest event. The bus never creates background
    tasks, so shutting it down cannot leave dangling work behind.
    """

    def __init__(self) -> None:
        """Create an empty bus."""
        self._handlers: list[EventHandler] = []
        self._queues: list[asyncio.Queue[ConfigEvent]] = []
        self._logger = logging.getLogger(__name__)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register a handler and return a callable that removes it.

        :param handler: sync or async callable receiving each event.
        """
        self._handlers.append(handler)

        def unsubscribe() -> None:
            """Remove this handler from the bus."""
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return unsubscribe

    def subscribe_queue(self, *, maxsize: int = 64) -> asyncio.Queue[ConfigEvent]:
        """Create and register a bounded queue subscriber.

        :param maxsize: maximum queued events; the oldest is dropped when full.
        """
        queue: asyncio.Queue[ConfigEvent] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(queue)
        return queue

    def unsubscribe_queue(self, queue: asyncio.Queue[ConfigEvent]) -> None:
        """Stop delivering events to a queue.

        :param queue: queue previously returned by :meth:`subscribe_queue`.
        """
        with contextlib.suppress(ValueError):
            self._queues.remove(queue)

    async def publish(self, event: ConfigEvent) -> None:
        """Deliver an event to every current subscriber.

        Handler failures are logged and swallowed: a subscriber must never be
        able to fail a write that has already committed.

        :param event: event to deliver.
        """
        for handler in tuple(self._handlers):
            try:
                result = handler(event)
                if result is not None:
                    await result
            except Exception:
                self._logger.exception("config event handler failed for %s", event.name)

        for queue in tuple(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
