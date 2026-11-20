"""The Home Assistant clients, their typed errors and the live-data adapter.

Import the public API from this package rather than from the individual modules:
the module layout is an implementation detail. ``HomeAssistantClient`` is the
REST client and ``HomeAssistantWebSocketClient`` the reconnecting state
subscriber; the ``Ha*`` error classes are what they raise, and ``HaError.transient``
is the retry predicate downstream tickets build on. ``HaEntityInfo`` is the
concrete live-data adapter over the websocket client's cache (the
:class:`~chime_deck.entity_info.LiveEntityInfo` the app runs in production).

Owning ticket: HSD-8qf6.
"""

from __future__ import annotations

from .client import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
    DEFAULT_RETRY_DELAY_S,
    RETRYABLE_METHODS,
    HaApiStatus,
    HaState,
    HaTtsUrl,
    HomeAssistantClient,
    SettingsReader,
    normalise_base_url,
    redact_headers,
)
from .errors import (
    HaAuthError,
    HaBadRequestError,
    HaBadResponseError,
    HaCommandError,
    HaConfigurationError,
    HaConnectionError,
    HaError,
    HaNotFoundError,
    HaServerError,
    HaTimeoutError,
)
from .live import (
    ENTITY_REGISTRY_TTL_S,
    LIVE_LOOKUP_BUDGET_S,
    REGISTRY_BUDGET_S,
    HaEntityInfo,
    sync_tile_ids,
    watch_tile_changes,
)
from .websocket import (
    DEFAULT_CLOSE_TIMEOUT_S,
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_HANDSHAKE_TIMEOUT_S,
    DEFAULT_OPEN_TIMEOUT_S,
    DEFAULT_PING_INTERVAL_S,
    DEFAULT_PING_TIMEOUT_S,
    DEFAULT_RECEIVE_TIMEOUT_S,
    DEFAULT_RECONNECT_INITIAL_S,
    DEFAULT_RECONNECT_MAX_S,
    DEFAULT_SHUTDOWN_TIMEOUT_S,
    ConnectionFactory,
    HaEntityState,
    HaWebSocketConnection,
    HomeAssistantWebSocketClient,
    default_connection_factory,
    websocket_url,
)

__all__ = [
    "DEFAULT_CLOSE_TIMEOUT_S",
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_HANDSHAKE_TIMEOUT_S",
    "DEFAULT_OPEN_TIMEOUT_S",
    "DEFAULT_PING_INTERVAL_S",
    "DEFAULT_PING_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_RECEIVE_TIMEOUT_S",
    "DEFAULT_RECONNECT_INITIAL_S",
    "DEFAULT_RECONNECT_MAX_S",
    "DEFAULT_RETRY_DELAY_S",
    "DEFAULT_SHUTDOWN_TIMEOUT_S",
    "ENTITY_REGISTRY_TTL_S",
    "LIVE_LOOKUP_BUDGET_S",
    "REGISTRY_BUDGET_S",
    "RETRYABLE_METHODS",
    "ConnectionFactory",
    "HaApiStatus",
    "HaAuthError",
    "HaBadRequestError",
    "HaBadResponseError",
    "HaCommandError",
    "HaConfigurationError",
    "HaConnectionError",
    "HaEntityInfo",
    "HaEntityState",
    "HaError",
    "HaNotFoundError",
    "HaServerError",
    "HaState",
    "HaTimeoutError",
    "HaTtsUrl",
    "HaWebSocketConnection",
    "HomeAssistantClient",
    "HomeAssistantWebSocketClient",
    "SettingsReader",
    "default_connection_factory",
    "normalise_base_url",
    "redact_headers",
    "sync_tile_ids",
    "watch_tile_changes",
    "websocket_url",
]
