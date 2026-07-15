// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';
import {
  rpcMock,
  modelTriggerLabel,
  thinkingTriggerLabel,
  triggerTextContent,
  openSearchableDropdown,
  selectSearchableOption,
  getSearchableRoot,
  getSearchableTrigger,
  getSearchablePanel,
  getSearchableOptionsContainer,
  openSimpleDropdown,
  selectSimpleOption,
  simpleOptionLabels,
  getSimpleRoot,
  getSimpleTrigger,
  getSimpleList,
  getButton,
  getDialog,
  setTextInputValueWithin,
  setNumberInputValueWithin,
  flushAsyncUpdates,
  textInputValue,
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

  it('opens Add as a compact modal and sends selected create payload', async () => {
    const agents = [baseAgent()];

    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents,
        connections: [
          usableConnection('openai:subscription', 'openai', 'ChatGPT Plus/Pro'),
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

    getButton('Add').click();
    flushSync();

    const modal = getDialog('Create agent');
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

  it('opens Add in a modal while keeping the current agent selected', async () => {
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

    const addButton = Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Add',
    );
    expect(addButton).toBeTruthy();

    addButton.click();
    flushSync();
    await Promise.resolve();
    flushSync();

    const modal = getDialog('Create agent');
    expect(modal.textContent).toContain('Create agent');
    expect(document.body.textContent).toContain('id: alpha');
    expect(document.body.textContent).toContain('Delete agent');
    expect(
      document.body.querySelector('button.agent-item.active'),
    ).toBeTruthy();
    expect(textInputValue(0)).toBe('alpha');
    expect(textInputValue(1)).toBe('Alpha');
  });

  it('keeps existing agent selection after cancelling Add modal', async () => {
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

    const addButton = Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Add',
    );
    expect(addButton).toBeTruthy();

    addButton.click();
    flushSync();
    await Promise.resolve();
    flushSync();

    const modal = getDialog('Create agent');
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
    expect(document.body.textContent).toContain('Delete agent');
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

    await waitForCondition(
      () => thinkingTriggerLabel() === 'Inherit (provider default)',
      100,
    );

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

  it('lets the memory dropdown escape the memory card clipping', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => getSimpleTrigger('agent-memory-prompt-mode') !== null,
      100,
    );

    // The memory dropdown now lives in its own Memory card (split out of the
    // System Prompt card), which must still let the portaled list escape.
    const memoryCard = Array.from(
      document.body.querySelectorAll('.detail-group.agents-view__memory-group'),
    ).find((group) => group.textContent.includes('Memory'));
    const identityCard = Array.from(
      document.body.querySelectorAll('.detail-group'),
    ).find((group) => group.textContent.includes('Identity'));
    const simpleRoot = getSimpleRoot('agent-memory-prompt-mode');

    expect(memoryCard).toBeTruthy();
    expect(identityCard).toBeTruthy();
    expect(identityCard.classList.contains('agents-view__memory-group')).toBe(
      false,
    );
    expect(simpleRoot.closest('.detail-group')).toBe(memoryCard);

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

  it('gates create-modal thinking effort by the selected model and defaults to inherit', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        models: [
          {
            id: 'openai/gpt-5.2',
            provider_id: 'openai',
            model_id: 'gpt-5.2',
            name: 'GPT-5.2',
            capabilities: {
              tools: true,
              reasoning: { supported: true, levels: ['high', 'xhigh'] },
            },
            context_window: 256000,
            effective_context_window: 256000,
          },
        ],
        settingsDefaults: { model: 'openai/gpt-5.2' },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('id: alpha'),
      100,
    );

    getButton('Add').click();
    flushSync();
    await flushAsyncUpdates();

    const modal = getDialog('Create agent');
    // The three run fields default to the inherit state. The model inherit
    // option shows the global default fetched on open.
    await waitForCondition(
      () =>
        triggerTextContent(getSearchableTrigger('agent-create-model')) ===
        'Inherited: openai/gpt-5.2 (global default)',
      100,
    );

    // Selecting the reasoning model narrows the thinking-effort options to its
    // ladder (default inherit + none + the levels).
    await openSearchableDropdown('agent-create-model');
    selectSearchableOption('agent-create-model', 'openai/gpt-5.2');
    await flushAsyncUpdates();

    openSimpleDropdown('agent-create-thinking-effort');
    const labels = simpleOptionLabels('agent-create-thinking-effort');
    expect(labels).toEqual([
      'Inherit (provider default)',
      'none',
      'high',
      'xhigh',
    ]);
    expect(modal.textContent).not.toContain('low');
  });
});
