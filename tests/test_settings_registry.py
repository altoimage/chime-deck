"""Settings registry: constraints, normalisation and store integration.

The registry is the single source of truth for every setting's type and value
constraints, so these tests pin the value rules directly (out of range, outside
the choices, wrong shape, too long, unknown key) and then check that the two
write paths (``ConfigStore.set_setting`` and ``import_config``) go through the
same normalisation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from chime_deck.config import LOG_LEVELS
from chime_deck.store import (
    SECRET_SETTING_KEYS,
    SETTINGS,
    ConfigStore,
    SettingSpec,
    SettingType,
    SettingTypeError,
    SettingValueError,
    UnknownSettingKey,
    import_config,
    normalise_value,
    validate_value,
)
from chime_deck.store.settings_registry import (
    LOG_LEVEL_CHOICES,
    is_secret_key,
    provider_id_from_secret_key,
    provider_secret_key,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


_EMPTY_PATTERN_SAMPLES: dict[str, str] = {"tts.remote_tts.url": "http://bigpc.local:8090"}
"""A real sample for every non-secret string setting that defaults to empty and carries a pattern.

Such a setting cannot be sampled with ``"sample"``: the pattern rejects it. Every
new one must be added here, and :func:`test_every_empty_pattern_string_has_a_sample`
refuses to let the table and the registry drift apart.
"""


def _representative_value(key: str, spec: SettingSpec) -> object:
    """Return a plausible non-empty value that *spec* is expected to accept.

    :param key: registry key, used to look the sample up.
    :param spec: specification to sample.
    """
    if spec.choices:
        return spec.choices[0]
    if spec.type is SettingType.STRING:
        if spec.default != "":
            return spec.default
        if spec.pattern is not None:
            return _EMPTY_PATTERN_SAMPLES[key]
        return "sample"
    return spec.default


def test_log_level_choices_mirror_config() -> None:
    """The registry's local choices cannot drift from ``config.LOG_LEVELS``.

    The registry may not import :mod:`chime_deck.config` (import-cycle rule),
    so the level list is duplicated and pinned here.
    """
    assert set(LOG_LEVEL_CHOICES) == {level.lower() for level in LOG_LEVELS}


def test_secret_keys_are_derived_from_the_specs() -> None:
    """Secret-ness is read off the specs, and every secret starts empty.

    The loop keeps its original intent: every non-secret setting must accept a
    real value, so a spec constrained to the empty string alone cannot hide a
    secret that the API would then expose. ``server.token`` joined the set with
    the optional shared-token gate (HSD-0jke), and ``server.admin_password_hash``
    with the configuration-interface password (HSD-n6wi).
    """
    assert (
        frozenset(
            {
                "ha.token",
                "mqtt.password",
                "server.admin_password_hash",
                "server.token",
                "tts.remote_tts.token",
                "tts.renderer.token",
            }
        )
        == SECRET_SETTING_KEYS
    )
    for key, spec in SETTINGS.items():
        if spec.secret:
            assert spec.default == "", key
            continue
        # A plain setting constrained to the empty string alone would be a
        # secret in disguise, so every non-secret spec accepts a real value.
        representative = _representative_value(key, spec)
        assert representative != "", key
        validate_value(key, representative)


def test_every_empty_pattern_string_has_a_sample() -> None:
    """Every non-secret string setting that defaults to empty and has a pattern has a sample.

    ``_representative_value`` cannot invent a value that such a pattern accepts,
    so a new one must be given a real sample instead of silently failing the loop
    above. ``tts.remote_tts.url`` keeps its ``""`` default on purpose: an
    unconfigured renderer must answer unavailable with no socket opened.
    """
    empty_pattern_keys = {
        key
        for key, spec in SETTINGS.items()
        if not spec.secret
        and spec.type is SettingType.STRING
        and spec.default == ""
        and spec.pattern is not None
    }

    assert set(_EMPTY_PATTERN_SAMPLES) == empty_pattern_keys


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("audio.volume", -1, "must be at least 0, got -1"),
        ("audio.volume", 101, "must be at most 100, got 101"),
        ("audio.hardware_volume", -1, "must be at least 0, got -1"),
        ("audio.hardware_volume", 101, "must be at most 100, got 101"),
        ("audio.wake_lead_in_ms", -1, "must be at least 0, got -1"),
        ("audio.wake_lead_in_ms", 10001, "must be at most 10000, got 10001"),
        ("mqtt.port", 0, "must be at least 1, got 0"),
        ("mqtt.port", 65536, "must be at most 65535, got 65536"),
        ("server.port", -1, "must be at least 0, got -1"),
        ("ui.dashboard_columns", 7, "must be at most 6, got 7"),
        ("ui.scale", 84, "must be at least 85, got 84"),
        ("ui.scale", 151, "must be at most 150, got 151"),
        ("tts.espeak_speed", 79, "must be at least 80, got 79"),
        ("tts.remote_tts.timeout_s", 0, "must be at least 1, got 0"),
        ("tts.remote_tts.timeout_s", 121, "must be at most 120, got 121"),
        ("buttons.default_dedupe_seconds", 3601, "must be at most 3600, got 3601"),
        ("audio.quiet_hours_volume", 101, "must be at most 100, got 101"),
        ("audio.start_timeout_s", 9, "must be at least 10, got 9"),
        ("audio.start_timeout_s", 121, "must be at most 120, got 121"),
    ],
)
def test_integer_out_of_range_is_rejected(key: str, value: int, reason: str) -> None:
    """A value outside the documented bound is rejected, naming the key and the bound.

    Each bound is reported on its own, so the message names the rule that was
    actually broken rather than the whole range.

    :param key: setting under test.
    :param value: out-of-range integer.
    :param reason: expected reason text naming the broken bound and the value.
    """
    with pytest.raises(SettingValueError) as exc_info:
        validate_value(key, value)

    assert exc_info.value.key == key
    assert exc_info.value.reason == reason
    assert str(exc_info.value) == f"setting {key!r} {reason}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("audio.volume", 0),
        ("audio.volume", 100),
        ("audio.hardware_volume", 0),
        ("audio.hardware_volume", 100),
        ("audio.wake_lead_in_ms", 0),
        ("audio.wake_lead_in_ms", 10000),
        ("mqtt.port", 1),
        ("mqtt.port", 65535),
        ("server.port", 0),
        ("server.port", 65535),
        ("ui.dashboard_columns", 0),
        ("ui.dashboard_columns", 6),
        ("ui.scale", 85),
        ("ui.scale", 150),
        ("tts.espeak_speed", 80),
        ("tts.espeak_speed", 450),
        ("tts.remote_tts.timeout_s", 1),
        ("tts.remote_tts.timeout_s", 120),
        ("buttons.default_dedupe_seconds", 0),
        ("buttons.default_dedupe_seconds", 3600),
        ("audio.quiet_hours_volume", 0),
        ("audio.quiet_hours_volume", 100),
        ("audio.start_timeout_s", 10),
        ("audio.start_timeout_s", 120),
    ],
)
def test_integer_boundaries_are_accepted(key: str, value: int) -> None:
    """Both documented limits are inclusive.

    :param key: setting under test.
    :param value: boundary value.
    """
    validate_value(key, value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ui.theme", "purple"),
        ("audio.backend", "alsa"),
        ("audio.backend", "sounddevice"),
        ("audio.rail_target", "both"),
    ],
)
def test_choices_are_enforced(key: str, value: str) -> None:
    """A value outside the registered choices is rejected with the rule in the message.

    :param key: setting under test.
    :param value: value outside the closed set.
    """
    with pytest.raises(SettingValueError) as exc_info:
        validate_value(key, value)

    assert exc_info.value.key == key
    assert "must be one of" in str(exc_info.value)


def test_the_scale_steps_are_the_only_accepted_sizes() -> None:
    """``ui.scale`` accepts the five offered steps and no other percent.

    The UI previews 85, 100, 115, 130 and 150 only, so a size between two steps
    must not reach the store: the reject message names the steps (HSD-nlbf).
    """
    steps = (85, 100, 115, 130, 150)
    for step in steps:
        validate_value("ui.scale", step)

    for non_step in (87, 145):
        with pytest.raises(SettingValueError) as exc_info:
            validate_value("ui.scale", non_step)

        assert exc_info.value.key == "ui.scale"
        assert "must be one of 85, 100, 115, 130, 150" in exc_info.value.reason


def test_the_scale_steps_are_integers_not_strings_or_booleans() -> None:
    """A step is an ``int`` percent: ``"100"`` and ``True`` are type errors.

    ``bool`` is an ``int`` subclass, so the type check excludes it explicitly;
    the UI sends JSON numbers.
    """
    with pytest.raises(SettingTypeError):
        validate_value("ui.scale", "100")
    with pytest.raises(SettingTypeError):
        validate_value("ui.scale", True)


def test_normalise_value_and_validate_value_agree_on_a_scale_step() -> None:
    """Both entry points reject a non-step, which is why the rule is in the validator.

    ``ui.scale`` has no normaliser, so a rule that lived there would be skipped by
    ``validate_value`` alone; a store write runs both.
    """
    with pytest.raises(SettingValueError):
        normalise_value("ui.scale", 87)
    with pytest.raises(SettingValueError):
        validate_value("ui.scale", 87)


def test_remote_tts_is_now_a_valid_default_engine() -> None:
    """D24 made ``remote_tts`` real, so the registry accepts it."""
    validate_value("tts.default_engine", "remote_tts")


def test_http_tts_is_now_a_valid_default_engine() -> None:
    """D25 (HSD-s856) ships ``http_tts``, so it is an accepted engine choice."""
    validate_value("tts.default_engine", "http_tts")

    assert "http_tts" in SETTINGS["tts.default_engine"].choices


def test_tts_can_be_switched_off_entirely() -> None:
    """``none`` is a supported engine choice, not an unavailable engine (D27)."""
    validate_value("tts.default_engine", "none")


@pytest.mark.parametrize(
    ("key", "value", "accepted"),
    [
        ("ui.accent_color", "red", False),
        ("ui.accent_color", "#ff", False),
        ("audio.quiet_hours_start", "7:00", False),
        ("audio.quiet_hours_end", "24:00", False),
        ("tts.ha.language", "english", False),
        ("ha.base_url", "ftp://ha.local:21", False),
        ("ui.accent_color", "#fff", True),
        ("ui.accent_color", "#4f9cf9", True),
        ("audio.quiet_hours_start", "07:00", True),
        ("audio.quiet_hours_end", "23:59", True),
        ("tts.ha.language", "en-GB", True),
        ("ha.base_url", "", True),
        ("ha.base_url", "http://homeassistant.local:8123", True),
        ("tts.remote_tts.url", "", True),
        ("tts.remote_tts.url", "http://bigpc.local:8090", True),
        ("tts.remote_tts.url", "https://bigpc.local:8090/render", True),
        ("tts.remote_tts.url", "ftp://x.local", False),
        ("tts.remote_tts.url", "bigpc.local:8090", False),
        ("tts.remote_tts.url", "http://", False),
        # ``device.name`` needs one alphanumeric character so the instance id it
        # pins can always be derived (HSD-vwdy).
        ("device.name", "Hall Doorbell", True),
        ("device.name", "x", True),
        ("device.name", "!!!", False),
        ("device.name", "---", False),
        ("device.name", "", False),
        ("device.name", "   ", False),
    ],
)
def test_patterns_are_enforced(key: str, value: str, accepted: bool) -> None:
    """Pattern-constrained settings accept the documented formats and nothing else.

    :param key: setting under test.
    :param value: candidate value.
    :param accepted: whether the registry is expected to accept *value*.
    """
    if accepted:
        validate_value(key, value)
        return

    with pytest.raises(SettingValueError) as exc_info:
        validate_value(key, value)

    assert exc_info.value.key == key


def test_longest_accepted_secret_fits_and_the_message_hides_the_value() -> None:
    """A 4096-character token fits, and a rejected one is never echoed back."""
    accepted = "t" * 4096

    validate_value("ha.token", accepted)

    with pytest.raises(SettingValueError) as exc_info:
        validate_value("ha.token", accepted + "s")

    message = str(exc_info.value)
    # Exact text: not one submitted character may be echoed back.
    assert message == "setting 'ha.token' must be at most 4096 characters"
    assert "ha.token" in message
    assert "4096" in message
    assert accepted not in message


@pytest.mark.parametrize(
    ("key", "value"),
    [("audio.volume", "loud"), ("mqtt.tls", 1), ("audio.volume", True), ("ui.theme", 3)],
)
def test_type_checks_still_run(key: str, value: object) -> None:
    """The registered Python type is checked before any constraint.

    :param key: setting under test.
    :param value: value of the wrong type.
    """
    with pytest.raises(SettingTypeError) as exc_info:
        validate_value(key, value)

    assert exc_info.value.key == key


def test_unknown_key_is_rejected_everywhere() -> None:
    """Both registry entry points reject an unregistered key by name."""
    with pytest.raises(UnknownSettingKey) as validate_info:
        validate_value("nope.nothing", "value")
    with pytest.raises(UnknownSettingKey) as normalise_info:
        normalise_value("nope.nothing", "value")

    assert validate_info.value.key == "nope.nothing"
    assert normalise_info.value.key == "nope.nothing"


def test_normalise_value_canonicalises_the_log_level() -> None:
    """The log level is stripped and lower-cased before it is validated."""
    assert normalise_value("system.log_level", " DEBUG ") == "debug"
    for level in LOG_LEVELS:
        assert normalise_value("system.log_level", level) == level.lower()
        assert normalise_value("system.log_level", level.lower()) == level.lower()

    with pytest.raises(SettingValueError):
        normalise_value("system.log_level", "loud")
    with pytest.raises(SettingTypeError):
        normalise_value("system.log_level", 3)


def test_device_name_defaults_to_the_product_name() -> None:
    """An unnamed unit is the product name, which the MQTT bridge also defaults to.

    ``tests/test_mqtt_bridge.py`` pins the bridge's own constant to this default,
    so the two cannot drift apart (HSD-vwdy).
    """
    assert SETTINGS["device.name"].default == "ChimeDeck"
    assert SETTINGS["device.name"].type is SettingType.STRING


def test_both_loudness_stages_default_to_full_and_the_rail_to_software() -> None:
    """cd-qul9: the app owns both stages, so a fresh install has nothing held back.

    ``audio.volume`` was 70 before this ticket, which is exactly half the reason a
    UI said 100% while the speaker was quieter than requested; a stored value is
    never rewritten, so existing installs keep what they deliberately chose.
    """
    assert SETTINGS["audio.volume"].default == 100
    assert SETTINGS["audio.hardware_volume"].default == 100
    assert SETTINGS["audio.rail_target"].default == "software"
    assert SETTINGS["audio.hardware_control"].default == ""


def test_the_stored_hardware_control_is_trimmed_and_length_capped() -> None:
    """The manual control name is an ordinary trimmed string with the device-name cap."""
    assert normalise_value("audio.hardware_control", "  Speaker  ") == "Speaker"

    with pytest.raises(SettingValueError) as exc_info:
        validate_value("audio.hardware_control", "s" * 65)

    assert exc_info.value.key == "audio.hardware_control"
    assert "at most 64 characters" in str(exc_info.value)


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [
        ("  Hall Doorbell  ", "Hall Doorbell"),
        ("\tKitchen\n", "Kitchen"),
        ("Hall Doorbell", "Hall Doorbell"),
    ],
)
def test_device_name_is_trimmed_before_it_is_stored(submitted: str, stored: str) -> None:
    """Surrounding whitespace never reaches the store, the HA device page or a slug.

    :param submitted: value the caller sends.
    :param stored: canonical value the registry produces.
    """
    assert normalise_value("device.name", submitted) == stored


def test_device_name_is_trimmed_before_the_length_check() -> None:
    """A 64-character name with surrounding spaces is accepted, not 68 characters.

    Normalising before validating is what makes the cap count the name the user
    meant rather than their keystrokes (HSD-vwdy).
    """
    name = "a" * 64

    assert normalise_value("device.name", f"  {name}  ") == name

    with pytest.raises(SettingValueError) as exc_info:
        normalise_value("device.name", f"  {name}a  ")

    assert exc_info.value.key == "device.name"
    assert "at most 64 characters" in str(exc_info.value)


@pytest.mark.parametrize("value", ["", "   ", "!!!"])
def test_a_device_name_that_cannot_be_slugged_is_rejected_by_name(value: str) -> None:
    """An empty or punctuation-only name is refused with the key and the rule.

    Such a name would make the derived instance id impossible, so the registry
    refuses it before the API can pin anything (HSD-vwdy).

    :param value: rejected device name.
    """
    with pytest.raises(SettingValueError) as exc_info:
        normalise_value("device.name", value)

    assert exc_info.value.key == "device.name"
    assert str(exc_info.value).startswith("setting 'device.name' ")


@pytest.mark.asyncio
async def test_store_normalises_on_write(store: ConfigStore) -> None:
    """``set_setting`` stores the canonical form whichever path wrote it.

    :param store: connected store backed by a temporary database.
    """
    await store.set_setting("system.log_level", "DEBUG")

    assert await store.get_setting("system.log_level") == "debug"


@pytest.mark.asyncio
async def test_import_normalises_settings(store: ConfigStore) -> None:
    """An imported document is normalised exactly like a settings write.

    :param store: connected store backed by a temporary database.
    """
    report = await import_config(
        store,
        {
            "format": "chime-deck/config",
            "version": 1,
            "exported_at": "2026-01-01T00:00:00.000000Z",
            "settings": {"system.log_level": "DEBUG"},
        },
    )

    assert report.settings_written == 1
    assert await store.get_setting("system.log_level") == "debug"


@pytest.mark.asyncio
async def test_store_rejects_a_value_the_registry_forbids(store: ConfigStore) -> None:
    """A rejected write leaves the previous (default) value in place.

    :param store: connected store backed by a temporary database.
    """
    with pytest.raises(SettingValueError) as exc_info:
        await store.set_setting("audio.volume", 101)

    assert exc_info.value.key == "audio.volume"
    assert await store.get_setting("audio.volume") == 100


_VALID_PROVIDER_ID = "0123456789ab"
"""A synthetic provider id: 12 lower-case hex characters, as D25 defines them."""


def test_allow_dynamic_paid_defaults_to_false() -> None:
    """D25: templated phrases on a paid provider need consent, so the default is ``False``."""
    spec = SETTINGS["tts.allow_dynamic_paid"]

    assert spec.type is SettingType.BOOLEAN
    assert spec.default is False
    validate_value("tts.allow_dynamic_paid", True)


def test_providers_default_to_an_empty_list_and_accept_records() -> None:
    """``tts.providers`` is a JSON array of records and starts empty (D25).

    The store checks only the container: a record's schema is validated by the
    provider routes, which may import :mod:`chime_deck.tts`.
    """
    spec = SETTINGS["tts.providers"]

    assert spec.type is SettingType.JSON
    assert spec.default == []
    validate_value("tts.providers", [])
    validate_value(
        "tts.providers",
        [{"id": _VALID_PROVIDER_ID, "label": "ElevenLabs", "paid": True}],
    )
    validate_value("tts.providers", {"not": "an array"})


@pytest.mark.parametrize("value", ["[]", 1, True, None])
def test_providers_reject_a_non_container(value: object) -> None:
    """A JSON setting accepts a list or an object and nothing else.

    :param value: JSON-compatible value of the wrong container kind.
    """
    with pytest.raises(SettingTypeError) as exc_info:
        validate_value("tts.providers", value)

    assert exc_info.value.key == "tts.providers"
    assert exc_info.value.expected == "json"


def test_provider_secret_keys_round_trip() -> None:
    """A provider's key row is derivable from its id and back, and counts as a secret."""
    key = provider_secret_key(_VALID_PROVIDER_ID)

    assert key == f"tts.providers.{_VALID_PROVIDER_ID}.api_key"
    assert provider_id_from_secret_key(key) == _VALID_PROVIDER_ID
    assert is_secret_key(key) is True
    assert is_secret_key("ha.token") is True
    assert is_secret_key("tts.providers") is False
    assert is_secret_key("tts.http_tts.provider") is False


@pytest.mark.parametrize(
    "key",
    [
        "tts.providers.abc.api_key",
        "tts.providers.0123456789abc.api_key",
        "tts.providers.0123456789a.api_key",
        "tts.providers.0123456789AB.api_key",
        "tts.providers.0123456789ag.api_key",
        "tts.providers..api_key",
        "tts.providers.api_key",
        "other.providers.0123456789ab.api_key",
        "tts.providers.0123456789ab.token",
    ],
)
def test_a_near_miss_is_not_a_provider_secret_key(key: str) -> None:
    """Only the exact prefix, 12 lower-case hex characters and suffix name a key row.

    :param key: key that looks close to ``tts.providers.<id>.api_key``.
    """
    assert provider_id_from_secret_key(key) is None
    assert is_secret_key(key) is False


def test_a_provider_secret_key_is_not_a_registered_setting() -> None:
    """The key rows live outside the registry, so only the dedicated store methods reach them."""
    key = provider_secret_key(_VALID_PROVIDER_ID)

    assert key not in SETTINGS
    with pytest.raises(UnknownSettingKey):
        validate_value(key, "some-key")
