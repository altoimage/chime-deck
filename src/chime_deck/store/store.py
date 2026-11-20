"""Async repository API over the SQLite configuration database.

All configuration writes flow through :class:`ConfigStore`. Every successful
mutation emits a configuration event after its outermost transaction commits;
failed writes publish nothing. Raw :class:`sqlite3.IntegrityError` exceptions
are translated into typed store errors so no driver-specific exception escapes.

Owning ticket: HSD-2sem. Extended by HSD-jwd3.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Self

import aiosqlite

from .database import Database
from .errors import (
    ButtonNotFound,
    EntityIdConflict,
    EntityNotFound,
    SettingValueError,
    SlugConflict,
    SlugDerivationError,
    SoundInUseError,
    SoundNotFound,
    StoreConflict,
    UnknownSettingKey,
    ValidationError,
)
from .events import EventBus, button_event, entity_event, settings_event, sound_event
from .models import (
    UNSET,
    Button,
    ButtonIn,
    Entity,
    EntityIn,
    PressLog,
    PressLogIn,
    Sound,
    SoundDeleteResult,
    SoundIn,
    SoundUpdate,
)
from .settings_registry import (
    PROVIDER_SECRET_KEY_PREFIX,
    PROVIDER_SECRET_MAX_LENGTH,
    SECRET_SETTING_KEYS,
    SETTINGS,
    TTS_BAKE_SETTING_KEYS,
    SettingSpec,
    SettingType,
    is_secret_key,
    normalise_value,
    provider_id_from_secret_key,
    provider_secret_key,
)

__all__ = ["PRESS_LOG_RETENTION", "ConfigStore", "normalise_timestamp", "utcnow_iso"]

_ACTION_TYPES = frozenset({"sound", "tts"})
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_ENTITY_ID_UNIQUE_VIOLATION: Final = "UNIQUE constraint failed: entities.entity_id"
"""Exact sqlite prefix for the only UNIQUE constraint on ``entities``."""

PRESS_LOG_RETENTION: Final = 1000
"""Rows of press history the ``press_log`` table keeps.

