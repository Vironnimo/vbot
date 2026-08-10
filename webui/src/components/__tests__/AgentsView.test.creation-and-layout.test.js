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

function listedAgentIds() {
  return Array.from(document.body.querySelectorAll('button.agent-item')).map(
    (button) =>
      button.closest('.agent-list-row').querySelector('.agent-order-handle')
        .dataset.agentOrderHandle,
  );
}

function agentOrderHandle(agentId) {
  return document.body.querySelector(`[data-agent-order-handle="${agentId}"]`);
}

function createDataTransfer() {
  const values = new Map();
  return {
    effectAllowed: 'none',
    dropEffect: 'none',
    setData(type, value) {
      values.set(type, String(value));
    },
    getData(type) {
      return values.get(type) ?? '';
    },
  };
}

function dragEvent(type, dataTransfer) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', {
    configurable: true,
    value: dataTransfer,
  });
  return event;
}

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

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

    getButton('Add').click();
    flushSync();

    const modal = getDialog('Create agent');
    expect(modal.querySelector('#agent-create-id')).toBeTruthy();
    expect(modal.querySelector('#agent-create-name')).toBeTruthy();
    expect(modal.querySelector('#agent-create-model')).toBeTruthy();
    expect(modal.querySelector('#agent-create-thinking-effort')).toBeTruthy();
    expect(modal.querySelector('#agent-create-temperature')).toBeTruthy();
    expect(modal.querySelector('#agent-fallback-model')).toBeNull();
    expect(modal.querySelector('[aria-label^="Toggle tool "]')).toBeNull();
    expect(
      modal.querySelector('label[for="agent-create-id"] .form-field__required'),
    ).toBeTruthy();
    expect(
      modal.querySelector(
        'label[for="agent-create-name"] .form-field__required',
      ),
    ).toBeNull();

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

    await waitForCondition(() => listedAgentIds().includes('bravo'), 100);
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
  });

  it('opens Add in a modal while keeping the current agent selected', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { sharedSelectedAgentId: 'alpha' },
    });
    flushSync();

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

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
    expect(
      document.body.querySelector('button.agent-item.active'),
    ).toBeTruthy();
    expect(textInputValue(0)).toBe('alpha');
    expect(textInputValue(1)).toBe('Alpha');
  });

  it('shows only the model name in the inset agent list row', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

    const agentItem = document.body.querySelector('button.agent-item');
    expect(agentItem.classList.contains('secondary-list__item')).toBe(true);
    expect(agentItem.querySelector('.agent-item-sub').textContent.trim()).toBe(
      'gpt-5.2',
    );
    const row = agentItem.closest('.agent-list-row');
    expect(row.firstElementChild).toBe(agentItem);
    expect(row.lastElementChild).toBe(agentOrderHandle('alpha'));
  });

  it('reorders agents by drag-and-drop and persists the roster revision', async () => {
    const agents = [
      baseAgent(),
      { ...baseAgent(), id: 'bravo', name: 'Bravo' },
      { ...baseAgent(), id: 'charlie', name: 'Charlie' },
    ];
    rpcMock.mockImplementation(
      createAgentsRpcMock({ agents, orderRevision: 7 }),
    );

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { sharedSelectedAgentId: 'alpha' },
    });
    flushSync();
    await waitForCondition(() => listedAgentIds().length === 3, 100);

    const dataTransfer = createDataTransfer();
    agentOrderHandle('alpha').dispatchEvent(
      dragEvent('dragstart', dataTransfer),
    );
    document
      .querySelector('[data-agent-order-handle="charlie"]')
      .closest('.agent-list-row')
      .dispatchEvent(dragEvent('drop', dataTransfer));
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some(([method]) => method === 'agent.reorder'),
      100,
    );
    expect(listedAgentIds()).toEqual(['bravo', 'charlie', 'alpha']);
    expect(
      rpcMock.mock.calls.find(([method]) => method === 'agent.reorder')[1],
    ).toEqual({
      agent_ids: ['bravo', 'charlie', 'alpha'],
      expected_revision: 7,
    });
  });

  it('reorders agents with arrow keys and announces the new position', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => listedAgentIds().length === 2, 100);

    const handle = agentOrderHandle('bravo');
    handle.focus();
    handle.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
        cancelable: true,
      }),
    );
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some(([method]) => method === 'agent.reorder'),
      100,
    );
    expect(listedAgentIds()).toEqual(['bravo', 'alpha']);
    expect(
      document.body.querySelector('.agent-list-pane__sr-only').textContent,
    ).toContain('position 1 of 2');
    expect(document.activeElement.dataset.agentOrderHandle).toBe('bravo');
  });

  it('reloads authoritative order and reports a failed reorder', async () => {
    const onToast = vi.fn();
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
        agentReorder: () => {
          throw new Error('Order changed in another window');
        },
      }),
    );

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { onToast },
    });
    flushSync();
    await waitForCondition(() => listedAgentIds().length === 2, 100);

    agentOrderHandle('bravo').dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
        cancelable: true,
      }),
    );
    flushSync();

    await waitForCondition(
      () =>
        onToast.mock.calls.length === 1 &&
        listedAgentIds().join(',') === 'alpha,bravo',
      100,
    );
    expect(onToast).toHaveBeenCalledWith({
      title: 'Order changed in another window',
      variant: 'error',
    });
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

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

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

    await waitForCondition(() => textInputValue(0) === 'bravo', 100);

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

    const modelCard = document.body.querySelector(
      '.detail-group.agents-view__model-group',
    );
    const simpleRoot = getSimpleRoot('agent-thinking-effort');

    expect(modelCard).toBeTruthy();
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
    const memoryCard = document.body.querySelector(
      '.detail-group.agents-view__memory-group',
    );
    const simpleRoot = getSimpleRoot('agent-memory-prompt-mode');

    expect(memoryCard).toBeTruthy();
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

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

    getButton('Add').click();
    flushSync();
    await flushAsyncUpdates();

    // The three run fields default to the inherit state. The model inherit
    // option shows the global default fetched on open.
    await waitForCondition(
      () =>
        triggerTextContent(getSearchableTrigger('agent-create-model')).includes(
          'openai/gpt-5.2',
        ),
      100,
    );

    // Selecting the reasoning model narrows the thinking-effort options to its
    // ladder (default inherit + none + the levels).
    await openSearchableDropdown('agent-create-model');
    selectSearchableOption('agent-create-model', 'openai/gpt-5.2');
    await flushAsyncUpdates();

    openSimpleDropdown('agent-create-thinking-effort');
    const labels = simpleOptionLabels('agent-create-thinking-effort');
    expect(labels).toHaveLength(4);
    expect(labels.slice(1)).toEqual(['none', 'high', 'xhigh']);
  });
});
