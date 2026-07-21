// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';
import {
  rpcMock,
  modelTriggerLabel,
  fallbackTriggerLabel,
  thinkingTriggerLabel,
  openSearchableDropdown,
  openSearchableDropdownSync,
  selectSearchableOption,
  openSimpleDropdown,
  selectSimpleOption,
  setTextInputValue,
  getButton,
  getDialog,
  getAgentButton,
  submitAgentForm,
  getAgentUpdateCalls,
  flushAsyncUpdates,
  textInputValue,
  setTextInputValueWithin,
  createAgentsRpcMock,
  usableConnection,
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

  it('renames through an explicit confirmation flow and keeps the renamed Agent selected', async () => {
    const onAgentSelected = vi.fn();
    const onAgentsChanged = vi.fn();
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { onAgentSelected, onAgentsChanged },
    });
    flushSync();
    await waitForCondition(() => textInputValue(1) === 'Alpha', 100);
    onAgentSelected.mockClear();
    onAgentsChanged.mockClear();

    getButton('Change ID').click();
    flushSync();
    const dialog = getDialog('Change Agent ID?');
    expect(dialog.textContent).toContain('Historical records keep the ID');
    setTextInputValueWithin(dialog, 0, 'researcher');
    dialog
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flushAsyncUpdates();

    expect(rpcMock).toHaveBeenCalledWith('agent.rename', {
      id: 'alpha',
      new_id: 'researcher',
    });
    expect(textInputValue(0)).toBe('researcher');
    expect(onAgentSelected).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'researcher' }),
    );
    expect(onAgentsChanged).toHaveBeenCalledWith([
      expect.objectContaining({ id: 'researcher' }),
    ]);
  });

  it('auto-saves model changes 800 ms after the last edit', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
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
    selectSearchableOption('agent-model', 'openai/gpt-5.2 (ChatGPT Plus/Pro)');

    expect(getAgentUpdateCalls()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(799);
    await flushAsyncUpdates();
    expect(getAgentUpdateCalls()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(1);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      model: 'openai/gpt-5.2::subscription',
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
      'input.s-input[inputmode="decimal"]',
    );
    expect(temperatureInput).toBeTruthy();
    temperatureInput.value = '';
    temperatureInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('agent-thinking-effort');
    selectSimpleOption('agent-thinking-effort', 'Inherit (provider default)');

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
    selectSearchableOption('agent-model', 'Inherit (not configured)');
    await waitForCondition(
      () => modelTriggerLabel() === 'Inherit (not configured)',
      100,
    );

    await openSearchableDropdown('agent-fallback-model');
    selectSearchableOption('agent-fallback-model', 'Inherit (not configured)');
    await waitForCondition(
      () => fallbackTriggerLabel() === 'Inherit (not configured)',
      100,
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
      model: '',
      fallback_model: '',
    });
  });

  it('sends selected update payload with model connection suffixes', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent()],
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
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
    await waitForCondition(
      () => modelTriggerLabel().includes('(API Key)'),
      100,
    );

    await openSearchableDropdown('agent-model');
    selectSearchableOption('agent-model', 'openai/gpt-5.2 (ChatGPT Plus/Pro)');

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
      model: 'openai/gpt-5.2::subscription',
      fallback_model: 'anthropic/claude-sonnet-4-20250219::api-key',
    });
  });
});
