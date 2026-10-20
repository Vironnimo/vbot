<script>
  import { t } from '$lib/i18n.js';
  import Modal from '../components/ui/Modal.svelte';
  import Button from '../components/ui/Button.svelte';

  let { transition } = $props();
</script>

{#if transition.prompt === 'slow'}
  <Modal
    title={t('autosave.stillSavingTitle')}
    labelledById="autosave-transition-slow-title"
    size="sm"
    onClose={transition.keepWaiting}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {t('autosave.stillSavingBody')}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={transition.leave}>
        {t('autosave.leaveAnyway')}
      </Button>
      <Button variant="primary" onClick={transition.keepWaiting}>
        {t('autosave.keepWaiting')}
      </Button>
    {/snippet}
  </Modal>
{:else if transition.prompt === 'failed'}
  <!-- Closing it stays with the unsaved edits; Discard stays available
       while a Retry runs, so a hanging save never locks the app. -->
  <Modal
    title={t('autosave.transitionFailureTitle')}
    labelledById="autosave-transition-failure-title"
    size="sm"
    onClose={transition.stay}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {t('autosave.transitionFailureBody')}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={transition.leave}>
        {t('autosave.discardAndContinue')}
      </Button>
      <Button
        variant="primary"
        disabled={transition.saving}
        onClick={transition.retry}
      >
        {transition.saving ? t('common.saving') : t('common.retry')}
      </Button>
    {/snippet}
  </Modal>
{/if}
