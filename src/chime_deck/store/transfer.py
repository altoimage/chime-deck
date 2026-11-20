"""JSON export and import of the whole configuration.

Exports contain no database ids: buttons reference sounds by natural key (the
sound's ``path`` for file sounds, its ``url`` for URL sounds), entities by
``entity_id`` and buttons by ``slug``. Timestamps and bake state are excluded,
so ``export(import(export(x))) == export(x)``.

Settings rows whose key is unknown to this build are local-only: they stay in
the database of the instance that stored them but are neither exported nor
accepted on import.

Import validates the entire document first, accumulating every problem into a
single :class:`ImportValidationError`, then applies everything inside one
transaction using the store's own mutators. Semantics are merge/restore rather
than replace: rows absent from the document are left untouched, which keeps
ids, ``data/tts/baked/<button_id>/`` directories and ``press_log.button_id``
references alive.

Owning ticket: HSD-2sem.

Exports made before the *Super Doorbell* to *ChimeDeck* rename carry the old
``ha-super-doorbell/config`` tag and are still accepted on import (see
:data:`LEGACY_EXPORT_FORMAT`): a backup taken yesterday must not become
unrestorable today. Exports always write the current tag.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, cast

from ..icons.tones import TONES, is_tone
from .errors import ConfigStoreError, ImportValidationError
from .models import (
    Button,
    ButtonIn,
    Entity,
    EntityIn,
    ImportReport,
    Sound,
    SoundIn,
    SoundUpdate,
)
from .settings_registry import (
    LOCAL_ONLY_SETTING_KEYS,
    PROVIDER_SECRET_MAX_LENGTH,
    SETTINGS,
    normalise_value,
    provider_id_from_secret_key,
)
from .store import ConfigStore, utcnow_iso

__all__ = ["export_config", "import_config"]

EXPORT_FORMAT = "chime-deck/config"
"""Format tag this build writes into every export."""

LEGACY_EXPORT_FORMAT: Final = "ha-super-doorbell/config"
"""Format tag written before the rename, still accepted on import.

This literal, :mod:`chime_deck.env` and :mod:`chime_deck.mqtt.legacy` are the
only places in the package that spell the pre-rename name. An export taken
before the rename must keep importing, or "restore my backup" becomes a hard
failure for exactly the user who needs it most; exports always emit
:data:`EXPORT_FORMAT`, so the tag retires itself once no old file is left to
restore.
"""

ACCEPTED_EXPORT_FORMATS: Final = (EXPORT_FORMAT, LEGACY_EXPORT_FORMAT)
"""Format tags :func:`import_config` accepts, newest first."""

EXPORT_VERSION = 2
"""Version of the document this build writes.

2 because the document now carries per-tile presentation fields
(``state_tones`` and ``colour_by_state``). An older binary must refuse the file
loudly rather than silently drop a user's tone mapping, so the version is bumped
instead of treating the extra keys as unknown ones.
"""

SUPPORTED_IMPORT_VERSIONS: Final = (1, 2)
"""Document versions this build imports.

