import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  isDesktop,
  isDesktopAccessor,
  desktopErrorCode,
  getDesktopCapabilities,
  getDesktopClipboardText,
  openDesktopExternalUrl,
  setDesktopClipboardText,
  listDesktopServers,
  addDesktopServer,
  removeDesktopServer,
  selectDesktopServer,
  disabledDesktopCapabilities,
  supportsDesktopVoice,
  normalizeVoiceStatus,
  normalizeVoiceEvent,
  getVoiceStatus,
  setVoiceEnabled,
  updateVoiceConfig,
  listMicrophones,
  listWakewordModels,
  importWakewordModel,
  deleteWakewordModel,
  retryVoice,
  stopVoiceRecording,
  startVoiceCalibration,
  restartVoiceCalibration,
  stopVoiceCalibration,
  onDesktopVoicePush,
  desktopMicrophoneAccess,
  playVoiceCue,
  waitForDesktopBridge,
  onDesktopLiveRequest,
  getDesktopLiveHotkey,
  setDesktopLiveHotkey,
} from '../desktopBridge.js';

const NO_VOICE_CAPABILITIES = {
  voiceApi: 0,
  liveHotkey: false,
  secureOrigins: [],
};

function desktopWindow(
  api,
  { secure = true, origin = 'http://pi.lan:8420' } = {},
) {
  globalThis.window = {
    location: { search: '?accessor=desktop', origin },
    isSecureContext: secure,
    pywebview: { api },
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

function rawStatus(overrides = {}) {
  return {
    enabled: true,
    mode: 'real',
    state: 'listening',
    error_code: null,
    sequence: 4,
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

describe('desktop detection', () => {
  let originalLocation;
  let originalPywebview;

  beforeEach(() => {
    originalLocation = globalThis.window?.location;
    originalPywebview = globalThis.window?.pywebview;
    globalThis.window = {
      location: { search: '' },
      pywebview: undefined,
    };
  });

  afterEach(() => {
    if (originalLocation !== undefined) {
      globalThis.window.location = originalLocation;
    }
    globalThis.window.pywebview = originalPywebview;
  });

  it('returns false without accessor param or bridge', () => {
    expect(isDesktopAccessor()).toBe(false);
    expect(isDesktop()).toBe(false);
  });

  it('detects the accessor param before the bridge is ready', () => {
    globalThis.window.location.search = '?accessor=desktop';
    expect(isDesktopAccessor()).toBe(true);
    expect(isDesktop()).toBe(false);
  });

  it('returns false with only bridge api', () => {
    globalThis.window.pywebview = { api: {} };
    expect(isDesktop()).toBe(false);
  });

  it('returns true with both accessor param and bridge api', () => {
    globalThis.window.location.search = '?accessor=desktop';
    globalThis.window.pywebview = { api: {} };
    expect(isDesktopAccessor()).toBe(true);
    expect(isDesktop()).toBe(true);
  });

  it('returns false when window is undefined', () => {
    const savedWindow = globalThis.window;
    globalThis.window = undefined;
    expect(isDesktopAccessor()).toBe(false);
    expect(isDesktop()).toBe(false);
    globalThis.window = savedWindow;
  });
});

describe('getDesktopCapabilities', () => {
  it('returns cached capabilities on second call', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({
            wakeword: true,
            serverSelection: true,
            contextMenu: true,
          }),
        },
      },
    };

    const caps1 = await getDesktopCapabilities();
    expect(caps1).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
      ...NO_VOICE_CAPABILITIES,
    });

    // Second call should return cached result
    const caps2 = await getDesktopCapabilities();
    expect(caps2).toBe(caps1);
  });

  it('normalizes the Voice bridge version and the Live voice capabilities', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({
            wakeword: true,
            voiceApi: 2,
            liveHotkey: 1,
            secureOrigins: ['http://pi.lan:8420', 42, null],
          }),
        },
      },
    };

    expect(await getDesktopCapabilities()).toEqual({
      wakeword: true,
      serverSelection: false,
      contextMenu: false,
      voiceApi: 2,
      liveHotkey: true,
      secureOrigins: ['http://pi.lan:8420'],
    });
  });

  it('reads an invalid Voice bridge version as none', async () => {
    for (const voiceApi of ['2', 1.5, -2, null]) {
      globalThis.window = {
        location: { search: '?accessor=desktop' },
        pywebview: {
          api: { getDesktopCapabilities: () => ({ wakeword: true, voiceApi }) },
        },
      };
      expect((await getDesktopCapabilities()).voiceApi).toBe(0);
    }
  });

  it('returns disabled when bridge absent', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    const caps = await getDesktopCapabilities();
    expect(caps).toEqual({
      wakeword: false,
      serverSelection: false,
      contextMenu: false,
      ...NO_VOICE_CAPABILITIES,
    });
    expect(disabledDesktopCapabilities()).toEqual(caps);
  });

  it('does not reuse cached capabilities for a different bridge api object', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () => ({
            wakeword: true,
            serverSelection: true,
            contextMenu: true,
          }),
        },
      },
    };

    expect(await getDesktopCapabilities()).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
      ...NO_VOICE_CAPABILITIES,
    });

    globalThis.window.pywebview = {
      api: {
        getDesktopCapabilities: () => ({ wakeword: false }),
      },
    };

    expect(await getDesktopCapabilities()).toEqual({
      wakeword: false,
      serverSelection: false,
      contextMenu: false,
      ...NO_VOICE_CAPABILITIES,
    });
  });

  it('propagates a transient capability failure so callers can retry', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          getDesktopCapabilities: () =>
            Promise.reject(new Error('bridge starting')),
        },
      },
    };

    await expect(getDesktopCapabilities()).rejects.toThrow('bridge starting');
  });

  it('offers the Voice UI only for the Voice bridge version it speaks', () => {
    expect(supportsDesktopVoice({ wakeword: true, voiceApi: 2 })).toBe(true);
    expect(supportsDesktopVoice({ wakeword: true, voiceApi: 0 })).toBe(false);
    expect(supportsDesktopVoice({ wakeword: true, voiceApi: 3 })).toBe(false);
    expect(supportsDesktopVoice({ wakeword: false, voiceApi: 2 })).toBe(false);
    expect(supportsDesktopVoice(null)).toBe(false);
  });
});

