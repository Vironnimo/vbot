<script>
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import SaveButton from './ui/SaveButton.svelte';
  import Banner from './ui/Banner.svelte';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from './ui/contextMenu.js';
  import EmptyState from './ui/EmptyState.svelte';
  import {
    needsRePoint,
    createProjectsController,
    createProjectsState,
    hasManageChanges,
  } from '$lib/projectsView.js';
  import StatusChip from './ui/StatusChip.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { writeClipboardText } from '$lib/clipboard.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
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
  import { projectRowDetails } from './projects/projectLabels.js';

  const noop = () => {};

  let {
    // The place is the shown Project (`[projectId]`); an empty place shows
    // the shared managed Project or the first one.
    navigation = createStandaloneNavigation(),
    // Seeds the empty place; the place decides what the view shows.
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
    targetProjectId: () => navigation.place[0] ?? '',
    onProjectSelected: (projectId) => onProjectSelected(projectId),
    // Showing the Project just added is a step.
    onProjectAdded: (projectId) => navigation.navigate([projectId]),
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
          title: t('common.alreadySaved'),
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

  // Place -> shown Project. Every list load shows the Project the place
  // names when it is listed; a place naming no listed Project waits for a
  // load in flight, else the entry is corrected to the shown Project.
  $effect(() => {
    const projectId = navigation.place[0] ?? '';
    untrack(() => {
      if (!projectsState.projectsLoaded) return;
      const listed = (id) =>
        Boolean(id) &&
        projectsState.projects.some((project) => project.project_id === id);
      if (!projectId) {
        const fallback =
          [preferredProjectId, projectsState.selectedProjectId].find(listed) ??
          projectsState.projects[0]?.project_id ??
          '';
        if (fallback && fallback !== projectsState.selectedProjectId)
          projectsController.selectProject(fallback);
        showSelectionInPlace();
        return;
      }
      if (listed(projectId)) {
        if (projectId !== projectsState.selectedProjectId)
          projectsController.selectProject(projectId);
        return;
      }
      if (!projectsState.loadingProjects) showSelectionInPlace();
    });
  });

  // Shown Project -> place. A list load that showed another Project (the
  // place named a removed one, or was empty) corrects the current entry.
  $effect(() => {
    const projectId = projectsState.selectedProjectId;
    if (!projectsState.projectsLoaded || projectsState.loadingProjects) return;
    untrack(() => showSelectionInPlace(projectId));
  });

  function showSelectionInPlace(projectId = projectsState.selectedProjectId) {
    if ((navigation.place[0] ?? '') === projectId) return;
    navigation.replace(projectId ? [projectId] : []);
  }

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

  function removeDisabled(project) {
    return (
      projectsState.removingProjectId === project.project_id ||
      (project.project_id === projectsState.selectedProjectId &&
        projectsState.editSaving)
    );
  }

  // The open row context menu (./ui/ContextMenu.svelte), or null.
  let menu = $state(null);

  // Copy path, Re-point... (only when the repository is missing) | Remove...
  function projectMenu(project) {
    const items = [
      {
        id: 'copy-path',
        label: t('projects.menu.copyPath'),
        group: 'project',
        onSelect: () => void copyProjectPath(project.cwd),
      },
    ];
    if (needsRePoint(project))
      items.push({
        id: 're-point',
        label: t('projects.menu.rePoint'),
        group: 'project',
        onSelect: () => openRePoint(project),
      });
    items.push({
      id: 'remove',
      label: t('projects.menu.remove'),
      danger: true,
      group: 'remove',
      disabled: removeDisabled(project),
      onSelect: () => removeOne(project),
    });
    return {
      label: t('projects.menu.label', {
        name: project.display_name || project.project_id,
      }),
      items,
    };
  }

  function openMenu(project, event) {
    event.preventDefault();
    menu = { ...contextMenuAnchor(event), ...projectMenu(project) };
  }

  async function copyProjectPath(path) {
    try {
      await writeClipboardText(path);
      onToast({ title: t('projects.menu.pathCopied'), variant: 'success' });
    } catch {
      onToast({ title: t('projects.menu.copyFailed'), variant: 'error' });
    }
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
          {t('projects.title')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="tertiary"
            icon
            ariaLabel={t('projects.add.title')}
            tooltip={t('projects.add.title')}
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
            {t('projects.loading')}
          </p>
        {:else if !hasProjects}
          <EmptyState
            title={t('projects.emptyTitle')}
            description={t('projects.emptySubtitle')}
          />
        {:else}
          {#each projectsState.projects as project (project.project_id)}
            <button
              type="button"
              class="project-item secondary-list__item"
              class:active={project.project_id ===
                projectsState.selectedProjectId}
              data-testid={`project-toggle-${project.project_id}`}
              use:tooltip={() => projectRowDetails(project)}
              onclick={() => navigation.navigate([project.project_id])}
              oncontextmenu={(event) => openMenu(project, event)}
              onkeydown={(event) => {
                if (isContextMenuKey(event)) openMenu(project, event);
              }}
            >
              <span class="project-item-inner">
                <span class="project-item-head">
                  <span class="project-item-name">
                    {project.display_name || project.project_id}
                  </span>
                  {#if needsRePoint(project)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title')}
                    </StatusChip>
                  {/if}
                </span>
                <span class="project-item-cwd">{project.cwd}</span>
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
          title={t('projects.detail.empty')}
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
                        {t('projects.rePoint.title')}
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
                      {t('projects.rePoint.submit')}
                    </Button>
                  {/if}
                  <Button
                    variant="danger"
                    data-testid={`project-remove-${selectedProject.project_id}`}
                    disabled={removeDisabled(selectedProject)}
                    onClick={() => removeOne(selectedProject)}
                  >
                    {t('projects.remove')}
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
                    ? t('projects.repository.rescanning')
                    : t('projects.repository.rescan')}
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
  <ContextMenu {menu} onClose={() => (menu = null)} />
</section>
