"""The bundled TTS provider presets, loaded from data files beside this one.

A preset is the *shape* of one HTTP text-to-speech request: the method and path,
the auth header style, the JSON (or SSML) body, and how to read the audio back
out of the reply. Presets live in ``tts/presets/*.json`` as data, so adding a
provider is a new JSON file and needs no code change; the packaged directory is
read with :mod:`importlib.resources` so an installed wheel has the same files a
checkout does.

Prices are estimates, not invoices: ``cost_per_1k_chars_usd`` is a starting
number copied from a published rate, ``pricing_note`` says so, and a provider
record can override it with the user's own plan.

Query-string API keys are deliberately unsupported. ``httpx`` logs every request
URL at INFO, so a key in the query would be printed to the log and travel into
support dumps; the only auth styles accepted are ``bearer``, a named header and
``none``.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import importlib.resources
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

__all__ = [
    "AUDIO_FORMATS",
    "AUTH_STYLES",
    "BODY_KINDS",
    "DEFAULT_PRESETS",
    "SHAPE_KEYS",
    "AuthStyle",
    "Preset",
    "PresetError",
    "PresetSummary",
    "ProviderPresets",
    "ProviderShape",
    "load_presets",
    "shape_from_overrides",
]

AUDIO_FORMATS: Final = ("mp3", "wav", "ogg", "flac")
"""Containers this project can play; anything else is refused at load time."""

BODY_KINDS: Final = ("json", "text")
"""``json`` sends a JSON object, ``text`` sends a raw body such as SSML."""

AUTH_STYLES: Final = ("bearer", "header", "none")
"""Accepted ``auth.style`` values; ``query`` is refused (see the module docstring)."""

SHAPE_KEYS: Final = (
    "method",
    "path",
    "headers",
    "auth",
    "body_kind",
    "body",
    "response",
    "format",
)
"""The request-shape fields a preset file carries, and a custom record may override."""

_PRESET_ID: Final = re.compile(r"[a-z][a-z0-9_]*")
"""A preset id is the file name: lower-case letters, digits and underscores."""

_MAX_BASE_URL_CHARS: Final = 2048
_URL_PATTERN: Final = re.compile(r"https?://\S+")
_VOICE_PLACEHOLDER: Final = "{voice}"
_LANGUAGE_PLACEHOLDER: Final = "{language}"
_JSON_RESPONSE_PREFIX: Final = "json:"


class PresetError(Exception):
    """A preset file, or a custom override, that cannot be used as written.

    The message is one whole sentence naming the offending preset and field, so
    it can be shown to a user and written to the log unchanged.
    """


@dataclass(frozen=True, slots=True)
class AuthStyle:
    """How one preset authenticates: a bearer token, a named header or nothing."""

    style: str
    name: str = ""
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class ProviderShape:
    """The one request a preset (or a custom provider) makes."""

    method: str
    path: str
    headers: Mapping[str, str]
    auth: AuthStyle
    body_kind: str
    body: object
    response: str
    format: str

    @property
    def uses_voice(self) -> bool:
        """Return whether the request text carries the ``{voice}`` placeholder."""
        return self._mentions(_VOICE_PLACEHOLDER)

    @property
    def uses_language(self) -> bool:
        """Return whether the request text carries the ``{language}`` placeholder."""
        return self._mentions(_LANGUAGE_PLACEHOLDER)

    @property
    def response_field(self) -> str | None:
        """Return the dotted JSON field holding the audio, or ``None`` for a raw body."""
        if self.response == "audio":
            return None
        return self.response[len(_JSON_RESPONSE_PREFIX) :]

    def _mentions(self, placeholder: str) -> bool:
        """Return whether *placeholder* appears in the path, headers or body text.

        :param placeholder: one of the substitution placeholders.
        """
        if placeholder in self.path:
            return True
        if any(placeholder in value for value in self.headers.values()):
            return True
        return any(placeholder in text for text in _body_strings(self.body))


@dataclass(frozen=True, slots=True)
class Preset:
    """One named request shape plus the facts the picker shows about it."""

    id: str
    label: str
    description: str
    shape: ProviderShape
    base_url: str | None
    paid: bool
    cost_per_1k_chars_usd: float | None
    pricing_note: str

    @property
    def requires_base_url(self) -> bool:
        """Return whether the user must supply the server address themselves."""
        return not self.base_url


@dataclass(frozen=True, slots=True)
class PresetSummary:
    """The picker's read-only row for one preset, without its request shape."""

    id: str
    label: str
    description: str
    paid: bool
    format: str
    cost_per_1k_chars_usd: float | None
    pricing_note: str
    requires_base_url: bool


