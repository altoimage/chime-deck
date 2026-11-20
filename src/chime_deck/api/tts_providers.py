"""The external TTS provider endpoints: list, add, edit, delete and test one.

Five routes live here, and they are the **only** write path for the D25
provider configuration:

``GET /api/tts/providers``
    the records, the preset library (request shape included, so the UI can
    prefill a ``custom`` provider), the selected provider id and, when a stored
    record could not be read, one sentence saying so.
``POST /api/tts/providers`` (201)
    adds one provider; the id is minted here.
``PATCH /api/tts/providers/{id}`` (200)
    changes some fields; the path id wins and any submitted ``id`` is ignored.
``DELETE /api/tts/providers/{id}`` (200)
    removes the record, its stored API key, and the selection if it pointed at
    it.
``POST /api/tts/providers/{id}/test`` (200)
    speaks one fixed phrase through **that** provider — not necessarily the
    selected one — measures the round trip and reports what happened. A failed
    synthesis is ``200`` with ``ok: false``, like every other connection test in
    this API.

**The API key is write-only and top-level.** It never appears in a record, in
any response model, or in an error message. ``api_key`` absent on a ``PATCH``
leaves the stored key alone, ``null`` clears it and a string replaces it; an
empty string is refused with the house sentence, because an empty credential is
almost always a form that failed to load rather than a deliberate clearing. A
submitted key that arrives anywhere but the top-level field — inside
``overrides``, or nested in one — is rejected naming the top-level field, never
echoing the value.

**Records are validated strictly, as a whole list.** Every mutation is
read-modify-write over ``tts.providers`` and the *resulting* list goes through
:func:`~chime_deck.tts.parse_providers`, because duplicate names and ids are
cross-record conditions no single record can see. A rejection is the parser's
own whole sentence, so the message names the offending provider by position and
field.

**Last writer wins.** This is a single-user appliance: two browsers editing the
provider list at the same time are not merged, and the later write replaces the
earlier one. That is a deliberate limitation, not an oversight — a merge policy
would be a concurrency model this product does not have anywhere else.

**The test route borrows the application's HTTP client.** The one client this
process may already have been handed — ``state.ha_client``, the seam the Home
Assistant routes and ``ha_tts`` borrow — is passed to
:class:`~chime_deck.tts.HttpTtsEngine`, so a test answers the probe with
``httpx.MockTransport`` and no socket is opened. Production leaves it ``None``,
and the engine then builds and closes a client of its own per request.

**A stored record this build cannot read is not silently thrown away.** The
list route reports it in ``warning``; a mutation refuses to write while one
exists, because writing back the readable records would delete the unreadable
one. The fix is the settings file, which is where the warning points.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, NoReturn, cast

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import SECRET_SCRUBBER, refresh_secret_scrubber
from ..tts import (
    DEFAULT_PRESETS,
    ConfiguredProviders,
    HttpTtsEngine,
    Preset,
    ProviderError,
    ProviderRecord,
    ResolvedProvider,
    new_provider_id,
    parse_providers,
    read_providers,
)
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/tts.py``).
    from ..app import AppState

__all__ = [
    "PROVIDER_TEST_TIMEOUT_S",
    "DeleteProviderResponse",
    "PresetSummaryResponse",
    "ProviderResponse",
    "ProviderShapeResponse",
    "ProviderWriteRequest",
    "TtsProviderTestResponse",
    "TtsProvidersResponse",
    "build_provider_router",
]

_LOGGER = logging.getLogger(__name__)

PROVIDER_TEST_TIMEOUT_S: Final = 10.0
"""Seconds the whole provider test route may take.

The engine bounds one render at
:data:`~chime_deck.tts.HTTP_TTS_RENDER_BUDGET_S` (7.5 s); this outer
deadline is what makes a wedged provider harmless to the settings page, and it
sits above the inner budget so the engine's own sentence normally wins.
"""

_TEST_PHRASE: Final = "This is a test of the doorbell's speech provider."
"""The one fixed phrase the test route speaks; never a user's button text."""

_SECRET_FIELD: Final = "api_key"
"""The one write-only field: top level, never inside a record."""

_EMPTY_SECRET_PROBLEM: Final = "send null to clear a secret, not an empty string"
"""The house sentence for an emptied secret (mirrors ``api/settings.py``)."""

