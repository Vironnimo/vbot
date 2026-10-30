<script>
  import { onDestroy, untrack } from 'svelte';
  import { debugSpeechClear, debugSpeechList } from '$lib/api.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';

  let {
    settings = null,
    active = true,
    debugTracesRefreshToken = 0,
  } = $props();
  let enabled = $derived(settings?.debug?.enabled === true);
  let limit = $derived(settings?.debug?.speech_recording_limit ?? 10);
  let recordings = $state([]);
  let opened = $state([]);
  let error = $state('');
  let loading = $state(false);
  let clearing = $state(false);
  let showConfirm = $state(false);
  let clearError = $state('');
  let retry = $state(0);
  let request = 0;
  let disposed = false;
  onDestroy(() => {
    disposed = true;
    request++;
  });

  $effect(() => {
    void debugTracesRefreshToken;
    void limit;
    void retry;
    const capture = enabled;
    if (!active || clearing) return;
    const token = ++request;
    if (!capture) {
      recordings = [];
      error = '';
      loading = false;
      return;
    }
    const controller = new AbortController();
    untrack(() => {
      loading = true;
      error = '';
      void debugSpeechList({ signal: controller.signal })
        .then(
          (result) => {
            if (disposed || token !== request) return;
            recordings = result.recordings;
            opened = opened.filter((id) =>
              recordings.some((record) => record.recording_id === id),
            );
          },
          (failure) => {
            if (!disposed && token === request)
              error = failure?.message || t('errors.generic');
          },
        )
        .finally(() => {
          if (!disposed && token === request) loading = false;
        });
    });
    return () => {
      request++;
      controller.abort();
    };
  });

  async function clearRecordings() {
    if (clearing) return;
    clearing = true;
    clearError = '';
    request++;
    try {
      await debugSpeechClear();
      if (disposed) return;
      recordings = [];
      opened = [];
      showConfirm = false;
    } catch (failure) {
      if (!disposed) clearError = failure?.message || t('errors.generic');
    } finally {
      if (!disposed) clearing = false;
    }
  }

  function toggle(recordingId, event) {
    opened = event.currentTarget.open
      ? [...opened.filter((id) => id !== recordingId), recordingId]
      : opened.filter((id) => id !== recordingId);
  }

  function timestamp(value) {
    return formatDateTimeInApplicationZone(value, activeLocaleTag(), {
      dateStyle: 'medium',
      timeStyle: 'medium',
    });
  }
</script>

<div class="speech-debug">
  <div class="speech-debug__head">
    <p>{t('debug.speech.description')}</p>
    <Button
      variant="secondary"
      disabled={clearing}
      onClick={() => {
        clearError = '';
        showConfirm = true;
      }}
    >
      {t('debug.speech.clear')}
    </Button>
  </div>
  {#if !enabled}
    <Banner>{t('debug.speech.disabled')}</Banner>
  {:else if error}
    <Banner variant="warn" role="alert">
      {error}
      <Button variant="secondary" onClick={() => retry++}
        >{t('common.retry')}</Button
      >
    </Banner>
  {:else if loading && recordings.length === 0}
    <span role="status">{t('common.loading')}</span>
  {:else if recordings.length === 0}
    <p>{t('debug.speech.empty')}</p>
  {/if}
  {#each recordings as recording (recording.recording_id)}
    <details ontoggle={(event) => toggle(recording.recording_id, event)}>
      <summary>
        <span class="speech-debug__time">{timestamp(recording.timestamp)}</span>
        <span class="speech-debug__preview"
          >{recording.text ||
            recording.error?.message ||
            recording.error?.type ||
            t('debug.speech.noText')}</span
        >
      </summary>
      <div class="speech-debug__body">
        {#if recording.error}
          <Banner variant="warn"
            >{recording.error.message || recording.error.type}</Banner
          >
        {:else}
          <p class="speech-debug__transcript">
            {recording.text || t('debug.speech.noText')}
          </p>
        {/if}
        {#if active && enabled && opened.includes(recording.recording_id)}
          <AudioPlayer
            src={recording.audio_url}
            ariaLabel={t('debug.speech.audio')}
          />
        {/if}
      </div>
    </details>
  {/each}
</div>

{#if showConfirm && active}
  <ConfirmDialog
    title={t('debug.speech.clear')}
    body={t('debug.speech.clearConfirm')}
    confirmLabel={t('common.delete')}
    busy={clearing}
    error={clearError}
    onConfirm={clearRecordings}
    onCancel={() => (showConfirm = false)}
  />
{/if}

<style>
  .speech-debug {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }
  .speech-debug__head {
    display: flex;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: var(--space-md);
  }
  .speech-debug__head p {
    flex: 1;
    min-width: 200px;
  }
  p {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  details {
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }
  summary {
    padding: var(--space-md);
    cursor: pointer;
  }
  summary:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 2px;
  }
  .speech-debug__time {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .speech-debug__preview {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin-top: var(--space-xs);
  }
  details[open] .speech-debug__preview {
    display: none;
  }
  .speech-debug__body {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: 0 var(--space-md) var(--space-md);
  }
  .speech-debug__transcript {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-hi);
  }
</style>
