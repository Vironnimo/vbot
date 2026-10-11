<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import { runTarget, runTitle } from './runPresentation.js';
  import './runDialogs.css';

  // Confirms deleting the Run in `model.swarmDeleteCandidate`, named by its
  // title, start and size. The dialog stays open while the deletion runs and
  // shows its failure for another try.
  let { model } = $props();
</script>

{#if model.swarmDeleteCandidate}<ConfirmDialog
    title={t('swarm.deleteRun.title')}
    body={t('swarm.deleteRun.body', {
      title: runTitle(model.swarmDeleteCandidate),
    })}
    confirmLabel={t('common.delete')}
    busy={model.pending === 'delete'}
    error={model.deleteError}
    onConfirm={model.deleteSwarm}
    onCancel={model.cancelDelete}
    >{#snippet bodyExtra()}<p class="swarm-dialog__meta">
        {runTarget(model.swarmDeleteCandidate, model.now)}
      </p>{/snippet}</ConfirmDialog
  >{/if}
