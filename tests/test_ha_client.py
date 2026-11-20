"""Fully offline tests for the Home Assistant REST client.

Every request is served by ``httpx.MockTransport``; no test resolves a name, opens a
socket or talks to a real Home Assistant. Each test therefore also proves the client's
own logic: URL building, the one-retry rule, the timeout arithmetic, the error mapping,
and the promise that the long-lived token never reaches a message, a ``repr`` or a log
line. Owning ticket: HSD-8qf6.
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, cast

import httpx
import pytest
from log_isolation import preserve_log_state

from chime_deck.config import REDACTED, SECRET_SCRUBBER
from chime_deck.ha import (
    DEFAULT_READ_TIMEOUT_S,
    HaApiStatus,
    HaAuthError,
    HaBadRequestError,
    HaBadResponseError,
    HaConfigurationError,
    HaConnectionError,
    HaError,
    HaNotFoundError,
    HaServerError,
    HaState,
    HaTimeoutError,
    HaTtsUrl,
    HomeAssistantClient,
    normalise_base_url,
    redact_headers,
)

pytestmark = pytest.mark.asyncio

TOKEN: Final = "secret-long-lived-token-abc123"
"""Any occurrence of this string in a message, a repr or a log record is a failure."""

BASE_URL: Final = "http://ha.test:8123"

_SLOW_HANDLER_S: Final = 0.2
"""How long the deliberately slow handler sleeps; far longer than the budget below."""

_BUDGET_S: Final = 0.05
"""Per-call budget used by the total-budget tests."""

_RETRY_DELAY_S: Final = 0.05
"""Retry delay used by the one test that measures the sleep."""

_STATE_FRONT_DOOR: Final = {
    "entity_id": "sensor.front_door",
    "state": "open",
    "attributes": {"friendly_name": "Front Door", "battery": 91},
    "last_changed": "2024-05-01T10:00:00+00:00",
    "last_updated": "2024-05-01T10:00:05+00:00",
    "context": {"id": "01HZX", "parent_id": None, "user_id": None},
    "last_reported": "2024-05-01T10:00:05+00:00",
}
"""A real ``/api/states`` entry, including the keys this client must ignore."""

_STATE_GARAGE: Final = {
    "entity_id": "cover.garage",
    "state": "closed",
    "attributes": {},
    "last_changed": "2024-05-01T09:00:00+00:00",
    "last_updated": "2024-05-01T09:00:00+00:00",
}


def _assert_token_absent(text: str) -> None:
    """Fail if the long-lived token appears in *text*.

    The message deliberately does not echo *text*: this helper is used on the very
    strings it is meant to protect.

    :param text: any message, repr or traceback that leaves the client.
    :raises AssertionError: if the token is in *text*.
    """
    if TOKEN in text:
        raise AssertionError("the Home Assistant token appears in text that leaves the client")


@dataclass
class Scripted:
    """Replay a list of responses/exceptions and record every request."""

    script: list[httpx.Response | Exception]
    delay_s: float = 0.0
    requests: list[httpx.Request] = field(default_factory=list)
    timeouts: list[dict[str, float]] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it with the next scripted item.

        :param request: the request httpx is about to send.
        :raises AssertionError: if the client sent more requests than the script allows.
        """
        self.requests.append(request)
        timeout = request.extensions.get("timeout")
        if isinstance(timeout, dict):
            self.timeouts.append(cast("dict[str, float]", timeout))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not self.script:
            raise AssertionError("the client sent more requests than the script allows")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(json_body: object | None = None, *, text: str | None = None) -> httpx.Response:
    """Build an unattached ``200`` answer.

    :param json_body: decoded JSON to serialise into the body.
    :param text: plain-text body, used instead of *json_body* when given.
    """
    if text is not None:
        return httpx.Response(200, text=text)
    return httpx.Response(200, json=json_body)


def err(
    status: int,
    body: str = "",
    *,
    message: str | None = None,
    location: str | None = None,
) -> httpx.Response:
    """Build an unattached non-2xx answer.

    :param status: status code of the answer.
    :param body: plain-text body; 401/404 bodies from real Home Assistant are plain text.
    :param message: when given, sent as ``{"message": ...}`` instead of *body*.
    :param location: when given, sent as the ``Location`` header of a redirect.
    """
    headers = {"Location": location} if location is not None else None
    if message is not None:
        return httpx.Response(status, json={"message": message}, headers=headers)
    return httpx.Response(status, text=body, headers=headers)


@asynccontextmanager
async def client_for(
    handler: Scripted,
    *,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    retry_delay_s: float = 0.0,
) -> AsyncIterator[HomeAssistantClient]:
    """Yield a client wired to *handler* and close the borrowed HTTP client afterwards.

    The borrowing matters: ``filterwarnings = ["error"]`` turns an unclosed
    ``httpx.AsyncClient`` into a failure, so every raw client is closed here.

    :param handler: the transport handler that answers every request.
    :param read_timeout_s: read timeout the client is built with.
    :param retry_delay_s: retry delay the client is built with; almost always zero.
    """
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HomeAssistantClient(
        base_url=BASE_URL,
        token=TOKEN,
        client=raw,
        retry_delay_s=retry_delay_s,
        read_timeout_s=read_timeout_s,
    )
    try:
        yield client
    finally:
        await raw.aclose()