describe('desktop context-menu actions', () => {
  it('reads, writes, and opens through the native bridge', async () => {
    const setClipboardText = vi.fn(() => ({ copied: true }));
    const openExternalUrl = vi.fn(() => ({ opened: true }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          setClipboardText,
          getClipboardText: () => 'paste me',
          openExternalUrl,
        },
      },
    };

    await expect(setDesktopClipboardText('copy me')).resolves.toEqual({
      copied: true,
    });
    await expect(getDesktopClipboardText()).resolves.toBe('paste me');
    await expect(
      openDesktopExternalUrl('https://example.com/path'),
    ).resolves.toEqual({ opened: true });
    expect(setClipboardText).toHaveBeenCalledWith('copy me');
    expect(openExternalUrl).toHaveBeenCalledWith('https://example.com/path');
  });

  it('normalizes a non-text clipboard response to empty text', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { getClipboardText: () => null } },
    };

    await expect(getDesktopClipboardText()).resolves.toBe('');
  });
});

describe('waitForDesktopBridge', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('resolves false outside the desktop accessor URL', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    await expect(waitForDesktopBridge()).resolves.toBe(false);
  });

  it('resolves true immediately when the bridge already exists', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: {} },
    };

    await expect(waitForDesktopBridge()).resolves.toBe(true);
  });

  it('waits for pywebviewready before resolving in desktop mode', async () => {
    const listeners = new Map();
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: undefined,
      addEventListener: (eventName, callback) => {
        listeners.set(eventName, callback);
      },
      removeEventListener: (eventName) => {
        listeners.delete(eventName);
      },
    };

    const readyPromise = waitForDesktopBridge();
    globalThis.window.pywebview = { api: {} };
    listeners.get('pywebviewready')();

    await expect(readyPromise).resolves.toBe(true);
  });

  it('resolves false after timeout when the bridge never appears', async () => {
    globalThis.window = createDesktopWindowWithoutBridge();

    const readyPromise = waitForDesktopBridge(100);
    await vi.advanceTimersByTimeAsync(100);

    await expect(readyPromise).resolves.toBe(false);
  });
});

