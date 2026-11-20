/**
 * Authentication prompt state: one explicit login attempt at a time, with no
 * polling or automatic retries. The cookie belongs to the browser, so success
 * reloads the app and lets every REST and websocket client initialise afresh.
 */

import {
  ApiError,
  getAuthStatus,
  loginWithPassword,
  loginWithToken,
  type AuthStatusResponse
} from './api.js';

let reloadImpl: () => void = () => window.location.reload();

/** The UI-facing camelCase form of the public authentication status. */
export interface AuthStatus {
  listener: 'admin' | 'loopback';
  passwordSet: boolean;
  tokenRequired: boolean;
  protected: boolean;
  authenticated: boolean;
}

function toAuthStatus(response: AuthStatusResponse): AuthStatus {
  return {
    listener: response.listener,
    passwordSet: response.password_set,
    tokenRequired: response.token_required,
    protected: response.protected,
    authenticated: response.authenticated
  };
}

/** Inject the post-login reload, or restore the browser default with `null`. */
export function setReloadImpl(fn: (() => void) | null): void {
  reloadImpl = fn ?? (() => window.location.reload());
}

function loginError(cause: unknown, passwordLogin: boolean): string {
  if (cause instanceof ApiError && cause.status === 401) {
    return passwordLogin ? 'That configuration password was not accepted.' : 'That token was not accepted.';
  }
  if (cause instanceof ApiError && (cause.status === 0 || cause.code === 'network_error')) {
    return 'The server could not be reached. Check your connection and try again.';
  }
  return passwordLogin
    ? 'The configuration password could not be checked. Please try again.'
    : 'The token could not be checked. Please try again.';
}

/** Rune-backed state for the authentication gate and settings roots. */
export class AuthState {
  #required = $state(false);
  #busy = $state(false);
  #error = $state<string | null>(null);
  #status = $state<AuthStatus | null>(null);
  #statusLoad: Promise<void> | null = null;

  /** Whether the login prompt is currently raised. */
  get required(): boolean {
    return this.#required;
  }

  /** Whether one login request is in flight. */
  get busy(): boolean {
    return this.#busy;
  }

  /** The last user-facing failure, without any submitted credential. */
  get error(): string | null {
    return this.#error;
  }

  /** Nullable listener/gate facts; loading them never blocks a route. */
  get status(): AuthStatus | null {
    return this.#status;
  }

  /**
   * Load the public auth facts once at a time. It is deliberately best-effort:
   * a missing endpoint on an older kiosk backend must not surface an error or
   * stop that route from working. Concurrent callers share the same request.
   */
  loadStatus(): Promise<void> {
    if (this.#statusLoad !== null) {
      return this.#statusLoad;
    }

    const loading = (async (): Promise<void> => {
      try {
        this.#status = toAuthStatus(await getAuthStatus());
      } catch {
        // Authentication status is advisory; preserve the last known state.
      } finally {
        this.#statusLoad = null;
      }
    })();
    this.#statusLoad = loading;
    return loading;
  }

  /** Update the local gate facts after a successful password mutation. */
  passwordSetChanged(passwordSet: boolean): void {
    if (this.#status === null) {
      return;
    }
    this.#status = {
      ...this.#status,
      passwordSet,
      protected: passwordSet || this.#status.tokenRequired,
      authenticated: true
    };
  }

  /** Raise the prompt once; repeated auth signals cannot queue more work. */
  promptRequired(): void {
    if (this.#required) {
      return;
    }
    this.#required = true;
  }

  /** Make one login request; concurrent submits are ignored until it settles. */
  async submit(value: string): Promise<boolean> {
    if (this.#busy) {
      return false;
    }
    const passwordLogin = this.#status?.passwordSet === true;
    if (value.length === 0) {
      this.#error = passwordLogin ? 'Enter the configuration password.' : 'Enter the shared token.';
      return false;
    }

    this.#busy = true;
    this.#error = null;
    try {
      if (passwordLogin) {
        await loginWithPassword(value);
      } else {
        // Unknown status intentionally retains the pre-auth shared-token path
        // used by the kiosk and older backends.
        await loginWithToken(value);
      }
    } catch (cause) {
      this.#error = loginError(cause, passwordLogin);
      this.#busy = false;
      return false;
    }

    this.#busy = false;
    reloadImpl();
    return true;
  }

  /** Clear the visible failure before the next attempt. */
  dismissError(): void {
    this.#error = null;
  }
}

export const auth = new AuthState();
