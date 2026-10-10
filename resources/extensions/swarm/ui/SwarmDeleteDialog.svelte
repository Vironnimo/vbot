<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';

  // Confirms deleting the Swarm (profile) in `model.profileDeleteCandidate`.
  let { model } = $props();
</script>

{#if model.profileDeleteCandidate}<Modal
    title={t('swarm.delete.title')}
    closeDisabled={model.pending === 'delete'}
    onClose={() => (model.profileDeleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>
          {t('swarm.delete.body', { name: model.profileDeleteCandidate.name })}
        </p>
        {#if model.deleteError}<Banner variant="error"
            >{model.deleteError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'delete'}
        onClick={() => (model.profileDeleteCandidate = null)}
        >{t('common.cancel')}</Button
      ><Button
        variant="danger"
        loading={model.pending === 'delete'}
        onClick={model.deleteProfile}>{t('common.delete')}</Button
      >{/snippet}</Modal
  >{/if}
