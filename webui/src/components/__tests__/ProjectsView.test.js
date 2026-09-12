// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  addProjectMock,
  listProjectsMock,
  showProjectMock,
  setProjectMock,
  rpcMock,
  ProjectsView,
  project,
  member,
  buttonByTestId,
  buttonWithTextContent,
  submitButtonInDialog,
  inputById,
  expectSectionOrder,
  setInputValue,
  waitForCondition,
  mockToolCatalog,
  selectDemo,
  toggleByAriaLabel,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

describe('ProjectsView', () => {
  const suite = setupProjectsViewSuite();

  it('automatically opens the first project when no selection is remembered', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-demo"]'),
    );
    expect(
      document.querySelector('[data-testid="project-panel-demo"]'),
    ).toBeTruthy();
    expectSectionOrder([
      'Project settings',
      'Team',
      'Auto-load files',
      'Tools',
      'Skills',
    ]);
  });

  it('opens Team and Context directly without losing the Project form', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo' })],
    });
    setProjectMock.mockImplementation(async (_id, changes) => {
      const saved = project({ project_id: 'demo', ...changes });
      listProjectsMock.mockResolvedValue({ projects: [saved] });
      return {
        project: saved,
        scan: { team: [], report: { clean: true, findings: [] } },
      };
    });
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await waitForCondition(() => document.querySelector('#project-edit-name'));
    const name = document.querySelector('#project-edit-name');
    name.value = 'Draft project';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    for (const topic of ['team', 'context', 'access', 'overview']) {
      document.querySelector(`#project-detail-tab-${topic}`).click();
      flushSync();
      await waitForCondition(
        () => !document.querySelector(`#project-detail-panel-${topic}`).hidden,
      );
      const visible = Array.from(
        document.querySelectorAll('.management-topic'),
      ).filter((panel) => !panel.hidden);
      expect(visible.map((panel) => panel.id)).toEqual([
        `project-detail-panel-${topic}`,
      ]);
    }
    expect(document.querySelector('#project-edit-name')).toBe(name);
    expect(name.value).toBe('Draft project');
  });

  it('opens the remembered project and reports later list selections', async () => {
    const onProjectSelected = vi.fn();
    listProjectsMock.mockResolvedValue({
      projects: [
        project({ project_id: 'alpha', display_name: 'Alpha' }),
        project({ project_id: 'beta', display_name: 'Beta' }),
      ],
    });
    showProjectMock.mockImplementation((projectId) =>
      Promise.resolve({
        project: project({ project_id: projectId }),
        scan: { team: [], report: { clean: true, findings: [] } },
      }),
    );

    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props: { selectedProjectId: 'beta', onProjectSelected },
    });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-beta"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('beta');

    document.querySelector('[data-testid="project-toggle-alpha"]').click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-alpha"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('alpha');
    expect(
      document
        .querySelector('[data-testid="project-toggle-alpha"]')
        .classList.contains('secondary-list__item'),
    ).toBe(true);
    expect(
      document
        .querySelector('.project-list-scroll')
        .classList.contains('secondary-list'),
    ).toBe(true);
  });

  it('adds a project from the modal and reviews its team and report', async () => {
    listProjectsMock.mockResolvedValueOnce({ projects: [] }).mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    addProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', display_name: 'Demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            model: 'openai/gpt-5.2',
          }),
        ],
        report: {
          clean: false,
          findings: [
            {
              type: 'bad_model',
              detail: 'model not configured',
              agent_id: 'builder',
            },
          ],
        },
      },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-add-open"]'),
    );
    const addButton = buttonByTestId('project-add-open');
    expect(addButton.querySelector('svg')).toBeTruthy();
    addButton.click();
    flushSync();

    await waitForCondition(() => inputById('projects-add-cwd'));
    setInputValue('projects-add-cwd', 'C:/repos/demo');

    submitButtonInDialog('Add project').click();

    await waitForCondition(() => addProjectMock.mock.calls.length === 1);
    expect(addProjectMock).toHaveBeenCalledWith({ cwd: 'C:/repos/demo' });

    await waitForCondition(() => document.body.textContent.includes('Builder'));
    expect(document.body.textContent).toContain('Builder');
    // A non-clean report surfaces a collapsed summary at the top of the Team
    // section — the findings themselves stay hidden until expanded.
    expect(document.body.textContent).toContain('1 issues found');
    expect(document.body.textContent).not.toContain('model not configured');
    buttonWithTextContent('Show details').click();
    flushSync();
    expect(document.body.textContent).toContain('model not configured');
  });

  it('omits the display name from the add payload only when it is blank', async () => {
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-add-open"]'),
    );
    buttonByTestId('project-add-open').click();
    flushSync();

    await waitForCondition(() => inputById('projects-add-cwd'));
    setInputValue('projects-add-cwd', 'C:/repos/demo');
    setInputValue('projects-add-display-name', 'My Repo');

    submitButtonInDialog('Add project').click();

    await waitForCondition(() => addProjectMock.mock.calls.length === 1);
    expect(addProjectMock).toHaveBeenCalledWith({
      cwd: 'C:/repos/demo',
      display_name: 'My Repo',
    });
  });

  it('treats a clean empty repo as healthy, not an error', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() =>
      document
        .querySelectorAll('.detail-section')[2]
        ?.querySelector('.empty-state'),
    );
    expect(document.querySelector('.projects-team')).toBeNull();
    expect(document.querySelector('[role="alert"]')).toBeFalsy();
  });

  it('confirms a manual Save even when the Project is already saved', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    const onToast = vi.fn();
    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props: { onToast },
    });
    flushSync();
    await selectDemo();
    await waitForCondition(() => inputById('project-edit-name'));
    buttonByTestId('project-save-demo').click();
    await waitForCondition(() => onToast.mock.calls.length > 0);
    expect(onToast).toHaveBeenCalledWith({
      title: 'Already saved',
      variant: 'success',
    });
    expect(setProjectMock).not.toHaveBeenCalled();
  });

  it('saves only the changed fields through a sparse project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          default_agent: 'builder',
          default_model: 'openai/gpt-5.2',
          auto_load: ['AGENTS.md'],
        }),
      ],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-name'));
    setInputValue('project-edit-name', 'Renamed');

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
    });
  });

  it('labels the project default inherit options from the global defaults', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
      }
      if (method === 'settings.get') {
        return Promise.resolve({
          defaults: { agent: { model: 'openai/gpt-5.2', temperature: 0.7 } },
        });
      }
      return Promise.resolve({});
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() => document.getElementById('project-edit-model'));
    await waitForCondition(() =>
      document
        .getElementById('project-edit-model')
        .textContent.includes('openai/gpt-5.2'),
    );
    expect(
      document.querySelector('.projects-inherit-hint').textContent,
    ).toContain('0.7');
  });

  it('toggles a tool into the whitelist and persists it via project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          allowed_tools: ['read'],
        }),
      ],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', allowed_tools: ['read'] }),
      scan: { team: [], report: { clean: true, findings: [] }, skills: {} },
    });
    mockToolCatalog(['read', 'edit'], ['read']);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle tool edit'));
    toggleByAriaLabel('Toggle tool edit').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      allowed_tools: ['read', 'edit'],
    });
  });

  it('groups the Project Tool Whitelist by real registry families', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', allowed_tools: ['read'] })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', allowed_tools: ['read'] }),
      scan: { team: [], report: { clean: true, findings: [] }, skills: {} },
    });
    mockToolCatalog(
      [
        { name: 'read', family: 'files' },
        { name: 'edit', family: 'files' },
        { name: 'bash', family: 'execution' },
        { name: 'process', family: 'execution' },
        { name: 'status', family: null },
      ],
      ['read'],
    );

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle tool read'));

    const headings = Array.from(
      document.querySelectorAll('.access-chips__group-title'),
    ).map((heading) => heading.textContent.trim());
    expect(headings).toEqual(['Execution', 'Files', 'Individual Tools']);
  });

  it('shows a persisted unavailable tool and lets the user remove it', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          allowed_tools: ['read', 'disabled_extension_tool'],
        }),
      ],
    });
    showProjectMock.mockResolvedValue({
      project: project({
        project_id: 'demo',
        allowed_tools: ['read', 'disabled_extension_tool'],
      }),
      scan: {
        team: [],
        report: {
          clean: false,
          findings: [
            {
              type: 'unavailable_tool',
              detail: 'The Extension tool is not currently registered.',
            },
          ],
        },
        skills: {},
      },
    });
    mockToolCatalog(['read'], ['read']);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      toggleByAriaLabel('Toggle tool disabled_extension_tool'),
    );
    const unavailableToggle = toggleByAriaLabel(
      'Toggle tool disabled_extension_tool',
    );
    expect(unavailableToggle.classList.contains('is-attention')).toBe(true);

    unavailableToggle.click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      allowed_tools: ['read'],
    });
  });

  it('renders a not-ready tool greyed with the shared notice and extensions link', async () => {
    const navigateMock = vi.fn();
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          allowed_tools: ['read'],
        }),
      ],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', allowed_tools: ['read'] }),
      scan: { team: [], report: { clean: true, findings: [] }, skills: {} },
    });
    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
      }
      if (method === 'settings.get') {
        return Promise.resolve({ defaults: { agent: {} } });
      }
      if (method === 'tool.list') {
        return Promise.resolve({
          tools: [
            { name: 'read', description: '', ready: true },
            {
              name: 'home_assistant',
              description: '',
              ready: false,
              readiness_hint: 'Set the Home Assistant token first.',
              extension: 'homeassistant',
            },
          ],
          default_project_tools: ['read'],
        });
      }
      return Promise.resolve({});
    });

    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props: { onNavigateToSettingsPanel: navigateMock },
    });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      toggleByAriaLabel('Toggle tool home_assistant'),
    );

    expect(document.body.textContent).toContain(
      'Set the Home Assistant token first.',
    );
    expect(toggleByAriaLabel('Toggle tool home_assistant').disabled).toBe(
      false,
    );

    const openExtensions = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Open Extensions',
    );
    expect(openExtensions).toBeTruthy();
    openExtensions.click();
    flushSync();
    expect(navigateMock).toHaveBeenCalledWith('extensions');
  });

  it('resets the tool whitelist to the base list', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          allowed_tools: ['read', 'edit', 'grep'],
        }),
      ],
    });
    showProjectMock.mockResolvedValue({
      project: project({
        project_id: 'demo',
        allowed_tools: ['read', 'edit', 'grep'],
      }),
      scan: { team: [], report: { clean: true, findings: [] }, skills: {} },
    });
    mockToolCatalog(['read', 'edit', 'grep'], ['read']);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-tools-reset"]'),
    );
    buttonByTestId('project-tools-reset').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      allowed_tools: ['read'],
    });
  });

  it('shows project skills on by default and persists an off-exception', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [],
        report: { clean: true, findings: [] },
        skills: { project: ['debugging'], bundled: ['pdf'] },
      },
    });
    mockToolCatalog([], []);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle skill debugging'));
    expect(
      toggleByAriaLabel('Toggle skill debugging').getAttribute('aria-checked'),
    ).toBe('true');
    expect(
      toggleByAriaLabel('Toggle skill pdf').getAttribute('aria-checked'),
    ).toBe('false');

    toggleByAriaLabel('Toggle skill debugging').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      skills_project_disabled: ['debugging'],
    });
  });

  it('shows global skills off by default and persists an opt-in', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [],
        report: { clean: true, findings: [] },
        skills: { project: [], bundled: [], global: ['deploy'] },
      },
    });
    mockToolCatalog([], []);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle skill deploy'));
    expect(
      toggleByAriaLabel('Toggle skill deploy').getAttribute('aria-checked'),
    ).toBe('false');

    toggleByAriaLabel('Toggle skill deploy').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      skills_global_enabled: ['deploy'],
    });
  });

  it('re-scans Team and Skills through the single repository action', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] }, skills: {} },
    });
    mockToolCatalog([], []);

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() => showProjectMock.mock.calls.length === 1);
    await waitForCondition(
      () => !buttonByTestId('project-repository-rescan').disabled,
    );

    buttonByTestId('project-repository-rescan').click();

    await waitForCondition(() => showProjectMock.mock.calls.length === 2);
    expect(
      document.querySelector('[data-testid="project-team-refresh"]'),
    ).toBeNull();
    expect(
      document.querySelector('[data-testid="project-skills-refresh"]'),
    ).toBeNull();
  });
});
