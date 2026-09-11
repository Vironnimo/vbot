<script>
  import { onMount, untrack } from 'svelte';
  import Button from './ui/Button.svelte';
  import { t } from '$lib/i18n.js';
  import {
    isDesktopAccessor,
    waitForDesktopBridge,
    getWakewordStatus,
  } from '$lib/desktopBridge.js';
  import {
    createLiveActions,
    createLiveVoice,
    createLiveVoiceState,
  } from '$lib/liveVoice.js';

  let {
    getContext,
    navigate,
    terminalView,
    runEvents = [],
    serverUnavailable = false,
    wakewordEnabled = false,
    onSetup = () => {},
  } = $props();
  let state = $state(createLiveVoiceState());
  let controller;
  let audioElement;
  let expanded = $state(false);
  const running = $derived(
    ['connecting', 'listening', 'closing'].includes(state.phase),
  );
  const messages = {
    wakeword_active: () =>
      t(
        'live.error.wakeword',
        'Turn off wakeword Voice in Settings before starting the voice companion.',
      ),
    api_key_required: () =>
      t(
        'live.error.apiKey',
        'Add and enable your OpenAI API key in Providers. GPT-Live access is required.',
      ),
    access_denied: () =>
      t(
        'live.error.access',
        'OpenAI rejected access. Check your API key and GPT-Live availability for your account.',
      ),
    microphone_unavailable: () =>
      t(
        'live.error.microphone',
        'Live voice needs microphone access over HTTPS or localhost.',
      ),
    microphone_denied: () =>
      t('live.error.permission', 'Allow microphone access, then start again.'),
    outcome_unknown: () =>
      t(
        'live.error.unknown',
        'OpenAI may have created the conversation, but its connection could not be confirmed. The request was not repeated.',
      ),
    finalization_incomplete: () =>
      t(
        'live.error.finalization',
        'Voice stopped. Final usage could not be confirmed.',
      ),
    notification_failed: () =>
      t(
        'live.error.notification',
        'An Agent update could not be read. Check Chat for its latest reply.',
      ),
    backend_failed: () =>
      t(
        'live.error.backend',
        'The voice companion could not finish that request. Check the action before repeating it.',
      ),
    conversation_limit: () =>
      t('live.error.limit', 'Start a new voice conversation to continue.'),
  };
  const errorText = $derived(
    state.error
      ? messages[state.error]?.() ||
          t(
            'live.error.connection',
            'Live voice could not continue. Check the connection and GPT-Live access. Actions were not automatically repeated.',
          )
      : '',
  );

  onMount(() => {
    const execute = createLiveActions({
      getContext: async () => ({
        ...(await getContext()),
        recent_updates: state.updates.slice(-10).map((item) => ({
          ...item,
          excerpt: item.excerpt.slice(-300),
          truncated: item.truncated || item.excerpt.length > 300,
        })),
      }),
      navigate,
      terminalView,
      isActive: () => controller?.active() === true,
    });
    controller = createLiveVoice({
      state,
      execute,
      audio: audioElement,
      onActive: (active) => {
        if (active) controller.seedRuns(runEvents);
      },
    });
    return () => controller.destroy();
  });
  $effect(() => {
    const events = runEvents;
    if (state.phase === 'listening')
      untrack(() => {
        void controller?.notifyRuns(events);
      });
  });
  $effect(() => {
    if (wakewordEnabled)
      untrack(() => {
        if (running) {
          controller?.stop();
          state.error = 'wakeword_active';
        }
      });
  });
  async function startVoice() {
    if (isDesktopAccessor()) {
      try {
        if (!(await waitForDesktopBridge()))
          throw new Error('bridge_unavailable');
        if ((await getWakewordStatus()).enabled) {
          state.error = 'wakeword_active';
          return;
        }
      } catch {
        state.error = 'connection_failed';
        return;
      }
    }
    await controller.start();
  }
  $effect(() => {
    if (serverUnavailable)
      untrack(() => {
        if (running) controller?.stop();
      });
  });
  export function isActive() {
    return controller?.active() === true;
  }
</script>

<section class="live-voice" aria-label={t('live.label', 'Voice companion')}>
  <div class="live-voice__bar">
    <span
      class:live-voice__active={state.phase === 'listening'}
      class="live-voice__label">{t('live.label', 'Voice companion')}</span
    >
    <span role="status" class="live-voice__status"
      >{state.phase === 'connecting'
        ? t('live.connecting', 'Connecting…')
        : state.phase === 'closing'
          ? t('live.closing', 'Ending…')
          : state.phase === 'listening'
            ? state.muted
              ? t('live.muted', 'Microphone muted')
              : t('live.listening', 'Listening')
            : t('live.off', 'Off')}</span
    >
    {#if running}
      {#if state.phase === 'listening'}
        <Button variant="secondary" onClick={() => controller.mute()}
          >{state.muted
            ? t('live.unmute', 'Unmute')
            : t('live.mute', 'Mute')}</Button
        >
      {/if}
      <Button
        variant="secondary"
        onClick={() => controller.stop()}
        disabled={state.phase === 'closing'}
        >{t('live.end', 'End voice')}</Button
      >
    {:else}
      <Button
        variant="secondary"
        onClick={startVoice}
        disabled={serverUnavailable}>{t('live.start', 'Start voice')}</Button
      >
    {/if}
    <Button
      variant="ghost"
      onClick={() => (expanded = !expanded)}
      aria-expanded={expanded}>{t('live.details', 'Details')}</Button
    >
  </div>
  {#if errorText}<div class="live-voice__error" role="alert">
      {errorText}
      <Button variant="ghost" onClick={onSetup}
        >{t('live.providers', 'Open Providers')}</Button
      >
    </div>{/if}
  <audio
    bind:this={audioElement}
    controls={state.playbackBlocked}
    aria-label={t('live.playback', 'Voice playback')}
  ></audio>
  {#if expanded}
    <div class="live-voice__details">
      <p>
        {t(
          'live.description',
          'Speak to operate Chat, Codex and Claude Code Terminals. The companion announces completed Runs and relays Agent questions. Voice time and the backend Model are billed separately by OpenAI.',
        )}
      </p>
      {#each state.transcript as line, index (index)}
        <p>
          <strong
            >{line.role === 'user'
              ? t('live.you', 'You')
              : t('live.companion', 'Companion')}:</strong
          >
          {line.text}
        </p>
      {/each}
      {#if state.actions.length}<p>
          {t('live.actions', 'Actions')}: {state.actions.length} · {state.actions.filter(
            (item) => !item.ok,
          ).length}
          {t('live.failed', 'failed')}
        </p>{/if}
      {#if state.finalized}<p>
          {t('live.finished', 'Conversation ended; final usage received.')}
        </p>{/if}
    </div>
  {/if}
</section>

<style>
  .live-voice {
    flex: 0 0 auto;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .live-voice__bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 14px;
    flex-wrap: wrap;
  }
  .live-voice__label {
    font-size: var(--fs-label-md);
    color: var(--text-hi);
  }
  .live-voice__active {
    color: var(--accent);
  }
  .live-voice__status {
    font-size: var(--fs-body-sm);
    color: var(--text-med);
    margin-right: auto;
  }
  .live-voice__error {
    color: var(--red);
    padding: 0 14px 8px;
    font-size: var(--fs-body-sm);
  }
  .live-voice__details {
    max-height: 220px;
    overflow: auto;
    padding: 0 14px 10px;
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }
  .live-voice__details p {
    margin: 7px 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  audio:not([controls]) {
    display: none;
  }
  audio {
    max-width: 100%;
  }
</style>