describe('desktop server management', () => {
  it('lists, adds, removes, and probes servers through the bridge', async () => {
    const servers = [
      { host: 'pi.lan', port: 8420, label: 'Home', active: true },
    ];
    const addServer = vi.fn(() => ({
      host: 'office.lan',
      port: 9000,
      label: 'Office',
    }));
    const removeServer = vi.fn(() => ({ removed: true }));
    const selectServer = vi.fn(() => ({
      status: 'server_unreachable',
      error_title: 'Server unreachable',
      error_body: 'Try again.',
    }));
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: {
        api: {
          listServers: () => servers,
          addServer,
          removeServer,
          selectServer,
        },
      },
    };

    await expect(listDesktopServers()).resolves.toEqual(servers);
    await expect(
      addDesktopServer('office.lan', 9000, 'Office'),
    ).resolves.toEqual({
      host: 'office.lan',
      port: 9000,
      label: 'Office',
    });
    await expect(removeDesktopServer('office.lan', 9000)).resolves.toEqual({
      removed: true,
    });
    await expect(selectDesktopServer('office.lan', 9000)).resolves.toEqual({
      status: 'server_unreachable',
      error_title: 'Server unreachable',
      error_body: 'Try again.',
    });
    expect(addServer).toHaveBeenCalledWith('office.lan', 9000, 'Office');
    expect(removeServer).toHaveBeenCalledWith('office.lan', 9000);
    expect(selectServer).toHaveBeenCalledWith('office.lan', 9000);
  });

  it('returns an empty list when the bridge returns no server array', async () => {
    globalThis.window = {
      location: { search: '?accessor=desktop' },
      pywebview: { api: { listServers: () => null } },
    };

    await expect(listDesktopServers()).resolves.toEqual([]);
  });
});

