import { afterEach, describe, expect, it, vi } from 'vitest';

import {
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
  onDesktopOpenSession,
  getDesktopLiveHotkey,
  setDesktopLiveHotkey,
  getDesktopUpdate,
  restartDesktop,
  onDesktopUpdate,
  onDesktopRestartRequest,
} from '../desktopBridge.js';

const DISABLED_CAPABILITIES = {
  wakeword: false,
  serverSelection: false,
  contextMenu: false,
  voiceApi: 0,
  liveHotkey: false,
  restart: false,
  secureOrigins: [],
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete globalThis.window;
});

/** A Desktop page on the accessor URL whose bridge offers `api`. */
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

/** A window that dispatches real DOM events to its listeners. */
function eventWindow(properties = {}) {
  const target = new EventTarget();
  globalThis.window = {
    addEventListener: target.addEventListener.bind(target),
    removeEventListener: target.removeEventListener.bind(target),
    dispatchEvent: target.dispatchEvent.bind(target),
    ...properties,
  };
  return globalThis.window;
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
  const pageWindow = (search, api) => ({
    location: { search },
    pywebview: api && { api },
  });

  it.each([
    ['without a window', undefined, false],
    ['in a browser', pageWindow(''), false],
    [
      'on the accessor URL before the bridge is ready',
      pageWindow('?accessor=desktop'),
      true,
    ],
    ['with a bridge but no accessor URL', pageWindow('', {}), false],
  ])('detects the Desktop %s', (_label, page, accessor) => {
    globalThis.window = page;

    expect(isDesktopAccessor()).toBe(accessor);
  });
});

describe('waitForDesktopBridge', () => {
  it.each([
    ['false outside the accessor URL', { location: { search: '' } }, false],
    [
      'true when the bridge already exists',
      { location: { search: '?accessor=desktop' }, pywebview: { api: {} } },
      true,
    ],
  ])('resolves %s at once', async (_label, page, ready) => {
    globalThis.window = page;

    await expect(waitForDesktopBridge()).resolves.toBe(ready);
  });

  it('waits for pywebviewready before resolving in desktop mode', async () => {
    const page = eventWindow({ location: { search: '?accessor=desktop' } });

    const ready = waitForDesktopBridge();
    page.pywebview = { api: {} };
    page.dispatchEvent(new Event('pywebviewready'));

    await expect(ready).resolves.toBe(true);
  });

  it('resolves false after the timeout when the bridge never appears', async () => {
    vi.useFakeTimers();
    eventWindow({ location: { search: '?accessor=desktop' } });

    const ready = waitForDesktopBridge(100);
    await vi.advanceTimersByTimeAsync(100);

    await expect(ready).resolves.toBe(false);
  });
});

