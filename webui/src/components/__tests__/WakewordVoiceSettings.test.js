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
  listWakewordModels: vi.fn(),
  importWakewordModel: vi.fn(),
  deleteWakewordModel: vi.fn(),
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
    desktopBridge.listWakewordModels.mockResolvedValue([
      {
        id: 'builtin/hey_jarvis',
        label: 'Hey Jarvis',
        source: 'built_in',
        format: 'onnx',
        removable: false,
      },
      {
        id: 'builtin/hey_mycroft',
        label: 'Hey Mycroft',
        source: 'built_in',
        format: 'onnx',
        removable: false,
      },
    ]);
    desktopBridge.setWakewordConfig.mockResolvedValue(undefined);
    desktopBridge.setWakewordEnabled.mockResolvedValue(undefined);
    desktopBridge.deleteWakewordModel.mockResolvedValue({ deleted: true });
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

  it('selects exactly one installed wakeword model', async () => {
    await mountPanel();

    buttonByLabel('Wakeword model').click();
    flushSync();
    buttonContainingText('Hey Mycroft').click();
    await tick();
    await Promise.resolve();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      model_id: 'builtin/hey_mycroft',
    });
  });

  it('imports a finished ONNX model and selects it', async () => {
    const importedModel = {
      id: 'custom/computer',
      label: 'Hey Computer',
      source: 'imported',
      format: 'onnx',
      removable: true,
    };
    desktopBridge.importWakewordModel.mockResolvedValue(importedModel);
    desktopBridge.getWakewordStatus
      .mockResolvedValueOnce(baseStatus())
      .mockResolvedValue({
        ...baseStatus(),
        model_id: importedModel.id,
      });
    desktopBridge.listWakewordModels
      .mockResolvedValueOnce([
        {
          id: 'builtin/hey_jarvis',
          label: 'Hey Jarvis',
          source: 'built_in',
          removable: false,
        },
      ])
      .mockResolvedValue([
        {
          id: 'builtin/hey_jarvis',
          label: 'Hey Jarvis',
          source: 'built_in',
          removable: false,
        },
        importedModel,
      ]);
    await mountPanel();

    const fileInput = document.body.querySelector('input[type="file"]');
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['model'], 'hey_computer.onnx')],
    });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    await waitForCondition(
      () => desktopBridge.importWakewordModel.mock.calls.length > 0,
    );
    await waitForCondition(() =>
      desktopBridge.setWakewordConfig.mock.calls.some(
        ([payload]) => payload.model_id === importedModel.id,
      ),
    );

    expect(desktopBridge.importWakewordModel).toHaveBeenCalledWith(
      'hey_computer.onnx',
      'bW9kZWw=',
    );
  });

  it('requires confirmation, switches away, and removes an imported model', async () => {
    const customModel = {
      id: 'custom/computer',
      label: 'Hey Computer',
      source: 'imported',
      format: 'onnx',
      removable: true,
    };
    desktopBridge.getWakewordStatus.mockResolvedValue({
      ...baseStatus(),
      model_id: customModel.id,
    });
    desktopBridge.listWakewordModels.mockResolvedValue([
      {
        id: 'builtin/hey_jarvis',
        label: 'Hey Jarvis',
        source: 'built_in',
        removable: false,
      },
      customModel,
    ]);
    await mountPanel();

    buttonByText('Remove imported model').click();
    flushSync();
    expect(document.body.textContent).toContain(
      'Remove “Hey Computer” permanently',
    );
    buttonByText('Delete').click();
    await tick();
    await Promise.resolve();
    await tick();

    expect(desktopBridge.setWakewordConfig).toHaveBeenCalledWith({
      model_id: 'builtin/hey_jarvis',
    });
    expect(desktopBridge.deleteWakewordModel).toHaveBeenCalledWith(
      customModel.id,
    );
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
    microphone: null,
    model_id: 'builtin/hey_jarvis',
    sensitivity: 0.5,
    target_agent_id: null,
    session_behavior: 'active',
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

async function waitForCondition(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    flushSync();
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
    await tick();
  }
  throw new Error('Condition was not met before timeout');
}
