"""Buttons API: the chime buttons' CRUD surface, reorder, bake and the UI test action.

Owning ticket: HSD-1drv; save-time and manual baking were added by HSD-dzek.

``GET/POST /api/buttons``, ``PATCH/DELETE /api/buttons/{id}``, ``POST
/api/buttons/reorder``, ``POST /api/buttons/{id}/bake`` and ``POST
/api/buttons/{id}/press`` are the UI's whole contract for chime buttons. The
routes are thin — validation, slug derivation, uniqueness, bake invalidation and
the event all live in :class:`~chime_deck.store.ConfigStore` — but
**save-time validation is the point of this ticket**, so every rule below is
checked here, before the write, with a field-specific rejection.

Error table (business rules answer 422 ``validation_error`` unless stated)::

    rule                                             status/code
    -----------------------------------------------  ----------------------
    a sound button without ``sound_id``              422 validation_error
    ``sound_id`` naming a row that does not exist    422 validation_error
    a TTS button with blank ``tts_text``             422 validation_error
    a TTS button while ``tts.default_engine`` = none 422 invalid_setting (D27)
    a templated phrase on a paid engine, unconfirmed 422 validation_error (D25)
    a slug another button already uses               409 conflict
    a name that derives no slug at all               422 validation_error

``POST /api/buttons/{id}/bake`` re-renders one static phrase by hand. Its
conditions are checked in this order, so the route always states the real
reason instead of translating one:

::

    condition                                         status/code
    ------------------------------------------------  ----------------------
    unknown button id                                 404 not_found
    no speech source is prepared yet                  503 engine_unavailable
    speech deliberately off (D27), per button or not  422 invalid_setting
    a sound button                                    422 validation_error
    a templated phrase (never baked, design 04 §2.2)  422 validation_error
    the engine could not render the phrase            503 engine_unavailable

The D27 row reports ``detail.setting = "tts.default_engine"`` and is answered
**before** any engine is probed: a deliberate switch-off is a configuration,
not an unavailable engine, so there is no fallback chain to walk.

Every business rejection carries ``detail.field`` naming the offending input,
because the UI has to focus that control. Pydantic's own shape rejections
(``ge``/``le`` on the overrides, an unknown ``action_type``) arrive in the
standard ``detail.fields`` list instead, handled by
:mod:`chime_deck.api.errors`. The UI therefore sees two ``422`` shapes and
must name both: this table for rules, pydantic's for shape.

``POST /api/buttons`` answers **201** — the first 201 in this API; every other
successful mutation answers 200. ``DELETE`` answers 200 with
``{"deleted": true, "id": N, "slug": S}`` rather than 204, because a body-less
route would be the odd one out here and its body is what makes the deletion
testable.

``acknowledge_paid_template`` is **write-only**: it is the request-scoped half
of D25, paired with the operator half, ``tts.allow_dynamic_paid``. ``false``
(the default) is today's behaviour: a templated phrase on a paid engine needs
the per-request ``acknowledge_paid_template`` override. ``true`` is the operator
saying templated paid phrases are allowed on this doorbell without the per-save
acknowledgement — the editor still shows the cost, so the policy is a deliberate
switch rather than a hidden one. Both halves are read at save time, never cached.
The override itself is never persisted and never returned by
:class:`ButtonResponse`.

The D27 consequence is deliberate, not an accident: a PATCH of an existing TTS
button is rejected while ``tts.default_engine`` is ``"none"``, because the
merged row is re-validated as a TTS row. That is D27's literal "rejected at
save" rule. A sound button's name can still be changed while speech is off.

HSD-bw8h hand-off: this ticket inherits D27's save-time rejection, and the
editor rule that follows from it — hide the TTS action type while speech is
off, show existing TTS buttons disabled with an explanation, never delete them —
belongs to the UI. It derives that state from ``GET /api/settings``'s
``tts.default_engine``; no extra field is added to :class:`ButtonResponse`.

Slug changes change the MQTT unique id, so a rename makes Home Assistant see a
new entity and leaves the old one to be removed on the next republish. The
mutation must surface that *and* leave the republishing to the bridge, which is
driven by the store's event and never by this module. The honest tension: this
module imports only the MQTT settings loader, its validated type, the two topic
helpers and the configuration error, so the warning it prints cannot lie, but it
never publishes. That is not "the API now knows MQTT exists": the
warning shown to the user names the topic Home Assistant will remove, and it
has to come from the single source of truth that actually publishes it.
``load_mqtt_settings`` is one helper but seven ``get_setting`` calls, so the
list route reads them once for the whole page.

HSD-po1g landed the validation: a slug this module would store that is not
MQTT-safe is refused at save with a 422 naming the rule, and so is an icon that
is not an ``mdi:`` name, because Home Assistant rejects the whole discovery
payload for either. A row written before this change keeps its unsafe slug or
icon, keeps being listed, and keeps the bridge's existing behaviour (discovery
skipped for the slug, ``DEFAULT_BUTTON_ICON`` substituted for the icon with a
WARNING), because only a *write* is refused and the store stays permissive so a
restored bundle is not bricked.

A stale ``sound_id`` kept on a TTS button (the merge never silently drops a
reference the user did not ask to drop) is preserved and ignored by the press
path. The store's ``_fetch_buttons_by_sound``/``delete_sound`` filter on
``sound_id`` alone, so such a button is listed in the
``DELETE /api/sounds/{id}`` 409 and is deleted by ``?confirm=true`` — the 409
names it, so nothing is invisible. That is store errata (HSD-2sem's area),
pinned by a test rather than papered over here.

Per-button ``tts_engine == "none"`` is **not** rejected: the press path and its
tests pin it as a supported deliberate state (D27).

**A save now bakes (D21).** ``create_button`` and ``update_button`` pre-render a
static TTS phrase before they answer, so a first save already shows its
artefact in the 201/200 body. A save whose row already has a current artefact on
disk bakes nothing, so renaming a button or changing its icon never pays for a
second synthesis. That is at most one extra store write — the bake
state, in its own transaction, emitting its own ``buttons`` event — and it
**cannot** be folded into the save: the artefact does not exist until the row
does (the bake directory is named by the button id), so the two writes are
ordered by necessity, not by choice. A bake that fails is reported, never
rejected: the row keeps ``baked_sha = NULL`` and carries the reason in
``baked_error``, because a phrase that cannot be pre-rendered is still a phrase
the doorbell can synthesise on the next press (design 04 §2.3). A templated
phrase is never baked at all, and a sound button has nothing to bake.

``DELETE /api/buttons/{id}`` also removes the button's bake directory,
best-effort: a deleted row must not be resurrected by a directory that refused
to go away, so an ``OSError`` is logged and swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..mqtt import (
    MqttConfigurationError,
    MqttSettings,
    command_topic,
    discovery_topic,
    is_mqtt_safe_icon,
    is_mqtt_safe_slug,
    load_mqtt_settings,
)
from ..press import SOURCE_UI_TEST, PressResult, SpeechUnavailable
from ..store import (
    Button,
    ButtonIn,
    ButtonNotFound,
    SlugConflict,
    SlugDerivationError,
    SoundNotFound,
    derive_slug,
)
from ..tts import BakeNotApplicable, has_template_syntax
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/settings.py``).
    from ..app import AppState

__all__ = [
    "ButtonCreateRequest",
    "ButtonResponse",
    "ButtonUpdateRequest",
    "DeleteButtonResponse",
    "MqttTopics",
    "PressResponse",
    "ReorderButtonsRequest",
    "SlugChange",
    "build_buttons_router",
]

_LOGGER = logging.getLogger(__name__)

_SLUG_CHANGE_WARNING: str = (
    "Home Assistant will see a NEW entity for slug {new!r}; the old entity for slug {old!r} "
    "and its topics are removed on the next republish. Update any automation that references "
    "the old entity."
)
"""What a slug change means for Home Assistant; the UI shows it verbatim."""


class MqttTopics(BaseModel):
    """The two topics one button owns; ``None`` when the broker settings are unusable."""

    command: str
    discovery: str


class SlugChange(BaseModel):
    """What a rename did to the button's MQTT identity."""

    old_slug: str
    new_slug: str
    old_topics: MqttTopics | None
    new_topics: MqttTopics | None
    warning: str


