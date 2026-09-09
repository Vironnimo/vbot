<script>
  import { onMount, onDestroy } from 'svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import {
    getLocalSpeechSetup,
    installLocalSpeechSupport,
    restartAfterLocalSpeechSetup,
    previewSpeech,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

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
  let setupState = $derived(localSetup?.state ?? 'checking');

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
  let localSetup = $state(null);
  let localSetupError = $state('');
  let localSetupAction = $state(false);
  let restartStartedAt = 0;
  let setupTimer = null;
  let setupRequestId = 0;
  onMount(() => {
    void refreshLocalSetup();
  });
  onDestroy(() => {
    destroyed = true;
    clearTimeout(setupTimer);
    setupRequestId += 1;
    previewController?.abort();
  });

  function scheduleSetupRefresh() {
    clearTimeout(setupTimer);
    if (!destroyed) {
      setupTimer = setTimeout(() => void refreshLocalSetup(), 1500);
    }
  }

  async function refreshLocalSetup() {
    const requestId = ++setupRequestId;
    try {
      const next = await getLocalSpeechSetup({ target });
      if (destroyed || requestId !== setupRequestId) return;
      localSetupError = '';
      if (restartStartedAt && next.state !== 'ready') {
        localSetup = { ...next, state: 'restarting' };
      } else {
        localSetup = next;
      }
      if (next.state === 'ready') {
        restartStartedAt = 0;
        await onReady();
      }
    } catch {
      if (destroyed || requestId !== setupRequestId) return;
      if (!restartStartedAt) localSetupError = 'connection';
    }
    if (restartStartedAt && Date.now() - restartStartedAt > 90_000) {
      localSetupError = 'restart_timeout';
      return;
    }
    if (restartStartedAt || localSetup?.state === 'installing')
      scheduleSetupRefresh();
  }

  async function installLocalSpeech() {
    if (localSetupAction || localSetup?.state === 'installing') return;
    localSetupAction = true;
    localSetupError = '';
    clearTimeout(setupTimer);
    setupRequestId += 1;
    try {
      const next = await installLocalSpeechSupport({ target });
      if (destroyed) return;
      localSetup = next;
      scheduleSetupRefresh();
    } catch {
      if (!destroyed) {
        localSetupError = 'connection';
        scheduleSetupRefresh();
      }
    } finally {
      if (!destroyed) localSetupAction = false;
    }
  }

  async function restartLocalSpeechServer() {
    if (localSetupAction || taskSurfaceBusy) return;
    localSetupAction = true;
    localSetupError = '';
    setupRequestId += 1;
    restartStartedAt = Date.now();
    try {
      const result = await restartAfterLocalSpeechSetup({ target });
      if (destroyed) return;
      if (result.state !== 'restarting') {
        restartStartedAt = 0;
        localSetupError = result.error || 'restart_unavailable';
      } else {
        localSetup = { ...localSetup, state: 'restarting' };
      }
    } catch {
      // The response may have been interrupted by the requested restart.
      // Inspect status, never automatically repeat the restart mutation.
      if (!destroyed) localSetup = { ...localSetup, state: 'restarting' };
    } finally {
      if (!destroyed) {
        localSetupAction = false;
        scheduleSetupRefresh();
      }
    }
  }
</script>

<Banner
  variant={localSetupError || setupState === 'failed' ? 'warn' : 'neutral'}
>
  <div role="status" aria-live="polite">
    {#if localSetupError}
      {t(
        `settings.localSpeech.error.${localSetupError}`,
        t('settings.localSpeech.error.install_failed'),
      )}
    {:else if setupState === 'failed'}
      {t(
        `settings.localSpeech.error.${localSetup.error}`,
        t('settings.localSpeech.error.install_failed'),
      )}
    {:else if setupState === 'installing'}
      {t(
        `settings.localSpeech.phase.${localSetup.phase}`,
        t('settings.localSpeech.phase.installing'),
      )}
    {:else if setupState === 'ready'}
      {t(tts ? 'settings.localSpeech.ttsReady' : 'settings.localSpeech.ready')}
    {:else if setupState === 'restart_required' && !localSetup.restart_available}
      {t('settings.localSpeech.error.restart_unavailable')}
    {:else}
      {t(
        tts && setupState === 'missing'
          ? 'settings.localSpeech.ttsMissing'
          : `settings.localSpeech.state.${setupState}`,
      )}
    {/if}
  </div>
  {#if localSetupError === 'connection' || localSetupError === 'restart_timeout'}
    <Button onClick={refreshLocalSetup}
      >{t('settings.localSpeech.checkAgain')}</Button
    >
  {:else if setupState === 'missing' || setupState === 'failed'}
    <Button
      variant="primary"
      loading={localSetupAction}
      onClick={installLocalSpeech}
    >
      {t(
        setupState === 'failed'
          ? 'settings.localSpeech.retry'
          : 'settings.localSpeech.installButton',
      )}
    </Button>
  {:else if setupState === 'restart_required'}
    <Button
      variant="primary"
      loading={localSetupAction}
      disabled={taskSurfaceBusy || !localSetup.restart_available}
      onClick={restartLocalSpeechServer}
    >
      {t('settings.localSpeech.restartButton')}
    </Button>
  {:else if setupState === 'installing' || setupState === 'restarting'}
    <Button loading
      >{t(
        setupState === 'installing'
          ? 'settings.localSpeech.installingButton'
          : 'settings.localSpeech.restartingButton',
      )}</Button
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
          {t(
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
    {#if previewAudio}<audio
        controls
        src={previewAudio}
        aria-label={t('settings.localSpeech.previewAudio')}
      ></audio>{/if}
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
    font-size: 12px;
    line-height: 1.45;
  }
  audio {
    width: 100%;
  }
</style>
