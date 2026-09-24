// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { init, t } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock(
  'svelte/reactivity',
  async () =>
    import('../../../node_modules/svelte/src/reactivity/index-client.js'),
);
const { status, factory, desktop, lease } = vi.hoisted(() => ({
  status: vi.fn(),
  factory: vi.fn(),
  lease: { acquire: async () => null, release: () => {} },
  desktop: {
    isDesktopAccessor: () => false,
    createDesktopLiveVoiceLease: () => null,
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
  const props = new SvelteMap(Object.entries(initial));
  component = mount(LiveVoice, {
    target: document.body,
    props: {
      get configured() {
        return props.get('configured');
      },
      get serverUnavailable() {
        return props.get('serverUnavailable');
      },
      get onToast() {
        return props.get('onToast');
      },
    },
  });
  flushSync();
  return props;
}

function simulateController() {
  factory.mockImplementation(
    ({ state, onNotice, uiActions, microphoneLease }) => {
      fake = {
        state,
        onNotice,
        uiActions,
        microphoneLease,
        start: vi.fn(async () => {
          state.phase = 'connecting';
        }),
        stop: vi.fn(() => {
          state.phase = 'off';
        }),
        mute: vi.fn(() => {
          state.muted = !state.muted;
        }),
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
  document.querySelector('button[aria-label="Mute microphone"]');
const caption = () => document.querySelector('.live-voice__caption');

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
  desktop.createDesktopLiveVoiceLease = vi.fn(() => lease);
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
    expect(toggle().getAttribute('aria-label')).toBe('Start Live');
    expect(muteButton()).toBeNull();
    toggle().click();
    await settle();
    expect(fake.start).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe('Stop Live');
    expect(muteButton()).not.toBeNull();
    expect(caption().dataset.role).toBe('status');
    toggle().click();
    flushSync();
    expect(fake.stop).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe('Start Live');
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

  it('passes the App UI actions to the controller', () => {
    simulateController();
    const uiActions = {
      context: vi.fn(),
      open: vi.fn(),
      terminalView: vi.fn(),
    };
    render({ uiActions });
    expect(fake.uiActions).toBe(uiActions);
  });

  it('turns each controller notice into a shared toast with its severity', () => {
    simulateController();
    const onToast = vi.fn();
    render({ onToast });
    fake.onNotice({ code: 'access_denied', severity: 'error' });
    fake.onNotice({ code: 'announcement_failed', severity: 'warn' });
    fake.onNotice({ code: 'replaced', severity: 'info' });
    const toasts = onToast.mock.calls.map(([toast]) => toast);
    expect(toasts.map((toast) => toast.variant)).toEqual([
      'error',
      'warn',
      'info',
    ]);
    expect(toasts.every((toast) => toast.title === 'Live voice')).toBe(true);
    expect(new Set(toasts.map((toast) => toast.message)).size).toBe(3);
    expect(toasts[0].message).not.toContain('live.');
    // Codes without a dedicated message still identify the problem.
    expect(toasts[1].message).toContain('announcement_failed');
  });

  it.each([
    'not_configured',
    'not_usable',
    'invalid_offer',
    'access_denied',
    'rate_limited',
    'outcome_unknown',
    'provider_error',
    'control_failed',
    'microphone_denied',
    'microphone_unavailable',
    'connection_timeout',
    'connection_failed',
    'connection_lost',
    'call_failed',
    'playback_blocked',
    'desktop_restart_required',
    'ui_action_failed',
    'notification_failed',
    'replaced',
    'ended',
  ])('has a dedicated message for %s', (code) => {
    simulateController();
    const onToast = vi.fn();
    render({ onToast });
    fake.onNotice({ code, severity: 'error' });
    const [{ message }] = onToast.mock.calls[0];
    expect(message).toBeTruthy();
    expect(message).not.toBe(t('live.error.generic', '', { code }));
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
    expect(onToast.mock.calls[0][0].variant).toBe('error');
    expect(toggle().getAttribute('aria-label')).toBe('Start Live');
  });
});

describe('Live voice in the Desktop app', () => {
  it('stays browser-only outside the Desktop accessor', () => {
    simulateController();
    render();
    expect(fake.microphoneLease).toBeNull();
    expect(desktop.onDesktopLiveRequest).not.toHaveBeenCalled();
  });

  it('shares the microphone with wakeword listening through the Desktop lease', async () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    render();
    expect(fake.microphoneLease).toBe(lease);
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

  it('toggles the call from the global shortcut', async () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    render();

    desktopRequest({ action: 'toggle', source: 'hotkey' });
    await settle();
    expect(fake.start).toHaveBeenCalledOnce();
    desktopRequest({ action: 'toggle', source: 'hotkey' });
    flushSync();
    expect(fake.stop).toHaveBeenCalledOnce();
    expect(toggle().getAttribute('aria-label')).toBe('Start Live');
  });

  it('leaves a closing call alone', () => {
    simulateController();
    desktop.isDesktopAccessor.mockReturnValue(true);
    render();
    fake.state.phase = 'closing';
    desktopRequest({ action: 'toggle', source: 'hotkey' });
    expect(fake.stop).not.toHaveBeenCalled();
    expect(fake.start).not.toHaveBeenCalled();
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
      t('live.error.notConfigured', ''),
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

describe('Live control conflicts', () => {
  it.each([
    ['the server becomes unavailable', 'serverUnavailable', true],
    ['Live voice is no longer configured', 'configured', false],
  ])('stops a running call when %s', async (_label, prop, value) => {
    simulateController();
    const onToast = vi.fn();
    const props = renderReactive({
      configured: true,
      serverUnavailable: false,
      onToast,
    });
    toggle().click();
    await settle();
    props.set(prop, value);
    flushSync();
    expect(fake.stop).toHaveBeenCalledOnce();
    expect(onToast).not.toHaveBeenCalled();
  });

  it('cannot start while the server is unavailable', async () => {
    simulateController();
    render({ serverUnavailable: true });
    expect(toggle().disabled).toBe(true);
    toggle().click();
    await settle();
    expect(fake.start).not.toHaveBeenCalled();
  });
});
