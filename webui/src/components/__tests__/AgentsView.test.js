// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { createAutosaveCoordinator } from '../../lib/autosave.js';
import { init, t } from '../../lib/i18n.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import {
  rpcMock,
  modelTriggerLabel,
  triggerTextContent,
  openSearchableDropdown,
  openSearchableDropdownSync,
  selectSearchableOption,
  searchableOptionLabels,
  getSearchableTrigger,
  openSimpleDropdown,
  selectSimpleOption,
  simpleOptionLabels,
  getSimpleRoot,
  getSimpleList,
  setTextInputValue,
  getButton,
  getButtonByAriaLabel,
  getDialog,
  getAgentButton,
  setTextInputValueWithin,
  submitAgentForm,
  findSetToDefaultButton,
  getAgentUpdateCalls,
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
const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.support.svelte');

function listedAgentIds() {
  return Array.from(
    document.body.querySelectorAll('.agent-list [data-sortable-key]'),
  ).map((row) => row.dataset.sortableKey);
}

function agentButton(agentId) {
  return document.body.querySelector(
    `.agent-list [data-sortable-key="${agentId}"] button.agent-item`,
  );
}

function openRowMenu(row) {
  const event = new MouseEvent('contextmenu', {
    bubbles: true,
    cancelable: true,
    clientX: 40,
    clientY: 80,
  });
  row.dispatchEvent(event);
  flushSync();
  expect(event.defaultPrevented).toBe(true);
  return [...document.body.querySelectorAll('[role="menuitem"]')];
}

function menuLabels(items) {
  return items.map(
    (item) => item.querySelector('.context-menu__label').textContent,
  );
}

function pressMoveUp(target) {
  target.focus();
  target.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: 'ArrowUp',
      altKey: true,
      bubbles: true,
      cancelable: true,
    }),
  );
}

