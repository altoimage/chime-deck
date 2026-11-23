/**
 * Typed transport for the local HTTP API, plus the wire types the store and
 * the websocket client share.
 *
 * The interfaces mirror the FastAPI models by hand (design 05 §5: a generated
 * client is a later ticket). Two families exist on purpose:
 *
 *   - `*Row` / `*Data` — the wire shape, snake_case, exactly as the backend
 *     sends it. Never reach for these in a component.
 *   - `*Tile` / `HealthState` — the camelCase view shape the UI renders.
 *
 * Error contract (`src/chime_deck/api/errors.py`): every failure is
 * `{"error": {"code", "message", "detail"}}` and `message` is human text meant
 * to be shown verbatim. `request()` turns that into an `ApiError` and performs
 * exactly one fetch per call — no retry loop, so a backend that is down can
 * never make the UI spin.
 *
 * Tests inject a fake fetch through `setFetchImpl()`; nothing here opens a
 * socket of its own. This module imports nothing from the stores or the
 * websocket client, so it stays a leaf (`device-clock.ts` is pure too), and
 * `readServerClock()` is the one call that reads a response header rather than a
 * body.
 */

import { parseHttpDate } from './device-clock.js';

/** One resolved `show_attributes` entry: what a tile can render. */
export interface AttributeHint {
  name: string;
  label: string;
  value: string;
}

/** One entity row of a `snapshot` frame, in the hub's snake_case shape. */
export interface EntityRow {
  id: number;
  entity_id: string;
  display_name: string | null;
  state: string | null;
  attributes: Record<string, unknown>;
  icon: string;
  icon_source: string;
  accent_color: string | null;
  last_changed: string | null;
  displayed_state: string | null;
  unit_of_measurement: string | null;
  attribute_hints: AttributeHint[];
  /**
   * The tone the backend resolved for this tile's CURRENT state — `ok`, `warn`,
   * `alert` or `off` — or `null` when there is no state at all. The UI renders
   * it verbatim: state→colour is the backend's table (HSD-d5x2), so nothing on
   * this side decides a colour from a state string.
   */
  tone: string | null;
  /** The tile's explicit state→tone overrides, or `null` when it has none. */
  state_tones: Record<string, string> | null;
  /**
   * Whether this tile's colour follows its state. Carried for the editor, which
   * is the only screen that writes it; the dashboard paints `tone` either way.
   */
  colour_by_state: boolean;
  /**
   * Whether an `unavailable` tile is hidden rather than greyed out. Optional
   * because the field is newer than the frame that carries it: a snapshot from
   * an older backend simply omits it, and `toEntityTile` then defaults to
   * `false` ("show it") rather than hiding a tile nobody asked to hide.
   */
  hide_when_unavailable?: boolean;
  /**
   * Whether the owner has this tile lit (D29). Optional for the same reason as
   * `hide_when_unavailable` — the field is newer than the frame that carries it,
   * so an older backend omits it and `toEntityTile` defaults to `false`
   * ("not selected") rather than lighting a tile nobody tapped.
   */
  selected?: boolean;
}

/** One button row of a `snapshot` frame. */
export interface ButtonRow {
  id: number;
  name: string;
  slug: string;
  show_on_dashboard: boolean;
}

/**
 * A health payload. The backend always reports the three known components;
 * `detail` is only present while there is something to say (health.py).
 */
export interface HealthRow {
  ha: string;
  mqtt: string;
  audio: string;
  detail?: string;
}

/** A status tile: the snapshot row in the shape the UI renders. */
export interface EntityTile {
  id: number;
  entityId: string;
  displayName: string | null;
  state: string | null;
  attributes: Record<string, unknown>;
  icon: string;
  iconSource: string;
  accentColor: string | null;
  lastChanged: string | null;
  displayedState: string | null;
  unitOfMeasurement: string | null;
  attributeHints: AttributeHint[];
  /**
   * The backend's tone for the current state (`ok`/`warn`/`alert`/`off`), or
   * `null` when there is no state. `tile-view.ts` passes it straight through to
   * the tile's colour; the view type never carries the state→tone map, because
   * painting a tile does not need it and a second resolution path is exactly
   * the drift HSD-d5x2 removed.
   */
  tone: string | null;
  /**
   * Whether an `unavailable` tile is hidden rather than greyed out. Both the
   * snapshot row and the REST row carry it; `liveState` still keeps the value it
   * already knows when a snapshot omits the field (an older backend).
   */
  hideWhenUnavailable: boolean;
  /**
   * Whether this tile is lit (D29). Unlike `hideWhenUnavailable` this is
   * required: the view type always knows the answer, because the selection is
   * runtime intent the hub broadcasts and the store keeps, never a configuration
   * field a REST row could be silent about.
   */
  selected: boolean;
}

/** A button tile: config-only, because the runtime press facts arrive as events. */
export interface ButtonTile {
  id: number;
  name: string;
  slug: string;
  showOnDashboard: boolean;
}

/** Header health of the three backend components, with the latest reason. */
export interface HealthState {
  ha: string;
  mqtt: string;
  audio: string;
  detail: string | null;
}

/**
 * What one press did. `queued` is the first, unsettled report; every other
 * outcome is terminal (`played`/`failed` for a queued press, `deduped`,
 * `suppressed_quiet_hours` and `suppressed_muted` never queue at all).
 */
export type PressOutcome =
  | 'queued'
  | 'played'
  | 'failed'
  | 'deduped'
  | 'suppressed_quiet_hours'
  | 'suppressed_muted';

export interface SnapshotData {
  entities: EntityRow[];
  buttons: ButtonRow[];
  health: HealthRow;
  config_version: number;
}

/**
 * One live entity delta. The optional presentation keys are emitted by the
 * same backend `presentation_hints` call used for a snapshot row (HSD-7oto),
 * so the client derives no word of its own. An absent key means "unchanged"
 * (for compatibility with older backends); an explicit `null` clears the value.
 *
 * `state` is optional for the same "absent means unchanged" reason, and because
 * a selection toggle with no state change arrives as a MINIMAL frame whose keys
 * are exactly `{entity_id, selected}` (D29). A frame that carried no `state`
 * must never be read as "the state is now null".
 */
export interface EntityStateData {
  entity_id: string;
  state?: string | null;
  attributes?: Record<string, unknown>;
  last_changed?: string | null;
  displayed_state?: string | null;
  unit_of_measurement?: string | null;
  attribute_hints?: AttributeHint[];
  /**
   * The backend's tone for the state this frame carries. Optional for the same
   * "absent means unchanged" reason as its siblings: a frame that names no tone
   * must never be read as "the tone is now null", because that would recolour
   * the tile grey on, say, a selection toggle.
   */
  tone?: string | null;
  /** The tile's ABSOLUTE selection after the change — never a toggle to apply. */
  selected?: boolean;
}

export interface PressData {
  button_id: number;
  button_name: string;
  outcome: PressOutcome;
  pressed_at: string;
}

export interface ConfigChangedData {
  what: string;
}

export interface SnapshotMessage {
  type: 'snapshot';
  data: SnapshotData;
}

export interface EntityStateMessage {
  type: 'entity_state';
  data: EntityStateData;
}

export interface PressMessage {
  type: 'press';
  data: PressData;
}

export interface ConfigChangedMessage {
  type: 'config_changed';
  data: ConfigChangedData;
}

export interface HealthMessage {
  type: 'health';
  data: HealthRow;
}

/** The server's declared heartbeat cadence, used to derive the client timeout. */
export interface HeartbeatData {
  interval_s: number;
}

export interface HeartbeatMessage {
  type: 'heartbeat';
  data: HeartbeatData;
}

/** Every frame the backend may send; the envelope is always `{type, data}`. */
export type WireMessage =
  | SnapshotMessage
  | EntityStateMessage
  | PressMessage
  | ConfigChangedMessage
  | HealthMessage
  | HeartbeatMessage;

/**
 * `GET /api/entities` row: one stored tile with its live facts already resolved
 * (`src/chime_deck/api/entities.py::EntityResponse`).
 *
 * The two icon fields are different things and must not be confused. `icon` is
 * the **resolved** D16 icon — the string the dashboard draws — while
 * `icon_override` is the raw stored column (`mdi:<name>`, `custom:<file>` or
 * `null`). An editor writes back `icon_override` and **never** `icon`: saving
 * what the resolver picked would turn a fallback into the user's deliberate
 * choice, and the row could never recover the choice it overwrote.
 *
 * `warning` is always present and is a finished sentence (`null` when there is
 * nothing to say), because a stored `custom:` file can disappear from
 * `data/icons/` at any moment.
 */
export interface EntityResponse {
  id: number;
  entity_id: string;
  display_name: string | null;
  icon: string;
  icon_source: string;
  icon_override: string | null;
  accent_color: string | null;
  show_attributes: string[];
  hide_when_unavailable: boolean;
  sort_order: number;
  created_at: string;
  state: string | null;
  displayed_state: string | null;
  unit_of_measurement: string | null;
  attribute_hints: AttributeHint[];
  /**
   * The tone resolved for the state above, or `null` when there is no state.
   * `state_tones` is this tile's explicit overrides (`null` = none) and
   * `colour_by_state` is the flag the editor writes; the dashboard reads `tone`
   * and ignores the other two.
   */
  tone: string | null;
  state_tones: Record<string, string> | null;
  colour_by_state: boolean;
  warning: string | null;
}

