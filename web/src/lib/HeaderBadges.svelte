<!-- State-only explanation badges for conditions that currently suppress sound (D28). -->
<script lang="ts">
  import { headerBadges } from './dashboard-badges.js';
  import { settings } from './settings.svelte.js';
  import Badge from './ui/Badge.svelte';

  let now = $state(new Date());

  $effect(() => {
    const interval = setInterval(() => {
      now = new Date();
    }, 30_000);
    return () => clearInterval(interval);
  });

  const badges = $derived(
    headerBadges({
      now,
      quietEnabled: settings.flag('audio.quiet_hours_enabled', false),
      quietStart: settings.text('audio.quiet_hours_start', '22:00'),
      quietEnd: settings.text('audio.quiet_hours_end', '07:00'),
      quietVolume: settings.integer('audio.quiet_hours_volume', 0, 100, 0),
      muted: settings.muted,
      volume: settings.volume,
      hardwareVolume: settings.hardwareVolume
    })
  );
</script>

<div class="badges">
  {#if badges.quietHours}
    <span data-testid="badge-quiet-hours">
      <Badge kind="neutral" label="Quiet hours" icon="mdi:weather-night" />
    </span>
  {/if}
  {#if badges.muted}
    <span data-testid="badge-muted">
      <Badge kind="neutral" label="Muted" icon="mdi:volume-off" />
    </span>
  {/if}
</div>

<style>
  .badges {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }
</style>
