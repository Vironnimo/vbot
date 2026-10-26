<script>
  import AudioPlayer from './ui/AudioPlayer.svelte';
  import Button from './ui/Button.svelte';
  import { t } from '$lib/i18n.js';
  let { owner, onOpenSession } = $props();
  let entry = $derived(owner.active);
  let label = $derived(
    entry?.agentId
      ? t('audio.background', { agent: entry.agentId })
      : t('audio.label'),
  );
</script>

{#if entry}
  <aside class="speech-playback-bar" aria-label={label}>
    <div class="speech-playback-bar__origin">
      {#if entry.agentId && entry.sessionId}<Button
          variant="tertiary"
          ariaLabel={t('audio.openSession')}
          onClick={() => onOpenSession(entry)}
        >
          {label}
        </Button>{:else}<span>{label}</span>{/if}
      {#if owner.pending}<span
          >{t('audio.pending', { count: owner.pending })}</span
        >{/if}
    </div>
    <AudioPlayer resource={entry.resource} />
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('audio.stopSpeech')}
      tooltip={t('audio.stopSpeech')}
      onClick={() => owner.stop()}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        aria-hidden="true"
        fill="currentColor"><path d="M6 6h12v12H6z" /></svg
      >
    </Button>
  </aside>
{/if}

<style>
  .speech-playback-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  .speech-playback-bar__origin {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }
  .speech-playback-bar__origin span {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .speech-playback-bar :global(.audio-player) {
    flex: 1;
    min-width: 260px;
  }
</style>
