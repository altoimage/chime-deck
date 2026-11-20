"""Canonical settings registry: keys, defaults, types and value constraints.

This module is the single source of truth for every setting the application
knows about. The HTTP settings API (HSD-91z1) reads it to render defaults and
validate writes; the store reads the same table to validate writes here.

**Import-cycle rule:** no module under ``chime_deck/store/`` may import
:mod:`chime_deck.config`. ``config`` imports ``store``, so the reverse
direction raises :class:`ImportError` under the production import order
(``cli -> app -> config -> store -> settings_registry``) even though pytest can
stay green by importing ``store`` first. That is why the ``system.log_level``
choices live here instead of being re-used from ``config.LOG_LEVELS``.

Owning ticket: HSD-2sem; constraints added by HSD-91z1 and the ``remote_tts`` and
renderer settings by HSD-5de8. ``device.name`` was added by HSD-vwdy, which also
makes the first write of it pin ``mqtt.instance_id`` (see
:func:`chime_deck.store.derive_instance_id`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

from .errors import SettingTypeError, SettingValueError, UnknownSettingKey

__all__ = [
    "AUDIO_BACKEND_CHOICES",
    "LOCAL_ONLY_SETTING_KEYS",
    "LOG_LEVEL_CHOICES",
    "PROVIDER_SECRET_KEY_PREFIX",
    "PROVIDER_SECRET_KEY_SUFFIX",
    "PROVIDER_SECRET_MAX_LENGTH",
    "SECRET_SETTING_KEYS",
    "SETTINGS",
    "TTS_BAKE_SETTING_KEYS",
    "SettingSpec",
    "SettingType",
    "is_secret_key",
    "normalise_value",
    "provider_id_from_secret_key",
    "provider_secret_key",
    "validate_value",
]

LOG_LEVEL_CHOICES: Final = ("debug", "info", "warning", "error", "critical")
"""Stored values for ``system.log_level``; mirrors ``config.LOG_LEVELS`` in lower case."""

AUDIO_BACKEND_CHOICES: Final = ("mpv",)
"""Stored values for ``audio.backend``; mpv is the only implemented backend.

``"sounddevice"`` was offered in the settings UI but no such backend ever
shipped, so the registry no longer accepts or writes it. A database that still
contains the old value (an older build or a hand edit) is repaired at startup
by :class:`chime_deck.wiring.ChimePipeline`, which writes ``"mpv"`` back
before any backend is built.
"""

PROVIDER_SECRET_KEY_PREFIX: Final = "tts.providers."
"""Start of the key holding one external provider's write-only API key (D25)."""

PROVIDER_SECRET_KEY_SUFFIX: Final = ".api_key"
"""End of that same key."""

PROVIDER_SECRET_MAX_LENGTH: Final = 4096
"""Longest accepted provider API key; mirrors the other secret tokens' cap."""

_PROVIDER_ID_PATTERN: Final = r"[0-9a-f]{12}"
"""A provider id is exactly 12 lower-case hex characters and nothing else."""

_THEME_CHOICES: Final = ("dark", "light", "auto")
# http_tts joined this tuple with its own registry keys (D25, HSD-s856), so a
# provider list and the provider it speaks through are ordinary settings.
# "none" means speech is switched off (D27): a supported state, not an
# unavailable engine, so a press reports tts_disabled rather than tts_unavailable.
_TTS_ENGINE_CHOICES: Final = ("ha_tts", "piper", "remote_tts", "espeak_ng", "http_tts", "none")

_MAX_DEVICE_NAME_LENGTH: Final = 64
"""Longest accepted ``device.name``.

Tied to ``mqtt/bridge.py``'s ``_MAX_INSTANCE_ID_LENGTH``: the instance id a first
name write derives is the slug of that name (:func:`chime_deck.store.derive_instance_id`),
and a slug is never longer than the name it came from, so a name accepted here
can only ever derive a legal instance id.
"""

