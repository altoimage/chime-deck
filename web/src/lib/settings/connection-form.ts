/**
 * The Connection rules the settings panel and the first-run wizard share
 * (HSD-vl7t): registry defaults, validation, the dirty test, the diff PATCH,
 * the secret hints and the two never-throwing test runners.
 *
 * The point of the module is drift. Two screens write the same settings, and a
 * second copy of these rules would eventually disagree with the first on three
 * things a user can actually see: which PATCH keys a save sends (an untouched
 * secret must send NO key, a cleared one `null`, never `""`), which endpoints a
 * "Test" button calls, and how a result is worded. So the rules live here once
 * and both screens render them.
 *
 * Pure TS, no runes and no DOM: a component renders, this module decides. The
 * reader is the structural shape `settings.svelte.ts` already exposes, so the
 * panel and the wizard pass the same live store and neither owns a copy.
 *
 * The device name is an ordinary drafted field (`device.name`). The one thing it
 * does that no rule here has to know about is the pin: the server derives
 * `mqtt.instance_id` from the first name it is given, so the echo of that save
 * carries a key the draft never sent. `draftFrom` already rebases onto the echo,
 * which is why the panel stays clean afterwards.
 */

import { ApiError, testHa, testMqtt, type HaTestResponse, type MqttTestResponse } from '../api.js';

/** Registry defaults and ranges (`settings_registry.py`), so a default never reads as a change. */
export const DEFAULT_BASE_URL = 'http://homeassistant.local:8123';
export const DEFAULT_HOST = 'core-mosquitto.local';
export const DEFAULT_PORT = 1883;
export const DEFAULT_DISCOVERY_PREFIX = 'homeassistant';
export const DEFAULT_INSTANCE_ID = 'default';
/** The `device.name` registry default (settings_registry.py). */
export const DEFAULT_DEVICE_NAME = 'ChimeDeck';

export const MIN_PORT = 1;
export const MAX_PORT = 65535;

/** Mirrors the registry's `_BASE_URL_PATTERN`; the server stays the authority. */
export const BASE_URL_PATTERN = /^$|^https?:\/\/\S+$/;

/** The generic readers `settings.svelte.ts` already exposes. */
export interface SettingsReader {
  text(key: string, fallback?: string): string;
  integer(key: string, minimum: number, maximum: number, fallback: number): number;
  flag(key: string, fallback: boolean): boolean;
  hasSecret(key: string): boolean;
}

/** Every field a Connection form drafts, secrets included. */
export interface ConnectionDraft {
  baseUrl: string;
  deviceName: string;
  host: string;
  port: string;
  username: string;
  discoveryPrefix: string;
  instanceId: string;
  tls: boolean;
  token: string;
  password: string;
  clearToken: boolean;
  clearPassword: boolean;
}

/**
 * What one connection test says, in the one shape both screens render.
 *
 * `authFailed` is the server's machine-readable classification — a broker that
 * rejected the username/password — so a screen can label the failure without
 * matching the English `error`, which stays a whole sentence rendered verbatim.
 */
export interface ConnectionTestView {
  ok: boolean;
  summary: string;
  error: string | null;
  authFailed: boolean;
}

/** The registry defaults, empty secrets and no pending clear. */
export function blankDraft(): ConnectionDraft {
  return {
    baseUrl: DEFAULT_BASE_URL,
    deviceName: DEFAULT_DEVICE_NAME,
    host: DEFAULT_HOST,
    port: String(DEFAULT_PORT),
    username: '',
    discoveryPrefix: DEFAULT_DISCOVERY_PREFIX,
    instanceId: DEFAULT_INSTANCE_ID,
    tls: false,
    token: '',
    password: '',
    clearToken: false,
    clearPassword: false
  };
}

/**
 * The last loaded snapshot as drafts (open the form; rebase after a save).
 *
 * Secrets are always empty: the backend returns only the `secrets` flags, so a
 * draft never holds a value it could accidentally render.
 */
export function draftFrom(reader: SettingsReader): ConnectionDraft {
  return {
    baseUrl: reader.text('ha.base_url', DEFAULT_BASE_URL),
    deviceName: reader.text('device.name', DEFAULT_DEVICE_NAME),
    host: reader.text('mqtt.host', DEFAULT_HOST),
    port: String(reader.integer('mqtt.port', MIN_PORT, MAX_PORT, DEFAULT_PORT)),
    username: reader.text('mqtt.username'),
    discoveryPrefix: reader.text('mqtt.discovery_prefix', DEFAULT_DISCOVERY_PREFIX),
    instanceId: reader.text('mqtt.instance_id', DEFAULT_INSTANCE_ID),
    tls: reader.flag('mqtt.tls', false),
    token: '',
    password: '',
    clearToken: false,
    clearPassword: false
  };
}

