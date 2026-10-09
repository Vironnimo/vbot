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
import { t } from '../i18n.js';
import { shouldApplyReloadNow } from '../resourceInvalidation.js';
import { normalizeToolAccess, normalizeToolLoading } from '../toolAccess.js';
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
  normalizeOverrideNumber,
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
  // The Project a list load shows when it is listed (the view's place),
  // before the current selection and the first Project.
  targetProjectId = () => '',
  onProjectSelected = () => {},
  // The add dialog created this Project and selected it.
  onProjectAdded = () => {},
  onToast = () => {},
} = {}) {
  let active = true;
  let listRequestId = 0;
  let scanRequestId = 0;
  let selectionVersion = 0;
  const overrideEditVersions = new Map();
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
    isActive: () => active,
    errorText,
    selectProject,
    onProjectAdded,
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
    return t('common.unknown');
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
        sources: state.editForm.sources,
        model_mappings: state.editForm.model_mappings,
        default_temperature: state.editForm.default_temperature,
        default_top_p: state.editForm.default_top_p,
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
    selectionVersion += 1;
    overrideEditVersions.clear();
    clearToolAccessOverrideAutoSave({ flushPending: false });
    state.editForm = createProjectEditForm(project);
    state.autoLoadDraft = '';
    state.editError = '';
    state.editSaving = false;
    state.activeTeam = [];
    state.activeReport = null;
    state.activeSources = [];
    state.shadowedTeam = [];
    state.activeScanSkills = emptyScanSkills();
    state.expandedMembers = {};
    state.overrideDrafts = {};
    state.pendingOverrideReset = null;
    state.overrideBusyKey = '';
  }

  function seedOverrideDrafts() {
    const next = { ...state.overrideDrafts };
    for (const member of state.activeTeam) {
      if (!next[member.agent_id]) {
        next[member.agent_id] = seedTeamOverrideDraft(member);
      }
    }
    state.overrideDrafts = next;
  }

  // Untouched fields follow the scan; edited ones keep their draft.
  function applyScan(scan) {
    const oldSeeds = Object.fromEntries(
      state.activeTeam.map((member) => [
        member.agent_id,
        seedTeamOverrideDraft(member),
      ]),
    );
    state.activeTeam = projectTeam(scan);
    state.activeSources = scan?.sources ?? [];
    state.shadowedTeam = projectTeam({ team: scan?.shadowed ?? [] });
    state.activeReport = normalizeScanReport(scan?.report);
    state.activeScanSkills = normalizeScanSkills(scan);
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
    seedOverrideDrafts();
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
    return (
      state.pendingOverrideReset !== null || pendingOverrideChanges().length > 0
    );
  }

  function projectReloadCanApply() {
    return shouldApplyReloadNow({
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
    state.projectsLoaded = true;
    const listed = (projectId) =>
      state.projects.find((project) => project.project_id === projectId);
    const projectToOpen =
      listed(targetProjectId()) ??
      listed(state.selectedProjectId) ??
      state.projects[0] ??
      null;
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
      state.listError = `${t('projects.loadError')} ${errorText(error)}`;
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
      state.editError = `${t('projects.loadError')} ${errorText(error)}`;
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
      shouldApplyReloadNow({
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

  async function saveSelectedProject() {
    const project = selectedProject();
    if (!project || state.editSaving) {
      return false;
    }
    const changes = pendingChanges();
    if (!hasManageChanges(changes)) {
      return true;
    }
    clearAutoSave({ flushPending: false });
    const selection = selectionVersion;
    state.editSaving = true;
    state.editError = '';
    state.statusMessage = '';
    const savedEditFormSnapshot = JSON.stringify(state.editForm);
    try {
      const result = await operations.setProject(project.project_id, changes);
      if (!active || selection !== selectionVersion) {
        return false;
      }
      await loadProjects({ reload: true });
      if (!active || selection !== selectionVersion) {
        return false;
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
      return true;
    } catch (error) {
      if (active && selection === selectionVersion) {
        state.editError = `${t('projects.manage.saveError')} ${errorText(error)}`;
      }
      return false;
    } finally {
      if (active && selection === selectionVersion) {
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
        top_p: '',
        thinking_effort: '',
        compaction_policy: null,
        tool_access: { mode: 'all' },
        tool_loading: null,
      }
    );
  }

  function updateOverrideDraft(agentId, field, value) {
    const key = overrideKey(agentId, field);
    overrideEditVersions.set(key, overrideEditVersion(agentId, field) + 1);
    state.overrideDrafts = {
      ...state.overrideDrafts,
      [agentId]: {
        ...overrideDraft(agentId),
        [field]: field === 'tool_loading' ? normalizeToolLoading(value) : value,
      },
    };
    state.editError = '';
    flushPendingProjects();
  }

  function overrideKey(agentId, field) {
    return `${agentId}:${field}`;
  }

  function overrideEditVersion(agentId, field) {
    return overrideEditVersions.get(overrideKey(agentId, field)) ?? 0;
  }

  // Clear is an explicit intent in the same participant as ordinary edits.
  // Its click-time edit version survives a write already running, whose
  // normalization must not count as a newer edit that cancels the reset.
  function requestOverrideReset(agentId, field) {
    if (!selectedProject() || state.pendingOverrideReset) return false;
    state.pendingOverrideReset = {
      agentId,
      field,
      editVersion: overrideEditVersion(agentId, field),
    };
    return true;
  }

  function isOverrideBusy(agentId, field) {
    return state.overrideBusyKey === overrideKey(agentId, field);
  }

  function overrideValueForField(agentId, field) {
    const draft = overrideDraft(agentId);
    if (field === 'model') {
      return draft.model.trim();
    }
    if (field === 'temperature' || field === 'top_p') {
      return normalizeOverrideNumber(draft[field]);
    }
    if (field === 'compaction_policy') {
      return draft.compaction_policy;
    }
    if (field === 'tool_access') {
      return normalizeToolAccess(draft.tool_access);
    }
    if (field === 'tool_loading') {
      return normalizeToolLoading(draft.tool_loading);
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
    if (field === 'temperature' || field === 'top_p') {
      return normalizeOverrideNumber(draft[field]) !== null;
    }
    if (field === 'compaction_policy') {
      return draft.compaction_policy !== null;
    }
    if (field === 'tool_access') {
      return draft.tool_access !== null;
    }
    if (field === 'tool_loading') {
      return normalizeToolLoading(draft.tool_loading) !== null;
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
    const reset = state.pendingOverrideReset;
    if (reset) {
      if (
        !(await clearMemberOverride(
          reset.agentId,
          reset.field,
          reset.editVersion,
        ))
      )
        return false;
      if (state.pendingOverrideReset === reset)
        state.pendingOverrideReset = null;
    }
    // Keep only field identities across awaits. A previous response can
    // update inheritance, and the user can edit a later field meanwhile.
    for (const { agentId, field } of pendingOverrideChanges()) {
      if (
        (state.pendingOverrideReset?.agentId === agentId &&
          state.pendingOverrideReset.field === field) ||
        !pendingOverrideChanges().some(
          (change) => change.agentId === agentId && change.field === field,
        )
      )
        continue;
      if (isClearedOverrideDraft(agentId, field)) {
        if (!(await clearMemberOverride(agentId, field))) return false;
        continue;
      }
      if (!canSetOverride(agentId, field)) {
        state.editError = t('errors.validation');
        return false;
      }
      if (!(await setMemberOverride(agentId, field))) return false;
    }
    return true;
  }

  // An emptied sampling box and On-demand Tools switched off without an
  // explicit Always loaded list mean "no override": saving them clears the
  // override. Switched off with a list, the override keeps the list.
  function isClearedOverrideDraft(agentId, field) {
    const draft = overrideDraft(agentId);
    if (field === 'tool_loading') {
      return normalizeToolLoading(draft.tool_loading) === null;
    }
    return (
      (field === 'temperature' || field === 'top_p') &&
      String(draft[field] ?? '').trim() === ''
    );
  }

  // A save or reset advances the scan's baseline, but the edited field may
  // already hold a newer value. Capture it before applyScan: a newer value
  // equal to the old baseline would otherwise look untouched there.
  function applyOverrideScan(scan, agentId, field, editVersion) {
    const latestValue = state.overrideDrafts[agentId]?.[field];
    const hasNewerEdit = overrideEditVersion(agentId, field) !== editVersion;
    applyScan(scan);
    const member = state.activeTeam.find((entry) => entry.agent_id === agentId);
    if (!member || !state.overrideDrafts[agentId]) return;
    state.overrideDrafts = {
      ...state.overrideDrafts,
      [agentId]: {
        ...state.overrideDrafts[agentId],
        [field]: hasNewerEdit
          ? latestValue
          : seedTeamOverrideDraft(member)[field],
      },
    };
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
    const selection = selectionVersion;
    const editVersion = overrideEditVersion(agentId, field);
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
      if (!active || selection !== selectionVersion) {
        return false;
      }
      applyOverrideScan(result?.scan, agentId, field, editVersion);
      return true;
    } catch (error) {
      if (active && selection === selectionVersion) {
        onToast({
          title: `${t('projects.team.overrideError')} ${errorText(error)}`,
          variant: 'error',
          sticky: true,
        });
      }
      return false;
    } finally {
      if (active && selection === selectionVersion) {
        state.overrideBusyKey = '';
        flushPendingProjects();
      }
    }
  }

  async function clearMemberOverride(
    agentId,
    field,
    editVersion = overrideEditVersion(agentId, field),
  ) {
    const project = selectedProject();
    if (!project || state.overrideBusyKey) {
      return false;
    }
    if (field === 'tool_access') {
      clearToolAccessOverrideAutoSave({ flushPending: false });
    }
    const selection = selectionVersion;
    state.overrideBusyKey = overrideKey(agentId, field);
    state.editError = '';
    try {
      const result = await operations.clearOverride(
        project.project_id,
        agentId,
        field,
      );
      if (!active || selection !== selectionVersion) {
        return false;
      }
      applyOverrideScan(result?.scan, agentId, field, editVersion);
      onToast({
        title: t('projects.team.overrideCleared'),
        variant: 'success',
      });
      return true;
    } catch (error) {
      if (active && selection === selectionVersion) {
        onToast({
          title: `${t('projects.team.overrideClearError')} ${errorText(error)}`,
          variant: 'error',
          sticky: true,
        });
      }
      return false;
    } finally {
      if (active && selection === selectionVersion) {
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
    requestOverrideReset,
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
