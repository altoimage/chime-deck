"""HTTP API package: error envelope, probes, settings, buttons, sounds and entities.

Everything under ``api/`` is imported by :mod:`chime_deck.app` only. The
package deliberately holds no module-level state: routers are built by factory
functions that close over the application state, so importing this package never
opens a database or reads the environment. The buttons routes own the chime
buttons' CRUD, reorder and the UI test press; the sounds routes list and rescan
the folder library, download a URL and delete a sound; the entities routes own the
status tiles' CRUD and reorder, including their resolved D16 icons and
presentation hints; the Home Assistant routes list the TTS engines the settings
page offers as a dropdown and search the entities the tile editor picks from; the
icons routes take only the custom icon directory rather than the application
state, because they serve the icon set shipped inside the package (searched names
and their paths, so the UI needs neither a webfont nor the whole set in its
bundle) and the user's own SVG/PNG files under ``data/icons``; the
MQTT route re-announces the discovery payloads of the enabled buttons; the audio
route lists the output devices and what the stored hint resolves to; the local
TTS routes list the speech engines and the voice
catalogue and manage voice-model downloads, and the tiles route owns the one
runtime interaction the status tiles have — toggling a tile's volatile D29
selection. The renderer router is the only one
built for the renderer role rather than the doorbell: it serves the token-gated
``POST /api/tts/render`` and its health probe. The auth router is the one route
that turns the optional shared token into a session cookie.

Owning ticket: HSD-91z1; the Home Assistant routes were added by HSD-npt0,
the local TTS routes by HSD-p50k, the renderer router by HSD-5de8, the MQTT
route by HSD-bq76, the icons routes by HSD-zyru, the custom-icon routes by
HSD-i7pw, the audio routes by HSD-wpm0, the history, config-transfer and
about routes by HSD-l22f, the shared-token login route by HSD-0jke and the tile
selection route by HSD-u6k5.
"""

from __future__ import annotations

from .about import (
    LOG_EXCERPT_MAX_BYTES,
    LOG_EXCERPT_MAX_LINES,
    LOG_FILENAME,
    AboutResponse,
    build_about_router,
)
from .audio import (
    AudioDeviceModel,
    AudioDevicesResponse,
    build_audio_router,
)
from .auth import LOGIN_MAX_BODY_BYTES, build_auth_router
from .buttons import (
    ButtonCreateRequest,
    ButtonResponse,
    ButtonUpdateRequest,
    DeleteButtonResponse,
    MqttTopics,
    PressResponse,
    ReorderButtonsRequest,
    SlugChange,
    build_buttons_router,
)
from .config_transfer import (
    EXPORT_FILENAME,
    ImportReportResponse,
    ImportRequest,
    build_config_router,
)
from .entities import (
    AttributeHintResponse,
    DeleteEntityResponse,
    EntityCreateRequest,
    EntityResponse,
    EntityUpdateRequest,
    ReorderEntitiesRequest,
    StateToneDefaultsResponse,
    build_entities_router,
)
from .errors import (
    ApiError,
    ErrorCode,
    ErrorDetail,
    ErrorEnvelope,
    install_error_handlers,
)
from .ha import HaTtsEngineResponse, HaTtsEnginesResponse, build_ha_router
from .history import (
    HISTORY_LIMIT_DEFAULT,
    HistoryResponse,
    HistoryRow,
    build_history_router,
)
from .icons import (
    CustomIconEntry,
    CustomIconListResponse,
    CustomIconSkip,
    IconPathResponse,
    IconSearchResponse,
    IconSearchResult,
    build_icons_router,
)
from .limits import SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX
from .mqtt import RepublishResponse, build_mqtt_router
from .probes import (
    PROBE_TIMEOUT_S,
    AudioProbeResult,
    ConnectionProbes,
    HaProbeCandidate,
    HaProbeResult,
    LiveProbes,
    MqttProbeResult,
)
from .render import PRESERVED_ORTHOGRAPHIC_CONTROLS, build_render_router
from .settings import (
    AudioTestResponse,
    HaTestRequest,
    HaTestResponse,
    MqttTestResponse,
    SettingsResponse,
    build_settings_router,
)
from .sounds import (
    AffectedButtonResponse,
    DeleteSoundResponse,
    FromUrlRequest,
    FromUrlResponse,
    RescanResponse,
    SoundResponse,
    build_sounds_router,
)
from .tiles import TileSelectionResponse, build_tiles_router
from .tts import (
    TtsEnginesResponse,
    TtsEngineStatusResponse,
    TtsVoiceResponse,
    TtsVoicesResponse,
    VoiceDownloadStartResponse,
    VoiceDownloadStatusResponse,
    build_tts_router,
)
from .tts_providers import (
    DeleteProviderResponse,
    PresetSummaryResponse,
    ProviderResponse,
    ProviderShapeResponse,
    ProviderWriteRequest,
    TtsProvidersResponse,
    TtsProviderTestResponse,
    build_provider_router,
)

