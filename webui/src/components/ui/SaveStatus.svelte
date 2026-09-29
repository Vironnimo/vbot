<script>
  // The save state beside an autosaved editor. At rest it shows nothing: the
  // editor saves on its own. An unsaved draft shows a small Save action that
  // writes it now; it is also the way to save a draft autosave does not write
  // (a failed save, or a change the editor saves only on request). A write in
  // flight reads "Saving…", and a successful write shows a check and "Saved"
  // briefly before it fades. The text is a status live region, so assistive
  // technology announces it; the root keeps the action's height, so a state
  // change never shifts the layout.
  import { onDestroy, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import Button from './Button.svelte';

  // How long "Saved" stays, its fade included (the CSS animation's duration).
  const SAVED_FEEDBACK_MS = 2400;

  let {
    saving = false,
    pending = false,
    onClick,
    class: className = '',
    ...rest
  } = $props();

  let root = $state(null);
  let statusRegion = $state(null);
  let savedVisible = $state(false);
  let savedTimer = null;
  let sawSaving = false;

  const actionVisible = $derived(pending && !saving);

  // "Saved" confirms a write this control watched finish with nothing left
  // pending; a failed write or newer edits bring the Save action back.
  $effect(() => {
    const isSaving = saving;
    const isPending = pending;
    untrack(() => {
      if (isSaving) {
        sawSaving = true;
        hideSaved();
        return;
      }
      const succeeded = sawSaving && !isPending;
      sawSaving = false;
      if (succeeded) showSaved();
      else if (isPending) hideSaved();
    });
  });

  // Removing the focused Save action would drop keyboard focus to the page
  // start; the status region keeps it in place instead.
  $effect.pre(() => {
    if (actionVisible) return;
    untrack(() => {
      const focused = root?.ownerDocument.activeElement;
      if (focused && focused !== statusRegion && root.contains(focused)) {
        statusRegion?.focus({ preventScroll: true });
      }
    });
  });

  function showSaved() {
    clearTimeout(savedTimer);
    savedVisible = true;
    savedTimer = setTimeout(hideSaved, SAVED_FEEDBACK_MS);
  }

  function hideSaved() {
    clearTimeout(savedTimer);
    savedTimer = null;
    savedVisible = false;
  }

  onDestroy(() => clearTimeout(savedTimer));
</script>

<span class={`save-status ${className}`.trim()} bind:this={root}>
  {#if actionVisible}
    <Button {...rest} variant="tertiary" {onClick}>{t('common.save')}</Button>
  {/if}
  <span
    class="save-status__state"
    role="status"
    tabindex="-1"
    bind:this={statusRegion}
  >
    {#if saving}
      <span class="save-status__text">{t('common.saving')}</span>
    {:else if savedVisible}
      <span
        class="save-status__text save-status__text--saved"
        style:animation-duration={`${SAVED_FEEDBACK_MS}ms`}
      >
        <svg
          class="save-status__check"
          viewBox="0 0 16 16"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <path d="m3.5 8.5 3 3 6-7" />
        </svg>
        {t('common.saved')}
      </span>
    {/if}
  </span>
</span>
