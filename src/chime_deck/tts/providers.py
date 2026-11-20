"""The paid-provider seam, the template predicate D25 couples to it, and the
configured provider records the ``http_tts`` engine speaks through.

Two questions travel together: "does this engine bill per character?" (D25's
``paid`` flag) and "will this phrase be rendered on every press?" (anything with
Jinja ``{{ … }}`` or ``{% … %}``). A templated phrase on a paid engine is billed
again and again, which is exactly the pairing D25 exists to gate, so the lookup
port and the predicate live in one module instead of being invented twice.

:func:`has_template_syntax` has two future consumers and must not be copied:

* **HSD-dzek** uses it for the static/dynamic split — a phrase without template
  syntax is baked once on save, a phrase with it is rendered per press.
* **HSD-s856** (the generic ``http_tts`` provider engine with presets, the
  ``paid`` flag and the ``tts.providers`` setting) imports it rather than
  re-implementing the Jinja sniff, and supplies the registry behind
  :class:`PaidProviderLookup`.

HSD-5de8 is the remote renderer engine and owns none of this.

:class:`ConfiguredProviders` is the registry HSD-s856 supplies. It reads
``tts.providers`` and the selected provider id from the live configuration on
every question, which is why :meth:`PaidProviderLookup.is_paid` is asynchronous:
a cached synchronous answer would go stale the moment a provider was edited, and
the D25 refusal has to be exact. Without a source wired,
:data:`NO_PAID_PROVIDERS` remains the honest default: it marks nothing paid and
D25 stays dormant.

A stored record that cannot be read is skipped by :func:`read_providers` rather
than crashing a press, because a broken hand-edited provider (or one imported
from a newer build) must never take the doorbell down. The writing path,
:func:`parse_providers`, is the strict one.

Owning ticket: HSD-1drv; the preset and provider registry by HSD-s856.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol, cast, runtime_checkable
from uuid import uuid4

from .engines import Availability
from .presets import (
    DEFAULT_PRESETS,
    SHAPE_KEYS,
    Preset,
    PresetError,
    ProviderPresets,
    ProviderShape,
    shape_from_overrides,
)

__all__ = [
    "HTTP_TTS_ENGINE",
    "NO_PAID_PROVIDERS",
    "PROVIDER_ID_PATTERN",
    "ConfiguredProviders",
    "NoPaidProviders",
    "PaidProviderLookup",
    "ProviderError",
    "ProviderRecord",
    "ProviderSource",
    "ResolvedProvider",
    "SynthesisCosting",
    "TtsCost",
    "has_template_syntax",
    "new_provider_id",
    "parse_provider",
    "parse_providers",
    "read_providers",
]

logger = logging.getLogger(__name__)

PROVIDER_ID_PATTERN: Final = r"[0-9a-f]{12}"
"""A provider id is exactly 12 lower-case hex characters, as the store requires."""

HTTP_TTS_ENGINE: Final = "http_tts"
"""The engine name whose billing and configuration this module answers for."""

_PROVIDERS_KEY: Final = "tts.providers"
_SELECTED_PROVIDER_KEY: Final = "tts.http_tts.provider"
_PROVIDER_ID: Final = re.compile(PROVIDER_ID_PATTERN)
_LANGUAGE_PATTERN: Final = re.compile(r"[a-z]{2,3}([_-][A-Za-z]{2,4})?")
_URL_PATTERN: Final = re.compile(r"https?://\S+")
_MAX_NAME_CHARS: Final = 200
_MAX_URL_CHARS: Final = 2048
_MAX_VOICE_CHARS: Final = 255
_CUSTOM_PRESET: Final = "custom"
_SECRET_FIELD: Final = "api_key"
_SHAPE_OVERRIDE_KEYS: Final = frozenset(SHAPE_KEYS)
_PRICE_OVERRIDE_KEYS: Final = frozenset({"paid", "cost_per_1k_chars_usd"})
_OVERRIDE_KEYS: Final = _SHAPE_OVERRIDE_KEYS | _PRICE_OVERRIDE_KEYS
_BASE_URL_HINTS: Final = MappingProxyType(
    {
        "azure": "the region endpoint, for example https://westeurope.tts.speech.microsoft.com",
        "openai_compatible": "the server's own address, for example http://kokoro.local:8880",
        "custom": "the server's own address",
    }
)
_DEFAULT_BASE_URL_HINT: Final = "the server address"

_TEMPLATE_PATTERN: Final = re.compile(r"{{.*?}}|{%.*?%}", re.DOTALL)
"""Jinja expression and statement delimiters, matched across line breaks."""


class PaidProviderLookup(Protocol):
    """Answer whether a TTS engine is billed per character (D25)."""

    async def is_paid(self, engine: str) -> bool:
        """Return whether *engine* costs money for every character rendered.

        Asynchronous on purpose: the answer comes from the live configuration,
        so a provider edited a second ago is reflected in the very next save.

        :param engine: configured engine name, exactly as stored.
        """
        ...


@dataclass(frozen=True, slots=True)
class NoPaidProviders:
    """Default: no provider metadata exists yet, so NO engine can be paid."""

    async def is_paid(self, engine: str) -> bool:
        """Return ``False`` for every engine.

        :param engine: configured engine name; ignored.
        """
        return False


NO_PAID_PROVIDERS: Final = NoPaidProviders()
"""The production default until a ticket supplies a real provider registry."""


class ProviderError(Exception):
    """A provider record that cannot be used, as one user-safe sentence.

    The message names the offending field (and where it helps, the provider's
    name) and never a secret's value, so it can be returned by the API as-is.
    """


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """One configured external provider, as stored in ``tts.providers``.

    ``base_url`` is ``""`` when the preset's own default is used or when the
    user has not filled one in yet; ``cost_per_1k_chars_usd`` ``None`` means the
    preset's estimate applies; ``overrides`` is honoured for the ``custom``
    preset only and replaces shape fields, never merges them.
    """

    id: str
    name: str
    preset: str
    base_url: str
    voice: str
    language: str
    paid: bool
    cost_per_1k_chars_usd: float | None
    overrides: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """A stored record plus everything needed to make its request.

    ``base_url`` has no trailing ``/`` and already falls back to the preset's
    default; ``api_key`` is ``""`` when none is stored. :attr:`paid` and
    :attr:`cost_per_1k_chars_usd` apply the record's custom-preset overrides
    before falling back to the preset, so a caller never re-implements the
    precedence.
    """

    record: ProviderRecord
    preset: Preset
    shape: ProviderShape
    base_url: str
    api_key: str

    @property
    def has_api_key(self) -> bool:
        """Return whether a non-empty API key is stored for this provider."""
        return self.api_key != ""

    @property
    def needs_api_key(self) -> bool:
        """Return whether the request shape authenticates with a key."""
        return self.shape.auth.style != "none"

    @property
    def paid(self) -> bool:
        """Return whether a synthesis through this provider is billed.

        The record is authoritative: the preset's ``paid`` flag only seeds a new
        record, so a user who marks a preset-paid provider as free gets a free
        provider.
        """
        override = _override(self.record, "paid")
        if isinstance(override, bool):
            return override
        return self.record.paid

    @property
    def cost_per_1k_chars_usd(self) -> float | None:
        """Return the price used for estimates, ``None`` when no price is known."""
        if self.record.cost_per_1k_chars_usd is not None:
            return self.record.cost_per_1k_chars_usd
        override = _override(self.record, "cost_per_1k_chars_usd")
        if isinstance(override, (int, float)) and not isinstance(override, bool):
            return float(override)
        return self.preset.cost_per_1k_chars_usd


@dataclass(frozen=True, slots=True)
class TtsCost:
    """What one synthesis cost, or charitably could have."""

    chars: int
    usd: float | None

    def note(self) -> str:
        """Return one sentence for a log line or the press detail."""
        noun = "character" if self.chars == 1 else "characters"
        if self.usd is None:
            return f"{self.chars} {noun} billed, cost unknown (no price configured)"
        return f"{self.chars} {noun} billed, estimated ${self.usd:.4f}"


@runtime_checkable
class SynthesisCosting(Protocol):
    """What an engine asks before spending money: the price of this text.

    Duck-typed the way :class:`~chime_deck.tts.engines.SynthesisTiming` is —
    ``runtime_checkable`` so a caller checks with ``isinstance`` and a typo
    cannot silently answer ``None`` — so an engine can take an implementation
    without importing one.
    """

    async def estimate_cost(self, text: str) -> TtsCost | None:
        """Return what *text* would cost through the selected provider.

        :param text: the phrase as it will be sent, after templating.
        """
        ...


class ProviderSource(Protocol):
    """The store-facing port: :class:`~chime_deck.store.ConfigStore` satisfies it."""

    async def get_setting(self, key: str) -> object:
        """Return the stored value of one setting.

        :param key: registered setting key.
        """
        ...

    async def get_provider_secret(self, provider_id: str) -> str:
        """Return one provider's stored API key, or ``""`` when it has none.

        :param provider_id: 12-character hexadecimal provider id.
        """
        ...


class ConfiguredProviders:
    """The real :class:`PaidProviderLookup` (D25), reading the live configuration.

    Every method re-reads ``tts.providers`` and ``tts.http_tts.provider`` from
    the source, so a provider added, edited or deleted in Settings applies to the
    next save, the next press and the next cost estimate without a restart.
    """

    name: Final = "configured_providers"

    def __init__(
        self,
        source: ProviderSource,
        *,
        presets: ProviderPresets = DEFAULT_PRESETS,
    ) -> None:
        """Create the registry over *source*'s live configuration.

        :param source: settings reader owning ``tts.providers`` and the secrets.
        :param presets: preset library to resolve records against.
        """
        self._source = source
        self._presets = presets

    async def records(self) -> tuple[ProviderRecord, ...]:
        """Return every readable provider record, skipping broken ones."""
        return read_providers(await self._source.get_setting(_PROVIDERS_KEY), presets=self._presets)

    async def resolved(self) -> tuple[ResolvedProvider, ...]:
        """Return every readable record with its preset, shape, address and key."""
        resolved: list[ResolvedProvider] = []
        for record in await self.records():
            preset = self._presets.get(record.preset)
            if preset is None:
                continue
            try:
                shape = self._shape_for(record, preset)
            except PresetError as exc:
                logger.warning("ignoring provider %r: %s", record.name, exc)
                continue
            api_key = await self._source.get_provider_secret(record.id)
            resolved.append(
                ResolvedProvider(
                    record=record,
                    preset=preset,
                    shape=shape,
                    base_url=_effective_base_url(record, preset),
                    api_key=api_key,
                )
            )
        return tuple(resolved)

    async def selected(self) -> ResolvedProvider | None:
        """Return the provider ``tts.http_tts.provider`` names, if it still exists."""
        wanted = await self._selected_id()
        if wanted is None:
            return None
        for provider in await self.resolved():
            if provider.record.id == wanted:
                return provider
        return None

    async def availability(self) -> Availability:
        """Return whether ``http_tts`` can speak, with the reason when it cannot."""
        if not await self.records():
            return Availability(
                False,
                "no TTS provider is configured yet — add one in Settings → Speech",
            )
        wanted = await self._selected_id()
        if wanted is None:
            return Availability(
                False,
                "no TTS provider is selected for the http_tts engine — "
                "choose one in Settings → Speech",
            )
        provider = await self.selected()
        if provider is None:
            return Availability(
                False,
                f"the selected TTS provider ({wanted}) is no longer configured — "
                "choose another in Settings → Speech",
            )
        if provider.needs_api_key and not provider.has_api_key:
            return Availability(
                False,
                f"TTS provider {provider.record.name!r} has no API key stored — "
                "add one in Settings → Speech",
            )
        return Availability(True)

    async def estimate_cost(self, text: str) -> TtsCost | None:
        """Return what *text* would cost through the selected provider.

        The count is ``len(text)``, the characters actually sent; the price is
        the record's own when set, otherwise the preset's estimate, and
        ``None`` — never ``0.0`` — when neither knows one.
        """
        provider = await self.selected()
        if provider is None:
            return None
        price = provider.cost_per_1k_chars_usd
        usd = None if price is None else len(text) * price / 1000.0
        return TtsCost(chars=len(text), usd=usd)

    async def is_paid(self, engine: str) -> bool:
        """Return whether *engine* bills per character right now.

        True only when *engine* is ``http_tts`` and the selected provider exists
        and is marked paid. With no provider configured, none selected, or a
        paid flag never set, every engine name answers ``False`` — the same
        honest default as :data:`NO_PAID_PROVIDERS`.

        :param engine: configured engine name, exactly as stored.
        """
        if engine != HTTP_TTS_ENGINE:
            return False
        provider = await self.selected()
        return provider is not None and provider.paid

    def _shape_for(self, record: ProviderRecord, preset: Preset) -> ProviderShape:
        """Return the preset shape with a custom record's overrides applied.

        :param record: already-validated provider record.
        :param preset: the preset the record names.
        """
        if record.preset != _CUSTOM_PRESET or not record.overrides:
            return preset.shape
        overrides = {
            key: value for key, value in record.overrides.items() if key in _SHAPE_OVERRIDE_KEYS
        }
        return shape_from_overrides(preset.shape, overrides, where=f"provider {record.name!r}")

    async def _selected_id(self) -> str | None:
        """Return the stored selected provider id, or ``None`` when none is set."""
        raw = await self._source.get_setting(_SELECTED_PROVIDER_KEY)
        return raw if isinstance(raw, str) and raw else None


def new_provider_id() -> str:
    """Return a fresh provider id: the first 12 hex characters of a UUID4."""
    return uuid4().hex[:12]


def parse_provider(
    raw: object,
    *,
    presets: ProviderPresets,
    index: int = 0,
) -> ProviderRecord:
    """Validate one stored provider record and return it.

    This is the strict path, used when a record is written. :func:`read_providers`
    is the tolerant reader for records that are already stored.

    :param raw: one element of the ``tts.providers`` array.
    :param presets: preset library the record's ``preset`` must name.
    :param index: zero-based position in the array, for the error sentence.
    :raises ProviderError: the first rule the record breaks, as one sentence.
    """
    where = f"provider #{index + 1}"
    if not isinstance(raw, Mapping):
        raise ProviderError(f"{where} must be a JSON object with an id, a name and a preset")
    data = cast(Mapping[str, object], raw)
    if _SECRET_FIELD in data:
        raise ProviderError(
            f"{where} must not contain an api_key — the API key is stored separately, "
            "outside tts.providers"
        )
    provider_id = data.get("id")
    if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
        raise ProviderError(f"{where} needs an id of exactly 12 lower-case hexadecimal characters")
    name = _name(data, where)
    preset = _preset(data, presets, where)
    base_url = _base_url(data, preset, where)
    voice = _voice(data, preset, where)
    language = _language(data, where)
    overrides = _overrides(data, preset, where)
    return ProviderRecord(
        id=provider_id,
        name=name,
        preset=preset.id,
        base_url=base_url,
        voice=voice,
        language=language,
        paid=_paid(data.get("paid"), where=where),
        cost_per_1k_chars_usd=_price(data.get("cost_per_1k_chars_usd"), where=where),
        overrides=overrides,
    )


def parse_providers(raw: object, *, presets: ProviderPresets) -> tuple[ProviderRecord, ...]:
    """Validate the whole stored ``tts.providers`` value and return its records.

    Rejects a duplicate id or name, because the picker and the selected id are
    both keyed on them. Names are compared ignoring case.

    :param raw: the stored ``tts.providers`` value; must be a JSON array.
    :param presets: preset library every record's ``preset`` must name.
    :raises ProviderError: the first rule any record breaks, as one sentence.
    """
    if not isinstance(raw, list):
        raise ProviderError("tts.providers must be a JSON array of provider objects")
    records: list[ProviderRecord] = []
    seen_ids: dict[str, int] = {}
    seen_names: dict[str, int] = {}
    for index, item in enumerate(cast(list[object], raw)):
        record = parse_provider(item, presets=presets, index=index)
        first = seen_ids.get(record.id)
        if first is not None:
            raise ProviderError(
                f"provider #{index + 1} repeats the id of provider #{first + 1} — "
                "every provider needs its own id"
            )
        first = seen_names.get(record.name.lower())
        if first is not None:
            raise ProviderError(
                f"provider #{index + 1} repeats the name of provider #{first + 1} — "
                "every provider needs its own name"
            )
        seen_ids[record.id] = index
        seen_names[record.name.lower()] = index
        records.append(record)
    return tuple(records)


def read_providers(raw: object, *, presets: ProviderPresets) -> tuple[ProviderRecord, ...]:
    """Return every record in *raw* this build can read, skipping the rest.

    Never raises. A value that is not an array, an unreadable record, a record
    naming a preset this build does not ship, and a duplicate id or name are all
    logged (by position, never by secret) and left out, so one bad hand-edited
    or imported provider cannot stop a press.

    :param raw: the stored ``tts.providers`` value, whatever it holds.
    :param presets: preset library to resolve records against.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("tts.providers is not a JSON array; ignoring the stored provider list")
        return ()
    records: list[ProviderRecord] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, item in enumerate(cast(list[object], raw)):
        try:
            record = parse_provider(item, presets=presets, index=index)
        except ProviderError as exc:
            logger.warning("skipping provider #%d in tts.providers: %s", index + 1, exc)
            continue
        if record.id in seen_ids or record.name.lower() in seen_names:
            logger.warning(
                "skipping provider #%d in tts.providers: its id or name is already used",
                index + 1,
            )
            continue
        seen_ids.add(record.id)
        seen_names.add(record.name.lower())
        records.append(record)
    return tuple(records)