/**
 * Body of `POST /api/entities` and `PATCH /api/entities/{id}`
 * (`EntityCreateRequest` / `EntityUpdateRequest`).
 *
 * Every key is optional: a patch omits what it does not change, while an
 * explicit `null` clears a nullable field. `icon` here is the **override** in
 * its stored form (`mdi:<name>`, `custom:<file>`, `null` to clear), never a
 * resolved icon. `sort_order` is absent on purpose — reordering has its own
 * route, so a patch can never silently move a tile.
 */
export interface EntityWriteBody {
  entity_id?: string;
  display_name?: string | null;
  icon?: string | null;
  accent_color?: string | null;
  show_attributes?: string[] | null;
  hide_when_unavailable?: boolean;
  /**
   * The tile's explicit state→tone overrides: a whole map, or `null` to clear
   * it (the resolver then falls back to the built-in table). Absent means
   * "unchanged", so a patch never rewrites a map it did not touch.
   */
  state_tones?: Record<string, string> | null;
  /** Whether this tile's colour follows its state. */
  colour_by_state?: boolean;
}

/** `DELETE /api/entities/{id}`: the row is gone, and the body says which one. */
export interface DeleteEntityResponse {
  deleted: boolean;
  id: number;
  entity_id: string;
}

/**
 * `GET /api/entities/state-tones` — the tone vocabulary and the built-in table
 * (`src/chime_deck/api/entities.py`).
 *
 * `tones` is the ORDERED vocabulary a mapping's values must come from, so the
 * editor renders its tone controls in that order instead of inventing one.
 * `defaults` is the table the resolver applies to a tile with no explicit
 * mapping: the editor pre-fills from it, which is why it reads the table rather
 * than a copy of it — a UI that hard-coded these pairs would be the second
 * source of truth this ticket exists to remove.
 */
export interface StateToneDefaults {
  tones: string[];
  defaults: Record<string, string>;
}

/**
 * `POST /api/tiles/{tile_id}/selection` — one tile's selection after a toggle
 * (`src/chime_deck/api/tiles.py::TileSelectionResponse`).
 *
 * `id` is the entity row's numeric primary key (the snapshot row's `id`), while
 * the selection itself is keyed on the `entity_id` string, so reordering the
 * tiles never moves a selection.
 */
export interface TileSelectionResponse {
  id: number;
  entity_id: string;
  selected: boolean;
}

/**
 * `GET /api/settings` — every non-secret setting plus whether each secret is stored.
 *
 * A secret's value never travels back out, so `secrets` is how the settings
 * screen can say "stored"/"not set" without pretending to know the value.
 */