_UNREADABLE_ONE: Final = (
    "one stored provider could not be read and is ignored — "
    "check the provider list in the settings file"
)
"""Warning when exactly one stored record is unusable."""

_UNREADABLE_MANY: Final = (
    "{count} stored providers could not be read and are ignored — "
    "check the provider list in the settings file"
)
"""Warning when several stored records are unusable."""

_UNUSABLE_PROVIDER: Final = (
    "this provider's stored settings cannot be used — check the provider list in the settings file"
)
"""Verdict for a record that parses but cannot be turned into a request."""

_UNKNOWN_PROVIDER: Final = (
    "no TTS provider has the id {provider_id!r} — reload the provider list and try again"
)
"""404 sentence for an id the provider list does not hold."""

_TEST_TIMEOUT: Final = (
    "the provider test did not answer within {seconds:g} s — check the provider's server "
    "and its base URL"
)
"""Verdict when the whole test route outlives its deadline."""


class AuthStyleResponse(BaseModel):
    """How one preset authenticates, as the UI shows it."""

    style: str
    name: str
    prefix: str


class ProviderShapeResponse(BaseModel):
    """One preset's request shape, so the UI can show it and prefill ``custom``.

    ``body`` is the preset's JSON body (or its SSML text), unchanged: it is data
    the user may copy into a ``custom`` provider's overrides.
    """

    method: str
    path: str
    headers: dict[str, str]
    auth: AuthStyleResponse
    body_kind: str
    body: object
    response: str
    format: str


class PresetSummaryResponse(BaseModel):
    """One preset library row plus the exact request it will send."""

    id: str
    label: str
    description: str
    paid: bool
    format: str
    cost_per_1k_chars_usd: float | None
    pricing_note: str
    requires_base_url: bool
    shape: ProviderShapeResponse


class ProviderResponse(BaseModel):
    """One configured provider, for the settings page's list.

    ``has_api_key`` is the only thing that ever describes the key; the key
    itself is never read back. ``overrides`` is ``{}`` unless the record uses
    the ``custom`` preset.
    """

    id: str
    name: str
    preset: str
    base_url: str
    voice: str
    language: str
    paid: bool
    cost_per_1k_chars_usd: float | None
    has_api_key: bool
    overrides: dict[str, object]


class TtsProvidersResponse(BaseModel):
    """The provider list, the preset library and the one selected provider."""

    providers: list[ProviderResponse]
    presets: list[PresetSummaryResponse]
    selected: str
    warning: str | None = None


class DeleteProviderResponse(BaseModel):
    """What deleting one provider removed."""

    deleted: bool
    id: str


class TtsProviderTestResponse(BaseModel):
    """One measured test synthesis, or the sentence explaining why there was none.

    ``latency_ms`` is the **measured** round trip, never an estimate, and it is
    ``null`` whenever no usable audio came back. ``chars`` counts the characters
    actually sent, so it is ``null`` on failure too. ``cost_per_100_chars_usd``
    is the provider's configured price per 100 characters and ``null`` when no
    price is known — never a fabricated ``0.0``; ``estimated_cost_usd`` prices
    the phrase and is ``null`` when nothing was billed.
    """

    ok: bool
    provider: str
    latency_ms: float | None
    chars: int | None
    format: str | None
    cost_per_100_chars_usd: float | None
    estimated_cost_usd: float | None
    error: str | None


class ProviderWriteRequest(BaseModel):
    """Body of ``POST`` and ``PATCH``; every field is optional on a patch.

    Omitted and explicit ``null`` differ only where the field is clearable:
    ``api_key`` (absent keeps, ``null`` clears) and ``cost_per_1k_chars_usd``
    (absent keeps, ``null`` returns to the preset's estimate). ``overrides``
    behaves the same way (``null`` clears).
    """

    name: str | None = None
    preset: str | None = None
    base_url: str | None = None
    voice: str | None = None
    language: str | None = None
    paid: bool | None = None
    cost_per_1k_chars_usd: float | None = None
    api_key: str | None = None
    overrides: dict[str, object] | None = None


def _scrub(value: str) -> str:
    """Redact any secret-looking text before it enters a response model.

    :param value: text from a record, a preset or an exception.
    """
    return SECRET_SCRUBBER.scrub(value)


def _scrub_json(value: object) -> object:
    """Redact secrets from every string leaf of a JSON value.

    The provider's ``overrides`` may carry a phrase the user typed, which could
    coincide with a stored secret; nothing that leaves this module is exempt.

    :param value: JSON value from a record.
    """
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_json(item)
            for key, item in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_json(item) for item in cast(list[object], value)]
    return value


