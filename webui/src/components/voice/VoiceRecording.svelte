<script>
  import { getVoiceRecording } from '$lib/desktopBridge.js';
  import { t } from '$lib/i18n.js';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';

  let { recording, active = true } = $props();
  let commandId = $derived(recording?.command_id);
  let src = $state('');
  let failed = $state(false);
  let retry = $state(0);

  $effect(() => {
    const id = commandId;
    void retry;
    if (!active || !id) return;
    let disposed = false;
    let url = '';
    src = '';
    failed = false;
    void getVoiceRecording(id)
      .then((blob) => {
        if (disposed) return;
        url = URL.createObjectURL(blob);
        src = url;
      })
      .catch(() => {
        if (!disposed) failed = true;
      });
    return () => {
      disposed = true;
      if (url) URL.revokeObjectURL(url);
      src = '';
    };
  });
</script>

<div class="s-row s-row--stacked">
  <div class="s-row-info">
    <div class="s-row-label">{t('settings.voice.lastRecording')}</div>
    <div class="s-row-desc">{t('settings.voice.lastRecordingHelp')}</div>
  </div>
  <div class="voice-recording-player">
    {#if active && src}
      {#key src}
        <AudioPlayer {src} ariaLabel={t('settings.voice.lastRecording')} />
      {/key}
    {:else if failed}
      <Banner variant="warn">{t('settings.voice.recordingLoadFailed')}</Banner>
      <Button variant="secondary" onClick={() => retry++}>
        {t('common.retry')}
      </Button>
    {:else if active}
      <span role="status">{t('audio.loading')}</span>
    {/if}
  </div>
</div>

<style>
  .voice-recording-player {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-sm);
    width: 100%;
    min-width: 0;
  }
</style>
