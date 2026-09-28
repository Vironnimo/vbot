// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';
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
  getButton,
  getButtonByAriaLabel,
  submitAgentForm,
  temperatureInput,
  getAgentUpdateCalls,
  flushAsyncUpdates,
  createAgentsRpcMock,
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

const ANTHROPIC_MODEL = 'anthropic/claude-sonnet-4-20250219';
const PROVIDER_DEFAULT = () => t('inherit.optionProviderDefault');

function reasoningModel(reasoning) {
  return {
    id: 'openai/gpt-5.2',
    provider_id: 'openai',
    model_id: 'gpt-5.2',
    name: 'GPT-5.2',
    capabilities: { reasoning },
  };
}

async function submitAndReadUpdate() {
  submitAgentForm();
  await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
  return getAgentUpdateCalls()[0][1];
}

describe('AgentsView models', () => {
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

  it('renders each model field once without a duplicate fallback status', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    expect(
      Array.from(
        document.body.querySelectorAll(
          '.agents-view__model-group .s-row-label',
        ),
        (label) => label.textContent.trim(),
      ),
    ).toEqual([
      t('agents.form.model'),
      t('agents.form.thinkingEffort'),
      t('agents.modelOptions'),
      t('agents.form.temperature'),
      t('agents.form.fallbackModels'),
    ]);
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

  it('opens model options and focuses an invalid field when saving the continuous page', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.querySelector('#agent-temperature'));
    const temperature = document.querySelector('#agent-temperature');
    const toggle = document.querySelector('#agent-model-options-toggle');
    const options = document.querySelector('#agent-model-options');
    expect(options.contains(temperature)).toBe(true);
    expect(toggle.getAttribute('aria-controls')).toBe('agent-model-options');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(options.hidden).toBe(true);
    toggle.click();
    flushSync();
    expect(options.hidden).toBe(false);
    setTextInputValue('agent-temperature', 'invalid');
    toggle.click();
    flushSync();
    expect(options.hidden).toBe(true);
    getButton(t('common.save')).click();
    await waitForCondition(() => document.activeElement === temperature);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(options.hidden).toBe(false);
    expect(temperature.getAttribute('aria-invalid')).toBe('true');
  });

  it('lists canonical model ids without a suffix when each Provider has one usable connection', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          { ...baseAgent(), fallback_models: [`${ANTHROPIC_MODEL}::api-key`] },
        ],
        connections: [
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        modelTriggerLabel() === 'openai/gpt-5.2' &&
        fallbackTriggerLabel() === ANTHROPIC_MODEL,
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('model.list');

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');
    await openSearchableDropdown('agent-fallback-model-0');
    const fallbackOptionLabels = searchableOptionLabels(
      'agent-fallback-model-0',
    );

    for (const labels of [modelOptionLabels, fallbackOptionLabels]) {
      expect(labels).toContain('openai/gpt-5.2');
      expect(labels).toContain(ANTHROPIC_MODEL);
      expect(labels).not.toContain('openai/gpt-5.2 (API Key)');
      expect(labels).not.toContain('openai / GPT-5.2');
    }
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

  it('preserves a saved unavailable model value in the searchable dropdown', async () => {
    const unavailable = t('agents.form.modelUnavailableOption', {
      model: 'legacy/custom-model',
    });
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), model: 'legacy/custom-model' }],
        models: [openaiModel()],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === unavailable, 100);

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');
    expect(modelOptionLabels).toContain(unavailable);
    expect(modelOptionLabels).toContain('openai/gpt-5.2');
  });

  it('keeps a saved unsuffixed model available and omits unchanged resolved fields on save', async () => {
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
      t('agents.form.modelUnavailableOption', {
        model: 'openai/gpt-5.2',
      }),
    );

    setTextInputValue('agent-name', 'Alpha Prime');

    expect(await submitAndReadUpdate()).toEqual({
      id: 'alpha',
      name: 'Alpha Prime',
    });
  });

  it('filters searchable options and sends the selected model and fallback with connection suffixes', async () => {
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

    // The connection suffix appears once both the Agent and the connection
    // catalog have loaded.
    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2 (API Key)',
      100,
    );

    await openSearchableDropdown('agent-model');
    setSearchableFilter('agent-model', 'Plus');
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
    selectSearchableOption('agent-fallback-model-0', ANTHROPIC_MODEL);
    await waitForCondition(
      () => fallbackTriggerLabel() === ANTHROPIC_MODEL,
      100,
    );

    expect(await submitAndReadUpdate()).toMatchObject({
      id: 'alpha',
      model: 'openai/gpt-5.2::subscription',
      fallback_models: [`${ANTHROPIC_MODEL}::api-key`],
    });
  });

  it('sends empty values after clearing model, fallback rows, temperature and thinking effort', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            thinking_effort: 'high',
            fallback_models: [`${ANTHROPIC_MODEL}::api-key`],
          },
        ],
        connections: [
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        modelTriggerLabel() === 'openai/gpt-5.2' &&
        fallbackTriggerLabel() === ANTHROPIC_MODEL &&
        thinkingTriggerLabel() === 'high',
      100,
    );

    const temperature = temperatureInput();
    temperature.value = '';
    temperature.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('agent-thinking-effort');
    selectSimpleOption('agent-thinking-effort', PROVIDER_DEFAULT());

    const notConfigured = t('inherit.optionNotConfigured');
    await openSearchableDropdown('agent-model');
    selectSearchableOption('agent-model', notConfigured);
    await waitForCondition(() => modelTriggerLabel() === notConfigured, 100);

    // Removing the only chain row clears the fallback list.
    document.body
      .querySelector('.agents-view__fallback-remove')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(await submitAndReadUpdate()).toMatchObject({
      id: 'alpha',
      model: '',
      fallback_models: [],
      temperature: null,
      thinking_effort: null,
    });
  });

  it('disables the thinking-effort dropdown for a non-reasoning model', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          { ...baseAgent(), model: 'openai/gpt-5.2', thinking_effort: '' },
        ],
        models: [reasoningModel({ supported: false, levels: [] })],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    expect(getSimpleTrigger('agent-thinking-effort').disabled).toBe(true);
    expect(
      document.body.querySelector('#agent-thinking-effort-help').textContent,
    ).toContain(t('agents.form.thinkingEffortUnsupported'));
  });

  it('offers exactly the reasoning ladder of the model and applies a selected effort', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          { ...baseAgent(), model: 'openai/gpt-5.2', thinking_effort: '' },
        ],
        models: [
          reasoningModel({ supported: true, levels: ['high', 'xhigh'] }),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);
    expect(getSimpleTrigger('agent-thinking-effort').disabled).toBe(false);

    openSimpleDropdown('agent-thinking-effort');
    // The inherit option (empty value) and "none" always apply; the rest are
    // exactly the ladder. With no effective source in the fixture the inherit
    // option reads as the provider-default variant.
    expect(simpleOptionLabels('agent-thinking-effort')).toEqual([
      PROVIDER_DEFAULT(),
      'none',
      'high',
      'xhigh',
    ]);

    selectSimpleOption('agent-thinking-effort', 'high');
    await waitForCondition(() => thinkingTriggerLabel() === 'high', 100);
  });

  it('labels the model and thinking inherit options from the effective global default', async () => {
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
    const inherited = (value) => t('inherit.option', { value });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    // The raw model is empty, so the field is in the inherit state; its trigger
    // shows the global-default inherit label.
    await waitForCondition(
      () => modelTriggerLabel() === inherited('openai/gpt-5.2'),
      100,
    );
    expect(document.querySelectorAll('.agents-view__fallback-row').length).toBe(
      0,
    );
    expect(thinkingTriggerLabel()).toBe(inherited('high'));
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

  it('reloads the catalog on modelsRefreshToken and defers the option swap while a picker is open', async () => {
    let models = [openaiModel()];
    const handler = createAgentsRpcMock({
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key'),
        usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
      ],
    });
    rpcMock.mockImplementation(async (method, params) =>
      method === 'model.list' ? { models } : handler(method, params),
    );
    const refreshCatalog = async (nextModels, token) => {
      const modelListBefore = modelListCallCount();
      const connectionListBefore = connectionListCallCount();
      models = nextModels;
      props.modelsRefreshToken = token;
      flushSync();
      await waitForCondition(() => modelListCallCount() > modelListBefore);
      await flushAsyncUpdates(6);
      expect(connectionListCallCount()).toBeGreaterThan(connectionListBefore);
    };

    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(AgentsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2');

    // A model DB refresh elsewhere bumps the token; with every picker closed
    // the new catalog applies at once, without a remount.
    await refreshCatalog([openaiModel(), anthropicModel()], 1);
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).toContain(ANTHROPIC_MODEL);

    // A reload that arrives while the picker is open must not change the open
    // option list underfoot.
    await refreshCatalog([openaiModel()], 2);
    expect(getSearchableRoot('agent-model').dataset.state).toBe('open');
    expect(searchableOptionLabels('agent-model')).toContain(ANTHROPIC_MODEL);

    // Closing the picker applies the deferred swap.
    getSearchableTrigger('agent-model').dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    flushSync();
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).not.toContain(
      ANTHROPIC_MODEL,
    );
  });
});