describe('getDesktopCapabilities', () => {
  it('normalizes the capabilities and caches them per bridge', async () => {
    const reportCapabilities = vi.fn(() => ({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
      voiceApi: 2,
      liveHotkey: 1,
      restart: true,
      secureOrigins: ['http://pi.lan:8420', 42, null],
    }));
    desktopWindow({ getDesktopCapabilities: reportCapabilities });

    const capabilities = await getDesktopCapabilities();
    expect(capabilities).toEqual({
      wakeword: true,
      serverSelection: true,
      contextMenu: true,
      voiceApi: 2,
      liveHotkey: true,
      restart: true,
      secureOrigins: ['http://pi.lan:8420'],
    });
    expect(await getDesktopCapabilities()).toBe(capabilities);
    expect(reportCapabilities).toHaveBeenCalledOnce();

    // A new bridge object (after a reload of the shell) is asked again.
    desktopWindow({ getDesktopCapabilities: () => ({ wakeword: false }) });
    expect(await getDesktopCapabilities()).toEqual(DISABLED_CAPABILITIES);
  });

  it('reads an invalid Voice bridge version as none', async () => {
    for (const voiceApi of ['2', 1.5, -2, null]) {
      desktopWindow({
        getDesktopCapabilities: () => ({ wakeword: true, voiceApi }),
      });
      expect((await getDesktopCapabilities()).voiceApi).toBe(0);
    }
  });

  it('returns disabled capabilities without a bridge', async () => {
    globalThis.window = { location: { search: '' } };

    const capabilities = await getDesktopCapabilities();
    expect(capabilities).toEqual(DISABLED_CAPABILITIES);
    expect(disabledDesktopCapabilities()).toEqual(capabilities);
  });

  it('propagates a transient capability failure so callers can retry', async () => {
    desktopWindow({
      getDesktopCapabilities: () =>
        Promise.reject(new Error('bridge starting')),
    });

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

describe('Desktop bridge calls', () => {
  // The bridge method names are the contract with the Desktop's Python API.
  it.each([
    [
      'setClipboardText',
      setDesktopClipboardText,
      ['copy me'],
      { copied: true },
    ],
    ['getClipboardText', getDesktopClipboardText, [], 'paste me'],
    [
      'openExternalUrl',
      openDesktopExternalUrl,
      ['https://example.com/path'],
      { opened: true },
    ],
    [
      'listServers',
      listDesktopServers,
      [],
      [{ host: 'pi.lan', port: 8420, label: 'Home', active: true }],
    ],
    [
      'addServer',
      addDesktopServer,
      ['office.lan', 9000, 'Office'],
      { host: 'office.lan', port: 9000, label: 'Office' },
    ],
    [
      'removeServer',
      removeDesktopServer,
      ['office.lan', 9000],
      { removed: true },
    ],
    [
      'selectServer',
      selectDesktopServer,
      ['office.lan', 9000],
      {
        status: 'server_unreachable',
        error_title: 'Server unreachable',
        error_body: 'Try again.',
      },
    ],
    [
      'updateVoiceConfig',
      updateVoiceConfig,
      [
        {
          model_sensitivities: { 'builtin/okay_nabu': 0.8 },
          phrase_actions: { 'builtin/hey_jarvis': null },
        },
      ],
      rawStatus({ sequence: 9 }),
    ],
    [
      'listMicrophones',
      listMicrophones,
      [],
      [{ index: 3, name: 'Desk mic', supported: true }],
    ],
    [
      'importWakewordModel',
      importWakewordModel,
      ['computer.tflite', 'b25ueA=='],
      { id: 'custom/model', activated: false },
    ],
    [
      'deleteWakewordModel',
      deleteWakewordModel,
      ['custom/model'],
      { deleted: true },
    ],
    ['retryVoice', retryVoice, [], { retried: true }],
    ['stopVoiceRecording', stopVoiceRecording, [], { stopped: true }],
    [
      'startVoiceCalibration',
      startVoiceCalibration,
      ['builtin/okay_nabu'],
      rawStatus({ sequence: 5 }),
    ],
    [
      'restartVoiceCalibration',
      restartVoiceCalibration,
      [],
      rawStatus({ sequence: 6 }),
    ],
    [
      'stopVoiceCalibration',
      stopVoiceCalibration,
      [],
      rawStatus({ sequence: 7 }),
    ],
    [
      'getLiveHotkey',
      getDesktopLiveHotkey,
      [],
      { supported: true, enabled: true, hotkey: null, error_code: null },
    ],
    [
      'setLiveHotkey',
      setDesktopLiveHotkey,
      [{ enabled: true }],
      { supported: true, enabled: true, hotkey: null, error_code: null },
    ],
    [
      'getDesktopUpdate',
      getDesktopUpdate,
      [],
      { pending: true, restarting: false, failed: true },
    ],
    ['restartDesktop', restartDesktop, [], { accepted: true }],
  ])('calls %s and resolves its answer', async (method, call, args, answer) => {
    const bridgeMethod = vi.fn(async () => answer);
    desktopWindow({ [method]: bridgeMethod });

    await expect(call(...args)).resolves.toEqual(answer);
    expect(bridgeMethod.mock.calls).toEqual([args]);
  });

  it.each([
    ['getClipboardText', getDesktopClipboardText, ''],
    ['listServers', listDesktopServers, []],
    ['listMicrophones', listMicrophones, []],
    ['listWakewordModels', listWakewordModels, []],
    [
      'getDesktopUpdate',
      getDesktopUpdate,
      { pending: false, restarting: false, failed: false },
    ],
  ])('reads a malformed %s answer as empty', async (method, call, empty) => {
    desktopWindow({ [method]: () => null });

    await expect(call()).resolves.toEqual(empty);
  });

  it('rejects without the bridge instead of fabricating a disabled state', async () => {
    globalThis.window = { location: { search: '' } };

    await expect(getVoiceStatus()).rejects.toThrow(
      'Desktop bridge not available',
    );
    await expect(listWakewordModels()).rejects.toThrow(
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

  it('lists models with their overlaps and a label for each', async () => {
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
    });

    await expect(listWakewordModels()).resolves.toEqual([
      {
        id: 'builtin/okay_nabu',
        label: 'Okay Nabu',
        overlaps: ['custom/nabu'],
      },
      { id: 'custom/nabu', label: 'custom/nabu', overlaps: [] },
    ]);
  });

  it('reads the stable error code of a rejected call and nothing else', async () => {
    desktopWindow({
      updateVoiceConfig: () =>
        Promise.reject(new Error('voice_config_invalid')),
    });

    const rejected = await updateVoiceConfig({ echo_cancellation: 1 }).catch(
      (error) => error,
    );

    expect(desktopErrorCode(rejected)).toBe('voice_config_invalid');
    for (const failure of [
      new Error('Desktop bridge not available'),
      new Error('Voice_config_invalid'),
      new Error('2fast'),
      new Error(''),
      { message: 7 },
      'voice_config_invalid',
      null,
    ]) {
      expect(desktopErrorCode(failure)).toBeNull();
    }
  });
});

describe('Voice status validation', () => {
  /** Read `raw` through the Desktop's status call. */
  function readStatus(raw) {
    desktopWindow({ getVoiceStatus: () => raw });
    return getVoiceStatus();
  }

  it('keeps a complete snapshot as reported', async () => {
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

    await expect(readStatus(raw)).resolves.toEqual(raw);
  });

  it('rejects values that are not snapshots', async () => {
    for (const raw of [
      null,
      [],
      { state: 'listening' },
      rawStatus({ sequence: undefined }),
      rawStatus({ sequence: -1 }),
      rawStatus({ sequence: 1.5 }),
    ]) {
      await expect(readStatus(raw)).rejects.toThrow(
        'The Desktop returned an invalid Voice status',
      );
    }
  });

  it('falls back to safe defaults for unknown or missing fields', async () => {
    const status = await readStatus({
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

  it('keeps every echo cancellation state the Desktop reports', async () => {
    for (const state of [
      'off',
      'starting',
      'active',
      'no_reference',
      'unavailable',
    ]) {
      const raw = rawStatus({ echo_cancellation: { enabled: true, state } });
      const status = await readStatus(raw);
      expect(status.echo_cancellation.state).toBe(state);
    }
  });
});

describe('pushed Voice status and events', () => {
  function listen(handler) {
    const page = eventWindow();
    const cleanup = onDesktopVoicePush(handler);
    const dispatch = (detail) =>
      page.dispatchEvent(new CustomEvent('vbot-desktop-voice', { detail }));
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

  it('accepts only events with a sequence and a well-formed kind', () => {
    const handler = vi.fn();
    const { cleanup, dispatch } = listen(handler);

    for (const event of [
      {
        sequence: 5,
        kind: 'command_failed',
        model_id: 'builtin/okay_nabu',
        command_id: 'c-1',
        error_code: 'target_unavailable',
      },
      // A kind a newer Desktop adds still passes.
      { sequence: 6, kind: 'future_kind' },
      { kind: 'sent' },
      { sequence: 1, kind: 'Sent!' },
      { sequence: 1, kind: '' },
      { sequence: 1, kind: 4 },
      { sequence: 2, kind: 'DROP TABLE' },
      'sent',
    ]) {
      dispatch({ type: 'event', event });
    }

    expect(handler.mock.calls.map(([push]) => push.event)).toEqual([
      {
        sequence: 5,
        kind: 'command_failed',
        model_id: 'builtin/okay_nabu',
        command_id: 'c-1',
        agent_id: null,
        session_id: null,
        error_code: 'target_unavailable',
      },
      {
        sequence: 6,
        kind: 'future_kind',
        model_id: null,
        command_id: null,
        agent_id: null,
        session_id: null,
        error_code: null,
      },
    ]);
    cleanup();
  });

  it('drops malformed pushes', () => {
    const handler = vi.fn();
    const { cleanup, dispatch } = listen(handler);

    dispatch(null);
    dispatch({ type: 'status', status: { state: 'listening' } });
    dispatch({ type: 'snapshot', status: rawStatus() });

    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });

  it('returns a noop cleanup without a window', () => {
    const cleanup = onDesktopVoicePush(() => {});

    expect(cleanup()).toBeUndefined();
  });
});

describe('Voice cues', () => {
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

describe('desktop Live voice integration', () => {
  describe('microphone access', () => {
    it('lets a secure page or a server the Desktop trusted open the microphone', async () => {
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      const reportCapabilities = vi.fn();
      desktopWindow({ getDesktopCapabilities: reportCapabilities });

      expect(await desktopMicrophoneAccess()).toBeNull();
      expect(reportCapabilities).not.toHaveBeenCalled();

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

    it.each([
      [
        'this server was added after the Desktop started',
        () => ({ secureOrigins: ['http://other.lan:8420'] }),
      ],
      ['the bridge fails', () => Promise.reject(new Error('bridge busy'))],
      ['the bridge answers too late', () => new Promise(() => {})],
    ])('asks for a restart when %s', async (_label, reportCapabilities) => {
      vi.useFakeTimers();
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      desktopWindow(
        { getDesktopCapabilities: reportCapabilities },
        { secure: false },
      );

      const access = desktopMicrophoneAccess({ timeoutMs: 100 });
      await vi.advanceTimersByTimeAsync(100);

      expect(await access).toBe('desktop_restart_required');
    });
  });

  describe('pushed Live voice requests', () => {
    function listen(handler) {
      const page = eventWindow();
      const cleanup = onDesktopLiveRequest(handler);
      // True when the page handled the request (cancelled the event).
      const dispatch = (detail) =>
        !page.dispatchEvent(
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
});

describe('pushed Session requests', () => {
  it('hands valid requests to the handler, acknowledges them, and ignores malformed ones', () => {
    const page = eventWindow();
    const handler = vi.fn();
    const cleanup = onDesktopOpenSession(handler);
    // True when the page handled the request (cancelled the event).
    const dispatch = (detail) =>
      !page.dispatchEvent(
        new CustomEvent('vbot-desktop-open-session', {
          cancelable: true,
          detail,
        }),
      );

    expect(dispatch({ agent: 'builder@project', session: 'session-1' })).toBe(
      true,
    );
    expect(dispatch({ agent: '', session: 'session-1' })).toBe(false);
    expect(dispatch({ agent: 'builder', session: 7 })).toBe(false);
    expect(dispatch(null)).toBe(false);
    expect(handler.mock.calls).toEqual([
      [{ agentId: 'builder@project', sessionId: 'session-1' }],
    ]);

    handler.mockReturnValueOnce(false);
    expect(dispatch({ agent: 'builder', session: 'session-2' })).toBe(false);
    cleanup();
    expect(dispatch({ agent: 'builder', session: 'session-3' })).toBe(false);
    expect(handler).toHaveBeenCalledTimes(2);
  });
});

describe('Desktop application update', () => {
  it('hands validated update pushes to the handler and drops malformed ones', () => {
    const page = eventWindow();
    const handler = vi.fn();
    const cleanup = onDesktopUpdate(handler);
    const push = (detail) =>
      page.dispatchEvent(new CustomEvent('vbot-desktop-update', { detail }));

    push({ pending: true, restarting: 'yes', failed: false });
    push(null);
    push('pending');
    expect(handler.mock.calls).toEqual([
      [{ pending: true, restarting: false, failed: false }],
    ]);

    cleanup();
    push({ pending: true, restarting: true, failed: false });
    expect(handler).toHaveBeenCalledOnce();
  });

  it('acknowledges restart requests unless the handler reports them unhandled', () => {
    const page = eventWindow();
    const handler = vi.fn();
    const cleanup = onDesktopRestartRequest(handler);
    // True when the page took the restart over (cancelled the event).
    const dispatch = (detail) =>
      !page.dispatchEvent(
        new CustomEvent('vbot-desktop-restart', { cancelable: true, detail }),
      );

    expect(dispatch({ reason: 'update' })).toBe(true);
    // A restart request needs nothing from its detail to be handled.
    expect(dispatch(null)).toBe(true);
    handler.mockReturnValueOnce(false);
    expect(dispatch({ reason: 'update' })).toBe(false);
    expect(handler.mock.calls).toEqual([
      [{ reason: 'update' }],
      [{ reason: null }],
      [{ reason: 'update' }],
    ]);

    cleanup();
    expect(dispatch({ reason: 'update' })).toBe(false);
    expect(handler).toHaveBeenCalledTimes(3);
  });
});
