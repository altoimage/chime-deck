<!--
  Dashboard control rail (D28): the dashboard's only control surface, present
  in normal and kiosk mode. The volume steps are gated by `ui.show_control_rail`
  (a panel with no speaker turns them off); the mute button and the Settings
  link are part of the shell and stay unconditional, so navigation starts the
  settings shell cleanly and the gear is always visible.
-->
<script lang="ts">
  import { settings } from './settings.svelte.js';
  import { stepVolume } from './volume.js';
  import Icon from './ui/Icon.svelte';
</script>

<div class="rail" data-testid="control-rail" role="group" aria-label="Dashboard controls">
  {#if settings.showControlRail}
    <button
      class="control"
      type="button"
      aria-label={`Turn ${settings.railTarget} volume up`}
      data-testid="rail-volume-up"
      onclick={() =>
        void settings.setRailVolume(
          stepVolume(
            settings.railTarget === 'hardware' ? settings.hardwareVolume : settings.volume,
            'up'
          )
        )}
    >
      <Icon name="mdi:volume-plus" size={24} />
    </button>
    <span class="target-caption" data-testid="rail-volume-target">
      {settings.railTarget === 'hardware' ? 'Hardware' : 'Software'}
    </span>
    <button
      class="control"
      type="button"
      aria-label={`Turn ${settings.railTarget} volume down`}
      data-testid="rail-volume-down"
      onclick={() =>
        void settings.setRailVolume(
          stepVolume(
            settings.railTarget === 'hardware' ? settings.hardwareVolume : settings.volume,
            'down'
          )
        )}
    >
      <Icon name="mdi:volume-minus" size={24} />
    </button>
  {/if}
  <button
    class="control"
    type="button"
    aria-label={settings.muted ? 'Unmute' : 'Mute'}
    data-testid="rail-mute"
    onclick={() => void settings.setMuted(!settings.muted)}
  >
    <Icon name={settings.muted ? 'mdi:volume-off' : 'mdi:volume-high'} size={24} />
  </button>
  <a class="control settings" href="/settings" aria-label="Settings" data-testid="rail-settings">
    <Icon name="mdi:cog-outline" size={24} />
  </a>
  {#if settings.error !== null}
    <p class="rail-error" role="status">{settings.error}</p>
  {/if}
</div>

<style>
  .rail {
    display: flex;
    flex: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--gap);
    width: 72px;
    max-height: 100%;
    padding: 0 var(--gap-sm);
    overflow: hidden;
  }

  .control {
    display: grid;
    flex: none;
    place-items: center;
    width: var(--tap-primary);
    height: var(--tap-primary);
    min-width: var(--tap-primary);
    min-height: var(--tap-primary);
    padding: 0;
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: var(--text);
    transition: background-color var(--motion-fast) var(--ease);
  }

  button.control {
    font: inherit;
    cursor: pointer;
  }

  .target-caption {
    display: flex;
    flex: none;
    align-items: center;
    justify-content: center;
    width: 72px;
    min-height: 24px;
    margin-inline: calc(-1 * var(--gap-sm));
    padding: 0;
    border: var(--border-width) solid var(--border);
    border-radius: 999px;
    background: var(--surface-hover);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    line-height: 1.1;
    white-space: nowrap;
  }

  .settings {
    display: grid;
    place-items: center;
    text-decoration: none;
  }

  .control:hover {
    background: var(--surface-hover);
  }

  .control:focus-visible {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .rail-error {
    max-height: 4.5em;
    margin: 0;
    overflow: hidden;
    overflow-wrap: anywhere;
    color: var(--state-alert);
    font-size: var(--text-label);
    line-height: 1.5;
    text-align: center;
  }
</style>
