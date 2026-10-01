<script>
  // The install row under a local Model that is not set up yet: what
  // installing downloads, the Install action, the running installation with
  // its download progress, and failures with the action that helps next.
  // `onReady` runs once the installation reports the Model ready.
  import { createLocalSetupJob } from './localSetupJob.svelte.js';
  import Button from '../ui/Button.svelte';
  import { t } from '$lib/i18n.js';
  import { describeLocalModelSetup } from '$lib/settingsView.js';

  let {
    target,
    // What installing fetches: "347 MB download · Apache-2.0 license".
    download = '',
    onReady = () => {},
  } = $props();

  const job = createLocalSetupJob({
    getTarget: () => target,
    onReady: () => onReady(),
  });
  let view = $derived(describeLocalModelSetup(job));
</script>

<div
  class="local-model-install"
  class:local-model-install--warn={view.tone === 'warn'}
  data-local-install={job.error ? 'error' : job.state}
>
  <div class="local-model-install__line">
    <div class="local-model-install__text" role="status" aria-live="polite">
      <span>{view.message}</span>
      {#if view.action === 'install' && download}
        <span class="local-model-install__facts">{download}</span>
      {:else if view.progress}
        <span class="local-model-install__facts">{view.progress.text}</span>
      {/if}
    </div>
    {#if view.action === 'install' || view.action === 'retry'}
      <Button loading={job.acting} onClick={job.install}>
        {view.action === 'retry'
          ? t('settings.localModel.retry')
          : t('settings.localModel.install')}
      </Button>
    {:else if view.action === 'installing'}
      <Button loading>{t('settings.localModel.installing')}</Button>
    {:else if view.action === 'check'}
      <Button onClick={job.refresh}
        >{t('settings.localModel.checkAgain')}</Button
      >
    {/if}
  </div>
  {#if view.progress}
    <div
      class="local-model-install__bar"
      role="progressbar"
      aria-label={t('settings.localModel.progressLabel')}
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={view.progress.percent}
      aria-valuetext={view.progress.text}
    >
      <span style:width={`${view.progress.percent}%`}></span>
    </div>
  {/if}
</div>

<style>
  .local-model-install {
    display: grid;
    gap: 6px;
    max-width: 64ch;
  }

  .local-model-install__line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 12px;
  }

  .local-model-install__text {
    display: flex;
    min-width: 0;
    flex: 1 1 220px;
    flex-direction: column;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    line-height: 1.45;
  }

  .local-model-install--warn .local-model-install__text > span:first-child {
    color: var(--amber);
  }

  .local-model-install__facts {
    color: var(--text-lo);
    font-variant-numeric: tabular-nums;
  }

  .local-model-install__bar {
    height: 4px;
    overflow: hidden;
    border-radius: 2px;
    background: var(--border);
  }

  .local-model-install__bar > span {
    display: block;
    height: 100%;
    background: var(--accent);
    transition: width 0.4s ease;
  }
</style>
