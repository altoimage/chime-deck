"""The bundled TTS presets and the configured-provider registry, all offline.

Presets are read from the packaged ``tts/presets`` directory or from a
``tmp_path`` copy, and the store is a dict-backed stub standing in for
``ConfigStore``, so no file outside the temporary directory is written, no
socket is opened and no API key leaves the process.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from chime_deck.tts.presets import (
    DEFAULT_PRESETS,
    PresetError,
    load_presets,
)
from chime_deck.tts.providers import (
    HTTP_TTS_ENGINE,
    NO_PAID_PROVIDERS,
    PROVIDER_ID_PATTERN,
    ConfiguredProviders,
    ProviderError,
    SynthesisCosting,
    TtsCost,
    new_provider_id,
    parse_provider,
    parse_providers,
    read_providers,
)

_ID: Final = "0123456789ab"
"""Provider id the records under test normally use."""

_OTHER_ID: Final = "fedcba987654"
"""A second provider id, for selections that do not exist."""

_CUSTOM_OVERRIDES: Final = {
    "method": "PUT",
    "path": "/v2/speak",
    "headers": {"Content-Type": "application/json"},
    "auth": {"style": "header", "name": "X-Acme-Key"},
    "body_kind": "json",
    "body": {"utterance": "{text}", "lang": "{language}"},
    "response": "json:audio.b64",
    "format": "ogg",
    "paid": True,
    "cost_per_1k_chars_usd": 0.5,
}
"""A complete override set: every shape field, the paid flag and the price."""


def _preset_data() -> dict[str, object]:
    """Return a complete, valid preset file body for the data-driven tests."""
    return {
        "label": "Acme",
        "description": "Acme's own speech server.",
        "method": "POST",
        "path": "/speak",
        "base_url": "https://acme.test",
        "headers": {"Content-Type": "application/json"},
        "auth": {"style": "bearer"},
        "body_kind": "json",
        "body": {"input": {"text": "{text}"}, "speaker": "{voice}"},
        "response": "audio",
        "format": "wav",
        "paid": False,
        "cost_per_1k_chars_usd": 0.0,
        "pricing_note": "estimate of zero; the provider record can override it",
    }


def _write_preset(directory: Path, preset_id: str, data: Mapping[str, object]) -> Path:
    """Write one ``<id>.json`` preset file and return its path.

    :param directory: directory to create the file in.
    :param preset_id: preset id, which is the file name without the suffix.
    :param data: the file's JSON object.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{preset_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _record(preset: str = "openai", **changes: object) -> dict[str, object]:
    """Return one valid provider record, with *changes* applied over it.

    :param preset: preset id the record names.
    :param changes: fields to replace, including a deliberately invalid value.
    """
    raw: dict[str, object] = {
        "id": _ID,
        "name": "Kitchen voice",
        "preset": preset,
        "base_url": "",
        "voice": "alloy",
        "language": "en_GB",
        "paid": False,
        "cost_per_1k_chars_usd": None,
        "overrides": {},
    }
    raw.update(changes)
    return raw


