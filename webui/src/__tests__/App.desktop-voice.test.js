// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { t } from '../lib/i18n.js';
import { App, cleanupAppHarness, resetAppHarness } from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

const cues = vi.hoisted(() => vi.fn());
vi.mock('$lib/desktopBridge.js', async (importOriginal) => ({
  ...(await importOriginal()),
  playVoiceCue: cues,
}));

const VOICE_CAPABILITIES = { wakeword: true, voiceApi: 2 };
// Longer than the auto-dismiss delay of non-error Toasts.
const PAST_AUTO_DISMISS_MS = 10000;

function voiceStatus(overrides = {}) {
  return {
    enabled: true,
    mode: 'real',
    state: 'listening',
    error_code: null,
    sequence: 3,
    microphone: null,
    active_microphone: null,
    echo_cancellation: { enabled: true, state: 'active' },
    default_agent_id: 'main',
    default_session_behavior: 'active',
    phrases: [],
    recording: null,
    commands: [],
    calibration: null,
    limits: {
      max_active_phrases: 8,
      min_sensitivity: 0.05,
      max_sensitivity: 0.95,
    },
    ...overrides,
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) {
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
  }
}

function pushStatus(status) {
  window.dispatchEvent(
    new CustomEvent('vbot-desktop-voice', {
      detail: { type: 'status', status },
    }),
  );
  flushSync();
}

function pushEvent(sequence, kind, extra = {}) {
  window.dispatchEvent(
    new CustomEvent('vbot-desktop-voice', {
      detail: { type: 'event', event: { sequence, kind, ...extra } },
    }),
  );
  flushSync();
}

const indicator = () => document.querySelector('.sidebar-footer__mic');
const indicatorShows = (stateKey) =>
  expect(indicator().textContent).toContain(t(`voice.state.${stateKey}`));
const toasts = (variant) =>
  [...document.querySelectorAll(`.toast.${variant}`)].map((toast) => ({
    title: toast.querySelector('.toast-title')?.textContent,
    message: toast.querySelector('.toast-msg')?.textContent ?? '',
  }));
const toast = (title, message = '') => ({ title, message });
// The English catalog has no entry for these keys; the UI shows the fallback.
const VOICE_ERROR_TITLE = t('settings.voice.errorTitle');
const MICROPHONE_UNAVAILABLE = t('settings.voice.error.microphone');

