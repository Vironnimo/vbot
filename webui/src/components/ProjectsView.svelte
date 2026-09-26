<script>
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import SaveButton from './ui/SaveButton.svelte';
  import Banner from './ui/Banner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import {
    needsRePoint,
    createProjectsController,
    createProjectsState,
    hasManageChanges,
  } from '$lib/projectsView.js';
  import StatusChip from './ui/StatusChip.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { onDestroy, onMount, untrack } from 'svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import ProjectOverviewPanel from './projects/ProjectOverviewPanel.svelte';
  import ProjectTeamPanel from './projects/ProjectTeamPanel.svelte';
  import ProjectContextPanel from './projects/ProjectContextPanel.svelte';
  import ProjectAccessPanel from './projects/ProjectAccessPanel.svelte';
  import ProjectDialogs from './projects/ProjectDialogs.svelte';

  const noop = () => {};

  let {
    selectedProjectId: preferredProjectId = '',
    onProjectSelected = noop,
    onToast = noop,
    onNavigateToSettingsPanel = noop,
    modelsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();

  let projectsState = $state(createProjectsState());
  const projectsController = createProjectsController({
    state: untrack(() => projectsState),
    translate: t,
    onProjectSelected: (projectId) => onProjectSelected(projectId),
    onToast: (toast) => onToast(toast),
  });

  let hasProjects = $derived(projectsState.projects.length > 0);

  let selectedProject = $derived(projectsController.selectedProject());
  let findingsExpanded = $state(false);

  // The sparse project.set changes the open form represents versus the saved
  // project — empty when the form matches what the server already holds.
  let pendingChanges = $derived(projectsController.pendingChanges());
  let pendingToolAccessOverrides = $derived(
    projectsController.pendingOverrideChanges(),
  );
  const autosaveContext = useAutosaveContext();
  const projectAutosave = createAutosaveParticipant({
    cancelPending: () => {
      projectsController.clearAutoSave({ flushPending: false });
      projectsController.clearToolAccessOverrideAutoSave({
        flushPending: false,
      });
    },
    getSnapshot: () => ({
      projectId: projectsState.selectedProjectId,
      project: pendingChanges,
      toolAccessOverrides: pendingToolAccessOverrides,
    }),
    hasChanges: () =>
      hasManageChanges(projectsController.pendingChanges()) ||
      projectsController.pendingOverrideChanges().length > 0,
    save: async (reason) => {
      if (
        reason === 'manual' &&
        !hasManageChanges(projectsController.pendingChanges()) &&
        projectsController.pendingOverrideChanges().length === 0
      ) {
        onToast({
          title: t('common.alreadySaved', 'Already saved'),
          variant: 'success',
        });
        return true;
      }
      if (
        hasManageChanges(projectsController.pendingChanges()) &&
        !(await projectsController.saveSelectedProject({
          manual: reason === 'manual',
        }))
      ) {
        return false;
      }
      return projectsController.savePendingOverrides();
    },
  });
  const unregisterProjectAutosave = autosaveContext.register(projectAutosave);

  onMount(() => {
    void projectsController.initialize(preferredProjectId);
  });

  onDestroy(() => {
    unregisterProjectAutosave();
    projectsController.destroy();
  });

  // Auto-save the settings form once it has been idle for the debounce window.
  $effect(() => {
    if (
      projectsState.editSaving ||
      (!hasManageChanges(pendingChanges) &&
        pendingToolAccessOverrides.length === 0)
    )
      return;

    JSON.stringify(projectsState.overrideDrafts);
    JSON.stringify(projectsState.editForm);
    projectsController.scheduleAutoSave(() => projectAutosave.runSave());

    return () => {
      projectsController.clearAutoSave();
    };
  });

  // Reload the model catalog when the generic invalidation channel signals a
  // model/provider change (first run is a no-op: mount already loaded).
  $effect(() => {
    projectsController.updateModelsRefreshToken(modelsRefreshToken);
  });

  // Reload Project list/detail/scan state after another surface mutates the
  // server-owned Project catalog. The controller defers visible replacement
  // while this view owns an active form, picker, modal, or save.
  $effect(() => {
    projectsController.updateProjectsRefreshToken(projectsRefreshToken);
  });

  function trackModelDropdownOpen(open) {
    projectsController.trackModelDropdownOpen(open);
  }

  function openAdd() {
    projectsController.openAdd();
  }

  function selectProject(projectId) {
    if (projectId === projectsState.selectedProjectId) {
      return;
    }
    autosaveContext.requestTransition(() =>
      projectsController.selectProject(projectId),
    );
  }

  // Live voice navigation: the App already passed its autosave transition.
  export function selectVoiceProject(projectId) {
    if (
      !projectsState.projects.some(
        (project) => project.project_id === projectId,
      )
    )
      return false;
    if (projectId !== projectsState.selectedProjectId)
      projectsController.selectProject(projectId);
    return true;
  }

  function refreshScan() {
    void projectsController.refreshScan();
  }

  function navigateToExtensions(_extensionName) {
    onNavigateToSettingsPanel('extensions');
  }

  function handleManualSave(event) {
    event.preventDefault();
    void projectAutosave.runSave('manual', { force: true });
  }

  function updateToolAccessOverride(agentId, value) {
    projectsController.updateOverrideDraft(agentId, 'tool_access', value);
    projectsController.scheduleToolAccessOverrideAutoSave(() =>
      projectAutosave.runSave(),
    );
  }

  function removeOne(project) {
    projectsController.openRemove(project);
  }

  function openRePoint(project) {
    projectsController.openRePoint(project);
  }
</script>

<section
  class="projects-view view active"
  aria-labelledby="projects-list-title"
>
  <div class="projects-layout">
    <aside
      class="project-list-pane secondary-pane"
      aria-labelledby="projects-list-title"
    >
      <div class="pane-header secondary-pane__header">
        <span id="projects-list-title" class="secondary-pane__title">
          {t('projects.title', 'Projects')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="tertiary"
            icon
            ariaLabel={t('projects.add.title', 'Add project')}
            tooltip={t('projects.add.title', 'Add project')}
            data-testid="project-add-open"
            onClick={openAdd}
          >
            <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
              <path d="M7 1.5v11M1.5 7h11" />
            </svg>
          </Button>
        </div>
      </div>

      <div class="project-list-scroll secondary-pane__scroll secondary-list">
        {#if projectsState.listError}
          <Banner variant="error" role="alert">
            {projectsState.listError}
          </Banner>
        {/if}
        {#if projectsState.statusMessage}
          <p class="project-list-state" role="status">
            {projectsState.statusMessage}
          </p>
        {/if}

        {#if projectsState.loadingProjects}
          <p class="project-list-state" role="status">
            {t('projects.loading', 'Loading projects…')}
          </p>
        {:else if !hasProjects}
          <EmptyState
            title={t('projects.emptyTitle', 'No projects yet')}
            description={t(
              'projects.emptySubtitle',
              'Choose Add project (+) to connect your first repository.',
            )}
          />
        {:else}
          {#each projectsState.projects as project (project.project_id)}
            <button
              type="button"
              class="project-item secondary-list__item"
              class:active={project.project_id ===
                projectsState.selectedProjectId}
              data-testid={`project-toggle-${project.project_id}`}
              onclick={() => selectProject(project.project_id)}
            >
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
            <div class="management-header">
              <div class="detail-top">
                <div>
                  <div class="detail-heading-row">
                    <h2 class="detail-heading">
                      {selectedProject.display_name ||
                        selectedProject.project_id}
                    </h2>
                    {#if needsRePoint(selectedProject)}
                      <StatusChip variant="error">
                        {t('projects.rePoint.title', 'Repository not found')}
                      </StatusChip>
                    {/if}
                  </div>
                  <div class="detail-sub">{selectedProject.cwd}</div>
                </div>
              </div>
            </div>

            {#if projectsState.editError}
              <Banner variant="error" role="alert">
                {projectsState.editError}
              </Banner>
            {/if}

            <ProjectOverviewPanel
              bind:projectsState
              {projectsController}
              {onNavigateToSettingsPanel}
              {handleManualSave}
              {trackModelDropdownOpen}
            >
              {#snippet repositoryActions()}
                <div class="detail-btns">
                  {#if needsRePoint(selectedProject)}
                    <Button
                      variant="secondary"
                      data-testid={`project-repoint-${selectedProject.project_id}`}
                      onClick={() => openRePoint(selectedProject)}
                    >
                      {t('projects.rePoint.submit', 'Re-point')}
                    </Button>
                  {/if}
                  <Button
                    variant="danger"
                    data-testid={`project-remove-${selectedProject.project_id}`}
                    disabled={projectsState.removingProjectId ===
                      selectedProject.project_id || projectsState.editSaving}
                    onClick={() => removeOne(selectedProject)}
                  >
                    {t('projects.remove', 'Remove')}
                  </Button>
                </div>{/snippet}
            </ProjectOverviewPanel>
            <ProjectTeamPanel
              bind:findingsExpanded
              bind:projectsState
              {projectsController}
              {trackModelDropdownOpen}
              {updateToolAccessOverride}
              {navigateToExtensions}
            >
              {#snippet scanAction()}
                <Button
                  variant="tertiary"
                  data-testid="project-repository-rescan"
                  loading={projectsState.scanRefreshRequested}
                  disabled={projectsState.scanLoading}
                  onClick={refreshScan}
                >
                  {projectsState.scanRefreshRequested
                    ? t('projects.repository.rescanning', 'Scanning…')
                    : t('projects.repository.rescan', 'Rescan repository')}
                </Button>{/snippet}
            </ProjectTeamPanel>
            <ProjectContextPanel bind:projectsState {projectsController} />
            <ProjectAccessPanel
              {projectsState}
              {projectsController}
              {navigateToExtensions}
            />
            <div class="management-footer">
              <SaveButton
                type="submit"
                form="project-settings-form"
                data-testid={`project-save-${selectedProject.project_id}`}
                saving={projectsState.editSaving}
                pending={projectAutosave.hasChanges()}
              />
            </div>
          </div>
        </div>
      {/key}
    {/if}
  </div>

  <ProjectDialogs bind:projectsState {projectsController} />
</section>
