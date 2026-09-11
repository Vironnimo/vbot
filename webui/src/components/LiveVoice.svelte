<script>
  import { onMount, untrack } from 'svelte';
  import { tooltip } from '$lib/tooltip.js';
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
    enabled = false,
    onToast = () => {},
  } = $props();
  let state = $state(createLiveVoiceState());
  let controller;
  let audioElement;

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
    if (!enabled || serverUnavailable) return;
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
    if (enabled && !serverUnavailable) await controller.start();
  }
  $effect(() => {
    if (serverUnavailable)
      untrack(() => {
        if (running) controller?.stop();
      });
  });
  $effect(() => {
    if (!enabled)
      untrack(() => {
        if (running) controller?.stop();
      });
  });
  $effect(() => {
    const message = errorText;
    if (message)
      untrack(() =>
        onToast({
          title: t('live.settings.label', 'Live voice'),
          message,
          variant: 'error',
        }),
      );
  });
  $effect(() => {
    if (state.playbackBlocked)
      untrack(() => {
        controller?.stop();
        onToast({
          title: t('live.settings.label', 'Live voice'),
          message: t(
            'live.error.playback',
            'Audio playback was blocked. Allow audio for this app and start Live again.',
          ),
          variant: 'error',
        });
      });
  });
  const buttonLabel = $derived(
    running
      ? t('live.stopButton', 'Stop Live')
      : t('live.startButton', 'Start Live'),
  );
  export function isActive() {
    return controller?.active() === true;
  }
</script>

{#if enabled}
  <div class="sidebar-footer__row live-voice">
    <button
      type="button"
      class="live-voice__button"
      class:live-voice__button--active={running}
      aria-label={buttonLabel}
      aria-pressed={running}
      use:tooltip={buttonLabel}
      disabled={state.phase === 'closing' || (!running && serverUnavailable)}
      onclick={() => (running ? controller.stop() : startVoice())}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        {#if running}<rect x="4" y="4" width="8" height="8" rx="1" />
        {:else}<path d="M5 3.5 12 8l-7 4.5Z" />{/if}
      </svg>
      <span class="sidebar-footer__label">{buttonLabel}</span>
    </button>
  </div>
{/if}
<audio bind:this={audioElement} hidden></audio>

<style>
  .live-voice__button {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 3px 0;
    border: 0;
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    text-align: left;
    cursor: pointer;
  }
  .live-voice__button svg {
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    fill: currentColor;
  }
  .live-voice__button:hover,
  .live-voice__button--active {
    color: var(--accent);
  }
  .live-voice__button:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 4px;
    border-radius: 3px;
  }
  .live-voice__button:disabled {
    cursor: default;
  }
  :global(.app-shell[data-sidebar-collapsed='true']) .live-voice__button {
    justify-content: center;
  }
</style>
