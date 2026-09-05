// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';
import { rpcBackedApiMock } from './apiMock.js';
import {
  rpcMock,
  modelListCallCount,
  connectionListCallCount,
  modelTriggerLabel,
  fallbackTriggerLabel,
  thinkingTriggerLabel,
  openSearchableDropdown,
  setSearchableFilter,
  selectSearchableOption,
  searchableOptionLabels,
  getSearchableRoot,
  getSearchableTrigger,
  getSearchablePanel,
  openSimpleDropdown,
  selectSimpleOption,
  simpleOptionLabels,
  getSimpleTrigger,
  setTextInputValue,
  getButtonByAriaLabel,
  temperatureInput,
  flushAsyncUpdates,
  createAgentsRpcMock,
  skillCatalog,
  usableConnection,
  openaiModel,
  anthropicModel,
  baseAgent,
  waitForCondition,
} from './AgentsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: AgentsView } = await import('../AgentsView.svelte');

describe('AgentsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
    window.innerWidth = 1280;
    window.innerHeight = 900;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  it('hides unsuitable models behind the show-all toggle and badges them', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        models: [
          openaiModel(),
          {
            id: 'ollama/tiny',
            provider_id: 'ollama',
            model_id: 'tiny',
            name: 'Tiny',
            capabilities: { tools: false },
            context_window: 262144,
            effective_context_window: 16384,
            local: true,
          },
        ],
        connections: [
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('ollama:local', 'ollama', 'Local'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() !== '', 100);

    // Default view: the unsuitable local model is hidden.
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).not.toContain('ollama/tiny');

    // The footer toggle reveals it with an honest badge.
    const footer = getSearchablePanel('agent-model').querySelector(
      '.searchable-dropdown__footer',
    );
    expect(footer).toBeTruthy();
    footer.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const revealed = Array.from(
      getSearchablePanel('agent-model').querySelectorAll(
        '.searchable-dropdown__option',
      ),
    ).find((option) => option.textContent.includes('ollama/tiny'));
    expect(revealed).toBeTruthy();
    expect(
      revealed.querySelector('.searchable-dropdown__option-meta'),
    ).toBeTruthy();
  });

  it('renders model dropdown options using canonical model ids', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return {
          models: [
            {
              id: 'anthropic/claude-sonnet-4-20250219',
              provider_id: 'anthropic',
              model_id: 'claude-sonnet-4-20250219',
              name: 'Claude Sonnet 4',
              capabilities: { tools: true },
              context_window: 200000,
              effective_context_window: 200000,
            },
            {
              id: 'openai/gpt-5.2',
              provider_id: 'openai',
              model_id: 'gpt-5.2',
              name: 'GPT-5.2',
              capabilities: { tools: true },
              context_window: 256000,
              effective_context_window: 256000,
            },
          ],
        };
      }

      if (method === 'connection.list') {
        return {
          connections: [
            usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
            usableConnection('openai:api-key', 'openai', 'API Key'),
          ],
        };
      }

      if (method === 'tool.list') {
        return { tools: [] };
      }

      if (method === 'skill.list') {
        return skillCatalog();
      }

      if (method === 'agent.list') {
        return {
          agents: [
            {
              id: 'alpha',
              name: 'Alpha',
              model: 'openai/gpt-5.2::api-key',
              fallback_models: ['anthropic/claude-sonnet-4-20250219::api-key'],
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              tool_access: { mode: 'all' },
              allowed_skills: ['*'],
              created_at: '2026-05-08T00:00:00+00:00',
              updated_at: '2026-05-08T00:00:00+00:00',
            },
          ],
        };
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2' && fallbackTriggerLabel(),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('model.list');

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');

    await openSearchableDropdown('agent-fallback-model-0');
    const fallbackOptionLabels = searchableOptionLabels(
      'agent-fallback-model-0',
    );

    expect(modelOptionLabels).toContain('openai/gpt-5.2');
    expect(modelOptionLabels).toContain('anthropic/claude-sonnet-4-20250219');
    expect(modelOptionLabels).not.toContain('openai / GPT-5.2');
    expect(modelOptionLabels).not.toContain('anthropic / Claude Sonnet 4');
    expect(fallbackOptionLabels).toContain('openai/gpt-5.2');
    expect(fallbackOptionLabels).toContain(
      'anthropic/claude-sonnet-4-20250219',
    );
  });

  it('disables the thinking-effort dropdown for a non-reasoning model', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          { ...baseAgent(), model: 'openai/gpt-5.2', thinking_effort: '' },
        ],
        models: [
          {
            id: 'openai/gpt-5.2',
            provider_id: 'openai',
            model_id: 'gpt-5.2',
            name: 'GPT-5.2',
            capabilities: { reasoning: { supported: false, levels: [] } },
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    const trigger = getSimpleTrigger('agent-thinking-effort');
    expect(trigger.disabled).toBe(true);
    expect(
      document.body.querySelector(
        '.agents-view__thinking-field .form-field__help',
      ),
    ).toBeTruthy();
  });

  it('shows only the model ladder options for a reasoning model', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          { ...baseAgent(), model: 'openai/gpt-5.2', thinking_effort: '' },
        ],
        models: [
          {
            id: 'openai/gpt-5.2',
            provider_id: 'openai',
            model_id: 'gpt-5.2',
            name: 'GPT-5.2',
            capabilities: {
              reasoning: { supported: true, levels: ['high', 'xhigh'] },
            },
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    const trigger = getSimpleTrigger('agent-thinking-effort');
    expect(trigger.disabled).toBe(false);

    openSimpleDropdown('agent-thinking-effort');
    const labels = simpleOptionLabels('agent-thinking-effort');
    // The inherit option (empty value) and "none" always apply; the rest are
    // exactly the ladder. With no effective source in the fixture the inherit
    // option reads as the provider-default variant.
    expect(labels).toEqual([
      'Inherit (provider default)',
      'none',
      'high',
      'xhigh',
    ]);
    expect(labels).not.toContain('low');
    expect(labels).not.toContain('medium');
  });

  it('preserves a saved unavailable model value in the searchable dropdown', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return {
          models: [
            {
              id: 'openai/gpt-5.2',
              provider_id: 'openai',
              model_id: 'gpt-5.2',
              name: 'GPT-5.2',
              capabilities: { tools: true },
              context_window: 256000,
              effective_context_window: 256000,
            },
          ],
        };
      }

      if (method === 'connection.list') {
        return {
          connections: [
            usableConnection('openai:api-key', 'openai', 'API Key'),
          ],
        };
      }

      if (method === 'tool.list') {
        return { tools: [] };
      }

      if (method === 'skill.list') {
        return skillCatalog();
      }

      if (method === 'agent.list') {
        return {
          agents: [
            {
              id: 'alpha',
              name: 'Alpha',
              model: 'legacy/custom-model',
              fallback_models: [],
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              tool_access: { mode: 'all' },
              allowed_skills: ['*'],
              created_at: '2026-05-08T00:00:00+00:00',
              updated_at: '2026-05-08T00:00:00+00:00',
            },
          ],
        };
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => modelTriggerLabel() === 'Unavailable / custom: legacy/custom-model',
      100,
    );

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');

    expect(modelTriggerLabel()).toBe(
      'Unavailable / custom: legacy/custom-model',
    );
    expect(modelOptionLabels).toContain(
      'Unavailable / custom: legacy/custom-model',
    );
    expect(modelOptionLabels).toContain('openai/gpt-5.2');
  });

  it('keeps a saved unsuffixed model available while omitting unchanged fields on save', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), model: 'openai/gpt-5.2' }],
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');
    expect(modelOptionLabels).toContain('openai/gpt-5.2');
    expect(modelOptionLabels).toContain('openai/gpt-5.2 (ChatGPT Plus/Pro)');
    expect(modelOptionLabels).toContain('openai/gpt-5.2 (API Key)');
    expect(modelOptionLabels).not.toContain(
      'Unavailable / custom: openai/gpt-5.2',
    );

    setTextInputValue('agent-name', 'Alpha Prime');

    document.body
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true }));
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'agent.update'),
      100,
    );

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'agent.update',
    );
    expect(updateCall[1]).toEqual({
      id: 'alpha',
      name: 'Alpha Prime',
    });
  });

  it('does not send unchanged resolved defaults when editing only the name', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            model: 'openai/gpt-5.2',
            fallback_models: ['openai/gpt-5.2-mini'],
            temperature: '0.6',
            thinking_effort: 'high',
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    setTextInputValue('agent-name', 'Alpha Renamed');

    document.body
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true }));
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'agent.update'),
      100,
    );

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'agent.update',
    );
    expect(updateCall[1]).toEqual({
      id: 'alpha',
      name: 'Alpha Renamed',
    });
  });

  it('renders each model field once without a duplicate fallback status', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    expect(
      document.body.querySelectorAll(
        '.agents-view__model-fields .form-field__label',
      ),
    ).toHaveLength(4);
    expect(document.querySelectorAll('#agent-model')).toHaveLength(1);
    // An empty chain renders no row dropdowns, only the single add affordance.
    expect(
      document.querySelectorAll('[id^="agent-fallback-model-"]'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('.agents-view__fallback-add'),
    ).toHaveLength(1);
    expect(document.querySelectorAll('#agent-thinking-effort')).toHaveLength(1);
    expect(document.querySelectorAll('#agent-temperature')).toHaveLength(1);
  });

  it('renders one usable connection without a label suffix', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [usableConnection('openai:api-key', 'openai', 'API Key')],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);
    await openSearchableDropdown('agent-model');

    const labels = searchableOptionLabels('agent-model');
    expect(labels).toContain('openai/gpt-5.2');
    expect(labels).not.toContain('openai/gpt-5.2 (API Key)');
  });

  it('renders multiple usable connections with connection label suffixes', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2 (API Key)',
      100,
    );
    await openSearchableDropdown('agent-model');

    const labels = searchableOptionLabels('agent-model');
    expect(labels).toContain('openai/gpt-5.2 (ChatGPT Plus/Pro)');
    expect(labels).toContain('openai/gpt-5.2 (API Key)');
  });

  it('filters searchable options and updates trigger labels on selection', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel(), 100);

    await openSearchableDropdown('agent-model');
    setSearchableFilter('agent-model', 'Plus');

    await waitForCondition(
      () => searchableOptionLabels('agent-model').length === 1,
      100,
    );

    expect(searchableOptionLabels('agent-model')).toEqual([
      'openai/gpt-5.2 (ChatGPT Plus/Pro)',
    ]);

    selectSearchableOption('agent-model', 'openai/gpt-5.2 (ChatGPT Plus/Pro)');
    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2 (ChatGPT Plus/Pro)',
      100,
    );

    // The chain starts empty: add a row, then pick the fallback model.
    document.body
      .querySelector('.agents-view__fallback-add')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    await openSearchableDropdown('agent-fallback-model-0');
    setSearchableFilter('agent-fallback-model-0', 'anthropic');
    selectSearchableOption(
      'agent-fallback-model-0',
      'anthropic/claude-sonnet-4-20250219',
    );

    await waitForCondition(
      () => fallbackTriggerLabel() === 'anthropic/claude-sonnet-4-20250219',
      100,
    );
  });

  it('updates thinking effort through the custom simple dropdown', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => thinkingTriggerLabel() === 'Inherit (provider default)',
      100,
    );

    openSimpleDropdown('agent-thinking-effort');
    expect(simpleOptionLabels('agent-thinking-effort')).toContain('high');

    selectSimpleOption('agent-thinking-effort', 'high');
    await waitForCondition(() => thinkingTriggerLabel() === 'high', 100);
  });

  it('treats connection.list failure as a catalog load error', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return { models: [openaiModel()] };
      }

      if (method === 'connection.list') {
        throw new Error('connection catalog failed');
      }

      if (method === 'tool.list') {
        return { tools: [] };
      }

      if (method === 'skill.list') {
        return skillCatalog();
      }

      if (method === 'agent.list') {
        return { agents: [baseAgent()] };
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('connection catalog failed'),
      100,
    );

    expect(document.body.textContent).toContain('connection catalog failed');
    expect(searchableOptionLabels('agent-model')).not.toContain(
      'openai/gpt-5.2',
    );
  });

  it('reloads the model catalog when modelsRefreshToken changes', async () => {
    let models = [openaiModel()];
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return { models };
      }
      if (method === 'connection.list') {
        return {
          connections: [
            usableConnection('openai:api-key', 'openai', 'API Key'),
            usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
          ],
        };
      }
      if (method === 'tool.list') {
        return { tools: [] };
      }
      if (method === 'skill.list') {
        return skillCatalog();
      }
      if (method === 'agent.list') {
        return { agents: [baseAgent()] };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });

    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(AgentsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2');

    const modelListBefore = modelListCallCount();
    const connectionListBefore = connectionListCallCount();

    // A model DB refresh elsewhere bumps the token; the catalog reloads.
    models = [openaiModel(), anthropicModel()];
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => modelListCallCount() > modelListBefore);

    expect(connectionListCallCount()).toBeGreaterThan(connectionListBefore);

    // The freshly added model is now selectable without a remount.
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).toContain(
      'anthropic/claude-sonnet-4-20250219',
    );
  });

  it('defers the visible option swap while a model picker is open', async () => {
    let models = [openaiModel()];
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return { models };
      }
      if (method === 'connection.list') {
        return {
          connections: [
            usableConnection('openai:api-key', 'openai', 'API Key'),
            usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
          ],
        };
      }
      if (method === 'tool.list') {
        return { tools: [] };
      }
      if (method === 'skill.list') {
        return skillCatalog();
      }
      if (method === 'agent.list') {
        return { agents: [baseAgent()] };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });

    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(AgentsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2');

    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).not.toContain(
      'anthropic/claude-sonnet-4-20250219',
    );

    // A reload arrives while the picker is open: fetched in the background, but
    // the open option list must not change underfoot.
    models = [openaiModel(), anthropicModel()];
    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => modelListCallCount() >= 2);
    await flushAsyncUpdates(6);

    expect(getSearchableRoot('agent-model').dataset.state).toBe('open');
    expect(searchableOptionLabels('agent-model')).not.toContain(
      'anthropic/claude-sonnet-4-20250219',
    );

    // Closing the picker applies the deferred swap.
    getSearchableTrigger('agent-model').dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    flushSync();
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).toContain(
      'anthropic/claude-sonnet-4-20250219',
    );
  });

  it('labels the model inherit option from the effective global default', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            config: {
              model: '',
              fallback_models: [],
              temperature: null,
              thinking_effort: null,
            },
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'global_default' },
              fallback_models: { value: null, source: null },
              temperature: { value: 0.7, source: 'global_default' },
              thinking_effort: { value: 'high', source: 'global_default' },
            },
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    // The raw model is empty, so the field is in the inherit state; its trigger
    // shows the global-default inherit label.
    await waitForCondition(
      () =>
        modelTriggerLabel() === 'Inherited: openai/gpt-5.2 (global default)',
      100,
    );
    // The fallback chain is empty → no rows render at all.
    expect(document.querySelectorAll('.agents-view__fallback-row').length).toBe(
      0,
    );
    // The thinking-effort inherit option reads the global-default value too.
    expect(thinkingTriggerLabel()).toBe('Inherited: high (global default)');
  });

  it('shows the temperature inherit hint and a reset affordance', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            temperature: '0.9',
            config: {
              model: 'openai/gpt-5.2::api-key',
              fallback_models: [],
              temperature: 0.9,
              thinking_effort: null,
            },
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'agent' },
              fallback_models: { value: null, source: null },
              temperature: { value: 0.9, source: 'agent' },
              thinking_effort: {
                value: 0.5,
                source: 'global_default',
              },
            },
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => temperatureInput()?.value === '0.9', 100);

    // While a value is typed, a reset-to-inherit affordance is present and the
    // inherit hint is hidden.
    const resetButton = getButtonByAriaLabel('Reset to inherited value');
    expect(resetButton).toBeTruthy();
    expect(document.querySelector('#agent-temperature-help')).toBeNull();

    resetButton.click();
    flushSync();

    // Clearing the field switches to the inherit state: hint appears, reset gone.
    await waitForCondition(() => temperatureInput()?.value === '', 100);
    expect(
      document.body.querySelector('[aria-label="Reset to inherited value"]'),
    ).toBeNull();
    expect(document.querySelector('#agent-temperature-help')).toBeTruthy();
  });
});
