// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';

import {
  flushSync,
  addProjectMock,
  listProjectsMock,
  showProjectMock,
  setProjectMock,
  removeProjectMock,
  rpcMock,
  AUTO_SAVE_WAIT_MS,
  project,
  member,
  cleanScan,
  serveProject,
  mockCatalogs,
  buttonByTestId,
  buttonWithTextContent,
  confirmDialog,
  submitButtonInDialog,
  inputById,
  optionByText,
  optionLabels,
  expectSectionOrder,
  setInputValue,
  wait,
  waitForCondition,
  selectDemo,
  toggleByAriaLabel,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

import { reactiveProps } from './reactiveProps.support.svelte.js';

function autoLoadNames() {
  return [...document.querySelectorAll('.projects-file-name')].map(
    (node) => node.textContent,
  );
}

function rpcCalls(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method).length;
}

describe('ProjectsView list and selection', () => {
  const view = setupProjectsViewSuite();

  it('opens the first Project as one page with every section and a healthy empty Team', async () => {
    serveProject();
    view.mount();
    await waitForCondition(() => inputById('project-edit-name'));
    expect(
      document.querySelector('[data-testid="project-panel-demo"]'),
    ).toBeTruthy();
    expectSectionOrder([
      'Repository',
      'Agent defaults',
      'Team',
      'Auto-load files',
      'Tools',
      'Skills',
    ]);

    // Editing keeps the one continuous page; no topic switch replaces the form.
    const name = inputById('project-edit-name');
    setInputValue('project-edit-name', 'Draft project');
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    const visible = Array.from(
      document.querySelectorAll('.management-topic'),
    ).filter((panel) => !panel.hidden);
    expect(visible.map((panel) => panel.id)).toEqual([
      'project-detail-panel-overview',
      'project-detail-panel-team',
      'project-detail-panel-context',
      'project-detail-panel-access',
    ]);
    expect(
      buttonByTestId('project-repository-rescan').closest(
        '#project-detail-panel-team',
      ),
    ).not.toBeNull();
    expect(
      buttonByTestId('project-remove-demo').closest(
        '.projects-repository-actions',
      ),
    ).not.toBeNull();
    expect(inputById('project-edit-name')).toBe(name);
    expect(name.value).toBe('Draft project');

    // A clean scan without agents is the normal case, not an error.
    await waitForCondition(() =>
      document.querySelector('#project-detail-panel-team .empty-state'),
    );
    expect(document.querySelector('.projects-team')).toBeNull();
    expect(document.querySelector('[role="alert"]')).toBeNull();
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
        scan: cleanScan(),
      }),
    );

    view.mount({ selectedProjectId: 'beta', onProjectSelected });

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-beta"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('beta');

    buttonByTestId('project-toggle-alpha').click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-alpha"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('alpha');
    expect(
      buttonByTestId('project-toggle-alpha').classList.contains(
        'secondary-list__item',
      ),
    ).toBe(true);
    expect(
      document
        .querySelector('.project-list-scroll')
        .classList.contains('secondary-list'),
    ).toBe(true);
  });

  it('adds a project from the modal and reviews its team and report', async () => {
    listProjectsMock.mockResolvedValueOnce({ projects: [] }).mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'My Repo' })],
    });
    addProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', display_name: 'My Repo' }),
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

    view.mount();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-add-open"]'),
    );
    const addButton = buttonByTestId('project-add-open');
    expect(addButton.querySelector('svg')).toBeTruthy();
    addButton.click();
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

    await waitForCondition(() => document.body.textContent.includes('Builder'));
    // A non-clean report surfaces a collapsed summary at the top of the Team
    // section; the findings themselves stay hidden until expanded.
    expect(document.body.textContent).toContain(
      t('projects.report.findingCount', { count: 1 }),
    );
    expect(document.body.textContent).not.toContain('model not configured');
    buttonWithTextContent('Show details').click();
    flushSync();
    expect(document.body.textContent).toContain('model not configured');
  });

  it('re-points a project with a missing cwd through project.set with the new cwd', async () => {
    serveProject({ cwd_exists: false });
    view.mount();
    await selectDemo();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-repoint-demo"]'),
    );
    buttonByTestId('project-repoint-demo').click();
    flushSync();

    await waitForCondition(() => inputById('projects-repoint-cwd'));
    setInputValue('projects-repoint-cwd', 'C:/repos/moved');
    submitButtonInDialog('Re-point').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      cwd: 'C:/repos/moved',
    });
  });

  it('surfaces a blocked removal as an alert', async () => {
    serveProject();
    removeProjectMock.mockRejectedValue({
      code: 'project_busy',
      message: 'busy',
    });
    view.mount();
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
  });

  it('sends one aggregate identity-file copy choice when removing a project', async () => {
    serveProject();
    removeProjectMock.mockResolvedValue({
      project_id: 'demo',
      archived: true,
      affected_agent_ids: ['alpha', 'beta'],
    });
    view.mount();
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

  it('reloads the catalogs and the Project list when their refresh tokens change', async () => {
    const props = reactiveProps({
      modelsRefreshToken: 0,
      projectsRefreshToken: 0,
    });
    view.mount(props);
    await waitForCondition(
      () => rpcCalls('model.list') > 0 && listProjectsMock.mock.calls.length,
    );
    const modelListBefore = rpcCalls('model.list');
    const connectionListBefore = rpcCalls('connection.list');

    props.modelsRefreshToken = 1;
    flushSync();
    await waitForCondition(() => rpcCalls('model.list') > modelListBefore);
    expect(rpcCalls('connection.list')).toBeGreaterThan(connectionListBefore);

    listProjectsMock.mockResolvedValue({
      projects: [
        project({ project_id: 'external', display_name: 'External project' }),
      ],
    });
    props.projectsRefreshToken = 1;
    flushSync();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-external"]'),
    );
    expect(listProjectsMock).toHaveBeenCalledTimes(2);
  });
});

