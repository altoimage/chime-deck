/**
 * The first-run setup wizard's routing and dismissal truth (HSD-vl7t).
 *
 * Why a module: the dashboard shell decides whether to show the wizard, the
 * wizard decides how to leave, and the About screen can point back at it. Each
 * of those would otherwise re-derive "is Home Assistant configured?" from the
 * settings snapshot, and the copies would drift — one treating a stored token
 * as configured while another counted the URL too.
 *
 * Two rules are deliberate and stated here so no caller can quietly relax them:
 *
 *   - the redirect is offered only from `/`, and only after a SUCCESSFUL
 *     settings load. An unknown state must never hijack the screen: a slow or
 *     failed load is not evidence that the install is unconfigured.
 *   - dismissal is install state (`ui.setup_dismissed`), not a browser flag, so
 *     an install deliberately left half-configured reaches every device.
 *
 * LAN hints exist because the honest answer is sometimes "there is no address
 * to give you": a wildcard bind and a loopback-only server cannot know their
 * own reachable address, and inventing one would send the user to a dead link.
 *
 * Pure TS, no runes, no DOM, no `api.ts`: the shell renders, this module decides.
 */

/** The wizard's route; the shell treats it as a single-segment page. */
export const SETUP_PATH = '/setup';

/** The generic readers `settings.svelte.ts` already exposes. */
export interface SettingsReader {
  text(key: string, fallback?: string): string;
  flag(key: string, fallback: boolean): boolean;
  hasSecret(key: string): boolean;
}

/** What the wizard's routing needs to know, and nothing else. */
export interface SetupState {
  baseUrl: string;
  hasToken: boolean;
  dismissed: boolean;
}

/** Read the routing facts from the loaded settings snapshot. */
export function setupStateFrom(reader: SettingsReader): SetupState {
  return {
    baseUrl: reader.text('ha.base_url'),
    hasToken: reader.hasSecret('ha.token'),
    dismissed: reader.flag('ui.setup_dismissed', false)
  };
}

/** `/setup` and `/setup/` are the same page; a deeper path is not the wizard. */
export function isSetupPath(pathname: string): boolean {
  return pathname === SETUP_PATH || pathname === `${SETUP_PATH}/`;
}

/** Configured means both halves a connection needs: a URL and a stored token. */
export function haConfigured(state: SetupState): boolean {
  return state.baseUrl.trim() !== '' && state.hasToken;
}

/** The wizard is due while the install is unconfigured AND not dismissed. */
export function needsSetup(state: SetupState): boolean {
  return !state.dismissed && !haConfigured(state);
}

/**
 * The redirect the shell may perform, or null. Only from the dashboard root:
 * a settings screen the user reached deliberately is never interrupted, and
 * the caller asks after a successful load only (see the module comment).
 */
export function setupRedirectTarget(pathname: string, state: SetupState): string | null {
  return pathname === '/' && needsSetup(state) ? SETUP_PATH : null;
}

/**
 * The redirect a *completed* settings load may perform. A failed load leaves the
 * install's state unknown, and a guess must never hijack the screen, so the
 * caller passes `settings.loaded` and nothing happens when it is false.
 */
export function setupRedirectAfterLoad(
  loaded: boolean,
  pathname: string,
  state: SetupState
): string | null {
  return loaded ? setupRedirectTarget(pathname, state) : null;
}

/** `?setup=done` is the wizard's exit marker; anything else is not an outcome. */
export function setupOutcome(search: string): 'done' | null {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  return params.get('setup') === 'done' ? 'done' : null;
}

/** Where the wizard sends the user when it finishes or is dismissed. */
export function setupDoneUrl(): string {
  return '/?setup=done';
}

/** The facts the LAN hint is derived from; the shell owns how they are observed. */
export interface LanHintInput {
  bindHost: string;
  serverPort: number;
  browserHostname: string;
  browserPort: string;
}

/** A hint about where this install is reachable from another device. */
export interface LanHint {
  kind: 'loopback' | 'network';
  /** null when there is no address the UI can honestly name. */
  url: string | null;
}

/**
 * A loopback (or empty) bind means no other device can reach the server at all,
 * so the hint is "this device only" rather than a URL that would not answer.
 */
export function isLoopbackHost(host: string): boolean {
  const normalised = host.trim().toLowerCase();
  return (
    normalised === '' ||
    normalised === '127.0.0.1' ||
    normalised === 'localhost' ||
    normalised === '::1' ||
    normalised === '[::1]'
  );
}

/** A wildcard bind listens everywhere but names nowhere. */
function isWildcardHost(host: string): boolean {
  const normalised = host.trim();
  return normalised === '0.0.0.0' || normalised === '::' || normalised === '[::]';
}

/** `host:port` (or bare IPv6 in brackets) without doubling a missing port. */
function authority(host: string, port: string): string {
  return port === '' ? host : `${host}:${port}`;
}

/**
 * Where to finish setup from another device.
 *
 * If the browser reached this page from somewhere other than loopback, its own
 * hostname IS this device's LAN address (that is how the page was loaded), so it
 * is the most trustworthy address available. Only then does a concrete bind
 * address act as the fallback, and a wildcard bind yields no URL at all.
 */
export function lanSetupHint(input: LanHintInput): LanHint {
  if (isLoopbackHost(input.bindHost)) {
    return { kind: 'loopback', url: null };
  }

  const browserHost = input.browserHostname.trim();
  if (!isLoopbackHost(browserHost)) {
    const host = authority(browserHost, input.browserPort.trim());
    return { kind: 'network', url: `http://${host}${SETUP_PATH}` };
  }

  const bindHost = input.bindHost.trim();
  if (isWildcardHost(bindHost)) {
    return { kind: 'network', url: null };
  }

  const host = authority(bindHost, String(input.serverPort));
  return { kind: 'network', url: `http://${host}${SETUP_PATH}` };
}
