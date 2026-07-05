// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const addProjectMock = vi.fn();
const listProjectsMock = vi.fn();
const showProjectMock = vi.fn();
const setProjectMock = vi.fn();
const removeProjectMock = vi.fn();
const setPinMock = vi.fn();
const clearPinMock = vi.fn();
const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  addProject: (...args) => addProjectMock(...args),
  listProjects: (...args) => listProjectsMock(...args),
  showProject: (...args) => showProjectMock(...args),
  setProject: (...args) => setProjectMock(...args),
  removeProject: (...args) => removeProjectMock(...args),
  setPin: (...args) => setPinMock(...args),
  clearPin: (...args) => clearPinMock(...args),
  rpc: (...args) => rpcMock(...args),
}));

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
    setPinMock.mockReset();
    clearPinMock.mockReset();
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
    setPinMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    clearPinMock.mockResolvedValue({
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

  it('shows the master-detail empty prompt until a project is selected', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    // The list renders but the detail pane starts on its empty prompt.
    expect(document.body.textContent).toContain('Select a project to view');
    expect(
      document.querySelector('[data-testid="project-panel-demo"]'),
    ).toBeFalsy();

    // Selecting the list row opens the detail pane with the ordered sections.
    await selectDemo();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-demo"]'),
    );
    expectSectionOrder([
      'Project settings',
      'Auto-load files',
      'Team',
      'Tools',
      'Skills',
    ]);
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
    buttonByTestId('project-add-open').click();
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
      document.body.textContent.includes('No agents discovered'),
    );
    expect(document.body.textContent).toContain('No agents discovered');
    expect(document.body.textContent).not.toContain('issues found');
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
      document.body.textContent.includes(
        'Inherited: openai/gpt-5.2 (global default)',
      ),
    );
    // The temperature inherit hint reflects the present global default.
    expect(document.body.textContent).toContain(
      'Inherited: 0.7 (global default)',
    );
  });

  it('shows the not-configured inherit label when no global default exists', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();
    await waitForCondition(() => document.getElementById('project-edit-model'));
    // No global model default → "Inherit (not configured)" on the model select.
    await waitForCondition(() =>
      document.body.textContent.includes('Inherit (not configured)'),
    );
    // No global temperature default → the provider-default hint.
    expect(document.body.textContent).toContain(
      'Provider default — nothing is set here',
    );
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

    expect(document.body.textContent).toContain('Currently unavailable');
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

  it('re-scans the selected project on refresh to pick up disk changes', async () => {
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

    await selectDemo();
    await waitForCondition(() => showProjectMock.mock.calls.length === 1);

    buttonByTestId('projects-refresh').click();

    await waitForCondition(() => showProjectMock.mock.calls.length === 2);
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

  // ── Team rows: effective values, source badges, pins ─────────────────────

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
              model: { value: 'openai/gpt-mini', source: 'pin' },
              temperature: { value: 0.2, source: 'agent' },
              thinking_effort: { value: 'high', source: 'project_default' },
            },
            pins: { model: 'openai/gpt-mini' },
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
    await waitForCondition(() =>
      document.body.textContent.includes('from pin'),
    );

    // Every source badge renders with its value.
    expect(document.body.textContent).toContain('from pin');
    expect(document.body.textContent).toContain('from agent file (repo)');
    expect(document.body.textContent).toContain('from project default');
    expect(document.body.textContent).toContain('openai/gpt-mini');
    // The source line names the file and the format.
    expect(document.body.textContent).toContain(
      'Source: .opencode/agents/builder.md (opencode)',
    );
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
    await waitForCondition(() =>
      document.body.textContent.includes('from global default'),
    );

    // A null model reads "not configured"; a null thinking reads "provider
    // default"; neither shows a source badge.
    expect(document.body.textContent).toContain('not configured');
    expect(document.body.textContent).toContain('provider default');
    expect(document.body.textContent).toContain('from global default');
  });

  it('sets a model pin through project.set_pin and refreshes from the scan', async () => {
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
    setPinMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: {
        team: [
          member({
            agent_id: 'builder',
            display_name: 'Builder',
            pins: { model: 'openai/gpt-5.2' },
            effective: {
              model: { value: 'openai/gpt-5.2', source: 'pin' },
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
      document.querySelector('[data-testid="project-pin-set-model-builder"]'),
    );
    buttonByTestId('project-pin-set-model-builder').click();

    await waitForCondition(() => setPinMock.mock.calls.length === 1);
    expect(setPinMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'model',
      'openai/gpt-5.2',
    );
    // After the pin, the model row reads "from pin" and a Clear pin appears.
    await waitForCondition(() =>
      document.body.textContent.includes('from pin'),
    );
    expect(
      document.querySelector('[data-testid="project-pin-clear-model-builder"]'),
    ).toBeTruthy();
  });

  it('sets a temperature pin with the comma-tolerant value', async () => {
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

    await waitForCondition(() => inputById('project-pin-temperature-builder'));
    setInputValue('project-pin-temperature-builder', '0,3');
    buttonByTestId('project-pin-set-temperature-builder').click();

    await waitForCondition(() => setPinMock.mock.calls.length === 1);
    expect(setPinMock).toHaveBeenCalledWith(
      'demo',
      'builder',
      'temperature',
      0.3,
    );
  });

  it('clears a pin through project.clear_pin', async () => {
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
            pins: { model: 'openai/gpt-mini' },
            effective: {
              model: { value: 'openai/gpt-mini', source: 'pin' },
              temperature: { value: null, source: null },
              thinking_effort: { value: null, source: null },
            },
          }),
        ],
        report: { clean: true, findings: [] },
      },
    });
    clearPinMock.mockResolvedValue({
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
      document.querySelector('[data-testid="project-pin-clear-model-builder"]'),
    );
    buttonByTestId('project-pin-clear-model-builder').click();

    await waitForCondition(() => clearPinMock.mock.calls.length === 1);
    expect(clearPinMock).toHaveBeenCalledWith('demo', 'builder', 'model');
    // The refreshed scan drops the pin — the Clear-pin control disappears.
    await waitForCondition(
      () =>
        !document.querySelector(
          '[data-testid="project-pin-clear-model-builder"]',
        ),
    );
  });

  it('surfaces a sticky error toast when a pin cannot be saved', async () => {
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
    setPinMock.mockRejectedValue({ message: 'model not usable' });

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
      document.querySelector('[data-testid="project-pin-set-model-builder"]'),
    );
    buttonByTestId('project-pin-set-model-builder').click();

    await waitForCondition(() => setPinMock.mock.calls.length === 1);
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

  it('gates the thinking-effort pin options by the member effective model', async () => {
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
      document.getElementById('project-pin-thinking-builder'),
    );
    document.getElementById('project-pin-thinking-builder').click();
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
          }),
          member({ agent_id: 'open', display_name: 'Open', denied_tools: [] }),
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
    await waitForCondition(() =>
      document.body.textContent.includes('Denied by the agent file'),
    );

    expect(document.body.textContent).toContain(
      'Denied by the agent file: bash, process',
    );
    expect(document.body.textContent).toContain(
      'All other tools follow the project tool whitelist.',
    );
    expect(document.body.textContent).toContain(
      'No tool denials — follows the project tool whitelist.',
    );
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

  it('shows a dedicated message when removal is blocked by an active run', async () => {
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
    await waitForCondition(() =>
      document.body.textContent.includes('active or queued run'),
    );
    expect(document.body.textContent).toContain('active or queued run');
  });

  it('shows a dedicated message when a cron job blocks removal', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    removeProjectMock.mockRejectedValue({
      code: 'project_in_use',
      message: 'in use',
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
    await waitForCondition(() =>
      document.body.textContent.includes('cron job'),
    );
    expect(document.body.textContent).toContain('cron job');
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

// A scan team member with the step-1/2 payload shape (effective + pins).
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
    pins: null,
    effective: {
      model: { value: null, source: null },
      temperature: { value: null, source: null },
      thinking_effort: { value: null, source: null },
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

// The ordered detail-section titles must appear in the given sequence.
function expectSectionOrder(titles) {
  const rendered = Array.from(
    document.querySelectorAll('.detail-section-title'),
  ).map((node) => node.textContent.trim());
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
        tools: toolNames.map((name) => ({ name, description: '' })),
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