_DEVICE_NAME_PATTERN: Final = r".*[0-9A-Za-z].*"
"""A device name needs at least one ASCII alphanumeric character.

Fullmatch-safe (``validate_value`` uses ``fullmatch``) and deliberately not
anchored: it is a *contains* rule, not a grammar. A name with nothing to slug
would make :func:`chime_deck.store.derive_slug` raise, so the registry refuses it
before the pin in :func:`chime_deck.api.settings.patch_settings` can run.
"""

_DEVICE_NAME_HINT: Final = (
    "must contain at least one letter or digit — this device's MQTT instance id is derived from it"
)

_QUIET_HOURS_PATTERN: Final = r"^([01]\d|2[0-3]):[0-5]\d$"
_LANGUAGE_PATTERN: Final = r"^[a-z]{2,3}([_-][A-Za-z]{2,4})?$"
_ACCENT_COLOR_PATTERN: Final = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
_BASE_URL_PATTERN: Final = r"^$|^https?://\S+$"

_QUIET_HOURS_HINT: Final = "must be a 24-hour time such as 22:00"
_LANGUAGE_HINT: Final = "must be a language tag such as en_GB or en-GB"
_ACCENT_COLOR_HINT: Final = "must be a hex colour such as #4f9cf9"
_BASE_URL_HINT: Final = "must be empty or an http(s) URL"


class SettingType(StrEnum):
    """The JSON-compatible type a setting value must have."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One registered setting, its default value and its value constraints.

    All constraint fields are optional and default to "unconstrained", so a
    spec that only needs a type check stays a single line. Constraints are
    checked after the type check, in the order range, choices, length, pattern.

    ``secret`` marks keys whose value must never be returned to the browser and
    must never appear in an error message.

    ``hint`` is the human sentence reported when ``pattern`` does not match, so
    the user is told the accepted format rather than a regular expression.

    ``normaliser`` converts a value to its canonical stored form (for example
    lower-casing a log level) before validation runs.

    A ``JSON``-typed default is a container, so it is copied before it is
    returned: a caller can never mutate the registry's own default.
    """

    key: str
    default: str | int | bool | list[object] | dict[str, object]
    type: SettingType
    secret: bool = False
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    max_length: int | None = None
    pattern: str | None = None
    hint: str = ""
    normaliser: Callable[[object], object] | None = None


def _normalise_log_level(value: object) -> object:
    """Lower-case a log level so the stored value is canonical.

    :param value: value about to be written.
    """
    return value.strip().lower() if isinstance(value, str) else value


def _normalise_device_name(value: object) -> object:
    """Strip surrounding whitespace from a device name.

    Normalising before validation is what makes the length check see the name the
    user meant: 64 characters with surrounding spaces must be accepted.

    :param value: value about to be written.
    """
    return value.strip() if isinstance(value, str) else value