# --------------------------------------------------------------------------- base URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.0.5:8123", "http://192.168.0.5:8123"),
        ("localhost:8123", "http://localhost:8123"),
        ("homeassistant.local:8123/", "http://homeassistant.local:8123"),
        ("http://ha.test:8123/", "http://ha.test:8123"),
        ("HTTPS://HA.Test:8123", "https://HA.Test:8123"),
        ("http://api", "http://api"),
        ("http://ha.test:8123/api", BASE_URL),
        ("http://ha.test:8123/api/", BASE_URL),
        ("http://ha.test:8123/api/api", BASE_URL),
        ("http://ha.test:8123/myapi", "http://ha.test:8123/myapi"),
        ("http://ha.test:8123/apiary", "http://ha.test:8123/apiary"),
        ("http://ha.test:8123/homeassistant", "http://ha.test:8123/homeassistant"),
        (f"  {BASE_URL}  ", BASE_URL),
    ],
)
async def test_normalise_base_url_rewrites_the_stored_value(raw: str, expected: str) -> None:
    """Every documented spelling must land on the prefix the endpoints are joined to."""
    assert normalise_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "http://ha test",
        "http://ha.test:8123/?a=1",
        "http://ha.test:8123/#frag",
        "ftp://ha.test",
        "ws://ha.test:8123",
        "http://",
        "http://ha.test:abcd",
        "http://ha.test:0",
        "http://user:pass@ha.test:8123",
        "http://user@ha.test:8123",
        "http://ha.test:",
    ],
)
async def test_normalise_base_url_rejects_unusable_values(raw: str) -> None:
    """A value that cannot be an http(s) base URL is a configuration error."""
    with pytest.raises(HaConfigurationError):
        normalise_base_url(raw)


async def test_a_reverse_proxy_prefix_survives_and_is_used_by_states() -> None:
    """A prefix that is not ``/api`` must be kept, and every call must go through it."""
    handler = Scripted([ok([_STATE_FRONT_DOOR])])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = HomeAssistantClient(
            base_url="http://ha.test:8123/homeassistant",
            token=TOKEN,
            client=raw,
            retry_delay_s=0.0,
        )
        await client.states()
    finally:
        await raw.aclose()
    assert str(handler.requests[0].url) == "http://ha.test:8123/homeassistant/api/states"


async def test_construction_opens_no_socket() -> None:
    """Building a client for an unresolvable name must not raise until a call is awaited."""
    handler = Scripted([])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = HomeAssistantClient(
            base_url="http://does-not-exist.invalid:8123",
            token=TOKEN,
            client=raw,
            retry_delay_s=0.0,
        )
    finally:
        await raw.aclose()
    assert isinstance(client, HomeAssistantClient)
    assert handler.requests == []


# ------------------------------------------------------------------------ success paths


async def test_api_status_returns_the_message_and_sends_one_bearer_request() -> None:
    """``GET /api/`` keeps its trailing slash and carries the token only in the header."""
    handler = Scripted([ok({"message": "API running."})])
    async with client_for(handler) as client:
        status = await client.api_status()
    assert status == HaApiStatus(message="API running.")
    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE_URL}/api/"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_states_parses_every_entry_and_ignores_unknown_keys() -> None:
    """Known fields are reduced; ``context`` and ``last_reported`` are dropped."""
    handler = Scripted([ok([dict(_STATE_FRONT_DOOR), dict(_STATE_GARAGE)])])
    async with client_for(handler) as client:
        states = await client.states()
    assert states == [
        HaState(
            entity_id="sensor.front_door",
            state="open",
            attributes={"friendly_name": "Front Door", "battery": 91},
            last_changed="2024-05-01T10:00:00+00:00",
            last_updated="2024-05-01T10:00:05+00:00",
        ),
        HaState(
            entity_id="cover.garage",
            state="closed",
            attributes={},
            last_changed="2024-05-01T09:00:00+00:00",
            last_updated="2024-05-01T09:00:00+00:00",
        ),
    ]
    assert str(handler.requests[0].url) == f"{BASE_URL}/api/states"


async def test_state_fetches_one_entity() -> None:
    """``GET /api/states/<id>`` returns the parsed state and uses the documented URL."""
    handler = Scripted([ok(_STATE_FRONT_DOOR)])
    async with client_for(handler) as client:
        parsed = await client.state("sensor.front_door")
    assert parsed.entity_id == "sensor.front_door"
    assert parsed.state == "open"
    assert parsed.attributes["battery"] == 91
    assert str(handler.requests[0].url) == f"{BASE_URL}/api/states/sensor.front_door"


