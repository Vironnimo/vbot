import { createToastState, dismissToast, addToast } from '$lib/toastState.js';
import { SvelteMap } from 'svelte/reactivity';
import { CONNECTION_STATUS_DISCONNECTED } from '$lib/connectionState.js';
import {
  stopWakewordRecording,
  playWakewordCue,
  isDesktopAccessor,
  getDesktopCapabilities,
  onWakewordStatusChange,
  waitForDesktopBridge,
} from '$lib/desktopBridge.js';
import { t } from '$lib/i18n.js';
import { onMount } from 'svelte';

export function createAppDesktop(context) {
  const TOAST_AUTO_DISMISS_MS = 3200;

  const DESKTOP_BRIDGE_PROBE_TIMEOUT_MS = 1000;

  const DESKTOP_CAPABILITY_RETRY_MS = 1000;

  let toastState = $state(createToastState());

  let desktopCapabilities = $state(null);

  let wakewordStatus = $state({ enabled: false, state: 'off' });

  let cleanupWakewordPoll = null;

  let lastWakewordEventSequence = null;

  const toastDismissTimers = new SvelteMap();

  const clearToastDismissTimer = (id) => {
    const timer = toastDismissTimers.get(id);
    if (!timer) {
      return;
    }

    clearTimeout(timer);
    toastDismissTimers.delete(id);
  };

  const clearToastDismissTimers = () => {
    for (const timer of toastDismissTimers.values()) {
      clearTimeout(timer);
    }
    toastDismissTimers.clear();
  };

  const dismissAppToast = (id) => {
    clearToastDismissTimer(id);
    dismissToast(toastState, id);
  };

  const showToast = ({
    title,
    message = '',
    variant = 'info',
    autoDismiss,
  }) => {
    // A disconnected server is already represented by the global availability
    // notice. Suppress dependent action/load errors so one transport failure
    // cannot flood the active view with duplicate symptoms.
    if (
      variant === 'error' &&
      context.connectionState.status === CONNECTION_STATUS_DISCONNECTED
    ) {
      return;
    }

    const id = addToast(toastState, { title, message, variant });
    // Error toasts stay until the user dismisses them (a transport/server
    // failure the user must acknowledge); success/info/warn auto-dismiss. An
    // explicit `autoDismiss` from the caller always wins over this default.
    const effectiveAutoDismiss =
      autoDismiss === undefined ? variant !== 'error' : autoDismiss;
    if (!effectiveAutoDismiss) {
      return;
    }

    const timer = setTimeout(() => {
      dismissToast(toastState, id);
      toastDismissTimers.delete(id);
    }, TOAST_AUTO_DISMISS_MS);
    toastDismissTimers.set(id, timer);
  };

  const handleStopWakewordRecording = () => {
    // Fire-and-forget: the status poll reconciles the indicator, and a failed
    // bridge call leaves the recording running rather than losing it.
    void stopWakewordRecording().catch(() => {});
  };

  const wakewordFailureMessage = (errorCode) => {
    if (errorCode === 'speech_to_text_unconfigured') {
      return t(
        'settings.voice.error.speechToTextUnconfigured',
        'Configure a Speech-to-text Model under Settings → Models before enabling wakeword listening.',
      );
    }
    if (errorCode === 'speech_to_text_unavailable') {
      return t(
        'settings.voice.error.speechToTextUnavailable',
        'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Models.',
      );
    }
    if (errorCode === 'server_unreachable') {
      return t(
        'settings.voice.error.serverUnreachable',
        'Voice could not reach the active server. Check the Desktop connection and try again.',
      );
    }
    return t(
      'voice.toast.errorMessage',
      'Open Voice settings for details. The failure was written to the Desktop log.',
    );
  };

  const showWakewordEventToast = (event) => {
    if (event?.state === 'sent') {
      showToast({
        title: t('voice.toast.sentTitle', 'Voice command sent'),
        variant: 'success',
      });
      return;
    }
    if (event?.state === 'no_speech') {
      showToast({
        title: t('voice.toast.noSpeechTitle', 'No speech heard'),
        message: t(
          'voice.toast.noSpeechMessage',
          'No command followed the wakeword. Try again and speak after the cue.',
        ),
        variant: 'warn',
      });
      return;
    }
    if (event?.state === 'transcription_failed') {
      showToast({
        title: t(
          'voice.toast.transcriptionFailedTitle',
          'Voice command could not be transcribed',
        ),
        message: t(
          'voice.toast.transcriptionFailedMessage',
          'Check the Speech-to-text Model and the Desktop log, then try again.',
        ),
        variant: 'error',
      });
      return;
    }
    if (event?.state === 'microphone_disconnected') {
      showToast({
        title: t(
          'voice.toast.microphoneDisconnectedTitle',
          'Microphone disconnected',
        ),
        message: t(
          'voice.toast.microphoneDisconnectedMessage',
          'Wakeword listening is paused. Reconnect the microphone and retry when you are ready.',
        ),
        variant: 'warn',
      });
      return;
    }
    if (event?.state === 'error') {
      showToast({
        title: t('settings.voice.errorTitle', 'Voice needs attention'),
        message: wakewordFailureMessage(event.error_code),
        variant: 'error',
      });
    }
  };

  const applyDesktopWakewordStatus = (status) => {
    wakewordStatus = status;
    const events = Array.isArray(status?.events) ? status.events : [];
    const latestSequence = events.reduce(
      (latest, event) =>
        Number.isFinite(event?.sequence)
          ? Math.max(latest, event.sequence)
          : latest,
      0,
    );
    if (lastWakewordEventSequence === null) {
      // Do not replay sounds that happened before this WebUI mounted.
      lastWakewordEventSequence = latestSequence;
      // A fatal startup failure is still current, not historical feedback.
      // Surface it even when the worker failed before the WebUI finished
      // mounting (for example an enabled Desktop starting without STT).
      if (status?.state === 'error') {
        showWakewordEventToast({
          state: 'error',
          error_code: status.error_code,
        });
      }
      return;
    }
    for (const event of events) {
      if (
        Number.isFinite(event?.sequence) &&
        event.sequence > lastWakewordEventSequence
      ) {
        void playWakewordCue(event.state);
        showWakewordEventToast(event);
      }
    }
    lastWakewordEventSequence = Math.max(
      lastWakewordEventSequence,
      latestSequence,
    );
  };

  onMount(() => {
    let cancelled = false;
    let desktopCapabilityRetryTimer = null;
    const scheduleDesktopCapabilityRetry = () => {
      if (cancelled || desktopCapabilityRetryTimer !== null) return;
      desktopCapabilityRetryTimer = setTimeout(() => {
        desktopCapabilityRetryTimer = null;
        void initializeDesktopCapabilities();
      }, DESKTOP_CAPABILITY_RETRY_MS);
    };

    const initializeDesktopCapabilities = async () => {
      try {
        const ready = await waitForDesktopBridge(
          DESKTOP_BRIDGE_PROBE_TIMEOUT_MS,
        );
        if (cancelled) return;
        if (!ready) {
          scheduleDesktopCapabilityRetry();
          return;
        }
        const caps = await getDesktopCapabilities();
        if (cancelled) return;
        desktopCapabilities = caps;
        if (caps?.wakeword && !cleanupWakewordPoll) {
          cleanupWakewordPoll = onWakewordStatusChange((status) => {
            applyDesktopWakewordStatus(status);
          });
        }
      } catch {
        scheduleDesktopCapabilityRetry();
      }
    };

    // Detect desktop capabilities and keep probing while the asynchronously
    // injected bridge is absent or temporarily rejects a capability call.
    if (isDesktopAccessor()) {
      void initializeDesktopCapabilities();
    } else {
      desktopCapabilities = {
        wakeword: false,
        serverSelection: false,
        contextMenu: false,
      };
    }

    return () => {
      cancelled = true;
      clearToastDismissTimers();
      if (desktopCapabilityRetryTimer !== null) {
        clearTimeout(desktopCapabilityRetryTimer);
        desktopCapabilityRetryTimer = null;
      }
      if (cleanupWakewordPoll) {
        cleanupWakewordPoll();
        cleanupWakewordPoll = null;
      }
    };
  });
  return {
    get toastState() {
      return toastState;
    },
    get desktopCapabilities() {
      return desktopCapabilities;
    },
    get wakewordStatus() {
      return wakewordStatus;
    },
    get dismissAppToast() {
      return dismissAppToast;
    },
    get showToast() {
      return showToast;
    },
    get handleStopWakewordRecording() {
      return handleStopWakewordRecording;
    },
  };
}
