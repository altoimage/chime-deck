<!--
  GALLERY_DEV_ONLY_SENTINEL

  Dev-only component gallery, reachable at /gallery only when
  import.meta.env.DEV is true (see main.ts). check:dist asserts this sentinel
  string and any Gallery* chunk are ABSENT from the production dist/.
-->
<script lang="ts">
  import { theme, type ThemeChoice } from '../lib/theme.svelte.js';
  import type { SortableItem } from '../lib/reorder.js';
  import { longPressDrag } from '../lib/ui/drag.js';
  import { toastStore } from '../lib/ui/toast.svelte.js';
  import Badge from '../lib/ui/Badge.svelte';
  import Button from '../lib/ui/Button.svelte';
  import Card from '../lib/ui/Card.svelte';
  import Checkbox from '../lib/ui/Checkbox.svelte';
  import ColorField from '../lib/ui/ColorField.svelte';
  import Dialog from '../lib/ui/Dialog.svelte';
  import EmptyState from '../lib/ui/EmptyState.svelte';
  import Icon from '../lib/ui/Icon.svelte';
  import IconButton from '../lib/ui/IconButton.svelte';
  import Input from '../lib/ui/Input.svelte';
  import SegmentedControl from '../lib/ui/SegmentedControl.svelte';
  import Select from '../lib/ui/Select.svelte';
  import Sheet from '../lib/ui/Sheet.svelte';
  import Slider from '../lib/ui/Slider.svelte';
  import SortableList from '../lib/ui/SortableList.svelte';
  import Spinner from '../lib/ui/Spinner.svelte';
  import Stepper from '../lib/ui/Stepper.svelte';
  import Textarea from '../lib/ui/Textarea.svelte';
  import Toast from '../lib/ui/Toast.svelte';
  import ToastHost from '../lib/ui/ToastHost.svelte';
  import Toggle from '../lib/ui/Toggle.svelte';

  // ---- theme ----
  let themeChoice = $state<ThemeChoice>(theme.choice);

  $effect(() => {
    theme.apply();
    return theme.subscribeSystem();
  });

  $effect(() => {
    theme.set(themeChoice);
  });

  // ---- demo state ----
  let name = $state('Front door');
  let token = $state('placeholder-not-a-token');
  let notes = $state('Ring twice, then flash the hallway light.');
  let showName = $state(true);
  let chimeVolume = $state(70);
  let repeatCount = $state(2);
  let doorSide = $state('single');
  let chime = $state('ding-dong');
  let showBattery = $state(true);
  let accent = $state<string | null>('#4f9cf9');

  let sheetOpen = $state(false);
  let dialogOpen = $state(false);
  let destructiveOpen = $state(false);
  let lastDialogResult = $state('nothing yet');

  const chimeOptions = [
    { value: 'ding-dong', label: 'Ding dong', description: 'The classic two-note chime' },
    { value: 'westminster', label: 'Westminster', description: 'Four-note clock chime' },
    { value: 'knock', label: 'Knock knock', description: 'Two soft knocks' },
    { value: 'chirp', label: 'Chirp', description: 'Short, high, attention-grabbing' },
    { value: 'bell', label: 'Bell', description: 'Single brass bell strike' },
    { value: 'whoosh', label: 'Whoosh', description: 'Soft synth sweep' },
    {
      value: 'siren',
      label: 'Siren',
      disabled: true,
      reason: 'Not installed on this device'
    }
  ];

  // ---- drag demo ----
  interface DemoRow {
    id: number;
    label: string;
  }

  let rows = $state<DemoRow[]>([
    { id: 1, label: 'Front door' },
    { id: 2, label: 'Back gate' },
    { id: 3, label: 'Garage' },
    { id: 4, label: 'Porch light' }
  ]);

  let listEl: HTMLDivElement | undefined = $state();
  let dragIndex = $state<number | null>(null);
  let overIndex = $state<number | null>(null);
  let dragDy = $state(0);
  let lineTop = $state(0);
  let rowBounds: { top: number; bottom: number; mid: number }[] = [];
  let grabOffsetY = 0;

  function moveRow(from: number, to: number): void {
    if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) {
      return;
    }
    const next = [...rows];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    rows = next;
  }

  function startDrag(index: number, startY: number): void {
    dragIndex = index;
    overIndex = null;
    dragDy = 0;
    const list = listEl;
    if (!list) {
      return;
    }
    const listRect = list.getBoundingClientRect();
    rowBounds = Array.from(list.querySelectorAll('.demo-row')).map((el) => {
      const rect = el.getBoundingClientRect();
      return {
        top: rect.top - listRect.top,
        bottom: rect.bottom - listRect.top,
        mid: (rect.top + rect.bottom) / 2 - listRect.top
      };
    });
    // Where inside the row the finger grabbed it, so the row does not jump.
    grabOffsetY = startY - listRect.top - (rowBounds[index]?.top ?? 0);
  }

  function moveDrag(y: number): void {
    const list = listEl;
    if (!list || dragIndex === null) {
      return;
    }
    const listTop = list.getBoundingClientRect().top;
    const relY = y - listTop;
    dragDy = relY - grabOffsetY - (rowBounds[dragIndex]?.top ?? relY);
    let position = rowBounds.length;
    for (let i = 0; i < rowBounds.length; i += 1) {
      if (relY < rowBounds[i].mid) {
        position = i;
        break;
      }
    }
    overIndex = position;
    lineTop =
      position >= rowBounds.length ? (rowBounds[rowBounds.length - 1]?.bottom ?? 0) : rowBounds[position].top;
  }

  function endDrag(): void {
    if (dragIndex !== null && overIndex !== null) {
      // Removing the source row shifts later insertion points down by one.
      const target = overIndex > dragIndex ? overIndex - 1 : overIndex;
      moveRow(dragIndex, target);
    }
    dragIndex = null;
    overIndex = null;
    rowBounds = [];
  }

  // ---- sortable list demo ----
  let sortableItems = $state<SortableItem[]>([
    { key: 'front', label: 'Front door' },
    { key: 'gate', label: 'Back gate' },
    { key: 'garage', label: 'Garage' },
    { key: 'porch', label: 'Porch light' }
  ]);

  function reorderSortable(keys: (string | number)[]): void {
    const byKey = new Map(sortableItems.map((item) => [item.key, item]));
    sortableItems = keys
      .map((key) => byKey.get(key))
      .filter((item): item is SortableItem => item !== undefined);
  }