export interface SettingsResponse {
  settings: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

/**
 * `POST /api/settings/test/audio` — `ok` carries the verdict, `error` is human text.
 *
 * A connection test answers HTTP 200 with `ok: false` when the test ran and
 * failed: the request succeeded, so that is a result, not an `ApiError`.
 */
export interface AudioTestResponse {
  ok: boolean;
  device: string | null;
  latency_ms: number | null;
  error: string | null;
}

/**
 * One output `GET /api/audio/devices` reports
 * (`src/chime_deck/audio/devices.py::AudioDevice`).
 *
 * `name` is mpv's device name — the value a stored `audio.device_hint` matches as
 * a case-insensitive substring and the value written back to the player. `hostapi`
 * is the prefix-derived label (libmpv exposes no host-API field), so the two must
 * never be swapped: a hint built from the label resolves to nothing.
 */
export interface AudioDeviceRow {
  name: string;
  hostapi: string;
  is_default: boolean;
}

/**
 * `GET /api/audio/devices` — what the device can see, and what it resolved.
 *
 * `resolved_device` is the device the running backend actually picked for the
 * stored hint, and `null` means it fell back to the system default: that pair is
 * the only way the UI can say "your hint matched nothing" out loud, because the
 * backend itself only logs a warning (HSD-h13q). `hint` is echoed back so a
 * panel cannot show a value it typed itself, and `error` is a whole sentence to
 * render verbatim.
 */
export interface AudioDevicesResponse {
  ok: boolean;
  hint: string;
  resolved_device: string | null;
  system_default: string;
  devices: AudioDeviceRow[];
  error: string | null;
}

/**
 * `GET /api/audio/hardware` — read-only status for the output's own volume stage.
 *
 * An unavailable mixer is a normal HTTP 200 verdict; `reason` is the sentence
 * the panel shows beside its disabled hardware control. `configured` is the
 * stored request, `percent` is the device's own read-back after the last write,
 * and `applied` records what that write actually reached.
 */
export interface HardwareVolumeResponse {
  ok: boolean;
  available: boolean;
  device: string | null;
  control: string | null;
  /** The device's own answer, read after the last write; coarse hardware steps can make it differ from `configured`. */
  percent: number | null;
  muted: boolean;
  reason: string;
  /** The stored `audio.hardware_volume` request; it is never overwritten with a read-back or rounded. */
  configured: number | null;
  /** The level the last write actually reached, or null when that write did not happen or nothing was written. */
  applied: number | null;
}

/**
 * The device's own clock as one HTTP exchange revealed it.
 *
 * There is no wall-clock field in the API (design 05 §3's `/api/health` is
 * `{status, version, uptime_s}`), so the reading comes from the `Date` response
 * header. `server_time_ms` is `null` when the device did not report a usable
 * time — never a guessed one. The pair is what matters: comparing the device's
 * stamp with this browser's clock is what makes drift obvious, and the header is
 * only good to a second, so no caller may claim sub-second precision.
 */
export interface ServerClockReading {
  /** The device's own clock as stamped on its response, or null when it did not report one. */
  server_time_ms: number | null;
  /** This browser's clock at the midpoint of the request, for the comparison. */
  client_time_ms: number;
}

/**
 * `POST /api/settings/test/ha` — the Home Assistant verdict.
 *
 * `ha_version` and `entity_count` are the facts a successful test reports;
 * `error` is a whole sentence to render verbatim on failure.
 */
export interface HaTestResponse {
  ok: boolean;
  ha_version: string | null;
  entity_count: number | null;
  error: string | null;
}

/**
 * `POST /api/settings/test/mqtt` — the MQTT broker verdict.
 *
 * `broker_version` is whatever the broker reported (often absent); `error` is
 * a whole sentence to render verbatim on failure. `auth_failed` is the
 * machine-readable classification — a broker that rejected the username or
 * password — so no screen has to string-match `error`, which stays the whole
 * sentence to render.
 */
export interface MqttTestResponse {
  ok: boolean;
  broker_version: string | null;
  error: string | null;
  auth_failed: boolean;
}

/**
 * `GET /api/ha/entities` row: one Home Assistant entity for the tile picker
 * (`src/chime_deck/api/ha.py::HaEntityRow`).
 *
 * `state` is `null` when Home Assistant reports none, which is a real state (an
 * entity that has never run), not a missing row. `icon`/`icon_source` come from
 * the same D16 resolution the tiles use, so the picker cannot promise an icon
 * the tile will not show.
 */
export interface HaEntityRow {
  entity_id: string;
  name: string;
  state: string | null;
  icon: string;
  icon_source: string;
  domain: string;
  device_class: string | null;
}

/**
 * `GET /api/ha/entities?q=` — the hits plus the sentence explaining why there
 * are none (`HaEntitySearchResponse`).
 *
 * The invariant is load-bearing: `warning !== null` **only** when Home Assistant
 * could not be asked, and then `entities` is empty. Rendering that case as "no
 * matches" would tell the user their spelling is wrong when the doorbell simply
 * could not reach Home Assistant; `{ entities: [], warning: null }` is the
 * honest no-match answer.
 */
export interface HaEntitySearchResponse {
  entities: HaEntityRow[];
  warning: string | null;
}

/**
 * `GET /api/ha/entities/{entity_id}` — one entity described live for the editor's
 * preview (`HaEntityResponse`).
 *
 * `found` is three-state on purpose: `null` means the lookup could not answer,
 * `false` means Home Assistant answered that the entity does not exist, and
 * `true` means the values are live. Those two failures need different fixes, so
 * they must not collapse into one "not found". There is no user icon override
 * here — this describes Home Assistant, not a stored tile.
 */
export interface HaEntityDetail extends HaEntityRow {
  attributes: Record<string, unknown>;
  last_changed: string | null;
  found: boolean | null;
  warning: string | null;
}

/**
 * One `GET /api/icons/search` hit (`src/chime_deck/api/icons.py::IconSearchResult`).
 *
 * `name` is already the full `mdi:<name>` reference, so a pick can be stored as
 * an entity's icon verbatim; `path` is SVG path data in the shipped view box, so
 * a 120-tile grid draws itself with zero further requests.
 */
export interface IconSearchResult {
  name: string;
  path: string;
}

/**
 * `GET /api/icons/search` — one page of the shipped icon set (`IconSearchResponse`).
 *
 * `total` is the whole set, `matched` is the hit count *before* the cap, so a
 * truncated answer describes itself: `matched > icons.length` means "keep typing".
 */
export interface IconSearchResponse {
  version: string;
  total: number;
  matched: number;
  icons: IconSearchResult[];
}

/** `GET /api/icon/{name}` — one icon's path data, ready to draw (`IconPathResponse`). */
export interface IconPathResponse {
  name: string;
  path: string;
  view_box: string;
}

/**
 * One usable file in the user's `data/icons/` folder (`CustomIconEntry`).
 *
 * `reference` is `custom:<name>` — the exact string an entity stores — so it is
 * kept verbatim rather than rebuilt from `name`; `bytes` is the size on disk.
 */
export interface CustomIconEntry {
  name: string;
  reference: string;
  bytes: number;
}

/**
 * One candidate file the server refuses to serve (`CustomIconSkip`).
 *
 * `reason` is a finished human sentence, rendered verbatim. A skipped file is
 * never offered as a choice — the editor lists it so the user learns *before*
 * picking that, say, a script-bearing SVG will be refused.
 */
export interface CustomIconSkip {
  name: string;
  reason: string;
}

/**
 * `GET /api/icons/custom` — the user's own icons plus what was refused
 * (`CustomIconListResponse`).
 *
 * An empty listing is normal: a fresh install has no `data/icons/` at all. `total`
 * counts only the usable files, so `skipped` is never part of it.
 */
export interface CustomIconListResponse {
  total: number;
  icons: CustomIconEntry[];
  skipped: CustomIconSkip[];
}

/**
 * `POST /api/mqtt/republish` — the result of re-announcing every button.
 *
 * `announced` is how many buttons were announced, `queued` means the broker
 * was offline so the payloads are buffered, and `failed` counts the rejected
 * payloads. `error` is a whole sentence to render verbatim.
 */
export interface RepublishResponse {
  ok: boolean;
  announced: number;
  queued: boolean;
  failed: number;
  error: string | null;
}

/**
 * `POST /api/buttons/{id}/press` — what one press did, as the press log recorded it.
 *
 * `synthesis_ms` is how long this press's own speech synthesis took. `null` means
 * nothing was synthesised for this press — a cache or bake hit, a sound button, a
 * failed press, or an engine that keeps no measurement — which for a hit is a
 * success, not a missing reading; it is never the same number as `latency_ms`.
 */
export interface PressResponse {
  button_id: number | null;
  button_name: string;
  source: string;
  outcome: string;
  pressed_at: string;
  code: string | null;
  detail: string | null;
  synthesis_ms: number | null;
}

/** `GET /api/buttons` row: what `listButtons()` maps from. */
export interface ButtonResponse {
  id: number;
  name: string;
  slug: string;
  show_on_dashboard: boolean;
}

/** The two MQTT topics one button owns (`MqttTopics` in `api/buttons.py`). */
export interface MqttTopics {
  command: string;
  discovery: string;
}

/** What a rename did to the button's MQTT identity (`slug_change` in the response). */
export interface SlugChange {
  old_slug: string;
  new_slug: string;
  old_topics: MqttTopics | null;
  new_topics: MqttTopics | null;
  /** The server's finished sentence about the Home Assistant entity that moves; shown verbatim. */
  warning: string;
}

/**
 * `GET /api/buttons` row — the full stored button, not the websocket snapshot shape.
 *
 * Two things this type does **not** promise, both deliberate:
 *
 *   - `mqtt` is populated for **every** button, including a disabled one. The
 *     MQTT exclusion of a disabled button happens at publish time in
 *     `mqtt/discovery.py`, not in this route, so `mqtt: null` means only one
 *     thing: the broker settings themselves are unusable (`_mqtt_settings`
 *     swallowed a configuration error). A disabled button with working broker
 *     settings still reports its topics.
 *   - The bake fields are carried on every row (nullable), and the bake routes exist.
 *     A build older than HSD-dzek omits those keys, so their optional type is also the
 *     feature-detection signal the panels use to render no bake control on that build.
 *
 * `action_type` stays a `string` rather than a union so a stored value this UI
 * does not know still renders instead of type-failing; `slug_change` is only
 * non-null on the answer that followed a rename.
 */
export interface ButtonDetail {
  id: number;
  slug: string;
  name: string;
  icon: string | null;
  /** `'sound'` | `'tts'` in practice, kept wide on purpose (see above). */
  action_type: string;
  sound_id: number | null;
  tts_text: string | null;
  tts_engine: string | null;
  /** Optional because builds older than HSD-dzek omit these keys; their absence makes panels hide the feature. */
  baked_sha?: string | null;
  baked_format?: string | null;
  baked_at?: string | null;
  baked_error?: string | null;
  volume_override: number | null;
  dedupe_seconds: number | null;
  show_on_dashboard: boolean;
  quiet_hours_exempt: boolean;
  enabled: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  /** `null` ONLY when the broker settings are unusable — never because the button is disabled. */
  mqtt: MqttTopics | null;
  /** Non-null only on the response that followed a rename. */
  slug_change: SlugChange | null;
}

/** The server-measured file count and byte total for one speech directory; never calculate these in the browser. */
export interface DiskUsage {
  files: number;
  bytes: number;
}

/** Device-measured usage for `data/tts/baked/` and `data/tts-cache/`, respectively. */
export interface BakeUsageResponse {
  baked: DiskUsage;
  cache: DiskUsage;
}

/** One button id whose bake failed; `error` is the engine's sentence, rendered verbatim. */
export interface BakeFailure {
  button: number;
  error: string;
}

/** The four distinct outcomes from `POST /api/tts/bake_all`; the skip lists are never interchangeable. */
export interface BakeAllResponse {
  baked: number[];
  skipped_dynamic: number[];
  skipped_current: number[];
  failed: BakeFailure[];
}

/**
 * `PATCH /api/buttons/{id}` body: omitted keys keep their stored value, `null` clears a
 * nullable one.
 *
 * `sort_order` is absent on purpose — there is no reorder in this ticket.
 *
 * `acknowledge_paid_template` is a **request-scoped** D25 confirmation, never a stored
 * field: a `tts_text` that carries template syntax while the engine bills per character
 * is rejected with a 422 whose `detail` is
 * `{requires: 'acknowledge_paid_template', field: 'tts_text', engine}`. Resending the same
 * body with `true` accepts the cost for that one request, and the flag is not persisted.
 */
export interface ButtonWriteBody {
  name?: string;
  slug?: string;
  icon?: string | null;
  action_type?: string;
  sound_id?: number | null;
  tts_text?: string | null;
  tts_engine?: string | null;
  volume_override?: number | null;
  dedupe_seconds?: number | null;
  show_on_dashboard?: boolean;
  quiet_hours_exempt?: boolean;
  enabled?: boolean;
  /** The one-request D25 confirmation; see this interface's own note above. */
  acknowledge_paid_template?: boolean;
}

/** `POST /api/buttons` body (answers 201 with the stored `ButtonDetail`). */
export interface ButtonCreateBody {
  name: string;
  action_type: string;
  /** `null`/absent lets the server derive the slug from the name. */
  slug?: string | null;
  icon?: string | null;
  sound_id?: number | null;
  tts_text?: string | null;
  tts_engine?: string | null;
  volume_override?: number | null;
  dedupe_seconds?: number | null;
  show_on_dashboard?: boolean;
  quiet_hours_exempt?: boolean;
  enabled?: boolean;
  /**
   * The one-request D25 confirmation, never stored: a 422 with
   * `detail.requires === 'acknowledge_paid_template'` (and `detail.field === 'tts_text'`,
   * `detail.engine`) is accepted by resending the same body with this `true`.
   */
  acknowledge_paid_template?: boolean;
}

/**
 * `GET /api/sounds` row.
 *
 * The fields are what the index stored when the file was last published, so `duration_ms`
 * and `bytes` are the server's probe results rather than anything this UI measures.
 */
export interface SoundRow {
  id: number;
  name: string;
  source_type: string;
  path: string | null;
  url: string | null;
  sha256: string | null;
  format: string | null;
  duration_ms: number | null;
  bytes: number | null;
  /** The indexed file is gone from the library folder; the row is stale, not deleted. */
  missing: boolean;
  created_at: string;
}

/** One button a sound deletion would take with it, as the 409 and the 200 both name it. */
export interface AffectedButton {
  id: number;
  name: string;
  slug: string;
}

/** What one folder rescan changed, counted the way the server counts it. */
export interface SoundRescanResponse {
  /** Rows created by this scan. */
  added: number;
  /** Rows that transitioned to missing during this scan (a vanished file is flagged, never deleted). */
  removed: number;
  /** The total number of rows flagged missing after the scan — not a deletion count. */
  missing: number;
}

/** The outcome of a URL download. `created: false` means no new row was created. */
export interface SoundFromUrlResponse {
  created: boolean;
  sound: SoundRow;
}

/** What deleting one sound did to the buttons that used it. */
export interface SoundDeleteResponse {
  affected_buttons: AffectedButton[];
}

/**
 * One speech engine in `GET /api/tts/engines`.
 *
 * `reason` is the engine's own whole actionable sentence (RAM, missing extra, missing
 * model) and is shown verbatim; this UI never invents a reason.
 */
export interface TtsEngineStatus {
  name: string;
  available: boolean;
  reason: string | null;
  last_synthesis_ms: number | null;
}

/**
 * `GET /api/tts/engines` — `disabled` true means speech is switched off (D27).
 *
 * The route never answers 500: a missing resolver, the deliberate off state and a hung
 * status call are each a 200 with an empty `engines` list and a `warning` sentence. That
 * is why "the list did not answer" and "speech is off" must be told apart by `disabled`
 * and `warning`, never by an empty array.
 */
export interface TtsEnginesResponse {
  disabled: boolean;
  engines: TtsEngineStatus[];
  warning: string | null;
}

/**
 * How one preset authenticates (`AuthStyleResponse`), as the UI shows it.
 *
 * `style` is `'bearer'` (the key travels in `Authorization` behind `prefix`),
 * `'header'` (the key travels in the header named `name`) or `'none'`. `prefix` is an
 * empty string when the style has none, so it is text to render, never a signal.
 */
export type TtsProviderAuthStyle = {
  style: 'bearer' | 'header' | 'none';
  name: string;
  prefix: string;
};

/**
 * One preset's request shape (`ProviderShapeResponse`), unchanged.
 *
 * This is data, not this UI's own template: a `custom` provider copies it into its
 * `overrides`, and `body` is the preset's JSON body or its SSML text verbatim. The
 * placeholder walk (`providers.ts`) reads `path`, the `headers` values and every string
 * leaf of `body`, so a shape is never inspected field by field here.
 */
export type TtsProviderShape = {
  method: string;
  path: string;
  headers: Record<string, string>;
  auth: TtsProviderAuthStyle;
  body_kind: 'json' | 'text';
  body: unknown;
  response: string;
  format: string;
};

/**
 * One preset library row (`PresetSummaryResponse`): what the picker shows before a provider
 * exists, plus the exact request it will send.
 *
 * `cost_per_1k_chars_usd` is an estimate copied from a published rate and `pricing_note`
 * says so; `null` is "no price is known", never a free request. `requires_base_url` is true
 * when the preset cannot name its own server, so the form must ask for the address.
 */
export type TtsPresetSummary = {
  id: string;
  label: string;
  description: string;
  paid: boolean;
  format: string;
  cost_per_1k_chars_usd: number | null;
  pricing_note: string;
  requires_base_url: boolean;
  shape: TtsProviderShape;
};

/**
 * One configured provider (`ProviderResponse`), as the list shows it.
 *
 * `has_api_key` is the only thing that ever describes the key — the key itself is never
 * read back. `overrides` is `{}` unless the record uses the `custom` preset, and
 * `cost_per_1k_chars_usd` is `null` when no price is known (never a fabricated `0`).
 */
export type TtsProvider = {
  id: string;
  name: string;
  preset: string;
  base_url: string;
  voice: string;
  language: string;
  paid: boolean;
  cost_per_1k_chars_usd: number | null;
  has_api_key: boolean;
  overrides: Record<string, unknown>;
};

/**
 * `GET /api/tts/providers` — the configured providers, the preset library and the selected
 * id.
 *
 * The route never fails: an unreadable stored record is skipped and named in `warning`, so
 * an empty `providers` list means "none configured" only when `warning` is `null`. A
 * `selected` id no provider carries means no provider is selected; the panel shows that as
 * "none", never as the first row.
 */
export type TtsProvidersResponse = {
  providers: TtsProvider[];
  presets: TtsPresetSummary[];
  selected: string;
  warning: string | null;
};

/**
 * One test synthesis (`TtsProviderTestResponse`).
 *
 * `latency_ms` is the **measured** round trip and `chars` the characters actually sent;
 * both are `null` when no usable audio came back. `cost_per_100_chars_usd` is the
 * provider's configured price per 100 characters and `estimated_cost_usd` prices this one
 * phrase — both are `null` when no price is known, never a fabricated `0`. Nothing in this
 * UI may invent a number that is absent here.
 */
export type TtsProviderTestResponse = {
  ok: boolean;
  provider: string;
  latency_ms: number | null;
  chars: number | null;
  format: string | null;
  cost_per_100_chars_usd: number | null;
  estimated_cost_usd: number | null;
  error: string | null;
};

/**
 * `POST`/`PATCH /api/tts/providers` body; every field is optional on a patch.
 *
 * `api_key` is **write-only** and its three states are not interchangeable: absent leaves
 * the stored key alone, `null` clears it, and `''` is rejected by the server with "send
 * null to clear a secret, not an empty string". It is never returned — only
 * `has_api_key` — so a draft can never render a secret it did not just type.
 * `cost_per_1k_chars_usd` follows the same absent/`null` split: absent keeps the stored
 * price, `null` returns to the preset's estimate. `overrides` behaves the same way.
 */
export type TtsProviderWriteBody = {
  name?: string;
  preset?: string;
  base_url?: string;
  voice?: string;
  language?: string;
  paid?: boolean;
  cost_per_1k_chars_usd?: number | null;
  /** absent = unchanged, `null` = clear, `''` = rejected by the server. */
  api_key?: string | null;
  overrides?: Record<string, unknown>;
};

/**
 * `GET /api/health`: process liveness only. Per-component health travels over
 * the websocket (design 05 §3), so this is what a "is the backend reachable"
 * probe can rely on.
 */
export interface LivenessResponse {
  status: string;
  version: string;
  uptime_s: number;
}

/** `POST /api/auth/login`: the session cookie is set by the server, never exposed here. */
export interface LoginResponse {
  authenticated: boolean;
  required?: boolean;
}

/** `GET /api/auth/status`: public facts about the active listener and gate. */
export interface AuthStatusResponse {
  listener: 'admin' | 'loopback';
  password_set: boolean;
  token_required: boolean;
  protected: boolean;
  authenticated: boolean;
}

/** `POST /api/auth/password` and `/clear`: the password is never returned. */
export interface AdminPasswordResponse {
  password_set: boolean;
}

/**
 * The part of a `Response` this module uses, so a fake is trivial to write.
 *
 * `headers` is optional because exactly one call needs it — `readServerClock()`
 * reads the `Date` header — and a fake for every other route should not have to
 * invent one. A missing `headers` is therefore "the server reported no headers",
 * never a crash.
 */
export interface FetchResponse {
  readonly ok: boolean;
  readonly status: number;
  json(): Promise<unknown>;
  readonly headers?: { get(name: string): string | null };
}

export type FetchLike = (path: string, init?: RequestInit) => Promise<FetchResponse>;

export interface ApiErrorInit {
  status: number;
  code: string;
  message: string;
  detail: unknown;
}

/** A failed API call, described well enough to render without guessing. */
export class ApiError extends Error {
  /** Stable machine-readable reason (`validation_error`, `not_found`, …). */
  readonly code: string;
  /** HTTP status, or `0` when the request never got an answer. */
  readonly status: number;
  /** Structured context from the envelope (which field failed, which id). */
  readonly detail: unknown;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = 'ApiError';
    this.code = init.code;
    this.status = init.status;
    this.detail = init.detail;
  }
}

