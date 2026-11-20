/**
 * The audio-device rows a picker offers, and the warning it must not swallow.
 *
 * The trap this module exists to expose (HSD-h13q): a stored `audio.device_hint`
 * that matches no discovered device makes the backend fall back to the system
 * default with only a log warning, so the doorbell plays on the wrong output and
 * nothing on screen says why. The picker therefore shows the device the running
 * backend actually resolved, and `matchNotice` says the fallback out loud.
 *
 * Two backend facts the rows follow exactly
 * (`src/chime_deck/audio/devices.py::resolve_device`, `mpv_backend._mark_default`):
 *
 *   - `resolve_device` matches a case-insensitive SUBSTRING of the device NAME
 *     only, never the description or the host API, so a row stores the full name
 *     (which always self-matches) and shows the host API separately;
 *   - mpv lists an `auto` device on most hosts, which must not become a second
 *     "system default" row: the sentinel row is the only one.
 *
 * Pure TS, no DOM and no runes: the panel renders, this module decides.
 */

import type { AudioDevicesResponse } from './api.js';

/** What the system-default row stores when the user lets the engine choose. */
const SYSTEM_DEFAULT_LABEL = 'System default';

/** Why the system-default row exists, in words a user can act on. */
const SYSTEM_DEFAULT_DESCRIPTION = 'Let the audio engine use the host default output';

/** One row the device picker offers. */
export interface DeviceChoice {
  /** What gets STORED in `audio.device_hint` — the full mpv name, or the `auto` sentinel. */
  value: string;
  /** What the user reads. */
  label: string;
  /** The host API, or the explanation for the system-default row. */
  description: string;
  /** The row the running backend actually resolved. */
  resolved: boolean;
}

/**
 * The picker rows for a discovery result: the system default first, then mpv's
 * order.
 *
 * A failed discovery still offers the system-default row, because that is the
 * honest fallback: the backend would use it, so the picker must not be empty
 * while the error sentence explains the failure.
 */
export function deviceChoices(response: AudioDevicesResponse): DeviceChoice[] {
  const systemDefault = response.system_default;
  const resolved = response.resolved_device;
  // The sentinel resolves to the system-default row when the backend names it back
  // verbatim, so the DEDUPE below can never leave the whole list showing no resolved row.
  const systemDefaultResolved =
    resolved === null || resolved.toLowerCase() === systemDefault.toLowerCase();

  const choices: DeviceChoice[] = [
    {
      value: systemDefault,
      label: SYSTEM_DEFAULT_LABEL,
      description: SYSTEM_DEFAULT_DESCRIPTION,
      resolved: systemDefaultResolved
    }
  ];

  // mpv lists its own `auto` entry on most hosts; a second row for it would be a
  // control whose only possible effect is to say what the first row already says.
  const seenNames = new Set([systemDefault.toLowerCase()]);

  for (const device of response.devices) {
    const name = device.name.toLowerCase();
    if (seenNames.has(name)) {
      continue;
    }
    seenNames.add(name);
    choices.push({
      value: device.name,
      label: device.name,
      description: device.is_default ? `${device.hostapi} · host default` : device.hostapi,
      resolved: device.name === resolved
    });
  }

  return choices;
}

/**
 * The honest warning when the stored hint matched no discovered device, else
 * `null`.
 *
 * The four silent cases are deliberate: a failed discovery already has its own
 * error row, an empty hint is the documented "let the engine choose", a non-null
 * `resolved_device` means the hint did match, and the `auto` sentinel needs no
 * warning because falling back to the system default is exactly what it asked
 * for.
 */
export function matchNotice(response: AudioDevicesResponse): string | null {
  if (!response.ok) {
    return null;
  }
  const hint = response.hint.trim();
  if (hint === '' || response.resolved_device !== null) {
    return null;
  }
  if (hint.toLowerCase() === response.system_default.toLowerCase()) {
    return null;
  }
  return `The stored device “${hint}” matches no discovered output, so the system default will be used. Pick a device from this list to change that.`;
}
