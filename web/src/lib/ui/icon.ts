/**
 * Icon resolution for the whole UI: a curated bundle, a runtime cache behind it,
 * and one fallback so nothing ever throws.
 *
 * Bundled icons come from named `@mdi/js` imports only, so the bundler
 * tree-shakes the other ~7400 paths out; a name the bundle does not carry is
 * looked up in `GET /api/icon/{name}`'s answer once and remembered here (the
 * icon picker offers the whole shipped set, so an arbitrary `mdi:` name must
 * draw something other than a question mark). The lookup order is deliberate:
 * bundled first, because a dashboard tile's rendering must not change when a
 * background fetch fills the cache, then the fetched path, then the
 * help-circle fallback.
 */

import {
  mdiAccount,
  mdiAlertCircleOutline,
  mdiBattery,
  mdiBatteryOutline,
  mdiBell,
  mdiBlinds,
  mdiCheck,
  mdiCheckCircleOutline,
  mdiChevronDown,
  mdiChevronRight,
  mdiChevronUp,
  mdiClose,
  mdiCogOutline,
  mdiDeleteOutline,
  mdiDevices,
  mdiDownload,
  mdiDoor,
  mdiDoorClosed,
  mdiDoorOpen,
  mdiDragVertical,
  mdiEye,
  mdiEyeOff,
  mdiGarage,
  mdiGauge,
  mdiHelpCircleOutline,
  mdiHistory,
  mdiHome,
  mdiInformationOutline,
  mdiLanConnect,
  mdiLightbulb,
  mdiLightbulbOutline,
  mdiLock,
  mdiLockAlert,
  mdiLockOpen,
  mdiMinus,
  mdiMotionSensor,
  mdiMusicNote,
  mdiPencil,
  mdiPlay,
  mdiPlus,
  mdiPowerPlug,
  mdiSpeaker,
  mdiStop,
  mdiThermometer,
  mdiUpload,
  mdiVolumeHigh,
  mdiVolumeMinus,
  mdiVolumeOff,
  mdiVolumePlus,
  mdiWater,
  mdiWeatherNight,
  mdiWindowClosed,
  mdiWindowOpen
} from '@mdi/js';

/** Bundled icons, keyed by the camelCase form of their mdi name. */
const ICON_PATHS: Record<string, string> = {
  account: mdiAccount,
  alertCircleOutline: mdiAlertCircleOutline,
  battery: mdiBattery,
  batteryOutline: mdiBatteryOutline,
  bell: mdiBell,
  blinds: mdiBlinds,
  check: mdiCheck,
  checkCircleOutline: mdiCheckCircleOutline,
  chevronDown: mdiChevronDown,
  chevronRight: mdiChevronRight,
  chevronUp: mdiChevronUp,
  close: mdiClose,
  /*
   * The UI's own chrome is bundled on purpose: the dashboard's settings link
   * and the settings nav must draw on the first paint, never depend on a
   * `GET /api/icon/…` answering.
   */
  cogOutline: mdiCogOutline,
  deleteOutline: mdiDeleteOutline,
  download: mdiDownload,
  /* The Entities settings section; a nav entry must never be a question mark. */
  devices: mdiDevices,
  lanConnect: mdiLanConnect,
  door: mdiDoor,
  doorClosed: mdiDoorClosed,
  doorOpen: mdiDoorOpen,
  dragVertical: mdiDragVertical,
  eye: mdiEye,
  eyeOff: mdiEyeOff,
  garage: mdiGarage,
  gauge: mdiGauge,
  helpCircleOutline: mdiHelpCircleOutline,
  /* The History settings nav entry must draw on first paint, never a question mark. */
  history: mdiHistory,
  home: mdiHome,
  informationOutline: mdiInformationOutline,
  lightbulb: mdiLightbulb,
  lightbulbOutline: mdiLightbulbOutline,
  lock: mdiLock,
  lockAlert: mdiLockAlert,
  lockOpen: mdiLockOpen,
  minus: mdiMinus,
  motionSensor: mdiMotionSensor,
  /*
   * The Sounds settings section's nav entry and its preview control: a nav
   * icon must draw on the first paint, and a "Stop preview" button drawn as a
   * cross would name its glyph, not its action.
   */
  musicNote: mdiMusicNote,
  pencil: mdiPencil,
  play: mdiPlay,
  plus: mdiPlus,
  powerPlug: mdiPowerPlug,
  speaker: mdiSpeaker,
  stop: mdiStop,
  thermometer: mdiThermometer,
  /*
   * The dashboard rail's volume up/down buttons and the header's moon badge
   * (D28): chrome that must draw on the first paint, so a fallback question mark
   * here would be a UI bug, not a missing decoration.
   */
  volumeHigh: mdiVolumeHigh,
  volumeMinus: mdiVolumeMinus,
  volumeOff: mdiVolumeOff,
  volumePlus: mdiVolumePlus,
  weatherNight: mdiWeatherNight,
  upload: mdiUpload,
  water: mdiWater,
  windowClosed: mdiWindowClosed,
  windowOpen: mdiWindowOpen
};

/**
 * Fallback path for unknown names: help-circle-outline reads as
 * "unknown icon" without breaking layout. Resolution must never throw —
 * a bad icon name from the server is a rendering detail, not a crash.
 */
export const FALLBACK_ICON: string = mdiHelpCircleOutline;

/**
 * Path data fetched at runtime, keyed by the same normalised key `ICON_PATHS`
 * uses. Module-level on purpose: a remembered path must survive the component
 * that fetched it, so the next render of the same icon costs no request.
 */
const fetchedPaths = new Map<string, string>();

/** One spelling for a name: `mdi:door-open`, `door-open` and `doorOpen` agree. */
function iconKey(name: string): string {
  const stripped = name.startsWith('mdi:') ? name.slice(4) : name;
  return stripped.replace(/-([a-z])/g, (_match, ch: string) => ch.toUpperCase());
}

/**
 * Resolve an icon name to SVG path data.
 *
 * Bundled paths win over remembered ones, so a later fetch can only ever fill a
 * gap — it can never change how an already-bundled icon draws. Unknown names
 * return `FALLBACK_ICON`.
 */
export function resolveIcon(name: string): string {
  const key = iconKey(name);
  return ICON_PATHS[key] ?? fetchedPaths.get(key) ?? FALLBACK_ICON;
}

/**
 * Cache one path fetched from `GET /api/icon/{name}` so later renders are free.
 * Accepts either spelling (`mdi:door` or `door`).
 */
export function rememberIconPath(name: string, path: string): void {
  fetchedPaths.set(iconKey(name), path);
}

/** Drop the runtime cache. Test hook only — the UI never needs to forget. */
export function forgetIconPaths(): void {
  fetchedPaths.clear();
}