def _not_found(provider_id: str) -> NoReturn:
    """Raise the house 404 for an unknown provider id.

    :param provider_id: id from the request path.
    """
    raise ApiError(
        404,
        ErrorCode.NOT_FOUND,
        _scrub(_UNKNOWN_PROVIDER.format(provider_id=provider_id)),
        {"field": "id", "id": _scrub(provider_id)},
    )


def _stored_length(raw: object) -> int:
    """Return how many entries a stored ``tts.providers`` value claims to hold.

    A value that is not an array counts as one broken entry, so a hand-edited
    ``"nonsense"`` is reported as unreadable rather than ignored silently.

    :param raw: the stored ``tts.providers`` value, whatever it holds.
    """
    if raw is None:
        return 0
    if isinstance(raw, list):
        return len(raw)
    return 1


async def _stored(state: AppState) -> tuple[tuple[ProviderRecord, ...], int]:
    """Return the readable stored records and how many entries were dropped.

    :param state: live application state providing the store.
    """
    raw = await state.store.get_setting("tts.providers")
    records = read_providers(raw, presets=DEFAULT_PRESETS)
    return records, max(0, _stored_length(raw) - len(records))


def _refuse_when_unreadable(unreadable: int) -> None:
    """Refuse a write that would delete an unreadable stored record.

    Writing back the readable records is the only thing this route can do, and
    it would silently drop the entry the list route warned about. Refusing names
    the real fix instead.

    :param unreadable: how many stored entries the tolerant reader skipped.
    :raises ApiError: if there is at least one.
    """
    if unreadable == 0:
        return
    noun = "entry" if unreadable == 1 else "entries"
    pronoun = "it" if unreadable == 1 else "them"
    raise ApiError(
        422,
        ErrorCode.INVALID_SETTING,
        _scrub(
            f"the stored provider list holds {unreadable} {noun} this build cannot read — "
            f"fix or remove {pronoun} in the settings file before adding, changing or "
            "deleting a provider"
        ),
        {"field": "tts.providers"},
    )


async def _prepare_scrubber(state: AppState, api_key: str) -> None:
    """Register *api_key* before the write that stores it.

    The value has to be known before it reaches the database, not after: any
    statement trace (``sqlite3``'s ``set_trace_callback``, a future driver that
    logs its parameters, a debugger) would print an ``INSERT`` the scrubber had
    not been told about yet. ``aiosqlite`` itself currently logs only function
    names at DEBUG, so this is defence in depth rather than a live leak.

    The registered set becomes the stored secrets plus the incoming key here;
    :func:`refresh_secret_scrubber` replaces it with the store's own set once
    the write has landed.

    :param state: live application state providing the store.
    :param api_key: the key about to be written, never logged itself.
    """
    SECRET_SCRUBBER.set_secrets([*await state.store.all_secret_values(), api_key])


def _contains_key(value: object) -> bool:
    """Return whether *value* carries an ``api_key`` member at any depth.

    :param value: JSON value from the submitted overrides.
    """
    if isinstance(value, Mapping):
        return any(
            str(key) == _SECRET_FIELD or _contains_key(item)
            for key, item in cast(Mapping[object, object], value).items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item) for item in cast(list[object], value))
    return False


def _reject_embedded_key(body: ProviderWriteRequest) -> None:
    """Refuse a request that hides the API key inside the record.

    :param body: decoded request body.
    :raises ApiError: if an ``api_key`` appears below the top-level field.
    """
    if body.overrides is not None and _contains_key(body.overrides):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "the api_key belongs in the top-level api_key field, not inside overrides — "
            "it is stored separately from the provider record",
            {"field": "overrides"},
        )


def _check_api_key(api_key: str | None) -> None:
    """Refuse an emptied API key before anything is written.

    :param api_key: submitted key, or ``None`` when the request did not send one.
    :raises ApiError: if the key is an empty string.
    """
    if api_key == "":
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"an api_key was sent as an empty string — {_EMPTY_SECRET_PROBLEM}",
            {"field": _SECRET_FIELD},
        )


