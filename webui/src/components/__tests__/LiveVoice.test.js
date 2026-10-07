// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';
import { claimMicrophone } from '../../lib/microphoneUse.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { status, factory, desktop } = vi.hoisted(() => ({
  status: vi.fn(),
  factory: vi.fn(),
  desktop: {
    isDesktopAccessor: () => false,
    desktopMicrophoneAccess: async () => null,
    onDesktopLiveRequest: () => () => {},
  },
}));
vi.mock('$lib/api.js', () => ({ getLiveVoiceStatus: status }));
vi.mock('$lib/desktopBridge.js', () => desktop);
vi.mock('$lib/liveVoice.js', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    createLiveVoice: (...args) =>
      factory(...args) ?? actual.createLiveVoice(...args),
  };
});
const { default: LiveVoice } = await import('../LiveVoice.svelte');

let component;
let fake;
let desktopRequest;
const stopDesktopRequests = vi.fn();

function render(props = {}) {
  component = mount(LiveVoice, {
    target: document.body,
    props: { configured: true, ...props },
  });
  flushSync();
}

// Mounts with reactive props so a test can change them after mount.
function renderReactive(initial) {
  const props = reactiveProps(initial);
  component = mount(LiveVoice, { target: document.body, props });
  flushSync();
  return props;
}

function simulateController() {
  factory.mockImplementation(
    ({ state, onNotice, uiActions, checkMicrophoneAccess, wakePhrases }) => {
      const holds = new Set();
      fake = {
        state,
        onNotice,
        uiActions,
        checkMicrophoneAccess,
        wakePhrases,
        start: vi.fn(async () => {
          state.phase = 'connecting';
        }),
        stop: vi.fn(() => {
          state.phase = 'off';
        }),
        mute: vi.fn(() => {
          state.muted = !state.muted;
        }),
        muteSpeaker: vi.fn(() => {
          state.speakerMuted = !state.speakerMuted;
        }),
        stay: vi.fn(() => true),
        hold: vi.fn((reason) => {
          holds.add(reason);
          state.held = true;
          return true;
        }),
        release: vi.fn((reason) => {
          holds.delete(reason);
          state.held = holds.size > 0;
        }),
        held: (reason) => holds.has(reason),
        reportContext: vi.fn(),
        active: () => state.phase === 'live',
        destroy: vi.fn(),
        handleFrame: vi.fn(),
      };
      return fake;
    },
  );
}

const toggle = () => document.querySelector('.live-voice__toggle');
const muteButton = () =>
  document.querySelector(`button[aria-label="${t('live.mute')}"]`);
const caption = () => document.querySelector('.live-voice__caption');
const button = (label) =>
  document.querySelector(`button[aria-label="${label}"]`);
const panel = () => document.querySelector('.live-activity');

async function settle() {
  await vi.waitFor(() => {
    flushSync();
  });
  await Promise.resolve();
  flushSync();
}

