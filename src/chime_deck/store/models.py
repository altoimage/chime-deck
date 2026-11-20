"""Immutable row and write-input models for the configuration store.

Each row-backed model exposes a ``from_row`` classmethod that converts an
:class:`aiosqlite.Row` into the dataclass, including the SQLite INTEGER to
boolean conversion and the JSON text to tuple conversion used by
``entities.show_attributes``.

Owning ticket: HSD-2sem.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

__all__ = [
    "UNSET",
    "Button",
    "ButtonIn",
    "Entity",
    "EntityIn",
    "ImportReport",
    "PressLog",
    "PressLogIn",
    "Sound",
    "SoundDeleteResult",
    "SoundIn",
    "SoundUpdate",
]


class _Unset:
    """Sentinel type marking an input field that should be left unchanged."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Return the stable representation used in debug output."""
        return "UNSET"


UNSET: Final = _Unset()
"""Sentinel for :class:`SoundUpdate` fields that must not be written."""


@dataclass(frozen=True, slots=True)
class Entity:
    """One configured Home Assistant status tile.

    ``state_tones`` holds *explicit overrides only*, keyed by the raw Home
    Assistant state string: the built-in default table lives in
    :mod:`chime_deck.icons.tones`, so a release can improve it without a
    data migration or rewriting rows a user never touched.
    """

    id: int
    entity_id: str
    display_name: str | None
    icon: str | None
    accent_color: str | None
    show_attributes: tuple[str, ...] | None
    state_tones: Mapping[str, str] | None
    hide_when_unavailable: bool
    colour_by_state: bool
    sort_order: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build an entity from a database row.

        :param row: row selected from the ``entities`` table.
        """
        raw_attributes = row["show_attributes"]
        attributes = (
            None
            if raw_attributes is None
            else tuple(str(item) for item in json.loads(raw_attributes))
        )
        raw_tones = row["state_tones"]
        state_tones = (
            None
            if raw_tones is None
            else {str(key): str(value) for key, value in json.loads(raw_tones).items()}
        )
        return cls(
            id=int(row["id"]),
            entity_id=str(row["entity_id"]),
            display_name=row["display_name"],
            icon=row["icon"],
            accent_color=row["accent_color"],
            show_attributes=attributes,
            state_tones=state_tones,
            hide_when_unavailable=bool(row["hide_when_unavailable"]),
            colour_by_state=bool(row["colour_by_state"]),
            sort_order=int(row["sort_order"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class EntityIn:
    """Write input for :meth:`ConfigStore.upsert_entity`.

    ``id`` is ``None`` for an insert. ``sort_order`` is ``None`` on insert to
    append after the current last entity, and ``None`` on update to keep the
    stored ordering.

    ``state_tones`` carries explicit state→tone overrides only; ``None`` means
    the tile follows the built-in default table unchanged.
    """

    entity_id: str
    id: int | None = None
    display_name: str | None = None
    icon: str | None = None
    accent_color: str | None = None
    show_attributes: tuple[str, ...] | None = None
    state_tones: Mapping[str, str] | None = None
    hide_when_unavailable: bool = False
    colour_by_state: bool = True
    sort_order: int | None = None


@dataclass(frozen=True, slots=True)
class Sound:
    """One indexed sound in the library."""

    id: int
    name: str
    source_type: str
    path: str | None
    url: str | None
    sha256: str | None
    format: str | None
    duration_ms: int | None
    bytes: int | None
    missing: bool
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a sound from a database row.

        :param row: row selected from the ``sounds`` table.
        """
        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            source_type=str(row["source_type"]),
            path=row["path"],
            url=row["url"],
            sha256=row["sha256"],
            format=row["format"],
            duration_ms=row["duration_ms"],
            bytes=row["bytes"],
            missing=bool(row["missing"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class SoundIn:
    """Write input for :meth:`ConfigStore.create_sound`."""

    name: str
    source_type: str
    path: str | None = None
    url: str | None = None
    sha256: str | None = None
    format: str | None = None
    duration_ms: int | None = None
    bytes: int | None = None
    missing: bool = False


@dataclass(frozen=True, slots=True)
class SoundUpdate:
    """All-optional patch for :meth:`ConfigStore.update_sound`.

    Fields left as :data:`UNSET` are not written. A nullable field set to
    ``None`` explicitly writes SQL ``NULL``, which is why the sentinel exists.
    """

    name: str | _Unset = UNSET
    source_type: str | _Unset = UNSET
    path: str | _Unset | None = UNSET
    url: str | _Unset | None = UNSET
    sha256: str | _Unset | None = UNSET
    format: str | _Unset | None = UNSET
    duration_ms: int | _Unset | None = UNSET
    bytes: int | _Unset | None = UNSET
    missing: bool | _Unset = UNSET


@dataclass(frozen=True, slots=True)
class Button:
    """One chime button exposed to Home Assistant over MQTT."""

    id: int
    slug: str
    name: str
    icon: str | None
    action_type: str
    sound_id: int | None
    tts_text: str | None
    tts_engine: str | None
    baked_sha: str | None
    baked_format: str | None
    baked_at: str | None
    baked_error: str | None
    volume_override: int | None
    dedupe_seconds: int | None
    show_on_dashboard: bool
    quiet_hours_exempt: bool
    enabled: bool
    sort_order: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a button from a database row.

        :param row: row selected from the ``buttons`` table.
        """
        return cls(
            id=int(row["id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            icon=row["icon"],
            action_type=str(row["action_type"]),
            sound_id=row["sound_id"],
            tts_text=row["tts_text"],
            tts_engine=row["tts_engine"],
            baked_sha=row["baked_sha"],
            baked_format=row["baked_format"],
            baked_at=row["baked_at"],
            baked_error=row["baked_error"],
            volume_override=row["volume_override"],
            dedupe_seconds=row["dedupe_seconds"],
            show_on_dashboard=bool(row["show_on_dashboard"]),
            quiet_hours_exempt=bool(row["quiet_hours_exempt"]),
            enabled=bool(row["enabled"]),
            sort_order=int(row["sort_order"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class ButtonIn:
    """Write input for :meth:`ConfigStore.upsert_button`.

    ``id`` is ``None`` for an insert. ``slug`` is ``None`` to derive the slug
    from ``name`` on insert, or to preserve the stored slug on update. Bake
    state is deliberately absent: it is written by the dedicated bake methods.
    """

    name: str
    action_type: str
    id: int | None = None
    slug: str | None = None
    icon: str | None = None
    sound_id: int | None = None
    tts_text: str | None = None
    tts_engine: str | None = None
    volume_override: int | None = None
    dedupe_seconds: int | None = None
    show_on_dashboard: bool = False
    quiet_hours_exempt: bool = False
    enabled: bool = True
    sort_order: int | None = None


@dataclass(frozen=True, slots=True)
class PressLog:
    """One row of the press history."""

    id: int
    button_id: int | None
    button_name: str
    pressed_at: str
    source: str
    outcome: str
    detail: str | None
    sound_name: str | None
    duration_ms: int | None
    job_id: str | None
    latency_ms: int | None
    synthesis_ms: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a press log entry from a database row.

        :param row: row selected from the ``press_log`` table.
        """
        return cls(
            id=int(row["id"]),
            button_id=row["button_id"],
            button_name=str(row["button_name"]),
            pressed_at=str(row["pressed_at"]),
            source=str(row["source"]),
            outcome=str(row["outcome"]),
            detail=row["detail"],
            sound_name=row["sound_name"],
            duration_ms=row["duration_ms"],
            job_id=row["job_id"],
            latency_ms=row["latency_ms"],
            synthesis_ms=row["synthesis_ms"],
        )


@dataclass(frozen=True, slots=True)
class PressLogIn:
    """Write input for :meth:`ConfigStore.log_press`.

    ``pressed_at`` defaults to the current UTC time when omitted.
    """

    button_id: int | None
    button_name: str
    source: str
    outcome: str
    pressed_at: str | None = None
    detail: str | None = None
    sound_name: str | None = None
    duration_ms: int | None = None
    job_id: str | None = None
    synthesis_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SoundDeleteResult:
    """Outcome of :meth:`ConfigStore.delete_sound`."""

    deleted: bool
    affected_buttons: tuple[Button, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Counts produced by :func:`chime_deck.store.transfer.import_config`."""

    dry_run: bool
    settings_written: int
    entities: int
    sounds: int
    buttons: int