Every insert trims the table back to this many newest rows, and the history
route's ``limit`` is capped at it, so the number the operator sees in the UI and
the number the database holds cannot disagree. It is one module-level constant
rather than a literal in the ``DELETE`` because both sides read it.
"""


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with microseconds."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def normalise_timestamp(value: str | None, *, field: str = "pressed_at") -> str:
    """Return ``value`` as a UTC ISO-8601 timestamp with microseconds.

    Timestamps are compared as strings for ordering and retention, so every
    stored value uses the one canonical form: UTC, microseconds and a trailing
    ``Z``. An omitted value becomes the current time, a naive value is treated
    as UTC and an offset-bearing value is converted to UTC.

    :param value: caller-supplied ISO-8601 timestamp, or ``None`` for now.
    :param field: field name quoted in the validation message.
    :raises ValidationError: if the value is not a valid ISO-8601 timestamp.
    """
    if value is None:
        return utcnow_iso()
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _require_spec(key: str) -> SettingSpec:
    """Return the registry entry for a key or raise ``UnknownSettingKey``.

    :param key: setting key to look up.
    """
    spec = SETTINGS.get(key)
    if spec is None:
        raise UnknownSettingKey(key)
    return spec


def _require_provider_id(provider_id: object) -> str:
    """Return *provider_id* when it is a provider id, or raise ``SettingValueError``.

    Provider ids are not settings keys, so this check cannot go through
    :func:`_require_spec`.

    :param provider_id: candidate provider id.
    :raises SettingValueError: if the id is not 12 lower-case hex characters.
    """
    candidate = str(provider_id)
    key = provider_secret_key(candidate)
    if provider_id_from_secret_key(key) is None:
        raise SettingValueError(key, "must be a 12-character hexadecimal provider id")
    return candidate


def derive_slug(name: str) -> str:
    """Derive a button slug from its display name.

    Lowercases the name, replaces each run of non-alphanumeric characters with
    a single ``-`` and strips leading and trailing separators. Public so a
    caller can predict exactly the slug an insert will write: one derivation
    shared, never a second copy (HSD-po1g). The store imposes no charset or
    length bound of its own — validation belongs to the writer, so a legacy row
    and a restored bundle keep working.

    :param name: button display name.
    :raises SlugDerivationError: if the name contains no alphanumeric characters.
    """
    slug = _SLUG_SEPARATORS.sub("-", name.lower()).strip("-")
    if not slug:
        raise SlugDerivationError(name)
    return slug


def derive_instance_id(name: str) -> str:
    """Derive the MQTT instance id a first device-name write pins (HSD-vwdy).

    The result is exactly :func:`derive_slug`'s output, deliberately: one
    derivation shared, never a second copy. That makes two properties true by
    construction:

    * it always matches ``MqttSettings``' instance-id grammar
      (``[A-Za-z0-9][A-Za-z0-9._-]*``, at most 64 characters), so it can be used
      in the topic tree and the client id; and
    * it never contains a dot, so Home Assistant's object-id matcher keeps the
      discovery entities visible (see ``discovery._OBJECT_ID_PATTERN``) — a
      dotted instance id still presses buttons but the entities silently
      disappear from HA.

    A name the settings registry accepted can never raise: ``device.name``
    requires at least one ASCII alphanumeric character, which is exactly what
    slugging needs.

    :param name: device name about to be stored as ``device.name``.
    :raises SlugDerivationError: if the name contains no alphanumeric characters.
    """
    return derive_slug(name)


async def _fetch_stored_setting(connection: aiosqlite.Connection, key: str) -> tuple[bool, object]:
    """Read one settings row and decode its JSON value.

    :param connection: connection to use.
    :param key: setting key to read.
    :returns: ``(found, value)`` where ``found`` distinguishes a missing row
        from a stored JSON ``null``.
    """
    async with connection.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False, None
    return True, json.loads(row["value"])


async def _next_sort_order(connection: aiosqlite.Connection, table: str) -> int:
    """Return the next sparse sort order (maximum plus ten).

    ``table`` is always an internal literal, never user input.

    :param connection: connection to use.
    :param table: one of ``entities`` or ``buttons``.
    """
    async with connection.execute(
        f"SELECT COALESCE(MAX(sort_order), 0) + 10 FROM {table}"
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 10


async def _fetch_entity(connection: aiosqlite.Connection, entity_id: int) -> Entity:
    """Read one entity row.

    :param connection: connection to use.
    :param entity_id: database id.
    :raises EntityNotFound: if the row does not exist.
    """
    async with connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise EntityNotFound(entity_id)
    return Entity.from_row(row)


async def _fetch_sound(connection: aiosqlite.Connection, sound_id: int) -> Sound:
    """Read one sound row.

    :param connection: connection to use.
    :param sound_id: database id.
    :raises SoundNotFound: if the row does not exist.
    """
    async with connection.execute("SELECT * FROM sounds WHERE id = ?", (sound_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise SoundNotFound(sound_id)
    return Sound.from_row(row)


async def _fetch_button(connection: aiosqlite.Connection, button_id: int) -> Button:
    """Read one button row.

    :param connection: connection to use.
    :param button_id: database id.
    :raises ButtonNotFound: if the row does not exist.
    """
    async with connection.execute("SELECT * FROM buttons WHERE id = ?", (button_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ButtonNotFound(button_id)
    return Button.from_row(row)


async def _fetch_buttons_by_sound(
    connection: aiosqlite.Connection, sound_id: int
) -> tuple[Button, ...]:
    """Read every button that references a sound.

    :param connection: connection to use.
    :param sound_id: database id of the referenced sound.
    """
    async with connection.execute(
        "SELECT * FROM buttons WHERE sound_id = ? ORDER BY sort_order, id", (sound_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(Button.from_row(row) for row in rows)


class ConfigStore:
    """Async repository for every configured object.

    Create it with an explicit database path, then call :meth:`connect` (or use
    it as an async context manager). Pass an :class:`EventBus` to share
    subscriptions with other components; otherwise the store creates its own
    bus and exposes it as :attr:`events`.
    """

    def __init__(self, db_path: Path | str, *, events: EventBus | None = None) -> None:
        """Create a store handle without opening the database.

        :param db_path: filesystem path of the SQLite file.
        :param events: optional shared event bus.
        """
        self._db = Database(db_path)
        self.events = events if events is not None else EventBus()
        self._db.set_event_sink(self.events.publish)
        self._logger = logging.getLogger(__name__)

    async def connect(self) -> None:
        """Open the database and apply migrations."""
        await self._db.connect()

    async def close(self) -> None:
        """Close the database connection."""
        await self._db.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        """Return the open connection for read-only queries and PRAGMA checks.

        :raises StoreNotConnected: if :meth:`connect` has not been called.
        """
        return self._db.connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a write transaction that composes the individual mutators.

        Only the outermost transaction commits and flushes events, so nesting
        several mutating methods inside this block gives all-or-nothing
        behaviour with a single event burst. That is what makes
        ``import_config`` atomic and how HSD-l22f composes writes.

        :yields: the connection bound to this transaction.
        """
        async with self._db.transaction() as connection:
            yield connection

    async def __aenter__(self) -> Self:
        """Connect and return this store."""
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the database connection on context exit."""
        await self.close()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: object = None) -> object:
        """Return the stored value of a setting, or its registry default.

        Secret values are returned here: the Home Assistant client and MQTT
        bridge need them. Only :meth:`all_settings` redacts secrets.

        :param key: setting key.
        :param default: fallback when neither the database nor the registry has
            a value; normally the registry default is used.
        :raises UnknownSettingKey: if the key is not registered.
        """
        spec = _require_spec(key)
        found, value = await _fetch_stored_setting(self._db.connection, key)
        if found:
            return value
        return spec.default if default is None else default

    async def is_setting_stored(self, key: str) -> bool:
        """Return whether *key* has a stored row of its own.

        :meth:`get_setting` cannot answer this: it substitutes the registry
        default when no row exists, so ``mqtt.instance_id`` reads as
        ``"default"`` whether it was pinned or never written. The first
        ``device.name`` write must distinguish those two cases to know whether
        it is the unit's first naming (HSD-vwdy), so this method reports row
        presence only.

        :param key: setting key.
        :raises UnknownSettingKey: if the key is not registered.
        """
        _require_spec(key)
        found, _value = await _fetch_stored_setting(self._db.connection, key)
        return found

    async def set_setting(self, key: str, value: object) -> None:
        """Normalise, validate and persist a setting, clearing affected TTS bakes.

        The value is normalised before it is stored, so ``"DEBUG"`` is written
        as ``"debug"`` whichever path wrote it.

        :param key: setting key.
        :param value: decoded Python value; must match the registered type and
            satisfy the key's constraints.
        :raises UnknownSettingKey: if the key is not registered.
        :raises SettingTypeError: if the value's Python type does not match.
        :raises SettingValueError: if the value breaks a per-key constraint.
        """
        spec = _require_spec(key)
        normalised = normalise_value(key, value)
        encoded = json.dumps(normalised)
        timestamp = utcnow_iso()
        async with self._db.transaction() as connection:
            await connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (spec.key, encoded, timestamp),
            )
            await self._clear_bakes_for_tts_change(connection, (key,))
            self._db.emit(settings_event(keys=(spec.key,)))

    async def all_settings(self, redact_secrets: bool = True) -> dict[str, object]:
        """Return registry defaults overlaid with every stored settings row.

        Unknown stored keys are included so an older or newer UI can still
        round-trip them. Every ``JSON``-typed default is copied, so a caller can
        mutate the result without touching the registry's own default.

        With ``redact_secrets=True`` the secret keys are absent from the result
        entirely, not masked: ``ha.token``, ``mqtt.password``, the two renderer
        tokens and every ``tts.providers.<id>.api_key`` row. A provider key is
        not a registered setting, so it arrives through the unknown-row overlay
        and has to be removed here too.

        :param redact_secrets: remove every key for which :func:`is_secret_key`
            is true.
        """
        result: dict[str, object] = {
            key: copy.deepcopy(spec.default) if spec.type is SettingType.JSON else spec.default
            for key, spec in SETTINGS.items()
        }
        async with self._db.connection.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            result[str(row["key"])] = json.loads(row["value"])
        if redact_secrets:
            for key in [key for key in result if is_secret_key(key)]:
                result.pop(key)
        return result

    async def all_secret_values(self) -> list[str]:
        """Return every non-empty secret value the database holds.

        This is what :class:`~chime_deck.config.SecretScrubber` registers, so
        it is also why a redaction miss on a value this process stored is
        impossible: *every* secret-bearing shape has to be listed here, the
        registered ``SECRET_SETTING_KEYS`` and the unregistered provider API key
        rows alike. A new secret-bearing key that is missing from this method is
        a redaction bug.
        """
        values: list[str] = []
        for key in sorted(SECRET_SETTING_KEYS):
            value = await self.get_setting(key)
            if isinstance(value, str) and value:
                values.append(value)
        async with self._db.connection.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (f"{PROVIDER_SECRET_KEY_PREFIX}%.api_key",),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            if provider_id_from_secret_key(str(row["key"])) is None:
                continue
            value = json.loads(row["value"])
            if isinstance(value, str) and value:
                values.append(value)
        return values

    async def secret_status(self) -> dict[str, bool]:
        """Return whether each secret setting currently has a value.

        This is the static registry map only: provider API keys are not listed
        here because their flags travel with the provider records
        (``has_api_key``), not through the settings envelope.
        """
        return {key: await self.has_secret(key) for key in sorted(SECRET_SETTING_KEYS)}

    async def has_secret(self, key: str) -> bool:
        """Return whether a setting has a non-empty stored value.

        :param key: setting key.
        :raises UnknownSettingKey: if the key is not registered.
        """
        spec = _require_spec(key)
        found, value = await _fetch_stored_setting(self._db.connection, key)
        if not found:
            value = spec.default
        return bool(value)

    # ------------------------------------------------------------------
    # Provider API keys (D25)
    # ------------------------------------------------------------------

    async def set_provider_secret(self, provider_id: str, api_key: str) -> None:
        """Write one provider's API key.

        The key is deliberately not a registered setting, so it never goes
        through :func:`_require_spec`: it is written and read only here, which
        is what keeps it out of the generic settings PATCH.

        Baked speech is *not* cleared: rotating a key changes credentials, not
        rendered bytes — the same reason ``tts.remote_tts.token`` is not in
        :data:`TTS_BAKE_SETTING_KEYS`.

        The caller is responsible for calling
        :func:`~chime_deck.config.refresh_secret_scrubber` afterwards, like
        every other writer of a secret.

        :param provider_id: 12-character hexadecimal provider id.
        :param api_key: the key itself; never echoed in an error message.
        :raises SettingValueError: if the id is malformed or the key is not a
            string or is longer than :data:`PROVIDER_SECRET_MAX_LENGTH`.
        """
        key = provider_secret_key(_require_provider_id(provider_id))
        if not isinstance(api_key, str):
            raise SettingValueError(key, "must be a string")
        if len(api_key) > PROVIDER_SECRET_MAX_LENGTH:
            raise SettingValueError(key, f"must be at most {PROVIDER_SECRET_MAX_LENGTH} characters")
        timestamp = utcnow_iso()
        async with self._db.transaction() as connection:
            await connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(api_key), timestamp),
            )
            self._db.emit(settings_event(keys=(key,)))

    async def get_provider_secret(self, provider_id: str) -> str:
        """Return one provider's stored API key, or ``""`` when it has none.

        This is the one reader of a secret value that is not itself a settings
        read; it exists so the value can never be reached through
        :meth:`get_setting`.

        :param provider_id: 12-character hexadecimal provider id.
        :raises SettingValueError: if the id is malformed.
        """
        key = provider_secret_key(_require_provider_id(provider_id))
        found, value = await _fetch_stored_setting(self._db.connection, key)
        return value if found and isinstance(value, str) else ""

    async def has_provider_secret(self, provider_id: str) -> bool:
        """Return whether one provider has a non-empty stored API key.

        :param provider_id: 12-character hexadecimal provider id.
        :raises SettingValueError: if the id is malformed.
        """
        return bool(await self.get_provider_secret(provider_id))

    async def delete_provider_secret(self, provider_id: str) -> None:
        """Delete one provider's API key; absent is not an error.

        The caller is responsible for calling
        :func:`~chime_deck.config.refresh_secret_scrubber` afterwards, like
        every other writer of a secret.

        :param provider_id: 12-character hexadecimal provider id.
        :raises SettingValueError: if the id is malformed.
        """
        key = provider_secret_key(_require_provider_id(provider_id))
        async with self._db.transaction() as connection:
            await connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._db.emit(settings_event(keys=(key,)))

    async def _clear_bakes_for_tts_change(
        self, connection: aiosqlite.Connection, changed_keys: Iterable[str]
    ) -> tuple[int, ...]:
        """Clear bakes invalidated by a TTS settings change.

        ``tts.default_engine`` invalidates every TTS button; engine-specific
        options invalidate only buttons whose effective engine matches. The
        update runs in the caller's transaction.

        :param connection: connection bound to the settings transaction.
        :param changed_keys: setting keys written by the caller.
        """
        keys = set(changed_keys)
        affected_settings = keys & set(TTS_BAKE_SETTING_KEYS)
        if not affected_settings:
            return ()

        default_changed = "tts.default_engine" in affected_settings
        affected_engines: set[str] = set()
        for key in affected_settings:
            engine = TTS_BAKE_SETTING_KEYS[key]
            if engine is not None:
                affected_engines.add(engine)

        stored_default = await self._read_setting(connection, "tts.default_engine")
        default_engine = stored_default if isinstance(stored_default, str) else None

        async with connection.execute(
            "SELECT id, tts_engine FROM buttons WHERE action_type = 'tts'"
        ) as cursor:
            rows = await cursor.fetchall()

        button_ids: list[int] = []
        for row in rows:
            effective_engine = row["tts_engine"] or default_engine
            if default_changed or effective_engine in affected_engines:
                button_ids.append(int(row["id"]))
        if not button_ids:
            return ()

        await self._clear_bakes(connection, button_ids)
        self._db.emit(button_event(ids=button_ids))
        return tuple(button_ids)

    async def _read_setting(self, connection: aiosqlite.Connection, key: str) -> object:
        """Read a setting from the given connection, falling back to the registry.

        :param connection: connection to use.
        :param key: registered setting key.
        """
        found, value = await _fetch_stored_setting(connection, key)
        if found:
            return value
        return _require_spec(key).default

    async def _clear_bakes(
        self, connection: aiosqlite.Connection, button_ids: Sequence[int]
    ) -> None:
        """Null the bake state of the given buttons.

        :param connection: connection bound to the caller's transaction.
        :param button_ids: ids whose bake columns must be cleared.
        """
        placeholders = ", ".join("?" for _ in button_ids)
        await connection.execute(
            "UPDATE buttons SET baked_sha = NULL, baked_at = NULL, baked_error = NULL, "
            "baked_format = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(button_ids),
        )

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    async def list_entities(self) -> list[Entity]:
        """Return every entity ordered by ``sort_order`` then id."""
        async with self._db.connection.execute(
            "SELECT * FROM entities ORDER BY sort_order, id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [Entity.from_row(row) for row in rows]

    async def get_entity_by_id(self, entity_id: int) -> Entity | None:
        """Return an entity by id, or ``None`` when it does not exist.

        The UI addresses entities by id: ``PATCH``/``DELETE /api/entities/{id}``
        (design 05 §3) need this lookup, and the route has to tell a missing id
        apart from a duplicate ``entity_id``, so it sits beside
        :meth:`list_entities` rather than being derived from it.

        :param entity_id: primary key of the entity.
        """
        async with self._db.connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else Entity.from_row(row)

    async def upsert_entity(self, entity: EntityIn) -> Entity:
        """Insert a new entity or update an existing one.

        :param entity: write input; ``id`` is ``None`` for an insert.
        :raises EntityNotFound: if updating an id that does not exist.
        :raises EntityIdConflict: if ``entity_id`` duplicates another row.
        """
        timestamp = utcnow_iso()
        attributes = (
            None if entity.show_attributes is None else json.dumps(list(entity.show_attributes))
        )
        # Explicit overrides only, keys sorted so the stored text is stable.
        # ``None`` stays SQL NULL: the code default table applies, which is not
        # the same as an empty object (a mapping the user cleared).
        state_tones = (
            None
            if entity.state_tones is None
            else json.dumps(dict(sorted(entity.state_tones.items())))
        )
        async with self._db.transaction() as connection:
            try:
                if entity.id is None:
                    if entity.sort_order is None:
                        sort_order = await _next_sort_order(connection, "entities")
                    else:
                        sort_order = entity.sort_order
                    async with connection.execute(
                        "INSERT INTO entities (entity_id, display_name, icon, accent_color, "
                        "show_attributes, state_tones, hide_when_unavailable, "
                        "colour_by_state, sort_order, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entity.entity_id,
                            entity.display_name,
                            entity.icon,
                            entity.accent_color,
                            attributes,
                            state_tones,
                            int(entity.hide_when_unavailable),
                            int(entity.colour_by_state),
                            sort_order,
                            timestamp,
                        ),
                    ) as cursor:
                        inserted_id = cursor.lastrowid
                    if inserted_id is None:
                        raise StoreConflict("insert did not return an entity id")
                    saved = await _fetch_entity(connection, int(inserted_id))
                else:
                    async with connection.execute(
                        "UPDATE entities SET entity_id = ?, display_name = ?, icon = ?, "
                        "accent_color = ?, show_attributes = ?, state_tones = ?, "
                        "hide_when_unavailable = ?, colour_by_state = ?, "
                        "sort_order = COALESCE(?, sort_order) WHERE id = ?",
                        (
                            entity.entity_id,
                            entity.display_name,
                            entity.icon,
                            entity.accent_color,
                            attributes,
                            state_tones,
                            int(entity.hide_when_unavailable),
                            int(entity.colour_by_state),
                            entity.sort_order,
                            entity.id,
                        ),
                    ) as cursor:
                        updated_rows = cursor.rowcount
                    if updated_rows == 0:
                        raise EntityNotFound(entity.id)
                    saved = await _fetch_entity(connection, entity.id)
            except sqlite3.IntegrityError as exc:
                # ``entities.entity_id`` is the table's only UNIQUE constraint.
                # Match the exact prefix so a NOT NULL failure on the same
                # column lands on the generic branch instead of claiming a
                # duplicate, and never reuse sqlite's text as the message.
                if str(exc).startswith(_ENTITY_ID_UNIQUE_VIOLATION):
                    raise EntityIdConflict(entity.entity_id, creating=entity.id is None) from exc
                raise StoreConflict("the entity row violates a database constraint") from exc

            self._db.emit(entity_event(ids=(saved.id,)))
        return saved

    async def reorder_entities(self, ids: Sequence[int]) -> None:
        """Renumber entities with sparse ``sort_order`` values.

        The ids in ``ids`` receive 10, 20, 30, … in the given order; every
        remaining entity is appended in its existing ``(sort_order, id)``
        order with continued numbering, so no two rows collide.

        :param ids: entity ids in their new order.
        :raises EntityNotFound: if any id does not exist.
        """
        # mirrors reorder_buttons — keep the two in sync
        if not ids:
            return

        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT id FROM entities ORDER BY sort_order, id"
            ) as cursor:
                rows = await cursor.fetchall()
            existing_order = [int(row["id"]) for row in rows]
            existing_ids = set(existing_order)
            for entity_id in ids:
                if entity_id not in existing_ids:
                    raise EntityNotFound(entity_id)

            requested: list[int] = []
            for entity_id in ids:
                if entity_id not in requested:
                    requested.append(entity_id)
            remaining = [entity_id for entity_id in existing_order if entity_id not in requested]
            touched = (*requested, *remaining)
            for position, entity_id in enumerate(touched, start=1):
                await connection.execute(
                    "UPDATE entities SET sort_order = ? WHERE id = ?",
                    (position * 10, entity_id),
                )
            self._db.emit(entity_event(ids=touched))

    async def reorder_buttons(self, ids: Sequence[int]) -> None:
        """Renumber buttons with sparse ``sort_order`` values.

        The ids in ``ids`` receive 10, 20, 30, … in the given order; every
        remaining button is appended in its existing ``(sort_order, id)``
        order with continued numbering, so no two rows collide.

        :param ids: button ids in their new order.
        :raises ButtonNotFound: if any id does not exist.
        """
        # mirrors reorder_entities — keep the two in sync
        if not ids:
            return

        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT id FROM buttons ORDER BY sort_order, id"
            ) as cursor:
                rows = await cursor.fetchall()
            existing_order = [int(row["id"]) for row in rows]
            existing_ids = set(existing_order)
            for button_id in ids:
                if button_id not in existing_ids:
                    raise ButtonNotFound(button_id)

            requested: list[int] = []
            for button_id in ids:
                if button_id not in requested:
                    requested.append(button_id)
            remaining = [button_id for button_id in existing_order if button_id not in requested]
            touched = (*requested, *remaining)
            for position, button_id in enumerate(touched, start=1):
                await connection.execute(
                    "UPDATE buttons SET sort_order = ? WHERE id = ?",
                    (position * 10, button_id),
                )
            self._db.emit(button_event(ids=touched))

    async def delete_entity(self, entity_id: int) -> None:
        """Delete an entity.

        :param entity_id: database id.
        :raises EntityNotFound: if the row does not exist.
        """
        async with self._db.transaction() as connection:
            async with connection.execute(
                "DELETE FROM entities WHERE id = ?", (entity_id,)
            ) as cursor:
                deleted = cursor.rowcount
            if deleted == 0:
                raise EntityNotFound(entity_id)
            self._db.emit(entity_event(removed_ids=(entity_id,)))

    # ------------------------------------------------------------------
    # Sounds
    # ------------------------------------------------------------------

    async def list_sounds(self) -> list[Sound]:
        """Return every indexed sound ordered by id."""
        async with self._db.connection.execute("SELECT * FROM sounds ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [Sound.from_row(row) for row in rows]

    async def get_sound(self, sound_id: int) -> Sound:
        """Return one sound.

        :param sound_id: database id.
        :raises SoundNotFound: if the row does not exist.
        """
        return await _fetch_sound(self._db.connection, sound_id)

    async def create_sound(self, sound: SoundIn) -> Sound:
        """Insert a sound.

        :param sound: write input.
        :raises StoreConflict: if a file sound already uses this path.
        """
        timestamp = utcnow_iso()
        async with self._db.transaction() as connection:
            try:
                async with connection.execute(
                    "INSERT INTO sounds (name, source_type, path, url, sha256, format, "
                    "duration_ms, bytes, missing, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sound.name,
                        sound.source_type,
                        sound.path,
                        sound.url,
                        sound.sha256,
                        sound.format,
                        sound.duration_ms,
                        sound.bytes,
                        int(sound.missing),
                        timestamp,
                    ),
                ) as cursor:
                    inserted_id = cursor.lastrowid
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(str(exc)) from exc
            if inserted_id is None:
                raise StoreConflict("insert did not return a sound id")
            saved = await _fetch_sound(connection, int(inserted_id))
            self._db.emit(sound_event(ids=(saved.id,)))
        return saved

    async def update_sound(self, sound_id: int, patch: SoundUpdate) -> Sound:
        """Apply an all-optional patch to a sound.

        :param sound_id: database id.
        :param patch: fields to change; :data:`UNSET` fields are untouched.
        :raises SoundNotFound: if the row does not exist.
        :raises StoreConflict: if a new path duplicates another file sound.
        """
        updates: dict[str, object] = {}
        if patch.name is not UNSET:
            updates["name"] = patch.name
        if patch.source_type is not UNSET:
            updates["source_type"] = patch.source_type
        if patch.path is not UNSET:
            updates["path"] = patch.path
        if patch.url is not UNSET:
            updates["url"] = patch.url
        if patch.sha256 is not UNSET:
            updates["sha256"] = patch.sha256
        if patch.format is not UNSET:
            updates["format"] = patch.format
        if patch.duration_ms is not UNSET:
            updates["duration_ms"] = patch.duration_ms
        if patch.bytes is not UNSET:
            updates["bytes"] = patch.bytes
        if patch.missing is not UNSET:
            updates["missing"] = int(bool(patch.missing))

        async with self._db.transaction() as connection:
            if not updates:
                return await _fetch_sound(connection, sound_id)

            assignments = ", ".join(f"{column} = ?" for column in updates)
            try:
                async with connection.execute(
                    f"UPDATE sounds SET {assignments} WHERE id = ?",
                    (*updates.values(), sound_id),
                ) as cursor:
                    updated_rows = cursor.rowcount
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(str(exc)) from exc
            if updated_rows == 0:
                raise SoundNotFound(sound_id)
            saved = await _fetch_sound(connection, sound_id)
            self._db.emit(sound_event(ids=(saved.id,)))
        return saved

    async def buttons_using_sound(self, sound_id: int) -> list[Button]:
        """Return every button that references a sound.

        :param sound_id: database id of the sound.
        """
        return list(await _fetch_buttons_by_sound(self._db.connection, sound_id))

    async def delete_sound(self, sound_id: int, *, force: bool = False) -> SoundDeleteResult:
        """Delete a sound and optionally the buttons that reference it.

        Without ``force`` and with referencing buttons, :class:`SoundInUseError`
        is raised and nothing changes. With ``force`` the affected buttons and
        the sound are deleted in one transaction, and snapshots of those
        buttons are returned so the UI can explain what happened.

        :param sound_id: database id.
        :param force: delete referencing buttons instead of failing.
        :raises SoundNotFound: if the row does not exist.
        :raises SoundInUseError: if buttons still reference the sound and
            ``force`` is false.
        """
        async with self._db.transaction() as connection:
            await _fetch_sound(connection, sound_id)
            affected = await _fetch_buttons_by_sound(connection, sound_id)
            if affected and not force:
                raise SoundInUseError(affected)

            # Design 01 §3 rule 3 describes disabling the referencing buttons
            # before deleting the sound. That is impossible here: the
            # `CHECK (action_type <> 'sound' OR sound_id IS NOT NULL)` fails
            # when ON DELETE SET NULL nulls a surviving sound button's
            # sound_id. Deleting those buttons is the only conformant way to
            # delete a referenced sound in a single transaction. The affected
            # set is re-queried above so a concurrent edit cannot slip past it.
            if affected:
                placeholders = ", ".join("?" for _ in affected)
                await connection.execute(
                    f"DELETE FROM buttons WHERE id IN ({placeholders})",
                    tuple(button.id for button in affected),
                )
            await connection.execute("DELETE FROM sounds WHERE id = ?", (sound_id,))

            if affected:
                self._db.emit(
                    button_event(
                        removed_ids=tuple(button.id for button in affected),
                        removed_slugs=tuple(button.slug for button in affected),
                    )
                )
            self._db.emit(sound_event(removed_ids=(sound_id,)))
        return SoundDeleteResult(deleted=True, affected_buttons=affected)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    async def list_buttons(self) -> list[Button]:
        """Return every button ordered by ``sort_order`` then id."""
        async with self._db.connection.execute(
            "SELECT * FROM buttons ORDER BY sort_order, id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [Button.from_row(row) for row in rows]

    async def get_button_by_slug(self, slug: str) -> Button | None:
        """Return a button by slug, or ``None`` when it does not exist.

        :param slug: MQTT-safe button slug.
        """
        async with self._db.connection.execute(
            "SELECT * FROM buttons WHERE slug = ?", (slug,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else Button.from_row(row)

    async def get_button_by_id(self, button_id: int) -> Button | None:
        """Return a button by id, or ``None`` when it does not exist.

        The UI addresses buttons by id: its press action is
        ``POST /api/buttons/{id}/press`` (design 05), and later id-keyed routes
        need the same lookup, so it sits beside the slug lookup rather than being
        derived by scanning :meth:`list_buttons`.

        :param button_id: primary key of the button.
        """
        async with self._db.connection.execute(
            "SELECT * FROM buttons WHERE id = ?", (button_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else Button.from_row(row)

    async def upsert_button(self, button: ButtonIn) -> Button:
        """Insert a new button or fully update an existing one.

        On insert the slug is derived from ``name`` when not supplied. On update
        the stored slug is preserved unless an explicit different slug is
        supplied; a rename is reported in the event's ``renamed`` pairs because
        it changes the MQTT unique id (02-mqtt-buttons §5), so HSD-c3si must
        remove the old discovery topic.

        Changing ``tts_text`` or ``tts_engine`` clears the button's bake state in
        the same transaction.

        :param button: write input; ``id`` is ``None`` for an insert.
        :raises ValidationError: if the sound/TTS/volume rules fail.
        :raises SlugDerivationError: if no slug can be derived from the name.
        :raises SlugConflict: if the slug belongs to a different button.
        :raises ButtonNotFound: if updating an id that does not exist.
        """
        _validate_button(button)
        timestamp = utcnow_iso()

        async with self._db.transaction() as connection:
            await self._ensure_sound_exists(connection, button.sound_id)

            if button.id is None:
                slug = button.slug if button.slug is not None else derive_slug(button.name)
                await _check_slug_conflict(connection, slug, exclude_id=None)
                if button.sort_order is None:
                    sort_order = await _next_sort_order(connection, "buttons")
                else:
                    sort_order = button.sort_order
                try:
                    async with connection.execute(
                        "INSERT INTO buttons (slug, name, icon, action_type, sound_id, "
                        "tts_text, tts_engine, volume_override, dedupe_seconds, "
                        "show_on_dashboard, quiet_hours_exempt, enabled, sort_order, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            slug,
                            button.name,
                            button.icon,
                            button.action_type,
                            button.sound_id,
                            button.tts_text,
                            button.tts_engine,
                            button.volume_override,
                            button.dedupe_seconds,
                            int(button.show_on_dashboard),
                            int(button.quiet_hours_exempt),
                            int(button.enabled),
                            sort_order,
                            timestamp,
                            timestamp,
                        ),
                    ) as cursor:
                        inserted_id = cursor.lastrowid
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(str(exc)) from exc
                if inserted_id is None:
                    raise StoreConflict("insert did not return a button id")
                saved = await _fetch_button(connection, int(inserted_id))
                self._db.emit(button_event(ids=(saved.id,), slugs=(saved.slug,)))
                return saved

            existing = await _fetch_button(connection, button.id)
            new_slug = existing.slug
            if button.slug is not None and button.slug != existing.slug:
                new_slug = button.slug
            if new_slug != existing.slug:
                await _check_slug_conflict(connection, new_slug, exclude_id=existing.id)
            renamed = ((existing.slug, new_slug),) if new_slug != existing.slug else ()

            clear_bake = (
                button.tts_text != existing.tts_text or button.tts_engine != existing.tts_engine
            )
            try:
                async with connection.execute(
                    "UPDATE buttons SET slug = ?, name = ?, icon = ?, action_type = ?, "
                    "sound_id = ?, tts_text = ?, tts_engine = ?, "
                    "volume_override = ?, dedupe_seconds = ?, show_on_dashboard = ?, "
                    "quiet_hours_exempt = ?, enabled = ?, "
                    "sort_order = COALESCE(?, sort_order), updated_at = ? "
                    "WHERE id = ?",
                    (
                        new_slug,
                        button.name,
                        button.icon,
                        button.action_type,
                        button.sound_id,
                        button.tts_text,
                        button.tts_engine,
                        button.volume_override,
                        button.dedupe_seconds,
                        int(button.show_on_dashboard),
                        int(button.quiet_hours_exempt),
                        int(button.enabled),
                        button.sort_order,
                        timestamp,
                        existing.id,
                    ),
                ) as cursor:
                    updated_rows = cursor.rowcount
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(str(exc)) from exc
            if updated_rows == 0:
                raise ButtonNotFound(existing.id)
            if clear_bake:
                await self._clear_bakes(connection, (existing.id,))

            saved = await _fetch_button(connection, existing.id)
            self._db.emit(button_event(ids=(saved.id,), slugs=(saved.slug,), renamed=renamed))
            return saved

    async def delete_button(self, button_id: int) -> None:
        """Delete a button.

        Bake directories are not touched here; the press path and startup sweep
        handle them.

        :param button_id: database id.
        :raises ButtonNotFound: if the row does not exist.
        """
        async with self._db.transaction() as connection:
            existing = await _fetch_button(connection, button_id)
            await connection.execute("DELETE FROM buttons WHERE id = ?", (button_id,))
            self._db.emit(button_event(removed_ids=(existing.id,), removed_slugs=(existing.slug,)))

    async def set_bake_state(
        self,
        button_id: int,
        sha: str,
        baked_at: str | None = None,
        *,
        fmt: str | None = None,
    ) -> Button:
        """Record a successful bake and clear the previous error.

        ``fmt`` is written verbatim, including ``None``: the caller has just
        sniffed the container of the artefact it wrote, so it always knows the
        answer, and a stale format left behind by an earlier bake is worse than
        no format at all. COALESCE would keep exactly that stale value.

        :param button_id: database id.
        :param sha: digest of the exact text, engine and options that were baked.
        :param baked_at: bake timestamp; defaults to now.
        :param fmt: real container of the artefact, for example ``"mp3"``.
        :raises ButtonNotFound: if the row does not exist.
        """
        timestamp = baked_at if baked_at is not None else utcnow_iso()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "UPDATE buttons SET baked_sha = ?, baked_format = ?, baked_at = ?, "
                "baked_error = NULL, updated_at = ? WHERE id = ?",
                (sha, fmt, timestamp, utcnow_iso(), button_id),
            ) as cursor:
                updated_rows = cursor.rowcount
            if updated_rows == 0:
                raise ButtonNotFound(button_id)
            saved = await _fetch_button(connection, button_id)
            self._db.emit(button_event(ids=(saved.id,), slugs=(saved.slug,)))
        return saved

    async def record_bake_failure(self, button_id: int, error: str) -> Button:
        """Record a bake failure, keeping the button usable.

        :param button_id: database id.
        :param error: failure message shown in the UI.
        :raises ButtonNotFound: if the row does not exist.
        """
        async with self._db.transaction() as connection:
            async with connection.execute(
                "UPDATE buttons SET baked_error = ?, baked_sha = NULL, baked_at = NULL, "
                "baked_format = NULL, updated_at = ? WHERE id = ?",
                (error, utcnow_iso(), button_id),
            ) as cursor:
                updated_rows = cursor.rowcount
            if updated_rows == 0:
                raise ButtonNotFound(button_id)
            saved = await _fetch_button(connection, button_id)
            self._db.emit(button_event(ids=(saved.id,), slugs=(saved.slug,)))
        return saved

    async def clear_bake(self, button_id: int, error: str | None = None) -> Button:
        """Clear a button's bake state.

        :param button_id: database id.
        :param error: optional error to record while clearing.
        :raises ButtonNotFound: if the row does not exist.
        """
        async with self._db.transaction() as connection:
            async with connection.execute(
                "UPDATE buttons SET baked_sha = NULL, baked_at = NULL, "
                "baked_format = NULL, baked_error = ?, "
                "updated_at = ? WHERE id = ?",
                (error, utcnow_iso(), button_id),
            ) as cursor:
                updated_rows = cursor.rowcount
            if updated_rows == 0:
                raise ButtonNotFound(button_id)
            saved = await _fetch_button(connection, button_id)
            self._db.emit(button_event(ids=(saved.id,), slugs=(saved.slug,)))
        return saved

    async def _ensure_sound_exists(
        self, connection: aiosqlite.Connection, sound_id: int | None
    ) -> None:
        """Check that a sound reference points at an existing row.

        :param connection: connection bound to the button transaction.
        :param sound_id: referenced sound id, or ``None`` for TTS buttons.
        :raises ValidationError: if the sound does not exist.
        """
        if sound_id is None:
            return
        async with connection.execute("SELECT 1 FROM sounds WHERE id = ?", (sound_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValidationError(f"sound_id {sound_id} does not exist")

    # ------------------------------------------------------------------
    # Press log
    # ------------------------------------------------------------------

    async def log_press(self, entry: PressLogIn) -> None:
        """Insert a press log entry and trim the table to the newest retention rows.

        ``pressed_at`` is normalised to the canonical UTC form before it is
        stored, because ordering and retention compare the column as a string.

        :param entry: log entry; ``pressed_at`` defaults to now.
        :raises ValidationError: if ``pressed_at`` is not ISO-8601.
        """
        pressed_at = normalise_timestamp(entry.pressed_at)
        async with self._db.transaction() as connection:
            try:
                await connection.execute(
                    "INSERT INTO press_log (button_id, button_name, pressed_at, source, "
                    "outcome, detail, sound_name, duration_ms, job_id, synthesis_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.button_id,
                        entry.button_name,
                        pressed_at,
                        entry.source,
                        entry.outcome,
                        entry.detail,
                        entry.sound_name,
                        entry.duration_ms,
                        entry.job_id,
                        entry.synthesis_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(str(exc)) from exc
            await connection.execute(
                "DELETE FROM press_log WHERE id NOT IN ("
                "SELECT id FROM press_log ORDER BY pressed_at DESC, id DESC LIMIT ?)",
                (PRESS_LOG_RETENTION,),
            )

    async def recent_presses(
        self, limit: int = 100, *, outcome: str | None = None
    ) -> list[PressLog]:
        """Return the newest press log entries first.

        Ordering is ``pressed_at DESC, id DESC``: the id breaks a tie between two
        rows written inside the same microsecond, so the ordering is total and a
        page never repeats or skips a row.

        ``outcome`` filters in SQL rather than after the fact, so ``limit``
        applies to the matching rows: asking for the single newest failure
        returns the newest failure, not an empty page because the newest rows
        happened to be something else.

        :param limit: maximum number of rows. Not range-checked here — the range
            belongs to the HTTP layer that promises it.
        :param outcome: only return rows with this outcome, or ``None`` for all.
        """
        sql = "SELECT * FROM press_log"
        params: list[object] = []
        if outcome is not None:
            sql += " WHERE outcome = ?"
            params.append(outcome)
        sql += " ORDER BY pressed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self._db.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [PressLog.from_row(row) for row in rows]

    async def record_playback(
        self,
        *,
        job_id: str,
        outcome: str,
        detail: str | None = None,
        duration_ms: int | None = None,
        latency_ms: int | None = None,
        note: str | None = None,
    ) -> bool:
        """Revise the ``queued`` press log row of *job_id* with the playback result.

        This never inserts a row: "exactly one row per press" is structural in
        :mod:`chime_deck.press`, so the playback worker (HSD-kfnd) only
        revises the row that press already wrote. It only touches a row still
        marked ``queued``, so an outcome recorded earlier (a failure, for
        instance) can never be overwritten. No :class:`ConfigEvent` is emitted:
        the press log is deliberately excluded from the config event bus.

        The nullable columns are written with ``COALESCE``: a successful
        playback must not wipe the ``detail`` the press path already wrote (the
        TTS engine used, the quiet-hours volume note), and a failure must not
        have to restate the numbers it does not have.

        ``synthesis_ms`` is deliberately absent: the playback worker has no
        synthesis to report, and the press path already wrote that column.

        :param job_id: id of the job the press path dispatched.
        :param outcome: final outcome, normally ``played`` or ``failed``.
        :param detail: replacement detail; ``None`` keeps the stored one, and a
            non-``None`` value still replaces the whole string.
        :param duration_ms: how long the sound played, when it finished.
        :param latency_ms: enqueue-to-first-sound delay, when playback started.
        :param note: playback fact appended to whatever the press path already
            wrote, separated by ``'; '``; ``None`` appends nothing. Appending,
            not replacing, is what keeps the press-time detail (the TTS engine,
            the quiet-hours note) visible next to it.
        :returns: ``True`` when a row still marked ``queued`` was revised.
        """
        async with (
            self._db.transaction() as connection,
            connection.execute(
                "UPDATE press_log SET "
                "outcome = ?, "
                "duration_ms = COALESCE(?, duration_ms), "
                "latency_ms = COALESCE(?, latency_ms), "
                "detail = COALESCE(?, "
                "CASE WHEN detail IS NULL OR detail = '' THEN ? ELSE detail || '; ' || ? END, "
                "detail) "
                "WHERE job_id = ? AND outcome = 'queued'",
                (outcome, duration_ms, latency_ms, detail, note, note, job_id),
            ) as cursor,
        ):
            updated_rows = cursor.rowcount
        return updated_rows == 1


def _validate_button(button: ButtonIn) -> None:
    """Validate the business rules shared by button inserts and updates.

    :param button: write input.
    :raises ValidationError: if the action, text or volume is invalid.
    """
    if button.action_type not in _ACTION_TYPES:
        raise ValidationError("action_type must be 'sound' or 'tts'")
    if button.action_type == "sound":
        if button.sound_id is None:
            raise ValidationError("sound buttons require a sound_id")
    elif button.tts_text is None or not button.tts_text.strip():
        raise ValidationError("tts buttons require non-empty tts_text")
    if button.volume_override is not None and not 0 <= button.volume_override <= 100:
        raise ValidationError("volume_override must be between 0 and 100")


async def _check_slug_conflict(
    connection: aiosqlite.Connection, slug: str, *, exclude_id: int | None
) -> None:
    """Raise if a different button already uses this slug.

    :param connection: connection bound to the caller's transaction.
    :param slug: slug about to be written.
    :param exclude_id: button id being updated, or ``None`` on insert.
    :raises SlugConflict: if another row owns the slug.
    """
    if exclude_id is None:
        query = "SELECT 1 FROM buttons WHERE slug = ?"
        parameters: tuple[object, ...] = (slug,)
    else:
        query = "SELECT 1 FROM buttons WHERE slug = ? AND id <> ?"
        parameters = (slug, exclude_id)
    async with connection.execute(query, parameters) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        raise SlugConflict(slug)
