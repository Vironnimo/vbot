import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  PROJECT_THINKING_EFFORT_NO_DEFAULT,
  createProjectsController,
  createProjectsState,
  normalizeScanReport,
  projectTeam,
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

// A controller whose Project list holds one stored `demo` record, loaded and
// selected the way the view does on mount.
async function loadedController(stored = {}, overrides = {}) {
  const record = {
    project_id: 'demo',
    display_name: 'Demo',
    cwd: 'C:/repos/demo',
    cwd_exists: true,
    ...stored,
  };
  const projectOperations = operations({
    listProjects: vi.fn().mockResolvedValue({ projects: [record] }),
    showProject: vi.fn().mockResolvedValue({ scan: {} }),
    setProject: vi.fn().mockResolvedValue({ project: record, scan: {} }),
    ...overrides,
  });
  const state = createProjectsState();
  const controller = createProjectsController({
    operations: projectOperations,
    state,
  });
  await controller.loadProjects();
  return { controller, operations: projectOperations, state };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('Projects controller loading', () => {
  it('normalizes listed Projects into stable records', async () => {
    const { state, controller } = await loadedController(
      {},
      {
        listProjects: vi
          .fn()
          .mockResolvedValueOnce({
            projects: [
              {
                project_id: 'demo',
                default_temperature: 0,
                default_top_p: 1,
                default_thinking_effort: '',
                source_format: 'claude',
                auto_load: ['AGENTS.md', '  '],
              },
              { project_id: 'bare', source_format: 'cursor' },
            ],
          })
          .mockResolvedValueOnce({ projects: 'not a list' }),
      },
    );

    // 0 is a real temperature and '' the explicit provider-default effort.
    expect(state.projects[0]).toMatchObject({
      project_id: 'demo',
      default_temperature: 0,
      default_top_p: 1,
      default_thinking_effort: '',
      source_format: 'claude',
      auto_load: ['AGENTS.md'],
    });
    // Absent values get stable defaults; an unknown format falls back.
    expect(state.projects[1]).toMatchObject({
      project_id: 'bare',
      display_name: '',
      cwd_exists: false,
      default_temperature: null,
      default_top_p: null,
      default_thinking_effort: null,
      source_format: 'opencode',
      auto_load: [],
      allowed_tools: [],
      skills_bundled_enabled: [],
      skills_global_enabled: [],
      skills_project_disabled: [],
    });
    // The form seeded from the normalized record starts without changes.
    expect(controller.pendingChanges()).toEqual({});

    await controller.loadProjects();
    expect(state.projects).toEqual([]);
    expect(state.selectedProjectId).toBe('');
  });

  it('degrades failed or malformed catalogs to empty lists', async () => {
    const state = createProjectsState();
    const controller = createProjectsController({
      operations: operations({
        listModels: vi.fn().mockResolvedValue({ models: [{ id: 'm' }] }),
        listConnections: vi.fn().mockResolvedValue({}),
        listTools: vi.fn().mockRejectedValue(new Error('offline')),
      }),
      state,
    });

    await controller.loadCatalogs();

    expect(state.availableModels).toEqual([{ id: 'm' }]);
    expect(state.availableConnections).toEqual([]);
    expect(state.toolCatalog).toEqual([]);
    expect(state.defaultProjectTools).toEqual([]);
  });

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

  it('reloads Projects on token changes and defers replacement while a modal is open', async () => {
    const oldProject = {
      project_id: 'project-one',
      display_name: 'Old name',
      cwd: 'C:/repo',
    };
    const refreshedProject = { ...oldProject, display_name: 'Fresh name' };
    const projectOperations = operations({
      listProjects: vi
        .fn()
        .mockResolvedValueOnce({ projects: [oldProject] })
        .mockResolvedValueOnce({ projects: [refreshedProject] }),
      showProject: vi.fn().mockResolvedValue({ scan: null }),
    });
    const state = createProjectsState();
    const controller = createProjectsController({
      operations: projectOperations,
      state,
    });

    await controller.loadProjects();
    controller.updateProjectsRefreshToken(0);
    controller.openAdd();
    await controller.updateProjectsRefreshToken(1);

    expect(projectOperations.listProjects).toHaveBeenCalledTimes(2);
    expect(state.projects[0].display_name).toBe('Old name');

    controller.closeAdd();

    expect(state.projects[0].display_name).toBe('Fresh name');
    expect(state.selectedProjectId).toBe('project-one');
  });
});

describe('Projects controller editing', () => {
  const STORED = {
    display_name: 'Demo',
    default_agent: 'builder',
    default_model: 'openai/gpt-5.2',
    default_temperature: 0.5,
    default_top_p: 0.9,
    default_thinking_effort: 'high',
    source_format: 'opencode',
    auto_load: ['AGENTS.md'],
    allowed_tools: ['read', 'edit'],
  };

  // Each case edits the form of the stored Project above (plus `stored`
  // deviations) and saves; project.set receives only the fields that changed.
  it.each([
    ['nothing for an untouched form', {}, {}, null],
    [
      'a renamed Project',
      {},
      { display_name: 'Renamed' },
      { display_name: 'Renamed' },
    ],
    [
      'null for a cleared display name (the id default)',
      {},
      { display_name: '' },
      { display_name: null },
    ],
    [
      'null for a cleared pointer',
      {},
      { default_agent: '' },
      { default_agent: null },
    ],
    [
      'a changed pointer, trimmed',
      {},
      { default_agent: '  planner  ' },
      { default_agent: 'planner' },
    ],
    [
      'the ordered auto-load list',
      {},
      { auto_load: ['README.md', 'AGENTS.md'] },
      { auto_load: ['README.md', 'AGENTS.md'] },
    ],
    [
      'a changed source format',
      {},
      { source_format: 'claude' },
      { source_format: 'claude' },
    ],
    [
      'nothing for an emptied source format, which is required',
      {},
      { source_format: '' },
      null,
    ],
    [
      'a changed temperature as a number',
      {},
      { default_temperature: '0,2' },
      { default_temperature: 0.2 },
    ],
    [
      'null for an emptied temperature',
      {},
      { default_temperature: '' },
      { default_temperature: null },
    ],
    [
      '0 as a real temperature versus a stored null',
      { default_temperature: null },
      { default_temperature: '0' },
      { default_temperature: 0 },
    ],
    [
      'a changed top_p as a number',
      {},
      { default_top_p: '0,95' },
      { default_top_p: 0.95 },
    ],
    [
      'null for an emptied top_p',
      {},
      { default_top_p: '' },
      { default_top_p: null },
    ],
    [
      'null for the no-default thinking effort',
      {},
      { default_thinking_effort: PROJECT_THINKING_EFFORT_NO_DEFAULT },
      { default_thinking_effort: null },
    ],
    [
      '"" to force the provider-default thinking effort',
      {},
      { default_thinking_effort: '' },
      { default_thinking_effort: '' },
    ],
    [
      'a changed thinking effort',
      {},
      { default_thinking_effort: 'low' },
      { default_thinking_effort: 'low' },
    ],
    [
      'nothing for reordered whitelist tools',
      {},
      { allowed_tools: ['edit', 'read'] },
      null,
    ],
    [
      'an empty Tool Whitelist as every tool off',
      {},
      { allowed_tools: [] },
      { allowed_tools: [] },
    ],
    [
      'changed Skill rules',
      {},
      {
        skills_bundled_enabled: ['pdf'],
        skills_project_disabled: ['debugging'],
      },
      {
        skills_bundled_enabled: ['pdf'],
        skills_project_disabled: ['debugging'],
      },
    ],
  ])('saves %s', async (_label, stored, edits, expected) => {
    const { controller, operations: projectOperations } =
      await loadedController({ ...STORED, ...stored });
    for (const [field, value] of Object.entries(edits)) {
      controller.updateEditField(field, value);
    }

    await expect(controller.saveSelectedProject()).resolves.toBe(true);

    if (expected === null) {
      expect(projectOperations.setProject).not.toHaveBeenCalled();
    } else {
      expect(projectOperations.setProject).toHaveBeenCalledWith(
        'demo',
        expected,
      );
    }
  });

  it('reconciles the saved Project and its scan into the list, form and Team', async () => {
    const stored = {
      project_id: 'demo',
      display_name: 'Demo',
      cwd: 'C:/repos/demo',
      source_format: 'opencode',
    };
    const saved = { ...stored, source_format: 'claude' };
    const { controller, state } = await loadedController(stored, {
      listProjects: vi
        .fn()
        .mockResolvedValueOnce({ projects: [stored] })
        .mockResolvedValue({ projects: [saved] }),
      setProject: vi.fn().mockResolvedValue({
        project: saved,
        scan: { team: [{ agent_id: 'claude-reviewer' }] },
      }),
    });

    controller.updateEditField('source_format', 'claude');
    await controller.saveSelectedProject({ manual: true });

    expect(state.projects[0].source_format).toBe('claude');
    expect(state.editForm.source_format).toBe('claude');
    expect(controller.pendingChanges()).toEqual({});
    // The Source Format decides the Team, so the saved scan replaces it.
    expect(state.activeTeam.map((entry) => entry.agent_id)).toEqual([
      'claude-reviewer',
    ]);
  });

  it('preserves edits made while a Project save is in flight', async () => {
    const firstSave = deferred();
    let persistedProject = {
      project_id: 'project-one',
      display_name: 'Project one',
      cwd: 'C:/repo',
    };
    const setProject = vi
      .fn()
      .mockImplementationOnce(async (_projectId, changes) => {
        await firstSave.promise;
        persistedProject = { ...persistedProject, ...changes };
        return { project: persistedProject, scan: {} };
      })
      .mockImplementationOnce(async (_projectId, changes) => {
        persistedProject = { ...persistedProject, ...changes };
        return { project: persistedProject, scan: {} };
      });
    const state = createProjectsState({ selectedProjectId: 'project-one' });
    state.projects = [persistedProject];
    state.editForm = createProjectsState().editForm;
    state.editForm.display_name = persistedProject.display_name;
    const controller = createProjectsController({
      operations: operations({
        listProjects: vi.fn(() => ({
          projects: [{ ...persistedProject }],
        })),
        setProject,
        showProject: vi.fn().mockResolvedValue({ scan: {} }),
      }),
      state,
    });

    controller.updateEditField('display_name', 'First draft');
    const initialSave = controller.saveSelectedProject();
    controller.updateEditField('display_name', 'Latest draft');
    firstSave.resolve();
    await expect(initialSave).resolves.toBe(true);

    expect(state.editForm.display_name).toBe('Latest draft');
    expect(controller.pendingChanges()).toEqual({
      display_name: 'Latest draft',
    });

    await expect(controller.saveSelectedProject()).resolves.toBe(true);
    expect(setProject).toHaveBeenNthCalledWith(2, 'project-one', {
      display_name: 'Latest draft',
    });
  });

  it("keeps edits made while another Project's removal is in flight", async () => {
    const removal = deferred();
    const records = {
      demo: {
        project_id: 'demo',
        display_name: 'Demo',
        cwd: 'C:/repos/demo',
        cwd_exists: true,
      },
      other: {
        project_id: 'other',
        display_name: 'Other',
        cwd: 'C:/repos/other',
        cwd_exists: true,
      },
    };
    const { controller, state } = await loadedController(
      {},
      {
        listProjects: vi.fn(async () => ({ projects: Object.values(records) })),
        removeProject: vi.fn(async (projectId) => {
          await removal.promise;
          delete records[projectId];
          return { project_id: projectId, archived: true };
        }),
      },
    );
    expect(state.selectedProjectId).toBe('demo');

    controller.openRemove(records.other);
    const removing = controller.confirmRemove();
    controller.updateEditField('display_name', 'Draft');
    removal.resolve();
    await removing;

    expect(state.projects.map((project) => project.project_id)).toEqual([
      'demo',
    ]);
    expect(state.selectedProjectId).toBe('demo');
    expect(state.editForm.display_name).toBe('Draft');
    expect(controller.pendingChanges()).toEqual({ display_name: 'Draft' });
    controller.destroy();
  });

  it('changes whitelist membership idempotently', async () => {
    const { controller, state } = await loadedController({
      allowed_tools: ['read'],
    });

    controller.updateListField('allowed_tools', 'read', true);
    controller.updateListField('allowed_tools', 'bash', false);
    controller.updateListField('allowed_tools', '  ', true);
    expect(state.editForm.allowed_tools).toEqual(['read']);

    controller.updateListField('allowed_tools', ' edit ', true);
    controller.updateListField('allowed_tools', 'read', false);
    expect(state.editForm.allowed_tools).toEqual(['edit']);
  });

  it('preserves duplicate auto-load entries and rejects invalid or busy moves', () => {
    const state = createProjectsState();
    const controller = createProjectsController({
      operations: operations(),
      state,
    });
    state.editForm.auto_load = ['AGENTS.md', 'NOTES.md', 'AGENTS.md'];
    for (const [from, to] of [
      [0, 0],
      [-1, 1],
      [0, 3],
      [3, 0],
      [null, 1],
      [0, 1.5],
    ]) {
      expect(controller.moveAutoLoadEntry(from, to)).toBe(false);
    }
    state.editSaving = true;
    expect(controller.moveAutoLoadEntry(0, 1)).toBe(false);
    state.editSaving = false;
    expect(state.editForm.auto_load).toEqual([
      'AGENTS.md',
      'NOTES.md',
      'AGENTS.md',
    ]);
    expect(controller.moveAutoLoadEntry(0, 1)).toBe(true);
    expect(state.editForm.auto_load).toEqual([
      'NOTES.md',
      'AGENTS.md',
      'AGENTS.md',
    ]);
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
});

describe('Projects controller add dialog', () => {
  async function addWith(fields, detected) {
    vi.useFakeTimers();
    const detectProject = vi.fn().mockResolvedValue(detected);
    const addProject = vi.fn().mockResolvedValue({
      project: { project_id: 'demo' },
      scan: {},
    });
    const controller = createProjectsController({
      operations: operations({
        addProject,
        detectProject,
        listProjects: vi.fn().mockResolvedValue({ projects: [] }),
        showProject: vi.fn().mockResolvedValue({ scan: {} }),
      }),
      detectDelayMs: 20,
    });
    controller.openAdd();
    for (const [field, value] of Object.entries(fields)) {
      controller.updateAddField(field, value);
    }
    await vi.advanceTimersByTimeAsync(20);
    await controller.submitAdd();
    return { addProject, controller };
  }

  const cwd = 'C:/repos/demo';

  it.each([
    [
      'trims the path and omits blank optional fields',
      { cwd: `  ${cwd}  `, display_name: '  ' },
      null,
      { cwd },
    ],
    [
      'includes a typed display name',
      { cwd, display_name: 'Demo' },
      null,
      { cwd, display_name: 'Demo' },
    ],
    [
      'sends the chosen source format when both formats were detected',
      { cwd, source_format: 'claude' },
      { formats: { opencode: { agents: 1 }, claude: { skills: 2 } } },
      { cwd, source_format: 'claude' },
    ],
    [
      'lets the server pick the format when only one was detected',
      { cwd, source_format: 'claude' },
      { formats: { claude: { agents: 1 } } },
      { cwd },
    ],
    [
      'adds a found CLAUDE.md the user opted into',
      { cwd, include_claude_md: true },
      { context_files: { agents_md: false, claude_md: 'CLAUDE.md' } },
      { cwd, auto_load: ['CLAUDE.md'] },
    ],
    [
      'never adds CLAUDE.md next to an AGENTS.md',
      { cwd, include_claude_md: true },
      { context_files: { agents_md: true, claude_md: 'CLAUDE.md' } },
      { cwd },
    ],
  ])('%s', async (_label, fields, detected, expected) => {
    const { addProject, controller } = await addWith(fields, detected);

    expect(addProject).toHaveBeenCalledWith(expected);
    expect(controller.state.isAddOpen).toBe(false);
    expect(controller.state.selectedProjectId).toBe('demo');
  });

  it('debounces path detection and normalizes the winning result', async () => {
    vi.useFakeTimers();
    const detectProject = vi.fn().mockResolvedValue({
      cwd_exists: true,
      formats: {
        opencode: { agents: 2, skills: 0 },
        claude: { agents: 0, skills: 3 },
      },
      context_files: { agents_md: true, claude_md: 'CLAUDE.md' },
    });
    const controller = createProjectsController({
      operations: operations({ detectProject }),
      detectDelayMs: 20,
    });
    controller.openAdd();

    controller.updateAddField('cwd', 'C:/old');
    controller.updateAddField('cwd', 'C:/new');
    await vi.advanceTimersByTimeAsync(20);

    expect(detectProject).toHaveBeenCalledOnce();
    expect(detectProject).toHaveBeenCalledWith('C:/new');
    // Skills alone make a format present.
    expect(controller.state.addDetect).toEqual({
      cwd_exists: true,
      formats: {
        opencode: { agents: 2, skills: 0, present: true },
        claude: { agents: 0, skills: 3, present: true },
      },
      agents_md: true,
      claude_md: 'CLAUDE.md',
    });

    // A missing or foreign response degrades to nothing found.
    detectProject.mockResolvedValue(null);
    controller.updateAddField('cwd', 'C:/elsewhere');
    await vi.advanceTimersByTimeAsync(20);
    expect(controller.state.addDetect).toEqual({
      cwd_exists: false,
      formats: {
        opencode: { agents: 0, skills: 0, present: false },
        claude: { agents: 0, skills: 0, present: false },
      },
      agents_md: false,
      claude_md: null,
    });
  });
});

describe('Projects controller Team overrides', () => {
  const scan = {
    team: [
      {
        agent_id: 'builder',
        overrides: {
          model: 'openai/gpt-mini',
          temperature: 0.3,
          top_p: 0.8,
          thinking_effort: 'low',
        },
        effective: {
          model: { value: 'openai/gpt-mini', source: 'override' },
          temperature: { value: 0.3, source: 'override' },
          top_p: { value: 0.8, source: 'override' },
          thinking_effort: { value: 'low', source: 'override' },
        },
      },
      {
        agent_id: 'planner',
        overrides: null,
        effective: {
          model: { value: 'openai/gpt-5.2', source: 'agent' },
          temperature: { value: null, source: null },
          top_p: { value: 0.95, source: 'project_default' },
          thinking_effort: { value: 'high', source: 'project_default' },
        },
      },
    ],
  };

  // Sampling drafts hold only the override itself, never an inherited value.
  it('seeds override drafts from overrides, else from the effective values', async () => {
    const { controller } = await loadedController();
    controller.selectProject('demo', scan);

    expect(controller.overrideDraft('builder')).toEqual({
      model: 'openai/gpt-mini',
      temperature: '0.3',
      top_p: '0.8',
      thinking_effort: 'low',
      compaction_policy: null,
      tool_access: { mode: 'all' },
    });
    expect(controller.overrideDraft('planner')).toEqual({
      model: 'openai/gpt-5.2',
      temperature: '',
      top_p: '',
      thinking_effort: 'high',
      compaction_policy: null,
      tool_access: { mode: 'all' },
    });
  });

  // null: the draft holds no number, so the override is refused unsent.
  it.each([
    ['temperature', '0,7', 0.7],
    ['temperature', '0', 0],
    ['top_p', '0,9', 0.9],
    ['temperature', '', null],
    ['top_p', 'abc', null],
  ])('sets the %s override typed as %j to %j', async (field, draft, sent) => {
    const setOverride = vi.fn().mockResolvedValue({ scan });
    const { controller } = await loadedController({}, { setOverride });
    controller.selectProject('demo', scan);
    controller.updateOverrideDraft('planner', field, draft);

    await expect(controller.setMemberOverride('planner', field)).resolves.toBe(
      sent !== null,
    );
    if (sent === null) {
      expect(setOverride).not.toHaveBeenCalled();
    } else {
      expect(setOverride).toHaveBeenCalledWith('demo', 'planner', field, sent);
    }
  });

  it('clears a sampling override whose box was emptied', async () => {
    const setOverride = vi.fn().mockResolvedValue({ scan });
    const clearOverride = vi.fn().mockResolvedValue({ scan });
    const { controller } = await loadedController(
      {},
      { setOverride, clearOverride },
    );
    controller.selectProject('demo', scan);
    controller.updateOverrideDraft('builder', 'temperature', '');

    await expect(controller.savePendingOverrides()).resolves.toBe(true);
    expect(clearOverride).toHaveBeenCalledWith(
      'demo',
      'builder',
      'temperature',
    );
    expect(setOverride).not.toHaveBeenCalled();
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

// Chat's Project context consumes these scan projections too, so they keep
// direct tests of their shape.
describe('Project scan projections', () => {
  it('projects the scan Team into display-ready members with overrides and provenance', () => {
    expect(
      projectTeam({
        team: [
          {
            agent_id: 'builder',
            display_name: 'Builder',
            description: 'Builds things',
            model: 'openai/gpt-5.2',
            temperature: 0.2,
            top_p: 0.9,
            thinking_effort: 'high',
            source_format: 'opencode',
            source_path: '.opencode/agents/builder.md',
            denied_tools: ['bash'],
            tools: { subagent: { allowed_agents: ['builder'] } },
            overrides: { model: 'openai/gpt-mini', top_p: 0.8, unknown: 'x' },
            effective: {
              model: { value: 'openai/gpt-mini', source: 'override' },
              temperature: { value: 0.2, source: 'agent' },
              top_p: { value: 0.8, source: 'override' },
              thinking_effort: { value: 'high', source: 'agent' },
            },
          },
          { agent_id: 'planner', overrides: { unknown: 'x' } },
        ],
      }),
    ).toEqual([
      {
        agent_id: 'builder',
        display_name: 'Builder',
        description: 'Builds things',
        model: 'openai/gpt-5.2',
        temperature: 0.2,
        top_p: 0.9,
        thinking_effort: 'high',
        source_format: 'opencode',
        source_path: '.opencode/agents/builder.md',
        denied_tools: ['bash'],
        tools: { subagent: { allowed_agents: ['builder'] } },
        // Only the known override fields survive.
        overrides: { model: 'openai/gpt-mini', top_p: 0.8 },
        effective: {
          model: { value: 'openai/gpt-mini', source: 'override' },
          temperature: { value: 0.2, source: 'agent' },
          top_p: { value: 0.8, source: 'override' },
          thinking_effort: { value: 'high', source: 'agent' },
          tool_access: { value: { mode: 'all' }, source: null },
        },
      },
      {
        agent_id: 'planner',
        display_name: 'planner',
        description: '',
        model: '',
        temperature: null,
        top_p: null,
        thinking_effort: null,
        source_format: '',
        source_path: '',
        denied_tools: [],
        tools: {},
        // An override object without known fields counts as no override.
        overrides: null,
        effective: {
          model: { value: null, source: null },
          temperature: { value: null, source: null },
          top_p: { value: null, source: null },
          thinking_effort: { value: null, source: null },
          tool_access: { value: { mode: 'all' }, source: null },
        },
      },
    ]);
    expect(projectTeam({})).toEqual([]);
    expect(projectTeam(undefined)).toEqual([]);
  });

  it('treats a missing or clean scan report as healthy', () => {
    for (const report of [undefined, { clean: true, findings: [] }]) {
      expect(normalizeScanReport(report)).toMatchObject({
        clean: true,
        findingCount: 0,
        groups: [],
      });
    }
    // Without a clean flag the findings decide.
    expect(
      normalizeScanReport({
        findings: [{ type: 'bad_model', detail: 'x' }],
      }).clean,
    ).toBe(false);
  });

  it('groups scan findings by type in the stable display order', () => {
    const report = normalizeScanReport({
      clean: false,
      findings: [
        {
          type: 'orphan',
          detail: 'orphan pointer',
          agent_id: 'ghost',
        },
        {
          type: 'slug_collision',
          detail: 'two on one id',
          agent_id: 'dup',
          source_path: 'a.md',
        },
        { type: 'bad_model', detail: 'bad model', agent_id: 'b' },
        {
          type: 'unslugifiable_name',
          detail: 'no slug',
          agent_id: '',
        },
        {
          type: 'slug_collision',
          detail: 'another collision',
          agent_id: 'dup2',
        },
        {
          type: 'unavailable_tool',
          detail: 'extension tool is unavailable',
        },
      ],
    });

    expect(report.clean).toBe(false);
    expect(report.findingCount).toBe(6);
    expect(report.groups.map((group) => group.type)).toEqual([
      'slug_collision',
      'unslugifiable_name',
      'bad_model',
      'orphan',
      'unavailable_tool',
    ]);
    expect(report.groups[0].findings).toHaveLength(2);
  });
});
