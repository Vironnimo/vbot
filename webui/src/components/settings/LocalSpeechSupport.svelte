<script>
  import { onDestroy } from 'svelte';
  import { createLocalSetupJob } from './localSetupJob.svelte.js';
  import Banner from '../ui/Banner.svelte';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { restartAfterLocalSpeechSetup, previewSpeech } from '$lib/api.js';
  import { t, tOr } from '$lib/i18n.js';

  const componentId = $props.id();
  let {
    target,
    tts = false,
    taskSurfaceBusy = false,
    onReady = () => {},
  } = $props();
  let destroyed = false;
  let previewText = $state(t('settings.localSpeech.previewText'));
  let previewBusy = $state(false);
  let previewError = $state('');
  let previewAudio = $state(null);
  let previewProgress = $state({ phase: 'preparing', elapsed_seconds: 0 });
  let previewController = null;
  const job = createLocalSetupJob({
    getTarget: () => target,
    onReady: () => onReady(),
    restart: (setupTarget) =>
      restartAfterLocalSpeechSetup({ target: setupTarget }),
  });
  let localSetup = $derived(job.status);
  let localSetupError = $derived(job.error);
  let setupState = $derived(job.state);

  async function playPreview() {
    if (previewBusy || taskSurfaceBusy || !previewText.trim()) return;
    previewController = new AbortController();
    previewBusy = true;
    previewError = '';
    previewAudio = null;
    previewProgress = { phase: 'queued', elapsed_seconds: 0 };
    try {
      const result = await previewSpeech(previewText, {
        signal: previewController.signal,
        onProgress: (progress) => {
          if (!destroyed) previewProgress = progress;
        },
      });
      if (!destroyed) previewAudio = result.url;
    } catch (error) {
      if (!destroyed && !previewController.signal.aborted)
        previewError =
          error?.message || t('settings.localSpeech.previewFailed');
    } finally {
      if (!destroyed) previewBusy = false;
    }
  }
  onDestroy(() => {
    destroyed = true;
    previewController?.abort();
  });

  function restartLocalSpeechServer() {
    if (!taskSurfaceBusy) void job.restartServer();
  }
</script>

<Banner
  variant={localSetupError || setupState === 'failed' ? 'warn' : 'neutral'}
>
  <div role="status" aria-live="polite">
    {#if localSetupError}
      {tOr(
        `settings.localSpeech.error.${localSetupError}`,
        t('settings.localSpeech.error.install_failed'),
      )}
    {:else if setupState === 'failed'}
      {tOr(
        `settings.localSpeech.error.${localSetup.error}`,
        t('settings.localSpeech.error.install_failed'),
      )}
    {:else if setupState === 'installing'}
      {tOr(
        `settings.localSpeech.phase.${localSetup.phase}`,
        t('settings.localSpeech.phase.installing'),
      )}
    {:else if setupState === 'ready'}
      {tts
        ? t('settings.localSpeech.ttsReady')
        : t('settings.localSpeech.ready')}
    {:else if setupState === 'restart_required' && !localSetup.restart_available}
      {t('settings.localSpeech.error.restart_unavailable')}
    {:else}
      {tts && setupState === 'missing'
        ? t('settings.localSpeech.ttsMissing')
        : t(`settings.localSpeech.state.${setupState}`)}
    {/if}
  </div>
  {#if localSetupError === 'connection' || localSetupError === 'restart_timeout'}
    <Button onClick={job.refresh}>{t('settings.localSpeech.checkAgain')}</Button
    >
  {:else if setupState === 'missing' || setupState === 'failed'}
    <Button variant="primary" loading={job.acting} onClick={job.install}>
      {setupState === 'failed'
        ? t('settings.localSpeech.retry')
        : t('settings.localSpeech.installButton')}
    </Button>
  {:else if setupState === 'restart_required'}
    <Button
      variant="primary"
      loading={job.acting}
      disabled={taskSurfaceBusy || !localSetup.restart_available}
      onClick={restartLocalSpeechServer}
    >
      {t('settings.localSpeech.restartButton')}
    </Button>
  {:else if setupState === 'installing' || setupState === 'restarting'}
    <Button loading
      >{setupState === 'installing'
        ? t('settings.localSpeech.installingButton')
        : t('settings.localSpeech.restartingButton')}</Button
    >
  {/if}
</Banner>

{#if tts && setupState === 'ready'}
  <div class="speech-preview">
    <FormField
      controlId={`${componentId}-preview`}
      label={t('settings.localSpeech.previewLabel')}
    >
      {#snippet children(field)}
        <TextArea
          id={field.controlId}
          value={previewText}
          onInput={(value) => (previewText = value)}
          rows={2}
          maxlength={5000}
          disabled={previewBusy}
        />
      {/snippet}
    </FormField>
    <div class="speech-preview-actions">
      <Button
        onClick={playPreview}
        loading={previewBusy}
        disabled={taskSurfaceBusy || !previewText.trim()}
        >{t('settings.localSpeech.previewButton')}</Button
      >
      {#if previewBusy}
        <span role="status" aria-live="polite">
          {tOr(
            `chat.voice.progress.${previewProgress.phase}`,
            t('chat.voice.progress.synthesizing'),
          )}
          · {previewProgress.elapsed_seconds ?? 0}s
        </span>
        <Button onClick={() => previewController?.abort()}
          >{t('settings.localSpeech.cancelPreview')}</Button
        >
      {/if}
    </div>
    {#if previewError}<Banner variant="warn"
        ><span role="alert">{previewError}</span></Banner
      >{/if}
    {#if previewAudio}<AudioPlayer
        src={previewAudio}
        ariaLabel={t('settings.localSpeech.previewAudio')}
      />{/if}
  </div>
{/if}

<style>
  .speech-preview {
    display: grid;
    gap: var(--space-sm);
  }
  .speech-preview-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }
  .speech-preview-actions [role='status'] {
    flex: 1 1 260px;
    font-size: var(--fs-mono-body);
    line-height: 1.45;
  }
</style>