class ButtonResponse(BaseModel):
    """One chime button, in the shape the UI consumes."""

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
    mqtt: MqttTopics | None = None
    slug_change: SlugChange | None = None

    @classmethod
    def from_button(
        cls,
        button: Button,
        *,
        mqtt: MqttTopics | None = None,
        slug_change: SlugChange | None = None,
    ) -> ButtonResponse:
        """Build the response body from a stored button row.

        :param button: row returned by the store.
        :param mqtt: topics the button owns, or ``None`` when unavailable.
        :param slug_change: rename report, when this response follows a rename.
        """
        return cls(
            id=button.id,
            slug=button.slug,
            name=button.name,
            icon=button.icon,
            action_type=button.action_type,
            sound_id=button.sound_id,
            tts_text=button.tts_text,
            tts_engine=button.tts_engine,
            baked_sha=button.baked_sha,
            baked_format=button.baked_format,
            baked_at=button.baked_at,
            baked_error=button.baked_error,
            volume_override=button.volume_override,
            dedupe_seconds=button.dedupe_seconds,
            show_on_dashboard=button.show_on_dashboard,
            quiet_hours_exempt=button.quiet_hours_exempt,
            enabled=button.enabled,
            sort_order=button.sort_order,
            created_at=button.created_at,
            updated_at=button.updated_at,
            mqtt=mqtt,
            slug_change=slug_change,
        )


