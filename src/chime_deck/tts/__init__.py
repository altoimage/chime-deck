"""Speech engines, the ephemeral clip cache and the engine resolver.

Import the public API from this package rather than from individual modules:
the module layout is an implementation detail. :class:`TtsEngine` is the engine
interface, :class:`TtsCache` is the delete-anytime clip cache, and
:class:`EspeakNgEngine` (local espeak-ng), :class:`HaTtsEngine` (Home
Assistant's own voice engines), :class:`PiperEngine` (local neural speech from
an optional extra, D14), :class:`RemoteTtsEngine` (another doorbell in
renderer mode, D24) and :class:`HttpTtsEngine` (a configured HTTP provider,
D25) are the engines implemented today.
:class:`SynthesisTiming` is the optional measurement port an engine may also
satisfy, and :class:`EngineStatus` is the read-only status row the settings page
reports. :class:`BakeStore`, :func:`expected_bake_sha` and :func:`is_static` are
the durable side of that story: a phrase with no Jinja syntax is pre-rendered
once into ``<data_dir>/tts/baked/<button_id>/`` and replayed forever (D21), while
:class:`BakeSweeper` retries the missing ones once per boot. The paid-provider
seam and the template predicate that D25 couples to it live in
:mod:`chime_deck.tts.providers`, together with the provider records and
:class:`ConfiguredProviders`, the registry of them; the packaged presets and
their request shapes live in :mod:`chime_deck.tts.presets`. Press-time templating
— rendering a Jinja phrase through Home Assistant before an engine sees it —
lives in :mod:`chime_deck.tts.templating` and is re-exported here. The
curated Piper voice catalogue, its verified downloader and its background-job
registry live in :mod:`chime_deck.tts.voices` and are re-exported here too.
:func:`options_for` is the one place that maps settings onto
:class:`TtsOptions`, and :data:`MAX_AUDIO_BYTES` is re-exported from
:mod:`chime_deck.tts.ha_tts` so a caller enforcing the same cap needs no
second import.

Owning ticket: HSD-bw8h; the provider seam was added by HSD-1drv, the
``ha_tts`` engine by HSD-npt0, press-time templating by HSD-bmqp, the local
``piper`` engine by HSD-p50k, the ``remote_tts`` engine by HSD-5de8 and the
durable bakes by HSD-dzek; alphabetic provider presets, the ``http_tts`` engine
and the cost channel by HSD-s856.
"""

from __future__ import annotations

from .bake import (
    BAKED_DIRNAME,
    JITTER_MAX_S,
    JITTER_MIN_S,
    BakeArtefact,
    BakeBatch,
    BakeFailure,
    BakeStore,
    BakeSweeper,
    BakeSweepReport,
    BakeUsage,
    ButtonReader,
    baked_root,
    directory_usage,
    expected_bake_sha,
    is_static,
)
from .cache import (
    CACHE_MAX_BYTES,
    CACHE_MAX_FILES,
    TtsCache,
    cache_key,
    suffix_for_format,
)
from .engines import (
    Availability,
    EspeakInvocation,
    EspeakNgEngine,
    NullTtsEngine,
    ProcessSpawner,
    SpawnedProcess,
    SynthesisTiming,
    TtsEngine,
    TtsOptions,
    sniff_container,
    validate_rendered_audio,
)
from .errors import (
    BakeNotApplicable,
    TtsError,
    TtsRenderError,
    VoiceError,
    VoiceErrorCode,
    clean_detail,
)
from .ha_tts import MAX_AUDIO_BYTES, HaTtsEngine
from .http_tts import (
    HTTP_TTS_CHUNK_BYTES,
    HTTP_TTS_CLOSE_GRACE_S,
    HTTP_TTS_MAX_AUDIO_BYTES,
    HTTP_TTS_MAX_REPLY_BYTES,
    HTTP_TTS_RENDER_BUDGET_S,
    HTTP_TTS_TEXT_MAX_CHARS,
    HttpTtsEngine,
)
from .piper import (
    DEFAULT_PIPER_MIN_RAM_MB,
    MAX_VOICE_ID_CHARS,
    MemoryVerdict,
    ModuleFinder,
    PiperEngine,
    PiperProbe,
    PiperVoicePort,
    read_meminfo,
    voice_id_problem,
)
from .presets import (
    DEFAULT_PRESETS,
    Preset,
    PresetError,
    PresetSummary,
    ProviderPresets,
    load_presets,
)
from .providers import (
    HTTP_TTS_ENGINE,
    NO_PAID_PROVIDERS,
    ConfiguredProviders,
    NoPaidProviders,
    PaidProviderLookup,
    ProviderError,
    ProviderRecord,
    ProviderSource,
    ResolvedProvider,
    SynthesisCosting,
    TtsCost,
    has_template_syntax,
    new_provider_id,
    parse_provider,
    parse_providers,
    read_providers,
)
from .remote_tts import (
    REMOTE_AVAILABILITY_NEGATIVE_TTL_S,
    REMOTE_AVAILABILITY_POSITIVE_TTL_S,
    REMOTE_AVAILABILITY_TIMEOUT_S,
    REMOTE_CHUNK_BYTES,
    REMOTE_CLOSE_GRACE_S,
    REMOTE_HEALTH_PATH,
    REMOTE_RENDER_BUDGET_S,
    REMOTE_RENDER_ENGINE,
    REMOTE_RENDER_FORMAT,
    REMOTE_RENDER_PATH,
    RemoteTtsEngine,
)
from .speech import (
    FALLBACK_CHAIN,
    BakeRecorder,
    EngineStatus,
    SettingsReader,
    TtsSpeechSource,
    build_speech_source,
    options_for,
)
from .templating import (
    MAX_TEMPLATE_CHARS,
    TEMPLATE_TIMEOUT_S,
    HaTemplateRenderer,
    PhraseTemplating,
    RenderedPhrase,
    TemplateRenderer,
    looks_like_markup,
    sanitise_render,
)
from .voices import (
    SUGGESTED_VOICES,
    InstalledVoice,
    SuggestedVoice,
    VoiceDownloadJob,
    VoiceDownloadRegistry,
    VoiceManager,
    VoiceState,
    install_url,
)