async def test_state_percent_quotes_the_entity_id() -> None:
    """An id containing a slash must be quoted into a single path segment."""
    handler = Scripted([ok(_STATE_FRONT_DOOR)])
    async with client_for(handler) as client:
        await client.state("light.kitchen/main")
    raw_path = handler.requests[0].url.raw_path
    assert raw_path.endswith(b"/api/states/light.kitchen%2Fmain")


async def test_state_attributes_are_read_only() -> None:
    """A frozen state object must not hand out a mapping the caller can mutate."""
    handler = Scripted([ok(_STATE_FRONT_DOOR)])
    async with client_for(handler) as client:
        parsed = await client.state("sensor.front_door")
    attributes = cast(dict[str, object], parsed.attributes)
    with pytest.raises(TypeError):
        operator.setitem(attributes, "x", 1)


async def test_render_template_returns_the_plain_text_body_verbatim() -> None:
    """The answer is Home Assistant's text, not JSON, and the body carries the template."""
    template = "The garage is {{ states('cover.garage') }}"
    handler = Scripted([ok(text="The garage is open")])
    async with client_for(handler) as client:
        rendered = await client.render_template(template)
    assert rendered == "The garage is open"
    request = handler.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/api/template"
    assert json.loads(request.content) == {"template": template}


async def test_render_template_treats_an_empty_body_as_a_valid_empty_render() -> None:
    """Design 04 §5 relies on ``trigger.*`` phrases rendering empty, not failing."""
    handler = Scripted([ok(text="")])
    async with client_for(handler) as client:
        rendered = await client.render_template("{{ trigger.to_state }}")
    assert rendered == ""


async def test_render_template_does_not_parse_numbers_as_json() -> None:
    """``{{ 1 + 1 }}`` renders to the text ``2``; a JSON parser would turn it into an int."""
    handler = Scripted([ok(text="2")])
    async with client_for(handler) as client:
        rendered = await client.render_template("{{ 1 + 1 }}")
    assert rendered == "2"
    assert isinstance(rendered, str)


async def test_render_template_sends_variables_only_when_given() -> None:
    """The optional variables object is sent when provided and absent otherwise."""
    handler = Scripted([ok(text="10:00"), ok(text="")])
    async with client_for(handler) as client:
        await client.render_template("{{ now }}", variables={"now": "10:00"})
        await client.render_template("{{ now }}")
    with_variables = json.loads(handler.requests[0].content)
    without_variables = json.loads(handler.requests[1].content)
    assert with_variables["variables"] == {"now": "10:00"}
    assert "variables" not in without_variables


async def test_tts_get_url_posts_every_option() -> None:
    """All four request fields are sent and the answer is reduced to URL plus path."""
    payload = {
        "url": "http://ha.test:8123/api/tts_proxy/abc.wav",
        "path": "/api/tts_proxy/abc.wav",
    }
    handler = Scripted([ok(payload)])
    async with client_for(handler) as client:
        result = await client.tts_get_url(
            engine_id="tts.piper",
            message="hi",
            language="en_GB",
            options={"preferred_format": "wav"},
        )
    assert result == HaTtsUrl(
        url="http://ha.test:8123/api/tts_proxy/abc.wav", path="/api/tts_proxy/abc.wav"
    )
    assert str(handler.requests[0].url) == f"{BASE_URL}/api/tts_get_url"
    assert json.loads(handler.requests[0].content) == {
        "engine_id": "tts.piper",
        "message": "hi",
        "language": "en_GB",
        "options": {"preferred_format": "wav"},
    }


async def test_tts_get_url_omits_optional_fields_and_allows_a_missing_path() -> None:
    """Without ``language``/``options`` only two keys are posted; no ``path`` means ``None``."""
    handler = Scripted([ok({"url": "http://ha.test:8123/api/tts_proxy/abc.wav"})])
    async with client_for(handler) as client:
        result = await client.tts_get_url(engine_id="tts.piper", message="hi")
    assert result.path is None
    assert result.url == "http://ha.test:8123/api/tts_proxy/abc.wav"
    assert json.loads(handler.requests[0].content) == {
        "engine_id": "tts.piper",
        "message": "hi",
    }


# ------------------------------------------------------------------- 401, 403 and 404


async def test_a_401_is_a_token_error_that_is_never_retried() -> None:
    """A plain-text 401 names the token, warns about admin rights and is final."""
    handler = Scripted([err(401, "401: Unauthorized")])
    async with client_for(handler) as client:
        with pytest.raises(HaAuthError) as excinfo:
            await client.api_status()
    exc = excinfo.value
    assert "token" in str(exc).lower()
    assert "admin" in str(exc).lower()
    assert exc.transient is False
    assert len(handler.requests) == 1


async def test_a_403_is_also_a_token_error() -> None:
    """A forbidden answer means the same thing to the user: fix the token."""
    handler = Scripted([err(403, "403: Forbidden")])
    async with client_for(handler) as client:
        with pytest.raises(HaAuthError) as excinfo:
            await client.states()
    assert excinfo.value.status_code == 403
    assert excinfo.value.transient is False
    assert len(handler.requests) == 1


