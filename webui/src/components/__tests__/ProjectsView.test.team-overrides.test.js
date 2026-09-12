// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  listProjectsMock,
  showProjectMock,
  setOverrideMock,
  clearOverrideMock,
  rpcMock,
  ProjectsView,
  AUTO_SAVE_WAIT_MS,
  project,
  member,
  buttonByTestId,
  inputById,
  optionByText,
  setInputValue,
  wait,
  waitForCondition,
  mockToolCatalog,
  selectDemo,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

describe('ProjectsView', () => {
  const suite = setupProjectsViewSuite();

  // ── Team rows: effective values, source badges, overrides ────────────────
  it('expands a team member and shows effective values with source badges', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            description: 'Builds things',
            source_format: 'opencode',
            source_path: '.opencode/agents/builder.md',
            effective: {
              model: { value: 'openai/gpt-mini', source: 'override' },
              temperature: { value: 0.2, source: 'agent' },
              thinking_effort: { value: 'high', source: 'project_default' },
            },
            overrides: { model: 'openai/gpt-mini' },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    // Description one-liner and the effective model summary render on the row.
    expect(document.body.textContent).toContain('Builds things');

    buttonByTestId('project-team-toggle-builder').click();
    flushSync();
    await waitForCondition(
      () =>
        document.querySelectorAll(
          '[data-testid="project-team-member-builder"] .projects-effective-source',
        ).length === 3,
    );

    const expandedMember = document.querySelector(
      '[data-testid="project-team-member-builder"]',
    );
    expect(
      expandedMember.classList.contains('projects-team-member--expanded'),
    ).toBe(true);
    expect(
      expandedMember.querySelector('.projects-team-chevron--open'),
    ).toBeTruthy();
    expect(
      expandedMember.querySelector('.projects-override-row--policy'),
    ).toBeTruthy();

    const builderDetail = document.querySelector(
      '[data-testid="project-team-member-builder"] .projects-team-detail',
    );
    expect(
      builderDetail.querySelectorAll('.projects-effective-row'),
    ).toHaveLength(3);
    expect(builderDetail.textContent).toContain('openai/gpt-mini');
    expect(builderDetail.textContent).toContain('.opencode/agents/builder.md');
    expect(builderDetail.textContent).toContain('opencode');
  });

  it('renders global-default source and null (not configured / provider default) values', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'planner',
            display_name: 'Planner',
            effective: {
              model: { value: null, source: null },
              temperature: { value: 0.5, source: 'global_default' },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-planner"]'),
    );
    buttonByTestId('project-team-toggle-planner').click();
    flushSync();
    await waitForCondition(
      () => document.querySelector('.projects-team-detail') !== null,
    );

    const rows = document.querySelectorAll('.projects-effective-row');
    expect(rows).toHaveLength(3);
    expect(
      document.querySelectorAll('.projects-effective-value--muted'),
    ).toHaveLength(2);
    expect(
      document.querySelectorAll('.projects-effective-source'),
    ).toHaveLength(1);
    expect(
      document.querySelector('.projects-effective-source').closest('li')
        .textContent,
    ).toContain('0.5');
  });

  it('sets a model override through project.set_override and refreshes from the scan', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'agent' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });
    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({
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
        });
      }
      if (method === 'connection.list') {
        return Promise.resolve({
          connections: [
            { id: 'openai:api-key', provider_id: 'openai', usable: true },
          ],
        });
      }
      if (method === 'settings.get') {
        return Promise.resolve({ defaults: { agent: {} } });
      }
      return Promise.resolve({});
    });
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            overrides: { model: 'openai/gpt-mini::api-key' },
            effective: {
              model: { value: 'openai/gpt-mini::api-key', source: 'override' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    expect(setOverrideMock).not.toHaveBeenCalled();
    document.getElementById('project-override-model-builder').click();
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
    expect(
      document.querySelector(
        '[data-testid="project-override-clear-model-builder"]',
      ),
    ).toBeTruthy();
  });

  it('sets an exact Project Agent Tool override and resets to the repository policy', async () => {
    const configuredProject = project({
      project_id: 'demo',
      display_name: 'Demo',
      allowed_tools: ['bash', 'read'],
    });
    listProjectsMock.mockResolvedValue({ projects: [configuredProject] });
    showProjectMock.mockResolvedValue({
      project: configuredProject,
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            denied_tools: ['read'],
            effective: {
              model: { value: null, source: null },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
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
        report: { clean: true, findings: [] },
      },
    });
    mockToolCatalog(['bash', 'read'], ['bash', 'read']);
    setOverrideMock.mockResolvedValue({
      project: configuredProject,
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            denied_tools: ['read'],
            overrides: {
              tool_access: { mode: 'selected', allowed: ['read'] },
            },
            effective: {
              model: { value: null, source: null },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
              tool_access: {
                value: { mode: 'selected', allowed: ['read'] },
                source: 'override',
              },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

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
      { mode: 'selected', allowed: ['read'] },
    );
    await waitForCondition(() =>
      document.body.textContent.includes('Override active'),
    );

    const reset = Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Reset to repository policy',
    );
    expect(reset).toBeTruthy();
    reset.click();
    await waitForCondition(() => clearOverrideMock.mock.calls.length === 1);
    expect(clearOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'tool_access',
    );
  });

  it('sets a temperature override with the comma-tolerant value', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [member({ agent_id: 'builder', display_name: 'Builder' })],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    await waitForCondition(() =>
      inputById('project-override-temperature-builder'),
    );
    setInputValue('project-override-temperature-builder', '0,3');
    await wait(AUTO_SAVE_WAIT_MS);

    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'temperature',
      0.3,
    );
  });

  it('clears an override through project.clear_override', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            overrides: { model: 'openai/gpt-mini' },
            effective: {
              model: { value: 'openai/gpt-mini', source: 'override' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });
    clearOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'agent' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector(
        '[data-testid="project-override-clear-model-builder"]',
      ),
    );
    buttonByTestId('project-override-clear-model-builder').click();

    await waitForCondition(() => clearOverrideMock.mock.calls.length === 1);
    expect(clearOverrideMock).toHaveBeenCalledWith('demo', 'builder', 'model');
    // The refreshed scan drops the override — the Clear-override control
    // disappears.
    await waitForCondition(
      () =>
        !document.querySelector(
          '[data-testid="project-override-clear-model-builder"]',
        ),
    );
  });

  it('surfaces a sticky error toast when an override cannot be saved', async () => {
    const toastMock = vi.fn();
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'agent' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });
    setOverrideMock.mockRejectedValue({ message: 'model not usable' });

    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    setInputValue('project-override-temperature-builder', '0.3');
    await wait(AUTO_SAVE_WAIT_MS);

    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        (call) => call[0]?.variant === 'error' && call[0]?.sticky === true,
      ),
    );
    const errorToast = toastMock.mock.calls
      .map((call) => call[0])
      .find((toast) => toast?.variant === 'error');
    expect(errorToast.sticky).toBe(true);
    expect(errorToast.title).toContain('could not be saved');
  });

  it('gates the thinking-effort override options by the member effective model', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            effective: {
              model: { value: 'openai/reasoner', source: 'agent' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });
    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({
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
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
      }
      if (method === 'settings.get') {
        return Promise.resolve({ defaults: { agent: {} } });
      }
      return Promise.resolve({});
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    await waitForCondition(() =>
      document.getElementById('project-override-thinking-builder'),
    );
    document.getElementById('project-override-thinking-builder').click();
    flushSync();
    await waitForCondition(() => optionByText('low'));

    // The model's ladder is low/high — medium/max are gated out; the provider
    // default ('') and none always apply.
    const optionLabels = Array.from(
      document.querySelectorAll('[role="option"]'),
    ).map((item) => item.textContent?.trim());
    expect(optionLabels).toContain('low');
    expect(optionLabels).toContain('high');
    expect(optionLabels).toContain('none');
    expect(optionLabels).not.toContain('medium');
    expect(optionLabels).not.toContain('max');
  });

  it('renders the denied-tools lines for a member with and without denials', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'restricted',
            display_name: 'Restricted',
            denied_tools: ['bash', 'process'],
            tools: { subagent: { allowed_agents: ['open'] } },
          }),
          member({
            agent_id: 'open',
            display_name: 'Open',
            denied_tools: [],
            tools: {
              subagent: { allowed_agents: ['restricted', 'observer'] },
            },
          }),
          member({
            agent_id: 'observer',
            display_name: 'Observer',
            denied_tools: [],
            tools: { subagent: { allowed_agents: [] } },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-restricted"]'),
    );
    buttonByTestId('project-team-toggle-restricted').click();
    buttonByTestId('project-team-toggle-open').click();
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.projects-team-detail').length === 2,
    );

    const restrictedDetail = document.querySelector(
      '[data-testid="project-team-member-restricted"] .projects-team-detail',
    );
    expect(restrictedDetail.textContent).toContain(
      'Repository baseline blocks',
    );
    expect(restrictedDetail.textContent).toContain(
      'A vBot Tool override replaces them',
    );
    expect(restrictedDetail.textContent).toContain('bash');
    expect(restrictedDetail.textContent).toContain('process');
    expect(restrictedDetail.textContent).toContain('open');

    const openDetail = document.querySelector(
      '[data-testid="project-team-member-open"] .projects-team-detail',
    );
    expect(openDetail.textContent).not.toContain('Repository baseline blocks');
    expect(openDetail.textContent).toContain('Tool access override');
  });
});
