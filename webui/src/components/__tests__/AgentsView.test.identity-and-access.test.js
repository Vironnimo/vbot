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

  it('hides Sub-Agent settings when neither Sub-Agent tool is allowed', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [
          {
            ...baseAgent(),
            allowed_tools: ['bash'],
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
    getButtonByAriaLabel('Toggle tool subagent').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(800);
    await flushAsyncUpdates();

    expect(getAgentUpdateCalls()).toHaveLength(1);
    expect(getAgentUpdateCalls()[0][1]).toEqual({
      allowed_tools: ['bash'],
      id: 'alpha',
    });
  });

  it('renders memory as a display-only first tool chip that is never a toggle', async () => {
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

    // The memory tool is shown, but never as an allow-list toggle — it renders
    // as the first, display-only "auto" chip that follows the Memory setting.
    expect(document.body.textContent).toContain('Run shell commands.');
    expect(document.body.textContent).toContain('Write files.');
    expect(document.body.textContent).toContain('Manage pinned memory.');
    expect(
      document.body.querySelector('button[aria-label="Toggle tool memory"]'),
    ).toBeNull();

    const memoryState = document.body.querySelector(
      '[data-testid="access-chip-locked-note"]',
    );
    expect(memoryState).toBeTruthy();
    // baseAgent() uses memory_prompt_mode 'agent_user' (not off) → available.
    expect(
      document
        .querySelector('.access-chip--locked')
        .classList.contains('is-on'),
    ).toBe(true);

    // Memory is the first chip in the tools cloud.
    const firstChipName = document
      .querySelector('.access-chip--locked .access-chip__name')
      .textContent.trim();
    expect(firstChipName).toBe('memory');
  });

  it('switches the memory tool row text when Memory is set to off', async () => {
    rpcMock.mockImplementation(
      createAgentsRpcMock({
        agents: [{ ...baseAgent(), memory_prompt_mode: 'off' }],
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          { name: 'memory', description: 'Manage pinned memory.' },
        ],
      }),
    );

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForText('Run shell commands.');

    const memoryState = document.body.querySelector(
      '[data-testid="access-chip-locked-note"]',
    );
    expect(memoryState).toBeTruthy();
    expect(
      document
        .querySelector('.access-chip--locked')
        .classList.contains('is-on'),
    ).toBe(false);
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

    // The toggle for a not-ready tool still fires (allow-list is independent of
    // readiness).
    const toggle = getButtonByAriaLabel('Toggle tool home_assistant');
    expect(toggle).toBeTruthy();
    expect(toggle.disabled).toBe(false);
    expect(toggle.classList.contains('is-attention')).toBe(true);

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
