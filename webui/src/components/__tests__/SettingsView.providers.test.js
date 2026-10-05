// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  buttonByAriaLabel,
  buttonByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  openProvidersPanel,
  openSubAgentsPanel,
  provider,
  providerRow,
  resetSettingsViewHarness,
  rpcMock,
  setInputValue,
  settingsPayload,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsView } = await import('../SettingsView.svelte');

const DEPTH = '#settings-subagents-max-depth';
const REFRESH = 'Update Model DB';

function refreshButtons(root = document) {
  return root.querySelectorAll(`button[aria-label="${REFRESH}"]`);
}

function modelListCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'model.list').length;
}

function localProvider() {
  const ollama = provider('ollama', 'Ollama', '/api/tags');
  ollama.connections = [
    {
      id: 'ollama:local',
      type: 'none',
      label: 'Local',
      added: true,
      configured: true,
      accounts: [{ id: 'default', usable: true, source: 'none' }],
    },
  ];
  return ollama;
}

// Serves the first settings.get from the backend and every later read
// (a Provider refresh) from `refreshReads`, in order; the last one repeats.
function withRefreshReads(backend, refreshReads) {
  const reads = { count: 0 };
  rpcMock.mockImplementation((method, params) => {
    if (method === 'settings.get' && ++reads.count > 1) {
      return refreshReads[Math.min(reads.count - 2, refreshReads.length - 1)]();
    }
    return backend(method, params);
  });
  return reads;
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe('SettingsView Providers', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  function mountView(props = {}) {
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
  }

  describe('Model DB refresh', () => {
    it.each([
      ['any provider appears refresh-eligible', true, 1],
      ['no provider appears refresh-eligible', false, 0],
    ])(
      'shows one global refresh button only when %s',
      async (_label, eligible, expectedButtons) => {
        rpcMock.mockImplementation(
          createSettingsRpcMock({
            settings: eligible
              ? settingsPayload({ includeSecondEligibleProvider: true })
              : settingsPayload({ eligibleProvider: false }),
          }),
        );
        mountView();
        await openProvidersPanel();

        expect(refreshButtons()).toHaveLength(expectedButtons);
        if (eligible) {
          expect(refreshButtons(providerRow('OpenRouter'))).toHaveLength(0);
          expect(refreshButtons(providerRow('Groq'))).toHaveLength(0);
        }
      },
    );

    it('refreshes the global model database, toasts success, and updates counts', async () => {
      const toastMock = vi.fn();
      const refresh = deferred();
      rpcMock.mockImplementation(
        createSettingsRpcMock({
          settings: settingsPayload({ includeSecondEligibleProvider: true }),
          refreshResult: refresh.promise,
        }),
      );
      mountView({ onToast: toastMock });
      await openProvidersPanel();

      buttonByAriaLabel(REFRESH).click();
      flushSync();
      expect(buttonByText('Updating…')).toBeTruthy();
      // One global refresh, never a per-provider one.
      expect(rpcMock).toHaveBeenCalledWith('model.refresh_db');
      expect(
        rpcMock.mock.calls.some(
          (call) => call[0] === 'model.refresh_db' && call[1]?.provider_id,
        ),
      ).toBe(false);

      refresh.resolve({
        providers: [
          {
            provider_id: 'openrouter',
            model_count: 2,
            fetched_at: '2026-05-08T19:08:00+00:00',
          },
          {
            provider_id: 'groq',
            model_count: 3,
            fetched_at: '2026-05-08T19:08:00+00:00',
          },
        ],
        refreshed_count: 2,
        model_count: 5,
      });
      await waitForCondition(() =>
        toastMock.mock.calls.some(([toast]) => toast?.variant === 'success'),
      );
      await waitForCondition(() =>
        /\b2\b/u.test(providerRow('OpenRouter').textContent),
      );
      expect(providerRow('Groq').textContent).toMatch(/\b3\b/u);
      expect(modelListCalls()).toBeGreaterThan(0);
    });

    it('accepts the compatible single-provider refresh result', async () => {
      const toastMock = vi.fn();
      rpcMock.mockImplementation(
        createSettingsRpcMock({
          refreshResult: {
            provider_id: 'openrouter',
            model_count: 2,
            fetched_at: '2026-05-08T19:08:00+00:00',
          },
        }),
      );
      mountView({ onToast: toastMock });
      await openProvidersPanel();
      const modelListBefore = modelListCalls();

      buttonByAriaLabel(REFRESH).click();
      await waitForCondition(() =>
        toastMock.mock.calls.some(([toast]) => toast?.variant === 'success'),
      );
      await waitForCondition(() => modelListCalls() > modelListBefore);
    });

    it('toasts refresh errors and skips the model list reload', async () => {
      const toastMock = vi.fn();
      rpcMock.mockImplementation(
        createSettingsRpcMock({ refreshError: new Error('fetch failed') }),
      );
      mountView({ onToast: toastMock });
      await openProvidersPanel();
      // Other always-mounted sections load the model catalog on mount; only
      // the refresh-triggered reload must be absent.
      const modelListBefore = modelListCalls();

      buttonByAriaLabel(REFRESH).click();
      // A sticky error toast carries the server detail as its message.
      await waitForCondition(() =>
        toastMock.mock.calls.some(
          ([toast]) =>
            toast?.variant === 'error' &&
            toast?.message?.includes('fetch failed'),
        ),
      );
      expect(modelListCalls()).toBe(modelListBefore);
    });
  });

  it('renders a keyless connection and saves a local model context window', async () => {
    const settings = settingsPayload();
    settings.providers.items.push(localProvider());
    settings.local_models = { context_windows: {} };
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings,
        models: [
          {
            id: 'ollama/ministral-3:8b',
            provider_id: 'ollama',
            model_id: 'ministral-3:8b',
            name: 'ministral-3:8b',
            capabilities: { tools: true },
            context_window: 262144,
            effective_context_window: 32768,
            local: true,
          },
        ],
      }),
    );
    mountView();
    await openProvidersPanel();

    // Keyless connection: listed without key management actions.
    await waitForCondition(() =>
      providerRow('Ollama').querySelector('.s-provider-connection-row'),
    );
    expect(
      providerRow('Ollama').querySelector('.s-provider-connection-label')
        .textContent,
    ).toContain('Local');
    expect(providerRow('Ollama').textContent).not.toContain('Replace key');

    // The local-context editor lists the flagged-local model.
    await waitForCondition(() =>
      document.querySelector('.s-local-context-input'),
    );
    const input = document.querySelector('.s-local-context-input');
    expect(input.placeholder).toBe('32768');
    input.value = '16384';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      local_models: {
        context_windows: { 'ollama/ministral-3:8b': 16384 },
      },
    });
  });

  // A Provider refresh (modelsRefreshToken) re-reads settings and applies
  // only Provider and local Model state; other editors keep their drafts.
  describe('Provider refresh', () => {
    it('keeps an unrelated draft and in-flight save mounted across a refresh', async () => {
      const save = deferred();
      rpcMock.mockImplementation(
        createSettingsRpcMock({ settingsUpdate: () => save.promise }),
      );
      const props = reactiveProps({ modelsRefreshToken: 0 });
      mountView(props);
      await openSubAgentsPanel();
      vi.useFakeTimers();
      const input = document.querySelector(DEPTH);
      setInputValue(DEPTH, '6');
      getButton('Save').click();
      await flushAsyncUpdates();
      expect(getSettingsUpdateCalls()).toHaveLength(1);
      setInputValue(DEPTH, '7');
      input.focus();

      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      expect(document.querySelector(DEPTH)).toBe(input);
      expect(document.activeElement).toBe(input);
      expect(input.value).toBe('7');
      save.resolve(null);
      await flushAsyncUpdates();
      expect(input.value).toBe('7');
      expect(getSettingsUpdateCalls()).toHaveLength(1);
    });

    it('retains editors on refresh failure and limits a later refresh to Provider and local Model state', async () => {
      const initial = settingsPayload();
      initial.providers.items.push(localProvider());
      initial.local_models = { context_windows: {} };
      const refreshed = structuredClone(initial);
      refreshed.providers.items[0].name = 'Updated Provider';
      refreshed.providers.items[0].routing.default.allow_fallbacks = false;
      refreshed.local_models.context_windows['ollama/test-model'] = 16384;
      refreshed.subagents.max_subagent_depth = 12;
      refreshed.appearance.chat_width = 'full';
      withRefreshReads(
        createSettingsRpcMock({
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
        }),
        [
          () => Promise.reject(new Error('refresh unavailable')),
          () => Promise.resolve(refreshed),
        ],
      );
      const onToast = vi.fn();
      const props = reactiveProps({ modelsRefreshToken: 0, onToast });
      mountView(props);
      await openSubAgentsPanel();
      vi.useFakeTimers();
      const input = document.querySelector(DEPTH);
      input.focus();
      setInputValue(DEPTH, '7');

      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      expect(document.querySelector(DEPTH)).toBe(input);
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
      expect(getSettingsUpdateCalls()).toEqual([
        [
          'settings.update',
          {
            subagents: {
              max_subagent_depth: 7,
              max_active_subagents: 8,
            },
            base: {
              subagents: {
                max_subagent_depth: 4,
                max_active_subagents: 8,
              },
            },
          },
        ],
      ]);
    });

    it('ignores an older refresh response after a newer Provider snapshot has arrived', async () => {
      const initial = settingsPayload();
      const refreshed = structuredClone(initial);
      refreshed.providers.items[0].name = 'Newest Provider';
      const oldRefresh = deferred();
      withRefreshReads(createSettingsRpcMock({ settings: initial }), [
        () => oldRefresh.promise,
        () => Promise.resolve(refreshed),
      ]);
      const props = reactiveProps({ modelsRefreshToken: 0 });
      mountView(props);
      await openSubAgentsPanel();
      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      expect(document.body.textContent).toContain('Newest Provider');

      oldRefresh.resolve(initial);
      await flushAsyncUpdates();
      expect(document.body.textContent).toContain('Newest Provider');
    });

    it.each([true, false])(
      'preserves Provider state when refresh finishes before the older Settings save: %s',
      async (refreshFinishesFirst) => {
        const initial = settingsPayload();
        const refreshed = structuredClone(initial);
        refreshed.providers.items[0].name = 'Changed during save';
        const save = deferred();
        const oldRefresh = deferred();
        const reads = withRefreshReads(
          createSettingsRpcMock({
            settings: initial,
            settingsUpdate: () => save.promise,
          }),
          [() => oldRefresh.promise, () => Promise.resolve(refreshed)],
        );
        const onSettingsCommit = vi.fn();
        const props = reactiveProps({
          modelsRefreshToken: 0,
          onSettingsCommit,
        });
        mountView(props);
        await openSubAgentsPanel();
        setInputValue(DEPTH, '6');
        getButton('Save').click();
        await flushAsyncUpdates();

        props.modelsRefreshToken += 1;
        await flushAsyncUpdates();
        expect(reads.count).toBe(2);
        if (refreshFinishesFirst) {
          oldRefresh.resolve(refreshed);
          await flushAsyncUpdates();
        }
        save.resolve(null);
        await flushAsyncUpdates();
        if (!refreshFinishesFirst) {
          oldRefresh.resolve(refreshed);
          await flushAsyncUpdates();
        }
        expect(reads.count).toBe(2);
        expect(document.body.textContent).toContain('Changed during save');
        expect(onSettingsCommit).toHaveBeenLastCalledWith(
          expect.objectContaining({ providers: refreshed.providers }),
        );
        expect(document.querySelector(DEPTH).value).toBe('6');
        expect(getSettingsUpdateCalls()).toHaveLength(1);
      },
    );

    it('rechecks a pending invalidation when the Model DB operation commits its Provider counts', async () => {
      const initial = settingsPayload();
      const refreshed = structuredClone(initial);
      refreshed.providers.items[0].name =
        'Provider changed during Model refresh';
      refreshed.providers.items[0].model_count = 5;
      const modelRefresh = deferred();
      const oldRefresh = deferred();
      const reads = withRefreshReads(
        createSettingsRpcMock({
          settings: initial,
          refreshResult: modelRefresh.promise,
        }),
        [() => oldRefresh.promise, () => Promise.resolve(refreshed)],
      );
      const onSettingsCommit = vi.fn();
      const props = reactiveProps({ modelsRefreshToken: 0, onSettingsCommit });
      mountView(props);
      await openProvidersPanel();
      buttonByAriaLabel(REFRESH).click();
      await flushAsyncUpdates();
      props.modelsRefreshToken += 1;
      await flushAsyncUpdates();
      expect(reads.count).toBe(2);

      modelRefresh.resolve({
        providers: [{ provider_id: 'openrouter', model_count: 5 }],
        refreshed_count: 1,
        model_count: 5,
      });
      await flushAsyncUpdates();
      expect(reads.count).toBe(3);
      oldRefresh.resolve(initial);
      await flushAsyncUpdates();
      expect(document.body.textContent).toContain(
        'Provider changed during Model refresh',
      );
      expect(onSettingsCommit).toHaveBeenLastCalledWith(
        expect.objectContaining({ providers: refreshed.providers }),
      );
    });
  });
});
