// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import {
  rpcMock,
  openSimpleDropdown,
  selectSimpleOption,
  simpleOptionLabels,
  getButton,
  getDialog,
  getButtonByAriaLabel,
  submitAgentForm,
  getAgentUpdateCalls,
  flushAsyncUpdates,
  createAgentsRpcMock,
  baseAgent,
  waitForCondition,
  waitForText,
} from './AgentsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: AgentsView } = await import('../AgentsView.svelte');

const AGENT_MEMORY = () => t('agents.memory.scope.agent');
const USER_PROFILE = () => t('agents.memory.scope.user');
const ADD_MEMORY = () => t('agents.memory.add');

function toolAccessToggle(name) {
  const button = document.body.querySelector(
    `[data-tool-name="${name}"][data-tool-access-toggle]`,
  );
  expect(button).toBeTruthy();
  return button;
}

function waitForElement(selector) {
  return waitForCondition(() => document.body.querySelector(selector), 100);
}

function memoryScopeNamed(name) {
  const scope = Array.from(
    document.body.querySelectorAll('.agents-view__memory-scope'),
  ).find(
    (candidate) => candidate.querySelector('h4')?.textContent.trim() === name,
  );
  expect(scope).toBeTruthy();
  return scope;
}

function getButtonWithin(container, label) {
  const button = Array.from(container.querySelectorAll('button')).find(
    (candidate) => candidate.textContent.trim() === label,
  );
  expect(button).toBeTruthy();
  return button;
}

