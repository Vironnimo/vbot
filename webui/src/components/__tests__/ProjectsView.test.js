// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';
import { rpcBackedApiMock } from './apiMock.js';

const addProjectMock = vi.fn();
const listProjectsMock = vi.fn();
const showProjectMock = vi.fn();
const setProjectMock = vi.fn();
const removeProjectMock = vi.fn();
const setOverrideMock = vi.fn();
const clearOverrideMock = vi.fn();
const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    addProject: (...args) => addProjectMock(...args),
    listProjects: (...args) => listProjectsMock(...args),
    showProject: (...args) => showProjectMock(...args),
    setProject: (...args) => setProjectMock(...args),
    removeProject: (...args) => removeProjectMock(...args),
    setOverride: (...args) => setOverrideMock(...args),
    clearOverride: (...args) => clearOverrideMock(...args),
  }),
);

const { default: ProjectsView } = await import('../ProjectsView.svelte');

// Just above the component's 800ms auto-save debounce, so the timer has fired
// by the time the test inspects the mock.
const AUTO_SAVE_WAIT_MS = 900;

describe('ProjectsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;

    addProjectMock.mockReset();
    listProjectsMock.mockReset();
    showProjectMock.mockReset();
    setProjectMock.mockReset();
    removeProjectMock.mockReset();
    setOverrideMock.mockReset();
    clearOverrideMock.mockReset();
    rpcMock.mockReset();

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
      return Promise.resolve({});
    });

    listProjectsMock.mockResolvedValue({ projects: [] });
    addProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    setProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    removeProjectMock.mockResolvedValue({ project_id: 'demo', archived: true });
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    clearOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('automatically opens the first project when no selection is remembered', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-demo"]'),
    );
    expect(
      document.querySelector('[data-testid="project-panel-demo"]'),
    ).toBeTruthy();
    expectSectionOrder([
      'Project settings',
      'Auto-load files',
      'Team',
      'Tools',
      'Skills',
    ]);
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

    mountedComponent = mount(ProjectsView, {
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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
    // A non-clean report surfaces its findings at the top of the Team section.
    expect(document.body.textContent).toContain('model not configured');
  });

  it('omits the display name from the add payload only when it is blank', async () => {
    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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
        { name: 'bash', family: 'execution' },
        { name: 'status', family: null },
      ],
      ['read'],
    );

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, {
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

  it('seeds the temperature field and thinking-effort dropdown from the project', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          default_temperature: 0.4,
          default_thinking_effort: 'high',
        }),
      ],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-temperature'));
    expect(inputById('project-edit-temperature').value).toBe('0.4');
    const trigger = document.getElementById('project-edit-thinking-effort');
    expect(trigger).toBeTruthy();
    expect(trigger.textContent).toContain('high');
  });

  it('saves a changed default temperature through project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-temperature'));
    setInputValue('project-edit-temperature', '0.2');

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_temperature: 0.2,
    });
  });

  it('saves a changed default thinking effort through project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() =>
      document.getElementById('project-edit-thinking-effort'),
    );
    document.getElementById('project-edit-thinking-effort').click();
    flushSync();
    await waitForCondition(() => optionByText('low'));
    optionByText('low').click();
    flushSync();

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_thinking_effort: 'low',
    });
  });

  it('adds and removes auto-load files through the list and saves them', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          auto_load: ['AGENTS.md'],
        }),
      ],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-auto-load'));
    setInputValue('project-edit-auto-load', 'docs/guide.md');
    buttonByTestId('project-auto-load-add').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-auto-load-remove-1"]'),
    );
    buttonByTestId('project-auto-load-remove-0').click();
    flushSync();

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      auto_load: ['docs/guide.md'],
    });
  });

  it('auto-saves edited fields after the debounce without a Save click', async () => {
    listProjectsMock
      .mockResolvedValueOnce({
        projects: [project({ project_id: 'demo', display_name: 'Demo' })],
      })
      .mockResolvedValue({
        projects: [project({ project_id: 'demo', display_name: 'Renamed' })],
      });
    setProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', display_name: 'Renamed' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-name'));
    setInputValue('project-edit-name', 'Renamed');

    await wait(AUTO_SAVE_WAIT_MS);
    flushSync();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);

    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
    });
  });

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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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
            { id: 'openai/gpt-mini', name: 'GPT-mini', capabilities: {} },
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
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            overrides: { model: 'openai/gpt-5.2' },
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'override' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-team-toggle-builder"]'),
    );
    buttonByTestId('project-team-toggle-builder').click();
    flushSync();

    // The draft is seeded from the effective model (openai/gpt-5.2), so Set is
    // enabled without picking anything.
    await waitForCondition(() =>
      document.querySelector(
        '[data-testid="project-override-set-model-builder"]',
      ),
    );
    buttonByTestId('project-override-set-model-builder').click();

    await waitForCondition(() => setOverrideMock.mock.calls.length === 1);
    expect(setOverrideMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'model',
      'openai/gpt-5.2',
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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
    buttonByTestId('project-override-set-temperature-builder').click();

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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, {
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

    await waitForCondition(() =>
      document.querySelector(
        '[data-testid="project-override-set-model-builder"]',
      ),
    );
    buttonByTestId('project-override-set-model-builder').click();

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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

    mountedComponent = mount(ProjectsView, { target: document.body });
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

  it('re-points a project with a missing cwd through project.set with the new cwd', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          cwd_exists: false,
        }),
      ],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-repoint-demo"]'),
    );
    buttonByTestId('project-repoint-demo').click();
    flushSync();

    await waitForCondition(() =>
      document.getElementById('projects-repoint-cwd'),
    );
    setInputValue('projects-repoint-cwd', 'C:/repos/moved');

    submitButtonInDialog('Re-point').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      cwd: 'C:/repos/moved',
    });
  });

  it('surfaces a blocked removal as an alert', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    removeProjectMock.mockRejectedValue({
      code: 'project_busy',
      message: 'busy',
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-remove-demo"]'),
    );
    buttonByTestId('project-remove-demo').click();
    flushSync();

    confirmDialog('Remove');

    await waitForCondition(() => removeProjectMock.mock.calls.length === 1);
    expect(removeProjectMock).toHaveBeenCalledWith('demo', false);
    await waitForCondition(() => document.querySelector('[role="alert"]'));
    expect(document.querySelector('[role="alert"]')).toBeTruthy();
  });

  it('sends one aggregate identity-file copy choice when removing a project', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    removeProjectMock.mockResolvedValue({
      project_id: 'demo',
      archived: true,
      affected_agent_ids: ['alpha', 'beta'],
    });
    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    buttonByTestId('project-remove-demo').click();
    flushSync();

    toggleByAriaLabel(
      'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
    ).click();
    flushSync();
    confirmDialog('Remove');

    await waitForCondition(() => removeProjectMock.mock.calls.length === 1);
    expect(removeProjectMock).toHaveBeenCalledWith('demo', true);
    await waitForCondition(() =>
      document.querySelector('.project-list-state[role="status"]'),
    );
  });

  it('reloads the model catalog when modelsRefreshToken changes', async () => {
    const props = reactiveProps({ modelsRefreshToken: 0 });
    mountedComponent = mount(ProjectsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'model.list'),
    );

    const modelListBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'model.list',
    ).length;
    const connectionListBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'connection.list',
    ).length;

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter((call) => call[0] === 'model.list').length >
        modelListBefore,
    );

    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'connection.list').length,
    ).toBeGreaterThan(connectionListBefore);
  });

  it('reloads Project management state when projectsRefreshToken changes', async () => {
    const props = reactiveProps({ projectsRefreshToken: 0 });
    mountedComponent = mount(ProjectsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() => listProjectsMock.mock.calls.length === 1);

    listProjectsMock.mockResolvedValue({
      projects: [
        project({ project_id: 'external', display_name: 'External project' }),
      ],
    });
    props.projectsRefreshToken = 1;
    flushSync();

    await waitForCondition(() => listProjectsMock.mock.calls.length === 2);
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-external"]'),
    );
    expect(
      document.querySelector('[data-testid="project-panel-external"]'),
    ).toBeTruthy();
  });
});