class ButtonCreateRequest(BaseModel):
    """Body of ``POST /api/buttons``."""

    name: str
    action_type: Literal["sound", "tts"]
    slug: str | None = None
    icon: str | None = None
    sound_id: int | None = None
    tts_text: str | None = None
    tts_engine: str | None = None
    volume_override: int | None = Field(default=None, ge=0, le=100)
    dedupe_seconds: int | None = Field(default=None, ge=0, le=3600)
    show_on_dashboard: bool = False
    quiet_hours_exempt: bool = False
    enabled: bool = True
    acknowledge_paid_template: bool = False


class ButtonUpdateRequest(BaseModel):
    """Body of ``PATCH /api/buttons/{id}``; every field is optional.

    Omitted and explicit ``null`` differ: omitting a field keeps the stored
    value, while ``null`` clears a nullable field (``icon``, ``sound_id``,
    ``tts_text``, ``tts_engine``, an override) and keeps a stored value for the
    non-nullable ones (``name``, ``action_type``, the booleans, the slug).
    """

    name: str | None = None
    slug: str | None = None
    icon: str | None = None
    action_type: Literal["sound", "tts"] | None = None
    sound_id: int | None = None
    tts_text: str | None = None
    tts_engine: str | None = None
    volume_override: int | None = Field(default=None, ge=0, le=100)
    dedupe_seconds: int | None = Field(default=None, ge=0, le=3600)
    show_on_dashboard: bool | None = None
    quiet_hours_exempt: bool | None = None
    enabled: bool | None = None
    acknowledge_paid_template: bool = False


class ReorderButtonsRequest(BaseModel):
    """Body of ``POST /api/buttons/reorder``."""

    ids: list[int]


class DeleteButtonResponse(BaseModel):
    """Outcome of one button deletion."""

    deleted: bool
    id: int
    slug: str


class PressResponse(BaseModel):
    """What one press did, in the shape the press log recorded it."""

    button_id: int | None
    button_name: str
    source: str
    outcome: str
    pressed_at: str
    code: str | None = None
    detail: str | None = None
    synthesis_ms: int | None = None

    @classmethod
    def from_result(cls, result: PressResult) -> PressResponse:
        """Build the response body from a press result.

        :param result: what the press service decided and recorded.
        """
        return cls(
            button_id=result.button_id,
            button_name=result.button_name,
            source=result.source,
            outcome=result.outcome.value,
            pressed_at=result.pressed_at,
            code=None if result.code is None else result.code.value,
            detail=result.detail,
            synthesis_ms=result.synthesis_ms,
        )


