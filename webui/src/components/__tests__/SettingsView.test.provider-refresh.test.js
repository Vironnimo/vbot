// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';
import { reactiveProps } from './_reactiveProps.svelte.js';
import {
  cleanupSettingsViewHarness,
  buttonByText,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  openSubAgentsPanel,
  openProvidersPanel,
  provider,
  resetSettingsViewHarness,
  rpcMock,
  setInputValue,
  settingsPayload,
  SettingsView,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

describe('SettingsView Provider refresh', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  it('keeps an unrelated draft and in-flight save mounted across a Provider refresh', async () => {
    let finishSave;
    const base = createSettingsRpcMock({
      settingsUpdate: () =>
        new Promise((resolve) => {
          finishSave = resolve;
        }),
    });
    const props = reactiveProps({ modelsRefreshToken: 0 });
    rpcMock.mockImplementation(base);
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openSubAgentsPanel();
    vi.useFakeTimers();
    const input = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    setInputValue('input[aria-label="Max sub-agent depth"]', '6');
    getButton('Save').click();
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
    setInputValue('input[aria-label="Max sub-agent depth"]', '7');
    input.focus();

    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    expect(
      document.querySelector('input[aria-label="Max sub-agent depth"]'),
    ).toBe(input);
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe('7');
    finishSave(null);
    await flushAsyncUpdates();
    expect(input.value).toBe('7');
    expect(getSettingsUpdateCalls()).toHaveLength(1);
  });

  it('retains editors on refresh failure and limits a later refresh to Provider and local Model state', async () => {
    const initial = settingsPayload();
    const localProvider = provider('ollama', 'Ollama', '/api/tags');
    localProvider.connections = [
      {
        id: 'ollama:local',
        type: 'none',
        label: 'Local',
        added: true,
        configured: true,
        accounts: [{ id: 'default', usable: true, source: 'none' }],
      },
    ];
    initial.providers.items.push(localProvider);
    initial.local_models = { context_windows: {} };
    const refreshed = structuredClone(initial);
    refreshed.providers.items[0].name = 'Updated Provider';
    refreshed.providers.items[0].routing.default.allow_fallbacks = false;
    refreshed.local_models.context_windows['ollama/test-model'] = 16384;
    refreshed.subagents.max_subagent_depth = 12;
    refreshed.appearance.chat_width = 'full';
    const base = createSettingsRpcMock({
      settings: initial,
      models: [
        {
          id: 'ollama/test-model',
          provider_id: 'ollama',
          model_id: 'test-model',
          name: 'Test Model',
          local: true,
          context_window: 32768,
        },
      ],
    });
    let readCount = 0;
    const onToast = vi.fn();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.get' && ++readCount > 1) {
        if (readCount === 2)
          return Promise.reject(new Error('refresh unavailable'));
        return Promise.resolve(refreshed);
      }
      return base(method, params);
    });
    const props = reactiveProps({ modelsRefreshToken: 0, onToast });
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openSubAgentsPanel();
    vi.useFakeTimers();
    const input = document.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    input.focus();
    setInputValue('input[aria-label="Max sub-agent depth"]', '7');

    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    expect(
      document.querySelector('input[aria-label="Max sub-agent depth"]'),
    ).toBe(input);
    expect(input.value).toBe('7');
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );

    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    expect(document.body.textContent).toContain('Updated Provider');
    expect(document.querySelector('.s-local-context-input').value).toBe(
      '16384',
    );
    expect(
      document
        .querySelector('.openrouter-routing [role="switch"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
    expect(input.value).toBe('7');
    await vi.advanceTimersByTimeAsync(1600);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(0);
    input.blur();
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('ignores an older refresh response after a newer Provider snapshot has arrived', async () => {
    const initial = settingsPayload();
    const refreshed = structuredClone(initial);
    refreshed.providers.items[0].name = 'Newest Provider';
    const base = createSettingsRpcMock({ settings: initial });
    let finishOldRefresh;
    let readCount = 0;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.get' && ++readCount > 1) {
        if (readCount === 2)
          return new Promise((resolve) => {
            finishOldRefresh = resolve;
          });
        return Promise.resolve(refreshed);
      }
      return base(method, params);
    });
    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openSubAgentsPanel();
    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    expect(document.body.textContent).toContain('Newest Provider');
    finishOldRefresh(initial);
    await flushAsyncUpdates();
    expect(document.body.textContent).toContain('Newest Provider');
  });

  it.each([true, false])(
    'preserves Provider state when refresh finishes before the older Settings save: %s',
    async (refreshFinishesFirst) => {
      const initial = settingsPayload();
      const refreshed = structuredClone(initial);
      refreshed.providers.items[0].name = 'Changed during save';
      let finishSave;
      const base = createSettingsRpcMock({
        settings: initial,
        settingsUpdate: () =>
          new Promise((resolve) => {
            finishSave = resolve;
          }),
      });
      let finishOldRefresh;
      let readCount = 0;
      rpcMock.mockImplementation((method, params) => {
        if (method === 'settings.get' && ++readCount > 1) {
          if (readCount === 2)
            return new Promise((resolve) => {
              finishOldRefresh = resolve;
            });
          return Promise.resolve(refreshed);
        }
        return base(method, params);
      });
      const onSettingsCommit = vi.fn();
      const props = reactiveProps({ modelsRefreshToken: 0, onSettingsCommit });
      mountedComponent = mount(SettingsView, { target: document.body, props });
      flushSync();
      await openSubAgentsPanel();
      setInputValue('input[aria-label="Max sub-agent depth"]', '6');
      getButton('Save').click();
      await flushAsyncUpdates();

      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      expect(readCount).toBe(2);
      if (refreshFinishesFirst) {
        finishOldRefresh(refreshed);
        await flushAsyncUpdates();
      }
      finishSave(null);
      await flushAsyncUpdates();
      if (!refreshFinishesFirst) {
        finishOldRefresh(refreshed);
        await flushAsyncUpdates();
      }
      expect(readCount).toBe(2);
      expect(document.body.textContent).toContain('Changed during save');
      expect(onSettingsCommit).toHaveBeenLastCalledWith(
        expect.objectContaining({ providers: refreshed.providers }),
      );
      expect(
        document.querySelector('input[aria-label="Max sub-agent depth"]').value,
      ).toBe('6');
      expect(getSettingsUpdateCalls()).toHaveLength(1);
    },
  );

  it('rechecks a pending invalidation when the Model DB operation commits its Provider counts', async () => {
    const initial = settingsPayload();
    const refreshed = structuredClone(initial);
    refreshed.providers.items[0].name = 'Provider changed during Model refresh';
    refreshed.providers.items[0].model_count = 5;
    let finishModelRefresh;
    const base = createSettingsRpcMock({
      settings: initial,
      refreshResult: new Promise((resolve) => {
        finishModelRefresh = resolve;
      }),
    });
    let finishOldRefresh;
    let readCount = 0;
    rpcMock.mockImplementation((method, params) => {
      if (method === 'settings.get' && ++readCount > 1) {
        if (readCount === 2)
          return new Promise((resolve) => {
            finishOldRefresh = resolve;
          });
        return Promise.resolve(refreshed);
      }
      return base(method, params);
    });
    const onSettingsCommit = vi.fn();
    const props = reactiveProps({ modelsRefreshToken: 0, onSettingsCommit });
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openProvidersPanel();
    buttonByText('Update Model DB').click();
    await flushAsyncUpdates();
    props.modelsRefreshToken += 1;
    await flushAsyncUpdates();
    expect(readCount).toBe(2);
    finishModelRefresh({
      providers: [{ provider_id: 'openrouter', model_count: 5 }],
      refreshed_count: 1,
      model_count: 5,
    });
    await flushAsyncUpdates();
    expect(readCount).toBe(3);
    finishOldRefresh(initial);
    await flushAsyncUpdates();
    expect(document.body.textContent).toContain(
      'Provider changed during Model refresh',
    );
    expect(onSettingsCommit).toHaveBeenLastCalledWith(
      expect.objectContaining({ providers: refreshed.providers }),
    );
  });
});