class _Source:
    """A dict-backed stand-in for ``ConfigStore``'s provider-facing reads."""

    def __init__(
        self,
        values: Mapping[str, object] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        """Create the stub with no providers and nothing selected.

        :param values: setting values to override.
        :param secrets: provider id to stored API key.
        """
        self.values: dict[str, object] = {"tts.providers": [], "tts.http_tts.provider": ""}
        if values is not None:
            self.values.update(values)
        self.secrets: dict[str, str] = dict(secrets or {})

    async def get_setting(self, key: str) -> object:
        """Return the configured value for *key*, or ``None``.

        :param key: setting key.
        """
        return self.values.get(key)

    async def get_provider_secret(self, provider_id: str) -> str:
        """Return the stored API key for *provider_id*, or ``""``.

        :param provider_id: 12-character hexadecimal provider id.
        """
        return self.secrets.get(provider_id, "")


def _providers(
    records: object,
    selected: str = "",
    secrets: Mapping[str, str] | None = None,
) -> ConfiguredProviders:
    """Return a registry over a stub holding *records* and *selected*.

    :param records: value of ``tts.providers``, valid or deliberately not.
    :param selected: value of ``tts.http_tts.provider``.
    :param secrets: provider id to stored API key.
    """
    return ConfiguredProviders(
        _Source({"tts.providers": records, "tts.http_tts.provider": selected}, secrets)
    )


def test_the_packaged_presets_are_exactly_the_expected_six() -> None:
    """Every shipped preset loads and carries the fields the picker needs."""
    assert DEFAULT_PRESETS.ids() == (
        "azure",
        "custom",
        "elevenlabs",
        "google",
        "openai",
        "openai_compatible",
    )
    for preset_id in DEFAULT_PRESETS.ids():
        preset = DEFAULT_PRESETS[preset_id]
        assert preset.id == preset_id
        assert preset.label
        assert preset.description
        assert "estimate" in preset.pricing_note
        assert preset.shape.method == "POST"
        assert preset.shape.format in {"mp3", "wav", "ogg", "flac"}


def test_openai_preset_is_the_documented_request() -> None:
    """The OpenAI preset posts the documented body and is marked paid."""
    preset = DEFAULT_PRESETS["openai"]
    assert preset.base_url == "https://api.openai.com"
    assert preset.shape.path == "/v1/audio/speech"
    assert preset.shape.headers == {"Content-Type": "application/json"}
    assert preset.shape.auth.style == "bearer"
    assert preset.shape.body_kind == "json"
    assert preset.shape.body == {
        "model": "tts-1",
        "input": "{text}",
        "voice": "{voice}",
        "response_format": "mp3",
    }
    assert preset.shape.response == "audio"
    assert preset.paid is True
    assert preset.cost_per_1k_chars_usd == 0.015


def test_the_awkward_presets_keep_their_real_shapes() -> None:
    """Google, Azure, ElevenLabs, the compatible server and custom stay distinct."""
    google = DEFAULT_PRESETS["google"]
    assert google.shape.response == "json:audioContent"
    assert google.shape.response_field == "audioContent"
    assert google.shape.body == {
        "input": {"text": "{text}"},
        "voice": {"languageCode": "{language}", "name": "{voice}"},
        "audioConfig": {"audioEncoding": "MP3"},
    }

    azure = DEFAULT_PRESETS["azure"]
    assert azure.base_url is None
    assert azure.requires_base_url is True
    assert azure.shape.body_kind == "text"
    body = azure.shape.body
    assert isinstance(body, str)
    assert '<voice name="{voice}">' in body
    assert "xml:lang='{language}'" in body
    assert azure.shape.auth.style == "header"
    assert azure.shape.auth.name == "Ocp-Apim-Subscription-Key"

    elevenlabs = DEFAULT_PRESETS["elevenlabs"]
    assert elevenlabs.shape.path == "/v1/text-to-speech/{voice}"
    assert elevenlabs.shape.auth.name == "xi-api-key"

    compatible = DEFAULT_PRESETS["openai_compatible"]
    assert compatible.base_url is None
    assert compatible.requires_base_url is True
    assert compatible.paid is False
    assert compatible.cost_per_1k_chars_usd == 0.0

    custom = DEFAULT_PRESETS["custom"]
    assert custom.base_url is None
    assert custom.shape.auth.style == "none"
    assert custom.cost_per_1k_chars_usd is None


def test_placeholder_detection_follows_path_headers_and_body() -> None:
    """A preset that never mentions {voice} does not demand one."""
    assert DEFAULT_PRESETS["openai"].shape.uses_voice is True
    assert DEFAULT_PRESETS["openai"].shape.uses_language is False
    assert DEFAULT_PRESETS["google"].shape.uses_language is True
    assert DEFAULT_PRESETS["custom"].shape.uses_voice is False
    assert DEFAULT_PRESETS["custom"].shape.uses_language is False


def test_summaries_describe_every_preset_for_the_picker() -> None:
    """The picker rows carry the id, label, price and whether a URL is needed."""
    summaries = {summary.id: summary for summary in DEFAULT_PRESETS.summaries()}
    assert tuple(summaries) == DEFAULT_PRESETS.ids()
    openai = summaries["openai"]
    assert openai.label == "OpenAI"
    assert openai.format == "mp3"
    assert openai.paid is True
    assert openai.cost_per_1k_chars_usd == 0.015
    assert openai.requires_base_url is False
    assert summaries["azure"].requires_base_url is True
    assert summaries["azure"].format == "mp3"
    assert summaries["custom"].cost_per_1k_chars_usd is None
    assert "estimate" in summaries["elevenlabs"].pricing_note


def test_a_preset_added_as_data_needs_no_code_change(tmp_path: Path) -> None:
    """A brand-new JSON file is a usable preset with no Python change at all."""
    directory = tmp_path / "presets"
    data = _preset_data()
    data["path"] = "/acme/speak"
    data["body"] = {"input": {"text": "{text}"}, "speaker": "{voice}"}
    _write_preset(directory, "acme", data)
    presets = load_presets(directory)
    assert presets.ids() == ("acme",)
    preset = presets["acme"]
    assert preset.shape.path == "/acme/speak"
    assert preset.shape.headers == {"Content-Type": "application/json"}
    assert preset.shape.auth.style == "bearer"
    assert preset.shape.format == "wav"
    assert preset.shape.uses_voice is True
    record = parse_provider(
        _record(preset="acme", voice="speaker-1", base_url="https://voice.acme.test"),
        presets=presets,
    )
    assert record.preset == "acme"


@pytest.mark.asyncio
async def test_a_data_preset_resolves_through_the_registry(tmp_path: Path) -> None:
    """The registry resolves a hand-added preset like a packaged one."""
    directory = tmp_path / "presets"
    _write_preset(directory, "acme", _preset_data())
    presets = load_presets(directory)
    providers = ConfiguredProviders(
        _Source(
            {
                "tts.providers": [
                    _record(preset="acme", base_url="https://acme.test/", voice="speaker-1")
                ],
                "tts.http_tts.provider": _ID,
            }
        ),
        presets=presets,
    )
    (resolved,) = await providers.resolved()
    assert resolved.base_url == "https://acme.test"
    assert resolved.shape.path == "/speak"
    assert resolved.preset.id == "acme"


def test_load_presets_rejects_a_format_this_build_cannot_play(tmp_path: Path) -> None:
    """``format`` must name a container the player handles."""
    directory = tmp_path / "presets"
    data = _preset_data()
    data["format"] = "m4a"
    _write_preset(directory, "acme", data)
    with pytest.raises(PresetError) as raised:
        load_presets(directory)
    message = str(raised.value)
    assert "format" in message
    assert "m4a" in message


def test_load_presets_rejects_query_auth_and_explains_the_log_leak(tmp_path: Path) -> None:
    """A key in the query string is refused, with the httpx log as the reason."""
    directory = tmp_path / "presets"
    data = _preset_data()
    data["auth"] = {"style": "query", "name": "key"}
    _write_preset(directory, "acme", data)
    with pytest.raises(PresetError) as raised:
        load_presets(directory)
    message = str(raised.value)
    assert "query" in message
    assert "httpx" in message
    assert "log" in message


def test_load_presets_rejects_a_response_naming_no_field(tmp_path: Path) -> None:
    """``json:`` without a field cannot locate any audio."""
    directory = tmp_path / "presets"
    data = _preset_data()
    data["response"] = ":field"
    _write_preset(directory, "acme", data)
    with pytest.raises(PresetError) as raised:
        load_presets(directory)
    assert "response" in str(raised.value)


def test_load_presets_rejects_a_missing_label(tmp_path: Path) -> None:
    """A preset with no label would render a blank picker row."""
    directory = tmp_path / "presets"
    data = _preset_data()
    del data["label"]
    _write_preset(directory, "acme", data)
    with pytest.raises(PresetError) as raised:
        load_presets(directory)
    assert "label" in str(raised.value)


def test_load_presets_reports_a_missing_directory(tmp_path: Path) -> None:
    """Zero presets is a fault, not a silent success."""
    with pytest.raises(PresetError) as raised:
        load_presets(tmp_path / "absent")
    assert "does not exist" in str(raised.value)


_BAD_RECORDS: Final = [
    pytest.param({"id": "abc"}, "id", id="id"),
    pytest.param({"name": ""}, "name", id="name"),
    pytest.param({"name": "x" * 201}, "name", id="long-name"),
    pytest.param({"preset": "nope"}, "preset", id="unknown-preset"),
    pytest.param({"preset": "azure", "base_url": ""}, "azure", id="azure-needs-url"),
    pytest.param({"base_url": "ftp://acme.test"}, "base_url", id="bad-url"),
    pytest.param({"preset": "elevenlabs", "voice": ""}, "voice", id="voice"),
    pytest.param({"language": "english"}, "language", id="language"),
    pytest.param({"paid": "yes"}, "paid", id="paid"),
    pytest.param({"cost_per_1k_chars_usd": -1}, "cost_per_1k_chars_usd", id="negative-price"),
    pytest.param({"cost_per_1k_chars_usd": True}, "cost_per_1k_chars_usd", id="bool-price"),
    pytest.param({"cost_per_1k_chars_usd": float("nan")}, "cost_per_1k_chars_usd", id="nan-price"),
    pytest.param({"overrides": {"method": "PUT"}}, "overrides", id="overrides-on-preset"),
]
"""One invalid field per case, and the word the rejection must name."""


@pytest.mark.parametrize(("changes", "field"), _BAD_RECORDS)
def test_parse_provider_rejects_a_bad_field(changes: Mapping[str, object], field: str) -> None:
    """Every invalid field is refused by name, as one sentence."""
    raw = _record()
    raw.update(changes)
    with pytest.raises(ProviderError) as raised:
        parse_provider(raw, presets=DEFAULT_PRESETS)
    assert field in str(raised.value)


def test_parse_provider_rejects_an_api_key_inside_the_record() -> None:
    """The array is secret-free, and the rejection never echoes the key."""
    secret = "sk-live-must-not-appear"
    with pytest.raises(ProviderError) as raised:
        parse_provider(_record(api_key=secret), presets=DEFAULT_PRESETS)
    message = str(raised.value)
    assert "stored separately" in message
    assert secret not in message


def test_parse_provider_rejects_an_api_key_inside_overrides() -> None:
    """A key smuggled into a custom override is refused the same way."""
    secret = "sk-live-must-not-appear"
    record = _record(preset="custom", base_url="http://acme.test", overrides={"api_key": secret})
    with pytest.raises(ProviderError) as raised:
        parse_provider(record, presets=DEFAULT_PRESETS)
    message = str(raised.value)
    assert "stored separately" in message
    assert secret not in message


def test_parse_providers_rejects_a_duplicate_id() -> None:
    """Two providers cannot share the id the selected setting names."""
    with pytest.raises(ProviderError) as raised:
        parse_providers([_record(), _record(name="Hallway")], presets=DEFAULT_PRESETS)
    message = str(raised.value)
    assert "provider #2" in message
    assert "id" in message


def test_parse_providers_rejects_a_duplicate_name() -> None:
    """Two providers cannot share a name, ignoring case, in the picker."""
    with pytest.raises(ProviderError) as raised:
        parse_providers(
            [_record(), _record(id=_OTHER_ID, name="kitchen voice")],
            presets=DEFAULT_PRESETS,
        )
    message = str(raised.value)
    assert "provider #2" in message
    assert "name" in message


def test_parse_providers_rejects_a_non_array() -> None:
    """The writing path insists on the documented container."""
    with pytest.raises(ProviderError) as raised:
        parse_providers({}, presets=DEFAULT_PRESETS)
    assert "JSON array" in str(raised.value)


def test_a_custom_record_may_override_every_shape_field() -> None:
    """A full override set is accepted and stored unchanged."""
    record = parse_provider(
        _record(preset="custom", base_url="http://acme.test", overrides=_CUSTOM_OVERRIDES),
        presets=DEFAULT_PRESETS,
    )
    assert record.overrides["path"] == "/v2/speak"
    assert record.overrides["format"] == "ogg"
    assert record.overrides["cost_per_1k_chars_usd"] == 0.5


@pytest.mark.asyncio
async def test_custom_overrides_reach_the_resolved_shape_and_price() -> None:
    """The resolved provider uses the override's shape, paid flag and price."""
    providers = _providers(
        [_record(preset="custom", base_url="http://acme.test", overrides=_CUSTOM_OVERRIDES)],
        selected=_ID,
    )
    (resolved,) = await providers.resolved()
    assert resolved.shape.method == "PUT"
    assert resolved.shape.path == "/v2/speak"
    assert resolved.shape.auth.name == "X-Acme-Key"
    assert resolved.shape.response_field == "audio.b64"
    assert resolved.shape.format == "ogg"
    assert resolved.shape.uses_language is True
    assert resolved.paid is True
    assert resolved.cost_per_1k_chars_usd == 0.5
    cost = await providers.estimate_cost("abc")
    assert cost is not None
    assert cost.usd == pytest.approx(3 * 0.5 / 1000.0)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"nonsense": 1}, "does not recognise", id="unknown-field"),
        pytest.param({"format": "m4a"}, "format", id="bad-format"),
        pytest.param({"auth": {"style": "query"}}, "httpx", id="query-auth"),
        pytest.param({"body_kind": "json", "body": "not an object"}, "body", id="bad-body"),
        pytest.param({"paid": "yes"}, "paid", id="bad-paid"),
        pytest.param({"cost_per_1k_chars_usd": -1}, "cost_per_1k_chars_usd", id="bad-price"),
    ],
)
def test_a_custom_record_with_a_bad_override_is_refused(
    overrides: Mapping[str, object], expected: str
) -> None:
    """Overrides are validated with the same rules as a preset file."""
    record = _record(preset="custom", base_url="http://acme.test", overrides=overrides)
    with pytest.raises(ProviderError) as raised:
        parse_provider(record, presets=DEFAULT_PRESETS)
    assert expected in str(raised.value)


