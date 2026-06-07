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
    vi.useRealTimers();
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
              model: 'openai/gpt-5.2::api-key',
              fallback_model: 'anthropic/claude-sonnet-4-20250219::api-key',
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

    expect(rpcMock).toHaveBeenCalledWith('model.list');

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
              fallback_model: '',
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

  it('keeps a saved unsuffixed model available while omitting unchanged fields on save', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), model: 'openai/gpt-5.2' }],
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
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
    expect(modelOptionLabels).toContain('openai/gpt-5.2 (OAuth)');
    expect(modelOptionLabels).toContain('openai/gpt-5.2 (API Key)');
    expect(modelOptionLabels).not.toContain(
      'Unavailable / custom: openai/gpt-5.2',
    );

    setTextInputValue(1, 'Alpha Prime');

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
            fallback_model: 'openai/gpt-5.2-mini',
            temperature: '0.6',
            thinking_effort: 'high',
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    setTextInputValue(1, 'Alpha Renamed');

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

  it('edits workspace from the identity section without duplicate workspace displays', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        document.body.querySelector('#agent-workspace')?.value ===
        'C:/agents/alpha',
      100,
    );

    const workspaceLabels = Array.from(
      document.body.querySelectorAll('.agent-detail-pane .f-label'),
    ).filter((label) => label.textContent.trim() === 'Workspace');
    expect(workspaceLabels).toHaveLength(1);

    const workspaceInput = document.body.querySelector('#agent-workspace');
    workspaceInput.value = 'D:/agents/alpha';
    workspaceInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      workspace: 'D:/agents/alpha',
    });
  });

  it('sends custom system prompt toggle changes from the agent detail pane', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('Custom system prompt');

    const toggle = getButtonByAriaLabel('Custom system prompt');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    toggle.click();
    flushSync();

    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      custom_system_prompt_enabled: true,
    });
  });

  it('sends memory prompt mode changes from the agent detail pane', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('Memory');

    openSimpleDropdown('agent-memory-prompt-mode');
    expect(simpleOptionLabels('agent-memory-prompt-mode')).toEqual([
      'Off',
      'MEMORY.md',
      'MEMORY.md + USER.md',
    ]);

    selectSimpleOption('agent-memory-prompt-mode', 'MEMORY.md');
    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      memory_prompt_mode: 'agent',
    });
  });

  it('does not render a duplicate fallback status below the fallback picker', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => modelTriggerLabel() === 'openai/gpt-5.2', 100);

    const modelLabels = Array.from(
      document.body.querySelectorAll('.agents-view__model-fields .f-label'),
    ).map((label) => label.textContent.trim());

    expect(modelLabels).toEqual([
      'Model',
      'Fallback model',
      'Thinking effort',
      'Temperature',
    ]);
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

    await waitForCondition(() => thinkingTriggerLabel() === '—', 100);

    openSimpleDropdown('agent-thinking-effort');
    expect(simpleOptionLabels('agent-thinking-effort')).toContain('high');

    selectSimpleOption('agent-thinking-effort', 'high');
    await waitForCondition(() => thinkingTriggerLabel() === 'high', 100);
    expect(document.body.textContent).toContain('high');
  });

  it('auto-saves model changes 800 ms after the last edit', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
        ],
      }),
    );

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForCondition(
      () => modelTriggerLabel() === 'openai/gpt-5.2 (API Key)',
      100,
    );

    vi.useFakeTimers();

    openSearchableDropdownSync('agent-model');
    selectSearchableOption('agent-model', 'openai/gpt-5.2 (OAuth)');

    expect(getAgentUpdateCalls()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(799);
    await flushAsyncUpdates();
    expect(getAgentUpdateCalls()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(1);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      model: 'openai/gpt-5.2::oauth',
    });
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Agent updated.',
        variant: 'success',
      }),
    );

    const saveButton = getButton('Save changes');
    expect(saveButton.disabled).toBe(false);
    toastMock.mockClear();

    saveButton.click();
    flushSync();
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Already saved',
        variant: 'success',
      }),
    );
    expect(document.body.textContent).not.toContain('Already saved');
  });

  it('auto-saves tool access changes', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          { name: 'write', description: 'Write files.' },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('write');

    vi.useFakeTimers();

    getButtonByAriaLabel('Toggle tool write').click();
    flushSync();

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      allowed_tools: ['bash'],
      id: 'alpha',
    });
  });

  it('hides memory from configurable tool access', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          { name: 'memory', description: 'Manage pinned memory.' },
          { name: 'write', description: 'Write files.' },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('write');

    expect(document.body.textContent).toContain('Run shell commands.');
    expect(document.body.textContent).toContain('Write files.');
    expect(document.body.textContent).not.toContain('Manage pinned memory.');
    expect(
      document.body.querySelector('button[aria-label="Toggle tool memory"]'),
    ).toBeNull();
  });

  it('manual save cancels a pending agent autosave', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => textInputValue(1) === 'Alpha', 100);

    vi.useFakeTimers();

    setTextInputValue(1, 'Alpha Manual');
    submitAgentForm();
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      name: 'Alpha Manual',
    });

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
  });

  it('does not apply an in-flight autosave to a newly selected agent', async () => {
    let resolveAgentUpdate;
    const agentUpdateReleased = new Promise((resolve) => {
      resolveAgentUpdate = resolve;
    });

    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          baseAgent(),
          {
            ...baseAgent(),
            id: 'bravo',
            name: 'Bravo',
            model: 'anthropic/claude-sonnet-4-20250219::api-key',
          },
        ],
        agentUpdate: async (params) => {
          await agentUpdateReleased;
          return { ...baseAgent(), ...params };
        },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => textInputValue(1) === 'Alpha', 100);

    vi.useFakeTimers();

    setTextInputValue(1, 'Alpha Autosaved');

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);

    getAgentButton('Bravo').click();
    flushSync();

    expect(document.body.textContent).toContain('id: bravo');
    expect(textInputValue(1)).toBe('Bravo');

    resolveAgentUpdate();
    await flushAsyncUpdates();

    expect(document.body.textContent).toContain('id: bravo');
    expect(textInputValue(1)).toBe('Bravo');
    expect(document.body.textContent).not.toContain('Agent updated.');
  });

  it('sends null for cleared temperature and thinking effort', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), thinking_effort: 'high' }],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => thinkingTriggerLabel() === 'high', 100);

    const temperatureInput = document.body.querySelector(
      'input.s-input[type="number"]',
    );
    expect(temperatureInput).toBeTruthy();
    temperatureInput.value = '';
    temperatureInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('agent-thinking-effort');
    selectSimpleOption('agent-thinking-effort', '—');

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
      temperature: null,
      thinking_effort: null,
    });
  });

  it('allows clearing model and fallback selections back to empty values', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            fallback_model: 'anthropic/claude-sonnet-4-20250219::api-key',
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
      fallback_model: '',
    });
  });

  it('opens New as a compact modal and sends selected create payload', async () => {
    const agents = [baseAgent()];

    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents,
        connections: [
          usableConnection('openai:oauth', 'openai', 'OAuth'),
          usableConnection('openai:api-key', 'openai', 'API Key'),
          usableConnection('anthropic:api-key', 'anthropic', 'API Key'),
        ],
        agentUpdate: (params, method) => {
          if (method === 'agent.create') {
            const createdAgent = {
              ...baseAgent(),
              ...params,
              current_session_id: 'session-saved',
            };
            agents.push(createdAgent);
            return createdAgent;
          }

          return { ...baseAgent(), ...params };
        },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('id: alpha'),
      100,
    );

    getButton('New').click();
    flushSync();

    const modal = getDialog('Create Agent');
    expect(modal.textContent).toContain('Agent ID');
    expect(modal.textContent).toContain('Name');
    expect(modal.textContent).toContain('Model');
    expect(modal.textContent).toContain('Thinking effort');
    expect(modal.textContent).toContain('Temperature');
    expect(modal.textContent).not.toContain('Fallback model');
    expect(modal.textContent).not.toContain('Allowed tools');

    setTextInputValueWithin(modal, 0, 'bravo');
    setTextInputValueWithin(modal, 1, 'Bravo');
    setNumberInputValueWithin(modal, 0, '0.4');

    await openSearchableDropdown('agent-create-model');
    selectSearchableOption('agent-create-model', 'openai/gpt-5.2 (API Key)');

    openSimpleDropdown('agent-create-thinking-effort');
    selectSimpleOption('agent-create-thinking-effort', 'high');

    modal
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
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
      model: 'openai/gpt-5.2::api-key',
      thinking_effort: 'high',
      temperature: 0.4,
    });

    await waitForCondition(
      () => document.body.textContent.includes('id: bravo'),
      100,
    );
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  it('sends selected update payload with model connection suffixes', async () => {
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

    // The panel only exists while open, so gate on the trigger label instead:
    // the connection suffix appears once both the agent and the connection
    // catalog have loaded.
    await waitForCondition(() => modelTriggerLabel().includes('(API Key)'), 100);

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
      model: 'openai/gpt-5.2::oauth',
      fallback_model: 'anthropic/claude-sonnet-4-20250219::api-key',
    });
  });

  it('opens New in a modal while keeping the current agent selected', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { sharedSelectedAgentId: 'alpha' },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('id: alpha'),
      100,
    );

    const newButton = Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'New',
    );
    expect(newButton).toBeTruthy();

    newButton.click();
    flushSync();
    await Promise.resolve();
    flushSync();

    const modal = getDialog('Create Agent');
    expect(modal.textContent).toContain('Create agent');
    expect(document.body.textContent).toContain('id: alpha');
    expect(document.body.textContent).toContain('Delete Agent');
    expect(
      document.body.querySelector('button.agent-item.active'),
    ).toBeTruthy();
    expect(textInputValue(0)).toBe('alpha');
    expect(textInputValue(1)).toBe('Alpha');
  });

  it('keeps existing agent selection after cancelling New modal', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
      }),
    );

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { sharedSelectedAgentId: 'alpha' },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('id: alpha'),
      100,
    );

    const newButton = Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'New',
    );
    expect(newButton).toBeTruthy();

    newButton.click();
    flushSync();
    await Promise.resolve();
    flushSync();

    const modal = getDialog('Create Agent');
    expect(modal).toBeTruthy();
    getButton('Cancel').click();
    flushSync();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();

    const bravoButton = Array.from(
      document.body.querySelectorAll('button.agent-item'),
    ).find((button) => button.textContent.includes('Bravo'));
    expect(bravoButton).toBeTruthy();

    bravoButton.click();
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('id: bravo'),
      100,
    );

    expect(document.body.textContent).toContain('Save changes');
    expect(document.body.textContent).toContain('Delete Agent');
    expect(textInputValue(0)).toBe('bravo');
    expect(textInputValue(1)).toBe('Bravo');
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
    // Portaled out of the card to <body> so it can never be clipped or
    // covered by a sibling card/modal.
    expect(searchablePanel).toBeTruthy();
    expect(searchablePanel.parentElement).toBe(document.body);
    expect(searchablePanel.closest('.detail-group')).toBeNull();
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

    // Closing removes the portaled panel from the DOM entirely.
    expect(getSearchablePanel('agent-model')).toBeNull();

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
    expect(simpleList).toBeTruthy();
    expect(simpleList.parentElement).toBe(document.body);
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
    // Closing removes the portaled list from the DOM entirely.
    expect(getSimpleList('agent-thinking-effort')).toBeNull();
  });

  it('lets the simple thinking-effort dropdown escape the model card clipping', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => thinkingTriggerLabel() === '—', 100);

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
    // The open list is portaled to <body>, so it lives outside the model card
    // and cannot be clipped or covered by it.
    expect(simpleList.parentElement).toBe(document.body);
    expect(simpleList.closest('.detail-group')).toBeNull();
  });

  it('lets the memory dropdown escape the system prompt card clipping', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => getSimpleTrigger('agent-memory-prompt-mode') !== null,
      100,
    );

    const promptCard = Array.from(
      document.body.querySelectorAll('.detail-group.agents-view__prompt-group'),
    ).find((group) => group.textContent.includes('System Prompt'));
    const identityCard = Array.from(
      document.body.querySelectorAll('.detail-group'),
    ).find((group) => group.textContent.includes('Identity'));
    const simpleRoot = getSimpleRoot('agent-memory-prompt-mode');

    expect(promptCard).toBeTruthy();
    expect(identityCard).toBeTruthy();
    expect(identityCard.classList.contains('agents-view__prompt-group')).toBe(
      false,
    );
    expect(simpleRoot.closest('.detail-group')).toBe(promptCard);

    openSimpleDropdown('agent-memory-prompt-mode');

    const simpleList = getSimpleList('agent-memory-prompt-mode');
    expect(simpleList).toBeTruthy();
    expect(simpleList.classList.contains('agents-view__memory-list')).toBe(
      true,
    );
    // The open list is portaled to <body>, so it lives outside the prompt card
    // and cannot be clipped or covered by it.
    expect(simpleList.parentElement).toBe(document.body);
    expect(simpleList.closest('.detail-group')).toBeNull();
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