def _not_found(button_id: int) -> ApiError:
    """Build the 404 for an id that names no button.

    :param button_id: primary key that was requested.
    """
    return ApiError(
        404,
        ErrorCode.NOT_FOUND,
        f"no button has the id {button_id}",
        {"id": button_id},
    )


def _save_rejection(exc: SlugConflict | SlugDerivationError) -> ApiError:
    """Translate a slug failure from the store into the API's field-specific 422/409.

    :param exc: conflict or derivation failure raised by ``upsert_button``.
    """
    if isinstance(exc, SlugConflict):
        return ApiError(
            409,
            ErrorCode.CONFLICT,
            f"the slug {exc.slug!r} is already used by another button — "
            "choose a different name or slug",
            {"field": "slug", "slug": exc.slug},
        )
    return ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        "this name produced no usable slug — use at least one letter or digit, "
        "or set an explicit slug",
        {"field": "name"},
    )


_T = TypeVar("_T")

_SPEECH_OFF_BAKE: str = (
    "speech is disabled (tts.default_engine is 'none') — set tts.default_engine to a speech "
    "engine in Settings before pre-rendering a phrase (D27 has no fallback to walk)"
)
"""The explicit D27 sentence a manual bake earns when speech is switched off."""


async def _bake_after_save(state: AppState, button: Button) -> Button:
    """Pre-render a static TTS phrase, never failing the save (D21).

    Called after the row is written and before the response is built, so a first
    save already answers with its artefact. Nothing is baked when there is no
    speech source, when speech is deliberately off (D27), when the button is not
    a TTS button, or when the row's bake is already current and still on disk
    (see :func:`_bake_is_current`) — an icon or volume edit must not re-synthesise
    the phrase. A phrase that cannot be pre-rendered (blank text, a Home
    Assistant template) leaves the row untouched.

    A render failure is recorded on the button by
    :meth:`~chime_deck.tts.speech.TtsSpeechSource.bake_button` and then
    **ignored here**: the save has already committed, and a phrase that cannot
    be pre-rendered is still synthesised on the next press (design 04 §2.3), so
    reporting the failure is the whole job. The row is always re-read, because
    the bake state lives in a second write of its own.

    :param state: live application state providing the store and the resolver.
    :param button: the row that was just written.
    :returns: the row to answer with, refreshed when a bake was attempted.
    """
    speech = state.speech
    if speech is None or speech.is_disabled or button.action_type != "tts":
        return button
    if _bake_is_current(state, button):
        return button
    try:
        await speech.bake_button(button=button)
    except BakeNotApplicable:
        return button
    except SpeechUnavailable:
        pass
    refreshed = await state.store.get_button_by_id(button.id)
    return refreshed or button


def _bake_is_current(state: AppState, button: Button) -> bool:
    """Return whether *button*'s recorded bake is current and still on disk.

    The row can only hold a digest the store did not clear, and the store clears
    it in the same transaction as a ``tts_text``/``tts_engine`` edit or a TTS
    setting change, so a present ``baked_sha`` already means "nothing invalidated
    this". The extra existence check covers the one thing the row cannot know:
    an artefact removed behind its back, which must be re-rendered, not trusted.

    :param state: live application state providing the bake store.
    :param button: the row that was just written.
    """
    bakes = state.bake_store
    if bakes is None or button.baked_sha is None:
        return False
    try:
        return bakes.path_for(button.id, button.baked_sha) is not None
    except ValueError:
        return False


def _pick(changes: Mapping[str, object], key: str, current: _T) -> _T:
    """Return a submitted non-null value, or the stored one when omitted or null.

    :param changes: body fields that were set, from ``model_dump(exclude_unset=True)``.
    :param key: body field name.
    :param current: value stored on the existing row.
    """
    value = changes.get(key, current)
    return current if value is None else cast(_T, value)


def _nullable(changes: Mapping[str, object], key: str, current: _T) -> _T:
    """Return the submitted value, or the stored one when the key was omitted.

    An explicit ``null`` is returned as ``None``, which is how a nullable field
    is cleared even though ``model_dump(exclude_unset=True)`` kept it in the body.

    :param changes: body fields that were set, from ``model_dump(exclude_unset=True)``.
    :param key: body field name.
    :param current: value stored on the existing row.
    """
    return cast(_T, changes.get(key, current))