async def test_a_401_from_the_template_endpoint_is_a_token_error() -> None:
    """The template endpoint needs an admin token, so the same mapping applies to a POST."""
    handler = Scripted([err(401, "401: Unauthorized")])
    async with client_for(handler) as client:
        with pytest.raises(HaAuthError):
            await client.render_template("{{ 1 + 1 }}")
    assert len(handler.requests) == 1


async def test_a_404_names_the_missing_entity_and_is_not_transient() -> None:
    """The message must name the entity the user typed, and a retry cannot help."""
    handler = Scripted([err(404, "Entity not found.")])
    async with client_for(handler) as client:
        with pytest.raises(HaNotFoundError) as excinfo:
            await client.state("sensor.ghost")
    exc = excinfo.value
    assert "sensor.ghost" in str(exc)
    assert exc.transient is False
    assert exc.status_code == 404
    assert len(handler.requests) == 1


async def test_a_404_from_the_api_root_is_a_not_found_error() -> None:
    """Home Assistant answers 404 for ``/api`` without the slash; that is still a 404."""
    handler = Scripted([err(404, "404: Not Found")])
    async with client_for(handler) as client:
        with pytest.raises(HaNotFoundError):
            await client.api_status()
    assert len(handler.requests) == 1


# ------------------------------------------------------------------ 5xx and the retry rule


async def test_a_500_on_a_get_is_retried_once_and_then_succeeds() -> None:
    """Exactly one retry, and the retry is authorised as well."""
    handler = Scripted([err(500), ok([_STATE_FRONT_DOOR])])
    async with client_for(handler) as client:
        states = await client.states()
    assert [state.entity_id for state in states] == ["sensor.front_door"]
    assert len(handler.requests) == 2
    assert handler.requests[1].headers["authorization"] == f"Bearer {TOKEN}"


async def test_two_503s_raise_a_transient_server_error() -> None:
    """A 5xx twice is the end of the road: the error is transient, not retried again."""
    handler = Scripted([err(503), err(503)])
    async with client_for(handler) as client:
        with pytest.raises(HaServerError) as excinfo:
            await client.states()
    exc = excinfo.value
    assert exc.status_code == 503
    assert exc.transient is True
    assert len(handler.requests) == 2
    assert "503" in str(exc)


async def test_a_500_on_tts_get_url_is_never_retried() -> None:
    """The POST may already have made Home Assistant write a file, so it is sent once."""
    handler = Scripted([err(500), ok({"url": "http://ha.test:8123/api/tts_proxy/abc.wav"})])
    async with client_for(handler) as client:
        with pytest.raises(HaServerError):
            await client.tts_get_url(engine_id="tts.piper", message="hi")
    assert len(handler.requests) == 1
    assert len(handler.script) == 1


async def test_a_500_on_render_template_is_never_retried() -> None:
    """The same no-retry rule covers the template POST."""
    handler = Scripted([err(500), ok(text="unused")])
    async with client_for(handler) as client:
        with pytest.raises(HaServerError):
            await client.render_template("{{ 1 + 1 }}")
    assert len(handler.requests) == 1
    assert len(handler.script) == 1


async def test_a_429_on_a_get_is_retried_once() -> None:
    """Too many requests is a "try again later", so the single retry applies."""
    handler = Scripted([err(429), ok([])])
    async with client_for(handler) as client:
        assert await client.states() == []
    assert len(handler.requests) == 2


async def test_two_429s_raise_a_transient_bad_request_error() -> None:
    """A second 429 is reported as transient, so the caller can wait and try later."""
    handler = Scripted([err(429), err(429)])
    async with client_for(handler) as client:
        with pytest.raises(HaBadRequestError) as excinfo:
            await client.states()
    exc = excinfo.value
    assert exc.transient is True
    assert exc.status_code == 429
    assert len(handler.requests) == 2


async def test_two_408s_raise_a_transient_bad_request_error() -> None:
    """A request timeout is transient even though no httpx timeout was involved."""
    handler = Scripted([err(408), err(408)])
    async with client_for(handler) as client:
        with pytest.raises(HaBadRequestError) as excinfo:
            await client.states()
    exc = excinfo.value
    assert exc.transient is True
    assert exc.status_code == 408
    assert len(handler.requests) == 2


async def test_a_400_from_the_template_endpoint_carries_ha_own_explanation() -> None:
    """A bad jinja template is a final failure, and HA's own text is the actionable part."""
    handler = Scripted([err(400, message="Error rendering template: unexpected '}' in line 1")])
    async with client_for(handler) as client:
        with pytest.raises(HaBadRequestError) as excinfo:
            await client.render_template("{{ 1 + 1 }}}")
    exc = excinfo.value
    assert exc.transient is False
    assert "unexpected '}'" in str(exc)
    assert len(handler.requests) == 1


async def test_a_redirect_names_the_address_it_points_at() -> None:
    """Following a redirect would drop the header, so it is a configuration error."""
    handler = Scripted([err(307, location="https://ha.example.com")])
    async with client_for(handler) as client:
        with pytest.raises(HaConfigurationError) as excinfo:
            await client.states()
    assert "https://ha.example.com" in str(excinfo.value)
    assert len(handler.requests) == 1