A version-1 document is a valid older export and must still import, with the
new fields simply absent, which resolves to "no overrides, colour on".
"""

_LOCAL_ONLY_SETTING_PROBLEM: Final = (
    "{key} is set on the device itself and is never imported — "
    "set the password from the settings page"
)
"""Sentence for a document that names a :data:`LOCAL_ONLY_SETTING_KEYS` entry."""


@dataclass(frozen=True, slots=True)
class _SoundPlan:
    """One validated sound to upsert, keyed by its natural reference."""

    reference: str
    payload: SoundIn
    existing_id: int | None


@dataclass(frozen=True, slots=True)
class _ButtonPlan:
    """One validated button to upsert, with its unresolved sound reference."""

    payload: ButtonIn
    sound_reference: str | None


@dataclass(frozen=True, slots=True)
class _SettingsPlan:
    """One validated ``settings`` section.

    ``settings`` holds the registered rows, ``provider_secrets`` the unregistered
    ``tts.providers.<id>.api_key`` rows keyed by provider id.
    """

    settings: Mapping[str, object]
    provider_secrets: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _ImportPlan:
    """Everything validated from one document, ready to apply."""

    settings: Mapping[str, object]
    provider_secrets: Mapping[str, str]
    entities: Sequence[EntityIn]
    sounds: Sequence[_SoundPlan]
    buttons: Sequence[_ButtonPlan]
    problems: Sequence[str]


class _DryRunRollback(Exception):
    """Internal signal that rolls back a dry-run import transaction."""


async def export_config(store: ConfigStore, *, include_secrets: bool = False) -> dict[str, object]:
    """Export the current configuration as a JSON-compatible mapping.

    Only settings registered in :data:`SETTINGS` are exported, plus — with
    ``include_secrets`` — the provider API key rows, which are not registered
    settings. Rows stored under other keys this build does not know are
    local-only: they stay in this instance's database but never leave it,
    because :func:`import_config` rejects unknown keys.

    :data:`LOCAL_ONLY_SETTING_KEYS` are excluded **unconditionally**, including
    with ``include_secrets=True``: the configuration-interface password hash is
    credential-equivalent (it signs the admin cookie) and rotating it must need
    the current password, so it never leaves the device in a backup either.

    The rule for a provider's key: it leaves the device only with
    ``include_secrets=True`` and is written back through
    :meth:`ConfigStore.set_provider_secret`, never through the registry.

    :param store: connected configuration store.
    :param include_secrets: include ``ha.token``, ``mqtt.password`` and every
        provider API key.
    """
    settings = await store.all_settings(redact_secrets=not include_secrets)
    entities = await store.list_entities()
    sounds = await store.list_sounds()
    buttons = await store.list_buttons()
    sounds_by_id = {sound.id: sound for sound in sounds}

    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": utcnow_iso(),
        "settings": {
            key: value
            for key, value in settings.items()
            if key not in LOCAL_ONLY_SETTING_KEYS
            and (key in SETTINGS or provider_id_from_secret_key(key) is not None)
        },
        "entities": [_entity_document(entity) for entity in entities],
        "sounds": [_sound_document(sound) for sound in sounds],
        "buttons": [_button_document(button, sounds_by_id) for button in buttons],
    }


def _entity_document(entity: Entity) -> dict[str, object]:
    """Convert an entity row to its export form.

    :param entity: entity to export.
    """
    return {
        "entity_id": entity.entity_id,
        "display_name": entity.display_name,
        "icon": entity.icon,
        "accent_color": entity.accent_color,
        "show_attributes": (
            None if entity.show_attributes is None else list(entity.show_attributes)
        ),
        "state_tones": (
            None if entity.state_tones is None else dict(sorted(entity.state_tones.items()))
        ),
        "hide_when_unavailable": entity.hide_when_unavailable,
        "colour_by_state": entity.colour_by_state,
        "sort_order": entity.sort_order,
    }


def _sound_document(sound: Sound) -> dict[str, object]:
    """Convert a sound row to its export form.

    :param sound: sound to export.
    """
    return {
        "name": sound.name,
        "source_type": sound.source_type,
        "path": sound.path,
        "url": sound.url,
        "sha256": sound.sha256,
        "format": sound.format,
        "duration_ms": sound.duration_ms,
        "bytes": sound.bytes,
        "missing": sound.missing,
    }


def _button_document(button: Button, sounds_by_id: Mapping[int, Sound]) -> dict[str, object]:
    """Convert a button row to its export form.

    :param button: button to export.
    :param sounds_by_id: sounds indexed by id, used to resolve the natural key.
    """
    sound_reference: str | None = None
    if button.sound_id is not None:
        sound = sounds_by_id.get(button.sound_id)
        if sound is not None:
            sound_reference = sound.path if sound.source_type == "file" else sound.url
    return {
        "slug": button.slug,
        "name": button.name,
        "icon": button.icon,
        "action_type": button.action_type,
        "sound": sound_reference,
        "tts_text": button.tts_text,
        "tts_engine": button.tts_engine,
        "volume_override": button.volume_override,
        "dedupe_seconds": button.dedupe_seconds,
        "show_on_dashboard": button.show_on_dashboard,
        "quiet_hours_exempt": button.quiet_hours_exempt,
        "enabled": button.enabled,
        "sort_order": button.sort_order,
    }


async def import_config(
    store: ConfigStore,
    document: Mapping[str, object],
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Validate and merge a configuration document into the store.

    The post-import sound rescan is the caller's job (HSD-l22f / HSD-cwvc): the
    store only writes the index rows and never touches the sound files.

    :param store: connected configuration store.
    :param document: decoded JSON document, usually from :func:`export_config`.
    :param dry_run: validate and apply, then roll the transaction back.
    :raises ImportValidationError: if any part of the document is invalid. No
        rows are written in that case.
    """
    existing_entities = await store.list_entities()
    existing_sounds = await store.list_sounds()
    existing_buttons = await store.list_buttons()
    plan = _build_plan(document, existing_entities, existing_sounds, existing_buttons)
    if plan.problems:
        raise ImportValidationError(plan.problems)

    report = ImportReport(
        dry_run=dry_run,
        settings_written=len(plan.settings) + len(plan.provider_secrets),
        entities=len(plan.entities),
        sounds=len(plan.sounds),
        buttons=len(plan.buttons),
    )

    try:
        async with store.transaction():
            await _apply_plan(store, plan, existing_sounds)
            if dry_run:
                raise _DryRunRollback
    except _DryRunRollback:
        pass
    return report


