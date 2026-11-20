"""Typed errors raised by the configuration store.

Callers of :mod:`chime_deck.store` never see a raw :mod:`sqlite3` error.
Every failure mode that the public API can produce is represented by one of
these classes, so the HTTP layer (HSD-91z1) can map errors to status codes
without inspecting driver-specific exception text.

Owning ticket: HSD-2sem. Extended by HSD-jwd3.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Button

__all__ = [
    "ButtonNotFound",
    "ConfigStoreError",
    "EntityIdConflict",
    "EntityNotFound",
    "ImportValidationError",
    "MigrationError",
    "NotFoundError",
    "SchemaTooNewError",
    "SettingTypeError",
    "SettingValueError",
    "SlugConflict",
    "SlugDerivationError",
    "SoundInUseError",
    "SoundNotFound",
    "StoreConflict",
    "StoreNotConnected",
    "UnknownSettingKey",
    "ValidationError",
    "WriteError",
]


class ConfigStoreError(Exception):
    """Base class for every error raised by the configuration store."""


class StoreNotConnected(ConfigStoreError):
    """Raised when the store is used before ``connect`` or after ``close``."""


class MigrationError(ConfigStoreError):
    """Raised when SQL migration files are malformed or cannot be applied."""


class SchemaTooNewError(MigrationError):
    """Raised when the database was written by a newer build than this one.

    Applying older migrations to a newer schema would corrupt data, and there is
    no downgrade path, so the store refuses to open. The message names both
    versions and the two ways out: upgrade the build, or restore a backup.
    """

    def __init__(self, database_version: int, supported_version: int) -> None:
        """Record the two schema versions that cannot be reconciled.

        :param database_version: ``PRAGMA user_version`` of the database file.
        :param supported_version: newest migration this build ships.
        """
        self.database_version = database_version
        self.supported_version = supported_version
        super().__init__(
            f"database schema version {database_version} is newer than the newest version "
            f"this build supports ({supported_version}): refusing to start. Upgrade ChimeDeck "
            "(git pull && uv sync) or restore a backup taken with this version; "
            "downgrades are not supported."
        )


class WriteError(ConfigStoreError):
    """Raised when a write transaction cannot be committed.

    The transaction is rolled back before this is raised, so the store remains
    usable and no event is published.
    """


class NotFoundError(ConfigStoreError):
    """Base class for errors reporting a missing row."""


class EntityNotFound(NotFoundError):
    """Raised when an entity row does not exist."""

    def __init__(self, entity: int) -> None:
        """Record the requested entity id.

        :param entity: database id that could not be found.
        """
        self.entity = entity
        super().__init__(f"entity {entity} not found")


class ButtonNotFound(NotFoundError):
    """Raised when a button row does not exist."""

    def __init__(self, button: int | str) -> None:
        """Record the requested button id or slug.

        :param button: database id or slug that could not be found.
        """
        self.button = button
        super().__init__(f"button {button!r} not found")


class SoundNotFound(NotFoundError):
    """Raised when a sound row does not exist."""

    def __init__(self, sound: int) -> None:
        """Record the requested sound id.

        :param sound: database id that could not be found.
        """
        self.sound = sound
        super().__init__(f"sound {sound} not found")


class ValidationError(ConfigStoreError):
    """Raised when a write would violate a business rule."""


class UnknownSettingKey(ValidationError):
    """Raised when a setting key is not present in the settings registry."""

    def __init__(self, key: str) -> None:
        """Record the unknown key.

        :param key: setting key that is not registered.
        """
        self.key = key
        super().__init__(f"unknown setting key {key!r}")


class SettingTypeError(ValidationError):
    """Raised when a setting value does not match its registered type."""

    def __init__(self, key: str, expected: str) -> None:
        """Record the key and the type it expects.

        :param key: setting key whose value was invalid.
        :param expected: human readable name of the expected type.
        """
        self.key = key
        self.expected = expected
        super().__init__(f"setting {key!r} expects {expected}")


class SettingValueError(ValidationError):
    """Raised when a setting value has the right type but is not allowed."""

    def __init__(self, key: str, reason: str) -> None:
        """Record the key and why the value was rejected.

        :param key: setting key whose value was invalid.
        :param reason: human readable rule that the value broke. Must never contain the value
            itself for a secret key.
        """
        self.key = key
        self.reason = reason
        super().__init__(f"setting {key!r} {reason}")


class SlugDerivationError(ValidationError):
    """Raised when a slug cannot be derived from a button name."""

    def __init__(self, name: str) -> None:
        """Record the name that produced no slug characters.

        :param name: button name that yielded an empty slug.
        """
        self.name = name
        super().__init__(f"cannot derive a slug from name {name!r}")


class StoreConflict(ConfigStoreError):
    """Raised when a write would violate a database uniqueness constraint."""


class SlugConflict(StoreConflict):
    """Raised when a button slug is already used by a different row."""

    def __init__(self, slug: str) -> None:
        """Record the conflicting slug.

        :param slug: slug that is already in use.
        """
        self.slug = slug
        super().__init__(f"button slug {slug!r} is already in use")


class EntityIdConflict(StoreConflict):
    """Raised when an ``entity_id`` is already used by a different entity row.

    ``creating`` separates the two arms that can produce it: a POST that adds a
    row Home Assistant already has configured, and a PATCH that renames a row
    onto another row's id. The message is written here, never taken from
    sqlite, so raw constraint text cannot reach an API response.
    """

    def __init__(self, entity_id: str, *, creating: bool) -> None:
        """Record the conflicting id and which write arm produced it.

        :param entity_id: entity id that is already in use.
        :param creating: ``True`` for an insert, ``False`` for an update.
        """
        self.entity_id = entity_id
        self.creating = creating
        sentence = (
            f"entity {entity_id!r} is already configured"
            if creating
            else f"entity {entity_id!r} is already used by another row"
        )
        super().__init__(sentence)


class SoundInUseError(ConfigStoreError):
    """Raised when deleting a sound that buttons still reference.

    The UI lists :attr:`affected_buttons` before asking the user to confirm a
    forced delete.
    """

    def __init__(self, affected_buttons: Iterable[Button]) -> None:
        """Record the buttons that reference the sound.

        :param affected_buttons: buttons whose ``sound_id`` points at the sound.
        """
        self.affected_buttons: tuple[Button, ...] = tuple(affected_buttons)
        ids = ", ".join(str(button.id) for button in self.affected_buttons)
        super().__init__(f"sound is used by buttons: {ids}")


class ImportValidationError(ConfigStoreError):
    """Raised when an import document fails validation.

    Every problem found is collected before the exception is raised so the UI
    can show the complete list in one pass.
    """

    def __init__(self, problems: Iterable[str]) -> None:
        """Record all validation problems.

        :param problems: human readable descriptions of each problem.
        """
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("; ".join(self.problems))