beforeEach(() => {
  init('en');
  factory.mockReset();
  status.mockReset();
  desktop.isDesktopAccessor = vi.fn(() => false);
  desktop.desktopMicrophoneAccess = vi.fn(async () => null);
  desktop.onDesktopLiveRequest = vi.fn((handler) => {
    desktopRequest = handler;
    return stopDesktopRequests;
  });
  stopDesktopRequests.mockReset();
  desktopRequest = null;
  fake = null;
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('sidebar Live control', () => {
  it('is absent until a Live voice Model is configured', () => {
    render({ configured: false });
    expect(document.querySelector('button')).toBeNull();
    expect(status).not.toHaveBeenCalled();
  });

  it('starts and stops the call from one control', async () => {
    simulateController();
    render();
    expect(toggle().getAttribute('aria-label')).toBe(t('live.startButton'));
    expect(muteButton()).toBeNull();
    toggle().click();
    await settle();
    expect(fake.start).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe(t('live.stopButton'));
    expect(muteButton()).not.toBeNull();
    expect(caption().dataset.role).toBe('status');
    toggle().click();
    flushSync();
    expect(fake.stop).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe(t('live.startButton'));
    expect(caption()).toBeNull();
  });

  it('shows the latest caption, busy activity and the mute state', () => {
    simulateController();
    render();
    fake.state.phase = 'live';
    fake.state.captions = [
      { role: 'user', text: 'Open my Terminals', final: true },
      { role: 'assistant', text: 'Opening Terminals now', final: false },
    ];
    fake.state.busy = true;
    flushSync();
    expect(caption().textContent).toBe('Opening Terminals now');
    expect(caption().dataset.role).toBe('assistant');
    expect(document.querySelector('[role="status"]')).not.toBeNull();

    expect(muteButton().getAttribute('aria-pressed')).toBe('false');
    muteButton().click();
    flushSync();
    expect(fake.mute).toHaveBeenCalledOnce();
    expect(muteButton().getAttribute('aria-pressed')).toBe('true');

    fake.state.phase = 'closing';
    flushSync();
    expect(toggle().disabled).toBe(true);
    expect(muteButton().disabled).toBe(true);
    expect(caption().dataset.role).toBe('status');
  });

  it('shows the call time, silences the assistant and keeps an idle call', async () => {
    simulateController();
    render();
    toggle().click();
    await settle();
    Object.assign(fake.state, {
      phase: 'live',
      liveSince: Date.now() - 65_000,
    });
    flushSync();
    expect(document.querySelector('.live-voice__time').textContent).toBe(
      '1:05',
    );

    button(t('live.speakerMute')).click();
    flushSync();
    expect(fake.muteSpeaker).toHaveBeenCalledOnce();
    expect(button(t('live.speakerUnmute')).getAttribute('aria-pressed')).toBe(
      'true',
    );

    // A warning outranks speech and offers to keep the call.
    fake.state.captions = [
      { seq: 1, role: 'assistant', text: 'Hi', final: true },
    ];
    fake.state.idleEndsAt = Date.now() + 45_000;
    flushSync();
    expect(caption().textContent).toBe(t('live.state.idle', { time: '0:45' }));
    expect(caption().dataset.role).toBe('warning');
    document.querySelector('.live-voice__stay').click();
    expect(fake.stay).toHaveBeenCalledOnce();
    fake.state.idleEndsAt = null;
    fake.state.expiresAt = Date.now() + 90_000;
    flushSync();
    expect(caption().textContent).toBe(
      t('live.state.expiring', { time: '1:30' }),
    );
    fake.state.expiresAt = Date.now() + 600_000;
    flushSync();
    expect(caption().textContent).toBe('Hi');
  });

  it('shows what was said and done, with links into the app', async () => {
    simulateController();
    const onToast = vi.fn();
    const uiActions = {
      context: () => null,
      open: vi.fn(async () => true),
      terminalView: vi.fn(async () => {
        throw new Error('terminal_view_unavailable');
      }),
    };
    render({ uiActions, onToast });
    toggle().click();
    await settle();
    fake.state.phase = 'live';
    fake.state.captions = [
      { seq: 1, role: 'user', text: 'Start Coder on the tests', final: true },
      { seq: 3, role: 'assistant', text: 'Coder is on it.', final: true },
    ];
    fake.state.actions = [
      {
        seq: 2,
        tool: 'start_agent_session',
        ok: true,
        arguments: { agent: 'coder', task: 'Fix the tests' },
        result: 'Started a Session at Coder with the task: s1.',
        links: [
          {
            ref: 's1',
            kind: 'session',
            label: 'Session at Coder',
            agent_id: 'coder',
            session_id: 'ses_1',
          },
          {
            ref: 't1',
            kind: 'terminal',
            label: 'Build',
            terminal_id: 'term_a',
          },
        ],
      },
    ];
    flushSync();
    button(t('live.activity.show')).click();
    flushSync();

    const items = [...panel().querySelectorAll('li')];
    expect(items.map((item) => item.classList[0])).toEqual([
      'live-activity__caption',
      'live-activity__action',
      'live-activity__caption',
    ]);
    expect(items[1].textContent).toContain(t('live.tool.start_agent_session'));
    expect(items[1].textContent).toContain('coder · Fix the tests');
    const [session, terminal] = items[1].querySelectorAll('button');
    session.click();
    await settle();
    expect(uiActions.open).toHaveBeenCalledWith(
      { view: 'chat', agent_id: 'coder', session_id: 'ses_1' },
      expect.objectContaining({ isCurrent: expect.any(Function) }),
    );
    terminal.click();
    await settle();
    expect(uiActions.terminalView).toHaveBeenCalledWith(
      { op: 'show', terminal_id: 'term_a' },
      expect.anything(),
    );
    expect(onToast).toHaveBeenCalledExactlyOnceWith({
      title: t('live.title'),
      message: t('live.error.link'),
      variant: 'warn',
    });

    // The call's own Sessions open in Chat; the backend's once it exists.
    const sessionLink = (which) =>
      panel().querySelector(`[data-live-session="${which}"]`);
    fake.state.sessions = {
      voice: { agent_id: 'live-voice', session_id: 'call-1' },
      backend: null,
    };
    flushSync();
    expect(sessionLink('voice').textContent).toBe(
      t('live.activity.voiceSession'),
    );
    expect(sessionLink('backend')).toBeNull();
    fake.state.sessions = {
      ...fake.state.sessions,
      backend: { agent_id: 'live-backend', session_id: 'call-2' },
    };
    flushSync();
    sessionLink('backend').click();
    await settle();
    expect(uiActions.open).toHaveBeenLastCalledWith(
      { view: 'chat', agent_id: 'live-backend', session_id: 'call-2' },
      expect.anything(),
    );

    // The record stays readable after the call, until Escape closes it.
    fake.state.phase = 'off';
    flushSync();
    expect(panel().querySelector('h2').textContent).toBe(
      t('live.activity.lastCall'),
    );
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(panel()).toBeNull();
    expect(button(t('live.activity.show'))).not.toBeNull();
  });

  it('holds a running call while the page records, and asks before leaving it', async () => {
    simulateController();
    render();
    const leave = () => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(leave()).toBe(false);
    toggle().click();
    await settle();
    fake.state.phase = 'live';
    flushSync();
    expect(leave()).toBe(true);

    const release = claimMicrophone();
    flushSync();
    expect(fake.hold).toHaveBeenCalledExactlyOnceWith('recording');
    expect(caption().textContent).toBe(t('live.state.heldRecording'));
    release();
    flushSync();
    expect(fake.release).toHaveBeenCalledExactlyOnceWith('recording');

    toggle().click();
    flushSync();
    expect(leave()).toBe(false);
  });

  it('passes the App UI actions and what the app shows to the controller', () => {
    simulateController();
    const shown = { view: 'chat', selected_agent_id: 'main' };
    const uiActions = {
      context: vi.fn(() => shown),
      open: vi.fn(),
      terminalView: vi.fn(),
    };
    render({ uiActions });
    expect(fake.uiActions).toBe(uiActions);
    expect(fake.reportContext).toHaveBeenCalledWith(shown);
  });

  it('turns each controller notice into a shared toast with its message and severity', () => {
    simulateController();
    const onToast = vi.fn();
    render({ onToast });
    const messageKeys = {
      not_configured: 'live.error.notConfigured',
      not_usable: 'live.error.notUsable',
      invalid_offer: 'live.error.invalidOffer',
      access_denied: 'live.error.access',
      rate_limited: 'live.error.rateLimited',
      outcome_unknown: 'live.error.unknown',
      provider_error: 'live.error.provider',
      control_failed: 'live.error.control',
      microphone_denied: 'live.error.permission',
      microphone_unavailable: 'live.error.microphone',
      connection_timeout: 'live.error.timeout',
      connection_failed: 'live.error.connection',
      connection_lost: 'live.error.connectionLost',
      call_failed: 'live.error.callFailed',
      playback_blocked: 'live.error.playback',
      audio_unsupported: 'live.error.audioUnsupported',
      media_mismatch: 'live.error.mediaMismatch',
      desktop_restart_required: 'live.error.desktopRestart',
      ui_action_failed: 'live.error.uiAction',
      notification_failed: 'live.error.notification',
      provider_unavailable: 'live.error.providerUnavailable',
      backend_unavailable: 'live.error.backendUnavailable',
      link_failed: 'live.error.link',
      replaced: 'live.notice.replaced',
      ended: 'live.notice.ended',
      hung_up: 'live.notice.hungUp',
      idle: 'live.notice.idle',
      idle_warning: 'live.notice.idleWarning',
      expired: 'live.notice.expired',
    };
    for (const code of Object.keys(messageKeys))
      fake.onNotice({ code, severity: 'error' });
    fake.onNotice({ code: 'announcement_failed', severity: 'warn' });
    fake.onNotice({ code: 'replaced', severity: 'info' });

    const toast = (message, variant) => ({
      title: t('live.title'),
      message,
      variant,
    });
    expect(onToast.mock.calls.map(([shown]) => shown)).toEqual([
      ...Object.values(messageKeys).map((key) => toast(t(key), 'error')),
      // Codes without a dedicated message still identify the problem.
      toast(t('live.error.generic', { code: 'announcement_failed' }), 'warn'),
      toast(t('live.notice.replaced'), 'info'),
    ]);
  });

  it('surfaces a missing Live voice binding from the real controller', async () => {
    status.mockResolvedValue({
      configured: false,
      usable: false,
      target: null,
    });
    const onToast = vi.fn();
    render({ onToast });
    toggle().click();
    await vi.waitFor(() => {
      flushSync();
      expect(onToast).toHaveBeenCalledOnce();
    });
    expect(onToast.mock.calls[0][0]).toMatchObject({
      message: t('live.error.notConfigured'),
      variant: 'error',
    });
    expect(toggle().getAttribute('aria-label')).toBe(t('live.startButton'));
  });
});

describe('Live voice in the Desktop app', () => {
  it('stays browser-only outside the Desktop accessor', () => {
    simulateController();
    render();
    expect(fake.checkMicrophoneAccess).toBeNull();
    expect(fake.wakePhrases()).toEqual([]);
    expect(desktop.onDesktopLiveRequest).not.toHaveBeenCalled();
  });

  it('checks Desktop microphone access and takes Desktop requests', async () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    desktop.desktopMicrophoneAccess.mockResolvedValue(
      'desktop_restart_required',
    );
    render();
    await expect(fake.checkMicrophoneAccess()).resolves.toBe(
      'desktop_restart_required',
    );
    expect(desktop.onDesktopLiveRequest).toHaveBeenCalledOnce();

    await unmount(component);
    component = null;
    expect(stopDesktopRequests).toHaveBeenCalledOnce();
    expect(fake.destroy).toHaveBeenCalledOnce();
  });

  it('starts from a wakeword and ignores it while a call runs', async () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    render();

    expect(desktopRequest({ action: 'start', source: 'wakeword' })).toBe(true);
    await settle();
    expect(fake.start).toHaveBeenCalledOnce();
    expect(desktopRequest({ action: 'start', source: 'wakeword' })).toBe(true);
    expect(fake.start).toHaveBeenCalledOnce();
    expect(fake.stop).not.toHaveBeenCalled();
  });

  it('toggles the call from the global shortcut but leaves a closing call alone', async () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    render();

    desktopRequest({ action: 'toggle', source: 'hotkey' });
    await settle();
    expect(fake.start).toHaveBeenCalledOnce();

    fake.state.phase = 'closing';
    desktopRequest({ action: 'toggle', source: 'hotkey' });
    expect(fake.stop).not.toHaveBeenCalled();

    fake.state.phase = 'live';
    desktopRequest({ action: 'toggle', source: 'hotkey' });
    flushSync();
    expect(fake.stop).toHaveBeenCalledOnce();
    expect(fake.start).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe(t('live.startButton'));
  });

  it('explains a missing Live voice Model instead of starting', () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    const onToast = vi.fn();
    render({ configured: false, onToast });

    expect(desktopRequest({ action: 'start', source: 'wakeword' })).toBe(true);
    expect(fake.start).not.toHaveBeenCalled();
    expect(onToast).toHaveBeenCalledOnce();
    expect(onToast.mock.calls[0][0].message).toBe(
      t('live.error.notConfigured'),
    );
  });

  it('ignores requests while the server is unavailable', () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    const onToast = vi.fn();
    render({ serverUnavailable: true, onToast });

    expect(desktopRequest({ action: 'toggle', source: 'hotkey' })).toBe(false);
    expect(fake.start).not.toHaveBeenCalled();
    expect(onToast).not.toHaveBeenCalled();
  });
});

