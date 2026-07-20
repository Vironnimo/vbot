import { normalizeCompactionPolicy } from './compactionPolicy.js';
import { SURFACE_FORM, shouldApplyReloadNow } from './resourceInvalidation.js';
import {
  addProject as requestAddProject,
  clearOverride as requestClearOverride,
  detectProject as requestDetectProject,
  getSettings,
  listConnections,
  listModels,
  listProjects,
  listTools,
  removeProject as requestRemoveProject,
  setOverride as requestSetOverride,
  setProject as requestSetProject,
  showProject,
} from './api.js';

const PROJECT_AUTO_SAVE_DEBOUNCE_MS = 800;
const PROJECT_DETECT_DEBOUNCE_MS = 500;
const PROJECT_BUSY_CODE = 'project_busy';
const PROJECT_IN_USE_CODE = 'project_in_use';

const emptyScanSkills = () => ({ project: [], bundled: [], global: [] });

export function createProjectAddForm() {
  return {
    cwd: '',
    display_name: '',
    source_format: 'opencode',
    include_claude_md: false,
  };
}

export function createProjectEditForm(project = null) {
  return {
    display_name: project?.display_name ?? '',
    default_agent: project?.default_agent ?? '',
    default_model: project?.default_model ?? '',
    source_format: project?.source_format ?? 'opencode',
    default_temperature:
      typeof project?.default_temperature === 'number'
        ? String(project.default_temperature)
        : '',
    default_thinking_effort:
      project?.default_thinking_effort === null ||
      project?.default_thinking_effort === undefined
        ? PROJECT_THINKING_EFFORT_NO_DEFAULT
        : project.default_thinking_effort,
    auto_load: [...(project?.auto_load ?? [])],
    allowed_tools: [...(project?.allowed_tools ?? [])],
    skills_bundled_enabled: [...(project?.skills_bundled_enabled ?? [])],
    skills_global_enabled: [...(project?.skills_global_enabled ?? [])],
    skills_project_disabled: [...(project?.skills_project_disabled ?? [])],
  };
}

export function createProjectsState({ selectedProjectId = '' } = {}) {
  return {
    projects: [],
    loadingProjects: false,
    listError: '',
    statusMessage: '',
    availableModels: [],
    availableConnections: [],
    modelDropdownOpenCount: 0,
    pendingModelCatalogs: null,
    lastModelsRefreshToken: null,
    lastProjectsRefreshToken: null,
    globalAgentDefaults: {},
    globalCompactionPolicy: null,
    isAddOpen: false,
    addForm: createProjectAddForm(),
    addingProject: false,
    addError: '',
    addDetect: null,
    selectedProjectId,
    editForm: createProjectEditForm(),
    editSaving: false,
    editError: '',
    autoLoadDraft: '',
    activeTeam: [],
    activeReport: null,
    activeScanSkills: emptyScanSkills(),
    scanLoading: false,
    scanRefreshRequested: false,
    removingProjectId: '',
    removeConfirmProject: null,
    copyRootedAgentIdentityFiles: false,
    expandedMembers: {},
    overrideDrafts: {},
    overrideBusyKey: '',
    toolCatalog: [],
    defaultProjectTools: [],
    rePointProject: null,
    rePointCwd: '',
    rePointing: false,
    rePointError: '',
    showAllModels: false,
    showAllOverrideModels: {},
  };
}

function defaultProjectOperations() {
  return {
    addProject: requestAddProject,
    clearOverride: requestClearOverride,
    detectProject: requestDetectProject,
    getSettings,
    listConnections,
    listModels,
    listProjects,
    listTools,
    removeProject: requestRemoveProject,
    setOverride: requestSetOverride,
    setProject: requestSetProject,
    showProject,
  };
}