@pytest.mark.parametrize("raw", [None, {}, "nonsense", [1, 2]])
def test_read_providers_tolerates_garbage(raw: object) -> None:
    """The read path answers an empty tuple instead of raising."""
    assert read_providers(raw, presets=DEFAULT_PRESETS) == ()


def test_read_providers_keeps_the_readable_records() -> None:
    """Unreadable and duplicate records are skipped, the rest survive."""
    raw = [
        1,
        _record(),
        _record(id=_OTHER_ID, name="Hallway", preset="from-the-future"),
        _record(id="ffffffffffff", name="Kitchen voice"),
    ]
    records = read_providers(raw, presets=DEFAULT_PRESETS)
    assert [record.id for record in records] == [_ID]
    assert records[0].name == "Kitchen voice"


def test_read_providers_skips_an_unknown_preset() -> None:
    """A preset this build does not ship leaves the record unreadable, not fatal."""
    assert read_providers([_record(preset="from-the-future")], presets=DEFAULT_PRESETS) == ()


@pytest.mark.asyncio
async def test_is_paid_is_true_only_for_a_paid_selected_http_provider() -> None:
    """D25's answer is exact: http_tts, selected, and marked paid."""
    paid = _record(paid=True)
    assert await _providers([paid], selected=_ID).is_paid(HTTP_TTS_ENGINE) is True
    assert await _providers([paid], selected=_ID).is_paid("piper") is False
    assert await _providers([paid], selected=_OTHER_ID).is_paid(HTTP_TTS_ENGINE) is False
    assert await _providers([_record(paid=False)], selected=_ID).is_paid(HTTP_TTS_ENGINE) is False
    assert await _providers([], selected=_ID).is_paid(HTTP_TTS_ENGINE) is False
    assert await _providers([paid], selected="").is_paid(HTTP_TTS_ENGINE) is False
    assert await NO_PAID_PROVIDERS.is_paid(HTTP_TTS_ENGINE) is False


