-- ---------------------------------------------------------------------------
-- settings: one row per key. Values are JSON so each key can have a real type.
-- ---------------------------------------------------------------------------
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,          -- JSON-encoded
    updated_at  TEXT NOT NULL           -- ISO-8601 UTC
);

-- Keys and their types (all optional; defaults applied in code):
--   ha.base_url            string   "http://homeassistant.local:8123"
--   ha.token               string   long-lived access token (never sent to browser)
--   mqtt.host              string   "core-mosquitto.local"
--   mqtt.port              integer  1883
--   mqtt.username          string
--   mqtt.password          string
--   mqtt.tls               boolean  false
--   mqtt.discovery_prefix  string   "homeassistant"
--   mqtt.instance_id       string   "default"     (see 02-mqtt-buttons.md)
--   server.host            string   "127.0.0.1"
--   server.port            integer  8080
--   audio.device_hint      string   ""       substring match, e.g. "USB"
--   audio.backend          string   "mpv"     "mpv" | (future) "sounddevice"
--   audio.volume           integer  70        0-100
--   audio.quiet_hours_enabled boolean false
--   audio.quiet_hours_start   string  "22:00"  local time
--   audio.quiet_hours_end     string  "07:00"
--   audio.quiet_hours_volume  integer 0        0 = silent, else clamp
--   buttons.default_dedupe_seconds integer 3
--   tts.default_engine     string   "ha_tts" | "piper" | "espeak_ng"
--   tts.ha.engine_id       string   "tts.piper"
--   tts.ha.language        string   "en_GB"
--   tts.piper.voice        string   "en_GB-alan-low"
--   tts.espeak_voice       string   "en-gb"
--   tts.espeak_speed       integer  160
--   ui.theme               string   "dark" | "light" | "auto"
--   ui.accent_color        string   "#4f9cf9"
--   ui.dashboard_columns   integer  0        0 = auto
--   ui.hide_cursor         boolean  true     kiosk nicety
--   ui.show_clock          boolean  true
--   system.log_level       string   "info"

-- ---------------------------------------------------------------------------
-- entities: the status tiles. Ordered by sort_order.
-- ---------------------------------------------------------------------------
CREATE TABLE entities (
    id              INTEGER PRIMARY KEY,
    entity_id       TEXT NOT NULL UNIQUE,      -- "lock.front_door"
    display_name    TEXT,                      -- NULL -> friendly_name from HA -> entity_id
    icon            TEXT,                      -- "mdi:door" or "custom:cat.svg"; NULL -> auto
    accent_color    TEXT,                      -- "#ff5555"; NULL -> derived from state
    show_attributes TEXT,                      -- JSON array of attribute names, e.g. ["battery_level"]
    hide_when_unavailable INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- sounds: the library index. Files may also exist on disk unindexed until scan.
-- ---------------------------------------------------------------------------
CREATE TABLE sounds (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,               -- display name, defaults to filename
    source_type   TEXT NOT NULL,               -- "file" | "url"
    path          TEXT,                        -- relative to data/sounds/ when source_type=file
    url           TEXT,                        -- when source_type=url
    sha256        TEXT,                        -- integrity + dedupe of downloads
    format        TEXT,                        -- "mp3" | "wav" | "ogg" | ...
    duration_ms   INTEGER,
    bytes         INTEGER,
    missing       INTEGER NOT NULL DEFAULT 0,  -- file vanished -> shown as broken in UI
    created_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX sounds_unique_file ON sounds(path) WHERE path IS NOT NULL;

-- ---------------------------------------------------------------------------
-- buttons: the chime buttons exposed to Home Assistant over MQTT.
-- ---------------------------------------------------------------------------
CREATE TABLE buttons (
    id                 INTEGER PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,   -- "front-door"; used in MQTT topics
    name               TEXT NOT NULL,          -- "Front Door Chime" (HA entity name)
    icon               TEXT,                   -- "mdi:doorbell"
    action_type        TEXT NOT NULL,          -- "sound" | "tts"
    sound_id           INTEGER REFERENCES sounds(id) ON DELETE SET NULL,
    tts_text           TEXT,                   -- may contain HA templates
    tts_engine         TEXT,                   -- NULL -> settings tts.default_engine
    -- Pre-rendered speech for static (template-free) phrases. When baked_sha matches the
    -- sha of the current text+engine+options, a press plays the file directly with no TTS
    -- work. NULL means never successfully baked; the press path bakes on demand.
    baked_sha          TEXT,
    baked_at           TEXT,
    baked_error        TEXT,                   -- last failure message, cleared on success
    volume_override    INTEGER,                -- 0-100, NULL -> global volume
    dedupe_seconds     INTEGER,                -- NULL -> settings default
    show_on_dashboard  INTEGER NOT NULL DEFAULT 0,
    quiet_hours_exempt INTEGER NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    sort_order         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (action_type IN ('sound','tts')),
    CHECK (action_type <> 'sound' OR sound_id IS NOT NULL),
    CHECK (action_type <> 'tts'   OR tts_text IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- press_log: why a sound played (or did not). Newest first in the UI, pruned.
-- ---------------------------------------------------------------------------
CREATE TABLE press_log (
    id            INTEGER PRIMARY KEY,
    button_id     INTEGER REFERENCES buttons(id) ON DELETE SET NULL,
    button_name   TEXT NOT NULL,               -- denormalised: survives deletion
    pressed_at    TEXT NOT NULL,
    source        TEXT NOT NULL,               -- "mqtt" | "ui_test"
    outcome       TEXT NOT NULL,               -- "played" | "queued" | "deduped"
                                               -- | "suppressed_quiet_hours"
                                               -- | "failed"
    detail        TEXT,                        -- error message, engine used, etc.
    sound_name    TEXT,
    duration_ms   INTEGER
);

CREATE INDEX press_log_pressed_at ON press_log(pressed_at DESC);