// Own Project management end-to-end. The Svelte view renders this state and
// forwards user intents; it does not know RPC ordering, stale-response rules,
// mutation reconciliation, catalog swap timing, or error ownership.
export function createProjectsController({
  state = createProjectsState(),
  operations = defaultProjectOperations(),
  autoSaveDelayMs = PROJECT_AUTO_SAVE_DEBOUNCE_MS,
  detectDelayMs = PROJECT_DETECT_DEBOUNCE_MS,
  translate = (_key, fallback, values = {}) =>
    String(fallback).replace(/\{(\w+)\}/g, (_match, key) => values[key] ?? ''),
  onProjectSelected = () => {},
  onToast = () => {},
} = {}) {
  let active = true;
  let listRequestId = 0;
  let scanRequestId = 0;
  let autoSaveTimer = null;
  let detectTimer = null;
  let pendingProjectList = null;

  function errorText(error) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }
    return translate('common.unknown', 'Unknown');
  }

  function selectedProject() {
    return (
      state.projects.find(
        (project) => project.project_id === state.selectedProjectId,
      ) ?? null
    );
  }

  function pendingChanges() {
    const project = selectedProject();
    if (!project) {
      return {};
    }
    return buildManageProjectPayload(
      {
        display_name: state.editForm.display_name,
        default_agent: state.editForm.default_agent,
        default_model: state.editForm.default_model,
        source_format: state.editForm.source_format,
        default_temperature: state.editForm.default_temperature,
        default_thinking_effort: state.editForm.default_thinking_effort,
        auto_load: state.editForm.auto_load,
        allowed_tools: state.editForm.allowed_tools,
        skills_bundled_enabled: state.editForm.skills_bundled_enabled,
        skills_global_enabled: state.editForm.skills_global_enabled,
        skills_project_disabled: state.editForm.skills_project_disabled,
      },
      project,
    );
  }

  function resetSelectionState(project = null) {
    state.editForm = createProjectEditForm(project);
    state.autoLoadDraft = '';
    state.editError = '';
    state.activeTeam = [];
    state.activeReport = null;
    state.activeScanSkills = emptyScanSkills();
    state.expandedMembers = {};
    state.overrideDrafts = {};
    state.overrideBusyKey = '';
  }

  function seedOverrideDrafts({ replace = false } = {}) {
    const next = replace ? {} : { ...state.overrideDrafts };
    for (const member of state.activeTeam) {
      if (!next[member.agent_id]) {
        next[member.agent_id] = seedTeamOverrideDraft(member);
      }
    }
    state.overrideDrafts = next;
  }

  function applyScan(scan, { replaceDrafts = false } = {}) {
    state.activeTeam = projectTeam(scan);
    state.activeReport = normalizeScanReport(scan?.report);
    state.activeScanSkills = normalizeScanSkills(scan);
    seedOverrideDrafts({ replace: replaceDrafts });
  }

  function clearSelectedProject() {
    invalidateScan();
    state.scanLoading = false;
    state.selectedProjectId = '';
    onProjectSelected('');
    resetSelectionState();
  }

  function selectProject(projectId, scan = null) {
    invalidateScan();
    const project =
      state.projects.find((item) => item.project_id === projectId) ?? null;
    state.selectedProjectId = projectId;
    onProjectSelected(projectId);
    resetSelectionState(project);
    if (scan) {
      state.scanLoading = false;
      applyScan(scan);
      return;
    }
    void loadScan(projectId);
  }

  function overrideDraftsHaveChanges() {
    return state.activeTeam.some((member) => {
      const draft = state.overrideDrafts[member.agent_id];
      if (!draft) {
        return false;
      }
      const savedDraft = seedTeamOverrideDraft(member);
      return (
        draft.model !== savedDraft.model ||
        draft.temperature !== savedDraft.temperature ||
        draft.thinking_effort !== savedDraft.thinking_effort ||
        JSON.stringify(draft.compaction_policy) !==
          JSON.stringify(savedDraft.compaction_policy)
      );
    });
  }

  function projectReloadCanApply() {
    return shouldApplyReloadNow(SURFACE_FORM, {
      dropdownOpen: state.modelDropdownOpenCount > 0,
      focused:
        state.isAddOpen ||
        Boolean(state.removeConfirmProject) ||
        Boolean(state.rePointProject) ||
        hasManageChanges(pendingChanges()) ||
        overrideDraftsHaveChanges(),
      savePending:
        autoSaveTimer !== null ||
        state.addingProject ||
        state.editSaving ||
        Boolean(state.overrideBusyKey) ||
        Boolean(state.removingProjectId) ||
        state.rePointing,
    });
  }

  function applyProjectList(projects) {
    pendingProjectList = null;
    state.projects = projects;
    const preferredProject = state.projects.find(
      (project) => project.project_id === state.selectedProjectId,
    );
    const projectToOpen = preferredProject ?? state.projects[0] ?? null;
    if (projectToOpen) {
      selectProject(projectToOpen.project_id);
    } else {
      clearSelectedProject();
    }
  }

  function flushPendingProjects() {
    if (!active || !pendingProjectList || !projectReloadCanApply()) {
      return false;
    }
    const projects = pendingProjectList;
    applyProjectList(projects);
    return true;
  }

  async function loadProjects({ reload = false } = {}) {
    const requestId = ++listRequestId;
    pendingProjectList = null;
    state.loadingProjects = true;
    state.listError = '';
    try {
      const result = await operations.listProjects();
      if (!active || requestId !== listRequestId) {
        return false;
      }
      const projects = normalizeProjects(result?.projects);
      if (reload && !projectReloadCanApply()) {
        pendingProjectList = projects;
      } else {
        applyProjectList(projects);
      }
      return true;
    } catch (error) {
      if (!active || requestId !== listRequestId) {
        return false;
      }
      state.listError = `${translate('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
      return false;
    } finally {
      if (active && requestId === listRequestId) {
        state.loadingProjects = false;
      }
    }
  }

  async function loadScan(projectId) {
    const requestId = ++scanRequestId;
    state.scanLoading = true;
    try {
      const result = await operations.showProject(projectId);
      if (!active || requestId !== scanRequestId) {
        return false;
      }
      applyScan(result?.scan ?? null);
      return true;
    } catch (error) {
      if (!active || requestId !== scanRequestId) {
        return false;
      }
      state.editError = `${translate('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
      return false;
    } finally {
      if (active && requestId === scanRequestId) {
        state.scanLoading = false;
      }
    }
  }

  function invalidateScan() {
    scanRequestId += 1;
  }

  async function loadGlobalDefaults() {
    try {
      const result = await operations.getSettings();
      if (!active) {
        return;
      }
      const defaults = result?.defaults?.agent;
      state.globalAgentDefaults =
        defaults && typeof defaults === 'object' ? defaults : {};
      state.globalCompactionPolicy = result?.compaction ?? null;
    } catch {
      if (active) {
        state.globalAgentDefaults = {};
        state.globalCompactionPolicy = null;
      }
    }
  }

  function applyModelCatalogs(catalogs) {
    state.availableModels = catalogs.models;
    state.availableConnections = catalogs.connections;
    state.pendingModelCatalogs = null;
  }

  async function fetchCatalogs() {
    const [modelsResult, connectionsResult, toolsResult] =
      await Promise.allSettled([
        operations.listModels(),
        operations.listConnections(),
        operations.listTools(),
      ]);
    if (!active) {
      return { stale: true };
    }
    return {
      stale: false,
      modelCatalogsAvailable:
        modelsResult.status === 'fulfilled' &&
        connectionsResult.status === 'fulfilled',
      models:
        modelsResult.status === 'fulfilled' &&
        Array.isArray(modelsResult.value?.models)
          ? modelsResult.value.models
          : [],
      connections:
        connectionsResult.status === 'fulfilled' &&
        Array.isArray(connectionsResult.value?.connections)
          ? connectionsResult.value.connections
          : [],
      tools:
        toolsResult.status === 'fulfilled' &&
        Array.isArray(toolsResult.value?.tools)
          ? toolsResult.value.tools
          : [],
      defaultProjectTools:
        toolsResult.status === 'fulfilled' &&
        Array.isArray(toolsResult.value?.default_project_tools)
          ? toolsResult.value.default_project_tools
          : [],
    };
  }

  async function loadCatalogs({ reload = false } = {}) {
    const catalogs = await fetchCatalogs();
    if (catalogs.stale) {
      return;
    }
    state.toolCatalog = catalogs.tools;
    state.defaultProjectTools = catalogs.defaultProjectTools;
    if (!catalogs.modelCatalogsAvailable) {
      return;
    }
    if (
      !reload ||
      shouldApplyReloadNow(SURFACE_FORM, {
        dropdownOpen: state.modelDropdownOpenCount > 0,
      })
    ) {
      applyModelCatalogs(catalogs);
    } else {
      state.pendingModelCatalogs = catalogs;
    }
  }

  function trackModelDropdownOpen(open) {
    state.modelDropdownOpenCount = Math.max(
      0,
      state.modelDropdownOpenCount + (open ? 1 : -1),
    );
    if (state.modelDropdownOpenCount === 0 && state.pendingModelCatalogs) {
      applyModelCatalogs(state.pendingModelCatalogs);
    }
    flushPendingProjects();
  }

  function updateModelsRefreshToken(token) {
    if (state.lastModelsRefreshToken === null) {
      state.lastModelsRefreshToken = token;
      return;
    }
    if (token !== state.lastModelsRefreshToken) {
      state.lastModelsRefreshToken = token;
      void loadCatalogs({ reload: true });
    }
  }

  function updateProjectsRefreshToken(token) {
    if (state.lastProjectsRefreshToken === null) {
      state.lastProjectsRefreshToken = token;
      return null;
    }
    if (token !== state.lastProjectsRefreshToken) {
      state.lastProjectsRefreshToken = token;
      return loadProjects({ reload: true });
    }
    return null;
  }

  async function initialize(preferredProjectId = '') {
    state.selectedProjectId = preferredProjectId;
    await Promise.all([loadCatalogs(), loadGlobalDefaults(), loadProjects()]);
  }

  function clearAutoSave({ flushPending = true } = {}) {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
    if (flushPending) {
      flushPendingProjects();
    }
  }

  function scheduleAutoSave(save) {
    clearAutoSave();
    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      if (active) {
        void save();
      }
    }, autoSaveDelayMs);
  }

  function clearDetect() {
    if (detectTimer !== null) {
      clearTimeout(detectTimer);
      detectTimer = null;
    }
  }

  function scheduleDetect(cwd, onResult) {
    clearDetect();
    const path = typeof cwd === 'string' ? cwd.trim() : '';
    if (!path) {
      onResult(null, path);
      return;
    }
    detectTimer = setTimeout(async () => {
      detectTimer = null;
      try {
        const result = await operations.detectProject(path);
        if (active) {
          onResult(normalizeDetectResult(result), path);
        }
      } catch {
        if (active) {
          onResult(null, path);
        }
      }
    }, detectDelayMs);
  }

  function openAdd() {
    state.addForm = createProjectAddForm();
    state.addError = '';
    state.addDetect = null;
    state.isAddOpen = true;
  }

  function closeAdd() {
    if (state.addingProject) {
      return;
    }
    state.isAddOpen = false;
    state.addError = '';
    clearDetect();
    flushPendingProjects();
  }

  function updateAddField(field, value) {
    state.addForm[field] = value;
    state.addError = '';
    if (field !== 'cwd') {
      return;
    }
    scheduleDetect(value, (result, detectedCwd) => {
      if (!state.isAddOpen || state.addForm.cwd.trim() !== detectedCwd) {
        return;
      }
      state.addDetect = result;
    });
  }

  async function submitAdd() {
    if (state.addForm.cwd.trim().length === 0) {
      state.addError = translate(
        'projects.add.missingCwd',
        'Enter a repository path to add a project.',
      );
      return;
    }
    state.addingProject = true;
    state.addError = '';
    state.statusMessage = '';
    try {
      const formats = state.addDetect ? presentFormats(state.addDetect) : [];
      const payload = buildAddProjectPayload({
        cwd: state.addForm.cwd,
        display_name: state.addForm.display_name,
        source_format: formats.length > 1 ? state.addForm.source_format : '',
        auto_load:
          state.addDetect &&
          shouldSuggestClaudeMd(state.addDetect) &&
          state.addForm.include_claude_md
            ? ['CLAUDE.md']
            : [],
      });
      const result = await operations.addProject(payload);
      if (!active) {
        return;
      }
      const project = normalizeProject(result?.project);
      state.statusMessage = translate('projects.add.success', 'Project added.');
      state.isAddOpen = false;
      state.addForm = createProjectAddForm();
      state.addDetect = null;
      await loadProjects();
      if (active) {
        selectProject(project.project_id, result?.scan);
      }
    } catch (error) {
      if (active) {
        state.addError = `${translate('projects.add.error', 'Project could not be added.')} ${errorText(error)}`;
      }
    } finally {
      if (active) {
        state.addingProject = false;
        flushPendingProjects();
      }
    }
  }

  async function refreshScan() {
    if (!state.selectedProjectId || state.scanLoading) {
      return;
    }
    state.scanRefreshRequested = true;
    await loadScan(state.selectedProjectId);
    if (active) {
      state.scanRefreshRequested = false;
    }
  }

  function updateEditField(field, value) {
    state.editForm[field] = value;
    state.editError = '';
  }

  function updateListField(field, name, enabled) {
    state.editForm[field] = setListMembership(
      state.editForm[field],
      name,
      enabled,
    );
    state.editError = '';
  }

  function replaceListField(field, values) {
    state.editForm[field] = [...values];
    state.editError = '';
  }

  async function saveSelectedProject({ manual = false } = {}) {
    const project = selectedProject();
    if (!project || state.editSaving) {
      return;
    }
    const changes = pendingChanges();
    if (!hasManageChanges(changes)) {
      if (manual) {
        onToast({
          title: translate('common.alreadySaved', 'Already saved'),
          variant: 'success',
        });
      }
      return;
    }
    clearAutoSave({ flushPending: false });
    state.editSaving = true;
    state.editError = '';
    state.statusMessage = '';
    try {
      const result = await operations.setProject(project.project_id, changes);
      if (!active) {
        return;
      }
      await loadProjects();
      if (!active) {
        return;
      }
      state.editForm = createProjectEditForm(normalizeProject(result?.project));
      applyScan(result?.scan);
      onToast({
        title: translate('projects.manage.saveSuccess', 'Project updated.'),
        variant: 'success',
      });
    } catch (error) {
      if (active) {
        state.editError = `${translate('projects.manage.saveError', 'Project changes could not be saved.')} ${errorText(error)}`;
      }
    } finally {
      if (active) {
        state.editSaving = false;
        flushPendingProjects();
      }
    }
  }

  function overrideDraft(agentId) {
    return (
      state.overrideDrafts[agentId] ?? {
        model: '',
        temperature: '',
        thinking_effort: '',
        compaction_policy: null,
      }
    );
  }

  function updateOverrideDraft(agentId, field, value) {
    state.overrideDrafts = {
      ...state.overrideDrafts,
      [agentId]: { ...overrideDraft(agentId), [field]: value },
    };
    state.editError = '';
    flushPendingProjects();
  }

  function overrideKey(agentId, field) {
    return `${agentId}:${field}`;
  }

  function isOverrideBusy(agentId, field) {
    return state.overrideBusyKey === overrideKey(agentId, field);
  }

  function overrideValueForField(agentId, field) {
    const draft = overrideDraft(agentId);
    if (field === 'model') {
      return draft.model.trim();
    }
    if (field === 'temperature') {
      return normalizeOverrideTemperature(draft.temperature);
    }
    if (field === 'compaction_policy') {
      return draft.compaction_policy;
    }
    return draft.thinking_effort;
  }

  function canSetOverride(agentId, field) {
    if (state.overrideBusyKey) {
      return false;
    }
    const draft = overrideDraft(agentId);
    if (field === 'model') {
      return typeof draft.model === 'string' && draft.model.trim().length > 0;
    }
    if (field === 'temperature') {
      return normalizeOverrideTemperature(draft.temperature) !== null;
    }
    if (field === 'compaction_policy') {
      return draft.compaction_policy !== null;
    }
    return typeof draft.thinking_effort === 'string';
  }

  async function setMemberOverride(agentId, field) {
    const project = selectedProject();
    if (!project || state.overrideBusyKey || !canSetOverride(agentId, field)) {
      return;
    }
    state.overrideBusyKey = overrideKey(agentId, field);
    state.editError = '';
    try {
      const result = await operations.setOverride(
        project.project_id,
        agentId,
        field,
        overrideValueForField(agentId, field),
      );
      if (!active) {
        return;
      }
      applyScan(result?.scan, { replaceDrafts: true });
      onToast({
        title: translate('projects.team.overrideSaved', 'Override saved.'),
        variant: 'success',
      });
    } catch (error) {
      if (active) {
        onToast({
          title: `${translate('projects.team.overrideError', 'The override could not be saved.')} ${errorText(error)}`,
          variant: 'error',
          sticky: true,
        });
      }
    } finally {
      if (active) {
        state.overrideBusyKey = '';
        flushPendingProjects();
      }
    }
  }

  async function clearMemberOverride(agentId, field) {
    const project = selectedProject();
    if (!project || state.overrideBusyKey) {
      return;
    }
    state.overrideBusyKey = overrideKey(agentId, field);
    state.editError = '';
    try {
      const result = await operations.clearOverride(
        project.project_id,
        agentId,
        field,
      );
      if (!active) {
        return;
      }
      applyScan(result?.scan, { replaceDrafts: true });
      onToast({
        title: translate('projects.team.overrideCleared', 'Override cleared.'),
        variant: 'success',
      });
    } catch (error) {
      if (active) {
        onToast({
          title: `${translate('projects.team.overrideClearError', 'The override could not be cleared.')} ${errorText(error)}`,
          variant: 'error',
          sticky: true,
        });
      }
    } finally {
      if (active) {
        state.overrideBusyKey = '';
        flushPendingProjects();
      }
    }
  }

  function openRemove(project) {
    state.removeConfirmProject = project;
    state.copyRootedAgentIdentityFiles = false;
  }

  function cancelRemove() {
    state.removeConfirmProject = null;
    flushPendingProjects();
  }

  function removeErrorText(error) {
    if (error?.code === PROJECT_BUSY_CODE) {
      return translate(
        'projects.remove.busy',
        'This project has an active or queued run and cannot be removed right now.',
      );
    }
    if (error?.code === PROJECT_IN_USE_CODE) {
      return translate(
        'projects.remove.inUse',
        'A cron job points at one of this project’s agents, so it cannot be removed. Remove or retarget the cron job first.',
      );
    }
    return `${translate('projects.remove.error', 'Project could not be removed.')} ${errorText(error)}`;
  }

  async function confirmRemove() {
    const project = state.removeConfirmProject;
    state.removeConfirmProject = null;
    if (!project) {
      flushPendingProjects();
      return;
    }
    state.removingProjectId = project.project_id;
    state.statusMessage = '';
    state.listError = '';
    state.editError = '';
    try {
      const result = await operations.removeProject(
        project.project_id,
        state.copyRootedAgentIdentityFiles,
      );
      if (!active) {
        return;
      }
      if (state.selectedProjectId === project.project_id) {
        state.selectedProjectId = '';
        state.activeTeam = [];
        state.activeReport = null;
        state.activeScanSkills = emptyScanSkills();
      }
      const affectedCount = Array.isArray(result?.affected_agent_ids)
        ? result.affected_agent_ids.length
        : 0;
      const copyState = state.copyRootedAgentIdentityFiles
        ? translate('projects.remove.filesCopied', 'were copied')
        : translate('projects.remove.filesNotCopied', 'were not copied');
      state.statusMessage =
        affectedCount === 1
          ? translate(
              'projects.remove.successOneAgent',
              'Project removed. 1 Agent was reset; identity files {copyState}.',
              { copyState },
            )
          : translate(
              'projects.remove.successManyAgents',
              'Project removed. {count} Agents were reset; identity files {copyState}.',
              { count: affectedCount, copyState },
            );
      await loadProjects();
    } catch (error) {
      if (!active) {
        return;
      }
      const message = removeErrorText(error);
      if (state.selectedProjectId === project.project_id) {
        state.editError = message;
      } else {
        state.listError = message;
      }
    } finally {
      if (active) {
        state.removingProjectId = '';
        flushPendingProjects();
      }
    }
  }

  function openRePoint(project) {
    state.rePointProject = project;
    state.rePointCwd = '';
    state.rePointError = '';
  }

  function closeRePoint() {
    if (state.rePointing) {
      return;
    }
    state.rePointProject = null;
    state.rePointError = '';
    flushPendingProjects();
  }

  async function submitRePoint() {
    if (!state.rePointProject) {
      return;
    }
    if (state.rePointCwd.trim().length === 0) {
      state.rePointError = translate(
        'projects.rePoint.missingCwd',
        'Enter the new repository path.',
      );
      return;
    }
    state.rePointing = true;
    state.rePointError = '';
    state.statusMessage = '';
    try {
      const projectId = state.rePointProject.project_id;
      const result = await operations.setProject(
        projectId,
        buildRePointPayload(state.rePointCwd),
      );
      if (!active) {
        return;
      }
      state.statusMessage = translate(
        'projects.rePoint.success',
        'Project re-pointed.',
      );
      state.rePointProject = null;
      await loadProjects();
      if (active && state.selectedProjectId === projectId) {
        selectProject(projectId, result?.scan);
      }
    } catch (error) {
      if (active) {
        state.rePointError = `${translate('projects.rePoint.error', 'The project could not be re-pointed.')} ${errorText(error)}`;
      }
    } finally {
      if (active) {
        state.rePointing = false;
        flushPendingProjects();
      }
    }
  }

  function destroy() {
    active = false;
    listRequestId += 1;
    scanRequestId += 1;
    pendingProjectList = null;
    clearAutoSave();
    clearDetect();
  }

  return {
    applyScan,
    cancelRemove,
    canSetOverride,
    clearAutoSave,
    clearDetect,
    clearMemberOverride,
    clearSelectedProject,
    closeAdd,
    closeRePoint,
    confirmRemove,
    destroy,
    invalidateScan,
    initialize,
    isOverrideBusy,
    loadCatalogs,
    loadProjects,
    loadScan,
    openAdd,
    openRemove,
    openRePoint,
    overrideDraft,
    pendingChanges,
    flushPendingProjects,
    refreshScan,
    replaceListField,
    scheduleAutoSave,
    saveSelectedProject,
    selectProject,
    selectedProject,
    setMemberOverride,
    state,
    submitAdd,
    submitRePoint,
    trackModelDropdownOpen,
    updateAddField,
    updateEditField,
    updateListField,
    updateModelsRefreshToken,
    updateProjectsRefreshToken,
    updateOverrideDraft,
  };
}