@pytest.mark.asyncio
async def test_the_registry_reads_the_api_key_from_the_source_port() -> None:
    """``resolved`` carries the stored key, the preset default and the flag."""
    providers = _providers([_record(paid=True)], selected=_ID, secrets={_ID: "token-abc"})
    records = await providers.records()
    assert [record.id for record in records] == [_ID]
    (resolved,) = await providers.resolved()
    assert resolved.api_key == "token-abc"
    assert resolved.has_api_key is True
    assert resolved.base_url == "https://api.openai.com"
    assert resolved.preset.id == "openai"


@pytest.mark.parametrize(
    ("records", "selected", "secrets", "expected"),
    [
        pytest.param([], _ID, {}, "no TTS provider is configured", id="none-configured"),
        pytest.param(
            [_record(paid=True)], "", {}, "no TTS provider is selected", id="none-selected"
        ),
        pytest.param(
            [_record(paid=True)], _OTHER_ID, {}, "no longer configured", id="selected-vanished"
        ),
        pytest.param([_record(paid=True)], _ID, {}, "no API key stored", id="no-key"),
    ],
)
@pytest.mark.asyncio
async def test_availability_names_each_gap(
    records: list[dict[str, object]],
    selected: str,
    secrets: Mapping[str, str],
    expected: str,
) -> None:
    """Each of the four ways http_tts cannot speak gets its own sentence."""
    availability = await _providers(records, selected, secrets).availability()
    assert availability.ok is False
    assert expected in (availability.reason or "")


