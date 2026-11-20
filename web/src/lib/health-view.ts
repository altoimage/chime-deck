import type { HealthState } from './api.js';

export type DotKind = 'ok' | 'warn' | 'alert' | 'off';
/** The mark drawn inside the dot, so kind survives greyscale. */
export type DotMark = 'filled' | 'ring' | 'cross' | 'hollow';

export interface HealthView {
  kind: DotKind;
  mark: DotMark;
  word: string;
  explanation: string;
}

export interface ComponentView extends HealthView {
  key: 'ha' | 'mqtt' | 'audio';
  label: string;
}

/**
 * Map one backend health status to its colour, shape, word and operator-facing
 * explanation. Unknown statuses stay visibly unconfigured.
 */
export function healthView(status: string): HealthView {
  switch (status) {
    case 'connected':
      return {
        kind: 'ok',
        mark: 'filled',
        word: 'connected',
        explanation: 'Home Assistant is connected and pushing state changes.'
      };
    case 'ready':
      return {
        kind: 'ok',
        mark: 'filled',
        word: 'ready',
        explanation: 'Ready and answering.'
      };
    case 'reconnecting':
      return {
        kind: 'warn',
        mark: 'ring',
        word: 'reconnecting',
        explanation: 'The connection dropped and the app is retrying with a growing delay.'
      };
    case 'degraded':
      return {
        kind: 'warn',
        mark: 'ring',
        word: 'degraded',
        explanation: 'Connected, but something is only partly working; the detail line below has the reason.'
      };
    case 'error':
      // Every HealthStatus.ERROR producer here is a connection or availability failure: the Home Assistant
      // websocket's rejected token or stopped terminal, MQTT bridge connect/publish failures, or the audio
      // backend failing to start. Design 05 §8 uses “Home Assistant disconnected” in its aria-live example,
      // so “disconnected” is the operator-facing word; the sheet prints the precise fault verbatim in detail.
      return {
        kind: 'alert',
        mark: 'cross',
        word: 'disconnected',
        explanation: 'The last connection attempt failed, so this is disconnected. The reported reason below has the detail; open Connection to test it.'
      };
    case 'disabled':
      return {
        kind: 'off',
        mark: 'hollow',
        word: 'disabled',
        explanation: 'This component is switched off in settings.'
      };
    case 'not_configured':
      return {
        kind: 'off',
        mark: 'hollow',
        word: 'not configured',
        explanation: 'No address or token is saved yet, so this can never connect.'
      };
    case 'no_entities':
      return {
        kind: 'off',
        mark: 'hollow',
        word: 'waiting for tiles',
        explanation: 'Home Assistant is configured, but no tiles are selected yet — add one and live state will start arriving.'
      };
    default:
      return {
        kind: 'off',
        mark: 'hollow',
        word: 'not configured',
        explanation: 'This status is not recognized, so the component state cannot be confirmed.'
      };
  }
}

export interface ConnectionInput {
  status: 'idle' | 'connecting' | 'open' | 'closed';
  stale: boolean;
  attempts: number;
  hasSnapshot: boolean;
}

export type ConnectionPhase = 'live' | 'reconnecting' | 'connecting' | 'disconnected';

function chooseConnectionPhase(input: ConnectionInput): ConnectionPhase {
  // An open, fresh socket is live only when its latest snapshot is current.
  if (input.status === 'open' && !input.stale) {
    return 'live';
  }

  // A stale socket with a failed attempt is retrying, not simply disconnected.
  if (input.stale && input.attempts > 0) {
    return 'reconnecting';
  }

  // Connecting, or idle before the first attempt, is a transient boot state.
  if (input.status === 'connecting' || (input.status === 'idle' && !input.attempts)) {
    return 'connecting';
  }

  // An open socket awaiting its first snapshot is also still a transient load.
  if (input.status === 'open' && !input.hasSnapshot) {
    return 'connecting';
  }

  // Anything left has no live socket; without a snapshot, no data was received.
  return 'disconnected';
}

/** Return the data attribute phase using the same rules as the visible view. */
export function connectionPhase(input: ConnectionInput): ConnectionPhase {
  return chooseConnectionPhase(input);
}

/**
 * Explain the websocket state without making a booting panel look like a
 * failure. `hasSnapshot` distinguishes an empty first load from stale history.
 */
export function connectionView(input: ConnectionInput): HealthView {
  const phase = chooseConnectionPhase(input);
  switch (phase) {
    case 'live':
      return {
        kind: 'ok',
        mark: 'filled',
        word: 'Live',
        explanation: 'The live connection is open and the last snapshot is current.'
      };
    case 'reconnecting':
      return {
        kind: 'warn',
        mark: 'ring',
        word: 'Reconnecting…',
        explanation: 'The connection dropped; retrying with a growing delay. The data on screen is the last one received.'
      };
    case 'connecting':
      return {
        kind: 'warn',
        mark: 'ring',
        word: 'Connecting…',
        explanation: input.status === 'open'
          ? 'The socket is open and waiting for its first snapshot.'
          : 'The app is starting its live connection; no failed attempt is recorded yet.'
      };
    case 'disconnected':
      return {
        kind: 'alert',
        mark: 'cross',
        word: 'Disconnected',
        explanation: input.hasSnapshot
          ? 'The live connection is closed. Check the doorbell service and the network.'
          : 'No live connection and nothing was ever received. Check the doorbell service and the network.'
      };
  }
}

/** Build the stable, ordered header rows for the three backend components. */
export function componentViews(health: HealthState): ComponentView[] {
  return [
    { key: 'ha', label: 'Home Assistant', ...healthView(health.ha) },
    { key: 'mqtt', label: 'MQTT', ...healthView(health.mqtt) },
    { key: 'audio', label: 'Audio', ...healthView(health.audio) }
  ];
}

/** The screen-reader summary shared with the visible component statuses. */
export function summarySentence(views: readonly Pick<ComponentView, 'label' | 'word'>[]): string {
  return views.map((view) => `${view.label} is ${view.word}`).join(', ');
}