describe('Live voice with Desktop Voice', () => {
  const COMMAND = {
    type: 'command',
    agent_id: 'main',
    session_behavior: 'active',
  };

  function voiceStatus(overrides = {}) {
    return {
      enabled: true,
      state: 'listening',
      sequence: 3,
      phrases: [
        {
          model_id: 'builtin/okay_nabu',
          label: 'Okay Nabu',
          effective: COMMAND,
        },
        {
          model_id: 'builtin/hey_jarvis',
          label: 'Hey Jarvis',
          effective: { type: 'live_voice', mode: 'toggle' },
        },
      ],
      recording: null,
      ...overrides,
    };
  }
  const RECORDING = { command_id: 'c-1', model_id: 'builtin/okay_nabu' };

  it('names the command wake phrases of the current status to the call', () => {
    simulateController();
    const props = renderReactive({ configured: true, voiceStatus: null });
    expect(fake.wakePhrases()).toEqual([]);

    props.voiceStatus = voiceStatus();
    expect(fake.wakePhrases()).toEqual(['Okay Nabu']);

    props.voiceStatus = voiceStatus({ state: 'microphone_disconnected' });
    expect(fake.wakePhrases()).toEqual(['Okay Nabu']);

    props.voiceStatus = voiceStatus({ enabled: false });
    expect(fake.wakePhrases()).toEqual([]);
  });

  it('holds a running call while a command is recorded', async () => {
    simulateController();
    const props = renderReactive({
      configured: true,
      voiceStatus: voiceStatus(),
    });
    toggle().click();
    await settle();
    fake.state.phase = 'live';
    fake.state.captions = [{ role: 'assistant', text: 'Hello', final: true }];
    flushSync();

    props.voiceStatus = voiceStatus({ sequence: 4, recording: RECORDING });
    flushSync();
    expect(fake.hold).toHaveBeenCalledExactlyOnceWith('wakeword');
    expect(caption().textContent).toBe(t('live.state.held'));
    expect(caption().dataset.role).toBe('status');

    // A newer snapshot of the same recording keeps the one hold.
    props.voiceStatus = voiceStatus({ sequence: 5, recording: RECORDING });
    flushSync();
    expect(fake.hold).toHaveBeenCalledOnce();

    // The mute stays the user's own choice.
    muteButton().click();
    flushSync();
    expect(muteButton().getAttribute('aria-pressed')).toBe('true');

    props.voiceStatus = voiceStatus({ sequence: 6, recording: null });
    flushSync();
    expect(fake.release).toHaveBeenCalledExactlyOnceWith('wakeword');
    expect(caption().textContent).toBe('Hello');
    expect(muteButton().getAttribute('aria-pressed')).toBe('true');
  });

  it('releases the hold when Desktop Voice goes away', async () => {
    simulateController();
    const props = renderReactive({
      configured: true,
      voiceStatus: voiceStatus({ recording: RECORDING }),
    });
    toggle().click();
    await settle();
    // A call that starts during a recording is held at once.
    expect(fake.hold).toHaveBeenCalledExactlyOnceWith('wakeword');

    props.voiceStatus = null;
    flushSync();
    expect(fake.release).toHaveBeenCalledExactlyOnceWith('wakeword');
    expect(fake.state.held).toBe(false);
  });

  it('holds nothing without a call', () => {
    simulateController();
    const props = renderReactive({ configured: true, voiceStatus: null });
    props.voiceStatus = voiceStatus({ recording: RECORDING });
    flushSync();
    expect(fake.hold).not.toHaveBeenCalled();
  });
});

describe('Live control conflicts', () => {
  it.each([
    ['the app connection drops', 'serverUnavailable', true, false],
    ['Live voice is no longer configured', 'configured', false, true],
  ])(
    'when %s, stops a running call: %s',
    async (_label, prop, value, stops) => {
      simulateController();
      const onToast = vi.fn();
      const props = renderReactive({
        configured: true,
        serverUnavailable: false,
        onToast,
      });
      toggle().click();
      await settle();
      props[prop] = value;
      flushSync();
      // The call has its own socket; only its own loss ends it.
      expect(fake.stop).toHaveBeenCalledTimes(stops ? 1 : 0);
      expect(onToast).not.toHaveBeenCalled();
    },
  );

  it('cannot start while the server is unavailable', async () => {
    simulateController();
    render({ serverUnavailable: true });
    expect(toggle().disabled).toBe(true);
    toggle().click();
    await settle();
    expect(fake.start).not.toHaveBeenCalled();
  });
});
