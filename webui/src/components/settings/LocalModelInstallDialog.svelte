<script>
  // The one-time installation of a local Model that vBot runs itself,
  // offered when the Model is chosen before it is installed: what the Model
  // is and what installing downloads, the Install action, the running
  // installation with its download progress, and failures with the action
  // that helps next. Closing the dialog leaves a running installation
  // running; `onInstalled` runs once the Model is ready, also when it was
  // already installed when the dialog opened.
  import { createLocalSetupJob } from './localSetupJob.svelte.js';
  import Button from '../ui/Button.svelte';
  import Modal from '../ui/Modal.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';
  import { t } from '$lib/i18n.js';
  import { describeLocalModelSetup } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    target,
    label,
    // What the Model is good for, from its recommendation.
    note = '',
    // What installing fetches: "347 MB download · Apache-2.0 license".
    download = '',
    onInstalled = noop,
    onClose = noop,
  } = $props();

  const job = createLocalSetupJob({
    getTarget: () => target,
    onReady: () => onInstalled(),
  });
  let view = $derived(describeLocalModelSetup(job));
  let installing = $derived(view.action === 'installing');
</script>

<Modal
  title={t('settings.localModel.dialogTitle', { model: label })}
  class="local-model-dialog"
  {onClose}
>
  {#snippet body()}
    <div class="modal-body local-model-dialog__body">
      <p>{t('settings.localModel.dialogIntro')}</p>
      {#if note}
        <p class="local-model-dialog__note">{note}</p>
      {/if}
      {#if download}
        <p class="local-model-dialog__facts" data-local-install-download>
          {download}
        </p>
      {/if}
      <div
        class="local-model-dialog__status"
        class:local-model-dialog__status--warn={view.tone === 'warn'}
        role="status"
        aria-live="polite"
        hidden={view.action === 'install'}
        data-local-install={job.error ? 'error' : job.state}
      >
        <span>{view.message}</span>
        {#if view.progress}
          <ProgressBar
            label={t('settings.localModel.progressLabel')}
            percent={view.progress.percent}
            text={view.progress.text}
          />
        {/if}
      </div>
    </div>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" onClick={onClose}>
      {installing ? t('common.close') : t('common.cancel')}
    </Button>
    {#if view.action === 'install' || view.action === 'retry'}
      <Button variant="primary" loading={job.acting} onClick={job.install}>
        {view.action === 'retry'
          ? t('settings.localModel.retry')
          : t('settings.localModel.install')}
      </Button>
    {:else if installing}
      <Button variant="primary" loading
        >{t('settings.localModel.installing')}</Button
      >
    {:else if view.action === 'check'}
      <Button variant="primary" onClick={job.refresh}
        >{t('settings.localModel.checkAgain')}</Button
      >
    {/if}
  {/snippet}
</Modal>

<style>
  .local-model-dialog__body {
    display: grid;
    gap: 10px;
  }

  .local-model-dialog__body p {
    margin: 0;
  }

  .local-model-dialog__note {
    color: var(--text-med);
  }

  .local-model-dialog__facts {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }

  .local-model-dialog__status {
    display: grid;
    gap: 6px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    line-height: 1.45;
  }

  .local-model-dialog__status[hidden] {
    display: none;
  }

  .local-model-dialog__status--warn > span:first-child {
    color: var(--amber);
  }
</style>