// Pure view helpers for the Projects tab. Business and normalization logic
// lives here so the Svelte component stays a thin display/input/orchestration
// layer (see webui.md → Conventions). Every export is unit-tested in
// __tests__/projectsView.test.js.
//
// The shapes mirror the verified backend contract (server/rpc/project_methods):
//   project: { project_id, display_name, cwd, cwd_exists, default_agent,
//              default_model, auto_load[], created_at, updated_at }
//   scan:    { team: [member…], report: { clean, findings: [finding…] } }
//   finding: { type, detail, agent_id, source_path }

// The scan report's `finding.type` discriminants (server scan_report.py).
export const FINDING_TYPE_SLUG_COLLISION = 'slug_collision';
export const FINDING_TYPE_UNSLUGIFIABLE_NAME = 'unslugifiable_name';
export const FINDING_TYPE_BAD_MODEL = 'bad_model';
export const FINDING_TYPE_ORPHAN = 'orphan';
export const FINDING_TYPE_UNAVAILABLE_TOOL = 'unavailable_tool';

// Stable display order for grouped findings, so the report always lists the
// same finding kinds in the same order regardless of server ordering.
const FINDING_TYPES = Object.freeze([
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_UNAVAILABLE_TOOL,
]);

// The mutable fields a manage form can change through project.set. cwd is
// handled by the dedicated re-point path, and default_temperature /
// default_thinking_effort have their own typed diff (number/null and
// null/''/level), so they are not part of this generic string-trim diff.
const MANAGE_FIELDS = Object.freeze([
  'display_name',
  'default_agent',
  'default_model',
  'source_format',
]);