function isBefore(first, second) {
  return Boolean(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
}

// Mounted under an explicit coordinator, whose flush is what leaving the
// editor runs.
function mountUnderCoordinator() {
  const coordinator = createAutosaveCoordinator();
  const component = mount(AutosaveContextHost, {
    target: document.body,
    props: { component: AgentsView, coordinator },
  });
  flushSync();
  return { component, coordinator };
}

function hasButton(label) {
  return Array.from(document.body.querySelectorAll('button')).some(
    (button) => button.textContent.trim() === label,
  );
}

function setWorkspace(value) {
  const input = document.body.querySelector('#agent-workspace');
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
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

  it('places shared defaults before the roster and returns from them to the same mounted Agent editor', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.querySelector('#agent-name'));
    const model = document.querySelector('#agent-model');
    expect(isBefore(document.querySelector('#agent-name'), model)).toBe(true);

    const entry = document.querySelector('.agent-list-defaults button');
    expect(isBefore(entry, document.querySelector('.agent-list-scroll'))).toBe(
      true,
    );
    entry.click();
    flushSync();
    await waitForCondition(
      () =>
        document.querySelector('#settings-defaults-model') &&
        document.querySelector('#agent-shared-compaction'),
    );
    expect(document.querySelector('.agent-editor-host').hidden).toBe(true);
    expect(document.querySelector('.agent-shared-pane').hidden).toBe(false);
    expect(
      document
        .querySelector('.agent-shared-content')
        .contains(document.querySelector('.agent-shared-title')),
    ).toBe(true);
    // Compaction modes are rows of the open section, not behind a disclosure.
    const compaction = document.querySelector('#agent-shared-compaction');
    expect(compaction.closest('[hidden]')).toBeNull();
    const modes = compaction.querySelectorAll('input[type="radio"]');
    expect(Array.from(modes, (input) => input.value)).toEqual([
      'continuation',
      'summary_tail',
    ]);
    expect(
      isBefore(modes[0], compaction.querySelector('[role="switch"]')),
    ).toBe(true);

    document.querySelector('.agent-item').click();
    flushSync();
    expect(document.querySelector('.agent-editor-host').hidden).toBe(false);
    expect(document.querySelector('#agent-model')).toBe(model);
  });

  it('reloads authoritative inherited values after saving shared defaults without changing Agent overrides', async () => {
    const handler = createAgentsRpcMock();
    let savedTemperature = 0.4;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'settings.get')
        return { defaults: { agent: { temperature: savedTemperature } } };
      if (method === 'settings.update') {
        savedTemperature = params.defaults.agent.temperature;
        return { defaults: { agent: params.defaults.agent } };
      }
      if (method === 'agent.list')
        return {
          agents: [
            {
              ...baseAgent(),
              temperature: null,
              effective: {
                temperature: {
                  value: savedTemperature,
                  source: 'global_default',
                },
              },
            },
          ],
        };
      return handler(method, params);
    });
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.querySelector('#agent-name'));
    document.querySelector('.agent-list-defaults button').click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector('#settings-defaults-temperature'),
    );
    const temperature = document.querySelector(
      '#settings-defaults-temperature',
    );
    temperature.value = '0.73';
    temperature.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    document
      .querySelector('[data-settings-section="defaults"] .save-status button')
      .click();
    await waitForCondition(() => savedTemperature === 0.73);
    await flushAsyncUpdates();
    document.querySelector('.agent-item').click();
    flushSync();
    expect(document.querySelector('#agent-temperature').value).toBe('');
    expect(
      document.querySelector('.agent-model-settings').textContent,
    ).toContain('0.73');
    expect(getAgentUpdateCalls()).toHaveLength(0);
  });

  it('opens shared defaults at the Compaction section its place names', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    // jsdom has no layout, so the scroll target is recorded instead.
    const scrolled = [];
    Element.prototype.scrollIntoView = function () {
      scrolled.push(this);
    };
    try {
      mountedComponent = mount(AgentsView, {
        target: document.body,
        props: {
          navigation: createStandaloneNavigation(['~defaults', 'compaction']),
        },
      });
      flushSync();
      await waitForCondition(() => scrolled.length > 0);
      expect(document.querySelector('.agent-shared-pane').hidden).toBe(false);
      expect(scrolled).toEqual([
        document.querySelector('[data-settings-section="compaction"]'),
      ]);
    } finally {
      delete Element.prototype.scrollIntoView;
    }
  });

  it('shows the Agent its place names, steps on list choices and corrects an unknown place', async () => {
    const agents = [
      baseAgent(),
      { ...baseAgent(), id: 'bravo', name: 'Bravo' },
    ];
    rpcMock.mockImplementation(createAgentsRpcMock({ agents }));
    const onAgentSelected = vi.fn();
    const navigation = createStandaloneNavigation();
    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { navigation, onAgentSelected, sharedSelectedAgentId: 'bravo' },
    });
    flushSync();

    // The empty place shows the shared selected Agent and names it.
    await waitForCondition(() => navigation.place[0] === 'bravo', 100);
    expect(textInputValue('agent-id')).toBe('bravo');
    expect(onAgentSelected).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: 'bravo' }),
    );

    getAgentButton('Alpha').click();
    flushSync();
    expect(navigation.place).toEqual(['alpha']);
    expect(textInputValue('agent-id')).toBe('alpha');
    expect(onAgentSelected).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: 'alpha' }),
    );

    document.querySelector('.agent-list-defaults button').click();
    flushSync();
    expect(navigation.place).toEqual(['~defaults']);
    await waitForCondition(() => document.querySelector('.agent-shared-title'));
    getButton(t('agents.shared.back')).click();
    flushSync();
    expect(navigation.place).toEqual(['alpha']);
    expect(document.querySelector('.agent-editor-host').hidden).toBe(false);

    // A place naming an Agent the roster does not know yet (the App remaps
    // the place after a rename elsewhere) is read once more and shown.
    const rosterReads = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list').length;
    let readsBefore = rosterReads();
    agents[0] = { ...agents[0], id: 'researcher' };
    navigation.replace(['researcher']);
    flushSync();
    await waitForCondition(() => textInputValue('agent-id') === 'researcher');
    expect(navigation.place).toEqual(['researcher']);
    expect(rosterReads()).toBe(readsBefore + 1);

    // An Agent the read does not find leaves the shown one and corrects the
    // entry.
    readsBefore = rosterReads();
    navigation.navigate(['ghost']);
    flushSync();
    await waitForCondition(() => navigation.place[0] === 'researcher', 100);
    expect(rosterReads()).toBe(readsBefore + 1);
    expect(textInputValue('agent-id')).toBe('researcher');
  });

  it('keeps the Agent on one page while local disclosures preserve edited fields', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.querySelector('#agent-name'));
    const name = document.querySelector('#agent-name');
    setTextInputValue('agent-name', 'Draft name');
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    for (const topic of ['overview', 'behavior', 'access']) {
      expect(
        document.querySelector('#agent-detail-panel-' + topic).hidden,
      ).toBe(false);
    }
    const advanced = document.querySelector('#agent-detail-panel-details');
    expect(advanced.open).toBe(false);
    advanced.querySelector('summary').click();
    flushSync();
    expect(advanced.open).toBe(true);
    expect(document.querySelector('#agent-name')).toBe(name);
    expect(name.value).toBe('Draft name');
    expect(
      document.querySelector('.agent-detail-scroll .management-header'),
    ).not.toBeNull();
  });

  it.each([
    [
      'agent-thinking-effort',
      'agent-model-settings',
      'agent-model-settings__thinking-list',
    ],
    [
      'agent-memory-prompt-mode',
      'agents-view__memory-group',
      'agents-view__memory-list',
    ],
  ])(
    'renders the %s dropdown inside its %s with a portaled list',
    async (dropdownId, groupClass, listClass) => {
      rpcMock.mockImplementation(createAgentsRpcMock());
      mountedComponent = mount(AgentsView, { target: document.body });
      flushSync();
      await waitForCondition(() =>
        document.body.querySelector(`button#${dropdownId}`),
      );

      expect(getSimpleRoot(dropdownId).closest('.s-group')).toBe(
        document.body.querySelector(`.s-group.${groupClass}`),
      );
      openSimpleDropdown(dropdownId);
      const list = getSimpleList(dropdownId);
      expect(list.classList.contains(listClass)).toBe(true);
      expect(list.closest('.s-group')).toBeNull();
    },
  );

  it('reports model and Project catalog load failures in their sections', async () => {
    const handler = createAgentsRpcMock();
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'connection.list') {
        throw new Error('connection catalog failed');
      }
      if (method === 'project.list') {
        throw new Error('projects offline');
      }
      return handler(method, params);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('connection catalog failed') &&
        document.querySelector('.agents-view__catalog-error'),
      100,
    );
    expect(
      document.querySelector('.agents-view__catalog-error').textContent,
    ).toContain('projects offline');
    await openSearchableDropdown('agent-model');
    expect(searchableOptionLabels('agent-model')).not.toContain(
      'openai/gpt-5.2',
    );
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
    expect(agentItem.closest('.agent-list-row').firstElementChild).toBe(
      agentItem,
    );

    // The row's details card gives the complete Model and the id.
    document.body.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
    );
    agentItem.focus();
    const card = document.getElementById('app-tooltip');
    expect(card.querySelector('.app-tooltip__title').textContent).toBe('Alpha');
    const rows = Object.fromEntries(
      [...card.querySelectorAll('dt')].map((term) => [
        term.textContent,
        term.nextElementSibling.textContent,
      ]),
    );
    expect(rows[t('agents.form.model')]).toBe('openai/gpt-5.2');
    expect(rows[t('agents.details.id')]).toBe('alpha');
    expect(card.dataset.floatingSide).toBe('right');
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

    const navigation = createStandaloneNavigation();
    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { navigation },
    });
    flushSync();

    await waitForCondition(() => listedAgentIds().includes('alpha'), 100);

    getButtonByAriaLabel('Create agent').click();
    flushSync();

    const modal = getDialog(t('agents.create'));
    expect(modal.querySelector('#agent-create-id')).toBeTruthy();
    expect(modal.querySelector('#agent-create-name')).toBeTruthy();
    expect(modal.querySelector('#agent-create-model')).toBeTruthy();
    expect(modal.querySelector('#agent-create-thinking-effort')).toBeTruthy();
    // Sampling stays with the inherited values; it is set in the editor.
    expect(modal.querySelector('input[inputmode="decimal"]')).toBeNull();
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
    });
    expect(createCall[1]).not.toHaveProperty('temperature');
    expect(createCall[1]).not.toHaveProperty('top_p');

    await waitForCondition(() => listedAgentIds().includes('bravo'), 100);
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    // Showing the created Agent is a step to its place.
    expect(navigation.place).toEqual(['bravo']);
    await waitForCondition(() => textInputValue('agent-id') === 'bravo', 100);
  });

  it('keeps the current Agent selected while the Add modal is open and after cancelling it', async () => {
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

    await waitForCondition(() => textInputValue('agent-id') === 'alpha', 100);

    getButtonByAriaLabel('Create agent').click();
    flushSync();
    await flushAsyncUpdates(1);

    getDialog(t('agents.create'));
    expect(
      document.body.querySelector('button.agent-item.active').textContent,
    ).toContain('Alpha');
    expect(textInputValue('agent-id')).toBe('alpha');
    expect(textInputValue('agent-name')).toBe('Alpha');

    getButton(t('common.cancel')).click();
    flushSync();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();

    getAgentButton('Bravo').click();
    flushSync();
    await waitForCondition(() => textInputValue('agent-id') === 'bravo', 100);
    expect(textInputValue('agent-name')).toBe('Bravo');
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

    getButtonByAriaLabel('Create agent').click();
    flushSync();
    await flushAsyncUpdates();

    // The three run fields default to the inherit state. The model inherit
    // option shows the global default fetched on open.
    await waitForCondition(
      () =>
        triggerTextContent(getSearchableTrigger('agent-create-model')) ===
        t('inherit.option', {
          value: 'openai/gpt-5.2',
        }),
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

  it('reorders agents with Alt+Arrow keys, keeps focus and persists the roster revision', async () => {
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

    pressMoveUp(agentButton('charlie'));
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some(([method]) => method === 'agent.reorder'),
      100,
    );
    expect(listedAgentIds()).toEqual(['alpha', 'charlie', 'bravo']);
    expect(
      rpcMock.mock.calls.find(([method]) => method === 'agent.reorder')[1],
    ).toEqual({
      agent_ids: ['alpha', 'charlie', 'bravo'],
      expected_revision: 7,
    });
    await waitForCondition(
      () => document.activeElement === agentButton('charlie'),
      100,
    );
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

    pressMoveUp(agentButton('bravo'));
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

  it('offers Open chat, Copy ID and a confirmed Delete on another Agent row', async () => {
    const onOpenChat = vi.fn();
    const onToast = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const navigation = createStandaloneNavigation(['alpha']);
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
      }),
    );
    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { navigation, onOpenChat, onToast },
    });
    flushSync();
    await waitForCondition(() => listedAgentIds().length === 2, 100);

    let items = openRowMenu(agentButton('bravo'));
    expect(menuLabels(items)).toEqual(['Open chat', 'Copy ID', 'Delete…']);
    items[0].click();
    expect(onOpenChat).toHaveBeenCalledWith('bravo');

    openRowMenu(agentButton('bravo'))[1].click();
    await waitForCondition(() => onToast.mock.calls.length === 1);
    expect(writeText).toHaveBeenCalledWith('bravo');
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );

    // Cancelling the confirmation deletes nothing.
    openRowMenu(agentButton('bravo'))[2].click();
    flushSync();
    getButton(t('common.cancel')).click();
    flushSync();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();

    openRowMenu(agentButton('bravo'))[2].click();
    flushSync();
    expect(getDialog(t('agents.delete')).textContent).toContain('Bravo');
    getButton(t('common.delete')).click();
    await waitForCondition(() => listedAgentIds().join() === 'alpha', 100);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'agent.delete'),
    ).toEqual([['agent.delete', { id: 'bravo' }]]);
    // The shown Agent keeps its editor.
    expect(navigation.place).toEqual(['alpha']);
    expect(textInputValue('agent-name')).toBe('Alpha');

    items = openRowMenu(agentButton('alpha'));
    expect(items[2].disabled).toBe(true);
    expect(items[2].textContent).toContain(t('agents.menu.lastAgent'));
  });

  it('confirms deleting the shown Agent from its editor, reports a refusal and then shows another Agent', async () => {
    const onToast = vi.fn();
    const navigation = createStandaloneNavigation(['alpha']);
    const agentDelete = vi.fn().mockRejectedValueOnce(
      Object.assign(new Error('cannot delete agent referenced by cron:c1'), {
        code: 'agent_in_use',
      }),
    );
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
        agentDelete,
      }),
    );
    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { navigation, onToast },
    });
    flushSync();
    await waitForCondition(() => textInputValue('agent-name') === 'Alpha', 100);

    const deleteFromEditor = async () => {
      getButton(t('agents.delete')).click();
      flushSync();
      expect(getDialog(t('agents.delete')).textContent).toContain('Alpha');
      getButton(t('common.delete')).click();
      await flushAsyncUpdates();
    };

    await deleteFromEditor();
    await waitForCondition(() => onToast.mock.calls.length === 1);
    expect(onToast).toHaveBeenCalledWith({
      title: t('agents.deleteError'),
      message: t('agents.deleteInUse'),
      variant: 'error',
    });
    expect(listedAgentIds()).toEqual(['alpha', 'bravo']);

    // The dialog's permanent option skips the Archive.
    getButton(t('agents.delete')).click();
    flushSync();
    const dialog = getDialog(t('agents.delete'));
    expect(dialog.textContent).toContain(t('archive.deleteNotice.kept'));
    [...dialog.querySelectorAll('[role="checkbox"]')]
      .find((box) =>
        box.textContent.includes(t('archive.deleteOption.permanent')),
      )
      .click();
    flushSync();
    expect(dialog.textContent).toContain(
      t('agents.deletePermanentConfirm', { name: 'Alpha' }),
    );
    getButton(t('archive.deletePermanently')).click();
    await waitForCondition(() => navigation.place[0] === 'bravo', 100);
    expect(agentDelete).toHaveBeenLastCalledWith({
      id: 'alpha',
      permanent: true,
    });
    expect(onToast).toHaveBeenLastCalledWith({
      title: t('agents.deletedPermanently'),
      variant: 'success',
    });
    expect(listedAgentIds()).toEqual(['bravo']);
    expect(textInputValue('agent-name')).toBe('Bravo');
  });

  it('renames through an explicit confirmation flow and keeps the renamed Agent selected', async () => {
    const onAgentSelected = vi.fn();
    const onAgentsChanged = vi.fn();
    const navigation = createStandaloneNavigation(['alpha']);
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { navigation, onAgentSelected, onAgentsChanged },
    });
    flushSync();
    await waitForCondition(() => textInputValue('agent-name') === 'Alpha', 100);
    onAgentSelected.mockClear();
    onAgentsChanged.mockClear();

    getButton(t('agents.rename.action')).click();
    flushSync();
    const dialog = getDialog(t('agents.rename.title'));
    setTextInputValueWithin(dialog, 0, 'researcher');
    dialog
      .querySelector('form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flushAsyncUpdates();

    expect(rpcMock).toHaveBeenCalledWith('agent.rename', {
      id: 'alpha',
      new_id: 'researcher',
    });
    expect(textInputValue('agent-id')).toBe('researcher');
    expect(navigation.place).toEqual(['researcher']);
    expect(onAgentSelected).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'researcher' }),
    );
    expect(onAgentsChanged).toHaveBeenCalledWith([
      expect.objectContaining({ id: 'researcher' }),
    ]);
  });

  // Leaving the editor saves the change too, once the decision is made.
  it.each([
    ['saving', t('agents.workspaceMove.dontCopy'), false],
    ['leaving the editor', t('agents.workspaceMove.copy'), true],
  ])(
    'on %s, saves a Workspace change only after the "%s" decision',
    async (trigger, decision, copyIdentityFiles) => {
      rpcMock.mockImplementation(createAgentsRpcMock());
      const mounted = mountUnderCoordinator();
      mountedComponent = mounted.component;
      await waitForCondition(
        () =>
          document.body.querySelector('#agent-workspace')?.value ===
          'C:/agents/alpha',
        100,
      );
      expect(
        document.body.querySelectorAll(
          '.agent-detail-pane label[for="agent-workspace"]',
        ),
      ).toHaveLength(1);

      setWorkspace('D:/agents/moved');
      expect(mounted.coordinator.hasPending()).toBe(true);
      let leaving = null;
      if (trigger === 'saving') {
        submitAgentForm();
      } else {
        leaving = mounted.coordinator.flushPending();
      }
      await waitForCondition(() => hasButton(decision));
      expect(getAgentUpdateCalls()).toHaveLength(0);

      getButton(decision).click();
      flushSync();
      await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
      expect(getAgentUpdateCalls()[0][1]).toEqual({
        id: 'alpha',
        workspace: 'D:/agents/moved',
        copy_workspace_identity_files: copyIdentityFiles,
      });
      if (leaving) {
        await expect(leaving).resolves.toBe(true);
      }
    },
  );

  // Cancel on leaving fails the save, which keeps the editor shown.
  it.each(['saving', 'leaving the editor'])(
    'cancels a Workspace save on %s without discarding the draft',
    async (trigger) => {
      rpcMock.mockImplementation(createAgentsRpcMock());
      const mounted = mountUnderCoordinator();
      mountedComponent = mounted.component;
      await waitForCondition(() =>
        document.body.querySelector('#agent-workspace'),
      );

      setWorkspace('D:/agents/draft');
      let leaving = null;
      if (trigger === 'saving') {
        submitAgentForm();
      } else {
        leaving = mounted.coordinator.flushPending();
      }
      await waitForCondition(() => hasButton(t('common.cancel')));
      getButton(t('common.cancel')).click();
      flushSync();

      if (leaving) {
        await expect(leaving).resolves.toBe(false);
      }
      expect(getAgentUpdateCalls()).toHaveLength(0);
      expect(document.body.querySelector('#agent-workspace').value).toBe(
        'D:/agents/draft',
      );
      expect(mounted.coordinator.hasPending()).toBe(true);
    },
  );

  it('resets a custom workspace to the default and hides the action once the default is saved', async () => {
    const defaultWorkspace = 'C:/data/agents/alpha/workspace';
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            workspace: 'C:/custom/rooted-repo',
            default_workspace: defaultWorkspace,
          },
        ],
        agentUpdate: (params) => ({
          ...baseAgent(),
          ...params,
          default_workspace: defaultWorkspace,
        }),
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        document.body.querySelector('#agent-workspace')?.value ===
        'C:/custom/rooted-repo',
      100,
    );

    findSetToDefaultButton().click();
    flushSync();

    getButton(t('agents.workspaceMove.dontCopy')).click();
    flushSync();

    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      workspace: defaultWorkspace,
      copy_workspace_identity_files: false,
    });
    // The saved Agent now uses its default Workspace, so the action disappears.
    await waitForCondition(() => findSetToDefaultButton() === undefined, 100);
  });

  // A Project change saves explicitly or on leaving, never automatically,
  // and holds back the edits made with it.
  it('saves a Project selection and the edits made with it on leaving', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        projects: [
          { project_id: 'demo', display_name: 'Demo', cwd: 'C:/repos/demo' },
        ],
      }),
    );
    const mounted = mountUnderCoordinator();
    mountedComponent = mounted.component;
    await waitForCondition(() => document.body.querySelector('#agent-project'));

    vi.useFakeTimers();
    try {
      openSimpleDropdown('agent-project');
      selectSimpleOption('agent-project', 'Demo');
      setTextInputValue('agent-name', 'Alpha Prime');
      await vi.advanceTimersByTimeAsync(800);
      await flushAsyncUpdates();
    } finally {
      vi.useRealTimers();
    }
    expect(getAgentUpdateCalls()).toHaveLength(0);
    expect(mounted.coordinator.hasPending()).toBe(true);

    await expect(mounted.coordinator.flushPending()).resolves.toBe(true);
    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      name: 'Alpha Prime',
      root_project_id: 'demo',
    });
  });

  it('auto-saves 800 ms after the last edit and confirms it in the save state', async () => {
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
    expect(toastMock).not.toHaveBeenCalled();
    expect(document.querySelector('.save-status button')).toBeNull();
    expect(
      document.querySelector('.save-status [role="status"]').textContent.trim(),
    ).toBe(t('common.saved'));
  });

  it('manual save cancels a pending agent autosave', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => textInputValue('agent-name') === 'Alpha', 100);

    vi.useFakeTimers();

    setTextInputValue('agent-name', 'Alpha Manual');
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

    await waitForCondition(() => textInputValue('agent-name') === 'Alpha', 100);

    vi.useFakeTimers();

    setTextInputValue('agent-name', 'Alpha Autosaved');

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);

    getAgentButton('Bravo').click();
    flushSync();

    expect(textInputValue('agent-name')).toBe('Bravo');

    resolveAgentUpdate();
    await flushAsyncUpdates();

    expect(textInputValue('agent-name')).toBe('Bravo');
  });
});
