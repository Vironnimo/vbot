// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';
import {
  rpcMock,
  openSimpleDropdown,
  selectSimpleOption,
  simpleOptionLabels,
  getButton,
  getDialog,
  getButtonByAriaLabel,
  submitAgentForm,
  findSetToDefaultButton,
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

function toolAccessToggle(name) {
  const button = document.body.querySelector(
    `[data-tool-name="${name}"][data-tool-access-toggle]`,
  );
  expect(button).toBeTruthy();
  return button;
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

    const workspaceLabels = document.body.querySelectorAll(
      '.agent-detail-pane label[for="agent-workspace"]',
    );
    expect(workspaceLabels).toHaveLength(1);

    const workspaceInput = document.body.querySelector('#agent-workspace');
    workspaceInput.value = 'D:/agents/alpha';
    workspaceInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    submitAgentForm();
    getButton("Don't copy").click();
    flushSync();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      workspace: 'D:/agents/alpha',
      copy_workspace_identity_files: false,
    });
  });

  it('resets a custom workspace to the default via the set-to-default button', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            workspace: 'C:/custom/rooted-repo',
            default_workspace: 'C:/data/agents/alpha/workspace',
          },
        ],
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

    const resetButton = findSetToDefaultButton();
    expect(resetButton).toBeTruthy();

    resetButton.click();
    flushSync();

    getButton("Don't copy").click();
    flushSync();

    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      workspace: 'C:/data/agents/alpha/workspace',
      copy_workspace_identity_files: false,
    });
  });

  it('copies identity files only after the workspace decision', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.querySelector('#agent-workspace'),
    );

    const workspaceInput = document.body.querySelector('#agent-workspace');
    workspaceInput.value = 'D:/agents/copied';
    workspaceInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    submitAgentForm();

    expect(getAgentUpdateCalls()).toHaveLength(0);
    getButton('Copy files').click();
    flushSync();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      workspace: 'D:/agents/copied',
      copy_workspace_identity_files: true,
    });
  });

  it('cancels a workspace save without discarding the draft', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());
    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.querySelector('#agent-workspace'),
    );

    const workspaceInput = document.body.querySelector('#agent-workspace');
    workspaceInput.value = 'D:/agents/draft';
    workspaceInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    submitAgentForm();
    getButton('Cancel').click();
    flushSync();

    expect(getAgentUpdateCalls()).toHaveLength(0);
    expect(document.body.querySelector('#agent-workspace').value).toBe(
      'D:/agents/draft',
    );
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

  it('hides the set-to-default button when the workspace already is the default', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            workspace: 'C:/data/agents/alpha/workspace',
            default_workspace: 'C:/data/agents/alpha/workspace',
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        document.body.querySelector('#agent-workspace')?.value ===
        'C:/data/agents/alpha/workspace',
      100,
    );

    expect(findSetToDefaultButton()).toBeUndefined();
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
      'Agent notes (MEMORY.md)',
      'Agent + user notes (MEMORY.md + USER.md)',
    ]);

    selectSimpleOption('agent-memory-prompt-mode', 'Agent notes (MEMORY.md)');
    submitAgentForm();
    await waitForCondition(() => getAgentUpdateCalls().length === 1, 100);

    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      memory_prompt_mode: 'agent',
    });
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

    toolAccessToggle('write').click();
    flushSync();

    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      tool_access: { mode: 'all', denied: ['write'] },
    });
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
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      tools: {
        subagent: { allowed_agents: ['builder@vbot'] },
      },
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
    await waitForCondition(() =>
      document.body.querySelector('button[aria-label="Toggle agent worker"]'),
    );

    getButton('Tools & Skills').click();
    flushSync();
    const panel = document.querySelector('#agent-detail-panel-access');
    expect(panel.hidden).toBe(false);
    const sections = Array.from(panel.querySelectorAll('.tl-section'));
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
    await waitForCondition(
      () => document.body.querySelectorAll('.tl-section').length === 2,
      100,
    );

    expect(document.body.querySelectorAll('.tl-section')).toHaveLength(2);
  });

  it.each([
    ['identity', 'all off', ['*'], ['builder@vbot', 'builder@demo']],
    ['project', 'all off', ['*'], ['worker', 'helper']],
    [
      'identity',
      'all on',
      ['builder@vbot'],
      ['builder@vbot', 'worker', 'helper'],
    ],
    [
      'project',
      'all on',
      ['worker'],
      ['worker', 'builder@vbot', 'builder@demo'],
    ],
    ['identity', 'all off', ['worker', 'builder@vbot'], ['builder@vbot']],
    ['project', 'all off', ['worker', 'builder@vbot'], ['worker']],
  ])(
    'sets %s Agents %s while preserving the other group (%j)',
    async (group, action, allowed, expected) => {
      rpcMock.mockImplementation(
        createAgentsRpcMock(agentGroupsFixture(allowed)),
      );
      mountedComponent = mount(AgentsView, { target: document.body });
      await waitForText('builder@demo');
      getButton('Tools & Skills').click();
      flushSync();
      vi.useFakeTimers();
      if (group === 'project') {
        document.getElementById('agent-project-targets-toggle').click();
        flushSync();
      }
      const section =
        group === 'identity'
          ? document.querySelector(
              'section[aria-labelledby="agent-identity-targets-label"]',
            )
          : document.getElementById('agent-project-targets');
      getButtonWithin(section, action).click();
      flushSync();
      await vi.advanceTimersByTimeAsync(800);
      await flushAsyncUpdates();
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
    expect(disclosure.textContent).toContain('(1/2)');
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
    const search = content.querySelector('input');
    search.value = 'demo';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(content.querySelectorAll('[role="switch"]')).toHaveLength(1);
    expect(disclosure.textContent).toContain('(1/2)');
    vi.useFakeTimers();
    getButtonByAriaLabel('Toggle agent builder@demo').click();
    flushSync();
    expect(disclosure.textContent).toContain('(2/2)');
    disclosure.click();
    flushSync();
    expect(content.hidden).toBe(true);
    expect(disclosure.textContent).toContain('(2/2)');
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
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
    ).toContain('(1/3)');
    vi.useFakeTimers();
    getButtonWithin(identity, 'all off').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
    expect(getAgentUpdateCalls()[0][1].tools.subagent.allowed_agents).toEqual([
      'missing@retired',
    ]);
  });

  it('keeps an existing wildcard on no-op group actions and keeps new complete selections explicit', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock(agentGroupsFixture(['*'])));
    mountedComponent = mount(AgentsView, { target: document.body });
    await waitForText('builder@demo');
    vi.useFakeTimers();
    const identity = document.querySelector(
      'section[aria-labelledby="agent-identity-targets-label"]',
    );
    getButtonWithin(identity, 'all on').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(800);
    expect(getAgentUpdateCalls()).toHaveLength(0);

    getButtonWithin(identity, 'all off').click();
    flushSync();
    getButtonWithin(identity, 'all on').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();
    expect(getAgentUpdateCalls()[0][1].tools.subagent.allowed_agents).toEqual([
      'builder@vbot',
      'builder@demo',
      'worker',
      'helper',
    ]);
  });

  it('does not clear Sub-Agent settings when its tool is temporarily disabled', async () => {
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
    await waitForText('Sub-Agent settings');

    vi.useFakeTimers();
    toolAccessToggle('subagent').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      id: 'alpha',
      tool_access: { mode: 'all', denied: ['subagent'] },
    });
  });

  it('renders Memory as automatic while keeping its independent block control', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          {
            name: 'memory',
            description: 'Manage pinned memory.',
            activation: 'memory_mode',
          },
          { name: 'write', description: 'Write files.' },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('write');

    const memoryChip = toolAccessToggle('memory');
    expect(memoryChip.textContent).toBe('memory');
    expect(memoryChip.getAttribute('aria-checked')).toBe('true');
    expect(memoryChip.classList.contains('is-automatic')).toBe(true);
    expect(memoryChip.disabled).toBe(false);
    expect(document.body.textContent).toContain('Automatic while Memory is on');
  });

  it('switches the memory tool row text when Memory is set to off', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), memory_prompt_mode: 'off' }],
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

    await waitForText('bash');

    const memoryChip = toolAccessToggle('memory');
    expect(memoryChip.textContent).toBe('memory');
    expect(memoryChip.getAttribute('aria-checked')).toBe('true');
    expect(document.body.textContent).toContain('Memory is currently off');
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
    await waitForText('Manage Memory entries');

    getButtonByAriaLabel('Manage Memory entries').click();
    flushSync();
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'memory.list'),
      100,
    );
    await waitForText('No memories');

    const scopes = Array.from(
      document.body.querySelectorAll('.agents-view__memory-scope'),
    );
    expect(scopes).toHaveLength(2);
    expect(
      scopes.every((scope) =>
        scope.classList.contains('agents-view__memory-scope--inactive'),
      ),
    ).toBe(true);
    expect(document.body.textContent).toContain('Agent Memory');
    expect(document.body.textContent).toContain('User profile');
    expect(
      document.body.querySelectorAll('.agents-view__memory-empty'),
    ).toHaveLength(2);
    expect(getButton('Add Memory').disabled).toBe(true);
    const inactiveAgentScope = memoryScopeNamed('Agent Memory');
    const inactiveAddArea = inactiveAgentScope.querySelector(
      'textarea[aria-label="New Agent Memory entry"]',
    );
    inactiveAddArea.value = 'Still editable while inactive.';
    inactiveAddArea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const inactiveAddButton = getButtonWithin(inactiveAgentScope, 'Add Memory');
    expect(inactiveAddButton.disabled).toBe(false);
    inactiveAddButton.click();
    await waitForText('Still editable while inactive.');
    expect(
      rpcMock.mock.calls.find((call) => call[0] === 'memory.add')[1],
    ).toEqual({
      agent_id: 'alpha',
      scope: 'agent',
      content: 'Still editable while inactive.',
    });
    expect(
      rpcMock.mock.calls.find((call) => call[0] === 'memory.list')[1],
    ).toEqual({ agent_id: 'alpha' });
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
    await waitForText('Manage Memory entries');
    getButtonByAriaLabel('Manage Memory entries').click();
    await waitForText('Keep releases small.');

    const agentScope = memoryScopeNamed('Agent Memory');
    getButtonWithin(agentScope, 'Edit').click();
    flushSync();
    const editArea = agentScope.querySelector(
      'textarea[aria-label="Edit Memory entry"]',
    );
    editArea.value = 'Keep releases focused.';
    editArea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    getButtonWithin(agentScope, 'Save').click();
    await waitForText('Keep releases focused.');

    const userScope = memoryScopeNamed('User profile');
    const addArea = userScope.querySelector(
      'textarea[aria-label="New User profile entry"]',
    );
    addArea.value = 'Prefers concise answers.';
    addArea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    getButtonWithin(userScope, 'Add Memory').click();
    await waitForText('Prefers concise answers.');

    getButtonWithin(memoryScopeNamed('Agent Memory'), 'Delete').click();
    flushSync();
    getButton('Delete Memory').click();
    await waitForCondition(
      () => !document.body.textContent.includes('Keep releases focused.'),
      100,
    );

    expect(
      rpcMock.mock.calls.find((call) => call[0] === 'memory.replace')[1],
    ).toEqual({
      agent_id: 'alpha',
      scope: 'agent',
      entry_id: 1,
      content: 'Keep releases focused.',
    });
    expect(
      rpcMock.mock.calls.find((call) => call[0] === 'memory.add')[1],
    ).toEqual({
      agent_id: 'alpha',
      scope: 'user',
      content: 'Prefers concise answers.',
    });
    expect(
      rpcMock.mock.calls.find((call) => call[0] === 'memory.remove')[1],
    ).toEqual({ agent_id: 'alpha', scope: 'agent', entry_id: 1 });
  });

  it('renders skill catalog warnings and unavailable diagnostics', async () => {
    rpcMock.mockImplementation(createAgentsRpcMock());

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('sample-skill');

    expect(document.body.textContent).toContain('A loadable sample skill.');
    expect(document.body.textContent).toContain('name differs from folder');
    const invalidSkills = document.querySelector(
      '.agents-view__invalid-skills',
    );
    expect(invalidSkills.textContent).toContain('broken-skill');
    expect(invalidSkills.textContent).toContain('missing description');

    const skillToggle = Array.from(
      document.body.querySelectorAll('button'),
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
    expect(updateCall[1].allowed_skills).toEqual(['sample-skill']);
  });

  it('renders a not-ready tool greyed with a badge, verbatim hint, and extensions link', async () => {
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

    // The not-ready state and server-delivered hint (verbatim) both render.
    expect(document.body.textContent).toContain(
      'Set the Home Assistant token first.',
    );

    // Policy controls stay editable because access is independent of readiness.
    const toolToggle = toolAccessToggle('home_assistant');
    expect(toolToggle.disabled).toBe(false);
    expect(toolToggle.classList.contains('is-unavailable')).toBe(true);

    // The extensions link navigates to the Extensions settings panel.
    const openExtensions = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.textContent.trim() === 'Open Extensions');
    expect(openExtensions).toBeTruthy();
    openExtensions.click();
    flushSync();
    expect(navigateMock).toHaveBeenCalledWith('extensions');
  });

  it('confirms before disabling a custom prompt that has customizations', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), custom_system_prompt_enabled: true }],
        scopes: [
          { type: 'default', label: 'Default' },
          {
            type: 'agent',
            agent_id: 'alpha',
            label: 'Alpha',
            has_customizations: true,
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('Custom system prompt');

    const toggle = getButtonByAriaLabel('Custom system prompt');
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    toggle.click();
    flushSync();
    await flushAsyncUpdates();

    // The confirm dialog appears; the toggle has NOT flipped yet.
    const dialog = getDialog('Disable custom system prompt?');
    expect(dialog).toBeTruthy();
    expect(
      getButtonByAriaLabel('Custom system prompt').getAttribute('aria-checked'),
    ).toBe('true');

    // Confirming applies the change.
    const confirmButton = Array.from(dialog.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Disable custom prompt',
    );
    expect(confirmButton).toBeTruthy();
    confirmButton.click();
    flushSync();
    await flushAsyncUpdates();

    expect(
      getButtonByAriaLabel('Custom system prompt').getAttribute('aria-checked'),
    ).toBe('false');
  });

  it('disables a custom prompt without a dialog when it has no customizations', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), custom_system_prompt_enabled: true }],
        scopes: [
          { type: 'default', label: 'Default' },
          {
            type: 'agent',
            agent_id: 'alpha',
            label: 'Alpha',
            has_customizations: false,
          },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('Custom system prompt');

    getButtonByAriaLabel('Custom system prompt').click();
    flushSync();
    await flushAsyncUpdates();

    // No dialog, and the toggle flipped straight to off.
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    expect(
      getButtonByAriaLabel('Custom system prompt').getAttribute('aria-checked'),
    ).toBe('false');
  });
});

function memoryScopeNamed(name) {
  const scope = Array.from(
    document.body.querySelectorAll('.agents-view__memory-scope'),
  ).find(
    (candidate) => candidate.querySelector('h3')?.textContent.trim() === name,
  );
  expect(scope).toBeTruthy();
  return scope;
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

function getButtonWithin(container, label) {
  const button = Array.from(container.querySelectorAll('button')).find(
    (candidate) => candidate.textContent.trim() === label,
  );
  expect(button).toBeTruthy();
  return button;
}