describe('Voice status validation', () => {
  it('keeps a complete snapshot as reported', () => {
    const raw = rawStatus({
      microphone: { index: 2, name: 'Desk mic', host_api: 'WASAPI' },
      active_microphone: {
        index: 2,
        name: 'Desk mic',
        host_api: 'WASAPI',
        sample_rate: 48000,
      },
      default_session_behavior: 'new',
      phrases: [
        {
          model_id: 'builtin/okay_nabu',
          label: 'Okay Nabu',
          sensitivity: 0.5,
          action: {
            type: 'command',
            agent_id: 'writer',
            session_behavior: 'new',
          },
          effective: {
            type: 'command',
            agent_id: 'writer',
            session_behavior: 'new',
          },
          problem: null,
        },
        {
          model_id: 'builtin/hey_jarvis',
          label: 'Hey Jarvis',
          sensitivity: 0.7,
          action: { type: 'live_voice', mode: 'start' },
          effective: { type: 'live_voice', mode: 'start' },
          problem: 'live_voice_unavailable',
        },
      ],
      recording: {
        command_id: 'c-1',
        model_id: 'builtin/okay_nabu',
        agent_id: 'writer',
      },
      commands: [
        { command_id: 'c-0', model_id: 'builtin/okay_nabu', stage: 'sending' },
      ],
      calibration: {
        model_id: 'builtin/okay_nabu',
        phase: 'phrases',
        score: 0.4,
        peak: 0.8,
        noise_level: 0.1,
        noise_high: true,
        sample_count: 2,
        required_samples: 3,
        recommended_sensitivity: null,
        noise_seconds_remaining: 0,
      },
    });

    expect(normalizeVoiceStatus(raw)).toEqual(raw);
  });

  it('rejects values that are not snapshots', () => {
    expect(normalizeVoiceStatus(null)).toBeNull();
    expect(normalizeVoiceStatus([])).toBeNull();
    expect(normalizeVoiceStatus(rawStatus({ sequence: undefined }))).toBeNull();
    expect(normalizeVoiceStatus(rawStatus({ sequence: -1 }))).toBeNull();
    expect(normalizeVoiceStatus(rawStatus({ sequence: 1.5 }))).toBeNull();
  });

  it('falls back to safe defaults for unknown or missing fields', () => {
    const status = normalizeVoiceStatus({
      sequence: 0,
      enabled: 'yes',
      state: 'paused',
      mode: 'turbo',
      echo_cancellation: { state: 'loud' },
      default_session_behavior: 'sometimes',
      phrases: [
        { model_id: 'a', action: { type: 'shout' }, effective: 7 },
        { model_id: 'a', label: 'Duplicate' },
        { label: 'No id' },
        {
          model_id: 'b',
          action: { type: 'live_voice', mode: 'forever' },
          sensitivity: 'high',
        },
      ],
      recording: { model_id: 'a' },
      commands: [{ stage: 'sending' }, { command_id: 'c-1' }],
      calibration: { model_id: 'a', phase: 'dance', required_samples: 0 },
      limits: {
        max_active_phrases: 0,
        min_sensitivity: 0.9,
        max_sensitivity: 0.1,
      },
    });

    expect(status).toMatchObject({
      enabled: false,
      state: 'off',
      mode: 'real',
      error_code: null,
      echo_cancellation: { enabled: true, state: 'off' },
      default_agent_id: null,
      default_session_behavior: 'active',
      recording: null,
      commands: [{ command_id: 'c-1', model_id: null, stage: null }],
      limits: {
        max_active_phrases: null,
        min_sensitivity: null,
        max_sensitivity: null,
      },
    });
    expect(status.phrases).toEqual([
      {
        model_id: 'a',
        label: 'a',
        sensitivity: null,
        action: { type: 'command', agent_id: null, session_behavior: null },
        effective: null,
        problem: null,
      },
      {
        model_id: 'b',
        label: 'b',
        sensitivity: null,
        action: { type: 'live_voice', mode: 'toggle' },
        effective: null,
        problem: null,
      },
    ]);
    expect(status.calibration).toMatchObject({
      model_id: 'a',
      phase: 'noise',
      required_samples: null,
      recommended_sensitivity: null,
    });
  });

  it('keeps every echo cancellation state the Desktop reports', () => {
    for (const state of [
      'off',
      'starting',
      'active',
      'no_reference',
      'unavailable',
    ]) {
      const raw = rawStatus({ echo_cancellation: { enabled: true, state } });
      expect(normalizeVoiceStatus(raw).echo_cancellation.state).toBe(state);
    }
  });

  it('accepts only events with a sequence and a well-formed kind', () => {
    expect(
      normalizeVoiceEvent({
        sequence: 5,
        kind: 'command_failed',
        model_id: 'builtin/okay_nabu',
        command_id: 'c-1',
        error_code: 'target_unavailable',
      }),
    ).toEqual({
      sequence: 5,
      kind: 'command_failed',
      model_id: 'builtin/okay_nabu',
      command_id: 'c-1',
      agent_id: null,
      session_id: null,
      error_code: 'target_unavailable',
    });
    // A kind a newer Desktop adds still passes.
    expect(normalizeVoiceEvent({ sequence: 6, kind: 'future_kind' })).not.toBe(
      null,
    );
    expect(normalizeVoiceEvent({ kind: 'sent' })).toBeNull();
    expect(normalizeVoiceEvent({ sequence: 1, kind: 'Sent!' })).toBeNull();
    expect(normalizeVoiceEvent({ sequence: 1, kind: '' })).toBeNull();
    expect(normalizeVoiceEvent({ sequence: 1, kind: 4 })).toBeNull();
    expect(normalizeVoiceEvent('sent')).toBeNull();
  });
});