const NETWORK_ERROR_CODE = 'network_error';
const INVALID_RESPONSE_CODE = 'invalid_response';

let fetchImpl: FetchLike | null = null;

/**
 * Install a fetch implementation, or pass `null` to restore `globalThis.fetch`.
 * Tests use this so nothing in the suite can reach the network.
 */
export function setFetchImpl(fn: FetchLike | null): void {
  fetchImpl = fn;
}

let unauthorizedHandler: (() => void) | null = null;

/**
 * Install the callback raised when a call is refused for want of a credential,
 * or pass `null` to remove it.
 *
 * This is how the login prompt is raised from *any* call — a dashboard read, a
 * settings load, a login attempt — without every caller having to know that
 * authentication exists. The hook observes the failure and never replaces it:
 * the call still rejects with the same `ApiError`, so no caller's control flow
 * changes because a prompt happens to be listening.
 */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

/**
 * Report one failed response to the installed unauthorized hook, if any.
 *
 * Shared by `request()` and `readServerClock()` so both transport paths notice
 * a 401 `unauthorized` in exactly the same way, and a third path added later
 * only has to call this. Only a refusal for want of a credential qualifies: a
 * 401 with any other code, and every other status, is somebody else's problem.
 * Password mutation endpoints also use 401 for a rejected current password,
 * which is an inline form error rather than an expired-session signal.
 *
 * The handler is called exactly once per failed response and is deliberately
 * not awaited — it raises a prompt, it does not answer the request.
 */
