<script>
  // One value field of a Secret form (Settings -> Secrets, the Chat request
  // card, the composer's `/secret` form). A concealed field is masked while
  // typed: a password input, or a masked text area for a multi-line text
  // value; Show unmasks it until Hide. The value lives only in the caller's
  // form state and is never autocompleted or spell-checked.
  import Button from '../ui/Button.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    id,
    value = '',
    onInput = noop,
    concealed = true,
    multiline = false,
    variant = 'modal',
    invalid = false,
    disabled = false,
    describedBy = undefined,
    placeholder = '',
  } = $props();

  let shown = $state(false);
  const masked = $derived(concealed && !shown);
</script>

<div class="secret-field-input">
  {#if multiline}
    <TextArea
      {id}
      {value}
      {onInput}
      {invalid}
      {disabled}
      {placeholder}
      code
      autoGrow
      rows={3}
      class={masked ? 'secret-field-input__masked' : ''}
      autocomplete="off"
      spellcheck="false"
      aria-describedby={describedBy}
    />
  {:else}
    <TextField
      {id}
      {value}
      {onInput}
      {variant}
      {invalid}
      {disabled}
      {placeholder}
      type={masked ? 'password' : 'text'}
      autocomplete="off"
      spellcheck="false"
      aria-describedby={describedBy}
    />
  {/if}
  {#if concealed}
    <Button
      variant="tertiary"
      class="secret-field-input__toggle"
      {disabled}
      aria-pressed={shown}
      onClick={() => (shown = !shown)}
    >
      {shown ? t('secrets.input.hide') : t('secrets.input.show')}
    </Button>
  {/if}
</div>

<style>
  .secret-field-input {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    min-width: 0;
  }

  .secret-field-input > :global(:first-child) {
    flex: 1 1 auto;
    min-width: 0;
  }

  .secret-field-input :global(.secret-field-input__masked) {
    -webkit-text-security: disc;
  }

  .secret-field-input :global(.secret-field-input__toggle) {
    flex-shrink: 0;
  }
</style>