describe('ProjectsView Project settings', () => {
  const view = setupProjectsViewSuite();

  it('confirms an unchanged manual Save and then saves only the changed fields', async () => {
    serveProject({
      default_agent: 'builder',
      default_model: 'openai/gpt-5.2',
      auto_load: ['AGENTS.md'],
    });
    const onToast = vi.fn();
    view.mount({ onToast });
    await selectDemo();
    await waitForCondition(() => inputById('project-edit-name'));

    buttonByTestId('project-save-demo').click();
    await waitForCondition(() => onToast.mock.calls.length > 0);
    expect(onToast).toHaveBeenCalledWith({
      title: t('common.alreadySaved'),
      variant: 'success',
    });
    expect(setProjectMock).not.toHaveBeenCalled();

    setInputValue('project-edit-name', 'Renamed');
    buttonByTestId('project-save-demo').click();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
    });
  });

  it('seeds the Agent defaults from the project and saves changed values', async () => {
    serveProject({ default_temperature: 0.4, default_thinking_effort: 'high' });
    view.mount();
    await selectDemo();

    await waitForCondition(() => inputById('project-edit-temperature'));
    expect(inputById('project-edit-temperature').value).toBe('0.4');
    const effort = inputById('project-edit-thinking-effort');
    expect(effort.textContent).toContain('high');

    setInputValue('project-edit-temperature', '0.2');
    effort.click();
    flushSync();
    await waitForCondition(() => optionByText('low'));
    optionByText('low').click();
    flushSync();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_temperature: 0.2,
      default_thinking_effort: 'low',
    });
  });

  it('offers the Team as default agents, keeps a stored agent outside it, and clears the default', async () => {
    serveProject(
      { default_agent: 'ghost' },
      {
        team: [
          member({ agent_id: 'builder', display_name: 'Builder' }),
          // Without a display name the id is the label, shown once.
          member({ agent_id: 'planner', display_name: '' }),
        ],
      },
    );
    view.mount();
    await selectDemo();
    await waitForCondition(
      () =>
        !inputById('project-edit-agent')?.disabled &&
        document.querySelector('.projects-team'),
    );

    inputById('project-edit-agent').click();
    flushSync();
    await waitForCondition(() => optionLabels().length > 0);
    const options = optionLabels();
    expect(options).toHaveLength(4);
    expect(options[1]).toContain('Builder');
    expect(options[1]).toContain('builder');
    expect(options[2]).toBe('planner');
    expect(options[3]).toContain('ghost');

    document.querySelectorAll('[role="option"]')[0].click();
    flushSync();
    buttonByTestId('project-save-demo').click();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_agent: null,
    });
  });

  it('labels the project default inherit options from the global defaults', async () => {
    serveProject();
    mockCatalogs({
      settings: {
        defaults: { agent: { model: 'openai/gpt-5.2', temperature: 0.7 } },
      },
    });
    view.mount();
    await selectDemo();

    await waitForCondition(() =>
      inputById('project-edit-model')?.textContent.includes('openai/gpt-5.2'),
    );
    expect(
      document.querySelector('.projects-inherit-hint').textContent,
    ).toContain('0.7');
  });

  it('auto-saves each Project edit after the debounce without a Save click', async () => {
    const records = {
      demo: project({ project_id: 'demo', display_name: 'Demo' }),
      other: project({ project_id: 'other', display_name: 'Other' }),
    };
    listProjectsMock.mockImplementation(async () => ({
      projects: Object.values(records),
    }));
    showProjectMock.mockImplementation(async (id) => ({
      project: records[id],
      scan: cleanScan(),
    }));
    setProjectMock.mockImplementation(async (id, changes) => {
      records[id] = { ...records[id], ...changes };
      return { project: records[id], scan: cleanScan() };
    });
    view.mount();
    await selectDemo();
    await waitForCondition(
      () => inputById('project-edit-name')?.value === 'Demo',
    );
    setInputValue('project-edit-name', 'Shared name');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Shared name',
    });

    // The same delta on another Project is its own change, not a no-op.
    buttonByTestId('project-toggle-other').click();
    await waitForCondition(
      () => inputById('project-edit-name')?.value === 'Other',
    );
    setInputValue('project-edit-name', 'Shared name');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setProjectMock.mock.calls.length === 2);
    expect(setProjectMock).toHaveBeenLastCalledWith('other', {
      display_name: 'Shared name',
    });
  });
});