function noteUnauthorized(failure: ApiError, path: string): void {
  const passwordRefusal =
    path === '/api/auth/password' || path === '/api/auth/password/clear';
  if (
    !passwordRefusal &&
    failure.status === 401 &&
    failure.code === 'unauthorized' &&
    unauthorizedHandler !== null
  ) {
    unauthorizedHandler();
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function resolveFetch(): FetchLike {
  if (fetchImpl !== null) {
    return fetchImpl;
  }
  const candidate: unknown = (globalThis as Record<string, unknown>).fetch;
  if (typeof candidate === 'function') {
    return candidate as FetchLike;
  }
  throw new ApiError({
    status: 0,
    code: NETWORK_ERROR_CODE,
    message: 'this environment has no fetch implementation',
    detail: null
  });
}

async function readBody(response: FetchResponse): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiError({
      status: response.status,
      code: INVALID_RESPONSE_CODE,
      message: `the server answered ${response.status} with a body that is not JSON`,
      detail: null
    });
  }
}

/**
 * The `ApiError` a request that never got an answer becomes.
 *
 * Shared by `request()` and `readServerClock()` so the two transport paths cannot
 * describe an unreachable device in two different ways: a status-0
 * `network_error` is what every caller handles.
 */
function networkFailure(cause: unknown): ApiError {
  return new ApiError({
    status: 0,
    code: NETWORK_ERROR_CODE,
    message: cause instanceof Error ? `the server could not be reached: ${cause.message}` : 'the server could not be reached',
    detail: null
  });
}

function errorFromEnvelope(status: number, body: unknown): ApiError {
  const envelope = isRecord(body) ? body.error : undefined;
  if (isRecord(envelope) && typeof envelope.code === 'string' && typeof envelope.message === 'string') {
    return new ApiError({ status, code: envelope.code, message: envelope.message, detail: envelope.detail ?? null });
  }
  return new ApiError({
    status,
    code: INVALID_RESPONSE_CODE,
    message: `the server answered ${status} without the error envelope`,
    detail: null
  });
}

/**
 * The `ApiError` a non-2xx answer becomes: the server's own envelope when it sent
 * one, the status alone when it did not (a proxy or a crash can answer with HTML).
 *
 * Extracted so `readServerClock()` maps a failure through the very same envelope
 * mapper as `request()` instead of duplicating the rules that decide what to show.
 */
async function failureFromResponse(response: FetchResponse): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // A proxy or a crash can answer with HTML; the status is all we know.
  }
  return errorFromEnvelope(response.status, body);
}

/**
 * One response header, or `null` when the response carries no `headers` object.
 *
 * A fake that omits `headers` models a server that reported none, and an absent
 * object must read as "no header" rather than throwing inside a clock probe.
 */
function readHeader(response: FetchResponse, name: string): string | null {
  const headers = response.headers;
  if (headers === undefined) {
    return null;
  }
  return headers.get(name);
}

/**
 * Fetch one path and parse the JSON body. Exactly one request per call: a
 * non-2xx answer and a malformed body both become an `ApiError`, never a raw
 * `SyntaxError`, so callers have a single failure type to handle.
 *
 * Caller headers are MERGED over the default `accept`, never substituted for
 * it: `patchSettings` adds `content-type` without silently dropping `accept`
 * (the defect this merge fixes). Headers are plain records everywhere in this
 * module, so a spread is the whole merge.
 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const impl = resolveFetch();
  const requestInit: RequestInit = {
    method: 'GET',
    ...init,
    headers: {
      accept: 'application/json',
      ...(init?.headers as Record<string, string> | undefined)
    }
  };

  let response: FetchResponse;
  try {
    response = await impl(path, requestInit);
  } catch (cause) {
    throw networkFailure(cause);
  }

  if (!response.ok) {
    const failure = await failureFromResponse(response);
    noteUnauthorized(failure, path);
    throw failure;
  }

  return (await readBody(response)) as T;
}

/** Map a hub entity row onto the view type. */
export function toEntityTile(row: EntityRow): EntityTile {
  return {
    id: row.id,
    entityId: row.entity_id,
    displayName: row.display_name,
    state: row.state,
    attributes: row.attributes ?? {},
    icon: row.icon,
    iconSource: row.icon_source,
    accentColor: row.accent_color,
    lastChanged: row.last_changed,
    displayedState: row.displayed_state,
    unitOfMeasurement: row.unit_of_measurement,
    attributeHints: row.attribute_hints ?? [],
    // `null` rather than `undefined` when an older backend omits the key: the
    // view type always knows an answer, and `tileView` falls back to grey only
    // for a value the four-tone vocabulary does not contain.
    tone: row.tone ?? null,
    hideWhenUnavailable: row.hide_when_unavailable ?? false,
    selected: row.selected ?? false
  };
}

/** Map a hub button row onto the view type. */
export function toButtonTile(row: ButtonRow): ButtonTile {
  return {
    id: row.id,
    name: row.name,
    slug: row.slug,
    showOnDashboard: row.show_on_dashboard
  };
}

/** Map a health payload onto the view type; `detail` is always present here. */
export function toHealthState(row: HealthRow): HealthState {
  return {
    ha: row.ha,
    mqtt: row.mqtt,
    audio: row.audio,
    detail: row.detail ?? null
  };
}

/**
 * Map a REST entity row onto the view type. The REST response carries no live
 * `attributes` and no `last_changed` (the live websocket frames do), so the
 * store preserves the values an `entity_state` frame already delivered when it
 * merges a refresh.
 *
 * It carries no selection either, by design: the configuration surface stays
 * runtime-free, so a config import can never resurrect a selection (D29). This
 * reads no `row.selected` and always answers `false`; `mergeEntities` puts the
 * live value back when the store merges a refresh, so a selected tile keeps its
 * border.
 */
export function toEntityTileFromResponse(row: EntityResponse): EntityTile {
  return {
    id: row.id,
    entityId: row.entity_id,
    displayName: row.display_name,
    state: row.state,
    attributes: {},
    icon: row.icon,
    iconSource: row.icon_source,
    accentColor: row.accent_color,
    lastChanged: null,
    displayedState: row.displayed_state,
    unitOfMeasurement: row.unit_of_measurement,
    attributeHints: row.attribute_hints ?? [],
    tone: row.tone ?? null,
    hideWhenUnavailable: row.hide_when_unavailable,
    selected: false
  };
}

/** Map a REST button row onto the view type. */
export function toButtonTileFromResponse(row: ButtonResponse): ButtonTile {
  return {
    id: row.id,
    name: row.name,
    slug: row.slug,
    showOnDashboard: row.show_on_dashboard
  };
}

/** Whether the backend process is serving (design 05 §3). */
export function getHealth(): Promise<LivenessResponse> {
  return request<LivenessResponse>('/api/health');
}

/** Log in with the shared token; a rejected token remains an `ApiError`. */
export function loginWithToken(token: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token })
  });
}

/** Log in to the admin listener with its configuration password. */
export function loginWithPassword(password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password })
  });
}

/** Read the current listener and authentication state. */
export function getAuthStatus(): Promise<AuthStatusResponse> {
  return request<AuthStatusResponse>('/api/auth/status');
}

/** Set or rotate the admin configuration password. */
export function setAdminPassword(body: {
  current: string | null;
  new: string;
}): Promise<AdminPasswordResponse> {
  return request<AdminPasswordResponse>('/api/auth/password', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/** Clear the admin configuration password. */
export function clearAdminPassword(current: string | null): Promise<AdminPasswordResponse> {
  return request<AdminPasswordResponse>('/api/auth/password/clear', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ current })
  });
}

/**
 * Read the device's own clock from the `Date` header of one `GET /api/health`.
 *
 * This is the ONE call in this module that cannot use `request<T>()`: it needs a
 * response header, and `request()` consumes the body and discards the response.
 * So it repeats exactly three steps — one fetch, the same `ApiError` for a network
 * failure (`networkFailure`), the same envelope mapper for a non-2xx answer
 * (`failureFromResponse`) — and nothing else.
 *
 * `client_time_ms` is stamped at the request/response midpoint, so the request's
 * own latency is not charged to the device. `server_time_ms` comes from the
 * `Date` header, which the device stamps at 1-second granularity: a missing
 * `headers` object, and a missing, blank or unparseable `date` header, all return
 * `null` rather than a time this function invented. The correct interpretation is
 * that this is the SERVER's (the device's) clock — the comparison against this
 * browser's clock is what makes drift obvious, and one second either way is not
 * drift. The health body itself is not read: it carries nothing this function
 * needs.
 */
export async function readServerClock(): Promise<ServerClockReading> {
  const impl = resolveFetch();
  const sentAt = Date.now();
  let response: FetchResponse;
  try {
    response = await impl('/api/health', {
      method: 'GET',
      headers: { accept: 'application/json' }
    });
  } catch (cause) {
    throw networkFailure(cause);
  }
  const receivedAt = Date.now();

  if (!response.ok) {
    const failure = await failureFromResponse(response);
    noteUnauthorized(failure, '/api/health');
    throw failure;
  }

  return {
    server_time_ms: parseHttpDate(readHeader(response, 'date')),
    client_time_ms: Math.round((sentAt + receivedAt) / 2)
  };
}

/** The configured entities, in `sort_order` order. */
export async function listEntities(): Promise<EntityTile[]> {
  const rows = await request<EntityResponse[]>('/api/entities');
  return rows.map(toEntityTileFromResponse);
}

