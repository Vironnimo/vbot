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
} from '../api.js';
import { scheduleAutosave } from '../autosave.js';
import { SURFACE_FORM, shouldApplyReloadNow } from '../resourceInvalidation.js';
import { normalizeToolAccess } from '../toolAccess.js';
import {
  emptyScanSkills,
  createProjectEditForm,
  createProjectsState,
  buildManageProjectPayload,
  setListMembership,
  normalizeScanSkills,
  hasManageChanges,
  normalizeProject,
  normalizeProjects,
  projectTeam,
  seedTeamOverrideDraft,
  normalizeOverrideTemperature,
  normalizeScanReport,
} from './presentation.js';
import { createProjectDialogs } from './dialogs.js';

const PROJECT_AUTO_SAVE_DEBOUNCE_MS = 800;

const TOOL_ACCESS_OVERRIDE_AUTO_SAVE_DEBOUNCE_MS = 800;

const PROJECT_DETECT_DEBOUNCE_MS = 500;

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
  toolAccessOverrideAutoSaveDelayMs = TOOL_ACCESS_OVERRIDE_AUTO_SAVE_DEBOUNCE_MS,
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
  let toolAccessOverrideAutoSaveTimer = null;

  let pendingProjectList = null;
  const {
    clearDetect,
    openAdd,
    closeAdd,
    updateAddField,
    submitAdd,
    openRemove,
    cancelRemove,
    confirmRemove,
    openRePoint,
    closeRePoint,
    submitRePoint,
  } = createProjectDialogs({
    state,
    operations,
    detectDelayMs,
    translate,
    isActive: () => active,
    errorText,
    selectProject,
    flushPendingProjects,
    loadProjects,
  });

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
    clearToolAccessOverrideAutoSave({ flushPending: false });
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
    const oldSeeds = Object.fromEntries(
      state.activeTeam.map((member) => [
        member.agent_id,
        seedTeamOverrideDraft(member),
      ]),
    );
    state.activeTeam = projectTeam(scan);
    state.activeReport = normalizeScanReport(scan?.report);
    state.activeScanSkills = normalizeScanSkills(scan);
    if (!replaceDrafts) {
      for (const member of state.activeTeam) {
        const previous = oldSeeds[member.agent_id];
        const draft = state.overrideDrafts[member.agent_id];
        if (!previous || !draft) continue;
        const next = seedTeamOverrideDraft(member);
        for (const field of Object.keys(next)) {
          if (JSON.stringify(draft[field]) === JSON.stringify(previous[field]))
            draft[field] = next[field];
        }
      }
    }
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
          JSON.stringify(savedDraft.compaction_policy) ||
        JSON.stringify(draft.tool_access) !==
          JSON.stringify(savedDraft.tool_access)
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
        toolAccessOverrideAutoSaveTimer !== null ||
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
      autoSaveTimer();
      autoSaveTimer = null;
    }
    if (flushPending) {
      flushPendingProjects();
    }
  }

  function clearToolAccessOverrideAutoSave({ flushPending = true } = {}) {
    if (toolAccessOverrideAutoSaveTimer !== null) {
      toolAccessOverrideAutoSaveTimer();
      toolAccessOverrideAutoSaveTimer = null;
    }
    if (flushPending) {
      flushPendingProjects();
    }
  }

  function scheduleAutoSave(save) {
    clearAutoSave();
    autoSaveTimer = scheduleAutosave(() => {
      autoSaveTimer = null;
      if (active) {
        void save();
      }
    }, autoSaveDelayMs);
  }

  function scheduleToolAccessOverrideAutoSave(save) {
    clearToolAccessOverrideAutoSave();
    toolAccessOverrideAutoSaveTimer = scheduleAutosave(() => {
      toolAccessOverrideAutoSaveTimer = null;
      if (active) {
        void save();
      }
    }, toolAccessOverrideAutoSaveDelayMs);
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

  function moveAutoLoadEntry(from, to) {
    const files = state.editForm.auto_load;
    if (
      state.editSaving ||
      !Number.isInteger(from) ||
      !Number.isInteger(to) ||
      from < 0 ||
      to < 0 ||
      from >= files.length ||
      to >= files.length ||
      from === to
    ) {
      return false;
    }
    const next = [...files];
    const [file] = next.splice(from, 1);
    next.splice(to, 0, file);
    updateEditField('auto_load', next);
    return true;
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
      return false;
    }
    const changes = pendingChanges();
    if (!hasManageChanges(changes)) {
      if (manual) {
        onToast({
          title: translate('common.alreadySaved', 'Already saved'),
          variant: 'success',
        });
      }
      return true;
    }
    clearAutoSave({ flushPending: false });
    state.editSaving = true;
    state.editError = '';
    state.statusMessage = '';
    const savedEditFormSnapshot = JSON.stringify(state.editForm);
    try {
      const result = await operations.setProject(project.project_id, changes);
      if (!active) {
        return true;
      }
      await loadProjects({ reload: true });
      if (!active) {
        return true;
      }
      const savedProject = normalizeProject(result?.project);
      if (pendingProjectList) {
        state.projects = pendingProjectList;
        pendingProjectList = null;
      } else if (savedProject.project_id) {
        state.projects = state.projects.map((candidate) =>
          candidate.project_id === savedProject.project_id
            ? savedProject
            : candidate,
        );
      }
      if (JSON.stringify(state.editForm) === savedEditFormSnapshot) {
        state.editForm = createProjectEditForm(
          selectedProject() ?? savedProject,
        );
      }
      applyScan(result?.scan);
      onToast({
        title: translate('projects.manage.saveSuccess', 'Project updated.'),
        variant: 'success',
      });
      return true;
    } catch (error) {
      if (active) {
        state.editError = `${translate('projects.manage.saveError', 'Project changes could not be saved.')} ${errorText(error)}`;
      }
      return false;
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
        tool_access: { mode: 'all' },
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
    if (field === 'tool_access') {
      return normalizeToolAccess(draft.tool_access);
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
    if (field === 'tool_access') {
      return draft.tool_access !== null;
    }
    return typeof draft.thinking_effort === 'string';
  }

  function pendingOverrideChanges() {
    return state.activeTeam.flatMap((member) => {
      const draft = state.overrideDrafts[member.agent_id];
      if (!draft) return [];
      const saved = seedTeamOverrideDraft(member);
      return Object.keys(saved).flatMap((field) => {
        if (JSON.stringify(draft[field]) === JSON.stringify(saved[field]))
          return [];
        return [
          {
            agentId: member.agent_id,
            field,
            value: overrideValueForField(member.agent_id, field),
          },
        ];
      });
    });
  }

  async function savePendingOverrides() {
    clearToolAccessOverrideAutoSave({ flushPending: false });
    for (const change of pendingOverrideChanges()) {
      if (!canSetOverride(change.agentId, change.field)) {
        state.editError = translate(
          'errors.validation',
          'Check the highlighted fields and try again.',
        );
        return false;
      }
      if (
        !(await setMemberOverride(change.agentId, change.field, change.value))
      )
        return false;
    }
    return true;
  }

  async function setMemberOverride(agentId, field, explicitValue = undefined) {
    const project = selectedProject();
    if (
      !project ||
      state.overrideBusyKey ||
      (explicitValue === undefined && !canSetOverride(agentId, field))
    ) {
      return false;
    }
    const submittedDrafts = JSON.parse(JSON.stringify(state.overrideDrafts));
    state.overrideBusyKey = overrideKey(agentId, field);
    state.editError = '';
    try {
      const result = await operations.setOverride(
        project.project_id,
        agentId,
        field,
        explicitValue === undefined
          ? overrideValueForField(agentId, field)
          : explicitValue,
      );
      if (!active) {
        return false;
      }
      applyScan(result?.scan);
      const member = state.activeTeam.find(
        (entry) => entry.agent_id === agentId,
      );
      if (
        member &&
        JSON.stringify(state.overrideDrafts[agentId]?.[field]) ===
          JSON.stringify(submittedDrafts[agentId]?.[field])
      ) {
        state.overrideDrafts = {
          ...state.overrideDrafts,
          [agentId]: {
            ...state.overrideDrafts[agentId],
            [field]: seedTeamOverrideDraft(member)[field],
          },
        };
      }
      onToast({
        title: translate('projects.team.overrideSaved', 'Override saved.'),
        variant: 'success',
      });
      return true;
    } catch (error) {
      if (active) {
        onToast({
          title: `${translate('projects.team.overrideError', 'The override could not be saved.')} ${errorText(error)}`,
          variant: 'error',
          sticky: true,
        });
      }
      return false;
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
    if (field === 'tool_access') {
      clearToolAccessOverrideAutoSave({ flushPending: false });
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

  function destroy() {
    active = false;
    listRequestId += 1;
    scanRequestId += 1;
    pendingProjectList = null;
    clearAutoSave();
    clearToolAccessOverrideAutoSave();
    clearDetect();
  }

  return {
    applyScan,
    cancelRemove,
    canSetOverride,
    clearAutoSave,
    clearToolAccessOverrideAutoSave,
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
    pendingOverrideChanges,
    flushPendingProjects,
    refreshScan,
    replaceListField,
    scheduleAutoSave,
    scheduleToolAccessOverrideAutoSave,
    savePendingOverrides,
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
    moveAutoLoadEntry,
    updateListField,
    updateModelsRefreshToken,
    updateProjectsRefreshToken,
    updateOverrideDraft,
  };
}
