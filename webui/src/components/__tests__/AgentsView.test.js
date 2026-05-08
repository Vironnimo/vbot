// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const MODEL_CONNECTION_VALUE_SEPARATOR = '\u001f';

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

    await waitForCondition(() => {
      const [modelSelect, fallbackModelSelect] = getSelects();

      return (
        Array.from(modelSelect?.options ?? []).some(
          (option) => option.textContent === 'openai/gpt-5.2',
        ) &&
        Array.from(fallbackModelSelect?.options ?? []).some(
          (option) =>
            option.textContent === 'anthropic/claude-sonnet-4-20250219',
        )
      );
    }, 100);

    const [modelSelect, fallbackModelSelect] = getSelects();
    const modelOptionLabels = Array.from(modelSelect.options).map(
      (option) => option.textContent,
    );
    const fallbackOptionLabels = Array.from(fallbackModelSelect.options).map(
      (option) => option.textContent,
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

  it('preserves a saved unavailable model value in the dropdown', async () => {
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
      () =>
        getSelects()[0]?.value ===
          modelConnectionValue('legacy/custom-model', '') &&
        Array.from(getSelects()[0]?.options ?? []).some(
          (option) => option.textContent === 'openai/gpt-5.2',
        ),
      100,
    );

    const [modelSelect] = getSelects();
    const modelOptionLabels = Array.from(modelSelect.options).map(
      (option) => option.textContent,
    );

    expect(modelSelect.value).toBe(
      modelConnectionValue('legacy/custom-model', ''),
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

    await waitForCondition(
      () => optionLabels(getSelects()[0]).includes('openai/gpt-5.2'),
      100,
    );

    const labels = optionLabels(getSelects()[0]);
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
      () => optionLabels(getSelects()[0]).includes('openai/gpt-5.2 (OAuth)'),
      100,
    );

    const labels = optionLabels(getSelects()[0]);
    expect(labels).toContain('openai/gpt-5.2 (OAuth)');
    expect(labels).toContain('openai/gpt-5.2 (API Key)');
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

    await waitForCondition(() => getSelects()[0]?.options.length > 1, 100);

    setInputValue('input:not([disabled])', 'bravo');
    setInputValue('label:nth-of-type(2) input', 'Bravo');
    selectOptionByLabel(getSelects()[0], 'openai/gpt-5.2 (API Key)');
    selectOptionByLabel(getSelects()[1], 'anthropic/claude-sonnet-4-20250219');

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

    await waitForCondition(() => getSelects()[0]?.options.length > 1, 100);

    selectOptionByLabel(getSelects()[0], 'openai/gpt-5.2 (OAuth)');
    selectOptionByLabel(getSelects()[1], 'anthropic/claude-sonnet-4-20250219');

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
    expect(optionLabels(getSelects()[0])).not.toContain('openai/gpt-5.2');
  });
});

function getSelects() {
  return Array.from(document.body.querySelectorAll('select'));
}

function optionLabels(select) {
  return Array.from(select?.options ?? []).map((option) => option.textContent);
}

function modelConnectionValue(model, connection) {
  return `${model}${MODEL_CONNECTION_VALUE_SEPARATOR}${connection || ''}`;
}

function selectOptionByLabel(select, label) {
  const option = Array.from(select.options).find(
    (item) => item.textContent === label,
  );
  expect(option).toBeTruthy();
  select.value = option.value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

function setInputValue(selector, value) {
  const input = document.body.querySelector(selector);
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

    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'agent.create' || method === 'agent.update') {
      return { ...params, current_session_id: 'session-saved' };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
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
