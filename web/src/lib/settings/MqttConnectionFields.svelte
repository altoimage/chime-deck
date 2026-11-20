<!--
  The MQTT field group shared by the settings Connection panel and the
  first-run wizard (HSD-9xpm): the broker host and port, the username, the
  write-only password, the "Clear stored password" control and the TLS switch.

  Both screens own their draft and pass these values with `bind:`; this
  component only decides the labels, hints and order, so the panel and the
  wizard can never show the same setting two different ways. The wizard's step 2
  asks for `mqtt.username`/`mqtt.password`, which is why the credentials live
  here rather than in the panel alone. The password starts EMPTY wherever it is
  rendered — `ui/Input` carries the show/hide control, and the backend never
  returns a secret, so there is no stored value to render.

  The Username hint is the one deliberate addition: step 2 asks for credentials,
  and a broker that allows anonymous connections needs none, so the field has to
  say out loud that it may stay blank.
-->
<script lang="ts">
  import Button from '../ui/Button.svelte';
  import Input from '../ui/Input.svelte';
  import Toggle from '../ui/Toggle.svelte';

  interface Props {
    host?: string;
    port?: string;
    username?: string;
    password?: string;
    tls?: boolean;
    portError?: string;
    /** From `connection-form.passwordHint`. */
    passwordHint: string;
    /** Whether a password is stored, and so clearable. */
    canClearPassword?: boolean;
    /** Whether a clear is already pending (the button then explains itself). */
    cleared?: boolean;
    onclear?: () => void;
  }

  let {
    host = $bindable(''),
    port = $bindable(''),
    username = $bindable(''),
    password = $bindable(''),
    tls = $bindable(false),
    portError,
    passwordHint,
    canClearPassword = false,
    cleared = false,
    onclear = () => {}
  }: Props = $props();
</script>

<Input
  label="Broker host"
  bind:value={host}
  hint="The host name or address of the broker."
/>
<Input
  label="Broker port"
  inputmode="numeric"
  bind:value={port}
  hint="The broker's TCP port, usually 1883."
  error={portError}
/>
<Input
  label="Username"
  bind:value={username}
  hint="Leave empty if the broker allows anonymous connections."
  autocomplete="off"
/>
<Input
  label="Password"
  secret
  bind:value={password}
  hint={passwordHint}
  autocomplete="new-password"
/>

{#if canClearPassword}
  <div class="actions">
    <Button variant="danger" disabled={cleared} onclick={onclear}>Clear stored password</Button>
  </div>
{/if}

<Toggle label="Use TLS" description="Encrypt the connection to the broker." bind:checked={tls} />

<style>
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }
</style>
