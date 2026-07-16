import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createProjectsController,
  createProjectsState,
} from '../projectsView.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

function operations(overrides = {}) {
  return {
    addProject: vi.fn(),
    clearOverride: vi.fn(),
    detectProject: vi.fn(),
    getSettings: vi.fn(),
    listConnections: vi.fn(),
    listModels: vi.fn(),
    listProjects: vi.fn(),
    listTools: vi.fn(),
    removeProject: vi.fn(),
    setOverride: vi.fn(),
    setProject: vi.fn(),
    showProject: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('Projects controller', () => {
  it('rejects an older Projects list response after a newer load wins', async () => {
    const older = deferred();
    const newer = deferred();
    const projectOperations = operations({
      listProjects: vi
        .fn()
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise),
      showProject: vi.fn().mockResolvedValue({ scan: null }),
    });
    const state = createProjectsState();
    const controller = createProjectsController({
      operations: projectOperations,
      state,
    });

    const olderLoad = controller.loadProjects();
    const newerLoad = controller.loadProjects();
    newer.resolve({
      projects: [
        {
          project_id: 'new-project',
          display_name: 'New project',
          cwd: 'C:/new',
        },
      ],
    });
    expect(await newerLoad).toBe(true);
    expect(state.projects).toMatchObject([{ project_id: 'new-project' }]);
    expect(state.selectedProjectId).toBe('new-project');

    older.resolve({ projects: [] });
    expect(await olderLoad).toBe(false);
    expect(state.projects).toMatchObject([{ project_id: 'new-project' }]);
  });

  it('debounces path detection and normalizes the winning result', async () => {
    vi.useFakeTimers();
    const detectProject = vi.fn().mockResolvedValue({
      formats: { opencode: { agents: 2, skills: 1 } },
    });
    const controller = createProjectsController({
      operations: operations({ detectProject }),
      state: createProjectsState(),
      detectDelayMs: 20,
    });
    controller.state.isAddOpen = true;

    controller.updateAddField('cwd', 'C:/old');
    controller.updateAddField('cwd', 'C:/new');
    await vi.advanceTimersByTimeAsync(20);

    expect(detectProject).toHaveBeenCalledOnce();
    expect(detectProject).toHaveBeenCalledWith('C:/new');
    expect(controller.state.addDetect).toMatchObject({
      formats: { opencode: { agents: 2, skills: 1 } },
    });
  });

  it('owns auto-save timing and cancels pending work when destroyed', async () => {
    vi.useFakeTimers();
    const controller = createProjectsController({
      operations: operations(),
      autoSaveDelayMs: 20,
    });
    const firstSave = vi.fn();
    const secondSave = vi.fn();

    controller.scheduleAutoSave(firstSave);
    controller.scheduleAutoSave(secondSave);
    await vi.advanceTimersByTimeAsync(20);
    expect(firstSave).not.toHaveBeenCalled();
    expect(secondSave).toHaveBeenCalledOnce();

    controller.scheduleAutoSave(secondSave);
    controller.destroy();
    await vi.advanceTimersByTimeAsync(20);
    expect(secondSave).toHaveBeenCalledOnce();
  });

  it('owns overrides and re-pointing without leaking transport details', async () => {
    const setOverride = vi.fn().mockResolvedValue({ scan: {} });
    const clearOverride = vi.fn().mockResolvedValue({ scan: {} });
    const setProject = vi.fn().mockResolvedValue({
      project: {
        project_id: 'project-one',
        display_name: 'Project one',
        cwd: 'C:/repo',
      },
      scan: {},
    });
    const state = createProjectsState({ selectedProjectId: 'project-one' });
    state.projects = [
      {
        project_id: 'project-one',
        display_name: 'Project one',
        cwd: 'C:/old',
      },
    ];
    const controller = createProjectsController({
      operations: operations({
        clearOverride,
        listProjects: vi.fn().mockResolvedValue({ projects: state.projects }),
        setOverride,
        setProject,
        showProject: vi.fn().mockResolvedValue({ scan: {} }),
      }),
      state,
    });

    controller.updateOverrideDraft('builder', 'model', 'gpt');
    await controller.setMemberOverride('builder', 'model');
    await controller.clearMemberOverride('builder', 'model');
    controller.openRePoint(state.projects[0]);
    state.rePointCwd = ' C:/repo ';
    await controller.submitRePoint();

    expect(setOverride).toHaveBeenCalledWith(
      'project-one',
      'builder',
      'model',
      'gpt',
    );
    expect(clearOverride).toHaveBeenCalledWith(
      'project-one',
      'builder',
      'model',
    );
    expect(setProject).toHaveBeenCalledWith('project-one', { cwd: 'C:/repo' });
  });
});
