/**
 * The settings route vocabulary: which sections exist, how a pathname maps to
 * one, and what URL names it.
 *
 * Pure TS (no runes, no DOM) so the routing truth table is testable in the
 * plain node environment.
 *
 * **Nav decision.** The nav lists only the sections this build implements — no
 * dead entries. A future section is added here together with its panel, so the
 * two can never disagree. A hand-typed deep link to a section that does not
 * exist yet therefore parses to `unknown` rather than silently falling back to
 * Connection: the shell can then say plainly that the section is not available
 * in this build instead of showing a blank screen or pretending the user landed
 * somewhere they did not.
 *
 * **`rest`.** The Entities section owns sub-routes of its own — the list at
 * `/settings/entities`, the add form at `/settings/entities/add`, and one row's
 * editor at `/settings/entities/<id>`. The shell has no business knowing what
 * those mean, so a parsed target carries the tail verbatim as `rest`: `''` for
 * the section itself, otherwise `'add'`, `'7'` or `'a/b'`. The panel decides what
 * a tail names, which keeps every future sub-route (a sounds editor, a button
 * detail) from needing a branch in the router.
 */

/** Every implemented settings section. */
export type SectionId =
  | 'connection'
  | 'security'
  | 'entities'
  | 'buttons'
  | 'sounds'
  | 'speech'
  | 'audio'
  | 'appearance'
  | 'history'
  | 'about';

/** One entry in the settings nav. */
export interface SectionDescriptor {
  id: SectionId;
  label: string;
  /** MDI icon name, resolved by `ui/Icon.svelte`. */
  icon: string;
  /** The canonical URL of the section. */
  path: string;
}

/**
 * The implemented sections, in nav order. This is the single extension point:
 * a later ticket adds one entry here and one panel branch in
 * `SettingsLayout.svelte` — nothing else in the shell changes.
 */
export const SECTIONS: readonly SectionDescriptor[] = [
  {
    id: 'connection',
    label: 'Connection',
    icon: 'mdi:lan-connect',
    path: '/settings/connection'
  },
  {
    id: 'security',
    label: 'Security',
    icon: 'mdi:shield-lock-outline',
    path: '/settings/security'
  },
  {
    id: 'entities',
    label: 'Entities',
    icon: 'mdi:devices',
    path: '/settings/entities'
  },
  {
    id: 'buttons',
    label: 'Buttons',
    icon: 'mdi:bell',
    path: '/settings/buttons'
  },
  {
    id: 'sounds',
    label: 'Sounds',
    icon: 'mdi:music-note',
    path: '/settings/sounds'
  },
  {
    id: 'speech',
    label: 'Speech',
    icon: 'mdi:speaker',
    path: '/settings/speech'
  },
  {
    id: 'audio',
    label: 'Audio',
    icon: 'mdi:volume-high',
    path: '/settings/audio'
  },
  {
    id: 'appearance',
    label: 'Appearance',
    icon: 'mdi:palette-outline',
    path: '/settings/appearance'
  },
  {
    id: 'history',
    label: 'History',
    icon: 'mdi:history',
    path: '/settings/history'
  },
  {
    id: 'about',
    label: 'About',
    icon: 'mdi:information-outline',
    path: '/settings/about'
  }
];

/** The section `/settings` itself resolves to. */
export const DEFAULT_SECTION: SectionId = 'connection';

const SETTINGS_ROOT = '/settings';

/** Whether *pathname* belongs to the settings route (and therefore the shell). */
export function isSettingsPath(pathname: string): boolean {
  return pathname === SETTINGS_ROOT || pathname.startsWith(`${SETTINGS_ROOT}/`);
}

/** What a `/settings…` pathname resolves to. */
export type SettingsTarget =
  | { kind: 'section'; id: SectionId; rest: string }
  | { kind: 'unknown'; requested: string };

/** Trim a trailing slash, except from the root (`/` stays `/`). */
function withoutTrailingSlash(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith('/')) {
    return pathname.slice(0, -1);
  }
  return pathname;
}

function isSectionId(value: string): value is SectionId {
  return SECTIONS.some((section) => section.id === value);
}

/**
 * Map a pathname onto a section or an explicit unknown target.
 *
 * - `/settings` and `/settings/` (and an empty pathname) → the default section
 * - `/settings/<implemented id>` → that section, `rest: ''`
 * - `/settings/<implemented id>/<tail>` → that section, `rest` = the tail
 *   verbatim (`add`, `7`, `a/b`) — the section's sub-routes belong to its panel
 * - `/settings/<anything else>` → `unknown`, carrying the raw requested segment
 * - anything outside `/settings` → `unknown`, carrying the raw pathname (the
 *   shell is only mounted for settings paths, so this is a defensive case)
 */
export function parseSettingsPath(pathname: string): SettingsTarget {
  const normalised = withoutTrailingSlash(pathname.trim());

  if (normalised === '' || normalised === SETTINGS_ROOT) {
    return { kind: 'section', id: DEFAULT_SECTION, rest: '' };
  }

  if (normalised.startsWith(`${SETTINGS_ROOT}/`)) {
    const tail = normalised.slice(SETTINGS_ROOT.length + 1);
    const slash = tail.indexOf('/');
    const segment = slash === -1 ? tail : tail.slice(0, slash);
    if (isSectionId(segment)) {
      return { kind: 'section', id: segment, rest: slash === -1 ? '' : tail.slice(slash + 1) };
    }
    return { kind: 'unknown', requested: tail };
  }

  return { kind: 'unknown', requested: normalised };
}

/** The canonical path of a section; `settingsPath(parseSettingsPath(p).id) === p`. */
export function settingsPath(id: SectionId): string {
  return `${SETTINGS_ROOT}/${id}`;
}