async def test_a_redirect_without_a_location_says_no_address_was_named() -> None:
    """A bare 3xx must still produce a sentence the user can act on."""
    handler = Scripted([err(301)])
    async with client_for(handler) as client:
        with pytest.raises(HaConfigurationError) as excinfo:
            await client.states()
    assert "did not name" in str(excinfo.value)
    assert len(handler.requests) == 1


async def test_the_retry_delay_is_really_slept() -> None:
    """The configured delay must be applied between the attempt and its retry."""
    handler = Scripted([err(500), ok([])])
    started = time.monotonic()
    async with client_for(handler, retry_delay_s=_RETRY_DELAY_S) as client:
        assert await client.states() == []
    elapsed = time.monotonic() - started
    assert elapsed >= _RETRY_DELAY_S
    assert len(handler.requests) == 2


# ----------------------------------------------------------------------------- timeouts


async def test_a_read_timeout_is_retried_and_reports_the_configured_seconds() -> None:
    """The message names the read timeout the client was built with."""
    timeout = httpx.ReadTimeout("simulated read timeout")
    handler = Scripted([timeout, timeout])
    async with client_for(handler, read_timeout_s=2.0) as client:
        with pytest.raises(HaTimeoutError) as excinfo:
            await client.states()
    exc = excinfo.value
    assert exc.transient is True
    assert exc.timeout_s == 2.0
    assert "within 2 s" in str(exc)
    assert len(handler.requests) == 2


async def test_the_configured_timeouts_reach_the_transport() -> None:
    """The phased budget is connect=connect, read=read, write=read, pool=connect.

    Regression test for a 3 s write budget on a POST body: the module docstring's
    ``2 x (connect + read) + retry_delay`` arithmetic only holds when ``write``
    matches ``read``.
    """
    handler = Scripted([ok([])])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = HomeAssistantClient(
            base_url=BASE_URL,
            token=TOKEN,
            client=raw,
            connect_timeout_s=1.5,
            read_timeout_s=4.5,
        )
        await client.states()
    finally:
        await raw.aclose()
    assert handler.timeouts == [{"connect": 1.5, "read": 4.5, "write": 4.5, "pool": 1.5}]


async def test_a_per_call_budget_reaches_all_four_transport_slots() -> None:
    """``timeout_s`` overrides every phase of the httpx timeout, not just some of them."""
    handler = Scripted([ok({"message": "API running."})])
    async with client_for(handler) as client:
        await client.api_status(timeout_s=0.5)
    assert handler.timeouts == [{"connect": 0.5, "read": 0.5, "write": 0.5, "pool": 0.5}]


async def test_a_connect_timeout_on_a_post_is_reported_after_one_request() -> None:
    """A POST that timed out may already have run, so it is never sent twice."""
    handler = Scripted([httpx.ConnectTimeout("simulated connect timeout")])
    async with client_for(handler) as client:
        with pytest.raises(HaTimeoutError) as excinfo:
            await client.tts_get_url(engine_id="tts.piper", message="hi")
    assert excinfo.value.transient is True
    assert len(handler.requests) == 1


async def test_the_per_call_budget_bounds_the_whole_call() -> None:
    """``timeout_s`` is a total budget, enforced even when the transport just hangs."""
    handler = Scripted([], delay_s=_SLOW_HANDLER_S)
    started = time.monotonic()
    async with client_for(handler) as client:
        with pytest.raises(HaTimeoutError) as excinfo:
            await client.api_status(timeout_s=_BUDGET_S)
    elapsed = time.monotonic() - started
    exc = excinfo.value
    assert exc.timeout_s == _BUDGET_S
    assert "0.05" in str(exc)
    assert exc.transient is True
    assert elapsed >= _BUDGET_S
    assert len(handler.requests) == 1


@pytest.mark.parametrize("budget", [0.0, -1.0])
async def test_a_non_positive_per_call_budget_is_rejected_before_any_request(
    budget: float,
) -> None:
    """A budget that cannot work is a configuration error, not a timeout."""
    handler = Scripted([])
    async with client_for(handler) as client:
        with pytest.raises(HaConfigurationError):
            await client.states(timeout_s=budget)
    assert handler.requests == []


async def test_a_connect_error_names_the_base_url_and_the_host_or_port() -> None:
    """The most common real failure gets the most actionable sentence."""
    failure = httpx.ConnectError("[Errno -2] Name or service not known")
    handler = Scripted([failure, failure])
    async with client_for(handler) as client:
        with pytest.raises(HaConnectionError) as excinfo:
            await client.states()
    exc = excinfo.value
    assert exc.transient is True
    assert BASE_URL in str(exc)
    assert "host or port" in str(exc)
    assert len(handler.requests) == 2


# ------------------------------------------------------------------------ malformed JSON


async def test_states_with_a_non_json_body_is_a_bad_response() -> None:
    """A 200 whose body is not JSON must not escape as a ``JSONDecodeError``."""
    handler = Scripted([ok(text="not json at all")])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.states()