_SPECS: Final = (
    SettingSpec(
        "device.name",
        "ChimeDeck",
        SettingType.STRING,
        max_length=_MAX_DEVICE_NAME_LENGTH,
        pattern=_DEVICE_NAME_PATTERN,
        hint=_DEVICE_NAME_HINT,
        normaliser=_normalise_device_name,
    ),
    SettingSpec(
        "ha.base_url",
        "http://homeassistant.local:8123",
        SettingType.STRING,
        max_length=2048,  # a URL longer than this is never a usable HA address
        pattern=_BASE_URL_PATTERN,
        hint=_BASE_URL_HINT,
    ),
    # HA long-lived tokens are ~180-260 characters and JWT-shaped.
    SettingSpec("ha.token", "", SettingType.STRING, secret=True, max_length=4096),
    SettingSpec("mqtt.host", "core-mosquitto.local", SettingType.STRING, max_length=255),
    # 0 and 65536 are not usable broker ports.
    SettingSpec("mqtt.port", 1883, SettingType.INTEGER, minimum=1, maximum=65535),
    SettingSpec("mqtt.username", "", SettingType.STRING, max_length=255),
    SettingSpec("mqtt.password", "", SettingType.STRING, secret=True, max_length=1024),
    SettingSpec("mqtt.tls", False, SettingType.BOOLEAN),
    SettingSpec("mqtt.discovery_prefix", "homeassistant", SettingType.STRING, max_length=255),
    SettingSpec("mqtt.instance_id", "default", SettingType.STRING, max_length=64),
    SettingSpec("server.host", "127.0.0.1", SettingType.STRING, max_length=255),
    # Matches config.parse_port; 0 means "let the OS pick a free port".
    SettingSpec("server.port", 8080, SettingType.INTEGER, minimum=0, maximum=65535),
    # An empty token means "no authentication": that is the default and the
    # documented deployment. A non-empty token gates every state-bearing request
    # (chime_deck.auth), so one PATCH turns the gate on with no restart.
    SettingSpec("server.token", "", SettingType.STRING, secret=True, max_length=1024),
    # The configuration-interface password (HSD-n6wi), stored as a PHC-style scrypt
    # hash (chime_deck.admin), never as the plaintext. Empty means the admin
    # listener has no password. Written only by /api/auth/password, which is why
    # PATCH /api/settings refuses the key: the generic write path would bypass the
    # current-password rule. 200 characters covers a 16-byte salt and a 32-byte
    # key in base64 with room to spare.
    SettingSpec("server.admin_password_hash", "", SettingType.STRING, secret=True, max_length=200),
    SettingSpec("audio.device_hint", "", SettingType.STRING, max_length=255),
    SettingSpec("audio.backend", "mpv", SettingType.STRING, choices=AUDIO_BACKEND_CHOICES),
    SettingSpec("audio.volume", 70, SettingType.INTEGER, minimum=0, maximum=100),
    # "Silence this device" (D28) is a state of its own, not a zero gain: mute
    # suppresses every press, quiet_hours_exempt ones included.
    SettingSpec("audio.muted", False, SettingType.BOOLEAN),
    # HDMI and SPDIF outputs take about a second to become audible after they
    # sleep, so the first chime after an idle period starts with this much
    # silence; 0 disables it. Consumed by the playback worker
    # (chime_deck.playback), not by the UI.
    SettingSpec("audio.wake_lead_in_ms", 1500, SettingType.INTEGER, minimum=0, maximum=10000),
    # Starting the player dlopens libmpv and initialises mpv; the reference
    # panel measured 5.1 s for it on a clean boot, so 10 s is the floor that
    # keeps a start from being guaranteed to fail there and 25 s leaves room
    # for a cold page cache. Read once per start attempt by the pipeline
    # (chime_deck.wiring), never by the UI.
    SettingSpec("audio.start_timeout_s", 25, SettingType.INTEGER, minimum=10, maximum=120),
    SettingSpec("audio.quiet_hours_enabled", False, SettingType.BOOLEAN),
    SettingSpec(
        "audio.quiet_hours_start",
        "22:00",
        SettingType.STRING,
        pattern=_QUIET_HOURS_PATTERN,
        hint=_QUIET_HOURS_HINT,
    ),
    SettingSpec(
        "audio.quiet_hours_end",
        "07:00",
        SettingType.STRING,
        pattern=_QUIET_HOURS_PATTERN,
        hint=_QUIET_HOURS_HINT,
    ),
    SettingSpec("audio.quiet_hours_volume", 0, SettingType.INTEGER, minimum=0, maximum=100),
    # Present in 03-audio-engine.md §8; the only setting not listed in
    # 01-data-model.md §2.
    SettingSpec("audio.self_test_on_start", False, SettingType.BOOLEAN),
    # One hour is a sanity cap: dedupe windows are measured in seconds.
    SettingSpec("buttons.default_dedupe_seconds", 3, SettingType.INTEGER, minimum=0, maximum=3600),
    SettingSpec("tts.default_engine", "ha_tts", SettingType.STRING, choices=_TTS_ENGINE_CHOICES),
    SettingSpec("tts.ha.engine_id", "tts.piper", SettingType.STRING, max_length=255),
    SettingSpec(
        "tts.ha.language",
        "en_GB",
        SettingType.STRING,
        pattern=_LANGUAGE_PATTERN,
        hint=_LANGUAGE_HINT,
    ),
    SettingSpec("tts.piper.voice", "en_GB-alan-low", SettingType.STRING, max_length=255),
    # D22: local Piper needs ~2 GB+ of RAM; below this the engine reports
    # unavailable (filtered, not merely warned about) and the voice download is
    # refused. A setting with an override so a user who knows better can lower it.
    # Deliberately NOT in TTS_BAKE_SETTING_KEYS: it changes no rendered byte, so
    # toggling it must not invalidate every baked phrase.
    SettingSpec("tts.piper.min_ram_mb", 2048, SettingType.INTEGER, minimum=128, maximum=131072),
    SettingSpec("tts.espeak_voice", "en-gb", SettingType.STRING, max_length=64),
    # espeak-ng accepts roughly 80-450 words per minute.
    SettingSpec("tts.espeak_speed", 160, SettingType.INTEGER, minimum=80, maximum=450),
    # The empty default is deliberate: an unconfigured renderer must answer
    # "unavailable" without opening a socket.
    SettingSpec(
        "tts.remote_tts.url",
        "",
        SettingType.STRING,
        max_length=2048,
        pattern=_BASE_URL_PATTERN,
        hint=_BASE_URL_HINT,
    ),
    # The token the renderer's tts.renderer.token must match.
    SettingSpec("tts.remote_tts.token", "", SettingType.STRING, secret=True, max_length=4096),
    # 20 is design 04 §6.5's transport ceiling; minimum=1 because a 0 would make
    # every render an instant timeout.
    SettingSpec("tts.remote_tts.timeout_s", 20, SettingType.INTEGER, minimum=1, maximum=120),
    # The renderer role refuses to start while this is empty, so an empty default
    # cannot silently expose the endpoint.
    SettingSpec("tts.renderer.token", "", SettingType.STRING, secret=True, max_length=4096),
    # HSD-s856 supplies the three settings below (D25).
    # D25: the external HTTP TTS providers, one JSON array of records that never
    # contains an API key — each key is its own tts.providers.<id>.api_key row
    # (see provider_secret_key), so this array is safe to return and to export.
    # Written only through /api/tts/providers; PATCH /api/settings refuses it.
    SettingSpec("tts.providers", [], SettingType.JSON),
    # D25: false means a templated phrase on a paid provider needs the per-request
    # acknowledge_paid_template override at save time; true is the operator saying
    # "templated paid phrases are fine here".
    SettingSpec("tts.allow_dynamic_paid", False, SettingType.BOOLEAN),
    # The provider the http_tts engine speaks through. Empty = none selected, which
    # is an honest "unavailable", never an optimistic default (D25).
    SettingSpec("tts.http_tts.provider", "", SettingType.STRING, max_length=64),
    SettingSpec("ui.theme", "dark", SettingType.STRING, choices=_THEME_CHOICES),
    SettingSpec(
        "ui.accent_color",
        "#4f9cf9",
        SettingType.STRING,
        pattern=_ACCENT_COLOR_PATTERN,
        hint=_ACCENT_COLOR_HINT,
    ),
    # 0 means "auto"; a wall panel shows at most a handful of columns.
    SettingSpec("ui.dashboard_columns", 0, SettingType.INTEGER, minimum=0, maximum=6),
    SettingSpec("ui.hide_cursor", True, SettingType.BOOLEAN),
    SettingSpec("ui.show_clock", True, SettingType.BOOLEAN),
    # D28's right-hand rail: an install whose panel has no speaker can turn the
    # volume steps off. The mute button and the Settings link are part of the
    # shell and stay visible (D28: the gear is always visible, in kiosk too).
    SettingSpec("ui.show_control_rail", True, SettingType.BOOLEAN),
    # The first-run wizard's "I'll do it later" (HSD-vl7t). Install state, not a
    # browser flag: an install deliberately left half-configured must reach the
    # dashboard from every device, not only the one it was skipped on.
    SettingSpec("ui.setup_dismissed", False, SettingType.BOOLEAN),
    SettingSpec(
        "system.log_level",
        "info",
        SettingType.STRING,
        choices=LOG_LEVEL_CHOICES,
        normaliser=_normalise_log_level,
    ),
)