def _override(record: ProviderRecord, key: str) -> object:
    """Return one custom-preset override value, or ``None``.

    :param record: provider record that may carry overrides.
    :param key: override key to read.
    """
    if record.preset != _CUSTOM_PRESET:
        return None
    return record.overrides.get(key)


def _name(data: Mapping[str, object], where: str) -> str:
    """Return the record's required, bounded display name.

    :param data: raw record mapping.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the name is missing, blank or too long.
    """
    value = data.get("name")
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{where} needs a non-empty name")
    name = value.strip()
    if len(name) > _MAX_NAME_CHARS:
        raise ProviderError(
            f"{where} has a name longer than {_MAX_NAME_CHARS} characters — shorten it"
        )
    return name


def _preset(data: Mapping[str, object], presets: ProviderPresets, where: str) -> Preset:
    """Return the preset the record names.

    :param data: raw record mapping.
    :param presets: preset library.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the preset is missing or unknown to this build.
    """
    preset_id = data.get("preset")
    known = ", ".join(presets.ids()) or "none"
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise ProviderError(f"{where} needs a preset id — known presets are {known}")
    preset = presets.get(preset_id.strip())
    if preset is None:
        raise ProviderError(
            f"{where} names an unknown preset {preset_id.strip()!r} — known presets are {known}"
        )
    return preset