def _merged_button(existing: Button, changes: Mapping[str, object]) -> ButtonIn:
    """Merge a PATCH body over the stored row into one complete write input.

    ``slug`` keeps its ``ButtonIn`` semantics: an explicit ``null`` (or an
    omitted field) preserves the stored slug, and only a different string
    renames it. ``sort_order`` is ``None`` so the store's
    ``COALESCE(?, sort_order)`` keeps the stored position. A stale ``sound_id``
    on a TTS button is preserved, never silently dropped.

    :param existing: row read before the patch.
    :param changes: body fields that were set, without ``acknowledge_paid_template``.
    """
    return ButtonIn(
        id=existing.id,
        name=_pick(changes, "name", existing.name),
        action_type=_pick(changes, "action_type", existing.action_type),
        slug=_nullable(changes, "slug", existing.slug),
        icon=_nullable(changes, "icon", existing.icon),
        sound_id=_nullable(changes, "sound_id", existing.sound_id),
        tts_text=_nullable(changes, "tts_text", existing.tts_text),
        tts_engine=_nullable(changes, "tts_engine", existing.tts_engine),
        volume_override=_nullable(changes, "volume_override", existing.volume_override),
        dedupe_seconds=_nullable(changes, "dedupe_seconds", existing.dedupe_seconds),
        show_on_dashboard=_pick(changes, "show_on_dashboard", existing.show_on_dashboard),
        quiet_hours_exempt=_pick(changes, "quiet_hours_exempt", existing.quiet_hours_exempt),
        enabled=_pick(changes, "enabled", existing.enabled),
        sort_order=None,
    )


async def _validate_save(state: AppState, button: ButtonIn, *, acknowledge: bool) -> None:
    """Reject a save that breaks a business rule, before anything is written.

    The sound rules are scoped to ``action_type == "sound"`` and the D25 rule to
    ``action_type == "tts"`` on purpose: ``tts_text`` and ``sound_id`` survive an
    action-type switch, so a sound button keeping a templated phrase must still
    save, and a TTS button keeping a **live** ``sound_id`` must still save. (A
    deleted sound nulls that column through the foreign key, so a dangling id is
    not a state the store can hold.)

    :param state: live application state providing the store and the paid lookup.
    :param button: the complete row that would be written.
    :param acknowledge: the request's explicit D25 override.
    :raises ApiError: the first rule the row breaks.
    """
    if button.action_type == "sound":
        if button.sound_id is None:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "a sound button requires a sound_id — choose a sound from the library, "
                "or make this button speak",
                {"field": "sound_id"},
            )
        try:
            await state.store.get_sound(button.sound_id)
        except SoundNotFound:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                f"the sound with id {button.sound_id} no longer exists — "
                "choose another sound or change this button to speech",
                {"field": "sound_id", "sound_id": button.sound_id},
            ) from None
        return

    if button.action_type != "tts":
        # A corrupt stored action type is the store's validation to report.
        return

    text = button.tts_text or ""
    if not text.strip():
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "a TTS button requires non-empty tts_text — type the phrase to speak",
            {"field": "tts_text"},
        )
    default_engine = await state.store.get_setting("tts.default_engine")
    if default_engine == "none":
        raise ApiError(
            422,
            ErrorCode.INVALID_SETTING,
            "speech is disabled (tts.default_engine is 'none') — set tts.default_engine "
            "to a speech engine in Settings, or make this button play a sound",
            {"field": "action_type", "setting": "tts.default_engine"},
        )
    engine = button.tts_engine or str(default_engine)
    allow_paid_templates = bool(await state.store.get_setting("tts.allow_dynamic_paid"))
    if (
        not acknowledge
        and not allow_paid_templates
        and has_template_syntax(text)
        and await state.paid_providers.is_paid(engine)
    ):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"tts_text contains template syntax and engine {engine!r} bills for every "
            "character — pre-render a static phrase, or resend with "
            "acknowledge_paid_template=true to accept the cost",
            {"field": "tts_text", "engine": engine, "requires": "acknowledge_paid_template"},
        )


