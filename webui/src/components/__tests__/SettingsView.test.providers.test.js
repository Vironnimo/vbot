// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  buttonByText,
  buttonsByText,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  getSettingsUpdateCalls,
  openProvidersPanel,
  providerRow,
  resetSettingsViewHarness,
  rpcMock,
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

  it('shows one global refresh button when any provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(1);
    expect(providerRow('OpenRouter').textContent).not.toContain(
      'Update Model DB',
    );
    expect(providerRow('Groq').textContent).not.toContain('Update Model DB');
  });

  it('hides the global refresh button when no provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ eligibleProvider: false }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(0);
  });

  it('refreshes the global model database, toasts success, and reloads model list', async () => {
    const toastMock = vi.fn();
    let resolveRefresh;
    const refreshPromise = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
        refreshResult: refreshPromise,
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    flushSync();

    expect(buttonByText('Updating…')).toBeTruthy();
    expect(rpcMock).toHaveBeenCalledWith('model.refresh_db');
    expect(
      rpcMock.mock.calls.some(
        (call) => call[0] === 'model.refresh_db' && call[1]?.provider_id,
      ),
    ).toBe(false);

    resolveRefresh({
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
    // The refresh result is now a success toast (auto-dismiss), not inline
    // text — an inline result would flash and vanish when the settings reload
    // briefly unmounts the panel.
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'success' &&
          toast?.title === 'Model DB updated: 2 providers, 5 models available.',
      ),
    );

    // The updated per-provider model counts still land in the provider rows.
    await waitForCondition(() =>
      providerRow('OpenRouter').textContent.includes('2 models available.'),
    );
    expect(providerRow('Groq').textContent).toContain('3 models available.');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('toasts the compatible single-provider refresh result and updates counts', async () => {
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

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'success' &&
          toast?.title === 'Model DB updated: 1 providers, 2 models available.',
      ),
    );

    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('toasts refresh errors and skips model list reload on failure', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshError: new Error('fetch failed'),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();

    // Other always-mounted sections (Defaults, Compaction) load the model
    // catalog on mount; only the refresh-triggered reload must be absent.
    const modelListCallsBeforeRefresh = rpcMock.mock.calls.filter(
      (call) => call[0] === 'model.list',
    ).length;

    buttonByText('Update Model DB').click();
    // The failure is a sticky error toast (routed through the unified settings
    // error seam), carrying the server detail as the toast message.
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'error' &&
          toast?.message === 'Model DB could not be updated. fetch failed',
      ),
    );

    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'model.list').length,
    ).toBe(modelListCallsBeforeRefresh);
  });

  it('renders a keyless connection and the local model context editor', async () => {
    const ollama = {
      id: 'ollama',
      name: 'Ollama',
      base_url: 'http://localhost:11434',
      models_endpoint: '/api/tags',
      connections: [
        {
          id: 'ollama:local',
          type: 'none',
          label: 'Local',
          added: true,
          configured: true,
          accounts: [{ id: 'default', usable: true, source: 'none' }],
        },
      ],
      credentials_configured: true,
      status: 'configured',
      model_count: 1,
      kind: 'remote',
      editable: false,
    };
    const settings = settingsPayload();
    settings.providers.items.push(ollama);
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

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    // Keyless connection: descriptive text, no key management actions.
    await waitForCondition(() =>
      document.body.textContent.includes('No key required'),
    );
    expect(providerRow('Ollama').textContent).not.toContain('Replace key');

    // The local-context editor lists the flagged-local model.
    await waitForCondition(() =>
      document.body.textContent.includes('Local model context'),
    );
    const input = document.body.querySelector('.s-local-context-input');
    expect(input).toBeTruthy();
    expect(input.placeholder).toBe('32768');

    // Setting a value writes the sparse local_models update.
    input.value = '16384';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      local_models: {
        context_windows: { 'ollama/ministral-3:8b': 16384 },
      },
    });
  });
});