class ProviderPresets:
    """Immutable library of presets loaded from data files."""

    __slots__ = ("_by_id",)

    def __init__(self, presets: Mapping[str, Preset]) -> None:
        """Index *presets* by id, sorted, so every listing is deterministic.

        :param presets: preset id to preset, normally from :func:`load_presets`.
        """
        self._by_id: Mapping[str, Preset] = MappingProxyType(dict(sorted(presets.items())))

    def __getitem__(self, preset_id: str) -> Preset:
        """Return the preset with *preset_id*.

        :param preset_id: preset id, normally one from :meth:`ids`.
        :raises PresetError: when this build has no such preset.
        """
        preset = self._by_id.get(preset_id)
        if preset is None:
            raise PresetError(
                f"unknown TTS preset {preset_id!r} — known presets are {_known_ids(self._by_id)}"
            )
        return preset

    def get(self, preset_id: str) -> Preset | None:
        """Return the preset with *preset_id*, or ``None`` when this build has none.

        :param preset_id: preset id from a stored provider record.
        """
        return self._by_id.get(preset_id)

    def ids(self) -> tuple[str, ...]:
        """Return every preset id, sorted, for a deterministic picker order."""
        return tuple(self._by_id)

    def summaries(self) -> tuple[PresetSummary, ...]:
        """Return the picker rows, in :meth:`ids` order."""
        return tuple(
            PresetSummary(
                id=preset.id,
                label=preset.label,
                description=preset.description,
                paid=preset.paid,
                format=preset.shape.format,
                cost_per_1k_chars_usd=preset.cost_per_1k_chars_usd,
                pricing_note=preset.pricing_note,
                requires_base_url=preset.requires_base_url,
            )
            for preset in self._by_id.values()
        )


def load_presets(directory: Path | None = None) -> ProviderPresets:
    """Load every ``*.json`` preset in *directory* (default: the packaged files).

    The default directory is
    ``importlib.resources.files("chime_deck.tts") / "presets"``, so an
    installed wheel reads the same files a checkout does. Files are read in
    sorted id order, which is the order the picker shows.

    :param directory: alternative directory of ``<id>.json`` files, for tests
        and for a hand-added preset. A directory that does not exist is an
        error: silently loading zero presets would look like a build fault.
    :raises PresetError: when the directory is missing or any preset file is
        unusable.
    """
    presets: dict[str, Preset] = {}
    for preset_id, text in _preset_sources(directory):
        presets[preset_id] = _parse_preset(preset_id, text)
    return ProviderPresets(presets)


def shape_from_overrides(
    base: ProviderShape,
    overrides: Mapping[str, object],
    *,
    where: str,
) -> ProviderShape:
    """Return *base* with any supplied shape field replaced and revalidated.

    Only the keys in :data:`SHAPE_KEYS` are accepted; a caller that also stores
    a ``paid`` flag or a price in its overrides filters those out first. A
    supplied ``headers`` or ``body`` replaces the preset's wholesale — presets
    merge nothing, so one request shape is always one readable object.

    :param base: the preset's shape, the fallback for every field.
    :param overrides: user-supplied shape fields.
    :param where: sentence prefix naming the offender, e.g. ``provider 'Kitchen'``.
    :raises PresetError: when a supplied field is missing, mistyped or unsafe.
    """
    unknown = sorted(set(overrides) - set(SHAPE_KEYS))
    if unknown:
        raise PresetError(
            f"{where} overrides a field a request shape does not have: "
            f"{', '.join(unknown)} — the shape fields are {', '.join(SHAPE_KEYS)}"
        )
    data: dict[str, object] = {
        "method": base.method,
        "path": base.path,
        "headers": dict(base.headers),
        "auth": {"style": base.auth.style, "name": base.auth.name, "prefix": base.auth.prefix},
        "body_kind": base.body_kind,
        "body": base.body,
        "response": base.response,
        "format": base.format,
    }
    data.update(overrides)
    return _shape_from_mapping(data, where=where)


def _preset_sources(directory: Path | None) -> list[tuple[str, str]]:
    """Return ``(preset id, file text)`` pairs, sorted by id.

    :param directory: directory of ``<id>.json`` files, or ``None`` for the
        packaged ``tts/presets`` directory.
    :raises PresetError: when an explicit *directory* does not exist.
    """
    if directory is not None:
        if not directory.is_dir():
            raise PresetError(
                f"the preset directory {directory} does not exist — "
                "point it at a folder of <id>.json files"
            )
        paths = sorted(directory.glob("*.json"), key=lambda path: path.name)
        return [(path.stem, path.read_text(encoding="utf-8")) for path in paths]
    root = importlib.resources.files("chime_deck.tts").joinpath("presets")
    entries = sorted(
        (entry for entry in root.iterdir() if entry.name.endswith(".json")),
        key=lambda entry: entry.name,
    )
    return [(entry.name[: -len(".json")], entry.read_text(encoding="utf-8")) for entry in entries]


