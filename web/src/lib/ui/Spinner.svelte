<script lang="ts">
  interface Props {
    /** Diameter in px. */
    size?: number;
    /** Visible and announced via role="status". */
    label?: string;
  }

  const { size = 32, label = 'Loading' }: Props = $props();
</script>

<span class="spinner" role="status" style:--spinner-size="{size}px">
  <span class="ring" aria-hidden="true"></span>
  <span class="status-label">{label}</span>
</span>

<style>
  .spinner {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--spinner-size);
    flex-shrink: 0;
  }

  /* Both colours derive from currentColor so the ring stays legible on any
     fill (e.g. a Spinner inside a primary Button on --accent). */
  .ring {
    width: var(--spinner-size);
    height: var(--spinner-size);
    flex-shrink: 0;
    border-radius: 50%;
    border: max(2px, calc(var(--spinner-size) / 8)) solid
      color-mix(in srgb, currentColor 30%, transparent);
    border-top-color: currentColor;
    animation: spinner-rotate 800ms linear infinite;
  }

  .status-label {
    font-size: var(--text-body);
    line-height: 1.35;
    font-weight: 600;
  }

  @keyframes spinner-rotate {
    to {
      transform: rotate(360deg);
    }
  }

  /*
   * Reduced motion: render a static ring (never a frozen animation) — the
   * role="status" label still announces state. The global 0ms kill-switch
   * would also stop the rotation; this makes the intent explicit.
   */
  @media (prefers-reduced-motion: reduce) {
    .ring {
      animation: none;
    }
  }
</style>