describe('Voice bridge calls', () => {
  it('reads the status snapshot and rejects an invalid one', async () => {
    desktopWindow({ getVoiceStatus: () => rawStatus() });
    await expect(getVoiceStatus()).resolves.toEqual(rawStatus());

    desktopWindow({ getVoiceStatus: () => ({ state: 'listening' }) });
    await expect(getVoiceStatus()).rejects.toThrow(
      'The Desktop returned an invalid Voice status',
    );
  });

  it('propagates bridge absence instead of fabricating disabled state', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    await expect(getVoiceStatus()).rejects.toThrow(
      'Desktop bridge not available',
    );
  });

  it('enables Voice and reports the reason of a refusal', async () => {
    const setEnabled = vi
      .fn()
      .mockResolvedValueOnce({ enabled: true, error_code: null })
      .mockResolvedValueOnce({
        enabled: false,
        error_code: 'speech_to_text_unconfigured',
      })
      .mockResolvedValueOnce(null);
    desktopWindow({ setVoiceEnabled: setEnabled });

    await expect(setVoiceEnabled(true)).resolves.toEqual({
      enabled: true,
      error_code: null,
    });
    await expect(setVoiceEnabled(1)).resolves.toEqual({
      enabled: false,
      error_code: 'speech_to_text_unconfigured',
    });
    await expect(setVoiceEnabled(false)).resolves.toEqual({
      enabled: false,
      error_code: null,
    });
    expect(setEnabled.mock.calls).toEqual([[true], [true], [false]]);
  });

  it('sends a configuration change and resolves the resulting snapshot', async () => {
    const update = vi.fn(() => rawStatus({ sequence: 9 }));
    desktopWindow({ updateVoiceConfig: update });
    const changes = {
      model_sensitivities: { 'builtin/okay_nabu': 0.8 },
      phrase_actions: { 'builtin/hey_jarvis': null },
    };

    await expect(updateVoiceConfig(changes)).resolves.toMatchObject({
      sequence: 9,
    });
    expect(update).toHaveBeenCalledWith(changes);
  });

  it('controls the calibration of one phrase', async () => {
    const start = vi.fn(() =>
      rawStatus({
        sequence: 5,
        calibration: { model_id: 'builtin/okay_nabu', phase: 'noise' },
      }),
    );
    const restart = vi.fn(() =>
      rawStatus({
        sequence: 6,
        calibration: { model_id: 'builtin/okay_nabu', phase: 'noise' },
      }),
    );
    const stop = vi.fn(() => rawStatus({ sequence: 7 }));
    desktopWindow({
      startVoiceCalibration: start,
      restartVoiceCalibration: restart,
      stopVoiceCalibration: stop,
    });

    expect(
      (await startVoiceCalibration('builtin/okay_nabu')).calibration.model_id,
    ).toBe('builtin/okay_nabu');
    expect((await restartVoiceCalibration()).sequence).toBe(6);
    expect((await stopVoiceCalibration()).calibration).toBeNull();
    expect(start).toHaveBeenCalledWith('builtin/okay_nabu');
    expect(restart).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });

  it('retries listening, stops a recording and lists microphones', async () => {
    const devices = [{ index: 3, name: 'Desk mic', supported: true }];
    const retry = vi.fn(() => ({ retried: true }));
    const stop = vi.fn(() => ({ stopped: true }));
    desktopWindow({
      listMicrophones: () => devices,
      retryVoice: retry,
      stopVoiceRecording: stop,
    });

    await expect(listMicrophones()).resolves.toEqual(devices);
    await expect(retryVoice()).resolves.toEqual({ retried: true });
    await expect(stopVoiceRecording()).resolves.toEqual({ stopped: true });
    expect(retry).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });

  it('lists models with their overlaps, imports and deletes them', async () => {
    const importModel = vi.fn(() => ({ id: 'custom/model', activated: false }));
    const deleteModel = vi.fn(() => ({ deleted: true }));
    desktopWindow({
      listWakewordModels: () => [
        {
          id: 'builtin/okay_nabu',
          label: 'Okay Nabu',
          overlaps: ['custom/nabu', 3],
        },
        { id: 'custom/nabu', label: '' },
        { label: 'No id' },
      ],
      importWakewordModel: importModel,
      deleteWakewordModel: deleteModel,
    });

    await expect(listWakewordModels()).resolves.toEqual([
      {
        id: 'builtin/okay_nabu',
        label: 'Okay Nabu',
        overlaps: ['custom/nabu'],
      },
      { id: 'custom/nabu', label: 'custom/nabu', overlaps: [] },
    ]);
    await expect(
      importWakewordModel('computer.tflite', 'b25ueA=='),
    ).resolves.toEqual({ id: 'custom/model', activated: false });
    await expect(deleteWakewordModel('custom/model')).resolves.toEqual({
      deleted: true,
    });
    expect(importModel).toHaveBeenCalledWith('computer.tflite', 'b25ueA==');
    expect(deleteModel).toHaveBeenCalledWith('custom/model');
  });

  it('reads the stable error code of a rejected call', async () => {
    desktopWindow({
      updateVoiceConfig: () =>
        Promise.reject(new Error('voice_config_invalid')),
    });

    const rejected = await updateVoiceConfig({ echo_cancellation: 1 }).catch(
      (error) => error,
    );

    expect(desktopErrorCode(rejected)).toBe('voice_config_invalid');
    expect(desktopErrorCode(new Error('wakeword_model_active'))).toBe(
      'wakeword_model_active',
    );
  });

  it('finds no error code in other failures', () => {
    expect(desktopErrorCode(new Error('Desktop bridge not available'))).toBe(
      null,
    );
    expect(desktopErrorCode(new Error('Voice_config_invalid'))).toBeNull();
    expect(desktopErrorCode(new Error('2fast'))).toBeNull();
    expect(desktopErrorCode(new Error(''))).toBeNull();
    expect(desktopErrorCode({ message: 7 })).toBeNull();
    expect(desktopErrorCode('voice_config_invalid')).toBeNull();
    expect(desktopErrorCode(null)).toBeNull();
  });

  it('propagates model-list bridge failure so callers retain known state', async () => {
    globalThis.window = { location: { search: '' }, pywebview: undefined };

    await expect(listWakewordModels()).rejects.toThrow(
      'Desktop bridge not available',
    );
  });
});