function typeInto(textarea, value) {
  textarea.value = value;
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function rpcParams(method) {
  return rpcMock.mock.calls.find((call) => call[0] === method)[1];
}

async function advanceAutosave() {
  await vi.advanceTimersByTimeAsync(800);
  await flushAsyncUpdates();
}

function agentGroupsFixture(allowed) {
  return {
    agents: [
      {
        ...baseAgent(),
        tools: {
          bash: { allowed_env: ['TEST_GROUP_ACCESS'] },
          subagent: { allowed_agents: allowed },
        },
      },
      { ...baseAgent(), id: 'worker' },
      { ...baseAgent(), id: 'helper' },
    ],
    projects: ['vbot', 'demo'].map((project_id) => ({
      project_id,
      display_name: project_id,
    })),
    projectScans: Object.fromEntries(
      ['vbot', 'demo'].map((project_id) => [
        project_id,
        {
          project: { project_id, display_name: project_id },
          scan: { team: [{ agent_id: 'builder', display_name: 'Builder' }] },
        },
      ]),
    ),
  };
}

describe('AgentsView behavior and access', () => {
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

  it('sends custom system prompt toggle changes from the agent detail pane', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForElement('button[aria-label="Custom system prompt"]');

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

  it.each([
    [true, 'confirms first'],
    [false, 'switches off directly'],
  ])(
    'disabling a custom prompt with has_customizations=%s %s',
    async (hasCustomizations) => {
      rpcMock.mockImplementation(
        createAgentsRpcMock({
          agents: [{ ...baseAgent(), custom_system_prompt_enabled: true }],
          scopes: [
            { type: 'default', label: 'Default' },
            {
              type: 'agent',
              agent_id: 'alpha',
              label: 'Alpha',
              has_customizations: hasCustomizations,
            },
          ],
        }),
      );
      const toggleState = () =>
        getButtonByAriaLabel('Custom system prompt').getAttribute(
          'aria-checked',
        );

      mountedComponent = mount(AgentsView, { target: document.body });
      flushSync();
      await waitForElement('button[aria-label="Custom system prompt"]');
      expect(toggleState()).toBe('true');

      getButtonByAriaLabel('Custom system prompt').click();
      flushSync();
      await flushAsyncUpdates();

      if (hasCustomizations) {
        // The toggle only flips once the dialog is confirmed.
        const dialog = getDialog(t('agents.confirmDisableCustomPrompt.title'));
        expect(toggleState()).toBe('true');
        getButtonWithin(
          dialog,
          t('agents.confirmDisableCustomPrompt.confirm'),
        ).click();
        flushSync();
        await flushAsyncUpdates();
      } else {
        expect(document.body.querySelector('[role="dialog"]')).toBeNull();
      }

      expect(toggleState()).toBe('false');
    },
  );

  it('sends memory prompt mode changes from the agent detail pane', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForElement('button#agent-memory-prompt-mode');

    openSimpleDropdown('agent-memory-prompt-mode');
    const modeLabel = (mode) => t(`agents.form.memoryPromptModeOption.${mode}`);
    expect(simpleOptionLabels('agent-memory-prompt-mode')).toEqual(
      ['off', 'agent', 'agent_user'].map(modeLabel),
    );

    selectSimpleOption('agent-memory-prompt-mode', modeLabel('agent'));
    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      memory_prompt_mode: 'agent',
    });
  });

  it('shows both empty Memory categories while they are inactive and keeps adding available', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), memory_prompt_mode: 'off' }],
        memories: { agent: [], user: [] },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForElement('button[aria-label="Manage Memory entries"]');

    getButtonByAriaLabel('Manage Memory entries').click();
    flushSync();
    await waitForText(t('agents.memory.emptyTitle'));
    expect(rpcParams('memory.list')).toEqual({ agent_id: 'alpha' });

    const scopes = Array.from(
      document.body.querySelectorAll('.agents-view__memory-scope'),
    );
    expect(scopes).toHaveLength(2);
    expect(
      scopes.every((scope) =>
        scope.classList.contains('agents-view__memory-scope--inactive'),
      ),
    ).toBe(true);
    expect(
      document.body.querySelectorAll('.agents-view__memory-empty'),
    ).toHaveLength(2);
    expect(getButton(ADD_MEMORY()).disabled).toBe(true);

    const agentScope = memoryScopeNamed(AGENT_MEMORY());
    memoryScopeNamed(USER_PROFILE());
    typeInto(
      agentScope.querySelector('textarea[aria-label="New Agent Memory entry"]'),
      'Still editable while inactive.',
    );
    const addButton = getButtonWithin(agentScope, ADD_MEMORY());
    expect(addButton.disabled).toBe(false);
    addButton.click();
    await waitForText('Still editable while inactive.');
    expect(rpcParams('memory.add')).toEqual({
      agent_id: 'alpha',
      scope: 'agent',
      content: 'Still editable while inactive.',
    });
  });

  it('adds, edits, and deletes Memory entries from the expanded Agent editor', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        memories: {
          agent: [{ id: 1, scope: 'agent', content: 'Keep releases small.' }],
          user: [],
        },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForElement('button[aria-label="Manage Memory entries"]');
    getButtonByAriaLabel('Manage Memory entries').click();
    await waitForText('Keep releases small.');

    const agentScope = memoryScopeNamed(AGENT_MEMORY());
    getButtonWithin(agentScope, t('common.edit')).click();
    flushSync();
    typeInto(
      agentScope.querySelector('textarea[aria-label="Edit Memory entry"]'),
      'Keep releases focused.',
    );
    getButtonWithin(agentScope, t('common.save')).click();
    await waitForText('Keep releases focused.');

    const userScope = memoryScopeNamed(USER_PROFILE());
    typeInto(
      userScope.querySelector('textarea[aria-label="New User profile entry"]'),
      'Prefers concise answers.',
    );
    getButtonWithin(userScope, ADD_MEMORY()).click();
    await waitForText('Prefers concise answers.');

    getButtonWithin(
      memoryScopeNamed(AGENT_MEMORY()),
      t('common.delete'),
    ).click();
    flushSync();
    getButton(t('agents.memory.deleteConfirmAction')).click();
    await waitForCondition(
      () => !document.body.textContent.includes('Keep releases focused.'),
      100,
    );

    expect(rpcParams('memory.replace')).toEqual({
      agent_id: 'alpha',
      scope: 'agent',
      entry_id: 1,
      content: 'Keep releases focused.',
    });
    expect(rpcParams('memory.add')).toEqual({
      agent_id: 'alpha',
      scope: 'user',
      content: 'Prefers concise answers.',
    });
    expect(rpcParams('memory.remove')).toEqual({
      agent_id: 'alpha',
      scope: 'agent',
      entry_id: 1,
    });
  });

  it('starts an Agent Compaction Policy from the effective policy and edits it as rows', async () => {
    const effectivePolicy = {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.7 },
      strategy: { type: 'continuation' },
    };
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            compaction_policy: null,
            effective_compaction_policy: effectivePolicy,
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForElement('button[aria-label="Use an Agent Policy"]');

    const group = document.querySelector('.agents-view__compaction-group');
    expect(
      group.querySelector('[data-testid="agent-compaction-editor"]'),
    ).toBeNull();
    vi.useFakeTimers();
    getButtonByAriaLabel('Use an Agent Policy').click();
    flushSync();

    const editor = group.querySelector(
      '[data-testid="agent-compaction-editor"]',
    );
    expect(editor.classList.contains('s-row')).toBe(true);
    expect(
      group.querySelector('input[name="agent-compaction-strategy"]:checked')
        .value,
    ).toBe('continuation');
    await advanceAutosave();
    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      compaction_policy: effectivePolicy,
    });
  });

  it('auto-saves a Tool denial without clearing the Sub-Agent settings of the denied Tool', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            tools: { subagent: { allowed_agents: ['worker'] } },
          },
        ],
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          { name: 'subagent', description: 'Delegate work.' },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForText(t('agents.form.subagentTargets'));

    vi.useFakeTimers();
    toolAccessToggle('subagent').click();
    flushSync();
    await advanceAutosave();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      tool_access: { mode: 'all', denied: ['subagent'] },
    });
  });

  it('orders access sections as Tools, Skills, then Sub-Agents', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [baseAgent(), { ...baseAgent(), id: 'worker' }],
        tools: [{ name: 'subagent', description: 'Delegate work.' }],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForElement('button[aria-label="Toggle agent worker"]');

    const panel = document.querySelector('#agent-detail-panel-access');
    expect(panel.hidden).toBe(false);
    const sections = Array.from(panel.querySelectorAll(':scope > .s-section'));
    expect(sections).toHaveLength(3);
    expect(sections[0].contains(toolAccessToggle('subagent'))).toBe(true);
    expect(
      sections[1].contains(getButtonByAriaLabel('Toggle skill sample-skill')),
    ).toBe(true);
    expect(
      sections[2].contains(getButtonByAriaLabel('Toggle agent worker')),
    ).toBe(true);
  });

  it('hides Sub-Agent settings when neither Sub-Agent tool is allowed', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            tool_access: { mode: 'selected', allowed: ['bash'] },
            tools: { subagent: { allowed_agents: ['worker'] } },
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    const accessSections = () =>
      document.body.querySelectorAll('#agent-detail-panel-access > .s-section');
    await waitForCondition(() => accessSections().length === 2, 100);

    expect(document.body.textContent).not.toContain(
      t('agents.form.subagentTargets'),
    );
  });

  it('auto-saves Identity and qualified Project Agent target access', async () => {
    const worker = { ...baseAgent(), id: 'worker', name: 'Worker' };
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), root_project_id: 'vbot' }, worker],
        projects: [
          { project_id: 'vbot', display_name: 'vBot', cwd: 'C:/repos/vbot' },
        ],
        projectScans: {
          vbot: {
            project: { project_id: 'vbot', display_name: 'vBot' },
            scan: {
              team: [{ agent_id: 'builder', display_name: 'Builder' }],
            },
          },
        },
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForText('builder@vbot');
    expect(
      document.body.querySelector('button[aria-label="Toggle agent alpha"]'),
    ).toBeNull();

    vi.useFakeTimers();
    getButtonByAriaLabel('Toggle agent worker').click();
    flushSync();
    await advanceAutosave();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      tools: {
        subagent: { allowed_agents: ['builder@vbot'] },
      },
    });
  });

  it.each([
    ['identity', ['*'], 'true', 1, ['builder@vbot', 'builder@demo']],
    ['project', ['*'], 'true', 1, ['worker', 'helper']],
    [
      'identity',
      ['builder@vbot'],
      'false',
      1,
      ['builder@vbot', 'worker', 'helper'],
    ],
    [
      'project',
      ['worker'],
      'false',
      1,
      ['worker', 'builder@vbot', 'builder@demo'],
    ],
    // A mixed group checkbox selects the whole group first; a second click
    // clears it.
    ['identity', ['worker', 'builder@vbot'], 'mixed', 2, ['builder@vbot']],
    ['project', ['worker', 'builder@vbot'], 'mixed', 2, ['worker']],
  ])(
    'sets %s Agents with the group checkbox while preserving the other group (%j)',
    async (group, allowed, initialState, clicks, expected) => {
      rpcMock.mockImplementation(
        createAgentsRpcMock(agentGroupsFixture(allowed)),
      );
      mountedComponent = mount(AgentsView, { target: document.body });
      await waitForText('builder@demo');
      flushSync();
      vi.useFakeTimers();
      const groupCheckbox = getButtonByAriaLabel(
        group === 'identity' ? 'All Identity Agents' : 'All Project Agents',
      );
      expect(groupCheckbox.getAttribute('role')).toBe('checkbox');
      expect(groupCheckbox.getAttribute('aria-checked')).toBe(initialState);
      for (let click = 0; click < clicks; click += 1) {
        groupCheckbox.click();
        flushSync();
      }
      await advanceAutosave();
      expect(getAgentUpdateCalls()).toHaveLength(1);
      expect(getAgentUpdateCalls()[0][1]).toEqual({
        id: 'alpha',
        tools: {
          bash: { allowed_env: ['TEST_GROUP_ACCESS'] },
          subagent: { allowed_agents: expected },
        },
      });
    },
  );

  it('starts Project Agents collapsed and keeps their full selection count through filtering and toggles', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock(agentGroupsFixture(['worker', 'builder@vbot'])),
    );
    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForText('builder@demo');
    const disclosure = document.getElementById('agent-project-targets-toggle');
    const content = document.getElementById('agent-project-targets');
    expect(disclosure.getAttribute('aria-expanded')).toBe('false');
    expect(disclosure.textContent).toContain('1/2');
    expect(content.hidden).toBe(true);
    expect(
      document
        .querySelector(
          'section[aria-labelledby="agent-identity-targets-label"]',
        )
        .contains(getButtonByAriaLabel('Toggle agent builder@vbot')),
    ).toBe(false);

    disclosure.click();
    flushSync();
    expect(content.hidden).toBe(false);
    const search = document.querySelector(
      '#agent-detail-panel-access input[type="search"][aria-label="Filter Agents"]',
    );
    expect(content.contains(search)).toBe(false);
    search.value = 'demo';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(content.querySelectorAll('[role="checkbox"]')).toHaveLength(1);
    expect(disclosure.textContent).toContain('1/2');
    vi.useFakeTimers();
    getButtonByAriaLabel('Toggle agent builder@demo').click();
    flushSync();
    expect(disclosure.textContent).toContain('2/2');
    disclosure.click();
    flushSync();
    expect(content.hidden).toBe(true);
    expect(disclosure.textContent).toContain('2/2');
    await advanceAutosave();
    expect(getAgentUpdateCalls()[0][1].tools.subagent.allowed_agents).toEqual([
      'worker',
      'builder@vbot',
      'builder@demo',
    ]);
  });

  it('preserves unavailable targets in their respective groups', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock(
        agentGroupsFixture(['missing-identity', 'missing@retired']),
      ),
    );
    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForText('builder@demo');
    const identity = document.querySelector(
      'section[aria-labelledby="agent-identity-targets-label"]',
    );
    const project = document.getElementById('agent-project-targets');
    expect(
      identity.contains(getButtonByAriaLabel('Toggle agent missing-identity')),
    ).toBe(true);
    expect(
      project.contains(getButtonByAriaLabel('Toggle agent missing@retired')),
    ).toBe(true);
    expect(
      document.getElementById('agent-project-targets-toggle').textContent,
    ).toContain('1/3');
    vi.useFakeTimers();
    // The mixed Identity group selects everything first, then clears.
    const identityAll = getButtonByAriaLabel('All Identity Agents');
    expect(identity.contains(identityAll)).toBe(true);
    identityAll.click();
    flushSync();
    identityAll.click();
    flushSync();
    await advanceAutosave();
    expect(getAgentUpdateCalls()[0][1].tools.subagent.allowed_agents).toEqual([
      'missing@retired',
    ]);
  });

  it('shows an existing wildcard as a full group and keeps new complete selections explicit', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock(agentGroupsFixture(['*'])));
    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForText('builder@demo');
    vi.useFakeTimers();
    const identityAll = getButtonByAriaLabel('All Identity Agents');
    expect(identityAll.getAttribute('aria-checked')).toBe('true');
    expect(
      getButtonByAriaLabel('All Project Agents').getAttribute('aria-checked'),
    ).toBe('true');

    identityAll.click();
    flushSync();
    expect(identityAll.getAttribute('aria-checked')).toBe('false');
    identityAll.click();
    flushSync();
    await advanceAutosave();
    expect(getAgentUpdateCalls()[0][1].tools.subagent.allowed_agents).toEqual([
      'builder@vbot',
      'builder@demo',
      'worker',
      'helper',
    ]);
  });

  it.each([
    ['agent_user', () => t('toolAccess.activation.memoryOn')],
    ['off', () => t('toolAccess.activation.memoryOff')],
  ])(
    'passes Memory mode %s to the automatic memory Tool while keeping its block control',
    async (memoryPromptMode, activationNote) => {
      rpcMock.mockImplementation(
        createAgentsRpcMock({
          agents: [{ ...baseAgent(), memory_prompt_mode: memoryPromptMode }],
          tools: [
            { name: 'bash', description: 'Run shell commands.' },
            {
              name: 'memory',
              description: 'Manage pinned memory.',
              activation: 'memory_mode',
            },
          ],
        }),
      );

      mountedComponent = mount(AgentsView, { target: document.body });
      flushSync();
      await waitForElement(
        '[data-tool-name="memory"][data-tool-access-toggle]',
      );

      const memoryChip = toolAccessToggle('memory');
      expect(memoryChip.getAttribute('aria-label')).toBe('memory');
      expect(memoryChip.getAttribute('aria-checked')).toBe('true');
      expect(memoryChip.disabled).toBe(false);
      expect(memoryChip.textContent).toContain(t('toolAccess.automatic'));
      expect(document.body.textContent).toContain(activationNote());
    },
  );

  it('renders skill descriptions, unmet requirements and invalid packages and excludes unticked Skills', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('sample-skill');

    expect(document.body.textContent).toContain('A loadable sample skill.');
    const skillToggle = getButtonByAriaLabel('Toggle skill warning-skill');
    const stateId = skillToggle.getAttribute('aria-describedby');
    expect(document.getElementById(stateId).textContent).toBe(
      'env:WARNING_TOKEN',
    );
    const invalidSkills = document.querySelector(
      '.agents-view__invalid-skills',
    );
    expect(invalidSkills.textContent).toContain('broken-skill');
    expect(invalidSkills.textContent).toContain('missing description');
    expect(skillToggle.getAttribute('aria-checked')).toBe('true');

    skillToggle.click();
    flushSync();

    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      excluded_skills: ['warning-skill'],
    });
  });

  it('keeps saved Skill names the catalog no longer lists', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            allowed_skills: ['sample-skill', 'retired-skill'],
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('retired-skill');
    expect(document.body.textContent).toContain('Saved but not found');
    getButtonByAriaLabel('Toggle skill sample-skill').click();
    flushSync();

    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      allowed_skills: ['retired-skill'],
    });
  });

  it('renders a not-ready tool with a visible status, verbatim hint, and extensions link', async () => {
    const navigateMock = vi.fn();
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        tools: [
          { name: 'bash', description: 'Run shell commands.', ready: true },
          {
            name: 'home_assistant',
            description: 'Control Home Assistant.',
            ready: false,
            readiness_hint: 'Set the Home Assistant token first.',
            extension: 'homeassistant',
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, {
      target: document.body,
      props: { onNavigateToSettingsPanel: navigateMock },
    });
    flushSync();

    await waitForText('home_assistant');

    // The server-delivered hint renders verbatim.
    expect(document.body.textContent).toContain(
      'Set the Home Assistant token first.',
    );

    // Policy controls stay editable because access is independent of readiness.
    const toolToggle = toolAccessToggle('home_assistant');
    expect(toolToggle.disabled).toBe(false);
    expect(
      toolToggle
        .closest('.tool-access-chip-wrap')
        .classList.contains('is-unavailable'),
    ).toBe(true);

    getButton(t('agents.tools.openExtensions')).click();
    flushSync();
    expect(navigateMock).toHaveBeenCalledWith('extensions');
  });
});
