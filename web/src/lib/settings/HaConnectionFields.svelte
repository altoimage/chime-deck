<!--
  The Home Assistant field group shared by the settings Connection panel and
  the first-run wizard (HSD-vl7t): the URL field, the write-only token field and
  the "Clear stored token" control.

  Both screens own their draft and pass these two values with `bind:`; this
  component only decides the labels, hints and order, so the panel and the
  wizard can never show the same setting two different ways. The token starts
  EMPTY wherever it is rendered — `ui/Input` carries the show/hide control, and
  the backend never returns a secret, so there is no stored value to render.
-->
<script lang="ts">
  import Button from '../ui/Button.svelte';
  import Input from '../ui/Input.svelte';

  interface Props {
    baseUrl?: string;
    token?: string;
    /** The sentence under the token field, from `connection-form.tokenHint`. */
    tokenHint: string;
    baseUrlError?: string;
    /** Whether a token is stored, and so clearable. */
    canClearToken?: boolean;
    /** Whether a clear is already pending (the button then explains itself). */
    cleared?: boolean;
    onclear?: () => void;
  }

  let {
    baseUrl = $bindable(''),
    token = $bindable(''),
    tokenHint,
    baseUrlError,
    canClearToken = false,
    cleared = false,
    onclear = () => {}
  }: Props = $props();
</script>

<Input
  label="Home Assistant URL"
  type="url"
  bind:value={baseUrl}
  hint="The address of your Home Assistant instance."
  error={baseUrlError}
/>

<Input
  label="Long-lived token"
  secret
  bind:value={token}
  hint={tokenHint}
  autocomplete="new-password"
/>

{#if canClearToken}
  <div class="actions">
    <Button variant="danger" disabled={cleared} onclick={onclear}>Clear stored token</Button>
  </div>
{/if}

<style>
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }
</style>
