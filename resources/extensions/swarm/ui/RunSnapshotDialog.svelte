<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';

  // Shows the Swarm (profile) the selected Run started from.
  let { model } = $props();
</script>

{#if model.profileSnapshotOpen}<Modal
    title={t('swarm.profileSnapshot')}
    onClose={() => (model.profileSnapshotOpen = false)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <pre>{JSON.stringify(
            model.selectedSwarm.profile_snapshot ??
              model.selectedSwarm.profile ??
              {},
            null,
            2,
          )}</pre>
      </div>{/snippet}{#snippet footer()}<Button
        variant="primary"
        onClick={() => (model.profileSnapshotOpen = false)}
        >{t('common.close')}</Button
      >{/snippet}</Modal
  >{/if}