def _validate_topics(button: ButtonIn, previous: Button | None) -> None:
    """Reject a save whose slug or icon MQTT could not carry, before the write.

    Home Assistant checks the command and discovery topics' grammar and rejects
    the *entire* discovery payload for an icon that is not an ``mdi:`` name, so
    both are refused here with a 422 naming the field and the rule (HSD-po1g).

    The effective slug is ``button.slug`` when it is not ``None``, otherwise the
    stored ``previous.slug`` (the store preserves it; an update never
    re-derives), otherwise ``derive_slug(button.name)`` for a create with no
    slug. A derived slug is always charset-safe, so its only possible failure is
    the 128-character bound.

    On a create this helper itself derives the slug with ``derive_slug(name)``
    and the store derives it again at write time **from the same function**, so
    the validated value and the written value cannot differ. On an update it
    never derives: it only inspects the merged ``button.slug``. The store is
    never asked to validate.

    Two grandfather clauses are what keep existing installs editable: a slug
    equal to ``previous.slug`` is accepted unchecked (an untouched, or
    deliberately resent, legacy slug), and the icon is skipped entirely when it
    is unchanged from the stored row (a legacy unsafe icon).

    :param button: the complete row that would be written.
    :param previous: the stored row being updated, or ``None`` for a create.
    :raises ApiError: the first unsafe value, naming its field.
    :raises SlugDerivationError: if a create's name derives no slug at all.
    """
    derived = False
    if button.slug is not None:
        effective = button.slug
    elif previous is not None:
        effective = previous.slug
    else:
        effective = derive_slug(button.name)
        derived = True

    unchanged = previous is not None and effective == previous.slug
    if not unchanged and not is_mqtt_safe_slug(effective):
        if derived:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "this name produces a slug longer than 128 characters for an MQTT topic — "
                "shorten the name, or set an explicit slug",
                {"field": "name", "slug": effective},
            )
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"the slug {effective!r} cannot be used in an MQTT topic — use lowercase a-z, "
            "digits, hyphens and underscores, 1 to 128 characters",
            {"field": "slug", "slug": effective},
        )

    if previous is not None and button.icon == previous.icon:
        return
    if not is_mqtt_safe_icon(button.icon):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"the icon {button.icon!r} cannot be used on a button — use an 'mdi:<name>' "
            "icon such as 'mdi:doorbell'",
            {"field": "icon", "icon": button.icon},
        )


async def _discard_bake(state: AppState, button_id: int) -> None:
    """Remove a deleted button's bake directory, best-effort.

    The row is already gone, so a directory that refuses to disappear is logged
    and otherwise ignored: deleting the button is the user's intent, and the
    startup sweep removes the orphan on the next boot. The resolver is preferred
    when it exists because it is the one object that owns the bake store; the
    store is the fallback for a process whose speech source never started.

    :param state: live application state providing the resolver or the store.
    :param button_id: id of the button that was just deleted.
    """
    try:
        if state.speech is not None:
            await state.speech.discard_bake(button_id)
        elif state.bake_store is not None:
            await state.bake_store.discard(button_id)
    except OSError as exc:
        _LOGGER.warning(
            "could not remove the bake directory of button %s: %s: %s",
            button_id,
            type(exc).__name__,
            exc,
        )


async def _mqtt_settings(state: AppState) -> MqttSettings | None:
    """Read the broker settings without letting a broken value fail the request.

    A save has already committed by the time this runs: a bad broker setting must
    never turn a successful save into a 500, and never undo the write.

    :param state: live application state providing the store.
    """
    try:
        return await load_mqtt_settings(state.store)
    except MqttConfigurationError as exc:
        _LOGGER.warning("button topics are unavailable until the MQTT settings are fixed: %s", exc)
        return None