function project(overrides = {}) {
  return {
    project_id: 'project-default',
    display_name: 'Default Project',
    cwd: 'C:/repos/default',
    cwd_exists: true,
    default_agent: '',
    default_model: '',
    auto_load: [],
    created_at: '2026-06-18T00:00:00Z',
    updated_at: '2026-06-18T00:00:00Z',
    ...overrides,
  };
}

// A scan team member with the step-1/2 payload shape (effective + overrides).
function member(overrides = {}) {
  return {
    agent_id: 'agent',
    display_name: 'Agent',
    description: '',
    model: '',
    temperature: null,
    thinking_effort: null,
    source_format: 'opencode',
    source_path: '.opencode/agents/agent.md',
    denied_tools: [],
    tools: {},
    overrides: null,
    effective: {
      model: { value: null, source: null },
      temperature: { value: null, source: null },
      thinking_effort: { value: null, source: null },
      tool_access: { value: { mode: 'all' }, source: 'agent' },
    },
    ...overrides,
  };
}

function buttonByTestId(testId) {
  const button = document.querySelector(`[data-testid="${testId}"]`);
  expect(button, testId).toBeTruthy();
  return button;
}

function confirmDialog(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

function submitButtonInDialog(label) {
  const dialog = document.querySelector('[role="dialog"]');
  expect(dialog, 'open dialog').toBeTruthy();
  const button = Array.from(dialog.querySelectorAll('button')).find(
    (item) =>
      item.getAttribute('type') === 'submit' &&
      item.textContent?.includes(label) &&
      !item.disabled,
  );
  expect(button, `submit button "${label}" in dialog`).toBeTruthy();
  return button;
}

function inputById(id) {
  return document.getElementById(id);
}

function optionByText(text) {
  return Array.from(document.querySelectorAll('[role="option"]')).find(
    (item) => item.textContent?.trim() === text,
  );
}

// The ordered detail-section titles must appear in the given sequence. The
// InfoHint "?" dot inside a title is presentation, not part of the title text.
function expectSectionOrder(titles) {
  const rendered = Array.from(
    document.querySelectorAll('.detail-section-title'),
  ).map((node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('.info-hint').forEach((dot) => dot.remove());
    clone
      .querySelectorAll('.projects-section-refresh')
      .forEach((button) => button.remove());
    return clone.textContent.trim();
  });
  expect(rendered).toEqual(titles);
}

function setInputValue(id, value) {
  const input = document.getElementById(id);
  expect(input, `input #${id}`).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForCondition(condition, maxAttempts = 20) {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (condition()) {
      return;
    }
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  throw new Error('Timed out waiting for condition');
}

// Stub the tool-catalog RPC for the whitelist editor while keeping the model/
// connection/defaults catalogs the settings form needs.
function mockToolCatalog(toolNames, defaultProjectTools) {
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
        tools: toolNames.map((tool) =>
          typeof tool === 'string'
            ? { name: tool, description: '' }
            : { description: '', ...tool },
        ),
        default_project_tools: defaultProjectTools,
      });
    }
    return Promise.resolve({});
  });
}

// Select the `demo` project in the list pane, opening its detail pane.
async function selectDemo() {
  await waitForCondition(() =>
    document.querySelector('[data-testid="project-toggle-demo"]'),
  );
  buttonByTestId('project-toggle-demo').click();
  flushSync();
}

function toggleByAriaLabel(label) {
  return document.querySelector(`button[aria-label="${label}"]`);
}