async def _apply_plan(
    store: ConfigStore, plan: _ImportPlan, existing_sounds: Sequence[Sound]
) -> None:
    """Apply a validated plan inside the caller's transaction.

    :param store: connected configuration store.
    :param plan: validated document contents.
    :param existing_sounds: sounds that existed before the import.
    """
    for key, value in plan.settings.items():
        await store.set_setting(key, value)

    for provider_id, api_key in plan.provider_secrets.items():
        await store.set_provider_secret(provider_id, api_key)

    for entity in plan.entities:
        await store.upsert_entity(entity)

    sound_ids: dict[str, int] = {}
    for sound_plan in plan.sounds:
        if sound_plan.existing_id is None:
            created = await store.create_sound(sound_plan.payload)
            sound_ids[sound_plan.reference] = created.id
        else:
            updated = await store.update_sound(
                sound_plan.existing_id, _sound_update(sound_plan.payload)
            )
            sound_ids[sound_plan.reference] = updated.id

    for button_plan in plan.buttons:
        sound_id: int | None = None
        if button_plan.sound_reference is not None:
            sound_id = sound_ids.get(button_plan.sound_reference)
            if sound_id is None:
                sound_id = _resolve_existing_sound(button_plan.sound_reference, existing_sounds)
        await store.upsert_button(replace(button_plan.payload, sound_id=sound_id))


def _sound_update(sound: SoundIn) -> SoundUpdate:
    """Build a full patch that writes every exported sound field.

    :param sound: validated sound input from the document.
    """
    return SoundUpdate(
        name=sound.name,
        source_type=sound.source_type,
        path=sound.path,
        url=sound.url,
        sha256=sound.sha256,
        format=sound.format,
        duration_ms=sound.duration_ms,
        bytes=sound.bytes,
        missing=sound.missing,
    )


def _resolve_existing_sound(reference: str, sounds: Sequence[Sound]) -> int | None:
    """Resolve a sound reference against existing rows, path first then url.

    URL rows have no unique index, so the first match by id wins. A concurrent
    change between read and write is possible; HSD-cwvc owns deeper sound
    deduplication.

    :param reference: a sound's ``path`` or ``url``.
    :param sounds: existing sounds ordered by id.
    """
    for sound in sounds:
        if sound.path == reference:
            return sound.id
    for sound in sounds:
        if sound.url == reference:
            return sound.id
    return None


