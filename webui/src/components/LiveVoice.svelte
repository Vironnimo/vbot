<script>
  import { onMount, untrack } from 'svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { t } from '$lib/i18n.js';
  import {
    desktopMicrophoneAccess,
    isDesktopAccessor,
    onDesktopLiveRequest,
  } from '$lib/desktopBridge.js';
  import { createLiveVoice, createLiveVoiceState } from '$lib/liveVoice.js';
  import { liveWakePhrases } from '$lib/wakewordSettings.js';

  // The hold reason while Desktop Voice records a spoken command.
  const VOICE_COMMAND_HOLD = 'wakeword';

  let {
    configured = false,
    uiActions = {},
    serverUnavailable = false,
    // The Desktop Voice status snapshot, or null without Desktop Voice.
    voiceStatus = null,
    onToast = () => {},
  } = $props();

  let voice = $state(createLiveVoiceState());
  let controller;
  let audioElement;

  const running = $derived(voice.phase !== 'off');
  const caption = $derived(voice.captions.at(-1) ?? null);

  const MESSAGES = {
    not_configured: () => t('live.error.notConfigured'),
    not_usable: () => t('live.error.notUsable'),
    invalid_offer: () => t('live.error.invalidOffer'),
    access_denied: () => t('live.error.access'),
    rate_limited: () => t('live.error.rateLimited'),
    outcome_unknown: () => t('live.error.unknown'),
    provider_error: () => t('live.error.provider'),
    control_failed: () => t('live.error.control'),
    microphone_denied: () => t('live.error.permission'),
    microphone_unavailable: () => t('live.error.microphone'),
    connection_timeout: () => t('live.error.timeout'),
    connection_failed: () => t('live.error.connection'),
    connection_lost: () => t('live.error.connectionLost'),
    call_failed: () => t('live.error.callFailed'),
    playback_blocked: () => t('live.error.playback'),
    audio_unsupported: () => t('live.error.audioUnsupported'),
    media_mismatch: () => t('live.error.mediaMismatch'),
    desktop_restart_required: () => t('live.error.desktopRestart'),
    ui_action_failed: () => t('live.error.uiAction'),
    notification_failed: () => t('live.error.notification'),
    replaced: () => t('live.notice.replaced'),
    ended: () => t('live.notice.ended'),
  };
  const TOAST_VARIANTS = { error: 'error', warn: 'warn', info: 'info' };

  function showNotice({ code, severity = 'error' }) {
    const message =
      MESSAGES[code]?.() ??
      t('live.error.generic', {
        code,
      });
    onToast({
      title: t('live.title'),
      message,
      variant: TOAST_VARIANTS[severity] ?? 'error',
    });
  }

  onMount(() => {
    // In the Desktop app, a wake phrase or the global shortcut can start Live,
    // and the call learns which wake phrases address other Agents.
    const desktop = isDesktopAccessor();
    controller = createLiveVoice({
      state: voice,
      audio: audioElement,
      uiActions,
      onNotice: showNotice,
      checkMicrophoneAccess: desktop ? () => desktopMicrophoneAccess() : null,
      wakePhrases: () => liveWakePhrases(voiceStatus),
    });
    const stopDesktopRequests = desktop
      ? onDesktopLiveRequest(handleDesktopRequest)
      : () => {};
    return () => {
      stopDesktopRequests();
      controller.destroy();
    };
  });

  // `start` starts a call when none runs; `toggle` also stops a running one.
  // Both come from a Live voice wake phrase or the global shortcut.
  function handleDesktopRequest({ action }) {
    if (serverUnavailable) return false;
    if (!configured) {
      showNotice({ code: 'not_configured' });
      return true;
    }
    if (voice.phase === 'off') void startVoice();
    else if (action === 'toggle' && voice.phase !== 'closing')
      controller.stop();
    return true;
  }

  $effect(() => {
    if (serverUnavailable || !configured)
      untrack(() => {
        if (voice.phase !== 'off') controller?.stop();
      });
  });

  // While Desktop Voice records a spoken command, the running call is held:
  // the command does not reach the Live voice Model and the assistant stays
  // silent. A snapshot without a recording releases it, also after a missed
  // event.
  $effect(() => {
    const recording = Boolean(voiceStatus?.recording);
    const active = voice.phase !== 'off' && voice.phase !== 'closing';
    untrack(() => {
      if (!controller) return;
      const holding = controller.held(VOICE_COMMAND_HOLD);
      if (recording && active && !holding) controller.hold(VOICE_COMMAND_HOLD);
      else if (!recording && holding) controller.release(VOICE_COMMAND_HOLD);
    });
  });

  async function startVoice() {
    if (!configured || serverUnavailable) return;
    await controller.start();
  }

  const toggleLabel = $derived(
    running ? t('live.stopButton') : t('live.startButton'),
  );
  const muteLabel = $derived(t('live.mute'));
  const busyLabel = $derived(t('live.busy'));
  const captionText = $derived(
    voice.phase === 'connecting'
      ? t('live.state.connecting')
      : voice.phase === 'closing'
        ? t('live.state.closing')
        : voice.held
          ? t('live.state.held')
          : (caption?.text ?? t('live.state.listening')),
  );
