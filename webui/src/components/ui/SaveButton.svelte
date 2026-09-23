<script>
  // The manual Save action that accompanies autosaved editors. It runs the
  // same tracked save as autosave and states where the draft stands: "Save"
  // while edits are pending, "Saving…" while a write is in flight, and a quiet
  // "Saved" once the persisted value matches the draft. It stays clickable in
  // every state so a manual save can still confirm an already-saved draft.
  import { t } from '$lib/i18n.js';
  import Button from './Button.svelte';

  let {
    saving = false,
    pending = true,
    onClick,
    class: className = '',
    ...rest
  } = $props();

  const saved = $derived(!saving && !pending);
</script>

<Button
  {...rest}
  variant="tertiary"
  class={`save-button ${saved ? 'save-button--saved' : ''} ${className}`}
  {onClick}
>
  {#if saving}
    {t('common.saving', 'Saving…')}
  {:else if saved}
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
    {t('common.saved', 'Saved')}
  {:else}
    {t('common.save', 'Save')}
  {/if}
</Button>
