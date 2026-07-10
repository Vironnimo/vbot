// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/desktopBridge.js', () => ({
  getWakewordStatus: vi.fn(),
  setWakewordEnabled: vi.fn(),
  setWakewordConfig: vi.fn(),
  listMicrophones: vi.fn(),
  onWakewordStatusChange: vi.fn(() => () => {}),
  retryWakeword: vi.fn(),
  isDesktop: vi.fn(() => true),
}));

const desktopBridge = await import('$lib/desktopBridge.js');
const { default: WakewordVoiceSettings } =
  await import('../WakewordVoiceSettings.svelte');

describe('WakewordVoiceSettings', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
    desktopBridge.isDesktop.mockReturnValue(true);
    desktopBridge.getWakewordStatus.mockResolvedValue(baseStatus());
    desktopBridge.listMicrophones.mockResolvedValue([
      {
        index: 4,
        name: 'Studio microphone',
        supported: true,
        default_sample_rate: 48000,
      },
      {
        index: 5,
        name: 'Bluetooth hands-free',
        supported: false,
        default_sample_rate: 8000,
      },
    ]);
    desktopBridge.setWakewordConfig.mockResolvedValue(undefined);
    desktopBridge.setWakewordEnabled.mockResolvedValue(undefined);
    desktopBridge.retryWakeword.mockResolvedValue(undefined);
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('shows an actionable microphone error and compatible device picker', async () => {
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      enabled: true,
      state: 'error',
      error_code: 'microphone_unavailable',
    });

    await mountPanel();

    expect(document.body.textContent).toContain('Voice needs attention');
    expect(document.body.textContent).toContain(
      'selected microphone cannot provide compatible audio',
    );
    expect(buttonByText('Retry listening')).not.toBeNull();

    buttonByLabel('Microphone').click();
    flushSync();
    expect(buttonByText('Studio microphone')).not.toBeNull();
    expect(buttonContainingText('Bluetooth hands-free').disabled).toBe(true);
  });

  it('saves the server-specific Personal Agent immediately without a second Save action', async () => {
    await mountPanel();

    buttonByText('— (none)').click();
    flushSync();
    buttonByText('Main').click();
    await tick();
    await Promise.resolve();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledTimes(1);
    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      target_agent_id: 'main',
    });
    expect(buttonByText('Save')).toBeUndefined();
  });

  async function mountPanel() {
    mountedComponent = mount(WakewordVoiceSettings, {
      target: document.body,
      props: { agents: [{ id: 'main', name: 'Main' }] },
    });
    flushSync();
    await tick();
    await Promise.resolve();
    flushSync();
  }
});

function baseStatus() {
  return {
    enabled: false,
    state: 'off',
    engine: 'openwakeword',
    microphone: null,
    sensitivity: 0.5,
    target_agent_id: null,
    session_behavior: 'active',
    wake_phrase: 'hey_jarvis',
    mock: false,
    mode: 'real',
    error_code: null,
    active_microphone: null,
  };
}

function buttonByText(text) {
  return [...document.body.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === text,
  );
}

function buttonByLabel(label) {
  return document.body.querySelector(`button[aria-label="${label}"]`);
}

function buttonContainingText(text) {
  return [...document.body.querySelectorAll('button')].find((button) =>
    button.textContent.includes(text),
  );
}