</script>

{#if configured}
  <div class="sidebar-footer__row live-voice">
    <button
      type="button"
      class="live-voice__toggle"
      class:live-voice__toggle--active={running}
      aria-label={toggleLabel}
      use:tooltip={{ text: toggleLabel, placement: 'right' }}
      disabled={voice.phase === 'closing' || (!running && serverUnavailable)}
      onclick={() => (running ? controller.stop() : startVoice())}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        {#if running}<rect x="4" y="4" width="8" height="8" rx="1" />
        {:else}<path d="M5 3.5 12 8l-7 4.5Z" />{/if}
      </svg>
      <span class="sidebar-footer__label">{toggleLabel}</span>
    </button>
    {#if running && voice.busy}
      <span
        class="live-voice__busy"
        role="status"
        aria-label={busyLabel}
        use:tooltip={{ text: busyLabel, placement: 'right' }}
      ></span>
    {/if}
    {#if running}
      <button
        type="button"
        class="live-voice__mute"
        aria-label={muteLabel}
        aria-pressed={voice.muted}
        use:tooltip={{
          text: voice.muted ? t('live.unmute') : muteLabel,
          placement: 'right',
        }}
        disabled={voice.phase === 'closing'}
        onclick={() => controller.mute()}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <rect x="6" y="2" width="4" height="7.5" rx="2" />
          <path d="M3.5 9.5a4.5 4.5 0 0 0 9 0" />
          <path d="M8 14v1.5" />
          {#if voice.muted}<path d="M2.5 2.5l11 11" />{/if}
        </svg>
      </button>
    {/if}
  </div>
  {#if running}
    <div class="sidebar-footer__row live-voice__caption-row">
      <span
        class="live-voice__caption"
        data-role={voice.phase === 'live' && caption && !voice.held
          ? caption.role
          : 'status'}
        use:tooltip={{
          text: captionText,
          placement: 'right',
          whenTruncated: true,
        }}>{captionText}</span
      >
    </div>
  {/if}
{/if}
<audio bind:this={audioElement} hidden></audio>

<style>
  .live-voice__toggle {
    display: flex;
    flex: 1 1 auto;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 3px 0;
    border: 0;
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    text-align: left;
    cursor: pointer;
  }
  .live-voice__toggle svg {
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    fill: currentColor;
  }
  .live-voice__toggle:hover {
    color: var(--text-hi);
  }
  /* A running Live call is a running state, so it takes amber. */
  .live-voice__toggle--active,
  .live-voice__toggle--active:hover {
    color: var(--amber);
  }
  .live-voice__toggle:disabled,
  .live-voice__mute:disabled {
    cursor: default;
  }
  .live-voice__toggle:focus-visible,
  .live-voice__mute:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 2px;
    border-radius: var(--r-sm);
  }
  .live-voice__busy {
    flex: 0 0 6px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--amber);
    animation: live-voice-pulse 1.2s ease-in-out infinite;
  }
  .live-voice__mute {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-lo);
    cursor: pointer;
  }
  .live-voice__mute svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.3;
  }
  .live-voice__mute:hover {
    color: var(--text-hi);
  }
  .live-voice__mute[aria-pressed='true'] {
    color: var(--red);
  }
  .live-voice__caption-row {
    padding-top: 0;
  }
  .live-voice__caption {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .live-voice__caption[data-role='user'],
  .live-voice__caption[data-role='status'] {
    color: var(--text-lo);
  }
  :global(.app-shell[data-sidebar-collapsed='true']) .live-voice__toggle {
    flex: 0 0 auto;
    justify-content: center;
  }
  /* The compact rail and the phone status band have no room for a caption. */
  :global(.app-shell[data-sidebar-collapsed='true']) .live-voice__caption-row {
    display: none;
  }
  @media (max-width: 640px) {
    .live-voice__caption-row {
      display: none;
    }
  }
  @keyframes live-voice-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.3;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .live-voice__busy {
      animation: none;
    }
  }
</style>