async def test_api_status_with_a_json_array_is_a_bad_response() -> None:
    """Valid JSON of the wrong shape is still unusable."""
    handler = Scripted([ok([])])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.api_status()


async def test_api_status_with_a_non_string_message_is_a_bad_response() -> None:
    """``{"message": 7}`` cannot be shown to the user, so it is rejected."""
    handler = Scripted([ok({"message": 7})])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.api_status()


async def test_a_state_entry_without_a_state_field_is_a_bad_response() -> None:
    """``entity_id`` alone is not a state object."""
    handler = Scripted([ok([{"entity_id": "sensor.x"}])])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.states()


async def test_a_state_entry_with_non_object_attributes_is_a_bad_response() -> None:
    """``attributes`` has to be a JSON object for the read-only mapping to exist."""
    handler = Scripted([ok([{"entity_id": "sensor.x", "state": "on", "attributes": 5}])])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.states()


async def test_tts_get_url_with_a_non_json_body_is_a_bad_response() -> None:
    """The POST path has the same JSON contract as the GETs."""
    handler = Scripted([ok(text="not json at all")])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.tts_get_url(engine_id="tts.piper", message="hi")


async def test_tts_get_url_with_a_non_string_url_is_a_bad_response() -> None:
    """``{"url": 7}`` is not something the caller could fetch."""
    handler = Scripted([ok({"url": 7})])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.tts_get_url(engine_id="tts.piper", message="hi")


async def test_a_state_entry_with_a_non_string_last_changed_is_a_bad_response() -> None:
    """Timestamps are optional, but when present they must be strings."""
    handler = Scripted([ok({"entity_id": "sensor.x", "state": "on", "last_changed": 7})])
    async with client_for(handler) as client:
        with pytest.raises(HaBadResponseError):
            await client.state("sensor.x")


# --------------------------------------------------------------------- token secrecy


async def _call_states(client: HomeAssistantClient) -> object:
    """Call the entity list, discarding any answer.

    :param client: the client under test.
    """
    return await client.states()


async def _call_status(client: HomeAssistantClient) -> object:
    """Call the API status, discarding any answer.

    :param client: the client under test.
    """
    return await client.api_status()


async def _call_template(client: HomeAssistantClient) -> object:
    """Call the template endpoint, discarding any answer.

    :param client: the client under test.
    """
    return await client.render_template("{{ 1 + 1 }}")


async def _call_tts(client: HomeAssistantClient) -> object:
    """Call the TTS endpoint, discarding any answer.

    :param client: the client under test.
    """
    return await client.tts_get_url(engine_id="tts.piper", message="hi")


async def _call_status_with_budget(client: HomeAssistantClient) -> object:
    """Call the API status under a budget far shorter than the handler's delay.

    :param client: the client under test.
    """
    return await client.api_status(timeout_s=_BUDGET_S)


async def _call_template_with_no_budget(client: HomeAssistantClient) -> object:
    """Call the template endpoint with an unusable budget, which fails before sending.

    :param client: the client under test.
    """
    return await client.render_template("{{ 1 + 1 }}", timeout_s=0.0)


@dataclass(frozen=True)
class FailureCase:
    """One failure script plus the call that must fail with it."""

    name: str
    handler: Callable[[], Scripted]
    call: Callable[[HomeAssistantClient], Awaitable[object]]
    expected: type[HaError]


def _case(
    name: str,
    script: Callable[[], Scripted],
    call: Callable[[HomeAssistantClient], Awaitable[object]],
    expected: type[HaError],
) -> FailureCase:
    """Build one :class:`FailureCase` without repeating the field names.

    :param name: parametrisation id.
    :param script: factory returning a fresh handler, because scripts are consumed.
    :param call: the call that must fail.
    :param expected: the error class the call must raise.
    """
    return FailureCase(name=name, handler=script, call=call, expected=expected)


_LEAK_CASES: Final = [
    _case(
        "401-plain-text",
        lambda: Scripted([err(401, "401: Unauthorized")]),
        _call_status,
        HaAuthError,
    ),
    _case("403", lambda: Scripted([err(403, "403: Forbidden")]), _call_states, HaAuthError),
    _case("404", lambda: Scripted([err(404, "Entity not found.")]), _call_states, HaNotFoundError),
    _case(
        "400-with-message",
        lambda: Scripted([err(400, message="Error rendering template: unexpected '}'")]),
        _call_template,
        HaBadRequestError,
    ),
    _case("429-twice", lambda: Scripted([err(429), err(429)]), _call_states, HaBadRequestError),
    _case("500-twice", lambda: Scripted([err(500), err(500)]), _call_states, HaServerError),
    _case(
        "307-redirect",
        lambda: Scripted([err(307, location="https://ha.example.com")]),
        _call_states,
        HaConfigurationError,
    ),
    _case(
        "200-not-json",
        lambda: Scripted([ok(text="not json at all")]),
        _call_states,
        HaBadResponseError,
    ),
    _case("200-wrong-shape", lambda: Scripted([ok([])]), _call_status, HaBadResponseError),
    _case(
        "read-timeout",
        lambda: Scripted([httpx.ReadTimeout("simulated"), httpx.ReadTimeout("simulated")]),
        _call_states,
        HaTimeoutError,
    ),
    _case(
        "connect-error",
        lambda: Scripted([httpx.ConnectError("simulated"), httpx.ConnectError("simulated")]),
        _call_states,
        HaConnectionError,
    ),
    _case(
        "budget-timeout",
        lambda: Scripted([], delay_s=_SLOW_HANDLER_S),
        _call_status_with_budget,
        HaTimeoutError,
    ),
    _case(
        "zero-budget",
        lambda: Scripted([]),
        _call_template_with_no_budget,
        HaConfigurationError,
    ),
]
"""Every error class, reached through a script that mentions nothing secret."""