def _build_plan(
    document: Mapping[str, object],
    existing_entities: Sequence[Entity],
    existing_sounds: Sequence[Sound],
    existing_buttons: Sequence[Button],
) -> _ImportPlan:
    """Validate a document completely and build an import plan.

    Problems are accumulated rather than raised one at a time so the UI can
    report every issue in a single pass.

    :param document: decoded JSON document.
    :param existing_entities: current entities, matched by ``entity_id``.
    :param existing_sounds: current sounds, matched by path or URL.
    :param existing_buttons: current buttons, matched by slug.
    """
    problems: list[str] = []
    raw_settings = _section_mapping(document, "settings", problems)
    raw_entities = _section_list(document, "entities", problems)
    raw_sounds = _section_list(document, "sounds", problems)
    raw_buttons = _section_list(document, "buttons", problems)

    if document.get("format") not in ACCEPTED_EXPORT_FORMATS:
        accepted = " or ".join(repr(tag) for tag in ACCEPTED_EXPORT_FORMATS)
        problems.append(f"format must be {accepted}")
    if document.get("version") not in SUPPORTED_IMPORT_VERSIONS:
        supported = ", ".join(str(version) for version in SUPPORTED_IMPORT_VERSIONS)
        problems.append(f"version must be one of {supported}")

    settings = _validate_settings(raw_settings, problems)
    entity_plan = _validate_entities(raw_entities, existing_entities, problems)
    sound_plan = _validate_sounds(raw_sounds, existing_sounds, problems)
    button_plan = _validate_buttons(
        raw_buttons, sound_plan, existing_sounds, existing_buttons, problems
    )
    return _ImportPlan(
        settings=settings.settings,
        provider_secrets=settings.provider_secrets,
        entities=entity_plan,
        sounds=sound_plan,
        buttons=button_plan,
        problems=tuple(problems),
    )


def _section_mapping(
    document: Mapping[str, object], key: str, problems: list[str]
) -> Mapping[str, object]:
    """Read an object-valued document section.

    :param document: decoded JSON document.
    :param key: section name.
    :param problems: accumulator for validation messages.
    """
    value = document.get(key, {})
    if not isinstance(value, Mapping):
        problems.append(f"{key} must be an object")
        return {}
    return cast(Mapping[str, object], value)


def _section_list(document: Mapping[str, object], key: str, problems: list[str]) -> list[object]:
    """Read an array-valued document section.

    :param document: decoded JSON document.
    :param key: section name.
    :param problems: accumulator for validation messages.
    """
    value = document.get(key, [])
    if not isinstance(value, list):
        problems.append(f"{key} must be an array")
        return []
    return value


def _validate_settings(raw_settings: Mapping[str, object], problems: list[str]) -> _SettingsPlan:
    """Validate and normalise the settings section.

    A ``tts.providers.<id>.api_key`` row is deliberately not a registered
    setting: it is validated here only as an opaque string and collected apart
    from the ordinary settings, so :func:`_apply_plan` can write it through
    :meth:`ConfigStore.set_provider_secret` rather than the registry.

    A :data:`LOCAL_ONLY_SETTING_KEYS` entry is refused outright, not skipped: the
    whole import then writes nothing, because a document that carries the
    password hash would otherwise be a second way to rotate a credential
    without the current password.

    :param raw_settings: decoded ``settings`` object.
    :param problems: accumulator for validation messages.
    """
    settings: dict[str, object] = {}
    provider_secrets: dict[str, str] = {}
    for key, value in raw_settings.items():
        if key in LOCAL_ONLY_SETTING_KEYS:
            problems.append(_LOCAL_ONLY_SETTING_PROBLEM.format(key=key))
            continue
        provider_id = provider_id_from_secret_key(key)
        if provider_id is not None:
            api_key = _validate_provider_secret(key, value, problems)
            if api_key is not None:
                provider_secrets[provider_id] = api_key
            continue
        if key not in SETTINGS:
            problems.append(f"unknown setting key {key!r}")
            continue
        try:
            settings[key] = normalise_value(key, value)
        except ConfigStoreError as exc:
            problems.append(str(exc))
            continue
    return _SettingsPlan(settings=settings, provider_secrets=provider_secrets)


