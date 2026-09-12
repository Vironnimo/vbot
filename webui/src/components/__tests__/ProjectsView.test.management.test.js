// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  listProjectsMock,
  setProjectMock,
  removeProjectMock,
  rpcMock,
  ProjectsView,
  project,
  buttonByTestId,
  confirmDialog,
  submitButtonInDialog,
  setInputValue,
  waitForCondition,
  selectDemo,
  toggleByAriaLabel,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

import { reactiveProps } from './_reactiveProps.svelte.js';

describe('ProjectsView', () => {
  const suite = setupProjectsViewSuite();

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

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
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

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
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
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
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
    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props,
    });
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
    suite.mountedComponent = mount(ProjectsView, {
      target: document.body,
      props,
    });
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
