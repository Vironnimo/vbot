// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { status } = vi.hoisted(() => ({ status: vi.fn() }));
vi.mock('$lib/api.js', () => ({ getLiveVoiceStatus: status }));
const { default: LiveVoice } = await import('../LiveVoice.svelte');
let component;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('global voice control', () => {
  it('shows setup guidance without touching the microphone when no key is configured', async () => {
    init('en');
    status.mockResolvedValue({ configured: false });
    const onSetup = vi.fn();
    component = mount(LiveVoice, {
      target: document.body,
      props: {
        getContext: () => ({}),
        navigate: vi.fn(),
        terminalView: vi.fn(),
        onSetup,
      },
    });
    flushSync();
    document.querySelector('button').click();
    await vi.waitFor(() => {
      flushSync();
      expect(document.querySelector('[role="alert"]')).not.toBeNull();
    });
    expect(component.isActive()).toBe(false);
    const setup = [...document.querySelectorAll('button')].find((button) =>
      button.textContent.includes('Open Providers'),
    );
    setup.click();
    expect(onSetup).toHaveBeenCalledOnce();
  });
});
