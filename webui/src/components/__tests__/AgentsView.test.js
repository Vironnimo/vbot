// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

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
            },
            {
              id: 'openai/gpt-5.2',
              provider_id: 'openai',
              model_id: 'gpt-5.2',
              name: 'GPT-5.2',
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
              model: 'openai/gpt-5.2',
              connection: 'openai:api-key',
              fallback_model: 'anthropic/claude-sonnet-4-20250219',
              fallback_connection: 'anthropic:api-key',
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              allowed_tools: ['*'],
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

    await openSearchableDropdown('agent-model');
    const modelOptionLabels = searchableOptionLabels('agent-model');

    await openSearchableDropdown('agent-fallback-model');
    const fallbackOptionLabels = searchableOptionLabels('agent-fallback-model');

    expect(modelOptionLabels).toContain('openai/gpt-5.2');
    expect(modelOptionLabels).toContain('anthropic/claude-sonnet-4-20250219');
    expect(modelOptionLabels).not.toContain('openai / GPT-5.2');
    expect(modelOptionLabels).not.toContain('anthropic / Claude Sonnet 4');
    expect(fallbackOptionLabels).toContain('openai/gpt-5.2');
    expect(fallbackOptionLabels).toContain(
      'anthropic/claude-sonnet-4-20250219',
    );
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
              connection: '',
              fallback_model: '',
              fallback_connection: '',
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              allowed_tools: ['*'],
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
          usableConnection('openai:oauth', 'openai', 'OAuth'),
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
    expect(labels).toContain('openai/gpt-5.2 (OAuth)');
    expect(labels).toContain('openai/gpt-5.2 (API Key)');
  });

  it('filters searchable options and updates trigger labels on selection', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel(), 100);

    await openSearchableDropdown('agent-model');
    setSearchableFilter('agent-model', 'oauth');

    await waitForCondition(
      () => searchableOptionLabels('agent-model').length === 1,
      100,
    );

    expect(searchableOptionLabels('agent-model')).toEqual([
      'openai/gpt-5.2 (OAuth)',
    ]);

    selectSearchableOption('agent-model', 'openai/gpt-5.2 (OAuth)');
    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2 (OAuth)',
      100,
    );

    await openSearchableDropdown('agent-fallback-model');
    setSearchableFilter('agent-fallback-model', 'anthropic');
    selectSearchableOption(
      'agent-fallback-model',
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

    await waitForCondition(() => thinkingTriggerLabel() === 'Default', 100);

    openSimpleDropdown('agent-thinking-effort');
    expect(simpleOptionLabels('agent-thinking-effort')).toContain('high');

    selectSimpleOption('agent-thinking-effort', 'high');
    await waitForCondition(() => thinkingTriggerLabel() === 'high', 100);
    expect(document.body.textContent).toContain('high');
  });

  it('allows clearing model and fallback selections back to empty values', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            fallback_model: 'anthropic/claude-sonnet-4-20250219',
            fallback_connection: 'anthropic:api-key',
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
        fallbackTriggerLabel() === 'anthropic/claude-sonnet-4-20250219',
      100,
    );

    await openSearchableDropdown('agent-model');
    selectSearchableOption('agent-model', 'Default (no model selected)');
    await waitForCondition(
      () => modelTriggerLabel() === 'Default (no model selected)',
      100,
    );

    await openSearchableDropdown('agent-fallback-model');
    selectSearchableOption('agent-fallback-model', 'None');
    await waitForCondition(() => fallbackTriggerLabel() === 'None', 100);

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
    expect(updateCall[1]).toMatchObject({
      id: 'alpha',
      model: '',
      connection: '',
      fallback_model: '',
      fallback_connection: '',
    });
  });

  it('sends selected create payload with connection and fallback_connection', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [],
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => searchableOptionCountReady(), 100);

    setTextInputValue(0, 'bravo');
    setTextInputValue(1, 'Bravo');

    await openSearchableDropdown('agent-model');
    selectSearchableOption('agent-model', 'openai/gpt-5.2 (API Key)');

    await openSearchableDropdown('agent-fallback-model');
    selectSearchableOption(
      'agent-fallback-model',
      'anthropic/claude-sonnet-4-20250219',
    );

    document.body
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true }));
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'agent.create'),
      100,
    );

    const createCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'agent.create',
    );
    expect(createCall[1]).toMatchObject({
      id: 'bravo',
      name: 'Bravo',
      model: 'openai/gpt-5.2',
      connection: 'openai:api-key',
      fallback_model: 'anthropic/claude-sonnet-4-20250219',
      fallback_connection: 'anthropic:api-key',
    });
  });

  it('sends selected update payload with connection and fallback_connection', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent()],
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => searchableOptionCountReady(), 100);

    await openSearchableDropdown('agent-model');
    selectSearchableOption('agent-model', 'openai/gpt-5.2 (OAuth)');

    await openSearchableDropdown('agent-fallback-model');
    selectSearchableOption(
      'agent-fallback-model',
      'anthropic/claude-sonnet-4-20250219',
    );

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
    expect(updateCall[1]).toMatchObject({
      id: 'alpha',
      model: 'openai/gpt-5.2',
      connection: 'openai:oauth',
      fallback_model: 'anthropic/claude-sonnet-4-20250219',
      fallback_connection: 'anthropic:api-key',
    });
  });

  it('matches dropdown open and close interaction expected by the design artifact', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel(), 100);

    const searchableRoot = await openSearchableDropdown('agent-model', {
      left: 120,
      top: 180,
      bottom: 212,
      width: 344,
      height: 32,
      right: 464,
    });
    const searchablePanel = getSearchablePanel('agent-model');

    expect(searchableRoot.classList.contains('open')).toBe(true);
    expect(searchableRoot.dataset.state).toBe('open');
    expect(
      getSearchableTrigger('agent-model').getAttribute('aria-expanded'),
    ).toBe('true');
    expect(searchablePanel.getAttribute('aria-hidden')).toBe('false');
    expect(searchablePanel.dataset.positioning).toBe('fixed');
    expect(searchablePanel.dataset.placement).toBe('bottom');
    expect(searchablePanel.getAttribute('style')).toContain('width: 344px');
    expect(searchableRoot.querySelector('.dropdown-chevron')).toBeTruthy();
    expect(
      searchableRoot.querySelector('.dropdown-chevron')?.getAttribute('width'),
    ).toBe('10');
    expect(
      searchableRoot.querySelector('.dropdown-chevron')?.getAttribute('height'),
    ).toBe('10');

    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    await waitForCondition(
      () => getSearchableRoot('agent-model').dataset.state === 'closed',
      100,
    );

    expect(getSearchablePanel('agent-model').getAttribute('aria-hidden')).toBe(
      'true',
    );

    await openSearchableDropdown('agent-model');
    getSearchableOptionsContainer('agent-model').dispatchEvent(
      new Event('scroll'),
    );
    flushSync();
    expect(getSearchableRoot('agent-model').dataset.state).toBe('open');

    window.dispatchEvent(new Event('scroll'));
    flushSync();
    await waitForCondition(
      () => getSearchableRoot('agent-model').dataset.state === 'closed',
      100,
    );

    const simpleRoot = openSimpleDropdown('agent-thinking-effort');
    const simpleList = getSimpleList('agent-thinking-effort');

    expect(simpleRoot.classList.contains('open')).toBe(true);
    expect(simpleRoot.dataset.state).toBe('open');
    expect(
      getSimpleTrigger('agent-thinking-effort').getAttribute('aria-expanded'),
    ).toBe('true');
    expect(simpleList.getAttribute('aria-hidden')).toBe('false');
    expect(simpleRoot.querySelector('.dropdown-chevron')).toBeTruthy();
    expect(
      simpleRoot.querySelector('.dropdown-chevron')?.getAttribute('width'),
    ).toBe('10');
    expect(
      simpleRoot.querySelector('.dropdown-chevron')?.getAttribute('height'),
    ).toBe('10');

    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    flushSync();
    await waitForCondition(
      () => getSimpleRoot('agent-thinking-effort').dataset.state === 'closed',
      100,
    );
    expect(
      getSimpleList('agent-thinking-effort').getAttribute('aria-hidden'),
    ).toBe('true');
  });

  it('lets the simple thinking-effort dropdown escape the model card clipping', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => thinkingTriggerLabel() === 'Default', 100);

    const modelCard = Array.from(
      document.body.querySelectorAll('.detail-group.agents-view__model-group'),
    ).find((group) => group.textContent.includes('Model'));
    const identityCard = Array.from(
      document.body.querySelectorAll('.detail-group'),
    ).find((group) => group.textContent.includes('Identity'));
    const simpleRoot = getSimpleRoot('agent-thinking-effort');

    expect(modelCard).toBeTruthy();
    expect(identityCard).toBeTruthy();
    expect(identityCard.classList.contains('agents-view__model-group')).toBe(
      false,
    );
    expect(simpleRoot.closest('.detail-group')).toBe(modelCard);

    openSimpleDropdown('agent-thinking-effort');

    const simpleList = getSimpleList('agent-thinking-effort');
    expect(simpleList).toBeTruthy();
    expect(simpleList.classList.contains('agents-view__thinking-list')).toBe(
      true,
    );
    expect(simpleList.getAttribute('aria-hidden')).toBe('false');
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

  it('renders skill catalog warnings and unavailable diagnostics', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('poem-writer');

    expect(document.body.textContent).toContain('Writes tiny poems.');
    expect(document.body.textContent).toContain('name differs from folder');
    expect(document.body.textContent).toContain('Unavailable skills');
    expect(document.body.textContent).toContain('broken-skill');
    expect(document.body.textContent).toContain('missing description');

    const skillToggle = Array.from(
      document.body.querySelectorAll('button.tl-toggle'),
    ).find((button) =>
      button.getAttribute('aria-label')?.includes('Toggle skill warning-skill'),
    );
    expect(skillToggle).toBeTruthy();
    expect(skillToggle.getAttribute('aria-checked')).toBe('true');

    skillToggle.click();
    flushSync();

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
    expect(updateCall[1].allowed_skills).toEqual(['poem-writer']);
  });
});