def _base_url(data: Mapping[str, object], preset: Preset, where: str) -> str:
    """Return the record's server address, ``""`` when the preset's default applies.

    :param data: raw record mapping.
    :param preset: the preset the record names.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the address is not an http(s) URL, or is blank
        for a preset that has no default.
    """
    value = data.get("base_url", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProviderError(f"{where} has a base_url that must be a string")
    base_url = value.strip()
    if len(base_url) > _MAX_URL_CHARS:
        raise ProviderError(
            f"{where} has a base_url longer than {_MAX_URL_CHARS} characters — shorten it"
        )
    if base_url and _URL_PATTERN.fullmatch(base_url) is None:
        raise ProviderError(
            f"{where} has a base_url that is not an http(s) address — "
            "it must start with http:// or https:// and contain no spaces"
        )
    if not base_url and preset.base_url is None:
        hint = _BASE_URL_HINTS.get(preset.id, _DEFAULT_BASE_URL_HINT)
        raise ProviderError(f"preset '{preset.id}' needs a base_url — {hint}")
    return base_url


def _voice(data: Mapping[str, object], preset: Preset, where: str) -> str:
    """Return the record's voice, refusing a blank one the preset needs.

    :param data: raw record mapping.
    :param preset: the preset the record names.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the voice is not a string, too long, or blank
        while the preset's request carries ``{voice}``.
    """
    value = data.get("voice", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProviderError(f"{where} has a voice that must be a string")
    voice = value.strip()
    if len(voice) > _MAX_VOICE_CHARS:
        raise ProviderError(
            f"{where} has a voice longer than {_MAX_VOICE_CHARS} characters — shorten it"
        )
    if not voice and preset.shape.uses_voice:
        raise ProviderError(
            f"{where} needs a voice — preset '{preset.id}' sends the {{voice}} "
            "placeholder in its request"
        )
    return voice


def _language(data: Mapping[str, object], where: str) -> str:
    """Return the record's language tag, ``""`` when unset.

    :param data: raw record mapping.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the value is not a language tag.
    """
    value = data.get("language", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProviderError(f"{where} has a language that must be a string")
    language = value.strip()
    if language and _LANGUAGE_PATTERN.fullmatch(language) is None:
        raise ProviderError(
            f"{where} has a language that is not a language tag — use a tag such as en_GB or en-GB"
        )
    return language


def _paid(value: object, *, where: str) -> bool:
    """Return the record's paid flag.

    :param value: raw ``paid`` value.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the value is not a boolean.
    """
    if not isinstance(value, bool):
        raise ProviderError(f"{where} needs paid to be true or false")
    return value


def _price(value: object, *, where: str) -> float | None:
    """Return the record's price per 1000 characters, ``None`` meaning unknown.

    :param value: raw ``cost_per_1k_chars_usd`` value.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when the value is a boolean, negative or not finite.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(
            f"{where} has a cost_per_1k_chars_usd that must be null or a number "
            "of dollars per 1000 characters"
        )
    price = float(value)
    if not math.isfinite(price) or price < 0:
        raise ProviderError(
            f"{where} has a cost_per_1k_chars_usd that is not a usable price — "
            "it must be null or a finite number of dollars per 1000 characters, 0 or more"
        )
    return price


def _overrides(data: Mapping[str, object], preset: Preset, where: str) -> Mapping[str, object]:
    """Return the record's validated overrides, empty unless the preset is custom.

    :param data: raw record mapping.
    :param preset: the preset the record names.
    :param where: sentence prefix naming the offender.
    :raises ProviderError: when overrides are not an object, are supplied for a
        non-custom preset, or carry an unknown field or an invalid value.
    """
    raw = data.get("overrides")
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, Mapping):
        raise ProviderError(f"{where} has overrides that are not a JSON object")
    overrides = cast(Mapping[str, object], raw)
    if not overrides:
        return MappingProxyType({})
    if _SECRET_FIELD in overrides:
        raise ProviderError(
            f"{where} must not contain an api_key — the API key is stored separately, "
            "outside tts.providers"
        )
    if preset.id != _CUSTOM_PRESET:
        raise ProviderError(
            f"{where} has overrides but preset '{preset.id}' does not accept them — "
            f"only the '{_CUSTOM_PRESET}' preset can be overridden"
        )
    unknown = sorted(set(overrides) - _OVERRIDE_KEYS)
    if unknown:
        raise ProviderError(
            f"{where} overrides a field this build does not recognise: "
            f"{', '.join(unknown)} — overrides may set "
            f"{', '.join(sorted(_OVERRIDE_KEYS))}"
        )
    shape_keys = {key: value for key, value in overrides.items() if key in _SHAPE_OVERRIDE_KEYS}
    try:
        shape_from_overrides(preset.shape, shape_keys, where=where)
    except PresetError as exc:
        raise ProviderError(str(exc)) from None
    if "paid" in overrides:
        _paid(overrides["paid"], where=where)
    if "cost_per_1k_chars_usd" in overrides:
        _price(overrides["cost_per_1k_chars_usd"], where=where)
    return MappingProxyType(dict(overrides))


def _effective_base_url(record: ProviderRecord, preset: Preset) -> str:
    """Return the address a request goes to, with no trailing ``/``.

    :param record: the provider record.
    :param preset: the preset the record names.
    """
    return (record.base_url or preset.base_url or "").rstrip("/")


def has_template_syntax(text: str) -> bool:
    """Return whether *text* contains Jinja ``{{ … }}`` or ``{% … %}`` syntax.

    The phrase is rendered per press when this is true, so it can be billed per
    character on a paid engine (D25) and can never be pre-rendered (HSD-dzek).

    :param text: TTS phrase to inspect.
    """
    return _TEMPLATE_PATTERN.search(text) is not None