async def _write(state: AppState, payload: list[dict[str, object]]) -> None:
    """Validate the whole resulting list and store it.

    The strict parser runs over the *whole* list, because duplicate names and
    ids cannot be seen from one record. :meth:`ConfigStore.set_setting` clears
    the affected TTS bakes as part of the same write.

    :param state: live application state providing the store.
    :param payload: the complete ``tts.providers`` array to store.
    :raises ApiError: 422 with the parser's own whole sentence.
    """
    try:
        parse_providers(payload, presets=DEFAULT_PRESETS)
    except ProviderError as exc:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            _scrub(str(exc)),
            {"field": "tts.providers"},
        ) from None
    await state.store.set_setting("tts.providers", payload)


def _record_json(record: ProviderRecord) -> dict[str, object]:
    """Return one record as the JSON object ``tts.providers`` stores.

    The field order matches :class:`ProviderResponse` minus ``has_api_key``, so
    the settings file reads like the API answer.

    :param record: validated provider record.
    """
    return {
        "id": record.id,
        "name": record.name,
        "preset": record.preset,
        "base_url": record.base_url,
        "voice": record.voice,
        "language": record.language,
        "paid": record.paid,
        "cost_per_1k_chars_usd": record.cost_per_1k_chars_usd,
        "overrides": dict(record.overrides),
    }


def _shape_row(preset: Preset) -> ProviderShapeResponse:
    """Return one preset's request shape as the wire model.

    :param preset: preset library row.
    """
    shape = preset.shape
    return ProviderShapeResponse(
        method=shape.method,
        path=_scrub(shape.path),
        headers={name: _scrub(value) for name, value in shape.headers.items()},
        auth=AuthStyleResponse(
            style=shape.auth.style,
            name=shape.auth.name,
            prefix=shape.auth.prefix,
        ),
        body_kind=shape.body_kind,
        body=_scrub_json(shape.body),
        response=shape.response,
        format=shape.format,
    )


def _preset_row(preset: Preset) -> PresetSummaryResponse:
    """Return one preset library row for the picker.

    :param preset: preset library row.
    """
    return PresetSummaryResponse(
        id=preset.id,
        label=_scrub(preset.label),
        description=_scrub(preset.description),
        paid=preset.paid,
        format=preset.shape.format,
        cost_per_1k_chars_usd=preset.cost_per_1k_chars_usd,
        pricing_note=_scrub(preset.pricing_note),
        requires_base_url=preset.requires_base_url,
        shape=_shape_row(preset),
    )


def _provider_row(provider: ResolvedProvider) -> ProviderResponse:
    """Return one resolved provider as the wire model.

    :param provider: record plus its preset, shape, address and key.
    """
    record = provider.record
    return ProviderResponse(
        id=record.id,
        name=_scrub(record.name),
        preset=record.preset,
        base_url=_scrub(provider.base_url),
        voice=_scrub(record.voice),
        language=_scrub(record.language),
        paid=provider.paid,
        cost_per_1k_chars_usd=provider.cost_per_1k_chars_usd,
        has_api_key=provider.has_api_key,
        overrides=cast("dict[str, object]", _scrub_json(dict(record.overrides))),
    )


async def _resolved_row(state: AppState, provider_id: str) -> ProviderResponse:
    """Read one provider back through the registry and describe it.

    Re-reading, rather than echoing the request, is what makes a POST or PATCH
    answer identical to the row the list route shows — the effective base URL and
    ``has_api_key`` included.

    :param state: live application state providing the store.
    :param provider_id: id of the provider just written.
    :raises ApiError: if the row cannot be read back.
    """
    for provider in await ConfiguredProviders(state.store).resolved():
        if provider.record.id == provider_id:
            return _provider_row(provider)
    raise ApiError(
        500,
        ErrorCode.INTERNAL_ERROR,
        "the provider was written but could not be read back — check the doorbell log",
    )


def _unreadable_warning(unreadable: int) -> str | None:
    """Return the list route's warning sentence, or ``None`` when all is readable.

    :param unreadable: how many stored entries the tolerant reader skipped.
    """
    if unreadable == 0:
        return None
    if unreadable == 1:
        return _scrub(_UNREADABLE_ONE)
    return _scrub(_UNREADABLE_MANY.format(count=unreadable))


def _price_per_100(provider: ResolvedProvider) -> float | None:
    """Return the provider's price per 100 characters, ``None`` when unset.

    :param provider: the provider under test.
    """
    price = provider.cost_per_1k_chars_usd
    return None if price is None else price / 10.0


