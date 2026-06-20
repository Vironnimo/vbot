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
    rpcMock.mockReset();

    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
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
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('adds a project from the modal and reviews its team and report', async () => {
    // After add the list reload returns the new project so its panel can render.
    listProjectsMock.mockResolvedValueOnce({ projects: [] }).mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    addProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', display_name: 'Demo' }),
      scan: {
        team: [
          {
            agent_id: 'builder',
            display_name: 'Builder',
            model: 'openai/gpt-5.2',
          },
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
    // A non-clean report surfaces its findings (add-then-review surface).
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

  it('expands a project and treats a clean empty repo as healthy, not an error', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    await waitForCondition(() => showProjectMock.mock.calls.length === 1);
    await waitForCondition(() =>
      document.querySelector('[data-testid="project-panel-demo"]'),
    );
    // The team section renders its empty-state copy once the scan settles; no
    // findings, no alert.
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

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    await waitForCondition(() => inputById('project-edit-name'));
    setInputValue('project-edit-name', 'Renamed');

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
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

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    // Add a file through the text input + Add button.
    await waitForCondition(() => inputById('project-edit-auto-load'));
    setInputValue('project-edit-auto-load', 'docs/guide.md');
    buttonByTestId('project-auto-load-add').click();
    flushSync();

    // The list now shows both entries; remove the seeded AGENTS.md (row 0) so only
    // the added file survives, proving per-row removal and order are preserved.
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
    // The reload after the auto-save returns the renamed project so the form
    // reads clean again and the debounce does not re-fire.
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

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    await waitForCondition(() => inputById('project-edit-name'));
    setInputValue('project-edit-name', 'Renamed');

    // No Save click: wait out the 800ms auto-save debounce, then let the request
    // settle. The edit persists on its own through the same sparse project.set.
    await wait(AUTO_SAVE_WAIT_MS);
    flushSync();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);

    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
    });
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

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

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
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-remove-demo"]'),
    );
    buttonByTestId('project-remove-demo').click();

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
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-toggle-demo"]'),
    );
    buttonByTestId('project-toggle-demo').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-remove-demo"]'),
    );
    buttonByTestId('project-remove-demo').click();

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

function buttonByTestId(testId) {
  const button = document.querySelector(`[data-testid="${testId}"]`);
  expect(button, testId).toBeTruthy();
  return button;
}

// The list row and a modal can share a label (e.g. "Add project", "Re-point"),
// so target the submit button inside the open dialog specifically.
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
