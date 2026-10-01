<script>
  // The archive choice inside a delete dialog: while the item moves to the
  // Archive, when the Archive deletes it; and the option to delete it
  // permanently right away instead. The dialog owns the choice and switches
  // its own wording and confirm label with it.
  import Checkbox from '../ui/Checkbox.svelte';
  import { archiveDeletionNotice } from '$lib/archiveRetention.svelte.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let { permanent = false, disabled = false, onChange = noop } = $props();
</script>

<div class="archive-delete-option">
  {#if !permanent}
    <p class="archive-delete-option__notice">
      {archiveDeletionNotice()}
    </p>
  {/if}
  <Checkbox checked={permanent} {disabled} {onChange}
    >{t('archive.deleteOption.permanent')}</Checkbox
  >
</div>

<style>
  .archive-delete-option {
    display: grid;
    gap: 10px;
    margin-top: 12px;
  }

  .archive-delete-option__notice {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }
</style>
