<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import { profileSummary } from './runPresentation.js';
  import './runDialogs.css';

  // Confirms deleting the Swarm (profile) in `model.runActions.profileDeleteCandidate`,
  // named with its size and Models. The dialog stays open while the deletion
  // runs and shows its failure for another try.
  let { model } = $props();
</script>

{#if model.runActions.profileDeleteCandidate}<ConfirmDialog
    title={t('swarm.delete.title')}
    body={t('swarm.delete.body', {
      name: model.runActions.profileDeleteCandidate.name,
    })}
    confirmLabel={t('common.delete')}
    busy={model.pending === 'delete'}
    error={model.runActions.deleteError}
    onConfirm={model.runActions.deleteProfile}
    onCancel={model.runActions.cancelDelete}
    >{#snippet bodyExtra()}<p class="swarm-dialog__meta">
        {profileSummary(model.runActions.profileDeleteCandidate)}
      </p>{/snippet}</ConfirmDialog
  >{/if}