function openSearchableDropdownSync(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  expect(getSearchableRoot(id).dataset.state).toBe('open');
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

function getSearchablePanel() {
  // The panel is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.searchable-dropdown__panel');
}

function getSearchableOptionsContainer() {
  return getSearchablePanel()?.querySelector('.searchable-dropdown__options');
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

function getSimpleList() {
  // The list is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.dropdown-primitive__list');
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

function getButton(label) {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button).toBeTruthy();
  return button;
}

function getDialog(title) {
  const dialog = Array.from(
    document.body.querySelectorAll('[role="dialog"]'),
  ).find((item) => item.textContent.includes(title));
  expect(dialog).toBeTruthy();
  return dialog;
}

function setTextInputValueWithin(container, index, value) {
  const input = Array.from(
    container.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function setNumberInputValueWithin(container, index, value) {
  const input = Array.from(
    container.querySelectorAll('input.s-input[type="number"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function getButtonByAriaLabel(label) {
  const button = document.body.querySelector(`button[aria-label="${label}"]`);
  expect(button).toBeTruthy();
  return button;
}

function getAgentButton(label) {
  const button = Array.from(
    document.body.querySelectorAll('button.agent-item'),
  ).find((item) => item.textContent.includes(label));
  expect(button).toBeTruthy();
  return button;
}

function submitAgentForm() {
  document.body
    .querySelector('form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

function getAgentUpdateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'agent.update');
}

async function flushAsyncUpdates(iterations = 4) {
  for (let index = 0; index < iterations; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

function textInputValue(index) {
  const input = Array.from(
    document.body.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  return input.value;
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
      return { tools: options.tools ?? [] };
    }

    if (method === 'skill.list') {
      return options.skills ?? skillCatalog();
    }

    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'agent.create' || method === 'agent.update') {
      if (typeof options.agentUpdate === 'function') {
        return options.agentUpdate(params, method);
      }

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
    model: 'openai/gpt-5.2::api-key',
    fallback_model: '',
    workspace: 'C:/agents/alpha',
    current_session_id: 'session-1',
    temperature: '0.1',
    thinking_effort: '',
    memory_prompt_mode: 'agent_user',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    custom_system_prompt_enabled: false,
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
