// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';

import {
  flushSync,
  createStandaloneNavigation,
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
    const navigation = createStandaloneNavigation();
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

    view.mount({ navigation, selectedProjectId: 'beta', onProjectSelected });

    // The empty place shows the remembered Project and names it.
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-beta"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('beta');
    expect(navigation.place).toEqual(['beta']);

    buttonByTestId('project-toggle-alpha').click();
    flushSync();
    expect(navigation.place).toEqual(['alpha']);
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-alpha"]'),
    );
    expect(onProjectSelected).toHaveBeenLastCalledWith('alpha');

    // Back to a Project that is no longer listed keeps the shown one and
    // corrects the entry.
    navigation.navigate(['removed']);
    flushSync();
    expect(navigation.place).toEqual(['alpha']);
    expect(
      document.querySelector('[data-testid="project-panel-alpha"]'),
    ).toBeTruthy();

    navigation.navigate(['beta']);
    flushSync();
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-beta"]'),
    );
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

    const navigation = createStandaloneNavigation();
    view.mount({ navigation });

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
    // Showing the added Project is a step to its place.
    expect(navigation.place).toEqual(['demo']);
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

    // The row's details card says why the Project needs attention and gives
    // the complete path and id.
    buttonByTestId('project-toggle-demo').focus();
    await vi.waitFor(() =>
      expect(document.getElementById('app-tooltip')?.dataset.floatingOpen).toBe(
        'true',
      ),
    );
    const card = document.getElementById('app-tooltip');
    expect(card.querySelector('.app-tooltip__title').textContent).toBe('Demo');
    expect(card.querySelector('.app-tooltip__text').textContent).toBe(
      t('projects.rePoint.description'),
    );
    const rows = Object.fromEntries(
      [...card.querySelectorAll('dt')].map((term) => [
        term.textContent,
        term.nextElementSibling.textContent,
      ]),
    );
    expect(rows[t('projects.details.repository')]).toBe('C:/repos/default');
    expect(rows[t('projects.details.id')]).toBe('demo');
    expect(rows[t('projects.details.added')]).toContain(' · ');
    expect(card.dataset.floatingSide).toBe('right');

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

  it('offers Copy path, Re-point when needed and Remove in a row context menu', async () => {
    const onToast = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const alpha = project({
      project_id: 'alpha',
      display_name: 'Alpha',
      cwd: 'C:/repos/alpha',
    });
    const beta = project({
      project_id: 'beta',
      display_name: 'Beta',
      cwd: 'C:/repos/beta',
      cwd_exists: false,
    });
    listProjectsMock.mockResolvedValue({ projects: [alpha, beta] });
    showProjectMock.mockImplementation((projectId) =>
      Promise.resolve({
        project: projectId === 'beta' ? beta : alpha,
        scan: cleanScan(),
      }),
    );
    const navigation = createStandaloneNavigation(['alpha']);
    view.mount({ navigation, onToast });
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-alpha"]'),
    );

    const openRowMenu = (projectId) => {
      const event = new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 40,
        clientY: 80,
      });
      buttonByTestId(`project-toggle-${projectId}`).dispatchEvent(event);
      flushSync();
      expect(event.defaultPrevented).toBe(true);
      return [...document.querySelectorAll('[role="menuitem"]')];
    };
    const labels = (items) => items.map((item) => item.textContent.trim());

    expect(labels(openRowMenu('alpha'))).toEqual(['Copy path', 'Remove…']);
    document.body.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();

    let items = openRowMenu('beta');
    expect(labels(items)).toEqual(['Copy path', 'Re-point…', 'Remove…']);
    items[0].click();
    await waitForCondition(() => onToast.mock.calls.length === 1);
    expect(writeText).toHaveBeenCalledWith('C:/repos/beta');

    openRowMenu('beta')[1].click();
    await waitForCondition(() => inputById('projects-repoint-cwd'));
    setInputValue('projects-repoint-cwd', 'C:/repos/moved');
    submitButtonInDialog('Re-point').click();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('beta', {
      cwd: 'C:/repos/moved',
    });

    openRowMenu('beta')[2].click();
    flushSync();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Beta',
    );
    confirmDialog('Remove');
    await waitForCondition(() => removeProjectMock.mock.calls.length === 1);
    expect(removeProjectMock).toHaveBeenCalledWith('beta', false);
    // Removing another Project keeps the shown one.
    expect(navigation.place).toEqual(['alpha']);
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

  it('offers Save only for unsaved edits and saves only the changed fields', async () => {
    serveProject({
      default_agent: 'builder',
      default_model: 'openai/gpt-5.2',
      auto_load: ['AGENTS.md'],
    });
    view.mount();
    await selectDemo();
    await waitForCondition(() => inputById('project-edit-name'));
    expect(
      document.querySelector('[data-testid="project-save-demo"]'),
    ).toBeNull();

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

    // An own value's reset control names the value it returns to.
    setInputValue('project-edit-temperature', '0.3');
    await waitForCondition(() =>
      document.querySelector('[aria-label="Reset to inherited value"]'),
    );
    document.querySelector('[aria-label="Reset to inherited value"]').focus();
    await vi.waitFor(() =>
      expect(document.getElementById('app-tooltip')?.textContent).toBe(
        t('inherit.resetToValue', { value: '0.7' }),
      ),
    );
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

  it('moves auto-load files with Alt+Arrow keys, keeps focus, respects boundaries and auto-saves', async () => {
    const original = serveProject({
      auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'],
    });
    const expected = ['docs/guide.md', 'AGENTS.md', 'NOTES.md'];
    setProjectMock.mockImplementation(async () => {
      const saved = { ...original, auto_load: expected };
      listProjectsMock.mockResolvedValue({ projects: [saved] });
      return { project: saved, scan: cleanScan() };
    });
    view.mount();
    await selectDemo();
    const row = (file) =>
      document.querySelector(
        `.projects-file-list [data-sortable-key="${file}"]`,
      );
    const press = (file, key) => {
      row(file).focus();
      row(file).dispatchEvent(
        new KeyboardEvent('keydown', {
          key,
          altKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      flushSync();
    };
    press('AGENTS.md', 'ArrowUp');
    press('NOTES.md', 'ArrowDown');
    expect(autoLoadNames()).toEqual(['AGENTS.md', 'docs/guide.md', 'NOTES.md']);

    press('AGENTS.md', 'ArrowDown');
    await waitForCondition(() => document.activeElement === row('AGENTS.md'));
    expect(autoLoadNames()).toEqual(expected);
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      auto_load: expected,
    });
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
    flushSync();
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
    flushSync();
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
    flushSync();
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
    expect(
      document.querySelectorAll('button[aria-label^="Toggle skill "]'),
    ).toHaveLength(3);
    // Descriptions are never inline; the row tooltip carries them.
    expect(document.body.textContent).not.toContain('Debug the repo.');
    document.body.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
    );
    toggleByAriaLabel('Toggle skill debugging').focus();
    expect(
      document.querySelector('#app-tooltip .app-tooltip__text').textContent,
    ).toBe('Debug the repo.');

    toggleByAriaLabel('Toggle skill debugging').click();
    toggleByAriaLabel('Toggle skill deploy').click();
    flushSync();
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
