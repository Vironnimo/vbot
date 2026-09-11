// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { init } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock(
  'svelte/reactivity',
  async () =>
    import('../../../node_modules/svelte/src/reactivity/index-client.js'),
);
const { status, factory } = vi.hoisted(() => ({
  status: vi.fn(),
  factory: vi.fn(),
}));
vi.mock('$lib/api.js', () => ({ getLiveVoiceStatus: status }));
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
let stop;
function render(props = {}) {
  component = mount(LiveVoice, {
    target: document.body,
    props: {
      getContext: () => ({}),
      navigate: vi.fn(),
      terminalView: vi.fn(),
      ...props,
    },
  });
  flushSync();
}
function simulateConnection() {
  factory.mockImplementation(({ state }) => {
    stop = vi.fn(() => {
      state.phase = 'off';
    });
    return {
      start: vi.fn(() => {
        state.phase = 'listening';
      }),
      stop,
      active: () => state.phase === 'listening',
      seedRuns: vi.fn(),
      notifyRuns: vi.fn(),
      destroy: vi.fn(),
    };
  });
}
beforeEach(() => {
  init('en');
  factory.mockReset();
  status.mockReset();
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('sidebar Live control', () => {
  it('is absent by default and does not check credentials or start audio', () => {
    render();
    expect(document.querySelector('button')).toBeNull();
    expect(status).not.toHaveBeenCalled();
  });
  it('uses the shared toast for missing credentials without opening a panel', async () => {
    status.mockResolvedValue({ configured: false });
    const onToast = vi.fn();
    render({ enabled: true, onToast });
    document.querySelector('button').click();
    await vi.waitFor(() => {
      flushSync();
      expect(onToast).toHaveBeenCalledOnce();
    });
    expect(onToast.mock.calls[0][0].message).toContain('OpenAI API key');
    expect(component.isActive()).toBe(false);
    expect(document.querySelector('button').textContent).toContain(
      'Start Live',
    );
    expect(document.querySelectorAll('button')).toHaveLength(1);
  });
  it('changes the single Start button to Stop and ends the connection on click', () => {
    simulateConnection();
    render({ enabled: true });
    const button = document.querySelector('button');
    expect(button.getAttribute('aria-label')).toBe('Start Live');
    button.click();
    flushSync();
    expect(button.getAttribute('aria-label')).toBe('Stop Live');
    expect(button.getAttribute('aria-pressed')).toBe('true');
    button.click();
    flushSync();
    expect(stop).toHaveBeenCalledOnce();
    expect(button.getAttribute('aria-label')).toBe('Start Live');
    expect(component.isActive()).toBe(false);
  });
  it('ends an active connection and hides the control when Settings disables Live', () => {
    simulateConnection();
    const settings = new SvelteMap([['enabled', true]]);
    component = mount(LiveVoice, {
      target: document.body,
      props: {
        get enabled() {
          return settings.get('enabled');
        },
        getContext: () => ({}),
        navigate: vi.fn(),
        terminalView: vi.fn(),
      },
    });
    flushSync();
    document.querySelector('button').click();
    flushSync();
    expect(component.isActive()).toBe(true);
    settings.set('enabled', false);
    flushSync();
    expect(stop).toHaveBeenCalledOnce();
    expect(document.querySelector('button')).toBeNull();
    expect(component.isActive()).toBe(false);
  });
});