function modelTriggerLabel() {
  return triggerTextContent(getSearchableTrigger('agent-model'));
}

function fallbackTriggerLabel() {
  return triggerTextContent(getSearchableTrigger('agent-fallback-model'));
}

function thinkingTriggerLabel() {
  return triggerTextContent(getSimpleTrigger('agent-thinking-effort'));
}

function triggerTextContent(trigger) {
  return (
    trigger
      ?.querySelector(
        '.searchable-dropdown__trigger-label, .dropdown-primitive__trigger-label',
      )
      ?.textContent?.trim() ?? ''
  );
}

function searchableOptionCountReady() {
  return (
    getSearchablePanel('agent-model')?.querySelectorAll(
      '.searchable-dropdown__option',
    ).length > 0
  );
}

async function openSearchableDropdown(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  await waitForCondition(
    () => getSearchableRoot(id).dataset.state === 'open',
    100,
  );
  return getSearchableRoot(id);
}

function setSearchableFilter(id, value) {
  const input = getSearchablePanel(id).querySelector('input');
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function selectSearchableOption(id, label) {
  const option = Array.from(
    getSearchablePanel(id).querySelectorAll('.searchable-dropdown__option'),
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function searchableOptionLabels(id) {
  return Array.from(
    getSearchablePanel(id)?.querySelectorAll('.searchable-dropdown__option') ??
      [],
  ).map((option) => option.textContent.trim());
}

function getSearchableRoot(id) {
  return getSearchableTrigger(id)?.closest('.searchable-dropdown');
}

function getSearchableTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSearchablePanel(id) {
  return getSearchableRoot(id)?.querySelector('.searchable-dropdown__panel');
}

function getSearchableOptionsContainer(id) {
  return getSearchablePanel(id)?.querySelector('.searchable-dropdown__options');
}

function openSimpleDropdown(id) {
  const trigger = getSimpleTrigger(id);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
  return getSimpleRoot(id);
}

function selectSimpleOption(id, label) {
  const option = Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function simpleOptionLabels(id) {
  return Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).map((option) => option.textContent.trim());
}

function getSimpleRoot(id) {
  return getSimpleTrigger(id)?.closest('.dropdown-primitive');
}

function getSimpleTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSimpleList(id) {
  return getSimpleRoot(id)?.querySelector('.dropdown-primitive__list');
}

function stubTriggerRect(trigger, rect) {
  trigger.getBoundingClientRect = () => ({
    x: rect.left,
    y: rect.top,
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
    toJSON: () => rect,
  });
}

function defaultTriggerRect() {
  return {
    left: 96,
    top: 144,
    right: 416,
    bottom: 176,
    width: 320,
    height: 32,
  };
}

function setTextInputValue(index, value) {
  const input = Array.from(
    document.body.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function createAgentsRpcMock(options = {}) {
  const models = options.models ?? [openaiModel(), anthropicModel()];
  const agents = options.agents ?? [baseAgent()];
  const connections = options.connections ?? [
    usableConnection('openai:api-key', 'openai', 'API Key'),
  ];

  return async (method, params) => {
    if (method === 'model.list') {
      return { models };
    }

    if (method === 'connection.list') {
      return { connections };
    }

    if (method === 'tool.list') {
      return { tools: [] };
    }

    if (method === 'skill.list') {
      return options.skills ?? skillCatalog();
    }

    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'agent.create' || method === 'agent.update') {
      return { ...params, current_session_id: 'session-saved' };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function skillCatalog() {
  return {
    skills: [
      {
        name: 'poem-writer',
        description: 'Writes tiny poems.',
        valid: true,
        warnings: [],
      },
      {
        name: 'warning-skill',
        description: 'Loads with a warning.',
        valid: false,
        warnings: ['name differs from folder'],
      },
    ],
    invalid_skills: [
      {
        name: 'broken-skill',
        path: 'C:/skills/broken-skill/SKILL.md',
        valid: false,
        warnings: ['missing description'],
      },
    ],
  };
}

function usableConnection(id, providerId, label) {
  return {
    id,
    provider_id: providerId,
    type: id.endsWith(':oauth') ? 'oauth' : 'api_key',
    label,
    usable: true,
  };
}

function openaiModel() {
  return {
    id: 'openai/gpt-5.2',
    provider_id: 'openai',
    model_id: 'gpt-5.2',
    name: 'GPT-5.2',
  };
}

function anthropicModel() {
  return {
    id: 'anthropic/claude-sonnet-4-20250219',
    provider_id: 'anthropic',
    model_id: 'claude-sonnet-4-20250219',
    name: 'Claude Sonnet 4',
  };
}

function baseAgent() {
  return {
    id: 'alpha',
    name: 'Alpha',
    model: 'openai/gpt-5.2',
    connection: 'openai:api-key',
    fallback_model: '',
    fallback_connection: '',
    workspace: 'C:/agents/alpha',
    current_session_id: 'session-1',
    temperature: '0.1',
    thinking_effort: '',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    created_at: '2026-05-08T00:00:00+00:00',
    updated_at: '2026-05-08T00:00:00+00:00',
  };
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}

async function waitForText(text, attempts = 20) {
  await waitForCondition(
    () => document.body.textContent?.includes(text),
    attempts,
  );
}
