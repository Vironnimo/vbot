<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import { runTarget, runTitle } from './runPresentation.js';
  import './runDialogs.css';

  // Confirms deleting the Run in `model.runActions.swarmDeleteCandidate`, named by its
  // title, start and size. The dialog stays open while the deletion runs and
  // shows its failure for another try.
  let { model } = $props();
</script>

{#if model.runActions.swarmDeleteCandidate}<ConfirmDialog
    title={t('swarm.deleteRun.title', {
      title: runTitle(model.runActions.swarmDeleteCandidate),
    })}
    body={t('swarm.deleteRun.body')}
    confirmLabel={t('common.delete')}
    busy={model.pending === 'delete'}
    error={model.runActions.deleteError}
    onConfirm={model.runActions.deleteSwarm}
    onCancel={model.runActions.cancelDelete}
    >{#snippet bodyExtra()}<p class="swarm-dialog__meta">
        {runTarget(model.runActions.swarmDeleteCandidate, model.now)}
      </p>{/snippet}</ConfirmDialog
  >{/if}