/**
 * The draft rebased onto a freshly loaded snapshot, keeping what the user typed.
 *
 * The panel is on screen before the shell's first load lands, so it opens on the
 * registry defaults and only later learns the stored values. Taking them field
 * by field is what makes that safe: replacing the whole draft (what the panel
 * used to do) discarded anything typed while the load was in flight, which left
 * the form clean, so Save went disabled and a click did nothing at all.
 *
 * A field still holding its unfilled default counts as untouched and adopts the
 * stored value; one the user has changed keeps what they typed. A secret is
 * never returned by the server, so a drafted one is always the user's: it is
 * carried over as-is and never erased by a load.
 */
export function rebaseDraft(draft: ConnectionDraft, reader: SettingsReader): ConnectionDraft {
  const unfilled = blankDraft();
  const stored = draftFrom(reader);
  return {
    baseUrl: adopted(draft.baseUrl, unfilled.baseUrl, stored.baseUrl),
    deviceName: adopted(draft.deviceName, unfilled.deviceName, stored.deviceName),
    host: adopted(draft.host, unfilled.host, stored.host),
    port: adopted(draft.port, unfilled.port, stored.port),
    username: adopted(draft.username, unfilled.username, stored.username),
    discoveryPrefix: adopted(
      draft.discoveryPrefix,
      unfilled.discoveryPrefix,
      stored.discoveryPrefix
    ),
    instanceId: adopted(draft.instanceId, unfilled.instanceId, stored.instanceId),
    tls: adopted(draft.tls, unfilled.tls, stored.tls),
    token: draft.token,
    password: draft.password,
    clearToken: draft.clearToken,
    clearPassword: draft.clearPassword
  };
}

/** The drafted value when the user has changed it, else the stored one. */
function adopted<T extends string | boolean>(drafted: T, unfilled: T, stored: T): T {
  return drafted !== unfilled ? drafted : stored;
}

/** The port draft as a number, or null when it is not a whole number. */
export function portNumber(port: string): number | null {
  return /^\d+$/.test(port.trim()) ? Number(port.trim()) : null;
}

/** The address sentence, or undefined when it is empty or an http(s) URL. */
export function baseUrlProblem(draft: ConnectionDraft): string | undefined {
  return BASE_URL_PATTERN.test(draft.baseUrl.trim())
    ? undefined
    : 'the address must be empty or an http(s):// URL';
}

/** The port sentence, or undefined when it is a whole number in range. */
export function portProblem(draft: ConnectionDraft): string | undefined {
  const value = portNumber(draft.port);
  return value !== null && value >= MIN_PORT && value <= MAX_PORT
    ? undefined
    : 'the port must be a whole number between 1 and 65535';
}

/** Whether a client-side rule currently blocks the save. */
export function draftInvalid(draft: ConnectionDraft): boolean {
  return baseUrlProblem(draft) !== undefined || portProblem(draft) !== undefined;
}

/**
 * Dirty compares the drafts with the store's last loaded snapshot (which is
 * also the echo of the last save). A secret counts as dirty only when it has a
 * new draft or a pending clear, never because a value is stored.
 */
export function draftDirty(draft: ConnectionDraft, reader: SettingsReader): boolean {
  return (
    draft.baseUrl !== reader.text('ha.base_url', DEFAULT_BASE_URL) ||
    draft.deviceName !== reader.text('device.name', DEFAULT_DEVICE_NAME) ||
    draft.host !== reader.text('mqtt.host', DEFAULT_HOST) ||
    portNumber(draft.port) !== reader.integer('mqtt.port', MIN_PORT, MAX_PORT, DEFAULT_PORT) ||
    draft.username !== reader.text('mqtt.username') ||
    draft.discoveryPrefix !== reader.text('mqtt.discovery_prefix', DEFAULT_DISCOVERY_PREFIX) ||
    draft.instanceId !== reader.text('mqtt.instance_id', DEFAULT_INSTANCE_ID) ||
    draft.tls !== reader.flag('mqtt.tls', false) ||
    draft.token !== '' ||
    draft.password !== '' ||
    draft.clearToken ||
    draft.clearPassword
  );
}

