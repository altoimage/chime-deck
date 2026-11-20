<!--
  One icon, drawn from SVG path data.

  The bundled set is deliberately small (the 43 paths the UI itself uses), so a name it does
  not carry would otherwise render the help-circle fallback and the icon picker
  would lie about what the user is choosing. That is why this component may ask
  `GET /api/icon/{name}` for exactly one missing path: one request per name for
  the whole session, shared by every instance through the module-level map
  below, and any failure falls back silently — a broken icon fetch is a
  rendering detail, never an error the user must see.

  The picker does not even need that request: it passes the `path` it already
  has from `/api/icons/search`, so a 120-tile grid costs zero extra requests.
-->
<script lang="ts">
  import { getIconPath } from '../api.js';
  import { FALLBACK_ICON, rememberIconPath, resolveIcon } from './icon.js';

  interface Props {
    /** Icon name: `mdi:door-open`, `door-open` or `doorOpen`. Unknown names render a fallback. */
    name: string;
    /** Rendered size in px (both width and height). */
    size?: number;
    /** Accessible label. Omit for decorative icons (they are aria-hidden). */
    label?: string;
    /**
     * SVG path data to draw directly, bypassing name resolution. The icon
     * picker's grid passes what the search response already carried.
     */
    path?: string;
  }

  const { name, size = 24, label, path }: Props = $props();

  /**
   * One lookup per name, shared by every Icon on the page. A settled `null`
   * means "the set does not ship this name", which is remembered too: a missing
   * icon must not turn into a request on every render.
   */
  const lookups = new Map<string, Promise<string | null>>();

  let fetchedPath = $state<string | null>(null);

  const drawn = $derived(path ?? fetchedPath ?? resolveIcon(name));

  /** Whether a name resolves to the fallback, read through the shared cache. */
  function needsLookup(iconName: string): boolean {
    // help-circle is what the caller asked for, not a gap in the bundle.
    if (iconName === 'mdi:help-circle-outline' || iconName === 'help-circle-outline') {
      return false;
    }
    return resolveIcon(iconName) === FALLBACK_ICON;
  }

  $effect(() => {
    const iconName = name;
    if (path !== undefined || !needsLookup(iconName)) {
      return;
    }

    let pending = lookups.get(iconName);
    if (pending === undefined) {
      pending = getIconPath(iconName)
        .then((answer) => {
          rememberIconPath(iconName, answer.path);
          return answer.path;
        })
        .catch(() => null);
      lookups.set(iconName, pending);
    }
    void pending.then((answer) => {
      fetchedPath = answer;
    });
  });
</script>

<svg
  viewBox="0 0 24 24"
  width={size}
  height={size}
  fill="currentColor"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  focusable="false"
>
  <path d={drawn} />
</svg>