__all__ = [
    "EXPORT_FILENAME",
    "HISTORY_LIMIT_DEFAULT",
    "LOGIN_MAX_BODY_BYTES",
    "LOG_EXCERPT_MAX_BYTES",
    "LOG_EXCERPT_MAX_LINES",
    "LOG_FILENAME",
    "PRESERVED_ORTHOGRAPHIC_CONTROLS",
    "PROBE_TIMEOUT_S",
    "SEARCH_LIMIT_DEFAULT",
    "SEARCH_LIMIT_MAX",
    "AboutResponse",
    "AffectedButtonResponse",
    "ApiError",
    "AttributeHintResponse",
    "AudioDeviceModel",
    "AudioDevicesResponse",
    "AudioProbeResult",
    "AudioTestResponse",
    "ButtonCreateRequest",
    "ButtonResponse",
    "ButtonUpdateRequest",
    "ConnectionProbes",
    "CustomIconEntry",
    "CustomIconListResponse",
    "CustomIconSkip",
    "DeleteButtonResponse",
    "DeleteEntityResponse",
    "DeleteProviderResponse",
    "DeleteSoundResponse",
    "EntityCreateRequest",
    "EntityResponse",
    "EntityUpdateRequest",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "FromUrlRequest",
    "FromUrlResponse",
    "HaProbeCandidate",
    "HaProbeResult",
    "HaTestRequest",
    "HaTestResponse",
    "HaTtsEngineResponse",
    "HaTtsEnginesResponse",
    "HistoryResponse",
    "HistoryRow",
    "IconPathResponse",
    "IconSearchResponse",
    "IconSearchResult",
    "ImportReportResponse",
    "ImportRequest",
    "LiveProbes",
    "MqttProbeResult",
    "MqttTestResponse",
    "MqttTopics",
    "PresetSummaryResponse",
    "PressResponse",
    "ProviderResponse",
    "ProviderShapeResponse",
    "ProviderWriteRequest",
    "ReorderButtonsRequest",
    "ReorderEntitiesRequest",
    "RepublishResponse",
    "RescanResponse",
    "SettingsResponse",
    "SlugChange",
    "SoundResponse",
    "StateToneDefaultsResponse",
    "TileSelectionResponse",
    "TtsEngineStatusResponse",
    "TtsEnginesResponse",
    "TtsProviderTestResponse",
    "TtsProvidersResponse",
    "TtsVoiceResponse",
    "TtsVoicesResponse",
    "VoiceDownloadStartResponse",
    "VoiceDownloadStatusResponse",
    "build_about_router",
    "build_audio_router",
    "build_auth_router",
    "build_buttons_router",
    "build_config_router",
    "build_entities_router",
    "build_ha_router",
    "build_history_router",
    "build_icons_router",
    "build_mqtt_router",
    "build_provider_router",
    "build_render_router",
    "build_settings_router",
    "build_sounds_router",
    "build_tiles_router",
    "build_tts_router",
    "install_error_handlers",
]