@pytest.mark.asyncio
async def test_availability_is_ok_with_a_keyed_selected_provider() -> None:
    """A selected provider with a stored key is available."""
    availability = await _providers(
        [_record(paid=True)], selected=_ID, secrets={_ID: "token"}
    ).availability()
    assert availability.ok is True
    assert availability.reason is None


@pytest.mark.asyncio
async def test_availability_does_not_demand_a_key_when_the_preset_needs_none() -> None:
    """The custom preset authenticates with nothing, so no key is required."""
    record = _record(preset="custom", base_url="http://acme.test")
    availability = await _providers([record], selected=_ID).availability()
    assert availability.ok is True
    assert availability.reason is None


@pytest.mark.asyncio
async def test_estimate_cost_prefers_the_record_price() -> None:
    """The record's own price beats the preset's estimate."""
    record = _record(paid=True, cost_per_1k_chars_usd=2.0)
    cost = await _providers([record], selected=_ID).estimate_cost("x" * 118)
    assert cost is not None
    assert cost.chars == 118
    assert cost.usd == pytest.approx(118 * 2.0 / 1000.0)
    assert cost.note() == "118 characters billed, estimated $0.2360"


@pytest.mark.asyncio
async def test_estimate_cost_falls_back_to_the_preset_price() -> None:
    """With no record price, the preset's estimate applies."""
    preset_price = DEFAULT_PRESETS["openai"].cost_per_1k_chars_usd
    assert preset_price is not None
    cost = await _providers([_record(paid=True)], selected=_ID).estimate_cost("abc")
    assert cost is not None
    assert cost.usd == pytest.approx(3 * preset_price / 1000.0)