def _estimated_cost(provider: ResolvedProvider, text: str) -> float | None:
    """Return what *text* costs through *provider*, ``None`` when unpriced.

    :param provider: the provider under test.
    :param text: the phrase that was spoken.
    """
    price = provider.cost_per_1k_chars_usd
    return None if price is None else len(text) * price / 1000.0


def build_provider_router(state: AppState) -> APIRouter:
    """Build the ``/api/tts/providers`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the store and the borrowed
        HTTP client a provider test may use.
    """
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/providers")
    async def list_providers() -> TtsProvidersResponse:
        """List the configured providers, the presets and the selected id.

        Never fails: an unreadable stored record is skipped and reported in
        ``warning``, exactly like the tolerant read a press does.
        """
        records, unreadable = await _stored(state)
        providers = ConfiguredProviders(state.store)
        resolved = {provider.record.id: provider for provider in await providers.resolved()}
        selected = await state.store.get_setting("tts.http_tts.provider")
        return TtsProvidersResponse(
            providers=[
                _provider_row(resolved[record.id]) for record in records if record.id in resolved
            ],
            presets=[
                _preset_row(DEFAULT_PRESETS[preset_id]) for preset_id in DEFAULT_PRESETS.ids()
            ],
            selected=_scrub(selected) if isinstance(selected, str) else "",
            warning=_unreadable_warning(unreadable),
        )

    @router.post("/providers", status_code=201)
    async def create_provider(body: ProviderWriteRequest) -> ProviderResponse:
        """Add one provider and return it, ``has_api_key`` included.

        The id is minted here; ``paid`` defaults to the preset's own flag so a
        new provider is billed exactly as its preset says until the user says
        otherwise.

        :param body: the record's fields; ``api_key`` is written separately.
        :raises ApiError: if the resulting list is rejected or a key is invalid.
        """
        _reject_embedded_key(body)
        _check_api_key(body.api_key)
        current, unreadable = await _stored(state)
        _refuse_when_unreadable(unreadable)
        provider_id = new_provider_id()
        preset = DEFAULT_PRESETS.get(body.preset) if isinstance(body.preset, str) else None
        record: dict[str, object] = {
            "id": provider_id,
            "name": body.name if body.name is not None else "",
            "preset": body.preset if body.preset is not None else "",
            "base_url": body.base_url if body.base_url is not None else "",
            "voice": body.voice if body.voice is not None else "",
            "language": body.language if body.language is not None else "",
            "paid": body.paid if body.paid is not None else (preset.paid if preset else False),
            "cost_per_1k_chars_usd": body.cost_per_1k_chars_usd,
            "overrides": {} if body.overrides is None else body.overrides,
        }
        payload = [_record_json(item) for item in current]
        payload.append(record)
        await _write(state, payload)
        if body.api_key is not None:
            await _prepare_scrubber(state, body.api_key)
            await state.store.set_provider_secret(provider_id, body.api_key)
            await refresh_secret_scrubber(state.store)
        return await _resolved_row(state, provider_id)

    @router.patch("/providers/{provider_id}")
    async def update_provider(provider_id: str, body: ProviderWriteRequest) -> ProviderResponse:
        """Change some fields of one provider and return the merged row.

        Only the submitted fields change; ``null`` clears the clearable ones.
        A submitted ``id`` is ignored — the path names the record.

        :param provider_id: id from the request path.
        :param body: the fields to change; ``api_key`` is written separately.
        :raises ApiError: 404 for an unknown id, otherwise the usual rejections.
        """
        _reject_embedded_key(body)
        _check_api_key(body.api_key)
        current, unreadable = await _stored(state)
        _refuse_when_unreadable(unreadable)
        existing = next((item for item in current if item.id == provider_id), None)
        if existing is None:
            _not_found(provider_id)
        payload = _record_json(existing)
        fields = body.model_fields_set
        if "name" in fields and body.name is not None:
            payload["name"] = body.name
        if "preset" in fields and body.preset is not None:
            payload["preset"] = body.preset
        if "base_url" in fields:
            payload["base_url"] = body.base_url if body.base_url is not None else ""
        if "voice" in fields:
            payload["voice"] = body.voice if body.voice is not None else ""
        if "language" in fields:
            payload["language"] = body.language if body.language is not None else ""
        if "paid" in fields and body.paid is not None:
            payload["paid"] = body.paid
        if "cost_per_1k_chars_usd" in fields:
            payload["cost_per_1k_chars_usd"] = body.cost_per_1k_chars_usd
        if "overrides" in fields:
            payload["overrides"] = {} if body.overrides is None else body.overrides
        replacements = [
            payload if item.id == provider_id else _record_json(item) for item in current
        ]
        await _write(state, replacements)
        if "api_key" in fields:
            if body.api_key is None:
                await state.store.delete_provider_secret(provider_id)
            else:
                await _prepare_scrubber(state, body.api_key)
                await state.store.set_provider_secret(provider_id, body.api_key)
            await refresh_secret_scrubber(state.store)
        return await _resolved_row(state, provider_id)

    @router.delete("/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> DeleteProviderResponse:
        """Remove one provider, its API key and its selection.

        A deleted provider must not stay silently selected, so a selection that
        pointed at it is cleared back to "none chosen".

        :param provider_id: id from the request path.
        :raises ApiError: 404 for an unknown id.
        """
        current, unreadable = await _stored(state)
        _refuse_when_unreadable(unreadable)
        if not any(item.id == provider_id for item in current):
            _not_found(provider_id)
        await _write(state, [_record_json(item) for item in current if item.id != provider_id])
        await state.store.delete_provider_secret(provider_id)
        selected = await state.store.get_setting("tts.http_tts.provider")
        if selected == provider_id:
            await state.store.set_setting("tts.http_tts.provider", "")
        await refresh_secret_scrubber(state.store)
        return DeleteProviderResponse(deleted=True, id=_scrub(provider_id))

    @router.post("/providers/{provider_id}/test")
    async def test_provider(provider_id: str) -> TtsProviderTestResponse:
        """Speak one fixed phrase through *provider_id* and report the measurement.

        The provider named by the path is tested whether or not it is the
        selected one. A failed synthesis is ``200`` with ``ok: false``, matching
        every other connection test in this API; only an unknown id is a 404.
        The borrowed HTTP client (the same seam the Home Assistant routes use)
        lets a test answer with ``httpx.MockTransport`` without a socket.

        :param provider_id: id from the request path.
        :raises ApiError: 404 when the id names no configured provider.
        """
        providers = ConfiguredProviders(state.store)
        records = await providers.records()
        if not any(record.id == provider_id for record in records):
            _not_found(provider_id)
        chosen = next(
            (item for item in await providers.resolved() if item.record.id == provider_id),
            None,
        )
        if chosen is None:
            return TtsProviderTestResponse(
                ok=False,
                provider=_scrub(provider_id),
                latency_ms=None,
                chars=None,
                format=None,
                cost_per_100_chars_usd=None,
                estimated_cost_usd=None,
                error=_scrub(_UNUSABLE_PROVIDER),
            )
        engine = HttpTtsEngine(state.store, client=state.ha_client, providers=providers)
        try:
            async with asyncio.timeout(PROVIDER_TEST_TIMEOUT_S):
                probe = await engine.probe(chosen, _TEST_PHRASE)
        except TimeoutError:
            return TtsProviderTestResponse(
                ok=False,
                provider=_scrub(provider_id),
                latency_ms=None,
                chars=None,
                format=None,
                cost_per_100_chars_usd=_price_per_100(chosen),
                estimated_cost_usd=None,
                error=_scrub(_TEST_TIMEOUT.format(seconds=PROVIDER_TEST_TIMEOUT_S)),
            )
        except Exception as exc:
            _LOGGER.warning("the TTS provider test failed unexpectedly", exc_info=True)
            return TtsProviderTestResponse(
                ok=False,
                provider=_scrub(provider_id),
                latency_ms=None,
                chars=None,
                format=None,
                cost_per_100_chars_usd=_price_per_100(chosen),
                estimated_cost_usd=None,
                error=_scrub(
                    f"the provider test failed unexpectedly ({type(exc).__name__}) — "
                    "check the doorbell log"
                ),
            )
        return TtsProviderTestResponse(
            ok=probe.ok,
            provider=_scrub(provider_id),
            latency_ms=probe.latency_ms,
            chars=len(_TEST_PHRASE) if probe.ok else None,
            format=probe.format,
            cost_per_100_chars_usd=_price_per_100(chosen),
            estimated_cost_usd=_estimated_cost(chosen, _TEST_PHRASE) if probe.ok else None,
            error=None if probe.error is None else _scrub(probe.error),
        )

    return router