def _parse_preset(preset_id: str, text: str) -> Preset:
    """Validate one preset file's text and return the preset.

    :param preset_id: file name without ``.json``, which is the preset id.
    :param text: the file's contents.
    :raises PresetError: when any required field is missing or wrong-typed.
    """
    where = f"preset {preset_id!r}"
    if _PRESET_ID.fullmatch(preset_id) is None:
        raise PresetError(
            f"{where} has an unusable id — a preset id is lower-case letters, "
            "digits and underscores, starting with a letter"
        )
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PresetError(
            f"{where} is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from None
    if not isinstance(raw, Mapping):
        raise PresetError(f"{where} must be a JSON object")
    data = cast(Mapping[str, object], raw)
    return Preset(
        id=preset_id,
        label=_text_field(data, "label", where),
        description=_text_field(data, "description", where),
        shape=_shape_from_mapping(data, where=where),
        base_url=_base_url(data, where),
        paid=_bool_field(data, "paid", where),
        cost_per_1k_chars_usd=_cost_field(data, "cost_per_1k_chars_usd", where),
        pricing_note=_text_field(data, "pricing_note", where),
    )


def _shape_from_mapping(data: Mapping[str, object], *, where: str) -> ProviderShape:
    """Validate the request-shape fields of a preset file or a custom override.

    :param data: mapping carrying the :data:`SHAPE_KEYS` fields.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when a shape field is missing, mistyped or unsafe.
    """
    method = _text_field(data, "method", where).upper()
    if not method.isalpha():
        raise PresetError(f"{where} has a method that is not an HTTP method — use POST or GET")
    path = _text_field(data, "path", where)
    if not path.startswith("/"):
        raise PresetError(
            f"{where} has a path that must start with '/' — it is appended to the server address"
        )
    body_kind = _text_field(data, "body_kind", where)
    if body_kind not in BODY_KINDS:
        raise PresetError(
            f"{where} has a body_kind of {body_kind!r} — it must be one of {', '.join(BODY_KINDS)}"
        )
    audio_format = _text_field(data, "format", where)
    if audio_format not in AUDIO_FORMATS:
        raise PresetError(
            f"{where} has a format of {audio_format!r} — "
            f"this build plays {', '.join(AUDIO_FORMATS)} only"
        )
    return ProviderShape(
        method=method,
        path=path,
        headers=_headers(data, where),
        auth=_auth(data, where),
        body_kind=body_kind,
        body=_body(data, body_kind, where),
        response=_response(data, where),
        format=audio_format,
    )


def _text_field(data: Mapping[str, object], key: str, where: str) -> str:
    """Return a required, non-empty, trimmed string field.

    :param data: mapping carrying the field.
    :param key: field name.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the field is absent, blank or not a string.
    """
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PresetError(f"{where} needs a non-empty {key}")
    return value.strip()


def _optional_string(data: Mapping[str, object], key: str, where: str) -> str:
    """Return a trimmed optional string field, ``""`` when absent.

    :param data: mapping carrying the field.
    :param key: field name.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the field is present but not a string.
    """
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PresetError(f"{where} has a {key} that must be a string")
    return value.strip()


def _bool_field(data: Mapping[str, object], key: str, where: str) -> bool:
    """Return a required boolean field.

    :param data: mapping carrying the field.
    :param key: field name.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the field is absent or not a boolean.
    """
    value = data.get(key)
    if not isinstance(value, bool):
        raise PresetError(f"{where} needs {key} to be true or false")
    return value


def _cost_field(data: Mapping[str, object], key: str, where: str) -> float | None:
    """Return a required price per 1000 characters, ``None`` meaning unknown.

    :param data: mapping carrying the field.
    :param key: field name.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the field is absent, a boolean, negative or not finite.
    """
    if key not in data:
        raise PresetError(f"{where} needs {key} — use null when no price is known")
    value = data[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetError(
            f"{where} has a {key} that must be null or a number of dollars per 1000 characters"
        )
    price = float(value)
    if not math.isfinite(price) or price < 0:
        raise PresetError(
            f"{where} has a {key} that must be null or a finite number of dollars "
            "per 1000 characters, 0 or more"
        )
    return price


def _base_url(data: Mapping[str, object], where: str) -> str | None:
    """Return the optional default server address, ``None`` when the user must supply one.

    :param data: mapping carrying the field.
    :param where: sentence prefix naming the offending preset.
    :raises PresetError: when the field is not null and not an http(s) URL.
    """
    value = data.get("base_url")
    if value is None:
        return None
    if not isinstance(value, str):
        raise PresetError(f"{where} has a base_url that must be a string or null")
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_BASE_URL_CHARS or _URL_PATTERN.fullmatch(value) is None:
        raise PresetError(
            f"{where} has a base_url that must be an http(s) address — "
            "it starts with http:// or https:// and contains no spaces"
        )
    return value


def _headers(data: Mapping[str, object], where: str) -> Mapping[str, str]:
    """Return the request headers, an empty mapping when the file has none.

    :param data: mapping carrying the field.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the field is not an object of string values.
    """
    value = data.get("headers")
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise PresetError(f"{where} has headers that must be a JSON object of strings")
    headers: dict[str, str] = {}
    for name, header_value in cast(Mapping[object, object], value).items():
        if not isinstance(name, str) or not name.strip():
            raise PresetError(f"{where} has a header name that must be a non-empty string")
        if not isinstance(header_value, str):
            raise PresetError(
                f"{where} has a header value that must be a string — header {name.strip()!r} is not"
            )
        headers[name.strip()] = header_value
    return MappingProxyType(headers)


def _auth(data: Mapping[str, object], where: str) -> AuthStyle:
    """Return the validated authentication style.

    :param data: mapping carrying the field.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the style is missing, unknown or unsafe, or a
        named header style has no header name.
    """
    value = data.get("auth")
    if not isinstance(value, Mapping):
        raise PresetError(
            f"{where} needs an auth object — {{'style': 'bearer'}}, a named header, or 'none'"
        )
    auth = cast(Mapping[str, object], value)
    style = auth.get("style")
    if not isinstance(style, str) or not style:
        raise PresetError(f"{where} needs an auth style — one of {', '.join(AUTH_STYLES)}")
    if style == "query":
        raise PresetError(
            f"{where} asks for query-string auth, which this build does not support — "
            "a key in a URL would be printed by httpx's INFO request log, "
            "so use a bearer token or a named header"
        )
    if style not in AUTH_STYLES:
        raise PresetError(
            f"{where} has an auth style of {style!r} — it must be one of {', '.join(AUTH_STYLES)}"
        )
    name = _optional_string(auth, "name", where)
    prefix = _optional_string(auth, "prefix", where)
    if style == "header" and not name:
        raise PresetError(
            f"{where} uses header auth without a header name — "
            "set auth.name to the header the key travels in"
        )
    return AuthStyle(style=style, name=name, prefix=prefix)


def _body(data: Mapping[str, object], body_kind: str, where: str) -> object:
    """Return the validated request body for *body_kind*.

    :param data: mapping carrying the field.
    :param body_kind: ``json`` or ``text``, already validated.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the body is missing or the wrong type for its kind.
    """
    if "body" not in data:
        raise PresetError(f"{where} needs a body")
    body = data["body"]
    if body_kind == "text":
        if not isinstance(body, str) or not body.strip():
            raise PresetError(
                f"{where} has a body_kind of 'text', so body must be a non-empty string"
            )
        return body
    if not isinstance(body, dict):
        raise PresetError(f"{where} has a body_kind of 'json', so body must be a JSON object")
    return body


def _response(data: Mapping[str, object], where: str) -> str:
    """Return the validated response locator, ``audio`` or ``json:<dotted path>``.

    :param data: mapping carrying the field.
    :param where: sentence prefix naming the offending preset or provider.
    :raises PresetError: when the locator is neither form or names no field.
    """
    value = _text_field(data, "response", where)
    if value == "audio":
        return value
    if not value.startswith(_JSON_RESPONSE_PREFIX):
        raise PresetError(
            f"{where} has a response of {value!r} — it must be 'audio' "
            "or 'json:<field>', a dotted path into the JSON reply"
        )
    field = value[len(_JSON_RESPONSE_PREFIX) :]
    if not field or any(not part for part in field.split(".")):
        raise PresetError(
            f"{where} has a response of {value!r} — the json: part must name "
            "a non-empty field such as json:audioContent"
        )
    return value


def _body_strings(value: object) -> Iterator[str]:
    """Yield every string leaf of a request body.

    :param value: JSON body value: a string, an object or an array.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in cast(Mapping[object, object], value).values():
            yield from _body_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in cast(list[object], value):
            yield from _body_strings(item)


def _known_ids(presets: Mapping[str, Preset]) -> str:
    """Return the known preset ids as one comma-separated sentence fragment.

    :param presets: preset id to preset.
    """
    return ", ".join(sorted(presets)) or "none"


DEFAULT_PRESETS: Final[ProviderPresets] = load_presets()
"""The presets shipped in ``tts/presets``, loaded once at import like ``SETTINGS``."""