SETTINGS: Final[Mapping[str, SettingSpec]] = MappingProxyType({spec.key: spec for spec in _SPECS})
"""Read-only mapping of setting key to specification."""

_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = MappingProxyType(
    {spec.key: re.compile(spec.pattern) for spec in _SPECS if spec.pattern is not None}
)
"""Compiled form of every :attr:`SettingSpec.pattern`, built once at import."""

SECRET_SETTING_KEYS: Final[frozenset[str]] = frozenset(spec.key for spec in _SPECS if spec.secret)
"""Keys whose values must never be returned to the browser."""

LOCAL_ONLY_SETTING_KEYS: Final = frozenset({"server.admin_password_hash"})
"""Keys a configuration export never carries and an import never accepts.

The configuration-interface password hash is written only by ``/api/auth/password``
(:mod:`chime_deck.admin`), never exported and never imported: the hash *is* the
credential — it signs the admin cookie, so a copy of it is a copy of the login —
and rotating or clearing the password must need the current password, which a
document cannot prove. Export excludes these keys even with ``include_secrets``;
import refuses a document that names one instead of silently skipping it, so a
hand-edited file cannot look like it rotated the credential.
"""


def provider_secret_key(provider_id: str) -> str:
    """Return the settings key holding one provider's write-only API key.

    :param provider_id: 12-character hexadecimal provider id.
    """
    return f"{PROVIDER_SECRET_KEY_PREFIX}{provider_id}{PROVIDER_SECRET_KEY_SUFFIX}"


