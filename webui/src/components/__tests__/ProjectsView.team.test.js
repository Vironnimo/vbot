// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';

import {
  flushSync,
  setOverrideMock,
  clearOverrideMock,
  AUTO_SAVE_WAIT_MS,
  project,
  member,
  cleanScan,
  serveProject,
  mockCatalogs,
  buttonByTestId,
  buttonWithTextContent,
  inputById,
  optionByText,
  optionLabels,
  setInputValue,
  wait,
  waitForCondition,
  selectDemo,
  expandMember,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

function effective(model, temperature, thinkingEffort, topP) {
  return {
    model: model ?? { value: null, source: null },
    temperature: temperature ?? { value: null, source: null },
    top_p: topP ?? { value: null, source: null },
    thinking_effort: thinkingEffort ?? { value: null, source: null },
  };
}

function memberDetail(agentId) {
  return document.querySelector(
    `[data-testid="project-team-member-${agentId}"] .projects-team-detail`,
  );
}

describe('ProjectsView Team', () => {
  const view = setupProjectsViewSuite();

  it('shows effective values with their provenance and unset values as muted', async () => {
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            description: 'Builds things',
            source_format: 'opencode',
            source_path: '.opencode/agents/builder.md',
            effective: effective(
              { value: 'openai/gpt-mini', source: 'override' },
              { value: 0.2, source: 'agent' },
              { value: 'high', source: 'project_default' },
              { value: 0.9, source: 'override' },
            ),
            overrides: { model: 'openai/gpt-mini', top_p: 0.9 },
          }),
          member({
            agent_id: 'planner',
            display_name: 'Planner',
            effective: effective(null, {
              value: 0.5,
              source: 'global_default',
            }),
          }),
        ],
      },
    );
    view.mount();
    await selectDemo();

    // Collapsed rows show the effective Model; the description is hover help.
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    const header = buttonByTestId('project-team-toggle-builder');
    expect(header.textContent).not.toContain('Builds things');
    expect(header.textContent).toContain('openai/gpt-mini');
    expect(header.getAttribute('aria-expanded')).toBe('false');
    header.focus();
    await vi.waitFor(() => {
      expect(document.querySelector('[role="tooltip"]').textContent).toContain(
        'Builds things',
      );
    });
    // The card adds the address, every effective value with its source, and
    // the defining file.
    const card = document.getElementById('app-tooltip');
    expect(card.querySelector('.app-tooltip__title').textContent).toBe(
      'Builder',
    );
    const rows = Object.fromEntries(
      [...card.querySelectorAll('dt')].map((term) => [
        term.textContent,
        term.nextElementSibling.textContent,
      ]),
    );
    expect(rows[t('projects.team.address')]).toBe('builder@demo');
    expect(rows[t('projects.team.effectiveTemperature')]).toBe(
      t('projects.team.valueWithSource', {
        value: '0.2',
        source: t('projects.team.sourceAgentFile'),
      }),
    );
    expect(rows[t('projects.team.sourceFileLabel')]).toBe(
      '.opencode/agents/builder.md',
    );
    header.blur();

    await expandMember('builder');
    const builder = document.querySelector(
      '[data-testid="project-team-member-builder"]',
    );
    expect(builder.classList.contains('projects-team-member--expanded')).toBe(
      true,
    );
    expect(builder.querySelector('.projects-team-chevron--open')).toBeTruthy();
    expect(
      builder.querySelector('.projects-override-row--policy'),
    ).toBeTruthy();
    const builderDetail = memberDetail('builder');
    expect(
      builderDetail.querySelectorAll('.projects-effective-row'),
    ).toHaveLength(4);
    expect(
      builderDetail.querySelectorAll('.projects-effective-source'),
    ).toHaveLength(4);
    // The collapsed sampling block names the member's own override only.
    expect(
      builderDetail
        .querySelector('#project-override-builder-sampling-toggle')
        .textContent.trim(),
    ).toBe(
      `${t('sampling.title')} ${t('sampling.summaryTopP', { value: '0.9' })}`,
    );
    expect(builderDetail.textContent).toContain('openai/gpt-mini');
    expect(builderDetail.textContent).toContain('.opencode/agents/builder.md');
    expect(builderDetail.textContent).toContain('opencode');

    // A global default carries its source; unset values render muted.
    await expandMember('planner');
    const plannerDetail = memberDetail('planner');
    expect(
      plannerDetail.querySelectorAll('.projects-effective-row'),
    ).toHaveLength(4);
    expect(
      plannerDetail.querySelectorAll('.projects-effective-value--muted'),
    ).toHaveLength(3);
    const sources = plannerDetail.querySelectorAll(
      '.projects-effective-source',
    );
    expect(sources).toHaveLength(1);
    expect(sources[0].closest('li').textContent).toContain('0.5');
  });

  it('sets a model override through project.set_override and refreshes from the scan', async () => {
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: effective({ value: 'openai/gpt-5.2', source: 'agent' }),
          }),
        ],
      },
    );
    mockCatalogs({
      models: [
        { id: 'openai/gpt-5.2', name: 'GPT-5.2', capabilities: {} },
        {
          id: 'openai/gpt-mini',
          name: 'GPT-mini',
          provider_id: 'openai',
          capabilities: { tools: true },
          context_window: 128000,
        },
      ],
      connections: [
        { id: 'openai:api-key', provider_id: 'openai', usable: true },
      ],
    });
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: cleanScan({
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            overrides: { model: 'openai/gpt-mini::api-key' },
            effective: effective({
              value: 'openai/gpt-mini::api-key',
              source: 'override',
            }),
          }),
        ],
      }),
    });
    view.mount();
    await selectDemo();
    await expandMember('builder');

    expect(setOverrideMock).not.toHaveBeenCalled();
    inputById('project-override-model-builder').click();
    flushSync();
    const option = Array.from(
      document.querySelectorAll('[role="option"]'),
    ).find((node) => node.textContent.includes('gpt-mini'));
    expect(option).toBeTruthy();
    option.click();
    flushSync();
    await wait(AUTO_SAVE_WAIT_MS);

    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'model',
      'openai/gpt-mini::api-key',
    );
    await waitForCondition(() =>
      document.querySelector(
        '[data-testid="project-override-clear-model-builder"]',
      ),
    );
  });

  it('clears an override through project.clear_override', async () => {
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            overrides: { model: 'openai/gpt-mini' },
            effective: effective({
              value: 'openai/gpt-mini',
              source: 'override',
            }),
          }),
        ],
      },
    );
    clearOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: cleanScan({
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: effective({ value: 'openai/gpt-5.2', source: 'agent' }),
          }),
        ],
      }),
    });
    view.mount();
    await selectDemo();
    await expandMember('builder');

    setInputValue('project-override-builder-top-p', '0,9');
    buttonByTestId('project-override-clear-model-builder').click();
    await waitForCondition(() => clearOverrideMock.mock.calls.length === 1);
    expect(clearOverrideMock).toHaveBeenCalledWith('demo', 'builder', 'model');
    // The refreshed scan drops the override, and with it the Clear control.
    await waitForCondition(
      () =>
        !document.querySelector(
          '[data-testid="project-override-clear-model-builder"]',
        ),
    );
    // Another field's unsaved draft survives the clear and still saves.
    expect(inputById('project-override-builder-top-p').value).toBe('0,9');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'top_p',
      0.9,
    );
  });

  it('sets a sampling override with the comma-tolerant value', async () => {
    serveProject(
      {},
      { team: [member({ agent_id: 'builder', display_name: 'Builder' })] },
    );
    view.mount();
    await selectDemo();
    await expandMember('builder');

    setInputValue('project-override-builder-top-p', '0,9');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'top_p',
      0.9,
    );
  });

  it('surfaces a sticky error toast when an override cannot be saved', async () => {
    const onToast = vi.fn();
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: effective({ value: 'openai/gpt-5.2', source: 'agent' }),
          }),
        ],
      },
    );
    setOverrideMock.mockRejectedValue({ message: 'model not usable' });
    view.mount({ onToast });
    await selectDemo();
    await expandMember('builder');

    setInputValue('project-override-builder-temperature', '0.3');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() =>
      onToast.mock.calls.some((call) => call[0]?.variant === 'error'),
    );
    expect(onToast).toHaveBeenCalledWith({
      title: `${t('projects.team.overrideError')} model not usable`,
      variant: 'error',
      sticky: true,
    });
  });

  it('gates the thinking-effort override options by the member effective model', async () => {
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: effective({ value: 'openai/reasoner', source: 'agent' }),
          }),
        ],
      },
    );
    mockCatalogs({
      models: [
        {
          id: 'openai/reasoner',
          name: 'Reasoner',
          capabilities: {
            reasoning: { supported: true, levels: ['low', 'high'] },
          },
        },
      ],
    });
    view.mount();
    await selectDemo();
    await expandMember('builder');

    inputById('project-override-thinking-builder').click();
    flushSync();
    await waitForCondition(() => optionByText('low'));
    // The model's ladder is low/high: medium/max are gated out; the provider
    // default ('') and none always apply.
    const labels = optionLabels();
    expect(labels).toEqual(expect.arrayContaining(['low', 'high', 'none']));
    expect(labels).not.toContain('medium');
    expect(labels).not.toContain('max');
  });

  it('starts a member Compaction Policy override from the global policy', async () => {
    const globalPolicy = {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.7 },
      strategy: { type: 'continuation' },
    };
    mockCatalogs({
      settings: { defaults: { agent: {} }, compaction: globalPolicy },
    });
    serveProject(
      {},
      { team: [member({ agent_id: 'builder', display_name: 'Builder' })] },
    );
    view.mount();
    await selectDemo();
    await expandMember('builder');

    buttonWithTextContent(
      'Customize for this agent',
      document.querySelector('[data-testid="project-team-member-builder"]'),
    ).click();
    flushSync();
    const editor = document.querySelector(
      '[data-testid="project-compaction-builder-editor"]',
    );
    expect(
      editor.querySelector(
        'input[name="project-compaction-builder-strategy"]:checked',
      ).value,
    ).toBe('continuation');

    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'compaction_policy',
      globalPolicy,
    );
  });

  it('sets an exact Project Agent Tool override and resets to the repository policy', async () => {
    const toolMember = (overrides) =>
      member({
        agent_id: 'builder',
        display_name: 'Builder',
        denied_tools: ['read'],
        ...overrides,
      });
    serveProject(
      { allowed_tools: ['bash', 'read'] },
      {
        team: [
          toolMember({
            effective: {
              ...effective(),
              tool_access: {
                value: {
                  mode: 'selected',
                  allowed: ['bash'],
                  denied: ['read'],
                },
                source: 'agent',
              },
            },
          }),
        ],
      },
    );
    mockCatalogs({
      tools: ['bash', 'read'],
      defaultProjectTools: ['bash', 'read'],
    });
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo', allowed_tools: ['bash', 'read'] }),
      scan: cleanScan({
        team: [
          toolMember({
            overrides: { tool_access: { mode: 'selected', allowed: ['read'] } },
            effective: {
              ...effective(),
              tool_access: {
                value: { mode: 'selected', allowed: ['read'] },
                source: 'override',
              },
            },
          }),
        ],
      }),
    });
    view.mount();
    await selectDemo();
    await expandMember('builder');

    await waitForCondition(() =>
      document.querySelector(
        '[data-tool-name="read"][data-tool-access-toggle]',
      ),
    );
    document.querySelector('[data-tool-name="bash"]').click();
    flushSync();
    document.querySelector('[data-tool-name="read"]').click();
    flushSync();
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'tool_access',
      {
        mode: 'selected',
        allowed: ['read'],
      },
    );
    await waitForCondition(() =>
      document.body.textContent.includes(t('projects.team.toolOverrideActive')),
    );

    buttonWithTextContent('Reset to repository policy').click();
    await waitForCondition(() => clearOverrideMock.mock.calls.length === 1);
    expect(clearOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'tool_access',
    );
  });

  it('shows each member repository-owned Sub-Agent targets and denied tools', async () => {
    serveProject(
      {},
      {
        team: [
          member({
            agent_id: 'restricted',
            denied_tools: ['bash', 'process'],
            tools: { subagent: { allowed_agents: ['open'] } },
          }),
          member({
            agent_id: 'open',
            tools: {
              subagent: { allowed_agents: ['restricted', 'observer', 'solo'] },
            },
          }),
          member({
            agent_id: 'observer',
            tools: { subagent: { allowed_agents: ['observer'] } },
          }),
          member({ agent_id: 'solo', tools: {} }),
        ],
      },
    );
    view.mount();
    await selectDemo();
    for (const agentId of ['restricted', 'open', 'observer', 'solo']) {
      await expandMember(agentId);
    }

    const lines = (agentId) =>
      [...memberDetail(agentId).querySelectorAll('.projects-tools-line')].map(
        (line) => line.textContent.trim(),
      );
    // The calling Agent is implicit; the list names only the other members.
    expect(lines('restricted')).toEqual([
      t('projects.team.agentTargetsLimited', {
        agents: 'open',
      }),
      t('projects.team.deniedToolsBaseline', {
        tools: 'bash, process',
      }),
    ]);
    expect(lines('open')).toEqual([t('projects.team.agentTargetsAll')]);
    expect(lines('observer')).toEqual([t('projects.team.agentTargetsSelf')]);
    expect(lines('solo')).toEqual([t('projects.team.agentTargetsUnavailable')]);
  });
});