__all__ = [
    "BAKED_DIRNAME",
    "CACHE_MAX_BYTES",
    "CACHE_MAX_FILES",
    "DEFAULT_PIPER_MIN_RAM_MB",
    "DEFAULT_PRESETS",
    "FALLBACK_CHAIN",
    "HTTP_TTS_CHUNK_BYTES",
    "HTTP_TTS_CLOSE_GRACE_S",
    "HTTP_TTS_ENGINE",
    "HTTP_TTS_MAX_AUDIO_BYTES",
    "HTTP_TTS_MAX_REPLY_BYTES",
    "HTTP_TTS_RENDER_BUDGET_S",
    "HTTP_TTS_TEXT_MAX_CHARS",
    "JITTER_MAX_S",
    "JITTER_MIN_S",
    "MAX_AUDIO_BYTES",
    "MAX_TEMPLATE_CHARS",
    "MAX_VOICE_ID_CHARS",
    "NO_PAID_PROVIDERS",
    "REMOTE_AVAILABILITY_NEGATIVE_TTL_S",
    "REMOTE_AVAILABILITY_POSITIVE_TTL_S",
    "REMOTE_AVAILABILITY_TIMEOUT_S",
    "REMOTE_CHUNK_BYTES",
    "REMOTE_CLOSE_GRACE_S",
    "REMOTE_HEALTH_PATH",
    "REMOTE_RENDER_BUDGET_S",
    "REMOTE_RENDER_ENGINE",
    "REMOTE_RENDER_FORMAT",
    "REMOTE_RENDER_PATH",
    "SUGGESTED_VOICES",
    "TEMPLATE_TIMEOUT_S",
    "Availability",
    "BakeArtefact",
    "BakeBatch",
    "BakeFailure",
    "BakeNotApplicable",
    "BakeRecorder",
    "BakeStore",
    "BakeSweepReport",
    "BakeSweeper",
    "BakeUsage",
    "ButtonReader",
    "ConfiguredProviders",
    "EngineStatus",
    "EspeakInvocation",
    "EspeakNgEngine",
    "HaTemplateRenderer",
    "HaTtsEngine",
    "HttpTtsEngine",
    "InstalledVoice",
    "MemoryVerdict",
    "ModuleFinder",
    "NoPaidProviders",
    "NullTtsEngine",
    "PaidProviderLookup",
    "PhraseTemplating",
    "PiperEngine",
    "PiperProbe",
    "PiperVoicePort",
    "Preset",
    "PresetError",
    "PresetSummary",
    "ProcessSpawner",
    "ProviderError",
    "ProviderPresets",
    "ProviderRecord",
    "ProviderSource",
    "RemoteTtsEngine",
    "RenderedPhrase",
    "ResolvedProvider",
    "SettingsReader",
    "SpawnedProcess",
    "SuggestedVoice",
    "SynthesisCosting",
    "SynthesisTiming",
    "TemplateRenderer",
    "TtsCache",
    "TtsCost",
    "TtsEngine",
    "TtsError",
    "TtsOptions",
    "TtsRenderError",
    "TtsSpeechSource",
    "VoiceDownloadJob",
    "VoiceDownloadRegistry",
    "VoiceError",
    "VoiceErrorCode",
    "VoiceManager",
    "VoiceState",
    "baked_root",
    "build_speech_source",
    "cache_key",
    "clean_detail",
    "directory_usage",
    "expected_bake_sha",
    "has_template_syntax",
    "install_url",
    "is_static",
    "load_presets",
    "looks_like_markup",
    "new_provider_id",
    "options_for",
    "parse_provider",
    "parse_providers",
    "read_meminfo",
    "read_providers",
    "sanitise_render",
    "sniff_container",
    "suffix_for_format",
    "validate_rendered_audio",
    "voice_id_problem",
]
