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
    /* One rail control plus one --gap-sm of inline padding either side is
       80 + 2 * 8 = 96px at ui.scale 100%. The gutters are the deliberate gap
       between the control and the column edge; the column still follows
       --rail-control under ui.scale. A fixed column, not shrink-to-fit. */
    --rail-column: calc(var(--rail-control) + 2 * var(--gap-sm));
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
    /* The controls ask how tall the column is (`cqh` below). The rail's height
       is already the stretched height of `.body`, i.e. definite, so size
       containment cannot collapse it; it only stops the controls' size from
       feeding back into the column. */
    container-type: size;
  }

  .control {
    display: grid;
    flex: none;
    place-items: center;
    /* The target is --rail-control (80px), but four of them plus three --gap
       must fit the column. Measured on the dev host: at ui.scale 130% on
       1024x600 the column is 392px, and at 150% on 1280x720 it is 460px, so a
       flat 80px stack (4 * 80 + 3 * 16 = 368 layout px) clips the settings gear
       at those steps. The clamp takes the largest square that fits and never
       goes below --tap-primary, so a touch target only ever grows from today's
       56px and the rail never clips where it does not clip today. 100cqh is the
       column's height (see container-type above). */
    --rail-fit: clamp(var(--tap-primary), calc((100cqh - 3 * var(--gap)) / 4), var(--rail-control));
    width: var(--rail-fit);
    height: var(--rail-fit);
    min-width: var(--tap-primary);
    min-height: var(--tap-primary);
    padding: 0;
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: var(--text);
    transition: background-color var(--motion-fast) var(--ease);
  }

  /* cd-bdjz: the icon rides the control instead of a fixed `size` prop, so the
     drawn glyph stays ~40% of the button at every size the clamp picks. The MDI
     volume path's own box is 19 of the 24 viewBox units, measured in the
     browser; a 50% box therefore paints the glyph at 38.6% of an 80px control
     measured in a real render (the percentage resolves against the 78px content
     box inside the 1px border). It was 24px in a 56px control, i.e. 33.9%,
     before. */
  .control :global(svg) {
    width: 50%;
    height: 50%;
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
