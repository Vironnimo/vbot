import { afterEach, describe, expect, it, vi } from 'vitest';

import { createProjectsController } from '../projectsView.js';

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
    });
    const controller = createProjectsController({
      operations: projectOperations,
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
    expect(await newerLoad).toMatchObject({
      stale: false,
      projects: [{ project_id: 'new-project' }],
    });

    older.resolve({ projects: [] });
    expect(await olderLoad).toMatchObject({ stale: true, projects: [] });
  });

  it('debounces path detection and normalizes the winning result', async () => {
    vi.useFakeTimers();
    const detectProject = vi.fn().mockResolvedValue({
      formats: { opencode: { agents: 2, skills: 1 } },
    });
    const controller = createProjectsController({
      operations: operations({ detectProject }),
      detectDelayMs: 20,
    });
    const onResult = vi.fn();

    controller.scheduleDetect('C:/old', onResult);
    controller.scheduleDetect('C:/new', onResult);
    await vi.advanceTimersByTimeAsync(20);

    expect(detectProject).toHaveBeenCalledOnce();
    expect(detectProject).toHaveBeenCalledWith('C:/new');
    expect(onResult).toHaveBeenCalledWith(expect.any(Object), 'C:/new');
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

  it('exposes overrides and re-pointing without leaking transport details', async () => {
    const setOverride = vi.fn().mockResolvedValue({ scan: {} });
    const clearOverride = vi.fn().mockResolvedValue({ scan: {} });
    const setProject = vi.fn().mockResolvedValue({ project: {} });
    const controller = createProjectsController({
      operations: operations({ clearOverride, setOverride, setProject }),
    });

    await controller.setOverride('project-one', 'builder', 'model', 'gpt');
    await controller.clearOverride('project-one', 'builder', 'model');
    await controller.repointProject('project-one', ' C:/repo ');

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
