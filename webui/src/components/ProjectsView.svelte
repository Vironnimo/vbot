<script>
  import { onDestroy, onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import ToggleChipList from './ui/ToggleChipList.svelte';
  import {
    addProject,
    clearOverride,
    detectProject,
    listProjects,
    removeProject,
    rpc,
    setOverride,
    setProject,
    showProject,
  } from '$lib/api.js';
  import {
    PROJECT_SOURCE_FORMATS,
    PROJECT_THINKING_EFFORT_NO_DEFAULT,
    PROJECT_THINKING_EFFORT_OPTIONS,
    buildAddProjectPayload,
    buildDefaultAgentOptions,
    buildManageProjectPayload,
    buildRePointPayload,
    buildSkillToggleSections,
    buildToolToggleList,
    hasManageChanges,
    memberFieldIsOverridden,
    needsRePoint,
    normalizeDetectResult,
    normalizeOverrideTemperature,
    normalizeProject,
    normalizeProjects,
    normalizeScanReport,
    normalizeScanSkills,
    presentFormats,
    projectTeam,
    seedTeamOverrideDraft,
    setListMembership,
    shouldSuggestClaudeMd,
  } from '$lib/projectsView.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import {
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import InfoHint from './ui/InfoHint.svelte';

  const PROJECT_BUSY_CODE = 'project_busy';
  const PROJECT_IN_USE_CODE = 'project_in_use';
  // The project settings form is a settings-style surface, so it follows the
  // shared save model (DESIGN.md → Save model): auto-save after a short idle,
  // plus the explicit Save button for users who prefer to commit manually.
  const AUTO_SAVE_DEBOUNCE_MS = 800;
  // Idle delay before the add dialog probes a typed repository path with
  // project.detect (per-format agent/skill counts drive the format choice).
  const ADD_DETECT_DEBOUNCE_MS = 500;

  // Human-facing labels for the source-format vocabulary.
  const FORMAT_LABELS = Object.freeze({
    opencode: () => t('projects.format.opencode', 'OpenCode'),
    claude: () => t('projects.format.claude', 'Claude Code'),
  });

  function formatLabel(formatKey) {
    return FORMAT_LABELS[formatKey] ? FORMAT_LABELS[formatKey]() : formatKey;
  }

  // Maps each effective/override field to its section-header label key and the empty
  // wording (a null model reads "not configured", a null temperature/thinking
  // reads "provider default").
  const EFFECTIVE_FIELD_META = Object.freeze({
    model: {
      labelKey: 'projects.team.effectiveModel',
      labelFallback: 'Model',
      emptyKey: 'projects.team.valueNotConfigured',
      emptyFallback: 'not configured',
    },
    temperature: {
      labelKey: 'projects.team.effectiveTemperature',
      labelFallback: 'Temperature',
      emptyKey: 'projects.team.valueProviderDefault',
      emptyFallback: 'provider default',
    },
    thinking_effort: {
      labelKey: 'projects.team.effectiveThinkingEffort',
      labelFallback: 'Thinking effort',
      emptyKey: 'projects.team.valueProviderDefault',
      emptyFallback: 'provider default',
    },
  });

  const noop = () => {};

  let {
    onToast = noop,
    onNavigateToSettingsPanel = noop,
    modelsRefreshToken = 0,
  } = $props();

  let projects = $state([]);
  let loadingProjects = $state(false);
  let listError = $state('');
  let statusMessage = $state('');

  // Model/connection catalogs feed the default-model / model-override searchable
  // dropdowns (the same picker the Agents tab uses, see modelSelection.js).
  let availableModels = $state([]);
  let availableConnections = $state([]);
  // A live model reload fetches in the background but holds the visible option
  // swap while a picker is open, so an open selection is never disturbed.
  let modelDropdownOpenCount = $state(0);
  let pendingModelCatalogs = null;
  let lastModelsRefreshToken = null;

  // The global agent defaults (`settings.get` → `defaults.agent`), fetched once
  // when the view loads so the project-default inherit options can name the
  // global default. Empty object on failure → the absent-case labels render.
  let globalAgentDefaults = $state({});
  let globalCompactionPolicy = $state(null);

  // Add modal state — the popup needs only the repo path plus an optional
  // display name (blank → backend derives the name from the folder).
  let isAddOpen = $state(false);
  let addForm = $state(createAddForm());
  let addingProject = $state(false);
  let addError = $state('');
  // The debounced project.detect result for the typed path (null until the
  // first probe answers). Drives the format choice and the CLAUDE.md suggestion.
  let addDetect = $state(null);
  let addDetectTimer = null;
  const addFormatsPresent = $derived(
    addDetect ? presentFormats(addDetect) : [],
  );
  // Both formats found → the informed radio choice; exactly one → a quiet
  // "Detected" line (the server auto-detects the same); none → silent default.
  const addShowsFormatChoice = $derived(addFormatsPresent.length > 1);
  const addDetectedFormat = $derived(
    addFormatsPresent.length === 1 ? addFormatsPresent[0] : '',
  );
  const addSuggestsClaudeMd = $derived(
    addDetect !== null && shouldSuggestClaudeMd(addDetect),
  );

  // The selected project drives the detail pane. Its settings form, scanned
  // team, report, and skill pool are held here and reset on selection change.
  let selectedProjectId = $state('');
  let editForm = $state(createEditForm());
  let editSaving = $state(false);
  let editError = $state('');
  // The draft text for the auto-load "add a file" input, kept apart from editForm
  // so typing a candidate path does not mark the form dirty until it is added.
  let autoLoadDraft = $state('');
  let activeTeam = $state([]);
  let activeReport = $state(null);
  let activeScanSkills = $state({ project: [], bundled: [], global: [] });
  let scanLoading = $state(false);
  let scanRefreshSection = $state('');
  let removingProjectId = $state('');
  // The project awaiting remove confirmation (null = dialog closed).
  let removeConfirmProject = $state(null);

  // Which team-member rows are expanded (keyed agent id → true), plus each
  // member's override draft and the in-flight override field, so a row's expand state
  // and controls persist while the detail is open. Reset when the project changes.
  let expandedMembers = $state({});
  let overrideDrafts = $state({});
  // The `${agentId}:${field}` currently being written, so its buttons disable.
  let overrideBusyKey = $state('');

  // The toggleable tool catalog and the base Tool Whitelist (reset target), both
  // from the tool-catalog RPC so new tools appear without hardcoding names.
  let toolCatalog = $state([]);
  let defaultProjectTools = $state([]);

  // Re-point modal state (a project whose cwd_exists === false).
  let rePointProject = $state(null);
  let rePointCwd = $state('');
  let rePointing = $state(false);
  let rePointError = $state('');

  let destroyed = false;
  let listRequestId = 0;
  let scanRequestId = 0;
  let autoSaveTimer = null;

  let hasProjects = $derived(projects.length > 0);
  let canSubmitAdd = $derived(addForm.cwd.trim().length > 0 && !addingProject);

  let selectedProject = $derived(
    projects.find((item) => item.project_id === selectedProjectId) ?? null,
  );

  let showAllModels = $state(false);
  // Per-member reveal state for the override pickers, keyed by agent id.
  let showAllOverrideModels = $state({});
  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: editForm.default_model,
      emptyLabel: defaultModelInheritLabel(),
      translate: t,
    }),
  );
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: editForm.default_model,
    }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
    }),
  );
  let modelSelectValue = $derived(
    selectModelValue(editForm.default_model, modelOptions),
  );
  // The per-project source-format selector (exactly one format per project).
  let sourceFormatOptions = $derived(
    PROJECT_SOURCE_FORMATS.map((formatKey) => ({
      value: formatKey,
      label: formatLabel(formatKey),
    })),
  );

  let agentOptions = $derived(
    buildDefaultAgentOptions({
      team: activeTeam,
      currentValue: editForm.default_agent,
      emptyLabel: t('projects.manage.defaultAgentEmpty', 'No project default'),
      unavailableLabel: (agentId) =>
        t(
          'projects.manage.defaultAgentUnavailable',
          '{agentId} (not in team)',
          { agentId },
        ),
    }),
  );

  // The project default thinking-effort options: the "no default" sentinel and
  // the inherit ('') choice wrap the shared effort ladder, reusing the agent
  // effort-level labels so there is no duplicate label catalog.
  let thinkingEffortOptions = $derived([
    {
      value: PROJECT_THINKING_EFFORT_NO_DEFAULT,
      label: t('projects.manage.noThinkingEffort', 'No project default'),
    },
    {
      value: '',
      label: defaultThinkingEffortInheritLabel(),
    },
    ...PROJECT_THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: t(`agents.form.thinkingEffortOption.${option}`, option),
    })),
  ]);

  // The sparse project.set changes the open form represents versus the saved
  // project — empty when the form matches what the server already holds.
  let pendingChanges = $derived(
    selectedProject
      ? buildManageProjectPayload(
          {
            display_name: editForm.display_name,
            default_agent: editForm.default_agent,
            default_model: editForm.default_model,
            default_temperature: editForm.default_temperature,
            default_thinking_effort: editForm.default_thinking_effort,
            auto_load: editForm.auto_load,
            allowed_tools: editForm.allowed_tools,
            skills_bundled_enabled: editForm.skills_bundled_enabled,
            skills_global_enabled: editForm.skills_global_enabled,
            skills_project_disabled: editForm.skills_project_disabled,
          },
          selectedProject,
        )
      : {},
  );
  let saveDisabled = $derived(editSaving || !hasManageChanges(pendingChanges));

  let temperatureIsInherit = $derived(editForm.default_temperature === '');

  let toolToggleRows = $derived(
    buildToolToggleList({
      catalog: toolCatalog,
      allowedTools: editForm.allowed_tools,
    }),
  );
  let skillToggleSections = $derived(
    buildSkillToggleSections({
      projectSkills: activeScanSkills.project,
      bundledSkills: activeScanSkills.bundled,
      globalSkills: activeScanSkills.global,
      skillsBundledEnabled: editForm.skills_bundled_enabled,
      skillsGlobalEnabled: editForm.skills_global_enabled,
      skillsProjectDisabled: editForm.skills_project_disabled,
    }),
  );
  // The shared chip list keys off `allowed`; the toggle builders track it as
  // `enabled`, so map it across for each list.
  let toolChipItems = $derived(
    toolToggleRows.map((tool) => ({ ...tool, allowed: tool.enabled })),
  );
  let projectSkillChips = $derived(
    skillToggleSections.project.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );
  let bundledSkillChips = $derived(
    skillToggleSections.bundled.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );
  let globalSkillChips = $derived(
    skillToggleSections.global.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );

  onMount(() => {
    void loadCatalogs();
    void loadGlobalDefaults();
    void loadProjects();

    return () => {
      destroyed = true;
    };
  });

  onDestroy(() => {
    clearAutoSaveTimer();
    clearAddDetectTimer();
  });

  // Auto-save the settings form once it has been idle for the debounce window.
  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveSelectedProject();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  // Reload the model catalog when the generic invalidation channel signals a
  // model/provider change (first run is a no-op: mount already loaded).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reloadModelCatalogs();
    }
  });

  function createAddForm() {
    return {
      cwd: '',
      display_name: '',
      // The explicit format pick when both formats are present (radio default:
      // opencode) and the CLAUDE.md suggestion checkbox (opt-in, off).
      source_format: 'opencode',
      include_claude_md: false,
    };
  }

  function createEditForm(project = null) {
    return {
      display_name: project?.display_name ?? '',
      default_agent: project?.default_agent ?? '',
      default_model: project?.default_model ?? '',
      source_format: project?.source_format ?? 'opencode',
      default_temperature: seedProjectTemperature(project?.default_temperature),
      default_thinking_effort: seedProjectThinkingEffort(
        project?.default_thinking_effort,
      ),
      auto_load: [...(project?.auto_load ?? [])],
      allowed_tools: [...(project?.allowed_tools ?? [])],
      skills_bundled_enabled: [...(project?.skills_bundled_enabled ?? [])],
      skills_global_enabled: [...(project?.skills_global_enabled ?? [])],
      skills_project_disabled: [...(project?.skills_project_disabled ?? [])],
    };
  }

  // number → text box; null/absent → empty box ("no project default").
  function seedProjectTemperature(value) {
    return typeof value === 'number' ? String(value) : '';
  }

  // null/absent → the "no default" sentinel; '' (provider default) and a level
  // seed verbatim so the dropdown shows the stored choice.
  function seedProjectThinkingEffort(value) {
    if (value === null || value === undefined) {
      return PROJECT_THINKING_EFFORT_NO_DEFAULT;
    }
    return value;
  }

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  // The inherit-option label for the project default-model select. A present
  // global default names its value; an absent one shows the not-configured copy.
  function defaultModelInheritLabel() {
    const value = globalDefaultText('model');
    if (value) {
      return t('inherit.option', 'Inherited: {value} (global default)', {
        value,
      });
    }
    return t('inherit.optionNotConfigured', 'Inherit (not configured)');
  }

  // The inherit ('') label for the project default thinking-effort select.
  function defaultThinkingEffortInheritLabel() {
    const value = globalDefaultText('thinking_effort');
    if (value) {
      return t('inherit.option', 'Inherited: {value} (global default)', {
        value,
      });
    }
    return t('inherit.optionProviderDefault', 'Inherit (provider default)');
  }

  function globalDefaultText(fieldName) {
    const raw =
      globalAgentDefaults && typeof globalAgentDefaults === 'object'
        ? globalAgentDefaults[fieldName]
        : null;
    if (raw === null || raw === undefined) {
      return '';
    }
    return String(raw).trim();
  }

  async function loadGlobalDefaults() {
    try {
      const result = await rpc('settings.get');
      if (destroyed) {
        return;
      }
      const defaults = result?.defaults?.agent;
      globalAgentDefaults =
        defaults && typeof defaults === 'object' ? defaults : {};
      globalCompactionPolicy = result?.compaction ?? null;
    } catch {
      globalAgentDefaults = {};
      globalCompactionPolicy = null;
    }
  }

  async function fetchModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
      ]);
      if (destroyed) {
        return null;
      }
      return {
        models: Array.isArray(modelsResult?.models) ? modelsResult.models : [],
        connections: Array.isArray(connectionsResult?.connections)
          ? connectionsResult.connections
          : [],
      };
    } catch {
      // A missing model catalog only degrades the model pickers (they still list
      // the empty option); it must not block the projects list itself.
      return null;
    }
  }

  function applyModelCatalogs(catalogs) {
    availableModels = catalogs.models;
    availableConnections = catalogs.connections;
    pendingModelCatalogs = null;
  }

  async function loadCatalogs() {
    const catalogs = await fetchModelCatalogs();
    if (catalogs) {
      applyModelCatalogs(catalogs);
    }
    await loadToolCatalog();
  }

  // The tool catalog feeds the Tool Whitelist toggle list and its reset target. A
  // failure only degrades that one section (the toggles render empty), so it never
  // blocks the projects list.
  async function loadToolCatalog() {
    try {
      const result = await rpc('tool.list');
      if (destroyed) {
        return;
      }
      toolCatalog = Array.isArray(result?.tools) ? result.tools : [];
      defaultProjectTools = Array.isArray(result?.default_project_tools)
        ? result.default_project_tools
        : [];
    } catch {
      toolCatalog = [];
      defaultProjectTools = [];
    }
  }

  async function reloadModelCatalogs() {
    const catalogs = await fetchModelCatalogs();
    if (!catalogs) {
      return;
    }
    if (
      shouldApplyReloadNow(SURFACE_FORM, {
        dropdownOpen: modelDropdownOpenCount > 0,
      })
    ) {
      applyModelCatalogs(catalogs);
    } else {
      pendingModelCatalogs = catalogs;
    }
  }

  function trackModelDropdownOpen(open) {
    modelDropdownOpenCount = Math.max(
      0,
      modelDropdownOpenCount + (open ? 1 : -1),
    );
    if (modelDropdownOpenCount === 0 && pendingModelCatalogs) {
      applyModelCatalogs(pendingModelCatalogs);
    }
  }

  async function loadProjects() {
    const requestId = listRequestId + 1;
    listRequestId = requestId;
    loadingProjects = true;
    listError = '';

    try {
      const result = await listProjects();
      if (destroyed || requestId !== listRequestId) {
        return;
      }
      projects = normalizeProjects(result?.projects);
    } catch (error) {
      if (destroyed || requestId !== listRequestId) {
        return;
      }
      listError = `${t('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
    } finally {
      if (!destroyed && requestId === listRequestId) {
        loadingProjects = false;
      }
    }
  }

  function openAdd() {
    addForm = createAddForm();
    addError = '';
    addDetect = null;
    isAddOpen = true;
  }

  function closeAdd() {
    if (addingProject) {
      return;
    }
    isAddOpen = false;
    addError = '';
    clearAddDetectTimer();
  }

  function updateAddField(field, value) {
    addForm[field] = value;
    addError = '';
    if (field === 'cwd') {
      scheduleAddDetect(value);
    }
  }

  function clearAddDetectTimer() {
    if (addDetectTimer !== null) {
      clearTimeout(addDetectTimer);
      addDetectTimer = null;
    }
  }

  // Probe the typed path after a short idle. Detection is advisory only: a
  // failed/void probe just leaves the dialog without a format choice (the
  // server auto-detects at add time anyway), so errors are swallowed.
  function scheduleAddDetect(cwd) {
    clearAddDetectTimer();
    const trimmed = cwd.trim();
    if (trimmed.length === 0) {
      addDetect = null;
      return;
    }
    addDetectTimer = setTimeout(() => {
      addDetectTimer = null;
      void runAddDetect(trimmed);
    }, ADD_DETECT_DEBOUNCE_MS);
  }

  async function runAddDetect(cwd) {
    try {
      const result = await detectProject(cwd);
      if (destroyed || !isAddOpen || addForm.cwd.trim() !== cwd) {
        return;
      }
      addDetect = normalizeDetectResult(result);
    } catch {
      if (!destroyed) {
        addDetect = null;
      }
    }
  }

  async function submitAdd(event) {
    event.preventDefault();
    if (addForm.cwd.trim().length === 0) {
      addError = t(
        'projects.add.missingCwd',
        'Enter a repository path to add a project.',
      );
      return;
    }

    addingProject = true;
    addError = '';
    statusMessage = '';

    try {
      const payload = buildAddProjectPayload({
        cwd: addForm.cwd,
        display_name: addForm.display_name,
        // Only an actual choice is sent: with both formats present the radio
        // decides; otherwise the field is omitted and the server auto-detects.
        source_format: addShowsFormatChoice ? addForm.source_format : '',
        // The accepted CLAUDE.md suggestion becomes a normal auto_load entry
        // (loads verbatim; the backend seeds AGENTS.md in front of it).
        auto_load:
          addSuggestsClaudeMd && addForm.include_claude_md ? ['CLAUDE.md'] : [],
      });
      const result = await addProject(payload);
      if (destroyed) {
        return;
      }
      const project = normalizeProject(result?.project);
      statusMessage = t('projects.add.success', 'Project added.');
      isAddOpen = false;
      addForm = createAddForm();
      addDetect = null;
      await loadProjects();
      if (!destroyed) {
        // Select the freshly added project so its scan (team + report) is the
        // review surface right away (add-then-review, no dry-run).
        selectProject(project.project_id, result?.scan);
      }
    } catch (error) {
      if (destroyed) {
        return;
      }
      addError = `${t('projects.add.error', 'Project could not be added.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        addingProject = false;
      }
    }
  }

  // Select a project for the detail pane. When a scan is already in hand (right
  // after add) it seeds the team/report immediately; otherwise it fetches one.
  function selectProject(projectId, scan = null) {
    const project =
      projects.find((item) => item.project_id === projectId) ?? null;
    selectedProjectId = projectId;
    editForm = createEditForm(project);
    autoLoadDraft = '';
    editError = '';
    activeTeam = [];
    activeReport = null;
    activeScanSkills = { project: [], bundled: [], global: [] };
    expandedMembers = {};
    overrideDrafts = {};
    overrideBusyKey = '';

    if (scan) {
      applyScan(scan);
      return;
    }

    void loadScan(projectId);
  }

  function applyScan(scan) {
    activeTeam = projectTeam(scan);
    activeReport = normalizeScanReport(scan?.report);
    activeScanSkills = normalizeScanSkills(scan);
    seedOverrideDrafts();
  }

  // Seed each team member's override draft from its current override/effective values,
  // only for members that have no draft yet (an open control's typed text is kept).
  function seedOverrideDrafts() {
    const next = { ...overrideDrafts };
    for (const member of activeTeam) {
      if (!next[member.agent_id]) {
        next[member.agent_id] = seedTeamOverrideDraft(member);
      }
    }
    overrideDrafts = next;
  }

  async function loadScan(projectId) {
    const requestId = scanRequestId + 1;
    scanRequestId = requestId;
    scanLoading = true;

    try {
      const result = await showProject(projectId);
      if (destroyed || requestId !== scanRequestId) {
        return;
      }
      applyScan(result?.scan);
    } catch (error) {
      if (destroyed || requestId !== scanRequestId) {
        return;
      }
      editError = `${t('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
    } finally {
      if (!destroyed && requestId === scanRequestId) {
        scanLoading = false;
      }
    }
  }

  // Team and Skills are two views of the same live project scan. Keep the
  // actions where users look for them, while preserving one authoritative
  // refresh path for repository agents, project skills, and global skills.
  async function refreshScan(section) {
    if (!selectedProjectId || scanLoading) {
      return;
    }
    scanRefreshSection = section;
    await loadScan(selectedProjectId);
    if (!destroyed) {
      scanRefreshSection = '';
    }
  }

  function updateEditField(field, value) {
    editForm[field] = value;
    editError = '';
  }

  function toggleTool(name, enabled) {
    editForm.allowed_tools = setListMembership(
      editForm.allowed_tools,
      name,
      enabled,
    );
    editError = '';
  }

  // A not-ready tool row's "Open Extensions" link jumps to Settings → Extensions.
  function navigateToExtensions(_extensionName) {
    onNavigateToSettingsPanel('extensions');
  }

  function navigateToAgentDefaults() {
    onNavigateToSettingsPanel('defaults');
  }

  function toggleProjectSkill(name, active) {
    editForm.skills_project_disabled = setListMembership(
      editForm.skills_project_disabled,
      name,
      !active,
    );
    editError = '';
  }

  function toggleBundledSkill(name, enabled) {
    editForm.skills_bundled_enabled = setListMembership(
      editForm.skills_bundled_enabled,
      name,
      enabled,
    );
    editError = '';
  }

  function toggleGlobalSkill(name, enabled) {
    editForm.skills_global_enabled = setListMembership(
      editForm.skills_global_enabled,
      name,
      enabled,
    );
    editError = '';
  }

  // Reset the Tool Whitelist to the base list (the server-provided default).
  function resetToolsToDefaults() {
    editForm.allowed_tools = [...defaultProjectTools];
    editError = '';
  }

  // Bulk "all on / all off" for each whitelist list. Tools and bundled/global
  // skills store the enabled names; project skills store the *disabled* names,
  // so all-on clears the disabled list and all-off names every project skill.
  function setAllTools(enabled) {
    editForm.allowed_tools = enabled
      ? toolToggleRows.map((tool) => tool.name)
      : [];
    editError = '';
  }

  function setAllProjectSkills(enabled) {
    editForm.skills_project_disabled = enabled
      ? []
      : skillToggleSections.project.map((skill) => skill.name);
    editError = '';
  }

  function setAllBundledSkills(enabled) {
    editForm.skills_bundled_enabled = enabled
      ? skillToggleSections.bundled.map((skill) => skill.name)
      : [];
    editError = '';
  }

  function setAllGlobalSkills(enabled) {
    editForm.skills_global_enabled = enabled
      ? skillToggleSections.global.map((skill) => skill.name)
      : [];
    editError = '';
  }

  function updateModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    editForm.default_model = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
    editError = '';
  }

  function clearDefaultTemperature() {
    updateEditField('default_temperature', '');
  }

  // Explicit Save button / form submit. On a clean form it confirms trust with
  // the shared "Already saved" toast instead of a no-op request (DESIGN.md →
  // Save model); otherwise it pre-empts the pending debounce and saves now.
  function handleManualSave(event) {
    event.preventDefault();
    if (editSaving) {
      return;
    }
    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }
    clearAutoSaveTimer();
    void saveSelectedProject();
  }

  // Persist the settings form's pending changes. Shared by the debounced
  // auto-save and the explicit Save button; both target the selected project and
  // re-seed the panel from the saved state so the form reads as clean afterwards.
  async function saveSelectedProject() {
    const project = selectedProject;
    if (!project || editSaving) {
      return;
    }

    const changes = pendingChanges;
    if (!hasManageChanges(changes)) {
      return;
    }

    editSaving = true;
    editError = '';
    statusMessage = '';

    try {
      const result = await setProject(project.project_id, changes);
      if (destroyed) {
        return;
      }
      await loadProjects();
      if (destroyed) {
        return;
      }
      const saved = normalizeProject(result?.project);
      editForm = createEditForm(saved);
      applyScan(result?.scan);
      onToast({
        title: t('projects.manage.saveSuccess', 'Project updated.'),
        variant: 'success',
      });
    } catch (error) {
      if (destroyed) {
        return;
      }
      editError = `${t('projects.manage.saveError', 'Project changes could not be saved.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        editSaving = false;
      }
    }
  }

  // ── Team rows ──────────────────────────────────────────────────────────────

  function toggleMember(agentId) {
    expandedMembers = {
      ...expandedMembers,
      [agentId]: !expandedMembers[agentId],
    };
  }

  function overrideDraft(agentId) {
    return (
      overrideDrafts[agentId] ?? {
        model: '',
        temperature: '',
        thinking_effort: '',
        compaction_policy: null,
      }
    );
  }

  function updateOverrideDraft(agentId, field, value) {
    overrideDrafts = {
      ...overrideDrafts,
      [agentId]: { ...overrideDraft(agentId), [field]: value },
    };
    editError = '';
  }

  function updateOverrideModelSelection(agentId, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    updateOverrideDraft(
      agentId,
      'model',
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  // The effective-value display: the label, the value text (with the null-case
  // wording), and the source label — for one field of one member.
  function effectiveDisplay(member, field) {
    const meta = EFFECTIVE_FIELD_META[field];
    const entry = member?.effective?.[field] ?? { value: null, source: null };
    const isEmpty = entry.value === null || entry.value === undefined;
    return {
      label: t(meta.labelKey, meta.labelFallback),
      value: isEmpty
        ? t(meta.emptyKey, meta.emptyFallback)
        : String(entry.value),
      isEmpty,
      sourceLabel: sourceLabel(entry.source),
    };
  }

  function sourceLabel(source) {
    switch (source) {
      case 'override':
        return t('projects.team.sourceOverride', 'override');
      case 'agent':
        return t('projects.team.sourceAgentFile', 'agent file (repo)');
      case 'project_default':
        return t('projects.team.sourceProjectDefault', 'project default');
      case 'global_default':
        return t('projects.team.sourceGlobalDefault', 'global default');
      default:
        return '';
    }
  }

  // The thinking-effort override options, gated by the member's effective model via
  // the shared agentForm helpers (mirrors the agent editor). The empty option is
  // the "provider default" override value.
  function overrideEffortOptions(member) {
    const reasoning = reasoningForModelValue(
      overrideDraft(member.agent_id).model ||
        member?.effective?.model?.value ||
        '',
      availableModels,
    );
    return effortOptionsForReasoning(reasoning).map((option) => ({
      value: option,
      label:
        option === ''
          ? t(
              'projects.manage.providerThinkingEffortDefault',
              '— (provider default)',
            )
          : t(`agents.form.thinkingEffortOption.${option}`, option),
    }));
  }

  // The model-override picker options for one member (its own draft as the selected
  // value so a saved/overridden value stays visible even if unavailable).
  function overrideModelOptions(member) {
    const selectedModelValue = overrideDraft(member.agent_id).model;
    return filterModelSelectOptions(allOverrideModelOptions(member), {
      showAll: Boolean(showAllOverrideModels[member.agent_id]),
      selectedModelValue,
    });
  }

  function allOverrideModelOptions(member) {
    return buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: overrideDraft(member.agent_id).model,
      emptyLabel: t('projects.team.overrideModelPlaceholder', 'No override'),
      translate: t,
    });
  }

  function overrideModelFilterFooter(member) {
    const showAll = Boolean(showAllOverrideModels[member.agent_id]);
    return modelFilterFooterLabel({
      showAll,
      hiddenCount:
        allOverrideModelOptions(member).length -
        overrideModelOptions(member).length,
      translate: t,
    });
  }

  function toggleShowAllOverrideModels(agentId) {
    showAllOverrideModels = {
      ...showAllOverrideModels,
      [agentId]: !showAllOverrideModels[agentId],
    };
  }

  function overrideKey(agentId, field) {
    return `${agentId}:${field}`;
  }

  function isOverrideBusy(agentId, field) {
    return overrideBusyKey === overrideKey(agentId, field);
  }

  // Whether the Set-override button is enabled: not busy and the draft carries a real
  // value for the field (an override must have a value; clearing is a separate action).
  function canSetOverride(agentId, field) {
    if (overrideBusyKey) {
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
    // thinking_effort: a level or '' (provider default) is a valid override value.
    return typeof draft.thinking_effort === 'string';
  }

  // The value sent to project.set_override for a field, from that field's draft.
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

  async function applySetOverride(agentId, field) {
    const project = selectedProject;
    if (!project || overrideBusyKey || !canSetOverride(agentId, field)) {
      return;
    }

    overrideBusyKey = overrideKey(agentId, field);
    editError = '';

    try {
      const result = await setOverride(
        project.project_id,
        agentId,
        field,
        overrideValueForField(agentId, field),
      );
      if (destroyed) {
        return;
      }
      refreshTeamFromScan(result?.scan);
      onToast({
        title: t('projects.team.overrideSaved', 'Override saved.'),
        variant: 'success',
      });
    } catch (error) {
      if (destroyed) {
        return;
      }
      onToast({
        title: `${t('projects.team.overrideError', 'The override could not be saved.')} ${errorText(error)}`,
        variant: 'error',
        sticky: true,
      });
    } finally {
      if (!destroyed) {
        overrideBusyKey = '';
      }
    }
  }

  async function applyClearOverride(agentId, field) {
    const project = selectedProject;
    if (!project || overrideBusyKey) {
      return;
    }

    overrideBusyKey = overrideKey(agentId, field);
    editError = '';

    try {
      const result = await clearOverride(project.project_id, agentId, field);
      if (destroyed) {
        return;
      }
      refreshTeamFromScan(result?.scan);
      onToast({
        title: t('projects.team.overrideCleared', 'Override cleared.'),
        variant: 'success',
      });
    } catch (error) {
      if (destroyed) {
        return;
      }
      onToast({
        title: `${t('projects.team.overrideClearError', 'The override could not be cleared.')} ${errorText(error)}`,
        variant: 'error',
        sticky: true,
      });
    } finally {
      if (!destroyed) {
        overrideBusyKey = '';
      }
    }
  }

  // Re-seed the team/report/skills from an override RPC's returned scan, then refresh
  // the affected members' override drafts so the controls reflect the new state.
  function refreshTeamFromScan(scan) {
    activeTeam = projectTeam(scan);
    activeReport = normalizeScanReport(scan?.report);
    activeScanSkills = normalizeScanSkills(scan);
    const next = {};
    for (const member of activeTeam) {
      next[member.agent_id] = seedTeamOverrideDraft(member);
    }
    overrideDrafts = next;
  }

  // ── Remove / re-point ────────────────────────────────────────────────────

  function removeOne(project) {
    removeConfirmProject = project;
  }

  function cancelRemove() {
    removeConfirmProject = null;
  }

  async function confirmRemove() {
    const project = removeConfirmProject;
    removeConfirmProject = null;
    if (!project) {
      return;
    }

    removingProjectId = project.project_id;
    statusMessage = '';
    listError = '';
    editError = '';

    try {
      await removeProject(project.project_id);
      if (destroyed) {
        return;
      }
      if (selectedProjectId === project.project_id) {
        selectedProjectId = '';
        activeTeam = [];
        activeReport = null;
        activeScanSkills = { project: [], bundled: [], global: [] };
      }
      statusMessage = t('projects.remove.success', 'Project removed.');
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      const message = removeErrorText(error);
      if (selectedProjectId === project.project_id) {
        editError = message;
      } else {
        listError = message;
      }
    } finally {
      if (!destroyed) {
        removingProjectId = '';
      }
    }
  }

  function openRePoint(project) {
    rePointProject = project;
    rePointCwd = '';
    rePointError = '';
  }

  function closeRePoint() {
    if (rePointing) {
      return;
    }
    rePointProject = null;
    rePointError = '';
  }

  async function submitRePoint(event) {
    event.preventDefault();
    if (!rePointProject) {
      return;
    }
    if (rePointCwd.trim().length === 0) {
      rePointError = t(
        'projects.rePoint.missingCwd',
        'Enter the new repository path.',
      );
      return;
    }

    rePointing = true;
    rePointError = '';
    statusMessage = '';

    try {
      const projectId = rePointProject.project_id;
      const result = await setProject(
        projectId,
        buildRePointPayload(rePointCwd),
      );
      if (destroyed) {
        return;
      }
      statusMessage = t('projects.rePoint.success', 'Project re-pointed.');
      rePointProject = null;
      await loadProjects();
      if (!destroyed && selectedProjectId === projectId) {
        selectProject(projectId, result?.scan);
      }
    } catch (error) {
      if (destroyed) {
        return;
      }
      rePointError = `${t('projects.rePoint.error', 'The project could not be re-pointed.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        rePointing = false;
      }
    }
  }

  function groupLabel(type) {
    return t(`projects.report.group.${type}`, type);
  }

  function removeErrorText(error) {
    if (error?.code === PROJECT_BUSY_CODE) {
      return t(
        'projects.remove.busy',
        'This project has an active or queued run and cannot be removed right now.',
      );
    }
    if (error?.code === PROJECT_IN_USE_CODE) {
      return t(
        'projects.remove.inUse',
        'A cron job points at one of this project’s agents, so it cannot be removed. Remove or retarget the cron job first.',
      );
    }
    return `${t('projects.remove.error', 'Project could not be removed.')} ${errorText(error)}`;
  }

  function errorText(error) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }
    return t('common.unknown', 'Unknown');
  }

  function addAutoLoadEntry() {
    const entry = autoLoadDraft.trim();
    if (entry === '') {
      return;
    }
    if (!editForm.auto_load.includes(entry)) {
      editForm.auto_load = [...editForm.auto_load, entry];
    }
    autoLoadDraft = '';
    editError = '';
  }

  function removeAutoLoadEntry(index) {
    editForm.auto_load = editForm.auto_load.filter(
      (_, position) => position !== index,
    );
    editError = '';
  }

  function handleAutoLoadKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      addAutoLoadEntry();
    }
  }
</script>

<section
  class="projects-view view active"
  aria-labelledby="projects-list-title"
>
  <div class="projects-layout">
    <aside class="project-list-pane" aria-labelledby="projects-list-title">
      <div class="pane-header">
        <span id="projects-list-title" class="pane-title">
          {t('projects.title', 'Projects')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="primary"
            data-testid="project-add-open"
            onClick={openAdd}
          >
            {t('projects.add.open', 'Add project')}
          </Button>
        </div>
      </div>

      <div class="project-list-scroll">
        {#if listError}
          <Banner variant="error" role="alert">
            {listError}
          </Banner>
        {/if}
        {#if statusMessage}
          <p class="project-list-state" role="status">{statusMessage}</p>
        {/if}

        {#if loadingProjects}
          <p class="project-list-state" role="status">
            {t('projects.loading', 'Loading projects…')}
          </p>
        {:else if !hasProjects}
          <EmptyState
            title={t('projects.emptyTitle', 'No projects yet')}
            description={t(
              'projects.emptySubtitle',
              'Add a repository path below to create your first project.',
            )}
          />
        {:else}
          {#each projects as project (project.project_id)}
            <button
              type="button"
              class="project-item"
              class:active={project.project_id === selectedProjectId}
              data-testid={`project-toggle-${project.project_id}`}
              onclick={() => selectProject(project.project_id)}
            >
              <span class="project-bar"></span>
              <span class="project-item-inner">
                <span class="project-item-head">
                  <span class="project-item-name">
                    {project.display_name || project.project_id}
                  </span>
                  {#if needsRePoint(project)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title', 'Repository not found')}
                    </StatusChip>
                  {/if}
                </span>
                <span class="project-item-cwd" use:tooltip={project.cwd}>
                  {project.cwd}
                </span>
              </span>
            </button>
          {/each}
        {/if}
      </div>
    </aside>

    {#if !selectedProject}
      <div class="project-detail-pane">
        <EmptyState
          fill
          class="master-detail-empty"
          title={t(
            'projects.detail.empty',
            'Select a project to view and edit it.',
          )}
        />
      </div>
    {:else}
      {#key selectedProject.project_id}
        <div
          class="project-detail-pane"
          data-testid={`project-panel-${selectedProject.project_id}`}
        >
          <div class="project-detail-scroll">
            <div class="detail-top">
              <div>
                <div class="detail-heading-row">
                  <span class="detail-heading">
                    {selectedProject.display_name || selectedProject.project_id}
                  </span>
                  {#if needsRePoint(selectedProject)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title', 'Repository not found')}
                    </StatusChip>
                  {/if}
                </div>
                <div class="detail-sub">{selectedProject.cwd}</div>
              </div>
              <div class="detail-btns">
                {#if needsRePoint(selectedProject)}
                  <Button
                    variant="secondary"
                    data-testid={`project-repoint-${selectedProject.project_id}`}
                    disabled={editSaving}
                    onClick={() => openRePoint(selectedProject)}
                  >
                    {t('projects.rePoint.submit', 'Re-point')}
                  </Button>
                {/if}
                <Button
                  variant="danger"
                  data-testid={`project-remove-${selectedProject.project_id}`}
                  disabled={removingProjectId === selectedProject.project_id ||
                    editSaving}
                  onClick={() => removeOne(selectedProject)}
                >
                  {t('projects.remove', 'Remove')}
                </Button>
              </div>
            </div>

            {#if editError}
              <Banner variant="error" role="alert">
                {editError}
              </Banner>
            {/if}

            <!-- Section 1: Project settings -->
            <form
              class="detail-section detail-section--overflow"
              onsubmit={handleManualSave}
            >
              <div class="detail-section-title">
                {t('projects.detail.sectionSettings', 'Project settings')}
              </div>
              <div class="detail-section-body">
                <div class="projects-field-grid">
                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.displayName', 'Display name')}
                    </span>
                    <TextField
                      id="project-edit-name"
                      value={editForm.display_name}
                      disabled={editSaving}
                      onInput={(next) => updateEditField('display_name', next)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.sourceFormat', 'Source format')}
                      <InfoHint
                        text={t(
                          'projects.manage.sourceFormatHelp',
                          'Where this project’s agents and skills come from. Switching re-derives the team and skills from the other ecosystem’s directories; sessions are kept.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-source-format"
                      value={editForm.source_format}
                      options={sourceFormatOptions}
                      ariaLabel={t(
                        'projects.manage.sourceFormat',
                        'Source format',
                      )}
                      disabled={editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('source_format', value)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.defaultAgent', 'Default agent')}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultAgentHelp',
                          'The team agent preselected when you open this project in Chat.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-agent"
                      value={editForm.default_agent}
                      options={agentOptions}
                      placeholder={t(
                        'projects.manage.defaultAgentEmpty',
                        'No project default',
                      )}
                      ariaLabel={t(
                        'projects.manage.defaultAgent',
                        'Default agent',
                      )}
                      disabled={editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('default_agent', value)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.defaultModel', 'Default model')}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultModelHelp',
                          'Used by team agents that do not declare their own model. Resolution order: per-agent override → the agent’s own value → this project default → the global default.',
                        )}
                      />
                    </span>
                    <SearchableDropdown
                      id="project-edit-model"
                      value={modelSelectValue}
                      options={modelOptions}
                      placeholder={defaultModelInheritLabel()}
                      searchPlaceholder={t(
                        'projects.manage.modelSearchPlaceholder',
                        'Filter models…',
                      )}
                      emptyLabel={t(
                        'projects.manage.modelSearchEmpty',
                        'No models match',
                      )}
                      ariaLabel={t(
                        'projects.manage.defaultModel',
                        'Default model',
                      )}
                      disabled={editSaving}
                      triggerClass="projects-dropdown"
                      panelClass="projects-view__search-panel"
                      footerActionLabel={modelFilterFooter}
                      onFooterAction={() => (showAllModels = !showAllModels)}
                      onOpenChange={trackModelDropdownOpen}
                      onValueChange={updateModelSelection}
                    />
                    <Button
                      variant="tertiary"
                      class="projects-inherit-link"
                      onClick={navigateToAgentDefaults}
                    >
                      {t('inherit.editGlobalDefaults', 'Edit global defaults')}
                    </Button>
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t(
                        'projects.manage.defaultTemperature',
                        'Default temperature',
                      )}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultTemperatureHelp',
                          'Used by team agents that do not set their own temperature. Same resolution order as the default model.',
                        )}
                      />
                    </span>
                    <div class="projects-override-controls">
                      <TextField
                        id="project-edit-temperature"
                        class="projects-override-input"
                        inputmode="decimal"
                        value={editForm.default_temperature}
                        disabled={editSaving}
                        ariaLabel={t(
                          'projects.manage.defaultTemperature',
                          'Default temperature',
                        )}
                        onInput={(next) =>
                          updateEditField('default_temperature', next)}
                      />
                      {#if !temperatureIsInherit}
                        <Button
                          variant="tertiary"
                          tooltip={t(
                            'inherit.resetToInherit',
                            'Reset to inherited value',
                          )}
                          ariaLabel={t(
                            'inherit.resetToInherit',
                            'Reset to inherited value',
                          )}
                          onClick={clearDefaultTemperature}
                        >
                          —
                        </Button>
                      {/if}
                    </div>
                    {#if temperatureIsInherit}
                      {#if globalDefaultText('temperature')}
                        <small class="projects-inherit-hint">
                          {t(
                            'inherit.hint',
                            'Inherited: {value} (global default)',
                            { value: globalDefaultText('temperature') },
                          )}
                        </small>
                      {:else}
                        <small class="projects-inherit-hint">
                          {t(
                            'inherit.hintProviderDefault',
                            'Provider default — nothing is set here or in the global defaults.',
                          )}
                        </small>
                      {/if}
                    {/if}
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t(
                        'projects.manage.defaultThinkingEffort',
                        'Default thinking effort',
                      )}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultThinkingEffortHelp',
                          'Used by team agents that do not set their own thinking effort. Same resolution order as the default model.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-thinking-effort"
                      value={editForm.default_thinking_effort}
                      options={thinkingEffortOptions}
                      ariaLabel={t(
                        'projects.manage.defaultThinkingEffort',
                        'Default thinking effort',
                      )}
                      disabled={editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('default_thinking_effort', value)}
                    />
                  </label>
                </div>

                <div class="detail-btns projects-save-row">
                  <Button
                    variant="primary"
                    type="submit"
                    data-testid={`project-save-${selectedProject.project_id}`}
                    disabled={editSaving}
                  >
                    {editSaving
                      ? t('projects.manage.saving', 'Saving…')
                      : t('projects.manage.save', 'Save changes')}
                  </Button>
                </div>
              </div>
            </form>

            <!-- Section 2: Auto-load files -->
            <div class="detail-section">
              <div class="detail-section-title">
                {t('projects.detail.sectionAutoLoad', 'Auto-load files')}
                <InfoHint
                  text={t(
                    'projects.detail.autoLoadInfo',
                    'These files are embedded into the system prompt of every session in this project — the agent always sees their full content, with higher weight than normal chat history, and they are never dropped or summarized by context compaction.\n\nPaths are relative to the project folder (absolute paths also work), files load in list order, and missing files are skipped. When an outside agent visits the project, the same files arrive as a context note instead.',
                  )}
                />
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  {#if editForm.auto_load.length > 0}
                    <ul class="projects-file-list">
                      {#each editForm.auto_load as filePath, index (index)}
                        <li class="projects-file-row">
                          <span class="projects-file-name">{filePath}</span>
                          <button
                            type="button"
                            class="projects-file-remove"
                            data-testid={`project-auto-load-remove-${index}`}
                            disabled={editSaving}
                            aria-label={t(
                              'projects.manage.autoLoadRemove',
                              'Remove {file}',
                              { file: filePath },
                            )}
                            onclick={() => removeAutoLoadEntry(index)}
                          >
                            ×
                          </button>
                        </li>
                      {/each}
                    </ul>
                  {:else}
                    <EmptyState
                      density="compact"
                      description={t(
                        'projects.manage.autoLoadEmpty',
                        'No auto-load files',
                      )}
                    />
                  {/if}
                  <div class="projects-file-add">
                    <TextField
                      id="project-edit-auto-load"
                      class="projects-file-input"
                      value={autoLoadDraft}
                      placeholder={t(
                        'projects.manage.autoLoadPlaceholder',
                        'Add a file path…',
                      )}
                      disabled={editSaving}
                      ariaLabel={t(
                        'projects.manage.autoLoad',
                        'Auto-load files',
                      )}
                      onInput={(next) => {
                        autoLoadDraft = next;
                      }}
                      onkeydown={handleAutoLoadKeydown}
                    />
                    <Button
                      variant="secondary"
                      data-testid="project-auto-load-add"
                      disabled={editSaving || autoLoadDraft.trim().length === 0}
                      onClick={addAutoLoadEntry}
                    >
                      {t('projects.manage.autoLoadAdd', 'Add')}
                    </Button>
                  </div>
                </div>
              </div>
            </div>

            <!-- Section 3: Team -->
            <div class="detail-section">
              <div class="detail-section-title">
                <span class="projects-section-title-copy">
                  {t('projects.detail.sectionTeam', 'Team')}
                  <InfoHint
                    text={t(
                      'projects.detail.teamInfo',
                      'Agents discovered live in the project repository — where they are read from depends on the source format. The list is re-derived on open and re-scan; the repository is the source of truth, so vBot never copies or edits these agents.',
                    )}
                  />
                </span>
                <Button
                  variant="tertiary"
                  class="projects-section-refresh"
                  data-testid="project-team-refresh"
                  loading={scanLoading && scanRefreshSection === 'team'}
                  disabled={scanLoading}
                  onClick={() => refreshScan('team')}
                >
                  <svg
                    viewBox="0 0 14 14"
                    width="11"
                    height="11"
                    aria-hidden="true"
                  >
                    <path d="M11.5 4.5V1.5m0 0h-3" />
                    <path d="M11 4A5 5 0 1 0 12 9" />
                  </svg>
                  {scanLoading && scanRefreshSection === 'team'
                    ? t('projects.team.refreshing', 'Scanning…')
                    : t('projects.team.refresh', 'Rescan team')}
                </Button>
              </div>
              <div class="detail-section-body">
                {#if activeReport && !(scanLoading && scanRefreshSection !== 'skills') && !activeReport.clean}
                  <div class="projects-field">
                    <Banner variant="warn" role="status">
                      {t(
                        'projects.report.findingCount',
                        '{count} issues found',
                        {
                          count: activeReport.findingCount,
                        },
                      )}
                    </Banner>
                    {#each activeReport.groups as group (group.type)}
                      <div class="projects-finding-group">
                        <h4 class="projects-finding-title">
                          {groupLabel(group.type)}
                        </h4>
                        <ul class="projects-findings">
                          {#each group.findings as finding, index (`${group.type}-${index}`)}
                            <li class="projects-finding">
                              <span class="projects-finding-detail">
                                {finding.detail}
                              </span>
                              {#if finding.agent_id}
                                <span class="projects-finding-meta">
                                  {t(
                                    'projects.report.finding.agent',
                                    'Agent {agentId}',
                                    { agentId: finding.agent_id },
                                  )}
                                </span>
                              {/if}
                              {#if finding.source_path}
                                <span class="projects-finding-meta">
                                  {t(
                                    'projects.report.finding.source',
                                    'Source: {source}',
                                    { source: finding.source_path },
                                  )}
                                </span>
                              {/if}
                            </li>
                          {/each}
                        </ul>
                      </div>
                    {/each}
                  </div>
                {/if}

                {#if scanLoading && scanRefreshSection !== 'skills'}
                  <p class="projects-scan-loading" role="status">
                    {t('projects.loading', 'Loading projects…')}
                  </p>
                {:else if activeTeam.length === 0}
                  <EmptyState
                    density="compact"
                    description={t(
                      'projects.team.empty',
                      'No agents discovered in this repository yet. An empty project is valid — add agent files to the repo to build a team.',
                    )}
                  />
                {:else}
                  <ul class="projects-team">
                    {#each activeTeam as member (member.agent_id)}
                      {@const expanded =
                        expandedMembers[member.agent_id] === true}
                      {@const summary = effectiveDisplay(member, 'model')}
                      <li
                        class="projects-team-member"
                        class:projects-team-member--expanded={expanded}
                        data-testid={`project-team-member-${member.agent_id}`}
                      >
                        <button
                          type="button"
                          class="projects-team-header"
                          data-testid={`project-team-toggle-${member.agent_id}`}
                          aria-expanded={expanded}
                          onclick={() => toggleMember(member.agent_id)}
                        >
                          <svg
                            class="projects-team-chevron"
                            class:projects-team-chevron--open={expanded}
                            viewBox="0 0 12 12"
                            width="11"
                            height="11"
                            aria-hidden="true"
                          >
                            <path d="M4 2l4 4-4 4" />
                          </svg>
                          <span class="projects-team-headline">
                            <span class="projects-team-name">
                              {member.display_name}
                            </span>
                            {#if member.description}
                              <span class="projects-team-description">
                                {member.description}
                              </span>
                            {/if}
                          </span>
                          <span
                            class="projects-team-summary"
                            use:tooltip={summary.value}
                          >
                            {summary.value}
                          </span>
                        </button>

                        {#if expanded}
                          <div class="projects-team-detail">
                            <ul class="projects-effective-list">
                              {#each ['model', 'temperature', 'thinking_effort'] as field (field)}
                                {@const display = effectiveDisplay(
                                  member,
                                  field,
                                )}
                                <li class="projects-effective-row">
                                  <span class="projects-effective-label">
                                    {display.label}
                                  </span>
                                  <span
                                    class="projects-effective-value"
                                    class:projects-effective-value--muted={display.isEmpty}
                                  >
                                    {display.value}
                                  </span>
                                  {#if display.sourceLabel}
                                    <span class="projects-effective-source">
                                      {t(
                                        'projects.team.fromSource',
                                        'from {source}',
                                        { source: display.sourceLabel },
                                      )}
                                    </span>
                                  {/if}
                                </li>
                              {/each}
                            </ul>

                            <div class="projects-overrides">
                              <!-- Model override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t('projects.team.effectiveModel', 'Model')}
                                </span>
                                <div class="projects-override-controls">
                                  <div class="projects-override-input">
                                    <SearchableDropdown
                                      id={`project-override-model-${member.agent_id}`}
                                      value={selectModelValue(
                                        overrideDraft(member.agent_id).model,
                                        overrideModelOptions(member),
                                      )}
                                      options={overrideModelOptions(member)}
                                      placeholder={t(
                                        'projects.team.overrideModelPlaceholder',
                                        'No override',
                                      )}
                                      searchPlaceholder={t(
                                        'projects.manage.modelSearchPlaceholder',
                                        'Filter models…',
                                      )}
                                      emptyLabel={t(
                                        'projects.manage.modelSearchEmpty',
                                        'No models match',
                                      )}
                                      ariaLabel={t(
                                        'projects.team.effectiveModel',
                                        'Model',
                                      )}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'model',
                                      )}
                                      triggerClass="projects-dropdown"
                                      panelClass="projects-view__search-panel"
                                      footerActionLabel={overrideModelFilterFooter(
                                        member,
                                      )}
                                      onFooterAction={() =>
                                        toggleShowAllOverrideModels(
                                          member.agent_id,
                                        )}
                                      onOpenChange={trackModelDropdownOpen}
                                      onValueChange={(value) =>
                                        updateOverrideModelSelection(
                                          member.agent_id,
                                          value,
                                        )}
                                    />
                                  </div>
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-model-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'model',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'model',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'model')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-model-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'model',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'model',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <!-- Temperature override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t(
                                    'projects.team.effectiveTemperature',
                                    'Temperature',
                                  )}
                                </span>
                                <div class="projects-override-controls">
                                  <TextField
                                    id={`project-override-temperature-${member.agent_id}`}
                                    class="projects-override-input"
                                    inputmode="decimal"
                                    value={overrideDraft(member.agent_id)
                                      .temperature}
                                    placeholder={t(
                                      'projects.team.overrideTemperaturePlaceholder',
                                      'e.g. 0.7',
                                    )}
                                    disabled={isOverrideBusy(
                                      member.agent_id,
                                      'temperature',
                                    )}
                                    ariaLabel={t(
                                      'projects.team.effectiveTemperature',
                                      'Temperature',
                                    )}
                                    onInput={(next) =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'temperature',
                                        next,
                                      )}
                                  />
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-temperature-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'temperature',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'temperature',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'temperature')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-temperature-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'temperature',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'temperature',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <!-- Thinking-effort override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t(
                                    'projects.team.effectiveThinkingEffort',
                                    'Thinking effort',
                                  )}
                                </span>
                                <div class="projects-override-controls">
                                  <div class="projects-override-input">
                                    <Dropdown
                                      id={`project-override-thinking-${member.agent_id}`}
                                      value={overrideDraft(member.agent_id)
                                        .thinking_effort}
                                      options={overrideEffortOptions(member)}
                                      ariaLabel={t(
                                        'projects.team.effectiveThinkingEffort',
                                        'Thinking effort',
                                      )}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                      triggerClass="projects-dropdown"
                                      onValueChange={(value) =>
                                        updateOverrideDraft(
                                          member.agent_id,
                                          'thinking_effort',
                                          value,
                                        )}
                                    />
                                  </div>
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-thinking-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'thinking_effort',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'thinking_effort')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-thinking-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'thinking_effort',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <div
                                class="projects-override-row projects-override-row--policy"
                              >
                                <span class="projects-label">
                                  {t(
                                    'projects.team.compactionPolicy',
                                    'Compaction Policy',
                                  )}
                                </span>
                                {#if overrideDraft(member.agent_id).compaction_policy}
                                  <CompactionPolicyEditor
                                    value={overrideDraft(member.agent_id)
                                      .compaction_policy}
                                    onChange={(value) =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'compaction_policy',
                                        value,
                                      )}
                                    idPrefix={`project-compaction-${member.agent_id}`}
                                  />
                                  <div class="projects-override-controls">
                                    <Button
                                      variant="secondary"
                                      disabled={!canSetOverride(
                                        member.agent_id,
                                        'compaction_policy',
                                      )}
                                      onClick={() =>
                                        applySetOverride(
                                          member.agent_id,
                                          'compaction_policy',
                                        )}
                                    >
                                      {t(
                                        'projects.team.setOverride',
                                        'Set override',
                                      )}
                                    </Button>
                                    <Button
                                      variant="tertiary"
                                      onClick={() =>
                                        memberFieldIsOverridden(
                                          member,
                                          'compaction_policy',
                                        )
                                          ? applyClearOverride(
                                              member.agent_id,
                                              'compaction_policy',
                                            )
                                          : updateOverrideDraft(
                                              member.agent_id,
                                              'compaction_policy',
                                              null,
                                            )}
                                    >
                                      {memberFieldIsOverridden(
                                        member,
                                        'compaction_policy',
                                      )
                                        ? t(
                                            'projects.team.clearOverride',
                                            'Clear override',
                                          )
                                        : t('common.cancel', 'Cancel')}
                                    </Button>
                                  </div>
                                {:else}
                                  <Button
                                    variant="secondary"
                                    onClick={() =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'compaction_policy',
                                        structuredClone(
                                          globalCompactionPolicy ?? {},
                                        ),
                                      )}
                                  >
                                    {t(
                                      'projects.team.customizeCompaction',
                                      'Customize for this agent',
                                    )}
                                  </Button>
                                {/if}
                              </div>

                              <p class="projects-override-help">
                                {t(
                                  'projects.team.overrideHelp',
                                  'An override replaces the agent file and all defaults for this agent in this project. The model override can also be set with /model in chat.',
                                )}
                              </p>
                            </div>

                            <div>
                              {#if member.denied_tools.length > 0}
                                <p class="projects-tools-line">
                                  {t(
                                    'projects.team.deniedTools',
                                    'Denied by the agent file: {tools}',
                                    { tools: member.denied_tools.join(', ') },
                                  )}
                                </p>
                                <p class="projects-tools-follow">
                                  {t(
                                    'projects.team.toolsFollowWhitelist',
                                    'All other tools follow the project tool whitelist.',
                                  )}
                                </p>
                              {:else}
                                <p class="projects-tools-line">
                                  {t(
                                    'projects.team.deniedToolsNone',
                                    'No tool denials — follows the project tool whitelist.',
                                  )}
                                </p>
                              {/if}
                            </div>

                            {#if member.source_path}
                              <p class="projects-source-line">
                                {t(
                                  'projects.team.sourceFile',
                                  'Source: {path} ({format})',
                                  {
                                    path: member.source_path,
                                    format: member.source_format,
                                  },
                                )}
                              </p>
                            {/if}
                          </div>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            </div>

            <!-- Section 4: Tools -->
            <div class="detail-section">
              <div class="detail-section-title">
                {t('projects.detail.sectionTools', 'Tools')}
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  <span class="projects-label">
                    {t('projects.manage.allowedTools', 'Tool whitelist')}
                  </span>
                  <p class="projects-help">
                    {t(
                      'projects.manage.allowedToolsHelp',
                      'The maximum tools this project’s agents may use. An individual agent may use fewer through its own permissions.',
                    )}
                  </p>
                  <ToggleChipList
                    items={toolChipItems}
                    disabled={editSaving}
                    emptyLabel={t(
                      'projects.manage.toolsEmpty',
                      'No tools available',
                    )}
                    ariaToggleLabel={(name) =>
                      t('projects.manage.toggleTool', 'Toggle tool {name}', {
                        name,
                      })}
                    onToggle={(name, next) => toggleTool(name, next)}
                    onSetAll={setAllTools}
                    onOpenExtensions={navigateToExtensions}
                  >
                    {#snippet headerActions()}
                      <Button
                        variant="tertiary"
                        data-testid="project-tools-reset"
                        disabled={editSaving}
                        onClick={resetToolsToDefaults}
                      >
                        {t(
                          'projects.manage.resetDefaults',
                          'Reset to defaults',
                        )}
                      </Button>
                    {/snippet}
                  </ToggleChipList>
                </div>
              </div>
            </div>

            <!-- Section 5: Skills -->
            <div class="detail-section">
              <div class="detail-section-title">
                <span>{t('projects.detail.sectionSkills', 'Skills')}</span>
                <Button
                  variant="tertiary"
                  class="projects-section-refresh"
                  data-testid="project-skills-refresh"
                  loading={scanLoading && scanRefreshSection === 'skills'}
                  disabled={scanLoading}
                  onClick={() => refreshScan('skills')}
                >
                  <svg
                    viewBox="0 0 14 14"
                    width="11"
                    height="11"
                    aria-hidden="true"
                  >
                    <path d="M11.5 4.5V1.5m0 0h-3" />
                    <path d="M11 4A5 5 0 1 0 12 9" />
                  </svg>
                  {scanLoading && scanRefreshSection === 'skills'
                    ? t('projects.skills.refreshing', 'Refreshing…')
                    : t('projects.skills.refresh', 'Refresh skills')}
                </Button>
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  <span class="projects-label">
                    {t('projects.manage.allowedSkills', 'Skill whitelist')}
                  </span>
                  <p class="projects-help">
                    {t(
                      'projects.manage.allowedSkillsHelp',
                      'Project skills are active by default; bundled and global skills are opt-in.',
                    )}
                  </p>
                  {#if skillToggleSections.project.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.projectSkills', 'Project skills')}
                    </span>
                    <ToggleChipList
                      items={projectSkillChips}
                      disabled={editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleProjectSkill(name, next)}
                      onSetAll={setAllProjectSkills}
                    />
                  {/if}
                  {#if skillToggleSections.bundled.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.bundledSkills', 'Bundled skills')}
                    </span>
                    <ToggleChipList
                      items={bundledSkillChips}
                      disabled={editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleBundledSkill(name, next)}
                      onSetAll={setAllBundledSkills}
                    />
                  {/if}
                  {#if skillToggleSections.global.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.globalSkills', 'Global skills')}
                    </span>
                    <ToggleChipList
                      items={globalSkillChips}
                      disabled={editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleGlobalSkill(name, next)}
                      onSetAll={setAllGlobalSkills}
                    />
                  {/if}
                  {#if skillToggleSections.project.length === 0 && skillToggleSections.bundled.length === 0 && skillToggleSections.global.length === 0}
                    <EmptyState
                      density="compact"
                      description={t(
                        'projects.manage.skillsEmpty',
                        'No skills available',
                      )}
                    />
                  {/if}
                </div>
              </div>
            </div>
          </div>
        </div>
      {/key}
    {/if}
  </div>

  {#if isAddOpen}
    <Modal
      title={t('projects.add.title', 'Add project')}
      labelledById="projects-add-title"
      class="projects-view__modal"
      closeDisabled={addingProject}
      onClose={closeAdd}
    >
      {#snippet body()}
        <form onsubmit={submitAdd}>
          <div class="modal-body">
            <p class="projects-help">
              {t(
                'projects.add.subtitle',
                'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
              )}
            </p>

            <FormField
              controlId="projects-add-cwd"
              label={t('projects.add.cwd', 'Repository path')}
              help={t(
                'projects.add.cwdHelp',
                'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
              )}
            >
              <TextField
                id="projects-add-cwd"
                variant="modal"
                value={addForm.cwd}
                placeholder={t(
                  'projects.add.cwdPlaceholder',
                  'C:/path/to/repository',
                )}
                disabled={addingProject}
                onInput={(next) => updateAddField('cwd', next)}
              />
            </FormField>

            <FormField
              controlId="projects-add-display-name"
              label={t('projects.add.displayName', 'Display name')}
            >
              <TextField
                id="projects-add-display-name"
                variant="modal"
                value={addForm.display_name}
                placeholder={t(
                  'projects.add.displayNamePlaceholder',
                  'Optional — defaults to the folder name',
                )}
                disabled={addingProject}
                onInput={(next) => updateAddField('display_name', next)}
              />
            </FormField>

            {#if addShowsFormatChoice}
              <FormField
                label={t('projects.add.format', 'Source format')}
                help={t(
                  'projects.add.formatHelp',
                  'This repository carries both ecosystems. Pick which one this project uses — its agents and skills come only from that one. You can switch later in the project settings.',
                )}
                role="radiogroup"
                aria-label={t('projects.add.format', 'Source format')}
              >
                <div class="projects-format-choice">
                  {#each PROJECT_SOURCE_FORMATS as formatKey (formatKey)}
                    <button
                      type="button"
                      class="projects-format-option"
                      class:projects-format-option--selected={addForm.source_format ===
                        formatKey}
                      role="radio"
                      aria-checked={addForm.source_format === formatKey}
                      disabled={addingProject}
                      onclick={() => updateAddField('source_format', formatKey)}
                    >
                      <span class="projects-format-option__name">
                        {formatLabel(formatKey)}
                      </span>
                      <span class="projects-format-option__detail">
                        {t(
                          'projects.add.formatCounts',
                          '{agents} agents · {skills} skills',
                          {
                            agents:
                              addDetect?.formats?.[formatKey]?.agents ?? 0,
                            skills:
                              addDetect?.formats?.[formatKey]?.skills ?? 0,
                          },
                        )}
                      </span>
                    </button>
                  {/each}
                </div>
              </FormField>
            {:else if addDetectedFormat}
              <p class="projects-help">
                {t('projects.add.formatDetected', 'Detected: {format}', {
                  format: formatLabel(addDetectedFormat),
                })}
              </p>
            {/if}

            {#if addSuggestsClaudeMd}
              <div class="projects-claude-md-suggestion">
                <Toggle
                  size="sm"
                  checked={addForm.include_claude_md}
                  disabled={addingProject}
                  ariaLabel={t(
                    'projects.add.claudeMdSuggestionLabel',
                    'Load CLAUDE.md as a project file',
                  )}
                  onChange={(next) => updateAddField('include_claude_md', next)}
                />
                <span>
                  {t(
                    'projects.add.claudeMdSuggestion',
                    'Load {path} as a project file. The repository has no AGENTS.md; the file is loaded as-is into project agent prompts.',
                    { path: addDetect?.claude_md ?? 'CLAUDE.md' },
                  )}
                </span>
              </div>
            {/if}

            {#if addError}
              <Banner variant="error" role="alert">
                {addError}
              </Banner>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={addingProject}
              onClick={closeAdd}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={!canSubmitAdd}>
              {addingProject
                ? t('projects.add.submitting', 'Adding project…')
                : t('projects.add.submit', 'Add project')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}

  {#if rePointProject}
    <Modal
      title={t('projects.rePoint.title', 'Repository not found')}
      labelledById="projects-repoint-title"
      class="projects-view__modal"
      onClose={closeRePoint}
    >
      {#snippet body()}
        <form onsubmit={submitRePoint}>
          <div class="modal-body">
            <p class="projects-help">
              {t(
                'projects.rePoint.description',
                'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
              )}
            </p>
            <FormField
              controlId="projects-repoint-cwd"
              label={t('projects.rePoint.cwd', 'New repository path')}
            >
              <TextField
                id="projects-repoint-cwd"
                variant="modal"
                value={rePointCwd}
                placeholder={t(
                  'projects.rePoint.cwdPlaceholder',
                  'C:/path/to/repository',
                )}
                disabled={rePointing}
                onInput={(next) => {
                  rePointCwd = next;
                  rePointError = '';
                }}
              />
            </FormField>

            {#if rePointError}
              <Banner variant="error" role="alert">
                {rePointError}
              </Banner>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={rePointing}
              onClick={closeRePoint}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={rePointing}>
              {rePointing
                ? t('projects.rePoint.submitting', 'Re-pointing…')
                : t('projects.rePoint.submit', 'Re-point')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}

  {#if removeConfirmProject}
    <ConfirmDialog
      title={t('projects.remove.confirmTitle', 'Remove project')}
      body={t(
        'projects.remove.confirm',
        'Remove project {name}? The project is archived and can be restored; the repository on disk is never touched.',
        {
          name:
            removeConfirmProject.display_name ||
            removeConfirmProject.project_id,
        },
      )}
      confirmLabel={t('common.remove', 'Remove')}
      onConfirm={confirmRemove}
      onCancel={cancelRemove}
    />
  {/if}
</section>