/**
 * The configured entity rows exactly as stored, resolved icon included.
 *
 * The editor needs `icon_override`, `show_attributes`, `sort_order` and
 * `warning`, which the view type deliberately drops — so it reads the wire rows
 * rather than the tiles the dashboard renders.
 */
export function listEntityRows(): Promise<EntityResponse[]> {
  return request<EntityResponse[]>('/api/entities');
}

/**
 * The tone vocabulary and the built-in state→tone table the resolver applies.
 *
 * Read, never guessed. The tile editor pre-fills its tone controls from the
 * `defaults` table rather than hard-coding a copy, so a default revised in a
 * later backend release shows up in the editor without a frontend change. A
 * rejection is an `ApiError` like every other call here.
 */
export function getStateToneDefaults(): Promise<StateToneDefaults> {
  return request<StateToneDefaults>('/api/entities/state-tones');
}

/** Create one entity and read back the stored row (the route answers 201). */
export function createEntity(body: EntityWriteBody): Promise<EntityResponse> {
  return request<EntityResponse>('/api/entities', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Apply a partial update to one entity and read back the stored row.
 *
 * Omitted keys keep their stored value; an explicit `null` clears a nullable
 * field. A rejected `entity_id` or `icon` is a 422/409 `ApiError`, so a caller
 * renders `ApiError.message` rather than inventing a rule.
 */
export function updateEntity(id: number, body: EntityWriteBody): Promise<EntityResponse> {
  return request<EntityResponse>(`/api/entities/${encodeURIComponent(String(id))}`, {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Delete one entity. The answer names the row (`{deleted, id, entity_id}`) so a
 * caller can report what it removed without keeping its own copy.
 */
export function deleteEntity(id: number): Promise<DeleteEntityResponse> {
  return request<DeleteEntityResponse>(`/api/entities/${encodeURIComponent(String(id))}`, {
    method: 'DELETE'
  });
}

/**
 * Renumber the tiles in the submitted order. The body is the complete list of
 * entity ids, not a move: a reorder is the whole new arrangement, so it cannot
 * half-apply after a dropped connection.
 */
export function reorderEntities(ids: number[]): Promise<{ ids: number[] }> {
  return request<{ ids: number[] }>('/api/entities/reorder', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ ids })
  });
}

/**
 * Flip one tile's selection and answer the new value (D29).
 *
 * It is a **toggle**, not a set: the caller sends no body and no desired value,
 * because the panel is the only writer and the server owns the truth. The hub
 * also broadcasts the result as an `entity_state` frame, so a lost response
 * still leaves the tile correct — and a caller that already painted the change
 * optimistically stays right, because the frame carries the absolute value.
 *
 * A rejected toggle is an `ApiError`: 404 means no entity has that id.
 */
export function toggleTileSelection(tileId: number): Promise<TileSelectionResponse> {
  return request<TileSelectionResponse>(
    `/api/tiles/${encodeURIComponent(String(tileId))}/selection`,
    { method: 'POST' }
  );
}

/** The configured buttons, in `sort_order` order. */
export async function listButtons(): Promise<ButtonTile[]> {
  const rows = await request<ButtonResponse[]>('/api/buttons');
  return rows.map(toButtonTileFromResponse);
}

/**
 * The configured buttons exactly as stored, `mqtt` topics included.
 *
 * The button editor needs `icon`, `action_type`, the sound/TTS fields, the two overrides,
 * the flags and `mqtt`, none of which the `ButtonTile` view type carries — so it reads the
 * wire rows. The bake fields are carried on every row when supported and are nullable;
 * a build without the bake API omits their keys, which is why the type is optional and
 * panels feature-detect by key presence.
 */
export function listButtonRows(): Promise<ButtonDetail[]> {
  return request<ButtonDetail[]>('/api/buttons');
}

/**
 * Create one button and read back the stored row (the route answers **201**).
 *
 * A rejection is an `ApiError` naming the offending field in `detail.field` — `name`
 * and `slug` are the two this UI can usually predict. A phrase of template syntax aimed at
 * an engine that bills per character is one more: its 422 carries
 * `detail.requires === 'acknowledge_paid_template'`, `detail.field === 'tts_text'` and
 * `detail.engine`, and resending the same body with `acknowledge_paid_template: true`
 * accepts that cost for this one request (the flag is never persisted).
 */
export function createButton(body: ButtonCreateBody): Promise<ButtonDetail> {
  return request<ButtonDetail>('/api/buttons', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Apply a partial update to one button and read back the stored row.
 *
 * Omitted keys keep their stored value and an explicit `null` clears a nullable one.
 * The server re-validates the **merged** row, so a PATCH of an existing TTS button is
 * rejected while `tts.default_engine` is `'none'` (D27) — that rejection is an `ApiError`
 * to render, not a bug here. A rename comes back with `slug_change` filled in.
 */
export function updateButton(id: number, body: ButtonWriteBody): Promise<ButtonDetail> {
  return request<ButtonDetail>(`/api/buttons/${encodeURIComponent(String(id))}`, {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Pre-render one static TTS button and answer with its refreshed stored row.
 *
 * Callers render refusal sentences verbatim: 404 means the button was not found; 503 means
 * no speech source was prepared; 422 `invalid_setting` means speech is off; and 422
 * `validation_error` with `detail.reason === 'dynamic_phrase'` (or
 * `detail.field === 'action_type'` for a sound button) explains why this row cannot bake.
 */
export function bakeButton(id: number): Promise<ButtonDetail> {
  return request<ButtonDetail>(`/api/buttons/${encodeURIComponent(String(id))}/bake`, {
    method: 'POST'
  });
}

/**
 * Delete one button. The answer names the row (`{deleted, id, slug}`) rather than answering
 * 204, so a caller can report what it removed without keeping its own copy.
 */
export function deleteButton(id: number): Promise<{ deleted: boolean; id: number; slug: string }> {
  return request<{ deleted: boolean; id: number; slug: string }>(
    `/api/buttons/${encodeURIComponent(String(id))}`,
    { method: 'DELETE' }
  );
}

/**
 * The sound library, one row per indexed file.
 *
 * A row with `missing: true` is still listed — the index outlived the file — and must
 * render as broken, never as playable: the row is a fact about the index, not about the
 * disk. Only a rescan changes that flag, so this list alone never repairs one.
 */
export function listSounds(): Promise<SoundRow[]> {
  return request<SoundRow[]>('/api/sounds');
}

/**
 * Rescan the library folder and report what changed.
 *
 * `removed` counts rows that became missing during this scan, not rows that were deleted:
 * a vanished file is flagged so the panel can show it as broken, and only a rescan can
 * bring a row back. That is why the summary wording in `sound-library.ts` says "removed"
 * beside "added" without ever implying a deletion count.
 */
export function rescanSounds(): Promise<SoundRescanResponse> {
  return request<SoundRescanResponse>('/api/sounds/rescan', { method: 'POST' });
}

/**
 * Download one HTTP(S) audio file into the library.
 *
 * `name` is left out of the body entirely when it is absent or blank: the server's model
 * has `name: str | None = None`, where `null` means "derive a name from the URL", while an
 * empty string is a different request that names the file nothing and is rejected. A
 * missing `name` key and a `null` one are the same request here, so the smaller body wins.
 */
export function addSoundFromUrl(url: string, name?: string | null): Promise<SoundFromUrlResponse> {
  const trimmedName = name?.trim() ?? '';
  const body = trimmedName === '' ? { url } : { url, name: trimmedName };
  return request<SoundFromUrlResponse>('/api/sounds/from_url', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Rename one sound and read back the stored row.
 *
 * Only the name is writable: the file name, format and digest describe bytes on disk, and
 * renaming the index entry does not move the file. A rejected name is a 422 `ApiError`, so
 * a caller renders `ApiError.message` rather than inventing a rule.
 */
export function renameSound(id: number, name: string): Promise<SoundRow> {
  return request<SoundRow>(`/api/sounds/${encodeURIComponent(String(id))}`, {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ name })
  });
}

/**
 * Delete one sound, reporting the buttons it took with it.
 *
 * This is deliberately a two-step flow: call it without `confirm` first, and a 409 whose
 * `detail.affected_buttons` names the buttons that use the sound is the discovery step.
 * The caller then asks the user, and only a user's yes leads to a second call with
 * `confirm: true` — a 409 must never be retried silently with `confirm`, because the
 * confirmed call DELETES those buttons rather than disabling them.
 *
 * `confirm` is only added to the query when it is true, so the discovery request is the
 * plain path with no empty `?` and the two calls are visibly different in a log.
 */
export function deleteSound(id: number, confirm = false): Promise<SoundDeleteResponse> {
  const path = `/api/sounds/${encodeURIComponent(String(id))}`;
  const query = confirm ? `?${new URLSearchParams({ confirm: 'true' }).toString()}` : '';
  return request<SoundDeleteResponse>(`${path}${query}`, { method: 'DELETE' });
}

/**
 * The URL an `<audio>` element's `src` points at for one sound — a string, not a request.
 *
 * The route answers the file's real bytes with the file's own `Content-Type` and honours
 * `Range`, which is what makes seeking work; the transport in this module cannot carry
 * bytes at all. A row whose file is missing answers a JSON error envelope with a 404
 * instead of bytes, so an `<audio>` element fails quietly rather than playing garbage.
 */
export function soundAudioUrl(id: number): string {
  return `/api/sounds/${encodeURIComponent(String(id))}/audio`;
}

/**
 * The local speech engines a press could try, plus whether speech is switched off.
 *
 * `disabled: true` is the deliberate D27 off state, not an error; a `warning` without
 * `disabled` means the list could not be produced. An engine missing from `engines` is not
 * an unavailable engine — the route lists only what a press would try, so `ha_tts` and
 * `remote_tts` can legitimately be absent.
 */
export function getTtsEngines(): Promise<TtsEnginesResponse> {
  return request<TtsEnginesResponse>('/api/tts/engines');
}

/**
 * One voice model in `GET /api/tts/voices`, curated or installed.
 *
 * The curated metadata is nullable because a model installed by hand has none: an unknown
 * voice is reported honestly rather than given a made-up quality. `size_mb` is MiB as the
 * server measures it, and is `0` when the file could not be measured.
 *
 * `problem` is the engine's own sentence about a model that cannot be loaded, verbatim —
 * this UI never invents one.
 */
export interface TtsVoice {
  id: string;
  language: string | null;
  quality: string | null;
  /** MiB, as the server measures it; 0 when the file could not be measured. */
  size_mb: number;
  sample_rate_hz: number | null;
  num_speakers: number | null;
  licence: string | null;
  licence_url: string | null;
  installed: boolean;
  /** The engine's own sentence about a model that cannot be loaded, verbatim. */
  problem: string | null;
}

/**
 * `GET /api/tts/voices` — the curated catalogue beside what is installed, plus why a
 * download may be refused.
 *
 * The route never fails: a missing voice manager answers an empty list with a `warning`
 * sentence, and a hung or broken install check becomes `can_download: false` with its
 * reason instead of a 500. `can_download` is the D22 memory/extra/import gate and
 * `unavailable_reason` is the whole refusal sentence; `memory_warning` is reported
 * separately so a small board still gets its RAM sentence even when a download is refused
 * for a different reason.
 */
export interface TtsVoicesResponse {
  disabled: boolean;
  can_download: boolean;
  unavailable_reason: string | null;
  memory_warning: string | null;
  suggested: TtsVoice[];
  installed: TtsVoice[];
  warning: string | null;
}

/** The first snapshot of a download job, returned by a `202` start. */
export interface VoiceDownloadStart {
  job_id: string;
  voice: string;
  state: string;
}

/** One download job's progress; `state` is `running` until it is terminal. */
export interface VoiceDownloadStatus {
  job_id: string;
  voice: string;
  state: string;
  bytes_done: number;
  bytes_total: number | null;
  error: string | null;
  detail: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
}

/**
 * The curated voice catalogue and the models installed on this device.
 *
 * The route never fails: an empty list with a `warning` sentence is how a missing voice
 * manager and the deliberate off state both answer, so the panel renders the sentence
 * rather than treating an empty answer as an error.
 */
export function getTtsVoices(): Promise<TtsVoicesResponse> {
  return request<TtsVoicesResponse>('/api/tts/voices');
}

/**
 * Start installing one curated voice model and return its job id (`202`).
 *
 * A refused action is a real `ApiError` (422 while speech is off, 409 for a download
 * already running, 413 above the size cap, 502 for a corrupt model); its `message` is the
 * engine's own whole sentence, which the panel shows verbatim.
 */
export function startVoiceDownload(voiceId: string): Promise<VoiceDownloadStart> {
  return request<VoiceDownloadStart>(
    `/api/tts/voices/${encodeURIComponent(voiceId)}/download`,
    { method: 'POST' }
  );
}

/**
 * One voice download's latest snapshot; poll it until `state` is terminal.
 *
 * `state` is `running` until the job ends as `done`, `failed` or `rejected`.
 */
export function getVoiceDownload(jobId: string): Promise<VoiceDownloadStatus> {
  return request<VoiceDownloadStatus>(
    `/api/tts/voices/downloads/${encodeURIComponent(jobId)}`
  );
}

/**
 * The configured external TTS providers, the preset library and the selected id.
 *
 * The route never fails: an unreadable stored record is skipped and reported in
 * `warning`, so render the sentence rather than treating an empty list as an error. Every
 * provider answers `has_api_key` and never the key itself — HTTP TTS keys are write-only,
 * exactly as `TtsProviderWriteBody.api_key` documents.
 */
export function getTtsProviders(): Promise<TtsProvidersResponse> {
  return request<TtsProvidersResponse>('/api/tts/providers');
}

/**
 * Add one provider and read back the stored row (the route answers **201**).
 *
 * A rejection is an `ApiError` whose `message` is the server's own sentence, so a
 * duplicate name, an unusable request shape or an empty `api_key` is rendered verbatim.
 * Absent fields fall back to the preset: `paid` to the preset's own flag and
 * `cost_per_1k_chars_usd` to its estimate. The key is written separately and never comes
 * back — only `has_api_key` does.
 */
export function createTtsProvider(body: TtsProviderWriteBody): Promise<TtsProvider> {
  return request<TtsProvider>('/api/tts/providers', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Change some fields of one provider and read back the merged row.
 *
 * Omitted keys keep their stored value. `api_key` is **write-only** and its three states
 * differ: absent leaves the stored key alone, `null` clears it, and `''` is rejected ("send
 * null to clear a secret, not an empty string"). `cost_per_1k_chars_usd: null` returns the
 * provider to its preset's estimate. The key itself is never returned — only
 * `has_api_key`.
 *
 * :param id: the provider id from the list; escaped here, never by the caller.
 */
export function updateTtsProvider(
  id: string,
  body: TtsProviderWriteBody
): Promise<TtsProvider> {
  return request<TtsProvider>(`/api/tts/providers/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/**
 * Remove one provider, its API key and any selection that pointed at it.
 *
 * The answer is a body (`{deleted, id}`), not a 204, so a caller can report what it
 * removed; a deleted provider is also un-selected by the server, so nothing is left
 * pointing at it.
 *
 * :param id: the provider id from the list; escaped here, never by the caller.
 */
export function deleteTtsProvider(id: string): Promise<{ deleted: boolean; id: string }> {
  return request<{ deleted: boolean; id: string }>(
    `/api/tts/providers/${encodeURIComponent(id)}`,
    { method: 'DELETE' }
  );
}

/**
 * Speak one fixed test phrase through one provider and report the measurement.
 *
 * A failed synthesis is a **200 with `ok: false`**, matching every other connection test
 * in this API: a rejected promise is therefore a transport or route failure (an unknown id
 * is a 404), never "the provider could not speak" — the two must not be conflated.
 *
 * The success facts are measured, never estimated: `latency_ms` is the round trip the
 * server observed, and the cost fields come from the provider's configured price
 * (`cost_per_100_chars_usd` plus this phrase's `estimated_cost_usd`). Both are `null` when
 * the provider has no price, and no number here is ever fabricated — render a `null` price
 * as unknown, never as `$0`.
 *
 * :param id: the provider id from the list; escaped here, never by the caller.
 */
export function testTtsProvider(id: string): Promise<TtsProviderTestResponse> {
  return request<TtsProviderTestResponse>(
    `/api/tts/providers/${encodeURIComponent(id)}/test`,
    { method: 'POST' }
  );
}

/** Every non-secret setting, plus whether each secret is stored. */
export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>('/api/settings');
}

/**
 * Apply a partial settings update and read back the whole effective config.
 *
 * The body is the flat key→value map itself: the backend's body model is
 * `dict[str, object]`, not an object with a `settings` key. `request()` merges
 * these over its default `accept`, so both headers always travel. One rejected
 * key rejects the whole patch, which is why the echoed response — not an
 * optimistic local edit — is the source of truth.
 *
 * Secrets are write-only: send a value to replace one, or `null` to clear it.
 * An empty string is rejected by the backend ("send null to clear a secret, not
 * an empty string"), so a clear must always be `null`.
 */
export function patchSettings(updates: Record<string, unknown>): Promise<SettingsResponse> {
  return request<SettingsResponse>('/api/settings', {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(updates)
  });
}

/**
 * Run the audio self-test. A test that ran and failed is `ok: false` at HTTP
 * 200, so only a broken request becomes an `ApiError`.
 */
export function testAudio(): Promise<AudioTestResponse> {
  return request<AudioTestResponse>('/api/settings/test/audio', { method: 'POST' });
}

/**
 * The audio outputs this device can see, plus the one the running backend
 * resolved for the stored hint (`GET /api/audio/devices`).
 *
 * A failed discovery is a 200 with `ok: false` and an `error` sentence, the same
 * convention as the connection tests: the request worked, so rendering the error
 * row is the panel's job, not an `ApiError`'s.
 */
export function listAudioDevices(): Promise<AudioDevicesResponse> {
  return request<AudioDevicesResponse>('/api/audio/devices');
}

/** Read, but never write, the selected output's hardware volume control. */
export function readHardwareVolume(): Promise<HardwareVolumeResponse> {
  return request<HardwareVolumeResponse>('/api/audio/hardware');
}

/**
 * Test the stored Home Assistant connection. A failed test is `ok: false` at
 * HTTP 200, so only a broken request becomes an `ApiError`.
 *
 * A candidate is used for that ONE probe and never stored: the server writes
 * nothing and the response never carries the values back. With no candidate the
 * request has no body at all, so it tests exactly the stored settings.
 */
export function testHa(candidate?: {
  base_url?: string;
  token?: string;
}): Promise<HaTestResponse> {
  if (candidate === undefined) {
    return request<HaTestResponse>('/api/settings/test/ha', { method: 'POST' });
  }
  return request<HaTestResponse>('/api/settings/test/ha', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(candidate)
  });
}

/**
 * Test the stored MQTT broker connection. A failed test is `ok: false` at HTTP
 * 200, so only a broken request becomes an `ApiError`.
 */
export function testMqtt(): Promise<MqttTestResponse> {
  return request<MqttTestResponse>('/api/settings/test/mqtt', { method: 'POST' });
}

/** Re-announce every chime button's MQTT discovery payload. */
export function republishAll(): Promise<RepublishResponse> {
  return request<RepublishResponse>('/api/mqtt/republish', { method: 'POST' });
}

/**
 * Press one chime through the same path MQTT uses (`source="ui_test"`), so the
 * press log records exactly what the dashboard triggered.
 */
export function pressButton(id: number): Promise<PressResponse> {
  return request<PressResponse>(`/api/buttons/${encodeURIComponent(String(id))}/press`, {
    method: 'POST'
  });
}

/**
 * Re-render every eligible static phrase and return the four separate outcome lists.
 *
 * This route is synchronous and unbounded within its route budget (button count multiplied
 * by the per-render bound), so callers must show a loading state. A failing button is listed
 * in `failed` and never fails the batch. Refusals are 503 when no speech source was prepared
 * and 422 when speech is off; render the server's sentence verbatim.
 */
export function bakeAllPhrases(): Promise<BakeAllResponse> {
  return request<BakeAllResponse>('/api/tts/bake_all', { method: 'POST' });
}

/**
 * Read device-measured usage for baked speech and the TTS cache.
 *
 * This is always 200 on a build that has the endpoint. A failure here is evidence that the
 * endpoint is absent, which the About panel relies on for feature detection; the browser
 * must never calculate either directory's usage itself.
 */
export function getBakeUsage(): Promise<BakeUsageResponse> {
  return request<BakeUsageResponse>('/api/tts/bake_usage');
}

/**
 * Search Home Assistant's entities for the tile picker.
 *
 * An empty `q` matches everything — that is the picker's opening view, not a
 * search. `limit` is 1..200 (the server rejects anything else with a 422 rather
 * than silently serving a different page size), and `matched` in the answer says
 * whether the page was truncated.
 */
export function searchHaEntities(q: string, limit = 50): Promise<HaEntitySearchResponse> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  return request<HaEntitySearchResponse>(`/api/ha/entities?${params.toString()}`);
}

/**
 * Describe one Home Assistant entity live, for the editor's preview.
 *
 * An id Home Assistant does not know is a 200 with `found: false`, so this is
 * one of the few calls where "nothing there" is not an `ApiError`.
 */
export function describeHaEntity(entityId: string): Promise<HaEntityDetail> {
  return request<HaEntityDetail>(`/api/ha/entities/${encodeURIComponent(entityId)}`);
}

/**
 * Search the shipped Material Design Icons set. Each hit carries its path data,
 * so the picker's grid draws without one request per tile.
 */
export function searchIcons(q: string, limit = 120): Promise<IconSearchResponse> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  return request<IconSearchResponse>(`/api/icons/search?${params.toString()}`);
}

/**
 * Fetch one icon's path data. This is how a stored `mdi:` name outside the UI's
 * curated bundle gets drawn, and a name the shipped set does not contain is a
 * 404 `ApiError`.
 */
export function getIconPath(name: string): Promise<IconPathResponse> {
  return request<IconPathResponse>(`/api/icon/${encodeURIComponent(name)}`);
}

/**
 * List the user's own icons from `data/icons/`.
 *
 * Store `icons[].reference` verbatim — it is the `custom:<name>` string an entity
 * stores. `skipped[].reason` is a finished sentence: those files are shown as
 * warnings and never offered. An empty listing is normal on a fresh install.
 */
export function listCustomIcons(): Promise<CustomIconListResponse> {
  return request<CustomIconListResponse>('/api/icons/custom');
}

/* ---- Press history, build facts and configuration transfer --------------- */

/**
 * One stored press, exactly as `GET /api/history` sends it.
 *
 * There is deliberately no `code` field: `press_log` has no code column, so the
 * machine-readable code a live `PressResponse` carries is derived at press time
 * and never stored. The failure reason is `detail`, and it is the only record of
 * one.
 *
 * `latency_ms` is nullable for a reason a caller must not flatten: `null` is a
 * playback job that never started, so nothing was measured, while `0` is an
 * immediate start and a real measurement. They must never render the same.
 *
 * `synthesis_ms` is how long the press's own speech synthesis took, and it is not
 * `latency_ms` restated: `null` means nothing was synthesised for this press (a
 * cache or bake hit, a sound button, a failed press, or an engine that keeps no
 * measurement), which for a hit is a success, not a missing measurement; it is
 * never the same number as `latency_ms`.
 */
export interface HistoryRow {
  id: number;
  button_id: number | null;
  button_name: string;
  pressed_at: string;
  source: string;
  outcome: string;
  duration_ms: number | null;
  latency_ms: number | null;
  synthesis_ms: number | null;
  detail: string | null;
}

/** One page of press history plus the retention it is a window onto. */
export interface HistoryResponse {
  rows: HistoryRow[];
  retention: number;
}

/**
 * The newest press-log rows, newest first, optionally only one outcome.
 *
 * `limit` is 1..1000 (the retention cap) and an out-of-range value is the
 * server's 422 rather than a silent clamp, so a caller that asks for more than
 * the table holds is told instead of handed a different page. `outcome` is
 * omitted from the query entirely when it is `null` or empty, because the
 * server's enum has no "all".
 */
export function getHistory(
  params: { limit?: number; outcome?: string | null } = {}
): Promise<HistoryResponse> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) {
    query.set('limit', String(params.limit));
  }
  const outcome = params.outcome ?? '';
  if (outcome !== '') {
    query.set('outcome', outcome);
  }
  const suffix = query.toString();
  return request<HistoryResponse>(suffix === '' ? '/api/history' : `/api/history?${suffix}`);
}

/** What this build is, how long it has been up and where its data lives. */
export interface AboutResponse {
  version: string;
  schema_version: number;
  uptime_s: number;
  data_dir: string;
  log_available: boolean;
}

/**
 * The process facts the About panel prints (`GET /api/about`).
 *
 * `data_dir` is served verbatim (a relative configured path stays relative), and
 * `log_available` is the one flag that decides whether the log excerpt control
 * can exist at all — file logging off means there is no file to read.
 */
export function getAbout(): Promise<AboutResponse> {
  return request<AboutResponse>('/api/about');
}

/** What one import wrote, exactly as the server counts it. */
export interface ImportReport {
  dry_run: boolean;
  settings_written: number;
  entities: number;
  sounds: number;
  buttons: number;
}

/**
 * Validate or apply a configuration export document.
 *
 * `dry_run` sits beside `document` because it is a property of the request, not
 * of the configuration. Every count is the exact number of rows written (for a
 * dry run: would be written), and import is a merge, so nothing absent from the
 * document is ever removed. A rejected document is one `422` whose `message`
 * joins every problem and whose `detail.problems` lists them; nothing is
 * written in that case.
 */
export function importConfig(document: unknown, dryRun: boolean): Promise<ImportReport> {
  return request<ImportReport>('/api/config/import', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify({ dry_run: dryRun, document })
  });
}

/**
 * The URL a download anchor points at for the whole configuration — a string,
 * not a request.
 *
 * The route answers a JSON *attachment*, and this module's transport parses a
 * JSON body rather than handing bytes to a caller, so the export control is a
 * real `<a download>`: the browser's own save flow, with the server's filename
 * and `Cache-Control: no-store`. With `includeSecrets` false the secret settings
 * are absent from the document, not masked.
 */
export function exportConfigUrl(includeSecrets: boolean): string {
  const query = new URLSearchParams({ include_secrets: includeSecrets ? 'true' : 'false' });
  return `/api/config/export?${query.toString()}`;
}

/**
 * The URL a download anchor points at for the bounded, scrubbed log excerpt — a
 * string, not a request, for the same reason as `exportConfigUrl`: the route
 * answers `text/plain` and this transport carries JSON only. The route takes no
 * path and no query parameter, so an anchor can never point it at another file.
 */
export function logExcerptUrl(): string {
  return '/api/about/log';
}
