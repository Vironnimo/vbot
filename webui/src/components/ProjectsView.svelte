<script>
  import { onDestroy, onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextField from './ui/TextField.svelte';
  import {
    addProject,
    listProjects,
    removeProject,
    rpc,
    setProject,
    showProject,
  } from '$lib/api.js';
  import {
    buildAddProjectPayload,
    buildDefaultAgentOptions,
    buildManageProjectPayload,
    buildRePointPayload,
    hasManageChanges,
    needsRePoint,
    normalizeProject,
    normalizeProjects,
    normalizeScanReport,
    projectTeam,
  } from '$lib/projectsView.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import { t } from '$lib/i18n.js';

  const PROJECT_BUSY_CODE = 'project_busy';
  const PROJECT_IN_USE_CODE = 'project_in_use';
  // The inline project edit panel is a settings-style surface, so it follows the
  // shared save model (DESIGN.md → Save model): auto-save after a short idle,
  // plus the explicit Save button for users who prefer to commit manually.
  const AUTO_SAVE_DEBOUNCE_MS = 800;

  const noop = () => {};

  let { onToast = noop } = $props();

  let projects = $state([]);
  let loadingProjects = $state(false);
  let listError = $state('');
  let statusMessage = $state('');

  // Model/connection catalogs feed the project default-model searchable
  // dropdown (the same picker the Agents tab uses, see modelSelection.js).
  let availableModels = $state([]);
  let availableConnections = $state([]);

  // Add modal state — the popup needs only the repo path plus an optional
  // display name (blank → backend derives the name from the folder).
  let isAddOpen = $state(false);
  let addForm = $state(createAddForm());
  let addingProject = $state(false);
  let addError = $state('');

  // The single expanded project: its inline edit form plus the scanned team and
  // report shown underneath. Editing happens in place — there is no manage modal.
  let expandedProjectId = $state('');
  let editForm = $state(createEditForm());
  let editSaving = $state(false);
  let editError = $state('');
  // The draft text for the auto-load "add a file" input, kept apart from editForm
  // so typing a candidate path does not mark the form dirty until it is added.
  let autoLoadDraft = $state('');
  let activeTeam = $state([]);
  let activeReport = $state(null);
  let scanLoading = $state(false);
  let removingProjectId = $state('');

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
  let modelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: editForm.default_model,
      emptyLabel: t('projects.manage.defaultModelEmpty', 'No project default'),
      translate: t,
    }),
  );
  let modelSelectValue = $derived(
    selectModelValue(editForm.default_model, modelOptions),
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

  // The currently expanded project record (or null). Both the auto-save diff and
  // the explicit Save target it as the single source of truth.
  let expandedProject = $derived(
    projects.find((item) => item.project_id === expandedProjectId) ?? null,
  );
  // The sparse project.set changes the open form represents versus the saved
  // project — empty when the form matches what the server already holds.
  let pendingChanges = $derived(
    expandedProject
      ? buildManageProjectPayload(
          {
            display_name: editForm.display_name,
            default_agent: editForm.default_agent,
            default_model: editForm.default_model,
            auto_load: editForm.auto_load,
          },
          expandedProject,
        )
      : {},
  );
  let saveDisabled = $derived(editSaving || !hasManageChanges(pendingChanges));

  onMount(() => {
    void loadCatalogs();
    void loadProjects();

    return () => {
      destroyed = true;
    };
  });

  onDestroy(() => {
    clearAutoSaveTimer();
  });

  // Auto-save the open edit form once it has been idle for the debounce window.
  // The effect only schedules while the form is dirty (saveDisabled is false),
  // so collapsing, switching projects, or saving cancels the pending timer.
  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveExpandedProject();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  function createAddForm() {
    return { cwd: '', display_name: '' };
  }

  function createEditForm(project = null) {
    return {
      display_name: project?.display_name ?? '',
      default_agent: project?.default_agent ?? '',
      default_model: project?.default_model ?? '',
      auto_load: [...(project?.auto_load ?? [])],
    };
  }

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  async function loadCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
      ]);
      if (destroyed) {
        return;
      }
      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
    } catch {
      // A missing model catalog only degrades the default-model picker (it still
      // lists the empty option); it must not block the projects list itself.
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
    isAddOpen = true;
  }

  function closeAdd() {
    if (addingProject) {
      return;
    }
    isAddOpen = false;
    addError = '';
  }

  function updateAddField(field, value) {
    addForm[field] = value;
    addError = '';
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
      });
      const result = await addProject(payload);
      if (destroyed) {
        return;
      }
      const project = normalizeProject(result?.project);
      statusMessage = t('projects.add.success', 'Project added.');
      isAddOpen = false;
      addForm = createAddForm();
      await loadProjects();
      if (!destroyed) {
        // Open the freshly added project so its scan (team + report) is the
        // review surface right away (add-then-review, no dry-run).
        expandProject(project.project_id, result?.scan);
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

  function toggleProject(project) {
    if (expandedProjectId === project.project_id) {
      collapseProject();
      return;
    }
    expandProject(project.project_id);
  }

  // Expand a project for inline editing. When a scan is already in hand (right
  // after add) it seeds the team/report immediately; otherwise it fetches one.
  function expandProject(projectId, scan = null) {
    const project =
      projects.find((item) => item.project_id === projectId) ?? null;
    expandedProjectId = projectId;
    editForm = createEditForm(project);
    autoLoadDraft = '';
    editError = '';
    activeTeam = [];
    activeReport = null;

    if (scan) {
      activeTeam = projectTeam(scan);
      activeReport = normalizeScanReport(scan.report);
      return;
    }

    void loadScan(projectId);
  }

  function collapseProject() {
    expandedProjectId = '';
    activeTeam = [];
    activeReport = null;
    editError = '';
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
      activeTeam = projectTeam(result?.scan);
      activeReport = normalizeScanReport(result?.scan?.report);
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

  function updateEditField(field, value) {
    editForm[field] = value;
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
    void saveExpandedProject();
  }

  // Persist the open form's pending changes. Shared by the debounced auto-save
  // and the explicit Save button; both target the expanded project and re-seed
  // the panel from the saved state so the form reads as clean afterwards.
  async function saveExpandedProject() {
    const project = expandedProject;
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
      activeTeam = projectTeam(result?.scan);
      activeReport = normalizeScanReport(result?.scan?.report);
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

  async function removeOne(project) {
    const confirmRemove =
      typeof globalThis.confirm === 'function'
        ? globalThis.confirm(
            t(
              'projects.remove.confirm',
              'Remove project {name}? The project is archived and can be restored; the repository on disk is never touched.',
              { name: project.display_name || project.project_id },
            ),
          )
        : true;

    if (!confirmRemove) {
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
      if (expandedProjectId === project.project_id) {
        collapseProject();
      }
      statusMessage = t('projects.remove.success', 'Project removed.');
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      const message = removeErrorText(error);
      if (expandedProjectId === project.project_id) {
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
      if (!destroyed && expandedProjectId === projectId) {
        expandProject(projectId, result?.scan);
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
    // Skip a duplicate so the same file can never be listed (and rendered) twice.
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
    // Enter adds the entry instead of submitting the surrounding edit form.
    if (event.key === 'Enter') {
      event.preventDefault();
      addAutoLoadEntry();
    }
  }
</script>

<section class="projects-view view active" aria-labelledby="projects-title">
  <header class="projects-view__header">
    <div>
      <p class="projects-view__eyebrow">
        {t('projects.eyebrow', 'Project workspaces')}
      </p>
      <h2 id="projects-title" class="projects-view__title">
        {t('projects.title', 'Projects')}
      </h2>
      <p class="projects-view__subtitle">
        {t(
          'projects.subtitle',
          'Add a repository as a project to discover its team and chat with project agents. Adding a project also scans its repo for issues.',
        )}
      </p>
    </div>

    <div class="projects-view__header-actions">
      <Button variant="secondary" onClick={() => loadProjects()}>
        {t('projects.refresh', 'Refresh')}
      </Button>
    </div>
  </header>

  {#if listError}
    <p class="projects-view__notice projects-view__notice--error" role="alert">
      {listError}
    </p>
  {/if}

  {#if statusMessage}
    <p class="projects-view__notice" role="status">{statusMessage}</p>
  {/if}

  <div class="projects-view__list">
    <div class="projects-view__list-head">
      <h3 class="projects-view__section-title">
        {t('projects.list.title', 'Your projects')}
      </h3>
      <Button
        variant="primary"
        data-testid="project-add-open"
        onClick={openAdd}
      >
        {t('projects.add.open', 'Add project')}
      </Button>
    </div>

    {#if loadingProjects}
      <p class="projects-view__notice" role="status">
        {t('projects.loading', 'Loading projects…')}
      </p>
    {:else if !hasProjects}
      <div class="projects-view__empty">
        <p class="projects-view__empty-title">
          {t('projects.emptyTitle', 'No projects yet')}
        </p>
        <p class="projects-view__empty-subtitle">
          {t(
            'projects.emptySubtitle',
            'Add a repository path below to create your first project.',
          )}
        </p>
      </div>
    {:else}
      <ul class="projects-view__items">
        {#each projects as project (project.project_id)}
          {@const expanded = expandedProjectId === project.project_id}
          <li
            class="projects-view__item"
            class:projects-view__item--expanded={expanded}
            data-testid={`project-${project.project_id}`}
          >
            <button
              type="button"
              class="projects-view__item-header"
              data-testid={`project-toggle-${project.project_id}`}
              aria-expanded={expanded}
              onclick={() => toggleProject(project)}
            >
              <svg
                class="projects-view__chevron"
                class:projects-view__chevron--open={expanded}
                viewBox="0 0 12 12"
                width="11"
                height="11"
                aria-hidden="true"
              >
                <path d="M4 2l4 4-4 4" />
              </svg>
              <span class="projects-view__item-main">
                <span class="projects-view__item-head">
                  <span class="projects-view__item-name">
                    {project.display_name || project.project_id}
                  </span>
                  {#if needsRePoint(project)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title', 'Repository not found')}
                    </StatusChip>
                  {/if}
                </span>
                <span class="projects-view__item-cwd" title={project.cwd}>
                  {project.cwd}
                </span>
              </span>
            </button>

            {#if expanded}
              <div
                class="projects-view__panel"
                data-testid={`project-panel-${project.project_id}`}
              >
                <form class="projects-view__edit" onsubmit={handleManualSave}>
                  <div class="projects-view__edit-grid">
                    <label class="projects-view__field">
                      <span class="projects-view__label">
                        {t('projects.manage.displayName', 'Display name')}
                      </span>
                      <TextField
                        id="project-edit-name"
                        value={editForm.display_name}
                        disabled={editSaving}
                        onInput={(next) =>
                          updateEditField('display_name', next)}
                      />
                    </label>

                    <label class="projects-view__field">
                      <span class="projects-view__label">
                        {t('projects.manage.defaultAgent', 'Default agent')}
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
                        triggerClass="projects-view__dropdown"
                        onValueChange={(value) =>
                          updateEditField('default_agent', value)}
                      />
                    </label>

                    <label class="projects-view__field">
                      <span class="projects-view__label">
                        {t('projects.manage.defaultModel', 'Default model')}
                      </span>
                      <SearchableDropdown
                        id="project-edit-model"
                        value={modelSelectValue}
                        options={modelOptions}
                        placeholder={t(
                          'projects.manage.defaultModelEmpty',
                          'No project default',
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
                          'projects.manage.defaultModel',
                          'Default model',
                        )}
                        disabled={editSaving}
                        triggerClass="projects-view__dropdown"
                        panelClass="projects-view__search-panel"
                        onValueChange={updateModelSelection}
                      />
                    </label>
                  </div>

                  <div class="projects-view__field">
                    <label
                      class="projects-view__label"
                      for="project-edit-auto-load"
                    >
                      {t('projects.manage.autoLoad', 'Auto-load files')}
                    </label>
                    {#if editForm.auto_load.length > 0}
                      <ul class="projects-view__file-list">
                        {#each editForm.auto_load as filePath, index (index)}
                          <li class="projects-view__file-row">
                            <span class="projects-view__file-name">
                              {filePath}
                            </span>
                            <button
                              type="button"
                              class="projects-view__file-remove"
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
                      <p class="projects-view__file-empty">
                        {t(
                          'projects.manage.autoLoadEmpty',
                          'No auto-load files',
                        )}
                      </p>
                    {/if}
                    <div class="projects-view__file-add">
                      <TextField
                        id="project-edit-auto-load"
                        class="projects-view__file-input"
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
                        disabled={editSaving ||
                          autoLoadDraft.trim().length === 0}
                        onClick={addAutoLoadEntry}
                      >
                        {t('projects.manage.autoLoadAdd', 'Add')}
                      </Button>
                    </div>
                  </div>

                  {#if editError}
                    <p
                      class="projects-view__notice projects-view__notice--error"
                      role="alert"
                    >
                      {editError}
                    </p>
                  {/if}

                  <div class="projects-view__edit-actions">
                    <Button
                      variant="danger"
                      data-testid={`project-remove-${project.project_id}`}
                      disabled={removingProjectId === project.project_id ||
                        editSaving}
                      onClick={() => removeOne(project)}
                    >
                      {t('projects.remove', 'Remove')}
                    </Button>
                    {#if needsRePoint(project)}
                      <Button
                        variant="secondary"
                        data-testid={`project-repoint-${project.project_id}`}
                        disabled={editSaving}
                        onClick={() => openRePoint(project)}
                      >
                        {t('projects.rePoint.submit', 'Re-point')}
                      </Button>
                    {/if}
                    <Button
                      variant="primary"
                      type="submit"
                      data-testid={`project-save-${project.project_id}`}
                      disabled={editSaving}
                    >
                      {editSaving
                        ? t('projects.manage.saving', 'Saving…')
                        : t('projects.manage.save', 'Save changes')}
                    </Button>
                  </div>
                </form>

                <div class="projects-view__panel-section">
                  <span class="projects-view__panel-label">
                    {t('projects.team.title', 'Team')}
                  </span>
                  {#if scanLoading}
                    <p class="projects-view__notice" role="status">
                      {t('projects.loading', 'Loading projects…')}
                    </p>
                  {:else if activeTeam.length === 0}
                    <p class="projects-view__team-empty">
                      {t(
                        'projects.team.empty',
                        'No agents discovered in this repository yet. An empty project is valid — add agent files to the repo to build a team.',
                      )}
                    </p>
                  {:else}
                    <ul class="projects-view__team">
                      {#each activeTeam as member (member.agent_id)}
                        <li class="projects-view__team-member">
                          <span class="projects-view__team-name">
                            {member.display_name}
                          </span>
                          <span class="projects-view__team-model">
                            {member.model ||
                              t('projects.team.noModel', 'No model')}
                          </span>
                        </li>
                      {/each}
                    </ul>
                  {/if}
                </div>

                {#if activeReport && !scanLoading && !activeReport.clean}
                  <div class="projects-view__panel-section">
                    <span class="projects-view__panel-label">
                      {t('projects.report.title', 'Scan report')}
                    </span>
                    <p
                      class="projects-view__notice projects-view__notice--warn"
                      role="status"
                    >
                      {t(
                        'projects.report.findingCount',
                        '{count} issues found',
                        {
                          count: activeReport.findingCount,
                        },
                      )}
                    </p>
                    {#each activeReport.groups as group (group.type)}
                      <div class="projects-view__finding-group">
                        <h4 class="projects-view__finding-title">
                          {groupLabel(group.type)}
                        </h4>
                        <ul class="projects-view__findings">
                          {#each group.findings as finding, index (`${group.type}-${index}`)}
                            <li class="projects-view__finding">
                              <span class="projects-view__finding-detail">
                                {finding.detail}
                              </span>
                              {#if finding.agent_id}
                                <span class="projects-view__finding-meta">
                                  {t(
                                    'projects.report.finding.agent',
                                    'Agent {agentId}',
                                    { agentId: finding.agent_id },
                                  )}
                                </span>
                              {/if}
                              {#if finding.source_path}
                                <span class="projects-view__finding-meta">
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
              </div>
            {/if}
          </li>
        {/each}
      </ul>
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
            <p class="projects-view__help">
              {t(
                'projects.add.subtitle',
                'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
              )}
            </p>

            <label class="modal-field">
              <span class="modal-label">
                {t('projects.add.cwd', 'Repository path')}
              </span>
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
              <span class="projects-view__help">
                {t(
                  'projects.add.cwdHelp',
                  'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
                )}
              </span>
            </label>

            <label class="modal-field">
              <span class="modal-label">
                {t('projects.add.displayName', 'Display name')}
              </span>
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
            </label>

            {#if addError}
              <p
                class="projects-view__notice projects-view__notice--error"
                role="alert"
              >
                {addError}
              </p>
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
            <p class="projects-view__help">
              {t(
                'projects.rePoint.description',
                'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
              )}
            </p>
            <label class="modal-field">
              <span class="modal-label">
                {t('projects.rePoint.cwd', 'New repository path')}
              </span>
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
            </label>

            {#if rePointError}
              <p
                class="projects-view__notice projects-view__notice--error"
                role="alert"
              >
                {rePointError}
              </p>
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
</section>

<style>
  .projects-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 16px;
    overflow: auto;
    padding: 24px 28px 28px;
    background: var(--bg);
  }

  .projects-view__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .projects-view__eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .projects-view__title {
    margin: 0;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .projects-view__subtitle {
    max-width: 760px;
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .projects-view__header-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
  }

  .projects-view__notice {
    margin: 0;
    padding: 11px 14px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--green);
    border-radius: var(--r-md);
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.4;
    background: var(--surface);
  }

  .projects-view__notice--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  .projects-view__notice--warn {
    border-left-color: var(--amber);
    color: var(--amber);
  }

  .projects-view__list {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 20px;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .projects-view__list-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .projects-view__section-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 600;
  }

  .projects-view__field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .projects-view__label {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 500;
  }

  .projects-view__help {
    margin: 0;
    color: var(--text-lo);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .projects-view__file-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__file-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 4px 6px 4px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: rgba(255, 255, 255, 0.02);
  }

  .projects-view__file-name {
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .projects-view__file-remove {
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    padding: 0;
    border: none;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
  }

  .projects-view__file-remove:hover:not(:disabled) {
    background: rgba(252, 129, 129, 0.12);
    color: var(--red);
  }

  .projects-view__file-remove:disabled {
    cursor: default;
    opacity: 0.5;
  }

  .projects-view__file-empty {
    margin: 0;
    color: var(--text-med);
    font-size: 12px;
  }

  .projects-view__file-add {
    display: flex;
    gap: 8px;
    margin-top: 6px;
  }

  /* The input is rendered inside the TextField child, so reach it through the
     scoped parent + :global (the project's pattern for styling a primitive). */
  .projects-view__file-add :global(.projects-view__file-input) {
    flex: 1;
    min-width: 0;
  }

  .projects-view__empty {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 22px;
    border: 1px dashed var(--border);
    border-radius: var(--r-lg);
    background: rgba(255, 255, 255, 0.02);
    text-align: center;
  }

  .projects-view__empty-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 600;
  }

  .projects-view__empty-subtitle {
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .projects-view__items {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__item {
    overflow: hidden;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .projects-view__item--expanded {
    border-color: rgba(232, 135, 10, 0.32);
  }

  .projects-view__item-header {
    display: flex;
    width: 100%;
    align-items: center;
    gap: 11px;
    padding: 11px 13px;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .projects-view__item-header:hover {
    background: var(--surface-3);
  }

  .projects-view__chevron {
    flex-shrink: 0;
    fill: none;
    stroke: var(--text-med);
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 0.18s ease;
  }

  .projects-view__chevron--open {
    transform: rotate(90deg);
  }

  .projects-view__item-main {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 3px;
  }

  .projects-view__item-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .projects-view__item-name {
    color: var(--text-hi);
    font-size: 13.5px;
    font-weight: 600;
  }

  .projects-view__item-cwd {
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .projects-view__panel {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 4px 14px 16px;
    border-top: 1px solid var(--border);
  }

  .projects-view__edit {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding-top: 14px;
  }

  .projects-view__edit-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
  }

  .projects-view__edit-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  /* The class lands on the dropdown component's own root, outside this
     component's scope, so it must be global to take effect. */
  :global(.projects-view__dropdown) {
    width: 100%;
  }

  .projects-view__panel-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .projects-view__panel-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .projects-view__team {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__team-member {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 5px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--surface);
  }

  .projects-view__team-name {
    color: var(--text-hi);
    font-size: 12.5px;
    font-weight: 500;
  }

  .projects-view__team-model {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .projects-view__team-empty {
    margin: 0;
    color: var(--text-lo);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .projects-view__finding-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .projects-view__finding-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 12.5px;
    font-weight: 600;
  }

  .projects-view__findings {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__finding {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 12px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--amber);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .projects-view__finding-detail {
    color: var(--text-med);
    font-size: 12px;
    line-height: 1.4;
  }

  .projects-view__finding-meta {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  :global(.projects-view__modal) {
    width: 480px;
    max-width: calc(100vw - 40px);
  }

  @media (max-width: 960px) {
    .projects-view {
      padding: 20px;
    }

    .projects-view__header,
    .projects-view__header-actions {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