describe('pushed Voice status and events', () => {
  function listen(handler) {
    const target = new EventTarget();
    globalThis.window = {
      addEventListener: target.addEventListener.bind(target),
      removeEventListener: target.removeEventListener.bind(target),
    };
    const cleanup = onDesktopVoicePush(handler);
    const dispatch = (detail) =>
      target.dispatchEvent(new CustomEvent('vbot-desktop-voice', { detail }));
    return { cleanup, dispatch };
  }

  it('hands validated snapshots and events to the handler', () => {
    const handler = vi.fn();
    const { cleanup, dispatch } = listen(handler);

    dispatch({ type: 'status', status: rawStatus({ sequence: 3 }) });
    dispatch({
      type: 'event',
      event: { sequence: 4, kind: 'recording_started', command_id: 'c-1' },
    });

    expect(handler.mock.calls).toEqual([
      [{ type: 'status', status: rawStatus({ sequence: 3 }) }],
      [
        {
          type: 'event',
          event: {
            sequence: 4,
            kind: 'recording_started',
            model_id: null,
            command_id: 'c-1',
            agent_id: null,
            session_id: null,
            error_code: null,
          },
        },
      ],
    ]);

    cleanup();
    dispatch({ type: 'status', status: rawStatus({ sequence: 5 }) });
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('drops malformed pushes', () => {
    const handler = vi.fn();
    const { cleanup, dispatch } = listen(handler);

    dispatch(null);
    dispatch({ type: 'status', status: { state: 'listening' } });
    dispatch({ type: 'event', event: { kind: 'sent' } });
    dispatch({ type: 'event', event: { sequence: 2, kind: 'DROP TABLE' } });
    dispatch({ type: 'snapshot', status: rawStatus() });

    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });

  it('returns noop cleanup without a window', () => {
    const savedWindow = globalThis.window;
    globalThis.window = undefined;
    try {
      const cleanup = onDesktopVoicePush(() => {});
      expect(cleanup()).toBeUndefined();
    } finally {
      globalThis.window = savedWindow;
    }
  });
});

describe('Voice cues', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('plays the tones of a cue kind and nothing for other kinds', async () => {
    const oscillators = [];
    class FakeAudioContext {
      state = 'running';
      currentTime = 0;
      destination = {};
      createOscillator() {
        const oscillator = {
          frequency: { value: 0 },
          connect: vi.fn(),
          start: vi.fn(),
          stop: vi.fn(),
        };
        oscillators.push(oscillator);
        return oscillator;
      }
      createGain() {
        return {
          gain: {
            setValueAtTime: vi.fn(),
            exponentialRampToValueAtTime: vi.fn(),
          },
          connect: vi.fn(),
        };
      }
    }
    globalThis.window = { AudioContext: FakeAudioContext };

    await playVoiceCue('sent');
    expect(oscillators.map((oscillator) => oscillator.frequency.value)).toEqual(
      [660, 880],
    );

    await playVoiceCue('recording_started');
    expect(oscillators).toHaveLength(2);
  });
});

function createDesktopWindowWithoutBridge() {
  const listeners = new Map();

  return {
    location: { search: '?accessor=desktop' },
    pywebview: undefined,
    addEventListener: (eventName, callback) => {
      listeners.set(eventName, callback);
    },
    removeEventListener: (eventName) => {
      listeners.delete(eventName);
    },
  };
}

describe('desktop Live voice integration', () => {
  let savedWindow;

  beforeEach(() => {
    savedWindow = globalThis.window;
  });

  afterEach(() => {
    globalThis.window = savedWindow;
    vi.restoreAllMocks();
  });

  describe('microphone access', () => {
    it('lets a secure page open the microphone without asking the bridge', async () => {
      const getDesktopCapabilities = vi.fn();
      desktopWindow({ getDesktopCapabilities });

      expect(await desktopMicrophoneAccess()).toBeNull();
      expect(getDesktopCapabilities).not.toHaveBeenCalled();
    });

    it('asks for a restart when this server was added after the Desktop started', async () => {
      desktopWindow(
        {
          getDesktopCapabilities: () => ({
            secureOrigins: ['http://other.lan:8420'],
          }),
        },
        { secure: false },
      );

      expect(await desktopMicrophoneAccess()).toBe('desktop_restart_required');
    });

    it('lets a server the Desktop trusted try the microphone', async () => {
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      desktopWindow(
        {
          getDesktopCapabilities: () => ({
            secureOrigins: ['http://pi.lan:8420'],
          }),
        },
        { secure: false },
      );

      expect(await desktopMicrophoneAccess()).toBeNull();
    });

    it('asks for a restart when the bridge fails or answers too late', async () => {
      vi.useFakeTimers();
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      try {
        desktopWindow(
          { getDesktopCapabilities: () => new Promise(() => {}) },
          { secure: false },
        );
        const access = desktopMicrophoneAccess({ timeoutMs: 100 });
        await vi.advanceTimersByTimeAsync(100);
        expect(await access).toBe('desktop_restart_required');

        desktopWindow(
          {
            getDesktopCapabilities: () =>
              Promise.reject(new Error('bridge busy')),
          },
          { secure: false },
        );
        expect(await desktopMicrophoneAccess()).toBe(
          'desktop_restart_required',
        );
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe('pushed Live voice requests', () => {
    function listen(handler) {
      const target = new EventTarget();
      globalThis.window = {
        addEventListener: target.addEventListener.bind(target),
        removeEventListener: target.removeEventListener.bind(target),
      };
      const cleanup = onDesktopLiveRequest(handler);
      const dispatch = (detail) =>
        !target.dispatchEvent(
          new CustomEvent('vbot-desktop-live', { cancelable: true, detail }),
        );
      return { cleanup, dispatch };
    }

    it('hands valid requests to the handler and acknowledges them', () => {
      const handler = vi.fn(() => true);
      const { cleanup, dispatch } = listen(handler);

      expect(dispatch({ action: 'toggle', source: 'hotkey' })).toBe(true);
      expect(dispatch({ action: 'start', source: 'wakeword' })).toBe(true);
      expect(handler.mock.calls).toEqual([
        [{ action: 'toggle', source: 'hotkey' }],
        [{ action: 'start', source: 'wakeword' }],
      ]);

      cleanup();
      expect(dispatch({ action: 'start', source: 'wakeword' })).toBe(false);
      expect(handler).toHaveBeenCalledTimes(2);
    });

    it('ignores malformed requests and reports declined ones as unhandled', () => {
      const handler = vi.fn(() => false);
      const { cleanup, dispatch } = listen(handler);

      expect(dispatch({ action: 'stop', source: 'hotkey' })).toBe(false);
      expect(dispatch({ action: 'start', source: 'server' })).toBe(false);
      expect(dispatch(null)).toBe(false);
      expect(handler).not.toHaveBeenCalled();

      expect(dispatch({ action: 'start', source: 'wakeword' })).toBe(false);
      expect(handler).toHaveBeenCalledOnce();
      cleanup();
    });
  });

  it('reads and changes the global shortcut through the bridge', async () => {
    const status = {
      supported: true,
      enabled: true,
      hotkey: { ctrl: true, alt: true, shift: false, win: false, key: 'Space' },
      error_code: null,
    };
    const api = {
      getLiveHotkey: vi.fn(async () => status),
      setLiveHotkey: vi.fn(async () => status),
    };
    desktopWindow(api);

    expect(await getDesktopLiveHotkey()).toBe(status);
    expect(await setDesktopLiveHotkey({ enabled: true })).toBe(status);
    expect(api.setLiveHotkey).toHaveBeenCalledWith({ enabled: true });
  });
});