def _validate_provider_secret(key: str, value: object, problems: list[str]) -> str | None:
    """Validate one provider API key row without ever echoing its value.

    :param key: the ``tts.providers.<id>.api_key`` key naming the row.
    :param value: decoded JSON value.
    :param problems: accumulator for validation messages.
    """
    if not isinstance(value, str):
        problems.append(f"provider key {key!r} must be a string")
        return None
    if len(value) > PROVIDER_SECRET_MAX_LENGTH:
        problems.append(
            f"provider key {key!r} must be at most {PROVIDER_SECRET_MAX_LENGTH} characters"
        )
        return None
    return value


def _validate_entities(
    raw_entities: Sequence[object], existing: Sequence[Entity], problems: list[str]
) -> list[EntityIn]:
    """Validate the entities section and match it to existing rows by entity_id.

    :param raw_entities: decoded ``entities`` array.
    :param existing: current entities.
    :param problems: accumulator for validation messages.
    """
    existing_by_key = {entity.entity_id: entity for entity in existing}
    seen: set[str] = set()
    plan: list[EntityIn] = []
    for index, item in enumerate(raw_entities):
        label = f"entities[{index}]"
        entity = _as_mapping(item, label, problems)
        if entity is None:
            continue
        entity_id = _require_string(entity, "entity_id", label, problems)
        if entity_id is None:
            continue
        if entity_id in seen:
            problems.append(f"{label}.entity_id duplicates {entity_id!r}")
        seen.add(entity_id)
        current = existing_by_key.get(entity_id)
        plan.append(
            EntityIn(
                entity_id=entity_id,
                id=None if current is None else current.id,
                display_name=_optional_string(entity, "display_name", label, problems),
                icon=_optional_string(entity, "icon", label, problems),
                accent_color=_optional_string(entity, "accent_color", label, problems),
                show_attributes=_optional_string_tuple(entity, "show_attributes", label, problems),
                state_tones=_optional_tone_map(entity, "state_tones", label, problems),
                hide_when_unavailable=_optional_bool(
                    entity, "hide_when_unavailable", label, problems
                ),
                colour_by_state=_optional_bool(
                    entity, "colour_by_state", label, problems, default=True
                ),
                sort_order=_optional_int(entity, "sort_order", label, problems),
            )
        )
    return plan