</script>

<div class="gallery" data-gallery-sentinel="GALLERY_DEV_ONLY_SENTINEL">
  <header class="gallery-header">
    <Icon name="mdi:door-open" size={36} />
    <div>
      <h1 class="gallery-title">Component gallery</h1>
      <p class="gallery-sub">Dev only — every ui/ primitive, live, on the real tokens.</p>
    </div>
  </header>

  <!-- Theme -------------------------------------------------------------- -->
  <section class="section" id="theme" aria-labelledby="theme-heading">
    <h2 class="section-heading" id="theme-heading">Theme</h2>
    <p class="note">Dark, light, or follow the OS. The choice persists in localStorage.</p>
    <SegmentedControl
      label="Colour scheme"
      options={[
        { value: 'dark', label: 'Dark' },
        { value: 'light', label: 'Light' },
        { value: 'auto', label: 'Auto' }
      ]}
      bind:value={themeChoice}
    />
    <div class="theme-meta">
      <span class="note">Resolved theme</span>
      <Badge kind="accent" label={theme.resolved} />
    </div>
  </section>

  <!-- Button -------------------------------------------------------------- -->
  <section class="section" id="button" aria-labelledby="button-heading">
    <h2 class="section-heading" id="button-heading">Button</h2>
    <p class="note">
      The only way to trigger an action. primary = the one main action per view, secondary = the
      default, ghost = low-emphasis inline actions, danger = destructive. loading keeps the label
      visible and announces aria-busy. Primary and lg get the 56px target.
    </p>
    <div class="row-demo">
      <Button variant="primary">Primary</Button>
      <Button variant="secondary">Secondary</Button>
      <Button variant="ghost">Ghost</Button>
      <Button variant="danger">Danger</Button>
    </div>
    <div class="row-demo">
      <Button variant="primary" size="lg">Primary lg</Button>
      <Button variant="secondary" size="lg">Secondary lg</Button>
      <Button variant="secondary" disabled>Disabled</Button>
      <Button variant="primary" loading>Saving</Button>
    </div>
    <Button variant="primary" fullWidth>Full width primary</Button>
  </section>

  <!-- IconButton ---------------------------------------------------------- -->
  <section class="section" id="iconbutton" aria-labelledby="iconbutton-heading">
    <h2 class="section-heading" id="iconbutton-heading">IconButton</h2>
    <p class="note">
      Icon-only actions. The label prop is required and becomes the aria-label. Use for chrome
      (close, mute, settings); never for the primary action of a view — that is a Button.
    </p>
    <div class="row-demo">
      <IconButton icon="mdi:close" label="Close" />
      <IconButton icon="mdi:volume-high" label="Unmuted" variant="secondary" />
      <IconButton icon="mdi:volume-off" label="Muted" variant="secondary" disabled />
      <IconButton icon="mdi:alert-circle-outline" label="Delete" variant="danger" />
    </div>
  </section>

  <!-- Icon ---------------------------------------------------------------- -->
  <section class="section" id="icon" aria-labelledby="icon-heading">
    <h2 class="section-heading" id="icon-heading">Icon</h2>
    <p class="note">
      Curated @mdi/js paths, tree-shaken. Unknown names fall back to help-circle-outline instead of
      throwing. Decorative by default; pass label for a meaningful icon.
    </p>
    <div class="row-demo">
      <Icon name="mdi:door-open" size={32} />
      <Icon name="mdi:thermometer" size={32} />
      <Icon name="mdi:lightbulb" size={32} />
      <Icon name="mdi:not-a-real-icon" size={32} label="Fallback for an unknown icon name" />
    </div>
  </section>

  <!-- Input ---------------------------------------------------------------->
  <section class="section" id="input" aria-labelledby="input-heading">
    <h2 class="section-heading" id="input-heading">Input</h2>
    <p class="note">
      Single-line text entry, label above. secret adds a show/hide toggle that never moves focus
      out of the field. On-screen keyboards are tamed with autocapitalize/autocorrect/spellcheck
      off and an inputmode.
    </p>
    <Input label="Door name" bind:value={name} hint="Shown on the dashboard tile." required />
    <Input
      label="Access token"
      bind:value={token}
      secret
      hint="Paste a long-lived Home Assistant token."
      error={token.length < 10 ? 'Token looks too short.' : undefined}
    />
    <Input label="Webhook URL" type="url" inputmode="url" placeholder="https://…" disabled />
  </section>

  <!-- Textarea -------------------------------------------------------------- -->
  <section class="section" id="textarea" aria-labelledby="textarea-heading">
    <h2 class="section-heading" id="textarea-heading">Textarea</h2>
    <p class="note">
      Multi-line entry with the same label/hint/error wiring as Input. Use for notes and JSON
      blobs; if it is one line, use Input.
    </p>
    <Textarea label="Chime behaviour" bind:value={notes} hint="Free text, stored verbatim." />
  </section>

  <!-- Toggle ---------------------------------------------------------------- -->
  <section class="section" id="toggle" aria-labelledby="toggle-heading">
    <h2 class="section-heading" id="toggle-heading">Toggle</h2>
    <p class="note">
      A full-row switch for on/off settings — the whole row is the target. State is the word plus
      the knob position, never colour alone.
    </p>
    <Toggle
      label="Show door name"
      description="Display the name under each camera tile."
      bind:checked={showName}
    />
  </section>

  <!-- Slider ---------------------------------------------------------------- -->
  <section class="section" id="slider" aria-labelledby="slider-heading">
    <h2 class="section-heading" id="slider-heading">Slider</h2>
    <p class="note">
      For a continuous range where the exact number matters less than the feel (volume, brightness).
      Prefer Stepper when the exact integer matters. 32px thumb, 48px tappable height.
    </p>
    <Slider label="Chime volume" bind:value={chimeVolume} unit="%" />
  </section>

  <!-- Stepper -------------------------------------------------------------- -->
  <section class="section" id="stepper" aria-labelledby="stepper-heading">
    <h2 class="section-heading" id="stepper-heading">Stepper</h2>
    <p class="note">
      For exact small integers — never an &lt;input type="number"&gt; (D26). Buttons clamp and
      disable at the bounds. Use Slider for wide, approximate ranges instead.
    </p>
    <Stepper label="Repeat chime" bind:value={repeatCount} min={1} max={5} unit="×" />
  </section>

  <!-- SegmentedControl ------------------------------------------------------ -->
  <section class="section" id="segmentedcontrol" aria-labelledby="segmentedcontrol-heading">
    <h2 class="section-heading" id="segmentedcontrol-heading">SegmentedControl</h2>
    <p class="note">
      For ≤4 mutually exclusive options that should all be visible at once (D26). More than 4
      options belongs in a Select — the control logs a console error if you exceed it. Selected
      state is colour plus a check icon.
    </p>
    <SegmentedControl
      label="Door type"
      options={[
        { value: 'single', label: 'Single' },
        { value: 'double', label: 'Double' },
        { value: 'gate', label: 'Gate' }
      ]}
      bind:value={doorSide}
    />
  </section>

  <!-- Select ---------------------------------------------------------------- -->
  <section class="section" id="select" aria-labelledby="select-heading">
    <h2 class="section-heading" id="select-heading">Select</h2>
    <p class="note">
      For &gt;4 options — never a native &lt;select&gt; (D26). The button opens a picker Sheet of
      56px rows. Keyboard: arrows move, Home/End jump, Enter/Space choose, Escape closes. If you
      have ≤4 options, use SegmentedControl instead.
    </p>
    <Select label="Chime sound" options={chimeOptions} bind:value={chime} />
  </section>

  <!-- Sheet ----------------------------------------------------------------- -->
  <section class="section" id="sheet" aria-labelledby="sheet-heading">
    <h2 class="section-heading" id="sheet-heading">Sheet</h2>
    <p class="note">
      The container for any focused task: full-screen ≤1024px wide, bottom sheet on larger screens
      (D26). Native &lt;dialog&gt;: focus trap, Escape, backdrop tap and focus restore are built
      in. Use Dialog instead when you need confirm/cancel semantics.
    </p>
    <Button variant="secondary" onclick={() => (sheetOpen = true)}>Open sheet</Button>
    <Sheet bind:open={sheetOpen} title="Chime volume" description="One tap from the dashboard.">
      <Slider label="Chime volume" bind:value={chimeVolume} unit="%" />
      <Stepper label="Repeat chime" bind:value={repeatCount} min={1} max={5} unit="×" />
      {#snippet footer()}
        <Button variant="primary" fullWidth onclick={() => (sheetOpen = false)}>Done</Button>
      {/snippet}
    </Sheet>
  </section>

  <!-- Dialog ---------------------------------------------------------------- -->
  <section class="section" id="dialog" aria-labelledby="dialog-heading">
    <h2 class="section-heading" id="dialog-heading">Dialog</h2>
    <p class="note">
      Blocking confirm/cancel — always rendered as a Sheet (D26 forbids centred modals). Use for
      destructive or paid actions; use a plain Sheet for anything that is not a yes/no decision.
      Last result: {lastDialogResult}
    </p>
    <div class="row-demo">
      <Button variant="secondary" onclick={() => (dialogOpen = true)}>Confirm dialog</Button>
      <Button variant="danger" onclick={() => (destructiveOpen = true)}>Delete dialog</Button>
    </div>
    <Dialog
      bind:open={dialogOpen}
      title="Restart doorbell?"
      message="The chime will be silent for about ten seconds while the service restarts."
      confirmLabel="Restart"
      onconfirm={() => (lastDialogResult = 'confirmed')}
      oncancel={() => (lastDialogResult = 'cancelled')}
    />
    <Dialog
      bind:open={destructiveOpen}
      title="Delete Front door?"
      message="This removes the camera tile and its chime mapping. This cannot be undone."
      confirmLabel="Delete"
      destructive
      onconfirm={() => (lastDialogResult = 'deleted')}
      oncancel={() => (lastDialogResult = 'kept')}
    />
  </section>

  <!-- Toast ----------------------------------------------------------------- -->
  <section class="section" id="toast" aria-labelledby="toast-heading">
    <h2 class="section-heading" id="toast-heading">Toast + ToastHost</h2>
    <p class="note">
      Transient feedback through the toastStore (5s default, cleared on dismiss). Never
      hover-dependent and never the only channel for something the user must not miss — use a
      Dialog for that.
    </p>
    <div class="row-demo">
      <Button variant="secondary" onclick={() => toastStore.push('info', 'Doorbell restarted.')}>
        Info toast
      </Button>
      <Button variant="secondary" onclick={() => toastStore.push('success', 'Settings saved.')}>
        Success toast
      </Button>
      <Button variant="secondary" onclick={() => toastStore.push('error', 'Home Assistant disconnected.')}>
        Error toast
      </Button>
    </div>
  </section>

  <!-- Badge ----------------------------------------------------------------- -->
  <section class="section" id="badge" aria-labelledby="badge-heading">
    <h2 class="section-heading" id="badge-heading">Badge</h2>
    <p class="note">
      A status pill: colour plus a word, never colour alone. Non-interactive — for a tappable
      status, use a Button.
    </p>
    <div class="row-demo">
      <Badge kind="neutral" label="Idle" />
      <Badge kind="ok" label="Connected" icon="mdi:check-circle-outline" />
      <Badge kind="warn" label="Volume low" icon="mdi:volume-off" />
      <Badge kind="alert" label="Offline" icon="mdi:alert-circle-outline" />
      <Badge kind="accent" label="Live view" icon="mdi:door-open" />
    </div>
  </section>

  <!-- Card ------------------------------------------------------------------- -->
  <section class="section" id="card" aria-labelledby="card-heading">
    <h2 class="section-heading" id="card-heading">Card</h2>
    <p class="note">
      The default grouping surface. interactive adds a whole-card target with an always-visible
      affordance; keep interactive cards free of nested controls.
    </p>
    <Card title="Front door">
      <p class="demo-text">A plain card groups related content on --surface.</p>
      {#snippet footer()}
        <span class="demo-text">Last chime: 12 minutes ago</span>
      {/snippet}
    </Card>
    <Card title="Garage" interactive onclick={() => toastStore.push('info', 'Card tapped.')}>
      <p class="demo-text">An interactive card — the chevron affordance is always visible.</p>
    </Card>
  </section>

  <!-- Spinner ---------------------------------------------------------------- -->
  <section class="section" id="spinner" aria-labelledby="spinner-heading">
    <h2 class="section-heading" id="spinner-heading">Spinner</h2>
    <p class="note">
      role="status" with a visually-hidden label; under prefers-reduced-motion it renders a static
      ring and the label still announces.
    </p>
    <div class="row-demo">
      <Spinner />
      <Spinner size={48} label="Loading cameras" />
    </div>
  </section>

  <!-- EmptyState -------------------------------------------------------------- -->
  <section class="section" id="emptystate" aria-labelledby="emptystate-heading">
    <h2 class="section-heading" id="emptystate-heading">EmptyState</h2>
    <p class="note">
      For a list or view with nothing to show. Always offer the next action if there is one.
    </p>
    <Card>
      <EmptyState
        icon="mdi:garage"
        title="No cameras yet"
        description="Add a camera entity to see it on the dashboard."
      >
        <Button variant="primary" onclick={() => toastStore.push('info', 'Would open the picker.')}>
          Add a camera
        </Button>
      </EmptyState>
    </Card>
  </section>

  <!-- Drag (longPressDrag) ----------------------------------------------------- -->
  <section class="section" id="drag" aria-labelledby="drag-heading">
    <h2 class="section-heading" id="drag-heading">Reordering (longPressDrag)</h2>
    <p class="note">
      Long-press (400ms) the grab handle to drag; a 1px insertion line shows the drop position.
      The ▲/▼ buttons are the always-visible accessible fallback (D26). NOTE: pointer drag must be
      verified with a finger on the real panel, not only with a desktop mouse.
    </p>
    <div class="drag-list" bind:this={listEl}>
      {#each rows as row, i (row.id)}
        <div
          class="demo-row"
          class:drag-source={dragIndex === i}
          style:transform={dragIndex === i ? `translateY(${dragDy}px)` : undefined}
        >
          <span
            class="grab-handle"
            aria-hidden="true"
            use:longPressDrag={{
              onstart: ({ y }) => startDrag(i, y),
              onmove: ({ y }) => moveDrag(y),
              onend: () => endDrag()
            }}
          >
            <Icon name="mdi:drag-vertical" size={24} />
          </span>
          <span class="demo-row-label">{row.label}</span>
          <span class="demo-row-actions">
            <IconButton
              icon="mdi:chevron-up"
              label={`Move ${row.label} up`}
              disabled={i === 0}
              onclick={() => moveRow(i, i - 1)}
            />
            <IconButton
              icon="mdi:chevron-down"
              label={`Move ${row.label} down`}
              disabled={i === rows.length - 1}
              onclick={() => moveRow(i, i + 1)}
            />
          </span>
        </div>
      {/each}
      {#if dragIndex !== null && overIndex !== null}
        <div class="insertion-line" style:top="{lineTop}px"></div>
      {/if}
    </div>
    <p class="note">Order: {rows.map((row) => row.label).join(' → ')}</p>
  </section>

  <!-- Checkbox ------------------------------------------------------------- -->
  <section class="section" id="checkbox" aria-labelledby="checkbox-heading">
    <h2 class="section-heading" id="checkbox-heading">Checkbox</h2>
    <p class="note">
      For "this option is included" — Toggle is for a setting that is on or off right now. The
      whole row is the 48px target and the box draws a check glyph when checked, so state is never
      colour alone.
    </p>
    <Checkbox
      label="Show battery level"
      description="Adds the battery hint under the state on the tile."
      bind:checked={showBattery}
    />
    <Checkbox label="Disabled option" description="Never selectable." disabled />
    <p class="note">Checked: {showBattery ? 'yes' : 'no'}</p>
  </section>

  <!-- ColorField ----------------------------------------------------------- -->
  <section class="section" id="colorfield" aria-labelledby="colorfield-heading">
    <h2 class="section-heading" id="colorfield-heading">ColorField</h2>
    <p class="note">
      Eight presets, a custom hex field that only appears when asked for (or when the stored value
      is not a preset), and a clear button for "use the theme". The value shown next to the label
      is the last valid choice, so an invalid hex never silently changes it.
    </p>
    <ColorField
      label="Tile accent"
      bind:value={accent}
      hint="The tile's border and icon colour."
    />
    <p class="note">Stored value: {accent ?? 'none'}</p>
  </section>

  <!-- SortableList --------------------------------------------------------- -->
  <section class="section" id="sortablelist" aria-labelledby="sortablelist-heading">
    <h2 class="section-heading" id="sortablelist-heading">SortableList</h2>
    <p class="note">
      The shared reorder list: one onreorder call per gesture, ▲/▼ as the accessible path and a
      long-press drag on the handle as the enhancement. The row snippet receives the original
      index into items, not the position it happens to occupy mid-drag.
    </p>
    <SortableList items={sortableItems} onreorder={reorderSortable}>
      {#snippet row(index)}
        <span class="demo-row-label">{sortableItems[index].label}</span>
      {/snippet}
    </SortableList>
    <p class="note">Order: {sortableItems.map((item) => item.label).join(' → ')}</p>
  </section>

  <ToastHost />
</div>

<style>
  .gallery {
    width: min(100%, 1120px);
    margin-inline: auto;
    padding: clamp(var(--gap), 3vw, calc(var(--gap) * 2));
    display: flex;
    flex-direction: column;
    gap: calc(var(--gap) * 2);
  }

  .gallery-header {
    display: flex;
    align-items: center;
    gap: var(--gap);
    padding-bottom: var(--gap);
    border-bottom: var(--border-width) solid var(--border);
    color: var(--accent);
  }

  .gallery-title {
    margin: 0;
    font-size: var(--text-state);
    line-height: 1.2;
    font-weight: 700;
    color: var(--text);
  }

  .gallery-sub {
    margin: var(--gap-sm) 0 0;
    font-size: var(--text-body);
    color: var(--text-muted);
  }

  .theme-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .section {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: var(--gap);
    padding-top: var(--gap);
    border-top: var(--border-width) solid var(--border);
  }

  .section-heading {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.25;
    font-weight: 700;
  }

  .note {
    margin: 0;
    max-width: 76ch;
    font-size: var(--text-body);
    line-height: 1.5;
    color: var(--text-muted);
  }

  .row-demo {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap);
  }

  .demo-text {
    margin: 0;
  }

  .drag-list {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .demo-row {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
  }

  .demo-row.drag-source {
    position: relative;
    z-index: 1;
    background: var(--surface-hover);
    box-shadow: var(--shadow-sheet);
  }

  .grab-handle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--tap);
    height: var(--tap);
    flex-shrink: 0;
    color: var(--text-muted);
    /* The browser must not claim the gesture for scrolling (see drag.ts). */
    touch-action: none;
    cursor: grab;
  }

  .demo-row-label {
    flex: 1;
    min-width: 0;
    font-size: var(--text-body);
  }

  .demo-row-actions {
    display: flex;
    gap: var(--gap-sm);
  }

  .insertion-line {
    position: absolute;
    inset-inline: 0;
    height: 1px;
    background: var(--accent);
    pointer-events: none;
  }
</style>