// Fields in the generic diff that are required non-empty on the backend: an
// empty form value is "no change", never a clear-to-null. Display name is
// intentionally clearable and then falls back to the stable Project id.
const NON_CLEARABLE_MANAGE_FIELDS = Object.freeze(new Set(['source_format']));

// The per-project source format vocabulary (mirrors the backend
// PROJECT_SOURCE_FORMATS): which coding-agent ecosystem the project's Team
// agents and skills come from. Exactly one per project — no mixing.
export const PROJECT_SOURCE_FORMATS = Object.freeze(['opencode', 'claude']);
export const DEFAULT_PROJECT_SOURCE_FORMAT = 'opencode';

// The list-valued whitelist fields, diffed by SET (order-insensitive) so a
// reorder alone never counts as a change. Tool/skill names are unordered membership
// sets; an empty list is a real value (e.g. every tool off).
const WHITELIST_LIST_FIELDS = Object.freeze([
  'allowed_tools',
  'skills_bundled_enabled',
  'skills_global_enabled',
  'skills_project_disabled',
]);

// The dropdown sentinel for "no project default" thinking effort. Defined here
// (not imported from settingsView.js) to keep the two view modules decoupled; it
// mirrors AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT. Distinct from '' which is a
// real value meaning "provider default" (stops the resolution chain).
export const PROJECT_THINKING_EFFORT_NO_DEFAULT =
  '__project_thinking_effort_no_default__';