/** Only the changed keys; a secret contributes nothing unless replaced or cleared. */
export function buildPatch(
  draft: ConnectionDraft,
  reader: SettingsReader
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};

  if (draft.baseUrl !== reader.text('ha.base_url', DEFAULT_BASE_URL)) {
    patch['ha.base_url'] = draft.baseUrl;
  }
  if (draft.deviceName !== reader.text('device.name', DEFAULT_DEVICE_NAME)) {
    patch['device.name'] = draft.deviceName;
  }
  if (draft.host !== reader.text('mqtt.host', DEFAULT_HOST)) {
    patch['mqtt.host'] = draft.host;
  }
  if (portNumber(draft.port) !== reader.integer('mqtt.port', MIN_PORT, MAX_PORT, DEFAULT_PORT)) {
    patch['mqtt.port'] = portNumber(draft.port);
  }
  if (draft.username !== reader.text('mqtt.username')) {
    patch['mqtt.username'] = draft.username;
  }
  if (draft.discoveryPrefix !== reader.text('mqtt.discovery_prefix', DEFAULT_DISCOVERY_PREFIX)) {
    patch['mqtt.discovery_prefix'] = draft.discoveryPrefix;
  }
  if (draft.instanceId !== reader.text('mqtt.instance_id', DEFAULT_INSTANCE_ID)) {
    patch['mqtt.instance_id'] = draft.instanceId;
  }
  if (draft.tls !== reader.flag('mqtt.tls', false)) {
    patch['mqtt.tls'] = draft.tls;
  }

  // Secrets: a pending clear wins over a draft, and a clear is `null` — never
  // `""`, which the backend rejects.
  if (draft.clearToken) {
    patch['ha.token'] = null;
  } else if (draft.token !== '') {
    patch['ha.token'] = draft.token;
  }
  if (draft.clearPassword) {
    patch['mqtt.password'] = null;
  } else if (draft.password !== '') {
    patch['mqtt.password'] = draft.password;
  }

  return patch;
}

/** The token hint: a pending clear wins over "stored" wins over "nothing stored". */
export function tokenHint(draft: ConnectionDraft, reader: SettingsReader): string {
  if (draft.clearToken) {
    return 'The stored token will be cleared when you save.';
  }
  if (reader.hasSecret('ha.token')) {
    return 'A token is stored. Type a new one to replace it.';
  }
  return 'No token stored.';
}

/** The password hint, in the same order of precedence. */
export function passwordHint(draft: ConnectionDraft, reader: SettingsReader): string {
  if (draft.clearPassword) {
    return 'The stored password will be cleared when you save.';
  }
  if (reader.hasSecret('mqtt.password')) {
    return 'A password is stored. Type a new one to replace it.';
  }
  return 'No password stored.';
}

/**
 * The success facts, honestly: a missing half is omitted rather than invented,
 * and a build that cannot report the version (HSD-8qf6) says so instead of
 * looking complete.
 */
export function haSummary(result: HaTestResponse): string {
  let summary = 'Connected to Home Assistant';
  if (result.ha_version) {
    summary += ` ${result.ha_version}`;
  }
  if (result.entity_count !== null) {
    const noun = result.entity_count === 1 ? 'entity' : 'entities';
    summary += ` — ${result.entity_count} ${noun}`;
  }
  if (result.ok && result.ha_version === null) {
    summary += ' (the version was not reported)';
  }
  return summary;
}

/** The MQTT success facts; the broker version is omitted when it was absent. */
export function mqttSummary(result: MqttTestResponse): string {
  return result.broker_version
    ? `Connected to the MQTT broker ${result.broker_version}`
    : 'Connected to the MQTT broker';
}

/** The one render shape for a Home Assistant test; `null` in, `null` out. */
export function haTestView(result: HaTestResponse | null): ConnectionTestView | null {
  if (result === null) {
    return null;
  }
  if (result.ok) {
    return { ok: true, summary: haSummary(result), error: null, authFailed: false };
  }
  return { ok: false, summary: '', error: result.error, authFailed: false };
}

/** The one render shape for an MQTT test; `null` in, `null` out. */
export function mqttTestView(result: MqttTestResponse | null): ConnectionTestView | null {
  if (result === null) {
    return null;
  }
  if (result.ok) {
    return { ok: true, summary: mqttSummary(result), error: null, authFailed: false };
  }
  return { ok: false, summary: '', error: result.error, authFailed: result.auth_failed };
}

/** The fallback for a broken request — never used when the server answered. */
export function requestFailure(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.message : fallback;
}

/**
 * Run the Home Assistant test and never throw: a broken request becomes the
 * same `ok: false` shape a failed probe answers with, so a screen renders one
 * result and never has to invent a second failure path.
 */
export async function runHaTest(): Promise<HaTestResponse> {
  try {
    return await testHa();
  } catch (cause) {
    return {
      ok: false,
      ha_version: null,
      entity_count: null,
      error: requestFailure(cause, 'the Home Assistant test could not be run')
    };
  }
}

/** Run the MQTT test and never throw, for the same reason as `runHaTest`. */
export async function runMqttTest(): Promise<MqttTestResponse> {
  try {
    return await testMqtt();
  } catch (cause) {
    return {
      ok: false,
      broker_version: null,
      error: requestFailure(cause, 'the MQTT test could not be run'),
      auth_failed: false
    };
  }
}