@pytest.mark.parametrize("case", _LEAK_CASES, ids=[case.name for case in _LEAK_CASES])
async def test_the_token_never_leaks_through_an_error(case: FailureCase) -> None:
    """No error class may carry the token in its message, its repr or its cause chain."""
    handler = case.handler()
    async with client_for(handler) as client:
        with pytest.raises(case.expected) as excinfo:
            await case.call(client)
    exc = excinfo.value
    _assert_token_absent(str(exc))
    _assert_token_absent(repr(exc))
    _assert_token_absent("".join(traceback.format_exception(exc)))
    for request in handler.requests:
        for header, value in request.headers.items():
            if header.lower() == "authorization":
                continue
            _assert_token_absent(value)


_STRADDLING_FILL: Final = 190
"""Prefix length that puts the secret at characters 190-220, across the 200 clip."""


def _straddling_the_clip(fill: str) -> str:
    """Build body text whose secret deliberately straddles the 200-character clip.

    Clipping before scrubbing would leave no complete literal to redact, so a
    regression fails the ``REDACTED`` assertion rather than passing by accident.

    :param fill: filler character, so each case's body is recognisable in a dump.
    """
    return fill * _STRADDLING_FILL + TOKEN + " trailing"


async def test_a_secret_straddling_the_clip_is_scrubbed_in_a_plain_text_body() -> None:
    """The common real 500 body is plain text, and the clip must not hide its secret.

    The scrubber registration is undone by ``preserve_log_state`` so the global
    scrubber is left as the rest of the suite expects to find it.
    """
    body = _straddling_the_clip("x")
    with preserve_log_state():
        SECRET_SCRUBBER.set_secrets([TOKEN])
        # A GET is retried once, so the same failing body is scripted twice.
        handler = Scripted([err(500, body=body), err(500, body=body)])
        async with client_for(handler) as client:
            with pytest.raises(HaServerError) as excinfo:
                await client.states()
    message = str(excinfo.value)
    assert REDACTED in message
    assert TOKEN[:12] not in message


async def test_a_secret_straddling_the_clip_is_scrubbed_in_ha_own_json_message() -> None:
    """A 400 from ``render_template`` carries HA's ``message``, which is long here.

    The POST is never retried, so one scripted answer is enough.
    """
    with preserve_log_state():
        SECRET_SCRUBBER.set_secrets([TOKEN])
        handler = Scripted([err(400, message=_straddling_the_clip("y"))])
        async with client_for(handler) as client:
            with pytest.raises(HaBadRequestError) as excinfo:
                await client.render_template("{{ 1 + 1 }}")
    message = str(excinfo.value)
    assert REDACTED in message
    assert TOKEN[:12] not in message


async def test_the_repr_of_the_client_never_contains_the_token() -> None:
    """The client can be logged freely."""
    handler = Scripted([])
    async with client_for(handler) as client:
        shown = repr(client)
    assert shown == f"<HomeAssistantClient base_url={BASE_URL!r} token='***'>"
    _assert_token_absent(shown)


async def test_the_debug_log_line_redacts_the_header(caplog: pytest.LogCaptureFixture) -> None:
    """The request log carries ``Bearer ***`` and never the real token.

    The scrubber is cleared so a genuine leak cannot be hidden by the pattern that
    redacts ``Bearer <credential>`` before the assertion can see it.
    """
    with preserve_log_state(), caplog.at_level(logging.DEBUG, logger="chime_deck.ha"):
        SECRET_SCRUBBER.clear()
        handler = Scripted([ok({"message": "API running."})])
        async with client_for(handler) as client:
            await client.api_status()
        messages = [record.getMessage() for record in caplog.records]
    assert messages, "the client logged nothing at DEBUG, so this test would be vacuous"
    for message in messages:
        _assert_token_absent(message)
    assert any("Bearer ***" in message for message in messages)


