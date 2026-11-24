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
  import { nextComponentId } from './ui/ids.js';

  /** The rail's description target; see the sr-only sentence below. */
  const stageId = nextComponentId('rail-stage');
</script>

<div
  class="rail"
  data-testid="control-rail"
  role="group"
  aria-label="Dashboard controls"
  aria-describedby={settings.showControlRail ? stageId : undefined}
>
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
    <!--
      The stage is announced, not printed. A pill naming the stage was clipped by
      this column at every panel size (cd-qpg6), and the label belongs in
      Settings -> Audio, which is where a person changes it; on the wall display
      "Software" is a word at three metres that costs a pill's worth of rail. It
      stays in the accessibility tree as the group's description, so a
      screen-reader user still learns what the two buttons move before reaching
      them. `.sr-only` clips, never hides (no display/visibility), so the text is
      still in the a11y tree.
    -->
    <span class="sr-only" id={stageId} data-testid="rail-volume-stage">
      The volume buttons change {settings.railTarget === 'hardware' ? 'hardware' : 'software'} volume.
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
    /* One primary tap target plus one --gap-sm of inline padding either side:
       56 + 2 * 8 = 72px at ui.scale 100%, and it keeps following --tap-primary
       under ui.scale. A fixed column, not a shrink-to-fit: the rail is the
       dashboard's right-hand edge (D28) and nothing in it is sized by its text
       any more. */
    --rail-column: calc(var(--tap-primary) + 2 * var(--gap-sm));
    display: flex;
    flex: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--gap);
    width: var(--rail-column);
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
    /* `overflow-wrap: anywhere` bounds the error's *min-content* width only, so a
       long unbroken string would still be laid out at its intrinsic width; the
       cap keeps it inside the column it lives in. */
    max-width: var(--rail-column);
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
