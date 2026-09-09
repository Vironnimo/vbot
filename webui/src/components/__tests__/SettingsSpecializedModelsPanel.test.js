// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const listTaskModelTargetsMock = vi.fn();
const getTaskModelOptionsMock = vi.fn();
const updateTaskModelSettingsMock = vi.fn();
const getLocalSpeechSetupMock = vi.fn();
const installLocalSpeechSupportMock = vi.fn();
const restartAfterLocalSpeechSetupMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listTaskModelTargets: (...args) => listTaskModelTargetsMock(...args),
  getTaskModelOptions: (...args) => getTaskModelOptionsMock(...args),
  updateTaskModelSettings: (...args) => updateTaskModelSettingsMock(...args),
  getLocalSpeechSetup: (...args) => getLocalSpeechSetupMock(...args),
  installLocalSpeechSupport: (...args) =>
    installLocalSpeechSupportMock(...args),
  restartAfterLocalSpeechSetup: (...args) =>
    restartAfterLocalSpeechSetupMock(...args),
}));

const { default: SettingsSpecializedModelsPanel } =
  await import('../settings/SettingsSpecializedModelsPanel.svelte');

describe('SettingsSpecializedModelsPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    listTaskModelTargetsMock.mockReset();
    getTaskModelOptionsMock.mockReset();
    updateTaskModelSettingsMock.mockReset();
    getLocalSpeechSetupMock
      .mockReset()
      .mockResolvedValue({ state: 'ready', restart_available: true });
    installLocalSpeechSupportMock.mockReset().mockResolvedValue({
      state: 'installing',
      phase: 'downloading',
      restart_available: true,
    });
    restartAfterLocalSpeechSetupMock
      .mockReset()
      .mockResolvedValue({ state: 'restarting' });
    listTaskModelTargetsMock.mockResolvedValue({ targets: [] });
    getTaskModelOptionsMock.mockResolvedValue({ fields: [] });
    updateTaskModelSettingsMock.mockResolvedValue({ model_tasks: {} });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('reloads task-model targets when modelsRefreshToken changes', async () => {
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForCondition(
      () => listTaskModelTargetsMock.mock.calls.length >= 1,
    );

    const before = listTaskModelTargetsMock.mock.calls.length;

    // The form is idle, so the queued reload runs immediately.
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(
      () => listTaskModelTargetsMock.mock.calls.length > before,
    );

    expect(listTaskModelTargetsMock.mock.calls.length).toBeGreaterThan(before);
  });

  it('offers installation for local engines while preserving their separate options', async () => {
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'missing',
      restart_available: true,
    });
    listTaskModelTargetsMock.mockImplementation((taskType) =>
      Promise.resolve({
        targets:
          taskType === 'speech_to_text'
            ? [
                {
                  id: 'local/qwen3-asr',
                  label: 'Qwen3 ASR',
                  kind: 'local',
                  usable: false,
                },
                {
                  id: 'local/parakeet',
                  label: 'Parakeet TDT v3',
                  kind: 'local',
                  usable: true,
                },
              ]
            : [],
      }),
    );
    getTaskModelOptionsMock.mockImplementation((_task, target) =>
      Promise.resolve({
        fields:
          target === 'local/qwen3-asr'
            ? [
                {
                  name: 'language',
                  label: 'Language',
                  type: 'text',
                  default: 'de',
                },
              ]
            : [
                {
                  name: 'device',
                  label: 'Device',
                  type: 'select',
                  default: 'auto',
                  options: [{ value: 'auto', label: 'Automatic' }],
                },
              ],
      }),
    );
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props: { settings: {}, modelsRefreshToken: 0 },
    });
    flushSync();
    await waitForCondition(
      () =>
        !document.getElementById('settings-specialized-speech_to_text')
          ?.disabled,
    );
    selectTarget('speech_to_text', 'Qwen3 ASR');
    await waitForCondition(() => button('Install'));
    expect(document.body.textContent).not.toContain('.[local-speech]');
    await waitForCondition(() =>
      document.querySelector('#task-model-speech_to_text-language'),
    );
    selectTarget('speech_to_text', 'Parakeet TDT v3');
    await waitForCondition(() =>
      document.querySelector('#task-model-speech_to_text-device'),
    );
    expect(
      document.querySelector('#task-model-speech_to_text-language'),
    ).toBeNull();
    expect(button('Install')).toBeTruthy();
    const trigger = document.getElementById(
      'settings-specialized-speech_to_text',
    );
    expect(trigger.textContent).toContain('Parakeet TDT v3 (local)');
    trigger.click();
    flushSync();
    const search = document.querySelector('.searchable-dropdown__search input');
    search.value = 'local';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const matches = [
      ...document.querySelectorAll('.searchable-dropdown__option'),
    ];
    expect(matches).toHaveLength(2);
    expect(
      matches.every((option) => option.textContent.includes('(local)')),
    ).toBe(true);
  });

  async function mountLocalPanel() {
    listTaskModelTargetsMock.mockImplementation((taskType) =>
      Promise.resolve({
        targets:
          taskType === 'speech_to_text'
            ? [
                {
                  id: 'local/qwen3-asr',
                  label: 'Qwen3 ASR',
                  kind: 'local',
                  usable: false,
                },
              ]
            : [],
      }),
    );
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props: {
        settings: {
          model_tasks: {
            speech_to_text: { target: 'local/qwen3-asr', options: {} },
          },
        },
      },
    });
    flushSync();
    await waitForCondition(() => document.querySelector('[role="status"]'));
  }

  it('keeps setup running across navigation and enables one explicit restart after verification', async () => {
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'missing',
      restart_available: true,
    });
    await mountLocalPanel();
    await waitForCondition(() => button('Install'));
    button('Install').click();
    button('Install')?.click();
    await waitForCondition(() => button('Installing…'));
    expect(installLocalSpeechSupportMock).toHaveBeenCalledTimes(1);
    expect(button('Installing…').disabled).toBe(true);
    expect(button('Restart server')).toBeUndefined();
    await unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'restart_required',
      restart_available: true,
    });
    await mountLocalPanel();
    await waitForCondition(
      () => button('Restart server') && !button('Restart server').disabled,
    );
    expect(installLocalSpeechSupportMock).toHaveBeenCalledTimes(1);
    button('Restart server').click();
    await waitForCondition(() => button('Restarting…'));
    expect(restartAfterLocalSpeechSetupMock).toHaveBeenCalledTimes(1);
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'ready',
      restart_available: true,
    });
    await waitForCondition(() => !button('Restarting…'), 30, 100);
    expect(restartAfterLocalSpeechSetupMock).toHaveBeenCalledTimes(1);
    expect(button('Install')).toBeUndefined();
  });

  it('offers retry after a failed installation and respects unsupported restarts', async () => {
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'failed',
      error: 'install_failed',
      restart_available: false,
    });
    await mountLocalPanel();
    await waitForCondition(() => button('Try again'));
    installLocalSpeechSupportMock.mockResolvedValue({
      state: 'restart_required',
      restart_available: false,
    });
    button('Try again').click();
    await waitForCondition(() => button('Restart server'));
    expect(button('Restart server').disabled).toBe(true);
    expect(restartAfterLocalSpeechSetupMock).not.toHaveBeenCalled();
    expect(installLocalSpeechSupportMock).toHaveBeenCalledTimes(1);
  });

  it('checks status after a lost restart response without replaying the restart', async () => {
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'restart_required',
      restart_available: true,
    });
    await mountLocalPanel();
    await waitForCondition(
      () => button('Restart server') && !button('Restart server').disabled,
    );
    restartAfterLocalSpeechSetupMock.mockRejectedValue(
      new Error('disconnected'),
    );
    button('Restart server').click();
    await waitForCondition(() => button('Restarting…'));
    getLocalSpeechSetupMock.mockResolvedValue({
      state: 'ready',
      restart_available: true,
    });
    await waitForCondition(() => !button('Restarting…'), 30, 100);
    expect(restartAfterLocalSpeechSetupMock).toHaveBeenCalledTimes(1);
    expect(button('Check again')).toBeUndefined();
  });

  it('loads image-understanding targets with the other specialized models', async () => {
    const props = reactiveProps({ settings: {}, modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForCondition(() =>
      listTaskModelTargetsMock.mock.calls.some(
        ([taskType]) => taskType === 'image_understanding',
      ),
    );

    expect(listTaskModelTargetsMock).toHaveBeenCalledWith(
      'image_understanding',
    );
    expect(
      document.querySelector('#settings-specialized-image_understanding'),
    ).toBeTruthy();
  });

  it('renders every task-model target picker as searchable and filters by target id', async () => {
    listTaskModelTargetsMock.mockResolvedValue({
      targets: [
        {
          id: 'openrouter/google/gemini-specialized::api-key',
          label: 'Gemini Specialized',
          kind: 'provider',
        },
        {
          id: 'openai/specialized-model::api-key',
          label: 'OpenAI Specialized',
          kind: 'provider',
        },
      ],
    });

    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props: { settings: {}, modelsRefreshToken: 0 },
    });
    flushSync();

    const taskTypes = [
      'speech_to_text',
      'text_to_speech',
      'image_understanding',
      'image_generation',
      'video_generation',
      'music_generation',
      'text_embedding',
    ];
    await waitForCondition(() =>
      taskTypes.every((taskType) => {
        const trigger = document.getElementById(
          `settings-specialized-${taskType}`,
        );
        return trigger && !trigger.disabled;
      }),
    );

    for (const taskType of taskTypes) {
      const trigger = document.getElementById(
        `settings-specialized-${taskType}`,
      );
      expect(trigger.closest('.searchable-dropdown')).toBeTruthy();
      expect(trigger.closest('.dropdown-primitive')).toBeNull();
    }

    document
      .getElementById('settings-specialized-speech_to_text')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    await waitForCondition(
      () =>
        document.body.querySelector('.searchable-dropdown__search input') !==
        null,
    );

    const searchInput = document.body.querySelector(
      '.searchable-dropdown__search input',
    );
    searchInput.value = 'google/gemini';
    searchInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    const visibleOptions = Array.from(
      document.body.querySelectorAll('.searchable-dropdown__option'),
    );
    expect(visibleOptions).toHaveLength(1);
    expect(visibleOptions[0].textContent).toContain('Gemini Specialized');
  });

  it('ignores a late option schema response for a previously selected model', async () => {
    const staleSchema = deferred();
    listTaskModelTargetsMock.mockImplementation((taskType) =>
      Promise.resolve({
        targets:
          taskType === 'speech_to_text'
            ? [
                { id: 'provider/first', label: 'First model' },
                { id: 'provider/second', label: 'Second model' },
              ]
            : [],
      }),
    );
    getTaskModelOptionsMock.mockImplementation((_taskType, target) => {
      if (target === 'provider/first') {
        return staleSchema.promise;
      }
      return Promise.resolve({
        fields: [
          {
            name: 'new_option',
            type: 'string',
            label: 'Newest option',
            default: 'new',
          },
        ],
      });
    });
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props: { settings: {}, modelsRefreshToken: 0 },
    });
    flushSync();
    await waitForCondition(
      () =>
        !document.getElementById('settings-specialized-speech_to_text')
          ?.disabled,
    );

    selectTarget('speech_to_text', 'First model');
    await waitForCondition(() =>
      getTaskModelOptionsMock.mock.calls.some(
        ([, target]) => target === 'provider/first',
      ),
    );
    selectTarget('speech_to_text', 'Second model');
    await waitForCondition(() =>
      document.body.textContent.includes('Newest option'),
    );

    staleSchema.resolve({
      fields: [
        {
          name: 'stale_option',
          type: 'string',
          label: 'Stale option',
          default: 'stale',
        },
      ],
    });
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    expect(document.body.textContent).toContain('Newest option');
    expect(document.body.textContent).not.toContain('Stale option');
  });

  it('auto-saves after a boolean option toggle is flipped', async () => {
    // The boolean option field is the shared Toggle (role="switch"); flipping it
    // must arm the same autosave flow as the other option controls.
    listTaskModelTargetsMock.mockImplementation((taskType) =>
      Promise.resolve({
        targets:
          taskType === 'speech_to_text'
            ? [
                {
                  id: 'openai:api-key/whisper-1',
                  label: 'Whisper',
                  kind: 'model',
                },
              ]
            : [],
      }),
    );
    getTaskModelOptionsMock.mockImplementation((taskType) =>
      Promise.resolve({
        fields:
          taskType === 'speech_to_text'
            ? [
                {
                  name: 'translate',
                  type: 'boolean',
                  label: 'Translate',
                  default: false,
                },
              ]
            : [],
      }),
    );

    const props = reactiveProps({
      settings: {
        model_tasks: {
          speech_to_text: { target: 'openai:api-key/whisper-1', options: {} },
        },
      },
      modelsRefreshToken: 0,
    });
    mountedComponent = mount(SettingsSpecializedModelsPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await waitForCondition(
      () => document.body.querySelector('button[role="switch"]') !== null,
    );

    const toggle = document.body.querySelector('button[role="switch"]');
    toggle.click();
    flushSync();

    // The autosave debounce is 800 ms of real time, so poll with a delay that
    // spans it (20 × 100 ms) rather than the default near-instant cadence.
    await waitForCondition(
      () => updateTaskModelSettingsMock.mock.calls.length >= 1,
      20,
      100,
    );

    const payload = updateTaskModelSettingsMock.mock.calls[0][0];
    expect(payload.speech_to_text.options.translate).toBe(true);
  });
});

async function waitForCondition(check, attempts = 20, delayMs = 0) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    flushSync();
    if (check()) {
      return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}

function selectTarget(taskType, label) {
  document
    .getElementById(`settings-specialized-${taskType}`)
    .dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
  const option = Array.from(
    document.body.querySelectorAll('.searchable-dropdown__option'),
  ).find((candidate) => candidate.textContent.includes(label));
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function button(label) {
  return [...document.querySelectorAll('button')].find(
    (element) => element.textContent.trim() === label,
  );
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
