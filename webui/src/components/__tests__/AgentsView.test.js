// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
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
  setNumberInputValueWithin,
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

function isBefore(first, second) {
  return Boolean(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
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
      .querySelector('[data-settings-section="defaults"] .s-save-button')
      .click();
    await waitForCondition(() => savedTemperature === 0.73);
    await flushAsyncUpdates();
    document.querySelector('.agent-item').click();
    flushSync();
    expect(document.querySelector('#agent-temperature').value).toBe('');
    expect(
      document.querySelector('.agents-view__model-group').textContent,
    ).toContain('0.73');
    expect(getAgentUpdateCalls()).toHaveLength(0);
  });

  it('opens an explicit Compaction target inside shared Agent defaults', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    const consumed = vi.fn();
    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: {
        targetDefaultsPanel: 'compaction',
        onDefaultsTargetHandled: consumed,
      },
    });
    flushSync();
    await waitForCondition(() =>
      document.querySelector('#agent-shared-compaction'),
    );
    expect(document.querySelector('#agent-shared-compaction').hidden).toBe(
      false,
    );
    expect(consumed).toHaveBeenCalledOnce();
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
      'agents-view__model-group',
      'agents-view__thinking-list',
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
    const row = agentItem.closest('.agent-list-row');
    expect(row.firstElementChild).toBe(agentItem);
    expect(row.lastElementChild).toBe(agentOrderHandle('alpha'));
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

    getButtonByAriaLabel('Create agent').click();
    flushSync();

    const modal = getDialog(t('agents.create'));
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
    agentOrderHandle('charlie')
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
    ).toContain(
      t('agents.order.announcement', { name: 'Bravo', position: 1, total: 2 }),
    );
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

  it('renames through an explicit confirmation flow and keeps the renamed Agent selected', async () => {
    const onAgentSelected = vi.fn();
    const onAgentsChanged = vi.fn();
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { onAgentSelected, onAgentsChanged },
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
    expect(onAgentSelected).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'researcher' }),
    );
    expect(onAgentsChanged).toHaveBeenCalledWith([
      expect.objectContaining({ id: 'researcher' }),
    ]);
  });

  it.each([
    [t('agents.workspaceMove.dontCopy'), false],
    [t('agents.workspaceMove.copy'), true],
  ])(
    'saves a Workspace change only after the "%s" decision',
    async (decision, copyIdentityFiles) => {
      rpcMock.mockImplementation(createAgentsRpcMock());
      mountedComponent = mount(AgentsView, { target: document.body });
      flushSync();
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
      submitAgentForm();
      expect(getAgentUpdateCalls()).toHaveLength(0);

      getButton(decision).click();
      flushSync();
      await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
      expect(getAgentUpdateCalls()[0][1]).toEqual({
        id: 'alpha',
        workspace: 'D:/agents/moved',
        copy_workspace_identity_files: copyIdentityFiles,
      });
    },
  );

  it('cancels a workspace save without discarding the draft', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.querySelector('#agent-workspace'),
    );

    setWorkspace('D:/agents/draft');
    submitAgentForm();
    getButton(t('common.cancel')).click();
    flushSync();

    expect(getAgentUpdateCalls()).toHaveLength(0);
    expect(document.body.querySelector('#agent-workspace').value).toBe(
      'D:/agents/draft',
    );
  });

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

  it('saves the edit-only Project selection independently of Workspace', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        projects: [
          { project_id: 'demo', display_name: 'Demo', cwd: 'C:/repos/demo' },
        ],
      }),
    );
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.body.querySelector('#agent-project'));

    openSimpleDropdown('agent-project');
    selectSimpleOption('agent-project', 'Demo');
    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      root_project_id: 'demo',
    });
  });

  it('auto-saves 800 ms after the last edit and treats a later Saved click as a no-op', async () => {
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

    const saveButton = getButton(t('common.saved'));
    expect(saveButton.disabled).toBe(false);
    saveButton.click();
    flushSync();
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );
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