describe('App Desktop Voice feedback', () => {
  let mountedComponent;
  let api;

  function installDesktop({
    capabilities = VOICE_CAPABILITIES,
    status = voiceStatus(),
  } = {}) {
    api = {
      getDesktopCapabilities: vi.fn().mockResolvedValue(capabilities),
      getVoiceStatus: vi.fn().mockResolvedValue(status),
      stopVoiceRecording: vi.fn().mockResolvedValue({ stopped: true }),
    };
    window.pywebview = { api };
    return api;
  }

  async function mountApp() {
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await settle();
  }

  beforeEach(() => {
    resetAppHarness();
    vi.useFakeTimers();
    cues.mockReset();
    mountedComponent = null;
    window.history.replaceState({}, '', '/?accessor=desktop');
  });

  afterEach(async () => {
    vi.useRealTimers();
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it('initializes Voice after the Desktop bridge arrives late', async () => {
    window.pywebview = undefined;
    await mountApp();
    await vi.advanceTimersByTimeAsync(1000);

    installDesktop();
    await vi.advanceTimersByTimeAsync(1000);
    await settle();

    expect(api.getDesktopCapabilities).toHaveBeenCalledOnce();
    expect(api.getVoiceStatus).toHaveBeenCalledOnce();
    indicatorShows('listening');
  });

  it('retries the first snapshot until the Desktop answers', async () => {
    installDesktop();
    api.getVoiceStatus.mockRejectedValueOnce(new Error('starting'));
    await mountApp();
    indicatorShows('off');

    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(api.getVoiceStatus).toHaveBeenCalledTimes(2);
    indicatorShows('listening');
  });

  it('offers no Voice to a Desktop without the Voice bridge it speaks', async () => {
    installDesktop({ capabilities: { wakeword: true, voiceApi: 1 } });
    await mountApp();

    expect(api.getVoiceStatus).not.toHaveBeenCalled();
    expect(indicator()).toBeNull();
    pushStatus(voiceStatus({ sequence: 9 }));
    expect(indicator()).toBeNull();
  });

  it('shows a sticky Toast when Voice already failed on load', async () => {
    installDesktop({
      status: voiceStatus({
        state: 'error',
        error_code: 'speech_to_text_unconfigured',
      }),
    });
    await mountApp();

    const expected = [
      toast(
        VOICE_ERROR_TITLE,
        t('settings.voice.error.speechToTextUnconfigured'),
      ),
    ];
    expect(toasts('error')).toEqual(expected);
    await vi.advanceTimersByTimeAsync(PAST_AUTO_DISMISS_MS);
    flushSync();
    expect(toasts('error')).toEqual(expected);
  });

  it('gives no feedback for events from before the page loaded', async () => {
    installDesktop({ status: voiceStatus({ sequence: 5 }) });
    let resolveStatus;
    api.getVoiceStatus.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStatus = resolve;
      }),
    );
    await mountApp();

    // Before the first snapshot nothing is known about the order.
    pushEvent(4, 'sent');
    resolveStatus(voiceStatus({ sequence: 5 }));
    await settle();
    // Already covered by the snapshot.
    pushEvent(5, 'command_failed', { error_code: 'send_failed' });
    pushEvent(3, 'sent');

    expect(document.querySelector('.toast')).toBeNull();
    expect(cues).not.toHaveBeenCalled();

    pushEvent(6, 'sent', { command_id: 'c-1' });
    expect(toasts('success')).toEqual([toast(t('voice.toast.sentTitle'))]);
    expect(cues).toHaveBeenCalledExactlyOnceWith('sent');

    // The same event again runs once.
    pushEvent(6, 'sent', { command_id: 'c-1' });
    expect(cues).toHaveBeenCalledOnce();
  });

  it.each([
    [
      'no_speech',
      {},
      'warn',
      toast(t('voice.toast.noSpeechTitle'), t('voice.toast.noSpeechMessage')),
    ],
    [
      'transcription_failed',
      {},
      'error',
      toast(
        t('voice.toast.transcriptionFailedTitle'),
        t('voice.toast.transcriptionFailedMessage'),
      ),
    ],
    [
      'command_failed',
      { error_code: 'target_agent_unavailable' },
      'error',
      toast(
        t('voice.toast.commandFailedTitle'),
        t('settings.voice.error.targetUnavailable'),
      ),
    ],
    [
      'command_failed',
      { error_code: 'something_new' },
      'error',
      toast(
        t('voice.toast.commandFailedTitle'),
        t('voice.toast.commandFailedMessage'),
      ),
    ],
    [
      'error',
      { error_code: 'microphone_unavailable' },
      'error',
      toast(VOICE_ERROR_TITLE, MICROPHONE_UNAVAILABLE),
    ],
  ])(
    'reports %s %j with a cue and a %s Toast that only an error keeps',
    async (kind, extra, variant, expected) => {
      installDesktop();
      await mountApp();

      pushEvent(4, kind, extra);

      expect(cues).toHaveBeenCalledExactlyOnceWith(kind);
      expect(toasts(variant)).toEqual([expected]);
      await vi.advanceTimersByTimeAsync(PAST_AUTO_DISMISS_MS);
      flushSync();
      expect(toasts(variant)).toEqual(variant === 'error' ? [expected] : []);
    },
  );

  it('shows an auto-dismissing warning when a running microphone disconnects', async () => {
    installDesktop();
    await mountApp();

    pushEvent(4, 'microphone_disconnected', {
      error_code: 'microphone_read_failed',
    });
    pushStatus(
      voiceStatus({
        sequence: 4,
        state: 'microphone_disconnected',
        error_code: 'microphone_read_failed',
      }),
    );

    expect(toasts('warn')).toEqual([
      toast(
        t('voice.toast.microphoneDisconnectedTitle'),
        t('voice.toast.microphoneDisconnectedMessage'),
      ),
    ]);
    expect(toasts('error')).toHaveLength(0);
    indicatorShows('microphone_disconnected');

    await vi.advanceTimersByTimeAsync(PAST_AUTO_DISMISS_MS);
    flushSync();
    expect(toasts('warn')).toHaveLength(0);
  });

  it('plays the detection cue without a Toast and ignores unknown kinds', async () => {
    installDesktop();
    await mountApp();

    pushEvent(4, 'detected', { model_id: 'builtin/okay_nabu' });
    pushEvent(5, 'something_new');

    expect(cues.mock.calls).toEqual([['detected'], ['something_new']]);
    expect(document.querySelector('.toast')).toBeNull();
  });

  it('shows a recording at once and stops it from the indicator', async () => {
    installDesktop();
    await mountApp();

    pushEvent(4, 'recording_started', {
      command_id: 'c-1',
      model_id: 'builtin/okay_nabu',
    });
    indicatorShows('recording');

    indicator().click();
    expect(api.stopVoiceRecording).toHaveBeenCalledOnce();

    pushEvent(5, 'recording_ended', { command_id: 'c-1' });
    indicatorShows('listening');
  });

  it('does not bring back a recording a newer snapshot already covers', async () => {
    installDesktop();
    await mountApp();

    pushStatus(voiceStatus({ sequence: 6 }));
    // Delivered late: the snapshot at 6 already reflects it.
    pushEvent(4, 'recording_started', { command_id: 'c-1' });

    indicatorShows('listening');
  });

  it('applies newer snapshots and ignores older ones', async () => {
    installDesktop();
    await mountApp();

    pushStatus(voiceStatus({ sequence: 7, state: 'starting' }));
    indicatorShows('starting');
    pushStatus(voiceStatus({ sequence: 6, state: 'error' }));
    indicatorShows('starting');
    pushStatus({ sequence: 'late', state: 'error' });
    indicatorShows('starting');
  });

  it('reads the snapshot again when events were missed', async () => {
    installDesktop();
    await mountApp();
    expect(api.getVoiceStatus).toHaveBeenCalledOnce();

    api.getVoiceStatus.mockResolvedValue(
      voiceStatus({ sequence: 9, state: 'microphone_disconnected' }),
    );
    pushEvent(4, 'sent');
    expect(api.getVoiceStatus).toHaveBeenCalledOnce();

    // 5 and 6 never arrived.
    pushEvent(7, 'no_speech');
    await settle();

    expect(api.getVoiceStatus).toHaveBeenCalledTimes(2);
    // Missed events are not replayed; the ones received still gave feedback.
    expect(cues.mock.calls).toEqual([['sent'], ['no_speech']]);
    indicatorShows('microphone_disconnected');
  });
});