def _validate_sounds(
    raw_sounds: Sequence[object], existing: Sequence[Sound], problems: list[str]
) -> list[_SoundPlan]:
    """Validate the sounds section and build upsert plans by natural key.

    File sounds match existing rows by ``path``; URL sounds match by ``url``.
    URL rows have no unique index, so duplicate URLs resolve to the first
    existing row by id. A concurrent insert between read and write is possible;
    HSD-cwvc owns sound deduplication.

    :param raw_sounds: decoded ``sounds`` array.
    :param existing: current sounds.
    :param problems: accumulator for validation messages.
    """
    existing_by_reference: dict[str, Sound] = {}
    for existing_sound in existing:
        if existing_sound.path is not None:
            existing_by_reference.setdefault(existing_sound.path, existing_sound)
    for existing_sound in existing:
        if existing_sound.url is not None:
            existing_by_reference.setdefault(existing_sound.url, existing_sound)

    seen: set[str] = set()
    plan: list[_SoundPlan] = []
    for index, item in enumerate(raw_sounds):
        label = f"sounds[{index}]"
        raw_sound = _as_mapping(item, label, problems)
        if raw_sound is None:
            continue
        name = _require_string(raw_sound, "name", label, problems)
        source_type = _require_string(raw_sound, "source_type", label, problems)
        if name is None or source_type is None:
            continue
        if source_type not in {"file", "url"}:
            problems.append(f"{label}.source_type must be 'file' or 'url'")
            continue
        path = _optional_string(raw_sound, "path", label, problems)
        url = _optional_string(raw_sound, "url", label, problems)
        reference = path if source_type == "file" else url
        if reference is None or not reference:
            field = "path" if source_type == "file" else "url"
            problems.append(f"{label}.{field} is required for {source_type} sounds")
            continue
        if reference in seen:
            problems.append(f"{label} duplicates sound reference {reference!r}")
        seen.add(reference)
        current = existing_by_reference.get(reference)
        plan.append(
            _SoundPlan(
                reference=reference,
                payload=SoundIn(
                    name=name,
                    source_type=source_type,
                    path=path,
                    url=url,
                    sha256=_optional_string(raw_sound, "sha256", label, problems),
                    format=_optional_string(raw_sound, "format", label, problems),
                    duration_ms=_optional_int(raw_sound, "duration_ms", label, problems),
                    bytes=_optional_int(raw_sound, "bytes", label, problems),
                    missing=_optional_bool(raw_sound, "missing", label, problems),
                ),
                existing_id=None if current is None else current.id,
            )
        )
    return plan


def _validate_buttons(
    raw_buttons: Sequence[object],
    sounds: Sequence[_SoundPlan],
    existing_sounds: Sequence[Sound],
    existing_buttons: Sequence[Button],
    problems: list[str],
) -> list[_ButtonPlan]:
    """Validate the buttons section and check every sound reference.

    :param raw_buttons: decoded ``buttons`` array.
    :param sounds: validated sound plans, whose references are resolvable.
    :param existing_sounds: current sounds, also valid reference targets.
    :param existing_buttons: current buttons, matched by slug.
    :param problems: accumulator for validation messages.
    """
    existing_by_slug = {button.slug: button for button in existing_buttons}
    document_references = {sound.reference for sound in sounds}
    existing_references = {
        reference
        for sound in existing_sounds
        for reference in (sound.path, sound.url)
        if reference is not None
    }

    seen: set[str] = set()
    plan: list[_ButtonPlan] = []
    for index, item in enumerate(raw_buttons):
        label = f"buttons[{index}]"
        button = _as_mapping(item, label, problems)
        if button is None:
            continue
        slug = _require_string(button, "slug", label, problems)
        name = _require_string(button, "name", label, problems)
        action_type = _require_string(button, "action_type", label, problems)
        if slug is None or name is None or action_type is None:
            continue
        if slug in seen:
            problems.append(f"{label}.slug duplicates {slug!r}")
        seen.add(slug)
        if action_type not in {"sound", "tts"}:
            problems.append(f"{label}.action_type must be 'sound' or 'tts'")
            continue

        sound_reference = _optional_string(button, "sound", label, problems)
        if action_type == "sound":
            if not sound_reference:
                problems.append(f"{label}.sound is required for sound buttons")
            elif (
                sound_reference not in document_references
                and sound_reference not in existing_references
            ):
                problems.append(
                    f"{label}.sound reference {sound_reference!r} does not resolve "
                    "to a document or existing sound"
                )

        tts_text = _optional_string(button, "tts_text", label, problems)
        if action_type == "tts" and (tts_text is None or not tts_text.strip()):
            problems.append(f"{label}.tts_text is required for tts buttons")

        current = existing_by_slug.get(slug)
        volume_override = _optional_int(button, "volume_override", label, problems)
        if volume_override is not None and not 0 <= volume_override <= 100:
            problems.append(f"{label}.volume_override must be between 0 and 100")
        plan.append(
            _ButtonPlan(
                payload=ButtonIn(
                    id=None if current is None else current.id,
                    slug=slug,
                    name=name,
                    icon=_optional_string(button, "icon", label, problems),
                    action_type=action_type,
                    sound_id=None,
                    tts_text=tts_text,
                    tts_engine=_optional_string(button, "tts_engine", label, problems),
                    volume_override=volume_override,
                    dedupe_seconds=_optional_int(button, "dedupe_seconds", label, problems),
                    show_on_dashboard=_optional_bool(button, "show_on_dashboard", label, problems),
                    quiet_hours_exempt=_optional_bool(
                        button, "quiet_hours_exempt", label, problems
                    ),
                    enabled=_optional_bool(button, "enabled", label, problems, default=True),
                    sort_order=_optional_int(button, "sort_order", label, problems),
                ),
                sound_reference=sound_reference,
            )
        )
    return plan


