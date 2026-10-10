<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';

  // Confirms deleting the Run in `model.swarmDeleteCandidate`.
  let { model } = $props();
</script>

{#if model.swarmDeleteCandidate}<Modal
    title={t('swarm.deleteRun.title')}
    closeDisabled={model.pending === 'delete'}
    onClose={() => (model.swarmDeleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>{model.swarmDeleteCandidate.prompt}</p>
        <p>
          {t('swarm.deleteRun.body')}
        </p>
        {#if model.deleteError}<Banner variant="error"
            >{model.deleteError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'delete'}
        onClick={() => (model.swarmDeleteCandidate = null)}
        >{t('common.cancel')}</Button
      ><Button
        variant="danger"
        loading={model.pending === 'delete'}
        onClick={model.deleteSwarm}>{t('common.delete')}</Button
      >{/snippet}</Modal
  >{/if}
