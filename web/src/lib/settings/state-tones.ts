/**
 * Pure rules for the entity editor's state→tone table.
 *
 * The resolver's built-in table belongs to the backend. This module only merges
 * the fetched defaults with a tile's explicit overrides for editing; it never
 * guesses the tone of the live tile (that still comes from the backend row).
 */

export interface StateToneRow {
  state: string;
  tone: string | null;
  provenance: 'default' | 'custom';
  hasDefault: boolean;
  pending: boolean;
}

function hasOwn(map: Record<string, string>, state: string): boolean {
  return Object.prototype.hasOwnProperty.call(map, state);
}

/** These Home Assistant sentinel states are always shown grey and cannot be mapped. */
export function isNeverMappedState(state: string): boolean {
  const normalized = state.trim().toLowerCase();
  return normalized === 'unavailable' || normalized === 'unknown';
}

/** The editor's effective view: backend defaults with explicit tile choices on top. */
export function mergeStateTones(
  defaults: Record<string, string>,
  overrides: Record<string, string>
): Record<string, string> {
  const entries = [
    ...Object.entries(defaults).filter(([state]) => !isNeverMappedState(state)),
    ...Object.entries(overrides).filter(([state]) => !isNeverMappedState(state))
  ];
  // fromEntries keeps raw tokens such as "__proto__" as ordinary own keys.
  return Object.fromEntries(entries);
}

/**
 * Current state first, then all explicit tile choices, then untouched defaults.
 * A pending row is the focused, not-yet-chosen row created by the current-state
 * callout; it is not written into the draft until the user picks a tone.
 */
export function stateToneRows(
  defaults: Record<string, string>,
  overrides: Record<string, string>,
  currentState: string | null,
  pendingState: string | null = null
): StateToneRow[] {
  const effective = mergeStateTones(defaults, overrides);
  const states: string[] = [];
  const seen = new Set<string>();
  const add = (state: string): void => {
    if (!isNeverMappedState(state) && !seen.has(state)) {
      seen.add(state);
      states.push(state);
    }
  };

  if (
    currentState !== null &&
    currentState.trim() !== '' &&
    (hasOwn(effective, currentState) || currentState === pendingState)
  ) {
    add(currentState);
  }

  for (const state of Object.keys(overrides)) {
    add(state);
  }
  if (pendingState !== null) {
    add(pendingState);
  }
  for (const state of Object.keys(defaults)) {
    add(state);
  }

  return states.map((state) => {
    const isPending = state === pendingState && !hasOwn(overrides, state) && !hasOwn(defaults, state);
    const explicit = hasOwn(overrides, state);
    return {
      state,
      tone: isPending ? null : (effective[state] ?? null),
      provenance: explicit || isPending ? 'custom' : 'default',
      hasDefault: hasOwn(defaults, state),
      pending: isPending
    };
  });
}

/** A human message for an invalid Add state attempt, or `null` when it is usable. */
export function validateNewState(
  state: string,
  defaults: Record<string, string>,
  overrides: Record<string, string>,
  pendingState: string | null = null
): string | null {
  if (state.trim() === '') {
    return 'Enter a state value.';
  }
  if (isNeverMappedState(state)) {
    return 'Unavailable and unknown are never coloured; the tile always shows them grey.';
  }
  if (hasOwn(defaults, state) || hasOwn(overrides, state) || state === pendingState) {
    return 'That state already has a tone mapping.';
  }
  return null;
}