def _topics(settings: MqttSettings | None, slug: str) -> MqttTopics | None:
    """Return the topics *slug* owns, or ``None`` when the settings are unusable.

    :param settings: validated broker settings, or ``None``.
    :param slug: button slug.
    """
    if settings is None:
        return None
    return MqttTopics(
        command=command_topic(settings, slug),
        discovery=discovery_topic(settings, slug),
    )


def build_buttons_router(state: AppState) -> APIRouter:
    """Build the ``/api/buttons`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store and the press path.
    """
    router = APIRouter(prefix="/api/buttons", tags=["buttons"])

    @router.get("")
    async def list_buttons() -> list[ButtonResponse]:
        """Return every button in ``sort_order`` order, with its MQTT topics.

        The broker settings are read once for the whole page.
        """
        settings = await _mqtt_settings(state)
        return [
            ButtonResponse.from_button(button, mqtt=_topics(settings, button.slug))
            for button in await state.store.list_buttons()
        ]

    @router.post("", status_code=201)
    async def create_button(body: ButtonCreateRequest) -> ButtonResponse:
        """Create one button and answer **201** with the stored row.

        A static TTS phrase is pre-rendered before the answer (D21), so the body
        already carries its bake state. That bake is one extra store write in its
        own transaction and its own ``buttons`` event; it can never fail this
        save, and a failure is reported in ``baked_error`` instead.

        :param body: fields of the new button.
        :raises ApiError: if a business rule or the slug is rejected.
        """
        button = ButtonIn(
            name=body.name,
            action_type=body.action_type,
            slug=body.slug,
            icon=body.icon,
            sound_id=body.sound_id,
            tts_text=body.tts_text,
            tts_engine=body.tts_engine,
            volume_override=body.volume_override,
            dedupe_seconds=body.dedupe_seconds,
            show_on_dashboard=body.show_on_dashboard,
            quiet_hours_exempt=body.quiet_hours_exempt,
            enabled=body.enabled,
        )
        await _validate_save(state, button, acknowledge=body.acknowledge_paid_template)
        try:
            # A create derives the slug here and the store derives it again from
            # the same function, so a name with no usable slug is still reported
            # by ``_save_rejection`` exactly as the store's own failure would be.
            _validate_topics(button, previous=None)
            saved = await state.store.upsert_button(button)
        except (SlugConflict, SlugDerivationError) as exc:
            raise _save_rejection(exc) from None
        settings = await _mqtt_settings(state)
        saved = await _bake_after_save(state, saved)
        return ButtonResponse.from_button(saved, mqtt=_topics(settings, saved.slug))

    @router.post("/reorder")
    async def reorder_buttons(body: ReorderButtonsRequest) -> dict[str, list[int]]:
        """Renumber the buttons in the submitted order.

        Ids that are not mentioned keep their relative order after the listed
        ones, so a partial list is a valid move-to-front operation.

        :param body: button ids in their new order.
        :raises ApiError: if any id names no button; nothing is written then.
        """
        try:
            await state.store.reorder_buttons(body.ids)
        except ButtonNotFound as exc:
            raise _not_found(int(exc.button)) from None
        return {"ids": body.ids}

    @router.patch("/{button_id}")
    async def update_button(button_id: int, body: ButtonUpdateRequest) -> ButtonResponse:
        """Apply a partial update to one button.

        The body is merged over the stored row, the complete row is validated,
        and only then written, so a rejected patch changes nothing and emits no
        event. A rename reports the old and new MQTT topics. An edit that
        invalidates the bake (a changed ``tts_text``/``tts_engine``) clears it in
        the save's own transaction and is then baked again; an edit that does not
        touch the phrase leaves the existing artefact alone.

        :param button_id: primary key of the button to change.
        :param body: fields to change; omitted fields keep their stored value.
        :raises ApiError: if the button does not exist or a rule is rejected.
        """
        before = await state.store.get_button_by_id(button_id)
        if before is None:
            raise _not_found(button_id)
        changes = body.model_dump(exclude_unset=True, exclude={"acknowledge_paid_template"})
        button = _merged_button(before, changes)
        await _validate_save(state, button, acknowledge=body.acknowledge_paid_template)
        try:
            # An update never derives: the merged row carries the stored slug
            # the store would keep, so only a changed value is checked.
            _validate_topics(button, previous=before)
            saved = await state.store.upsert_button(button)
        except (SlugConflict, SlugDerivationError) as exc:
            raise _save_rejection(exc) from None

        settings = await _mqtt_settings(state)
        saved = await _bake_after_save(state, saved)
        slug_change = None
        if saved.slug != before.slug:
            slug_change = SlugChange(
                old_slug=before.slug,
                new_slug=saved.slug,
                old_topics=_topics(settings, before.slug),
                new_topics=_topics(settings, saved.slug),
                warning=_SLUG_CHANGE_WARNING.format(new=saved.slug, old=before.slug),
            )
        return ButtonResponse.from_button(
            saved,
            mqtt=_topics(settings, saved.slug),
            slug_change=slug_change,
        )

    @router.delete("/{button_id}")
    async def delete_button(button_id: int) -> DeleteButtonResponse:
        """Delete one button and name it in the response.

        The row is read first so the response can carry the slug the MQTT bridge
        needs to retract the discovery topic; a concurrent double delete still
        maps through :class:`~chime_deck.store.ButtonNotFound` to a 404. The
        bake directory goes with the row, best-effort: a directory that cannot be
        removed is logged, never resurrects the row.

        :param button_id: primary key of the button to delete.
        :raises ApiError: if no button has that id.
        """
        before = await state.store.get_button_by_id(button_id)
        if before is None:
            raise _not_found(button_id)
        try:
            await state.store.delete_button(button_id)
        except ButtonNotFound as exc:
            raise _not_found(int(exc.button)) from None
        await _discard_bake(state, button_id)
        return DeleteButtonResponse(deleted=True, id=before.id, slug=before.slug)

    @router.post("/{button_id}/bake")
    async def bake_button(button_id: int) -> ButtonResponse:
        """Pre-render one button's static phrase now, and answer the stored row.

        This is the manual form of what a save does: the panel shows the artefact
        and its failure reason, and an operator can retry without editing the
        phrase. Every refusal is answered before a render is paid for.

        :param button_id: primary key of the button to bake.
        :raises ApiError: if the button does not exist, speech is off or not
            prepared, the phrase cannot be baked, or the engine could not
            render it.
        """
        button = await state.store.get_button_by_id(button_id)
        if button is None:
            raise _not_found(button_id)
        speech = state.speech
        if speech is None:
            raise ApiError(
                503,
                ErrorCode.ENGINE_UNAVAILABLE,
                "no speech source is prepared yet; check the doorbell log",
            )
        if (
            speech.is_disabled
            or (button.tts_engine or "").strip() == "none"
            or await state.store.get_setting("tts.default_engine") == "none"
        ):
            raise ApiError(
                422,
                ErrorCode.INVALID_SETTING,
                _SPEECH_OFF_BAKE,
                {"field": "tts.default_engine", "setting": "tts.default_engine"},
            )
        try:
            await speech.bake_button(button=button)
        except BakeNotApplicable as exc:
            if button.action_type != "tts":
                detail: dict[str, object] = {"field": "action_type"}
            else:
                detail = {"field": "tts_text", "reason": "dynamic_phrase"}
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc), detail) from None
        except SpeechUnavailable as exc:
            raise ApiError(503, ErrorCode.ENGINE_UNAVAILABLE, str(exc)) from None
        refreshed = await state.store.get_button_by_id(button_id)
        saved = refreshed or button
        settings = await _mqtt_settings(state)
        return ButtonResponse.from_button(saved, mqtt=_topics(settings, saved.slug))

    @router.post("/{button_id}/press")
    async def press_button(button_id: int) -> PressResponse:
        """Press one button as the UI's test action.

        :param button_id: primary key of the button to press.
        :raises ApiError: if no button has that id.
        """
        button = await state.store.get_button_by_id(button_id)
        if button is None:
            raise ApiError(
                404,
                ErrorCode.NOT_FOUND,
                f"no button has the id {button_id}",
                {"id": button_id},
            )
        result = await state.chime.press.press(button.slug, source=SOURCE_UI_TEST)
        return PressResponse.from_result(result)

    return router