def provider_id_from_secret_key(key: str) -> str | None:
    """Return the provider id a secret key names, or ``None`` when it is not one.

    The middle has to be exactly 12 lower-case hex characters, so a near miss
    such as ``tts.providers.abc.api_key`` is not a provider key.

    :param key: candidate settings key.
    """
    if not key.startswith(PROVIDER_SECRET_KEY_PREFIX):
        return None
    if not key.endswith(PROVIDER_SECRET_KEY_SUFFIX):
        return None
    middle = key[len(PROVIDER_SECRET_KEY_PREFIX) : len(key) - len(PROVIDER_SECRET_KEY_SUFFIX)]
    if re.fullmatch(_PROVIDER_ID_PATTERN, middle) is None:
        return None
    return middle


def is_secret_key(key: str) -> bool:
    """Return whether *key* holds a secret value.

    True for the registered secret keys and for a provider API key row, which
    is deliberately NOT a registered setting: it is written and read only
    through ``ConfigStore``'s dedicated provider-secret methods, so the generic
    settings PATCH can never read or write one.

    :param key: settings key to classify.
    """
    return key in SECRET_SETTING_KEYS or provider_id_from_secret_key(key) is not None


TTS_BAKE_SETTING_KEYS: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        "tts.default_engine": None,
        "tts.ha.engine_id": "ha_tts",
        "tts.ha.language": "ha_tts",
        "tts.piper.voice": "piper",
        "tts.espeak_voice": "espeak_ng",
        "tts.espeak_speed": "espeak_ng",
        # A repointed URL can render different audio and the cache key carries no
        # URL, so a change must invalidate remote bakes. tts.remote_tts.token,
        # tts.remote_tts.timeout_s and tts.renderer.token are deliberately NOT
        # here: they change no rendered byte.
        "tts.remote_tts.url": "remote_tts",
        "tts.providers": "http_tts",  # a provider edit changes rendered bytes
        "tts.http_tts.provider": "http_tts",  # a different provider is different audio
        # tts.allow_dynamic_paid is deliberately NOT here: it gates a confirmation
        # dialog and changes no rendered byte. The provider API keys are not here
        # either: a key rotates credentials, not audio.
    }
)
"""TTS settings that invalidate baked speech, mapped to the engine they affect.

A value of ``None`` means the setting affects every TTS engine: changing
``tts.default_engine`` can change the effective engine of any button.

Mapped settings are the ones that change a rendered byte: ``remote_tts``'s URL
does, because the cache key carries no URL, while its token, its timeout and the
renderer's own token do not, so changing any of those must not invalidate every
baked phrase.
"""


