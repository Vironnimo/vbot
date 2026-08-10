// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const listTaskModelTargetsMock = vi.fn();
const getTaskModelOptionsMock = vi.fn();
const updateTaskModelSettingsMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listTaskModelTargets: (...args) => listTaskModelTargetsMock(...args),
  getTaskModelOptions: (...args) => getTaskModelOptionsMock(...args),
  updateTaskModelSettings: (...args) => updateTaskModelSettingsMock(...args),
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