def _as_mapping(item: object, label: str, problems: list[str]) -> Mapping[str, object] | None:
    """Coerce one array item to an object.

    :param item: raw JSON value.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    """
    if not isinstance(item, Mapping):
        problems.append(f"{label} must be an object")
        return None
    return cast(Mapping[str, object], item)


def _require_string(
    mapping: Mapping[str, object], key: str, label: str, problems: list[str]
) -> str | None:
    """Read a required non-empty string field.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    """
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        problems.append(f"{label}.{key} must be a non-empty string")
        return None
    return value


def _optional_string(
    mapping: Mapping[str, object], key: str, label: str, problems: list[str]
) -> str | None:
    """Read an optional string field.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    """
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(f"{label}.{key} must be a string or null")
        return None
    return value


def _optional_int(
    mapping: Mapping[str, object], key: str, label: str, problems: list[str]
) -> int | None:
    """Read an optional integer field, rejecting booleans.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    """
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{label}.{key} must be an integer or null")
        return None
    return value


def _optional_bool(
    mapping: Mapping[str, object],
    key: str,
    label: str,
    problems: list[str],
    *,
    default: bool = False,
) -> bool:
    """Read an optional boolean field.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    :param default: value used when the field is absent.
    """
    value = mapping.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        problems.append(f"{label}.{key} must be a boolean")
        return default
    return value


def _optional_string_tuple(
    mapping: Mapping[str, object], key: str, label: str, problems: list[str]
) -> tuple[str, ...] | None:
    """Read an optional array-of-strings field.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    """
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{label}.{key} must be an array of strings or null")
        return None
    return tuple(cast(str, item) for item in value)


def _optional_tone_map(
    mapping: Mapping[str, object], key: str, label: str, problems: list[str]
) -> dict[str, str] | None:
    """Read an optional object field of state→tone entries.

    A bad entry is reported and dropped, but the valid remainder still reaches
    the import plan, the same way a bad field elsewhere in an entity document
    does: one typo must not discard the rest of a tile's mapping. Keys are
    trimmed and stored verbatim, because a tone lookup is case-sensitive and a
    key that only differs by surrounding whitespace would silently never match.

    :param mapping: decoded object.
    :param key: field name.
    :param label: human readable location for messages.
    :param problems: accumulator for validation messages.
    :returns: the sorted tone mapping, or ``None`` when the field is absent or
        ``null`` (which is "no overrides", not an empty mapping).
    """
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        problems.append(f"{label}.{key} must be an object or null")
        return None
    allowed = ", ".join(TONES)
    tones: dict[str, str] = {}
    for raw_state, raw_tone in cast("Mapping[object, object]", value).items():
        state = raw_state.strip() if isinstance(raw_state, str) else ""
        if not state:
            problems.append(f"{label}.{key} keys must be non-empty strings")
            continue
        if not is_tone(raw_tone):
            problems.append(f"{label}.{key}.{state} must be one of {allowed} — got {raw_tone!r}")
            continue
        tones[state] = cast(str, raw_tone)
    return dict(sorted(tones.items()))