async def test_the_leak_helper_detects_a_planted_token(caplog: pytest.LogCaptureFixture) -> None:
    """Control for the test above: a record with the token in it must be caught."""
    logger = logging.getLogger("chime_deck.ha")
    with preserve_log_state(), caplog.at_level(logging.DEBUG, logger="chime_deck.ha"):
        SECRET_SCRUBBER.clear()
        logger.debug("planted credential %s", TOKEN)
        planted = [record.getMessage() for record in caplog.records]
    assert planted
    with pytest.raises(AssertionError):
        _assert_token_absent(planted[-1])


async def test_redact_headers_replaces_only_the_authorisation_value() -> None:
    """Both header spellings are redacted; everything else is untouched."""
    redacted = redact_headers({"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    assert redacted == {"Authorization": "Bearer ***", "Accept": "application/json"}
    lowercase = redact_headers({"authorization": f"Bearer {TOKEN}"})
    assert lowercase == {"authorization": "Bearer ***"}
    for value in (*redacted.values(), *lowercase.values()):
        _assert_token_absent(value)


# --------------------------------------------------------------- from_store and lifetime


class FakeStore:
    """A settings store that answers only the two keys the client needs."""

    def __init__(self, settings: dict[str, object]) -> None:
        """Remember the settings to answer from.

        :param settings: the stored key/value pairs.
        """
        self._settings = dict(settings)

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or ``None`` when it is absent.

        :param key: registered setting key.
        """
        return self._settings.get(key)


async def test_from_store_builds_a_client_from_the_stored_settings() -> None:
    """The stored base URL and token must be the ones the request uses."""
    handler = Scripted([ok([_STATE_FRONT_DOOR])])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        store = FakeStore({"ha.base_url": BASE_URL, "ha.token": TOKEN})
        client = await HomeAssistantClient.from_store(store, client=raw)
        await client.states()
    finally:
        await raw.aclose()
    request = handler.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert str(request.url) == f"{BASE_URL}/api/states"


@pytest.mark.parametrize(
    ("settings", "needle"),
    [
        ({"ha.base_url": BASE_URL, "ha.token": ""}, "token"),
        ({"ha.base_url": BASE_URL, "ha.token": "   "}, "token"),
        ({"ha.base_url": BASE_URL, "ha.token": 7}, "ha.token"),
        ({"ha.base_url": 7, "ha.token": TOKEN}, "ha.base_url"),
        ({"ha.token": TOKEN}, "ha.base_url"),
    ],
)
async def test_from_store_rejects_unusable_settings(
    settings: dict[str, object], needle: str
) -> None:
    """A missing or wrongly typed setting is a configuration error naming the key."""
    with pytest.raises(HaConfigurationError) as excinfo:
        await HomeAssistantClient.from_store(FakeStore(settings))
    assert needle in str(excinfo.value)


async def test_aclose_leaves_a_borrowed_client_open() -> None:
    """A client passed to the constructor belongs to the caller, not to this object."""
    handler = Scripted([])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = HomeAssistantClient(base_url=BASE_URL, token=TOKEN, client=raw)
        await client.aclose()
        assert raw.is_closed is False
    finally:
        await raw.aclose()
    assert raw.is_closed is True


async def test_the_context_manager_closes_a_client_it_created_itself() -> None:
    """The async-with form must leave nothing for the garbage collector."""
    async with HomeAssistantClient(base_url=BASE_URL, token=TOKEN, retry_delay_s=0.0) as client:
        http = client._http()
        assert http.is_closed is False
    assert http.is_closed is True


async def test_aclose_closes_a_client_it_created_itself() -> None:
    """The explicit form does the same, so both lifetimes are covered."""
    client = HomeAssistantClient(base_url=BASE_URL, token=TOKEN, retry_delay_s=0.0)
    http = client._http()
    assert http.is_closed is False
    await client.aclose()
    assert http.is_closed is True


async def test_a_closed_client_fails_loudly() -> None:
    """After ``aclose()`` every call is a configuration error, borrowed client or not."""
    handler = Scripted([])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = HomeAssistantClient(base_url=BASE_URL, token=TOKEN, client=raw)
        await client.aclose()
        with pytest.raises(HaConfigurationError):
            await client.api_status()
        assert raw.is_closed is False
    finally:
        await raw.aclose()
    assert handler.requests == []

    async with HomeAssistantClient(base_url=BASE_URL, token=TOKEN, retry_delay_s=0.0) as owned:
        pass
    with pytest.raises(HaConfigurationError):
        await owned.states()


async def test_an_empty_token_is_a_configuration_error() -> None:
    """A client that could only ever send an unusable header is refused up front."""
    with pytest.raises(HaConfigurationError):
        HomeAssistantClient(base_url=BASE_URL, token="")
    with pytest.raises(HaConfigurationError):
        HomeAssistantClient(base_url=BASE_URL, token="   ")


async def test_non_positive_timeouts_are_configuration_errors() -> None:
    """Neither timeout may be zero or negative."""
    with pytest.raises(HaConfigurationError):
        HomeAssistantClient(base_url=BASE_URL, token=TOKEN, connect_timeout_s=0.0)
    with pytest.raises(HaConfigurationError):
        HomeAssistantClient(base_url=BASE_URL, token=TOKEN, read_timeout_s=-1.0)