// The effort ladder a project default may pick (mirrors the agent thinking
// levels). The sentinel and '' (provider default) are added around these in the
// dropdown; only these literals are accepted as a real level in the payload.
export const PROJECT_THINKING_EFFORT_OPTIONS = Object.freeze([
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

// Build the project.add payload from the add-form values. cwd is required (the
// thin api wrapper enforces it too); the optional pointers are only included
// when the user actually typed something, matching the backend's
// "non-empty string" rule for these params.
export function buildAddProjectPayload(formValues) {
  const payload = {
    cwd: asText(formValues?.cwd).trim(),
  };

  const displayName = optionalText(formValues?.display_name);
  if (displayName !== null) {
    payload.display_name = displayName;
  }

  const defaultAgent = optionalText(formValues?.default_agent);
  if (defaultAgent !== null) {
    payload.default_agent = defaultAgent;
  }

  const defaultModel = optionalText(formValues?.default_model);
  if (defaultModel !== null) {
    payload.default_model = defaultModel;
  }

  // Only send an explicit, known format — absent means the server auto-detects
  // from the repo (exactly one format present → that one, else opencode).
  const sourceFormat = asText(formValues?.source_format).trim();
  if (PROJECT_SOURCE_FORMATS.includes(sourceFormat)) {
    payload.source_format = sourceFormat;
  }

  // Only include the knobs when the form carries a real value: a number for
  // temperature, and a level or '' (provider default) for thinking effort. The
  // "no default" sentinel / empty temperature box means "omit" at add time.
  const defaultTemperature = normalizeProjectTemperature(
    formValues?.default_temperature,
  );
  if (defaultTemperature !== null) {
    payload.default_temperature = defaultTemperature;
  }

  const defaultThinkingEffort = normalizeProjectThinkingEffortForPayload(
    formValues?.default_thinking_effort,
  );
  if (defaultThinkingEffort !== null) {
    payload.default_thinking_effort = defaultThinkingEffort;
  }

  const autoLoad = normalizeAutoLoad(formValues?.auto_load);
  if (autoLoad.length > 0) {
    payload.auto_load = autoLoad;
  }

  return payload;
}

// Build the sparse project.set changes for a manage form: only fields whose
// value actually differs from the current project, and at least one (callers
// must guard with `hasManageChanges` before sending). project.set rejects an
// empty change set, so this never produces one silently — an unchanged form
// yields `{}` and the caller short-circuits.
//
// auto_load is compared as an ordered list; display_name / default_agent /
// default_model compare as trimmed strings. A pointer field
// (default_agent/default_model) cleared to empty is sent as `null` — the
// backend's `_optional_string` rejects a sent empty string with
// `invalid_request`, and only maps JSON `null` (None) to "" to clear the
// pointer (fall through the model chain). A non-empty pointer is sent as the
// trimmed string. A cleared display_name is likewise sent as null and the
// Project domain falls back to project_id.
export function buildManageProjectPayload(formValues, project) {
  const changes = {};

  for (const field of MANAGE_FIELDS) {
    const next = asText(formValues?.[field]).trim();
    const current = asText(project?.[field]).trim();
    if (next === current) {
      continue;
    }
    if (NON_CLEARABLE_MANAGE_FIELDS.has(field) && next === '') {
      // Required non-empty on the backend; an empty box is not a clear.
      continue;
    }
    // A cleared pointer must be sent as null (the backend maps None → "" to
    // clear it); a sent empty string would be rejected as invalid_request.
    changes[field] = next === '' ? null : next;
  }

  // Temperature: form string → number|null; send only when it differs from the
  // stored value. null clears the project default (fall through the chain), a
  // number sets it (0 is a real value, the sampling floor).
  const nextTemperature = normalizeProjectTemperature(
    formValues?.default_temperature,
  );
  const currentTemperature = numberOrNull(project?.default_temperature);
  if (nextTemperature !== currentTemperature) {
    changes.default_temperature = nextTemperature;
  }

  // Thinking effort: form (sentinel|''|level) → null|''|level; send only on a
  // change. null clears the project default, '' forces the provider default, a
  // level sets it.
  const nextThinkingEffort = normalizeProjectThinkingEffortForPayload(
    formValues?.default_thinking_effort,
  );
  const currentThinkingEffort = stringOrNull(project?.default_thinking_effort);
  if (nextThinkingEffort !== currentThinkingEffort) {
    changes.default_thinking_effort = nextThinkingEffort;
  }

  const nextAutoLoad = normalizeAutoLoad(formValues?.auto_load);
  const currentAutoLoad = normalizeAutoLoad(project?.auto_load);
  if (!sameStringList(nextAutoLoad, currentAutoLoad)) {
    changes.auto_load = nextAutoLoad;
  }

  // The Tool/Skill Whitelist lists are membership sets: send a field only when its
  // set actually changed, so toggling tools/skills persists but a mere reorder does
  // not. An empty list (e.g. every tool off) is a real value and is sent as `[]`.
  for (const field of WHITELIST_LIST_FIELDS) {
    const next = normalizeStringList(formValues?.[field]);
    const current = normalizeStringList(project?.[field]);
    if (!sameStringSet(next, current)) {
      changes[field] = next;
    }
  }

  return changes;
}

// Build the tool toggle rows for the editor: every server-marked Project-configurable
// catalog tool with whether it is in the project's current Tool Whitelist. The
// tool-catalog RPC owns that policy, so new tools and policy changes appear without a
// frontend name list. Rows are sorted by
// name for a stable display. Each row carries the tool's readiness fields
// (`ready`/`readiness_hint`/`extension`) so a not-ready tool renders the shared
// "currently unavailable" notice (its toggle stays functional — the whitelist is
// independent of readiness). A string catalog entry has no readiness metadata, so
// it defaults to ready.
export function buildToolToggleList({ catalog = [], allowedTools = [] } = {}) {
  const enabled = new Set(normalizeStringList(allowedTools));
  const byName = new Map();
  for (const tool of Array.isArray(catalog) ? catalog : []) {
    const isObject = tool !== null && typeof tool === 'object';
    const name = asText(isObject ? tool?.name : tool).trim();
    const projectConfigurable =
      !isObject || tool?.project_configurable !== false;
    if (name.length === 0 || !projectConfigurable || byName.has(name)) {
      continue;
    }
    byName.set(name, {
      name,
      description: isObject ? (tool.description ?? '') : '',
      enabled: enabled.has(name),
      ready: isObject ? tool.ready !== false : true,
      readiness_hint: isObject ? (tool.readiness_hint ?? null) : null,
      extension: isObject ? (tool.extension ?? null) : null,
    });
  }

  // Keep persisted entries that disappeared from the live catalog visible and
  // removable. This is common when an Extension is temporarily disabled: the
  // backend deliberately preserves the permission, reports it as unavailable,
  // and only rejects *new* unknown grants. A stale row therefore stays on and
  // gets the shared not-ready treatment until the tool returns or the user turns
  // it off. Project-excluded names are included only through this recovery path;
  // they can never be newly selected from the catalog.
  for (const name of enabled) {
    if (byName.has(name)) {
      continue;
    }
    byName.set(name, {
      name,
      description: '',
      enabled: true,
      ready: false,
      readiness_hint: null,
      extension: null,
      registered: false,
    });
  }
  return Array.from(byName.values()).sort((left, right) =>
    left.name.localeCompare(right.name),
  );
}

// Build the skill toggle sections for the editor from a project's skill pool and
// its stored whitelist rule. Project skills are on by default (off only when named
// in `skills_project_disabled`); bundled and global skills are off by default (on
// only when named in `skills_bundled_enabled` / `skills_global_enabled`). A bundled
// or global skill shadowed by a project skill of the same name is dropped from its
// section (project wins).
// Normalize one skill-pool entry to `{ name, description }`. The scan sends
// `{name, description}` objects (so the whitelist chips can show a description on
// hover); a bare string is tolerated too and gets an empty description. Nameless
// entries drop out.
function normalizeSkillPoolEntry(entry) {
  if (entry !== null && typeof entry === 'object') {
    const name = asText(entry.name).trim();
    return name ? { name, description: asText(entry.description) } : null;
  }
  const name = asText(entry).trim();
  return name ? { name, description: '' } : null;
}

export function normalizeSkillPool(list) {
  return (Array.isArray(list) ? list : [])
    .map(normalizeSkillPoolEntry)
    .filter(Boolean);
}

export function buildSkillToggleSections({
  projectSkills = [],
  bundledSkills = [],
  globalSkills = [],
  skillsBundledEnabled = [],
  skillsGlobalEnabled = [],
  skillsProjectDisabled = [],
} = {}) {
  const disabled = new Set(normalizeStringList(skillsProjectDisabled));
  const enabledBundled = new Set(normalizeStringList(skillsBundledEnabled));
  const enabledGlobal = new Set(normalizeStringList(skillsGlobalEnabled));
  const projectEntries = normalizeSkillPool(projectSkills);
  const projectSet = new Set(projectEntries.map((skill) => skill.name));
  return {
    project: projectEntries.map((skill) => ({
      name: skill.name,
      description: skill.description,
      enabled: !disabled.has(skill.name),
    })),
    bundled: normalizeSkillPool(bundledSkills)
      .filter((skill) => !projectSet.has(skill.name))
      .map((skill) => ({
        name: skill.name,
        description: skill.description,
        enabled: enabledBundled.has(skill.name),
      })),
    global: normalizeSkillPool(globalSkills)
      .filter((skill) => !projectSet.has(skill.name))
      .map((skill) => ({
        name: skill.name,
        description: skill.description,
        enabled: enabledGlobal.has(skill.name),
      })),
  };
}

// Add or remove a name from a list (returns a new normalized list), the single
// primitive the editor's toggle handlers use to mutate a whitelist field.
export function setListMembership(list, name, include) {
  const normalized = normalizeStringList(list);
  const target = asText(name).trim();
  if (!target) {
    return normalized;
  }
  const has = normalized.includes(target);
  if (include && !has) {
    return [...normalized, target];
  }
  if (!include && has) {
    return normalized.filter((item) => item !== target);
  }
  return normalized;
}

// Normalize the scan response's skill pool into the editor's `{name, description}`
// lists (each group carries descriptions for the whitelist chips' hover cards).
export function normalizeScanSkills(scan) {
  const skills = scan?.skills ?? {};
  return {
    project: normalizeSkillPool(skills.project),
    bundled: normalizeSkillPool(skills.bundled),
    global: normalizeSkillPool(skills.global),
  };
}

// Whether a manage payload carries at least one change (project.set needs ≥1).
export function hasManageChanges(changes) {
  return isPlainObject(changes) && Object.keys(changes).length > 0;
}

// Build the option list for a project's default-agent dropdown from the scanned
// team. The leading empty option (value '') is "no project default — fall
// through the resolution chain". A stored default_agent that is no longer in the
// team is kept as a trailing option so the current value stays visible and
// selectable rather than silently dropping when the team changes.
export function buildDefaultAgentOptions({
  team = [],
  currentValue = '',
  emptyLabel = '',
  unavailableLabel = (agentId) => agentId,
} = {}) {
  const current = asText(currentValue).trim();
  const options = [{ value: '', label: emptyLabel }];
  const seen = new Set();

  for (const member of Array.isArray(team) ? team : []) {
    const agentId = asText(member?.agent_id).trim();
    if (!agentId || seen.has(agentId)) {
      continue;
    }
    seen.add(agentId);
    const displayName = asText(member?.display_name).trim() || agentId;
    options.push({
      value: agentId,
      label: displayName,
      secondaryLabel: displayName === agentId ? '' : agentId,
    });
  }

  if (current && !seen.has(current)) {
    options.push({ value: current, label: unavailableLabel(current) });
  }

  return options;
}

// Normalize a project.detect response into the add dialog's stable shape:
// per-format `{agents, skills, present}` (present = ≥1 agent OR ≥1 skill — the
// creation-time detection rule) plus the context-file facts. A missing/foreign
// response degrades to "nothing found".
export function normalizeDetectResult(result) {
  const rawFormats = isPlainObject(result?.formats) ? result.formats : {};
  const formats = {};
  for (const key of PROJECT_SOURCE_FORMATS) {
    const entry = isPlainObject(rawFormats[key]) ? rawFormats[key] : {};
    const agents = countOrZero(entry.agents);
    const skills = countOrZero(entry.skills);
    formats[key] = { agents, skills, present: agents > 0 || skills > 0 };
  }
  const context = isPlainObject(result?.context_files)
    ? result.context_files
    : {};
  return {
    cwd_exists: result?.cwd_exists === true,
    formats,
    agents_md: context.agents_md === true,
    claude_md: optionalText(context.claude_md),
  };
}

// The formats a detect result found present, in canonical order. Drives the add
// dialog's three states: none → silent opencode default, one → quiet "Detected"
// line, both → the informed radio choice.
export function presentFormats(detect) {
  return PROJECT_SOURCE_FORMATS.filter(
    (key) => detect?.formats?.[key]?.present === true,
  );
}

// The CLAUDE.md comfort suggestion (decision 4): offer adding the found
// CLAUDE.md as a normal project file only when the repo has no AGENTS.md —
// an explicit user opt-in checkbox, nothing automatic.
export function shouldSuggestClaudeMd(detect) {
  return Boolean(detect?.claude_md) && detect?.agents_md !== true;
}

// A project's cwd no longer resolves to a directory → offer Re-Point. The flag
// is server-computed (`cwd_exists`); only an explicit `false` triggers it, so a
// missing/undefined flag never forces the re-point UI.
export function needsRePoint(project) {
  return project?.cwd_exists === false;
}

// The change set for a Re-Point: project.set with the new cwd only. The caller
// passes the project_id separately to setProject, so this is just `{ cwd }`.
export function buildRePointPayload(cwd) {
  return { cwd: asText(cwd).trim() };
}

// Normalize one project record from the backend into a stable display shape.
export function normalizeProject(project) {
  return {
    project_id: asText(project?.project_id),
    display_name: asText(project?.display_name),
    cwd: asText(project?.cwd),
    cwd_exists: project?.cwd_exists === true,
    default_agent: asText(project?.default_agent),
    default_model: asText(project?.default_model),
    default_temperature: numberOrNull(project?.default_temperature),
    default_thinking_effort: stringOrNull(project?.default_thinking_effort),
    source_format: PROJECT_SOURCE_FORMATS.includes(project?.source_format)
      ? project.source_format
      : DEFAULT_PROJECT_SOURCE_FORMAT,
    auto_load: normalizeAutoLoad(project?.auto_load),
    allowed_tools: normalizeStringList(project?.allowed_tools),
    skills_bundled_enabled: normalizeStringList(
      project?.skills_bundled_enabled,
    ),
    skills_global_enabled: normalizeStringList(project?.skills_global_enabled),
    skills_project_disabled: normalizeStringList(
      project?.skills_project_disabled,
    ),
    created_at: optionalText(project?.created_at),
    updated_at: optionalText(project?.updated_at),
  };
}

export function normalizeProjects(projects) {
  const raw = Array.isArray(projects) ? projects : [];
  return raw.map((project) => normalizeProject(project));
}

// The three per-agent overridable / effective run fields, in display order. Each is
// resolved through the config-agent chain (override → agent file → project default →
// global default) and reported by the scan as `effective[field] = {value, source}`.
export const TEAM_EFFECTIVE_FIELDS = Object.freeze([
  'model',
  'temperature',
  'thinking_effort',
]);

// The winning-source discriminants the scan reports on `effective[field].source`.
export const EFFECTIVE_SOURCE_OVERRIDE = 'override';
export const EFFECTIVE_SOURCE_AGENT = 'agent';
export const EFFECTIVE_SOURCE_PROJECT_DEFAULT = 'project_default';
export const EFFECTIVE_SOURCE_GLOBAL_DEFAULT = 'global_default';

// Project the scan's team into a stable, display-ready list. The repo is the
// source of truth (no copy drift) — this only shapes what the view renders. Each
// member carries its raw repo-declared values (for reference), the per-agent
// `overrides` object (or null), and the `effective` map of `{value, source}` per
// run field so the row can show the resolved value with provenance.
//
// NOTE: `agent_id` and `display_name` are consumed by ChatView's project team bar
// (the second consumer of this helper) — do not drop or rename them.
export function projectTeam(scan) {
  const raw = Array.isArray(scan?.team) ? scan.team : [];
  return raw.map((member) => ({
    agent_id: asText(member?.agent_id),
    display_name: asText(member?.display_name) || asText(member?.agent_id),
    description: asText(member?.description),
    model: asText(member?.model),
    temperature:
      typeof member?.temperature === 'number' ? member.temperature : null,
    thinking_effort: stringOrNull(member?.thinking_effort),
    source_format: asText(member?.source_format),
    source_path: asText(member?.source_path),
    denied_tools: normalizeStringList(member?.denied_tools),
    tools:
      member?.tools && typeof member.tools === 'object' ? member.tools : {},
    // The per-agent override object (any subset of model/temperature/thinking_effort),
    // or null when the agent has no override. Read shape-only here — the row derives
    // whether a field is overridden from `effective[field].source === 'override'`.
    overrides: normalizeOverrides(member?.overrides),
    // The provenance-aware resolved values, one entry per run field:
    // `{ value, source }`. A null value means "not configured" (model) or
    // "provider default" (temperature/thinking); a null source means no tier won.
    effective: normalizeEffective(member?.effective),
  }));
}

// Summarize one Project Agent's repository-owned Sub-Agent targets against the
// current Team. Project targets are always local bare ids; there is deliberately
// no vBot override tier for this policy.
export function projectAgentTargetSummary(member, team = []) {
  const configured = member?.tools?.subagent?.allowed_agents;
  if (!Array.isArray(configured)) {
    return { mode: 'unavailable', agents: [] };
  }
  const callerAgentId = asText(member?.agent_id).trim();
  const allowed = normalizeStringList(configured).filter(
    (agentId) => agentId !== callerAgentId,
  );
  const teamIds = (Array.isArray(team) ? team : [])
    .map((candidate) => asText(candidate?.agent_id).trim())
    .filter((agentId) => agentId && agentId !== callerAgentId);
  if (allowed.length === 0) {
    return { mode: 'self', agents: [] };
  }
  const allowedSet = new Set(allowed);
  if (
    teamIds.length > 0 &&
    allowed.length === teamIds.length &&
    teamIds.every((agentId) => allowedSet.has(agentId))
  ) {
    return { mode: 'all', agents: allowed };
  }
  return { mode: 'limited', agents: allowed };
}

// Normalize the member's `overrides` object into a plain map of the known fields, or
// null when absent/empty. The value shapes are field-specific and passed through
// verbatim (model string, temperature number, thinking-effort string).
function normalizeOverrides(overrides) {
  if (!isPlainObject(overrides)) {
    return null;
  }
  const normalized = {};
  for (const field of TEAM_EFFECTIVE_FIELDS) {
    if (Object.hasOwn(overrides, field)) {
      normalized[field] = overrides[field];
    }
  }
  if (isPlainObject(overrides.compaction_policy)) {
    normalized.compaction_policy = normalizeCompactionPolicy(
      overrides.compaction_policy,
    );
  }
  return Object.keys(normalized).length > 0 ? normalized : null;
}

// Normalize the member's `effective` map into `{ field: { value, source } }` for
// the known run fields. A missing field entry becomes `{ value: null, source:
// null }` so the row renders a stable "not configured / provider default" state
// rather than crashing on an absent key.
function normalizeEffective(effective) {
  const source = isPlainObject(effective) ? effective : {};
  const normalized = {};
  for (const field of TEAM_EFFECTIVE_FIELDS) {
    const entry = isPlainObject(source[field]) ? source[field] : {};
    normalized[field] = {
      value: entry.value ?? null,
      source: stringOrNull(entry.source),
    };
  }
  return normalized;
}

// Whether a team member currently has an override for the given field. Derived from
// `effective[field].source === 'override'`, the single truth for "overridden" that
// also drives the Clear-override control's visibility.
export function memberFieldIsOverridden(member, field) {
  if (field === 'compaction_policy') {
    return isPlainObject(member?.overrides?.compaction_policy);
  }
  return member?.effective?.[field]?.source === EFFECTIVE_SOURCE_OVERRIDE;
}

// Seed the per-field override draft (the values the override controls edit) for one
// team member. The model draft is the member's overridden model (or the
// effective/repo model as a starting suggestion), the temperature draft a text box
// seeded from the overridden/effective number, the thinking-effort draft the
// overridden/effective level. A blank draft means "nothing typed yet".
export function seedTeamOverrideDraft(member) {
  const overrides = isPlainObject(member?.overrides) ? member.overrides : {};
  const effective = member?.effective ?? {};

  const modelSeed = hasText(overrides.model)
    ? String(overrides.model)
    : effectiveTextValue(effective.model);
  const temperatureSeed = hasNumber(overrides.temperature)
    ? String(overrides.temperature)
    : effectiveTextValue(effective.temperature);
  const thinkingSeed =
    typeof overrides.thinking_effort === 'string'
      ? overrides.thinking_effort
      : effectiveTextValue(effective.thinking_effort);

  return {
    model: modelSeed,
    temperature: temperatureSeed,
    thinking_effort: thinkingSeed,
    compaction_policy: isPlainObject(overrides.compaction_policy)
      ? normalizeCompactionPolicy(overrides.compaction_policy)
      : null,
  };
}

// The temperature override value for the payload: a comma-tolerant number, or null
// when the box is empty/non-numeric (the Set button is disabled on null — an
// override must carry a value; clearing is a separate action).
export function normalizeOverrideTemperature(value) {
  return normalizeProjectTemperature(value);
}

function effectiveTextValue(entry) {
  const value = isPlainObject(entry) ? entry.value : null;
  return value === null || value === undefined ? '' : String(value);
}

function hasText(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function hasNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

// Normalize the scan report into a render-ready shape: the `clean` flag plus
// findings grouped by type in a stable order. An empty / clean report is the
// normal case (a bare or empty repo), NOT an error — `clean` is true and
// `groups` is empty, and callers must treat that as a healthy project.
export function normalizeScanReport(report) {
  const rawFindings = Array.isArray(report?.findings) ? report.findings : [];
  const findings = rawFindings.map((finding) => ({
    type: asText(finding?.type),
    detail: asText(finding?.detail),
    agent_id: asText(finding?.agent_id),
    source_path: optionalText(finding?.source_path),
  }));

  const groups = FINDING_TYPES.map((type) => ({
    type,
    findings: findings.filter((finding) => finding.type === type),
  })).filter((group) => group.findings.length > 0);

  // The server's `clean` flag is authoritative; fall back to "no findings" only
  // when it is absent so a malformed payload still renders sensibly.
  const clean =
    typeof report?.clean === 'boolean' ? report.clean : findings.length === 0;

  return {
    clean,
    findingCount: findings.length,
    findings,
    groups,
  };
}

// Trim + drop empties from a list-of-strings value (a non-array → []). The shared
// primitive behind auto_load and the whitelist list fields.
function normalizeStringList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => asText(item).trim())
    .filter((item) => item.length > 0);
}

function normalizeAutoLoad(value) {
  return normalizeStringList(value);
}

// Form temperature (a string, possibly comma-decimal) → number|null. Mirrors
// settingsView.js' normalizeAgentDefaultsTemperature: an empty/non-numeric box
// is "no value" (null), so the chain falls through.
function normalizeProjectTemperature(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return null;
  }
  const numberValue = Number(normalized.replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

// Form thinking effort (sentinel|''|level) → null|''|level for the payload.
// Mirrors settingsView.js' normalizeAgentDefaultsThinkingEffortForPayload: the
// sentinel and a missing value mean "no default" (null), '' means "provider
// default", and only a known level passes through (an unknown one → null).
function normalizeProjectThinkingEffortForPayload(value) {
  if (value === PROJECT_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }
  return PROJECT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}

function countOrZero(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : 0;
}

function numberOrNull(value) {
  return typeof value === 'number' ? value : null;
}

function stringOrNull(value) {
  return typeof value === 'string' ? value : null;
}

function sameStringList(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((item, index) => item === right[index]);
}

// Order-insensitive equality for the membership-set whitelist fields.
function sameStringSet(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  const rightSet = new Set(right);
  return left.every((item) => rightSet.has(item));
}

function optionalText(value) {
  const normalized = asText(value).trim();
  return normalized ? normalized : null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