describe('ProjectsView auto-load files', () => {
  const view = setupProjectsViewSuite();

  it('adds and removes auto-load files through the list and saves them', async () => {
    serveProject({ auto_load: ['AGENTS.md'] });
    view.mount();
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

  it.each([
    [0, 2, ['docs/guide.md', 'NOTES.md', 'AGENTS.md']],
    [2, 0, ['NOTES.md', 'AGENTS.md', 'docs/guide.md']],
  ])(
    'drags auto-load file %i to %i and auto-saves its order',
    async (from, to, expected) => {
      const original = serveProject({
        auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'],
      });
      const saved = { ...original, auto_load: expected };
      setProjectMock.mockImplementation(async () => {
        listProjectsMock.mockResolvedValue({ projects: [saved] });
        return { project: saved, scan: cleanScan() };
      });
      view.mount();
      await selectDemo();
      const handle = document.querySelector(
        `[data-auto-load-handle="${from}"]`,
      );
      const row = document.querySelectorAll('.projects-file-row')[to];
      const dataTransfer = {
        setData: vi.fn(),
        effectAllowed: '',
        dropEffect: '',
      };
      const start = new Event('dragstart', { bubbles: true });
      Object.defineProperty(start, 'dataTransfer', { value: dataTransfer });
      handle.dispatchEvent(start);
      const over = new Event('dragover', { bubbles: true, cancelable: true });
      row.dispatchEvent(over);
      flushSync();
      expect(over.defaultPrevented).toBe(true);
      expect(row.classList.contains('projects-file-row--drop')).toBe(true);
      expect(dataTransfer.effectAllowed).toBe('move');
      row.dispatchEvent(new Event('drop', { bubbles: true, cancelable: true }));
      flushSync();
      expect(autoLoadNames()).toEqual(expected);
      expect(document.querySelector('.projects-file-row--drop')).toBeNull();
      await wait(AUTO_SAVE_WAIT_MS);
      await waitForCondition(() => setProjectMock.mock.calls.length === 1);
      expect(setProjectMock).toHaveBeenCalledWith('demo', {
        auto_load: expected,
      });
      flushSync();
      expect(autoLoadNames()).toEqual(expected);
    },
  );

  it('reorders auto-load files by keyboard, retains focus, and respects list boundaries', async () => {
    serveProject({ auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'] });
    view.mount();
    await selectDemo();
    const handle = (index) =>
      document.querySelector(`[data-auto-load-handle="${index}"]`);
    const press = (index, key) => {
      handle(index).focus();
      handle(index).dispatchEvent(
        new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }),
      );
      flushSync();
    };
    press(0, 'ArrowUp');
    press(2, 'ArrowDown');
    expect(setProjectMock).not.toHaveBeenCalled();
    press(0, 'ArrowDown');
    await waitForCondition(() => document.activeElement === handle(1));
    expect(autoLoadNames()).toEqual(['docs/guide.md', 'AGENTS.md', 'NOTES.md']);
    expect(
      document.querySelector('[aria-live="polite"]').textContent,
    ).toContain('AGENTS.md');
    press(1, 'ArrowUp');
    await waitForCondition(() => document.activeElement === handle(0));
    expect(autoLoadNames()).toEqual(['AGENTS.md', 'docs/guide.md', 'NOTES.md']);
  });

  it('ignores external, canceled, same-row, and stale auto-load drops', async () => {
    serveProject({ auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'] });
    view.mount();
    await selectDemo();
    const handle = document.querySelector('[data-auto-load-handle="0"]');
    const drop = (index) => {
      const row = document.querySelectorAll('.projects-file-row')[index];
      row.dispatchEvent(new Event('drop', { bubbles: true, cancelable: true }));
      flushSync();
    };
    drop(2);
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    handle.dispatchEvent(new Event('dragend', { bubbles: true }));
    drop(2);
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    drop(0);
    expect(autoLoadNames()).toEqual(['AGENTS.md', 'docs/guide.md', 'NOTES.md']);
    expect(setProjectMock).not.toHaveBeenCalled();
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    buttonByTestId('project-auto-load-remove-1').click();
    flushSync();
    drop(1);
    expect(autoLoadNames()).toEqual(['AGENTS.md', 'NOTES.md']);
  });
});

describe('ProjectsView Tool and Skill whitelists', () => {
  const view = setupProjectsViewSuite();

  it('groups the Tool Whitelist by registry family and omits tools Projects cannot configure', async () => {
    serveProject({ allowed_tools: ['read', 'bash'] }, { skills: {} });
    mockCatalogs({
      tools: [
        { name: 'read', family: 'files' },
        { name: 'edit', family: 'files' },
        { name: 'bash', family: 'execution' },
        { name: 'process', family: 'execution' },
        ...['ha_get_state', 'ha_call_service'].map((name) => ({
          name,
          family: 'extension:homeassistant:home_assistant',
          family_label: 'Home Assistant',
        })),
        { name: 'status', family: null },
        { name: 'memory', family: null, project_configurable: false },
      ],
      defaultProjectTools: ['read'],
    });
    view.mount();
    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle tool read'));

    const headings = Array.from(
      document.querySelectorAll(
        '#project-detail-panel-access .s-check-group__title',
      ),
    ).map((heading) => heading.textContent.trim());
    expect(headings).toEqual([
      'Files',
      'Execution',
      'Home Assistant',
      'Individual Tools',
    ]);
    // Project configurability is server-owned metadata, not a name list.
    expect(toggleByAriaLabel('Toggle tool memory')).toBeNull();

    const search = document.querySelector(
      '#project-detail-panel-access input[type="search"]',
    );
    search.value = 'edit';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    document.querySelector('[aria-label="All Files Tools"]').click();
    buttonByTestId('project-save-demo').click();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      allowed_tools: ['read', 'bash', 'edit'],
    });
  });

  it('keeps a persisted unavailable tool removable and toggles single tools', async () => {
    serveProject(
      { allowed_tools: ['read', 'disabled_extension_tool'] },
      {
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
    );
    mockCatalogs({ tools: ['read', 'edit'], defaultProjectTools: ['read'] });
    view.mount();
    await selectDemo();
    await waitForCondition(() =>
      toggleByAriaLabel('Toggle tool disabled_extension_tool'),
    );
    const unavailableToggle = toggleByAriaLabel(
      'Toggle tool disabled_extension_tool',
    );
    expect(
      unavailableToggle
        .closest('.tool-access-chip-wrap')
        .classList.contains('is-unavailable'),
    ).toBe(true);

    unavailableToggle.click();
    toggleByAriaLabel('Toggle tool edit').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      allowed_tools: ['read', 'edit'],
    });
  });

  it('renders a not-ready tool greyed with the shared notice and extensions link', async () => {
    const navigateMock = vi.fn();
    serveProject({ allowed_tools: ['read'] }, { skills: {} });
    mockCatalogs({
      tools: [
        { name: 'read', ready: true },
        {
          name: 'home_assistant',
          ready: false,
          readiness_hint: 'Set the Home Assistant token first.',
          extension: 'homeassistant',
        },
      ],
      defaultProjectTools: ['read'],
    });
    view.mount({ onNavigateToSettingsPanel: navigateMock });
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
    buttonWithTextContent('Open Extensions').click();
    flushSync();
    expect(navigateMock).toHaveBeenCalledWith('extensions');
  });

  it('resets the tool whitelist to the base list', async () => {
    serveProject({ allowed_tools: ['read', 'edit', 'grep'] }, { skills: {} });
    mockCatalogs({
      tools: ['read', 'edit', 'grep'],
      defaultProjectTools: ['read'],
    });
    view.mount();
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

  it('defaults project Skills on and bundled or global Skills off, and persists exceptions', async () => {
    serveProject(
      {},
      {
        skills: {
          project: [{ name: 'debugging', description: 'Debug the repo.' }, ' '],
          // A project Skill shadows a bundled Skill of the same name.
          bundled: ['pdf', 'debugging'],
          global: ['deploy'],
        },
      },
    );
    view.mount();
    await selectDemo();
    await waitForCondition(() => toggleByAriaLabel('Toggle skill debugging'));

    const checked = (name) =>
      toggleByAriaLabel(`Toggle skill ${name}`).getAttribute('aria-checked');
    expect(checked('debugging')).toBe('true');
    expect(checked('pdf')).toBe('false');
    expect(checked('deploy')).toBe('false');
    expect(
      document.querySelectorAll('button[aria-label="Toggle skill debugging"]'),
    ).toHaveLength(1);
    expect(document.querySelectorAll('.projects-skill-row')).toHaveLength(3);
    expect(document.body.textContent).toContain('Debug the repo.');

    toggleByAriaLabel('Toggle skill debugging').click();
    toggleByAriaLabel('Toggle skill deploy').click();
    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      skills_global_enabled: ['deploy'],
      skills_project_disabled: ['debugging'],
    });
  });

  it('re-scans Team and Skills through the single repository action', async () => {
    serveProject({}, { skills: {} });
    view.mount();

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
