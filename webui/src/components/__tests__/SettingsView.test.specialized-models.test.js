// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  getSimpleList,
  getSimpleTrigger,
  openRecallPanel,
  openSimpleDropdown,
  openSpecializedModelsPanel,
  resetSettingsViewHarness,
  rpcMock,
  selectSimpleOption,
  setTextareaValue,
  settingsPayload,
  SettingsView,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  it('renders a json option field and stores the parsed structure on valid input', async () => {
    const target = 'openrouter/recraft/recraft-v3::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'recraft/recraft-v3',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Recraft v3',
            task_types: ['image_generation'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'image_generation',
              target,
              fields: [
                {
                  name: 'text_layout',
                  type: 'json',
                  label: 'Text layout',
                  default: [],
                  description: 'Array of {text, bbox} entries (recraft-v3).',
                },
              ],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    openSimpleDropdown('settings-specialized-image_generation');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption('settings-specialized-image_generation', 'Recraft v3');

    await waitForCondition(
      () => document.body.querySelector('.text-area--code') !== null,
    );

    const jsonTextarea = document.body.querySelector('.text-area--code');
    expect(jsonTextarea).toBeTruthy();
    // Default empty array serializes to "[]" — confirms the renderer
    // stringifies structured defaults rather than treating them as text.
    expect(jsonTextarea.value).toBe('[]');
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('false');

    const validText = JSON.stringify([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
    setTextareaValue('.text-area--code', validText);

    await waitForCondition(
      () =>
        document.body.querySelector('.form-field__error') === null &&
        jsonTextarea.getAttribute('aria-invalid') === 'false',
    );

    // Save and inspect what reached the settings update — must be the
    // parsed array, not the raw JSON string.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.update' &&
          call[1]?.model_tasks?.image_generation?.options?.text_layout !==
            undefined,
      ),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.image_generation.target).toBe(target);
    expect(
      updateCall[1].model_tasks.image_generation.options.text_layout,
    ).toEqual([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
  });

  it('shows an inline parse error for invalid JSON and does not update the binding', async () => {
    const target = 'openrouter/recraft/recraft-v3::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'recraft/recraft-v3',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Recraft v3',
            task_types: ['image_generation'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'image_generation',
              target,
              fields: [
                {
                  name: 'text_layout',
                  type: 'json',
                  label: 'Text layout',
                  default: [],
                },
              ],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    openSimpleDropdown('settings-specialized-image_generation');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption('settings-specialized-image_generation', 'Recraft v3');

    await waitForCondition(
      () => document.body.querySelector('.text-area--code') !== null,
    );

    setTextareaValue('.text-area--code', '[{"text": "hi"');

    await waitForCondition(
      () => document.body.querySelector('.form-field__error') !== null,
    );

    const jsonTextarea = document.body.querySelector('.text-area--code');
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('true');
    expect(document.body.querySelector('.form-field__error')).toBeTruthy();
    expect(document.body.textContent).toContain('Invalid JSON');

    // The parse error means the typed text was NOT applied to the
    // binding — saving now persists only the default `[]` (from the
    // schema), not the malformed string the user typed.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'task_model.update'),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.image_generation.target).toBe(target);
    // The options either contain the parsed default (empty array) or no
    // text_layout key at all — never the malformed input string.
    const savedTextLayout =
      updateCall[1].model_tasks.image_generation.options?.text_layout;
    expect(savedTextLayout).not.toBe('[{"text": "hi"');
    expect(
      savedTextLayout === undefined || Array.isArray(savedTextLayout),
    ).toBe(true);

    // Repairing the input clears the error and lets the binding update.
    setTextareaValue(
      '.text-area--code',
      JSON.stringify([
        {
          text: 'repaired',
          bbox: [
            [0, 0],
            [1, 1],
          ],
        },
      ]),
    );

    await waitForCondition(
      () => document.body.querySelector('.form-field__error') === null,
    );
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('false');
  });

  it('renders the Recall picker with the vector backend when available', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: {
          ...settingsPayload(),
          recall: {
            backend: 'jsonl_scan',
            available_backends: ['jsonl_scan', 'sqlite_fts', 'vector'],
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openRecallPanel();

    expect(document.body.textContent).toContain('Recall backend');
    openSimpleDropdown('settings-recall-backend');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption(
      'settings-recall-backend',
      'Semantic — finds matches by meaning, needs an embedding model',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      recall: {
        backend: 'vector',
      },
    });
  });

  it('renders and saves the embedding model row in the Specialized Models panel', async () => {
    const target = 'openrouter/google/gemini-embedding-2::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'google/gemini-embedding-2',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Gemini Embedding 2',
            task_types: ['text_embedding'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'text_embedding',
              target,
              fields: [],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    // The row title renders the i18n label, the panel called
    // list_targets for text_embedding alongside the other task types,
    // and the dropdown is in the DOM.
    expect(document.body.textContent).toContain('Embedding model');
    expect(document.body.textContent).toContain(
      'Turns text into numeric vectors for meaning-based search. Required when Recall is set to Semantic.',
    );
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.list_targets' &&
          call[1]?.task_type === 'text_embedding',
      ),
    ).toBe(true);

    const embeddingTrigger = getSimpleTrigger(
      'settings-specialized-text_embedding',
    );
    expect(embeddingTrigger).toBeTruthy();
    expect(embeddingTrigger.textContent).toContain('Not configured');

    openSimpleDropdown('settings-specialized-text_embedding');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption(
      'settings-specialized-text_embedding',
      'Gemini Embedding 2',
    );

    // Manually click Save to bypass the auto-save debounce for a
    // deterministic assertion on the exact persisted payload.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.update' &&
          call[1]?.model_tasks?.text_embedding?.target === target,
      ),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.text_embedding.target).toBe(target);
    expect(updateCall[1].model_tasks.text_embedding.options).toEqual({});
  });
});