def _check_integer(spec: SettingSpec, value: int) -> None:
    """Check an integer setting against its range.

    :param spec: specification of the setting being validated.
    :param value: decoded Python value already known to be an int.
    :raises SettingValueError: if the value is outside the allowed range.
    """
    minimum = spec.minimum
    if minimum is not None and value < minimum:
        raise SettingValueError(spec.key, f"must be at least {minimum}, got {value}")
    maximum = spec.maximum
    if maximum is not None and value > maximum:
        raise SettingValueError(spec.key, f"must be at most {maximum}, got {value}")


def _check_string(spec: SettingSpec, value: str) -> None:
    """Check a string setting against its choices, length and pattern.

    Secret values are never interpolated into the message, so a rejected
    ``ha.token`` cannot leak into a log line or an HTTP response.

    :param spec: specification of the setting being validated.
    :param value: decoded Python value already known to be a str.
    :raises SettingValueError: if the value breaks a per-key constraint.
    """
    if spec.choices and value not in spec.choices:
        reason = f"must be one of {', '.join(spec.choices)}"
        if not spec.secret:
            reason = f"{reason}; got {value!r}"
        raise SettingValueError(spec.key, reason)

    if spec.max_length is not None and len(value) > spec.max_length:
        raise SettingValueError(spec.key, f"must be at most {spec.max_length} characters")

    pattern = _PATTERNS.get(spec.key)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise SettingValueError(spec.key, spec.hint or "has an invalid format")


def validate_value(key: str, value: object) -> None:
    """Validate that a setting key exists and the value is allowed for that key.

    The registered type is checked first, then the per-key constraints in the
    order range, choices, length, pattern. Messages for secret keys never
    include the submitted value.

    :attr:`SettingType.JSON` is a *container* check only — a list or an object,
    nothing deeper. Record-level schema validation lives above the store, in the
    HTTP provider routes, because the store may not import
    :mod:`chime_deck.tts` (see the import-cycle rule at the top).

    :param key: setting key to validate.
    :param value: decoded Python value about to be written.
    :raises UnknownSettingKey: if the key is not registered.
    :raises SettingTypeError: if the value's Python type does not match.
    :raises SettingValueError: if the value breaks a per-key constraint.
    """
    spec = SETTINGS.get(key)
    if spec is None:
        raise UnknownSettingKey(key)

    if spec.type is SettingType.BOOLEAN:
        valid = isinstance(value, bool)
    elif spec.type is SettingType.INTEGER:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif spec.type is SettingType.JSON:
        valid = isinstance(value, (list, dict))
    else:
        valid = isinstance(value, str)

    if not valid:
        raise SettingTypeError(key, spec.type.value)

    if spec.type is SettingType.INTEGER:
        _check_integer(spec, cast("int", value))
    elif spec.type is SettingType.STRING:
        _check_string(spec, cast("str", value))


def normalise_value(key: str, value: object) -> object:
    """Normalise a setting value to its canonical form and validate it.

    *Any* failure of a registered normaliser is reported as
    :class:`SettingValueError`, because a normaliser bug must never surface as a
    500: the value came from the user, so the answer is a 422 naming the key.

    :param key: setting key.
    :param value: decoded Python value about to be written.
    :returns: the value as it should be stored.
    :raises UnknownSettingKey: if the key is not registered.
    :raises SettingTypeError: if the value's Python type does not match.
    :raises SettingValueError: if the value breaks a per-key constraint, or if the
        registered normaliser rejects it or fails at all.
    """
    spec = SETTINGS.get(key)
    if spec is None:
        raise UnknownSettingKey(key)

    normalised = value
    if spec.normaliser is not None:
        try:
            normalised = spec.normaliser(value)
        except SettingValueError:
            raise
        except Exception as exc:
            raise SettingValueError(key, spec.hint or "is not valid") from exc

    validate_value(key, normalised)
    return normalised
