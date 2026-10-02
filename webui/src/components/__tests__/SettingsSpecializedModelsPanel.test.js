// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';

import { t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  api,
  button,
  cleanupSpecializedModelsHarness,
  deferred,
  mountPanel,
  optionLabels,
  resetSpecializedModelsHarness,
  selectTarget,
  settle,
  targetsFor,
  waitForCondition,
} from './SettingsSpecializedModelsPanel.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

// Settings props whose onCommit feeds the saved response back, as the host does.
function committingProps(initial) {
  const props = reactiveProps(initial);
  props.onCommit = (settings) => {
    props.settings = settings;
  };
  return props;
}

function echoSavedModelTasks() {
  api.updateTaskModelSettings.mockImplementation(async (model_tasks) => ({
    model_tasks,
  }));
}

describe('SettingsSpecializedModelsPanel', () => {
  beforeEach(() => {
    resetSpecializedModelsHarness();
  });

  afterEach(async () => {
    await cleanupSpecializedModelsHarness();
  });

  it.each([
    ['speech_to_text', 'text_embedding'],
    ['text_embedding', 'image_generation'],
    ['image_generation', 'speech_to_text'],
    ['decision', 'text_to_speech'],
  ])(
    'keeps %s edits isolated when another page updates %s',
    async (taskType, otherTask) => {
      const binding = {
        target: 'test/owned',
        options: { stale_option: 'remove-me' },
      };
      const otherBinding = { target: 'test/other-before', options: {} };
      const props = committingProps({
        taskTypes: [taskType],
        settings: {
          model_tasks: { [taskType]: binding, [otherTask]: otherBinding },
        },
      });
      mountPanel(props);
      await waitForCondition(
        () => button('Reset options') && !button('Reset options').disabled,
      );
      expect(api.listTaskModelTargets.mock.calls.map(([task]) => task)).toEqual(
        [taskType],
      );
      expect(
        document.getElementById('settings-specialized-' + otherTask),
      ).toBeNull();
      if (taskType !== 'speech_to_text')
        expect(api.getLocalSpeechMemory).not.toHaveBeenCalled();

      button('Reset options').click();
      flushSync();
      // An independent page publishes its result while this page has a draft.
      const changedOtherBinding = {
        target: 'test/other-after',
        options: { temperature: 0.4 },
      };
      props.settings = {
        model_tasks: {
          [taskType]: binding,
          [otherTask]: changedOtherBinding,
        },
      };
      api.updateTaskModelSettings.mockResolvedValue({
        model_tasks: {
          [taskType]: { ...binding, options: {} },
          [otherTask]: changedOtherBinding,
        },
      });
      flushSync();
      button('Save').click();
      await waitForCondition(
        () => api.updateTaskModelSettings.mock.calls.length === 1,
      );
      expect(api.updateTaskModelSettings.mock.calls[0][0]).toEqual({
        [taskType]: { target: binding.target, options: {} },
      });
      expect(props.settings.model_tasks[otherTask]).toEqual(
        changedOtherBinding,
      );
      expect(button('Reset options')).toBeUndefined();
    },
  );

  describe('when the bindings changed elsewhere since they were read', () => {
    const listener = { target: 'test/stt-a', options: {} };
    const voice = { target: 'test/tts-a', options: { voice: 'a' } };
    const edited = { target: 'test/tts-c', options: {} };
    const newerListener = { target: 'test/stt-b', options: {} };
    const newerVoice = { target: voice.target, options: { voice: 'b' } };

    it.each([
      {
        scenario: 'retries the edit over the newer bindings',
        saved: { speech_to_text: newerListener, text_to_speech: voice },
        retried: true,
        shown: ['Listener B', 'Voice C'],
      },
      {
        // The draft's target with the other side's options would be invalid.
        scenario: 'keeps a binding changed on both sides as saved, as a whole',
        saved: { speech_to_text: listener, text_to_speech: newerVoice },
        retried: false,
        shown: ['Listener A', 'Voice A'],
      },
    ])('$scenario', async ({ saved, retried, shown }) => {
      const targets = {
        speech_to_text: [
          { id: listener.target, label: 'Listener A' },
          { id: newerListener.target, label: 'Listener B' },
        ],
        text_to_speech: [
          { id: voice.target, label: 'Voice A' },
          { id: edited.target, label: 'Voice C' },
        ],
      };
      api.listTaskModelTargets.mockImplementation(async (taskType) => ({
        targets: targets[taskType],
      }));
      api.getSettings.mockResolvedValue({ model_tasks: saved });
      api.updateTaskModelSettings
        .mockRejectedValueOnce(
          Object.assign(new Error('Settings changed since they were read'), {
            code: 'settings_conflict',
          }),
        )
        .mockImplementation(async (model_tasks) => ({
          model_tasks: { ...saved, ...model_tasks },
        }));
      const onError = vi.fn();
      const props = committingProps({
        taskTypes: ['speech_to_text', 'text_to_speech'],
        settings: {
          model_tasks: { speech_to_text: listener, text_to_speech: voice },
        },
        onError,
      });
      mountPanel(props);
      await waitForCondition(
        () =>
          !document.getElementById('settings-specialized-text_to_speech')
            ?.disabled,
      );

      selectTarget('text_to_speech', 'Voice C');
      button('Save').click();
      await waitForCondition(() => api.getSettings.mock.calls.length === 1);
      await settle();

      const writes = api.updateTaskModelSettings.mock.calls;
      // Only the edited binding is written, based on the values it was read from.
      expect(writes[0]).toEqual([
        { text_to_speech: edited },
        { base: { speech_to_text: listener, text_to_speech: voice } },
      ]);
      expect(writes.slice(1)).toEqual(
        retried ? [[{ text_to_speech: edited }, { base: saved }]] : [],
      );
      expect(props.settings.model_tasks).toEqual(
        retried ? { ...saved, text_to_speech: edited } : saved,
      );
      expect(
        ['speech_to_text', 'text_to_speech'].map((taskType) =>
          document
            .getElementById(`settings-specialized-${taskType}`)
            .textContent.trim(),
        ),
      ).toEqual(shown);
      expect(onError.mock.calls.at(-1)).toEqual([
        retried ? '' : t('settings.saveConflict'),
      ]);
    });
  });

  it('loads every task type including image understanding, reports a failed load, and reloads on modelsRefreshToken', async () => {
    const onError = vi.fn();
    api.listTaskModelTargets.mockRejectedValueOnce(
      new Error('targets offline'),
    );
    const props = reactiveProps({
      settings: {},
      modelsRefreshToken: 0,
      onError,
    });
    mountPanel(props);
    await waitForCondition(() =>
      api.listTaskModelTargets.mock.calls.some(
        ([taskType]) => taskType === 'image_understanding',
      ),
    );
    expect(
      document.querySelector('#settings-specialized-image_understanding'),
    ).toBeTruthy();
    await waitForCondition(() => onError.mock.calls.length > 1);
    expect(onError).toHaveBeenLastCalledWith(
      `${t('settings.specializedModels.loadError')} targets offline`,
    );
    const before = api.listTaskModelTargets.mock.calls.length;

    // The form is idle, so the queued reload runs immediately.
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(
      () => api.listTaskModelTargets.mock.calls.length > before,
    );
    expect(onError).toHaveBeenLastCalledWith('');
  });

  it('renders every task-model target picker as searchable and filters by target id', async () => {
    api.listTaskModelTargets.mockResolvedValue({
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
    mountPanel({ settings: {}, modelsRefreshToken: 0 });

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
    await waitForCondition(() =>
      document.body.querySelector('.searchable-dropdown__search input'),
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
    targetsFor('speech_to_text', [
      { id: 'provider/first', label: 'First model' },
      { id: 'provider/second', label: 'Second model' },
    ]);
    api.getTaskModelOptions.mockImplementation((_taskType, target) => {
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
    mountPanel({ settings: {}, modelsRefreshToken: 0 });
    await waitForCondition(
      () =>
        !document.getElementById('settings-specialized-speech_to_text')
          ?.disabled,
    );

    selectTarget('speech_to_text', 'First model');
    await waitForCondition(() =>
      api.getTaskModelOptions.mock.calls.some(
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

  describe('options', () => {
    it.each([
      {
        target: 'local/qwen3-tts-1.7b',
        name: 'instructions',
        type: 'textarea',
        value: 'test-owned style',
      },
      {
        target: 'local/chatterbox',
        name: 'exaggeration',
        type: 'number',
        value: 0.7,
      },
    ])(
      'shows $target options before the preview and autosaves edits without a disclosure',
      async (scenario) => {
        targetsFor('text_to_speech', [
          {
            id: scenario.target,
            label: 'Test voice',
            kind: 'local',
            usable: true,
          },
        ]);
        api.getTaskModelOptions.mockResolvedValue({
          fields: [
            {
              name: scenario.name,
              type: scenario.type,
              label: 'Test option',
              default: scenario.type === 'number' ? 0.5 : '',
            },
          ],
        });
        echoSavedModelTasks();
        mountPanel(
          committingProps({
            settings: {
              model_tasks: {
                text_to_speech: { target: scenario.target, options: {} },
              },
            },
          }),
        );
        await waitForCondition(() =>
          document.querySelector('.speech-preview textarea'),
        );
        const input = document.getElementById(
          `task-model-text_to_speech-${scenario.name}`,
        );
        const preview = document.querySelector('.speech-preview textarea');
        expect(input.closest('[hidden]')).toBeNull();
        expect(
          input.compareDocumentPosition(preview) &
            Node.DOCUMENT_POSITION_FOLLOWING,
        ).toBeTruthy();
        expect(document.querySelector('.s-disclosure-btn')).toBeNull();
        expect(api.updateTaskModelSettings).not.toHaveBeenCalled();

        input.value = String(scenario.value);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        flushSync();
        // Real autosave debounce (800 ms).
        await waitForCondition(
          () => api.updateTaskModelSettings.mock.calls.length > 0,
          20,
          100,
        );
        expect(
          api.updateTaskModelSettings.mock.calls[0][0].text_to_speech,
        ).toEqual({
          target: scenario.target,
          options: { [scenario.name]: scenario.value },
        });
      },
    );

    it('shows the new model options immediately when switching local TTS engines', async () => {
      const targets = [
        { id: 'local/qwen3-tts-1.7b', label: 'Qwen3-TTS', kind: 'local' },
        { id: 'local/chatterbox', label: 'Chatterbox', kind: 'local' },
      ];
      targetsFor('text_to_speech', targets);
      api.getTaskModelOptions.mockImplementation(async (_taskType, target) => ({
        fields: [
          target === targets[0].id
            ? {
                name: 'instructions',
                type: 'textarea',
                label: 'Style',
                default: '',
              }
            : {
                name: 'exaggeration',
                type: 'number',
                label: 'Expression',
                default: 0.5,
              },
        ],
      }));
      mountPanel({
        settings: {
          model_tasks: {
            text_to_speech: { target: targets[0].id, options: {} },
          },
        },
      });
      await waitForCondition(() =>
        document.getElementById('task-model-text_to_speech-instructions'),
      );

      selectTarget('text_to_speech', 'Chatterbox');
      await waitForCondition(() =>
        document.getElementById('task-model-text_to_speech-exaggeration'),
      );
      expect(
        document.getElementById('task-model-text_to_speech-instructions'),
      ).toBeNull();
      const field = document.getElementById(
        'task-model-text_to_speech-exaggeration',
      );
      expect(field.closest('[hidden]')).toBeNull();
      expect(field.value).toBe('0.5');
      expect(
        field.compareDocumentPosition(
          document.querySelector('.speech-preview'),
        ) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });

    it('autosaves a boolean toggle without replaying stale siblings or displayed defaults', async () => {
      // The boolean option is the shared Toggle (role="switch"); flipping it
      // arms the same autosave as the other option controls.
      const model_tasks = {
        speech_to_text: {
          target: 'openrouter/transcribe::api-key',
          options: {},
        },
        text_to_speech: {
          target: 'openrouter/voice::api-key',
          options: { retired: true },
        },
      };
      api.getTaskModelOptions.mockImplementation((taskType) =>
        Promise.resolve({
          fields: [
            {
              name: 'temperature',
              type: 'number',
              label: 'Temperature',
              default: 0,
            },
            ...(taskType === 'speech_to_text'
              ? [
                  {
                    name: 'translate',
                    type: 'boolean',
                    label: 'Translate',
                    default: false,
                  },
                ]
              : []),
          ],
        }),
      );
      mountPanel({ settings: { model_tasks } });
      await waitForCondition(() =>
        document.querySelector('button[role="switch"]'),
      );
      expect(api.updateTaskModelSettings).not.toHaveBeenCalled();

      document.querySelector('button[role="switch"]').click();
      flushSync();
      // Real autosave debounce (800 ms).
      await waitForCondition(
        () => api.updateTaskModelSettings.mock.calls.length > 0,
        20,
        100,
      );
      expect(api.updateTaskModelSettings.mock.calls[0][0]).toEqual({
        speech_to_text: {
          target: model_tasks.speech_to_text.target,
          options: { translate: true },
        },
      });
    });

    it('edits a JSON option as a parsed structure and never saves invalid text', async () => {
      const target = 'openrouter/recraft/recraft-v3::api-key';
      targetsFor('image_generation', [
        { id: target, kind: 'provider', label: 'Recraft v3', usable: true },
      ]);
      api.getTaskModelOptions.mockResolvedValue({
        fields: [
          {
            name: 'text_layout',
            type: 'json',
            label: 'Text layout',
            default: [],
          },
        ],
      });
      echoSavedModelTasks();
      mountPanel(
        committingProps({ taskTypes: ['image_generation'], settings: {} }),
      );
      await waitForCondition(
        () =>
          !document.getElementById('settings-specialized-image_generation')
            ?.disabled,
      );
      selectTarget('image_generation', 'Recraft v3');
      await waitForCondition(() => document.querySelector('.text-area--code'));

      const textarea = document.querySelector('.text-area--code');
      // JSON options live behind the closed advanced disclosure.
      const advanced = textarea.closest('details');
      expect(advanced.open).toBe(false);
      advanced.querySelector('summary').click();
      expect(advanced.open).toBe(true);
      // The structured default serializes to JSON text.
      expect(textarea.value).toBe('[]');
      expect(textarea.getAttribute('aria-invalid')).toBe('false');

      const setText = (value) => {
        textarea.value = value;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        flushSync();
      };
      const lastSavedLayout = () =>
        api.updateTaskModelSettings.mock.calls.at(-1)[0].image_generation;

      setText('[{"text": "hi"');
      expect(textarea.getAttribute('aria-invalid')).toBe('true');
      expect(document.querySelector('.form-field__error')).toBeTruthy();
      button('Save').click();
      await waitForCondition(
        () => api.updateTaskModelSettings.mock.calls.length === 1,
      );
      // The malformed text never reaches the binding.
      expect(lastSavedLayout().target).toBe(target);
      expect(lastSavedLayout().options?.text_layout ?? []).toEqual(
        expect.any(Array),
      );

      const layout = [
        {
          text: 'hi',
          bbox: [
            [0, 0],
            [1, 1],
          ],
        },
      ];
      setText(JSON.stringify(layout));
      expect(textarea.getAttribute('aria-invalid')).toBe('false');
      expect(document.querySelector('.form-field__error')).toBeNull();
      button('Save').click();
      await waitForCondition(
        () => api.updateTaskModelSettings.mock.calls.length === 2,
      );
      expect(lastSavedLayout()).toEqual({
        target,
        options: { text_layout: layout },
      });
    });

    it.each([{ retired: true }, { voice: 'removed' }])(
      'resets stale options without changing the target: %j',
      async (options) => {
        const target = 'openrouter/voice::api-key';
        api.getTaskModelOptions.mockResolvedValue({
          fields: [
            {
              name: 'voice',
              type: 'select',
              label: 'Voice',
              default: '',
              required: true,
              options: [{ value: 'available', label: 'Available' }],
            },
          ],
        });
        mountPanel({
          settings: { model_tasks: { text_to_speech: { target, options } } },
        });
        await waitForCondition(
          () => button('Reset options') && !button('Reset options').disabled,
        );
        button('Reset options').click();
        flushSync();
        document.getElementById('task-model-text_to_speech-voice').click();
        flushSync();
        document.querySelector('[role="option"]').click();
        flushSync();
        button('Save').click();
        await waitForCondition(
          () => api.updateTaskModelSettings.mock.calls.length,
        );
        expect(api.updateTaskModelSettings.mock.calls[0][0]).toEqual({
          text_to_speech: { target, options: { voice: 'available' } },
        });
      },
    );

    it('narrows dependent choices and never keeps a hidden value', async () => {
      const target = 'openai/gpt-live-1::api-key';
      api.getTaskModelOptions.mockResolvedValue({
        fields: [
          {
            name: 'backend_model',
            type: 'select',
            label: 'Backend model',
            default: 'terra',
            required: true,
            options: [
              { value: 'terra', label: 'Terra' },
              { value: 'astra', label: 'Astra' },
            ],
          },
          {
            name: 'backend_thinking_effort',
            type: 'select',
            label: 'Backend reasoning',
            default: 'low',
            options: ['', 'none', 'low', 'medium', 'high'].map((value) => ({
              value,
              label: value || 'Model default',
            })),
            options_by: {
              field: 'backend_model',
              values: {
                terra: ['', 'none', 'low', 'high'],
                astra: ['', 'none', 'medium'],
              },
            },
          },
        ],
      });
      mountPanel({
        taskTypes: ['live_voice'],
        settings: { model_tasks: { live_voice: { target, options: {} } } },
      });
      const effortId = 'task-model-live_voice-backend_thinking_effort';
      const toggleEffort = () => {
        document.getElementById(effortId).click();
        flushSync();
      };
      await waitForCondition(() => document.getElementById(effortId));

      toggleEffort();
      expect(optionLabels()).toEqual(['Model default', 'none', 'low', 'high']);
      toggleEffort();

      document.getElementById('task-model-live_voice-backend_model').click();
      flushSync();
      [...document.querySelectorAll('[role="option"]')]
        .find((option) => option.textContent.trim() === 'Astra')
        .click();
      flushSync();
      expect(document.getElementById(effortId).textContent.trim()).toBe(
        'Model default',
      );
      toggleEffort();
      expect(optionLabels()).toEqual(['Model default', 'none', 'medium']);
      toggleEffort();

      button('Save').click();
      await waitForCondition(
        () => api.updateTaskModelSettings.mock.calls.length,
      );
      expect(api.updateTaskModelSettings.mock.calls[0][0]).toEqual({
        live_voice: {
          target,
          options: { backend_model: 'astra', backend_thinking_effort: '' },
        },
      });
    });

    it('hides a dependent field that the current value makes irrelevant', async () => {
      const target = 'xai/grok-voice-think-fast-2.0::subscription';
      api.getTaskModelOptions.mockResolvedValue({
        fields: [
          {
            name: 'backend_model',
            type: 'select',
            label: 'Backend model',
            default: '',
            options: [
              { value: '', label: 'None (the voice model uses vBot directly)' },
              { value: 'terra', label: 'Terra' },
            ],
          },
          {
            name: 'backend_thinking_effort',
            type: 'select',
            label: 'Backend reasoning',
            default: 'low',
            options: ['', 'low', 'high'].map((value) => ({
              value,
              label: value || 'Model default',
            })),
            options_by: { field: 'backend_model', values: { '': [] } },
          },
        ],
      });
      mountPanel({
        taskTypes: ['live_voice'],
        settings: {
          model_tasks: {
            live_voice: {
              target,
              options: { backend_thinking_effort: 'high' },
            },
          },
        },
      });
      const backendId = 'task-model-live_voice-backend_model';
      const effortId = 'task-model-live_voice-backend_thinking_effort';
      await waitForCondition(() => document.getElementById(backendId));

      expect(document.getElementById(effortId)).toBeNull();
      document.getElementById(backendId).click();
      flushSync();
      [...document.querySelectorAll('[role="option"]')]
        .find((option) => option.textContent.trim() === 'Terra')
        .click();
      flushSync();
      expect(document.getElementById(effortId).textContent.trim()).toBe('high');
    });
  });
});
