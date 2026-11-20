/**
 * Configuration transfer's rules: what an import would change, and how long a
 * process has been up.
 *
 * An import is a **merge**, not a replace: every row the document names is
 * written and everything it does not name is left untouched. That is why every
 * section's `removed` is `0` — no row can be deleted by importing a file — and why
 * the panel must print that reason beside the column rather than leave a zero to
 * be read as "nothing to remove".
 *
 * The server is the validator. This module compares the selected document against
 * the current export only to show the user what they are about to write; it must
 * therefore never throw on a malformed file, because the 422 the server answers is
 * the message the user should see. A document that is not an object, or that lacks
 * a section, is treated as an empty section.
 *
 * Secrets are the one asymmetry worth naming: the current export is fetched with
 * `include_secrets=false`, so a secret that is already set is **absent** from the
 * current document and therefore appears under `added`. The settings note says so,
 * so a `1 added` that is really "the token you already have" can be read correctly.
 *
 * Pure TS, no runes, no DOM and no fetch.
 */

/** What one exported section would gain from the selected document. */
export interface SectionDiff {
  /** Keys/rows in the document that are absent from the current config. */
  added: number;
  /** Keys/rows present in both whose values differ. */
  changed: number;
  /** Always `0`: a merge never deletes a key or a row. */
  removed: number;
  /** Why `removed` is zero, in the panel's own words. */
  note: string;
}

/** The four sections one import report compares. */
export interface TransferDiff {
  settings: SectionDiff;
  entities: SectionDiff;
  sounds: SectionDiff;
  buttons: SectionDiff;
}

const SETTINGS_NOTE =
  'merge keeps absent keys; secrets are never exported, so one that is already set appears as added';

const ROWS_NOTE = 'merge keeps absent rows';

/** One value's stable serialisation: object keys sorted, so key order never counts. */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(',')}]`;
  }
  if (typeof value === 'object' && value !== null) {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

/** A section of a document as an object; anything else is an empty section. */
function section(document: unknown, name: string): Record<string, unknown> {
  if (typeof document !== 'object' || document === null || Array.isArray(document)) {
    return {};
  }
  const value = (document as Record<string, unknown>)[name];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

/** A section of a document as a list of rows; anything else is an empty list. */
function rows(document: unknown, name: string): unknown[] {
  if (typeof document !== 'object' || document === null || Array.isArray(document)) {
    return [];
  }
  const value = (document as Record<string, unknown>)[name];
  return Array.isArray(value) ? value : [];
}

/** One row's natural-key fields as strings; a missing or non-string value is `''`. */
function text(row: unknown, key: string): string {
  if (typeof row !== 'object' || row === null || Array.isArray(row)) {
    return '';
  }
  const value = (row as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : '';
}

/** Index rows by one of their natural keys; a row without one cannot be compared. */
function byKey(
  list: readonly unknown[],
  keyOf: (row: unknown) => string
): Map<string, unknown> {
  const indexed = new Map<string, unknown>();
  for (const row of list) {
    const key = keyOf(row);
    if (key !== '') {
      indexed.set(key, row);
    }
  }
  return indexed;
}

/** Compare two keyed collections by the keys and values they hold. */
function diffRows(
  current: readonly unknown[],
  incoming: readonly unknown[],
  keyOf: (row: unknown) => string
): SectionDiff {
  const currentByKey = byKey(current, keyOf);
  let added = 0;
  let changed = 0;
  for (const row of incoming) {
    const key = keyOf(row);
    if (key === '') {
      continue;
    }
    if (!currentByKey.has(key)) {
      added += 1;
    } else if (stableJson(currentByKey.get(key)) !== stableJson(row)) {
      changed += 1;
    }
  }
  return { added, changed, removed: 0, note: ROWS_NOTE };
}

/**
 * The per-section difference between the current configuration and a document.
 *
 * Settings are keyed by setting name, entities by `entity_id`, sounds by `path`
 * for a folder sound and by `url` for a downloaded one (the export's own natural
 * reference, the same one a button uses), and buttons by `slug`. A malformed
 * document — `null`, a string, `{}` — yields four zero counts instead of throwing:
 * the server's 422 is what the user is shown, so this comparison may never be the
 * thing that fails first.
 */
export function compareDocuments(current: unknown, incoming: unknown): TransferDiff {
  const currentSettings = section(current, 'settings');
  const incomingSettings = section(incoming, 'settings');
  let settingsAdded = 0;
  let settingsChanged = 0;
  for (const [key, value] of Object.entries(incomingSettings)) {
    if (!Object.prototype.hasOwnProperty.call(currentSettings, key)) {
      settingsAdded += 1;
    } else if (stableJson(currentSettings[key]) !== stableJson(value)) {
      settingsChanged += 1;
    }
  }

  return {
    settings: {
      added: settingsAdded,
      changed: settingsChanged,
      removed: 0,
      note: SETTINGS_NOTE
    },
    entities: diffRows(rows(current, 'entities'), rows(incoming, 'entities'), (row) =>
      text(row, 'entity_id')
    ),
    sounds: diffRows(rows(current, 'sounds'), rows(incoming, 'sounds'), (row) =>
      text(row, 'source_type') === 'file' ? text(row, 'path') : text(row, 'url')
    ),
    buttons: diffRows(rows(current, 'buttons'), rows(incoming, 'buttons'), (row) =>
      text(row, 'slug')
    )
  };
}

/**
 * How many rows/keys the comparison claims the import would write.
 *
 * Added plus changed, summed over the four sections. A test pins this against the
 * server's aggregate counts, because the two numbers are the same fact said twice:
 * the server counts the rows it wrote, and this counts the differences it found.
 */
export function totalWritten(diff: TransferDiff): number {
  return Object.values(diff).reduce(
    (total, sectionDiff) => total + sectionDiff.added + sectionDiff.changed,
    0
  );
}

/**
 * A process uptime as the About panel prints it: `2 h 14 min`.
 *
 * Two units at most, largest first, because the third never adds a fact a reader
 * uses — "1 d 2 h 14 min" reads as noise on a wall panel. A non-finite or negative
 * value is a value that must never reach the user as `NaN h`.
 */
export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return '0 s';
  }
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (days > 0) {
    return `${days} d ${hours} h`;
  }
  if (hours > 0) {
    return `${hours} h ${minutes} min`;
  }
  if (minutes > 0) {
    return `${minutes} min ${remainder} s`;
  }
  return `${remainder} s`;
}
