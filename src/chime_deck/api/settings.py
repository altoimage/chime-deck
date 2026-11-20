"""Settings API: read the effective configuration, change it, test connections.

Canonical paths carry **no trailing slash**: ``GET /api/settings`` answers, while
``GET /api/settings/`` is not a second endpoint — the catch-all UI route claims
the slashed path first, so it answers the standard 404 envelope rather than the
307 redirect a bare router would emit. ``POST`` is claimed the same way: a
slashed test path such as ``POST /api/settings/test/ha/`` answers a 405 with
``Allow: GET`` for the same reason. Clients must use the slash-less form.

Two behaviours are deliberate and must not be "fixed":

* A connection test returns **HTTP 200 with ``ok: false``** when the test ran and
  the connection failed. The request succeeded; ``ok`` carries the verdict. Only
  a malformed request is a 4xx.
* ``system.log_level`` takes effect **on restart**. The stored value is applied
  by the application lifespan, so HSD-bq76 does not promise a live reconfigure.

Secret settings never travel back out: ``GET`` omits their values entirely and
reports only whether one is stored, and ``PATCH`` accepts ``null`` to clear a
secret (stored as ``""``) while rejecting ``""`` with "send null to clear a
secret" so the UI cannot blank a credential by accident. ``""`` stays legal for
the ordinary string settings such as ``audio.device_hint``.

Owning ticket: HSD-91z1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import SECRET_SCRUBBER, refresh_secret_scrubber
from ..store import (
    LOCAL_ONLY_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    SETTINGS,
    SettingTypeError,
    SettingValueError,
    derive_instance_id,
    normalise_value,
)
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..app import AppState

__all__ = [
    "AudioTestResponse",
    "HaTestResponse",
    "MqttTestResponse",
    "SettingsResponse",
    "build_settings_router",
]

_TIMEOUT_TEXT: Final = (
    "the {label} connection test did not answer within {seconds:g} s — check the address and "
    "that the device or broker is reachable"
)
_EMPTY_SECRET_PROBLEM: Final = "send null to clear a secret, not an empty string"
_UNKNOWN_KEY_PROBLEM: Final = "is not a known setting"
_PROVIDERS_MANAGED_ELSEWHERE: Final = (
    "is managed through /api/tts/providers, not the settings patch"
)
_ADMIN_PASSWORD_MANAGED_ELSEWHERE: Final = (
    "is managed through /api/auth/password, not the settings patch"
)

_LOGGER = logging.getLogger(__name__)


class SettingsResponse(BaseModel):
    """Effective settings plus the "a value is stored" flag for each secret."""

    settings: dict[str, object]
    secrets: dict[str, bool]


class HaTestResponse(BaseModel):
    """Result of the Home Assistant connection test."""

    ok: bool
    ha_version: str | None = None
    entity_count: int | None = None
    error: str | None = None


class MqttTestResponse(BaseModel):
    """Result of the MQTT broker connection test."""

    ok: bool
    broker_version: str | None = None
    error: str | None = None
    auth_failed: bool = False
    """Whether the broker rejected the credentials; the classification, not a second sentence."""


class AudioTestResponse(BaseModel):
    """Result of the audio output test."""

    ok: bool
    device: str | None = None
    latency_ms: float | None = None
    error: str | None = None


def _scrub(value: str | None) -> str | None:
    """Redact any secret-looking text before it enters a response model.

    Everything that leaves a response model goes through here, so the rule is
    absolute and visible in one place.

    :param value: text from a probe (or an exception), or ``None`` when there is none.
    """
    return None if value is None else SECRET_SCRUBBER.scrub(value)


def _rejection(problems: list[dict[str, object]]) -> ApiError:
    """Build the 422 that reports every rejected setting at once.

    :param problems: one ``{"field": ..., "reason": ...}`` entry per rejected key.
    """
    names = [str(problem["field"]) for problem in problems]
    if len(problems) == 1:
        message = f"setting {names[0]!r} {problems[0]['reason']}"
    else:
        listed = ", ".join(repr(name) for name in names)
        message = f"{len(problems)} settings were rejected: {listed}"
    unknown_only = all(problem["reason"] == _UNKNOWN_KEY_PROBLEM for problem in problems)
    code = ErrorCode.UNKNOWN_SETTING if unknown_only else ErrorCode.INVALID_SETTING
    return ApiError(422, code, message, {"fields": problems})


def build_settings_router(state: AppState) -> APIRouter:
    """Build the ``/api/settings`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store and the probes.
    """
    router = APIRouter(prefix="/api/settings", tags=["settings"])

    async def _snapshot() -> SettingsResponse:
        """Return the effective settings and the secret flags."""
        stored = await state.store.all_settings()
        settings = {
            key: stored.get(key, spec.default) for key, spec in SETTINGS.items() if not spec.secret
        }
        return SettingsResponse(settings=settings, secrets=await state.store.secret_status())

    def _validated_updates(body: dict[str, object]) -> dict[str, object]:
        """Normalise and validate every submitted setting, or reject the whole patch.

        :param body: decoded request body, keyed by setting key.
        :raises ApiError: if any key or value is rejected; nothing is written then.
        """
        updates: dict[str, object] = {}
        problems: list[dict[str, object]] = []
        for key, value in body.items():
            # The provider array is written only by /api/tts/providers. Refusing
            # it here is what guarantees the generic PATCH can never be a second
            # write path for provider configuration, so an api_key can never
            # arrive through it.
            if key == "tts.providers":
                problems.append({"field": key, "reason": _PROVIDERS_MANAGED_ELSEWHERE})
                continue
            # The configuration-interface password (HSD-n6wi) is written only by
            # /api/auth/password, which enforces the current-password rule. Refusing
            # every LOCAL_ONLY_SETTING_KEYS entry here is what keeps the generic
            # PATCH from being a second write path that could rotate the credential
            # without knowing the old one.
            if key in LOCAL_ONLY_SETTING_KEYS:
                problems.append({"field": key, "reason": _ADMIN_PASSWORD_MANAGED_ELSEWHERE})
                continue
            spec = SETTINGS.get(key)
            if spec is None:
                problems.append({"field": key, "reason": _UNKNOWN_KEY_PROBLEM})
                continue
            if spec.secret and value is None:
                updates[key] = ""
                continue
            if spec.secret and value == "":
                problems.append({"field": key, "reason": _EMPTY_SECRET_PROBLEM})
                continue
            try:
                updates[key] = normalise_value(key, value)
            except SettingTypeError as exc:
                problems.append({"field": key, "reason": f"expects {exc.expected}"})
            except SettingValueError as exc:
                problems.append({"field": key, "reason": exc.reason})
        if problems:
            raise _rejection(problems)
        return updates

    async def _pin_instance_id(updates: dict[str, object]) -> None:
        """Pin ``mqtt.instance_id`` to the slug of a first ``device.name`` write.

        The instance id is the identity root of every discovery identifier
        (``unique_id``, the device identifiers and every topic), so deriving it at
        *use* time would move every Home Assistant entity on a rename, splitting
        history and breaking automations. Instead it is derived exactly once, in
        the same transaction as the name that produced it: after that the name is
        a label and a rename changes only the device page's title (HSD-vwdy).

        An explicitly stored or same-patch ``mqtt.instance_id`` always wins, so the
        operator can pin an identity of their choosing. "Absent or blank" is what
        :meth:`chime_deck.store.ConfigStore.is_setting_stored` exists for:
        :meth:`~chime_deck.store.ConfigStore.get_setting` cannot tell a pinned id
        from the registry default.

        :param updates: already normalised and validated patch, updated in place.
        """
        name = updates.get("device.name")
        if not isinstance(name, str) or "mqtt.instance_id" in updates:
            return
        if await state.store.is_setting_stored("mqtt.instance_id"):
            stored = await state.store.get_setting("mqtt.instance_id")
            if stored != "":
                return
        instance_id = derive_instance_id(name)
        updates["mqtt.instance_id"] = instance_id
        _LOGGER.info(
            "the first device name %r pinned the MQTT instance id to %r; renaming the "
            "device will not move its Home Assistant entities (HSD-vwdy)",
            name,
            instance_id,
        )

    @router.get("")
    async def get_settings() -> SettingsResponse:
        """Return every non-secret setting and whether each secret is stored."""
        return await _snapshot()

    @router.patch("")
    async def patch_settings(body: dict[str, object]) -> SettingsResponse:
        """Apply a set of setting changes in one transaction and echo the result.

        An empty object is a successful no-op. Any rejected key rejects the whole
        patch, so the stored configuration never ends up half-updated.

        The first ``device.name`` write also pins ``mqtt.instance_id`` to the slug
        of that name, in the same transaction, so a later rename cannot move every
        Home Assistant entity (HSD-vwdy). An explicitly stored or same-patch
        ``mqtt.instance_id`` always wins, and the pin never fires again once the id
        is stored.
        """
        updates = _validated_updates(body)
        if not updates:
            return await _snapshot()
        await _pin_instance_id(updates)
        async with state.store.transaction():
            for key, value in updates.items():
                await state.store.set_setting(key, value)
        if SECRET_SETTING_KEYS.intersection(updates):
            await refresh_secret_scrubber(state.store)
        return await _snapshot()

    def _timeout_response(label: str) -> str:
        """Describe a probe that ran out of time.

        :param label: human name of the connection being tested.
        """
        return _TIMEOUT_TEXT.format(label=label, seconds=state.probe_timeout)

    @router.post("/test/ha")
    async def test_ha() -> HaTestResponse:
        """Test the Home Assistant connection; ``ok`` carries the verdict."""
        try:
            async with asyncio.timeout(state.probe_timeout):
                result = await state.probes.test_ha()
        except TimeoutError:
            return HaTestResponse(ok=False, error=_timeout_response("Home Assistant"))
        except Exception as exc:
            return HaTestResponse(ok=False, error=_scrub(str(exc)))
        return HaTestResponse(
            ok=result.ok,
            ha_version=_scrub(result.ha_version),
            entity_count=result.entity_count,
            error=_scrub(result.error),
        )

    @router.post("/test/mqtt")
    async def test_mqtt() -> MqttTestResponse:
        """Test the MQTT broker connection; ``ok`` carries the verdict."""
        try:
            async with asyncio.timeout(state.probe_timeout):
                result = await state.probes.test_mqtt()
        except TimeoutError:
            return MqttTestResponse(ok=False, error=_timeout_response("MQTT"))
        except Exception as exc:
            return MqttTestResponse(ok=False, error=_scrub(str(exc)))
        return MqttTestResponse(
            ok=result.ok,
            broker_version=_scrub(result.broker_version),
            error=_scrub(result.error),
            auth_failed=result.auth_failed,
        )

    @router.post("/test/audio")
    async def test_audio() -> AudioTestResponse:
        """Test the audio device; ``ok`` carries the verdict."""
        try:
            async with asyncio.timeout(state.probe_timeout):
                result = await state.probes.test_audio()
        except TimeoutError:
            return AudioTestResponse(ok=False, error=_timeout_response("audio"))
        except Exception as exc:
            return AudioTestResponse(ok=False, error=_scrub(str(exc)))
        return AudioTestResponse(
            ok=result.ok,
            device=_scrub(result.device),
            latency_ms=result.latency_ms,
            error=_scrub(result.error),
        )

    return router