@pytest.mark.asyncio
async def test_estimate_cost_is_unknown_when_no_price_is_configured() -> None:
    """Unknown is ``None``, never ``0.0`` dressed up as free."""
    record = _record(preset="custom", base_url="http://acme.test")
    cost = await _providers([record], selected=_ID).estimate_cost("hello")
    assert cost is not None
    assert cost.usd is None
    assert "cost unknown" in cost.note()


@pytest.mark.asyncio
async def test_estimate_cost_is_none_without_a_selected_provider() -> None:
    """Nothing selected means nothing to price."""
    assert await _providers([_record(paid=True)], selected="").estimate_cost("hi") is None


def test_a_cost_note_says_characters_and_unknown() -> None:
    """The note is one readable sentence in both cases."""
    assert TtsCost(1, None).note() == "1 character billed, cost unknown (no price configured)"
    assert TtsCost(118, 0.0021).note() == "118 characters billed, estimated $0.0021"


def test_new_provider_id_matches_the_store_pattern() -> None:
    """A generated id is exactly the 12 lower-case hex characters the store accepts."""
    assert re.fullmatch(PROVIDER_ID_PATTERN, new_provider_id()) is not None
    assert len({new_provider_id() for _ in range(20)}) > 1


def test_the_registry_satisfies_the_costing_port() -> None:
    """An engine can ``isinstance`` check the costing seam, as SynthesisTiming is."""
    assert isinstance(ConfiguredProviders(_Source()), SynthesisCosting)
